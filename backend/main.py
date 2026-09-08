"""
============================================================================
 TORRADOR DE CAFE - Backend (ponte entre o T-Beam e o frontend)
 CEFET-MG Varginha - Projeto de extensao
============================================================================

    [T-Beam]  --WebSocket-->  [ESTE BACKEND]  --WebSocket-->  [React]
     servidor                  cliente + servidor               cliente
                    ws://<ip>/ws          /ws/temperatura

 Responsabilidades:
   1. Conectar como CLIENTE no WebSocket do T-Beam, com reconexao automatica.
   2. Reexpor as leituras num WebSocket PROPRIO em /ws/temperatura.
   3. Guardar o historico da sessao em memoria (lista simples, sem banco).
   4. Receber POST /comando do frontend e repassar para o T-Beam.
   5. Nunca travar nem cair se o T-Beam sumir: marca "offline": true nas
      mensagens enviadas ao frontend e segue tentando reconectar em background.

 O frontend NUNCA fala direto com o T-Beam - so com este backend.

 Configuracao por variavel de ambiente (todas opcionais):
   TBEAM_HOST     IP do T-Beam na rede        (padrao: 192.168.0.50)
   TBEAM_PORT     porta HTTP/WebSocket        (padrao: 80)
   TBEAM_WS_PATH  caminho do WebSocket        (padrao: /ws)
============================================================================
"""

import asyncio
import json
import logging
import math
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Literal, Optional

import websockets
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Configuracao
# --------------------------------------------------------------------------
TBEAM_HOST = os.getenv("TBEAM_HOST", "192.168.0.50")
TBEAM_PORT = int(os.getenv("TBEAM_PORT", "80"))
TBEAM_WS_PATH = os.getenv("TBEAM_WS_PATH", "/ws")
TBEAM_URI = f"ws://{TBEAM_HOST}:{TBEAM_PORT}{TBEAM_WS_PATH}"

INTERVALO_RECONEXAO_S = 3.0   # espera entre tentativas de reconexao

# --------------------------------------------------------------------------
# Parametros da estimativa de Agtron (ver estimar_agtron abaixo)
# --------------------------------------------------------------------------
AGTRON_CRU = 98.0        # grao verde, antes de comecar a escurecer
AGTRON_ESCURO = 42.0     # piso da escala nesta simulacao (torra escura)
TEMP_INICIO_COR = 140.0  # abaixo disto o grao so seca, praticamente nao escurece
TEMP_PATAMAR = 215.0     # temperatura de torra plena
TAU_DESENVOLVIMENTO = 240.0  # segundos: constante de tempo do escurecimento
PESO_TEMPERATURA = 0.65  # quanto do escurecimento a temperatura sozinha explica
PESO_TEMPO = 0.35        # quanto vem de permanecer no calor (tempo de "development")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("torrador")


# ==========================================================================
#  #############   ESTIMATIVA TEMPORARIA DE AGTRON   #######################
# ==========================================================================
#
#   ESTA FUNCAO NAO MEDE NADA. Ela CHUTA, de forma plausivel, o quanto o
#   grao ja escureceu, a partir de duas coisas que o sistema realmente sabe:
#   a temperatura atual e ha quanto tempo a torra comecou.
#
#   O Agtron de verdade e uma medida OPTICA: le-se a cor do grao. Na fase
#   futura do projeto ele vira do pipeline de visao computacional que ja
#   existe no repositorio principal (camera + modelo que estima o indice a
#   partir da cor). Quando isso acontecer, ESTA funcao e a unica coisa que
#   sai daqui - o campo "agtron_estimado" continua saindo no mesmo lugar do
#   mesmo JSON, e nem o frontend nem o resto do backend mudam.
#
#   POR ISSO: nada fora daqui pode depender de COMO o numero e calculado.
#   Quem consome so recebe o valor pronto. Se voce precisar de um parametro
#   interno desta funcao em outro lugar do sistema, pare - e sinal de que o
#   acoplamento que queremos evitar esta comecando.
#
#   --------------------------------------------------------------------
#   COMO O CHUTE E FEITO
#
#   Dois fatores, ambos entre 0 e 1, multiplicados:
#
#   1) fator_temperatura - o quanto passamos da faixa em que o grao comeca
#      a escurecer (140 C) rumo ao patamar de torra plena (215 C). Abaixo de
#      140 C o grao esta secando, nao dourando: o fator e 0 e o Agtron nao
#      se move. Este fator PORTEIA o resto - sem calor nao ha cor.
#
#   2) fator_tempo - saturacao exponencial 1 - e^(-t/tau). Traduz o fato de
#      que ficar no calor escurece o grao mesmo com a temperatura parada; e
#      o "tempo de desenvolvimento" que o torrador controla no fim da torra.
#
#   progresso = fator_temperatura * (0,65 + 0,35 * fator_tempo)
#
#   Ou seja: chegar ao patamar leva o grao a cerca de 2/3 do caminho
#   (Agtron ~62, torra media); o terco final so vem de PERMANECER la. E o
#   comportamento certo - quem decide entre torra media e escura e o tempo
#   de permanencia, nao o pico de temperatura.
#
#   Agtron = 98 - (98 - 42) * progresso
#
#   Escala invertida de proposito: Agtron ALTO = grao CLARO.
# ==========================================================================
def estimar_agtron(temperatura: float, tempo_decorrido: float) -> float:
    """Estimativa (NAO medicao) do indice Agtron. Ver o bloco acima.

    temperatura     -- graus Celsius da leitura atual
    tempo_decorrido -- segundos desde o inicio da sessao de torra (CHARGE)

    Retorna um valor na escala Agtron, com uma casa decimal.
    """
    # 1) o calor abriu a porta para o escurecimento?
    faixa = TEMP_PATAMAR - TEMP_INICIO_COR
    fator_temperatura = (temperatura - TEMP_INICIO_COR) / faixa
    fator_temperatura = min(1.0, max(0.0, fator_temperatura))

    # 2) ha quanto tempo o grao esta exposto a esse calor
    fator_tempo = 1.0 - math.exp(-max(0.0, tempo_decorrido) / TAU_DESENVOLVIMENTO)

    progresso = fator_temperatura * (PESO_TEMPERATURA + PESO_TEMPO * fator_tempo)

    agtron = AGTRON_CRU - (AGTRON_CRU - AGTRON_ESCURO) * progresso
    return round(agtron, 1)


# --------------------------------------------------------------------------
# Estado em memoria da sessao
# --------------------------------------------------------------------------
class Estado:
    def __init__(self) -> None:
        # Historico completo da sessao. Lista simples de dicionarios -
        # suficiente para montar o grafico da curva inteira depois.
        self.historico: list[dict[str, Any]] = []
        # Ultima leitura recebida do T-Beam (None ate a primeira chegar).
        self.ultima_leitura: Optional[dict[str, Any]] = None
        # True enquanto nao houver conexao viva com o T-Beam.
        self.offline: bool = True
        # Conexao WebSocket aberta com o T-Beam (usada para enviar comandos).
        self.conexao_tbeam: Optional[Any] = None
        # Clientes (frontends) conectados no nosso proprio WebSocket.
        self.clientes: set[WebSocket] = set()
        # Instante da primeira leitura da sessao. E o nosso "CHARGE" (o
        # momento em que o grao entra no torrador). Por enquanto ele e
        # implicito: assume-se que a torra comeca quando o backend recebe a
        # primeira leitura. Se um dia houver um botao de "iniciar torra" no
        # painel, e este campo que ele passa a definir.
        self.inicio_torra: Optional[float] = None
        # Menor Agtron ja atingido na sessao (ver processar_leitura).
        self.agtron_estimado: Optional[float] = None


estado = Estado()


def montar_mensagem() -> dict[str, Any]:
    """Mensagem enviada ao frontend: ultima leitura + flag de conexao."""
    base: dict[str, Any] = {
        "temp": None,
        "timestamp": None,
        "potencia_atual": None,
        "motor_ligado": None,
        "agtron_estimado": None,
    }
    if estado.ultima_leitura:
        base.update(estado.ultima_leitura)
    base["offline"] = estado.offline
    base["tbeam_uri"] = TBEAM_URI
    return base


async def enviar_para_todos(mensagem: dict[str, Any]) -> None:
    """Broadcast para todos os frontends, descartando os que morreram."""
    if not estado.clientes:
        return
    texto = json.dumps(mensagem)
    mortos: list[WebSocket] = []
    for cliente in list(estado.clientes):
        try:
            await cliente.send_text(texto)
        except Exception:
            mortos.append(cliente)
    for cliente in mortos:
        estado.clientes.discard(cliente)


# --------------------------------------------------------------------------
# Ponte: cliente WebSocket do T-Beam, com reconexao automatica
# --------------------------------------------------------------------------
async def processar_leitura(bruta: str) -> None:
    try:
        leitura = json.loads(bruta)
    except json.JSONDecodeError:
        log.warning("Mensagem invalida vinda do T-Beam: %r", bruta[:120])
        return

    # Carimbo do backend: o timestamp do T-Beam e millis() desde o boot dele,
    # entao guardamos tambem o relogio real para o grafico e para logs.
    leitura["recebido_em"] = time.time()

    # ---- estimativa de Agtron -------------------------------------------
    if estado.inicio_torra is None:
        estado.inicio_torra = leitura["recebido_em"]
    tempo_decorrido = leitura["recebido_em"] - estado.inicio_torra

    agtron = estimar_agtron(leitura.get("temp", 0.0), tempo_decorrido)

    # Trava de sentido unico: torrar e irreversivel - grao nao clareia. Sem
    # isto, baixar a potencia faria o Agtron SUBIR na tela, o que nao existe
    # no mundo real. A trava vive aqui, e nao dentro de estimar_agtron, para
    # que aquela funcao continue sendo um calculo puro e facil de trocar.
    if estado.agtron_estimado is not None:
        agtron = min(agtron, estado.agtron_estimado)
    estado.agtron_estimado = agtron

    leitura["agtron_estimado"] = agtron
    leitura["tempo_torra"] = round(tempo_decorrido, 1)
    # ---------------------------------------------------------------------

    estado.ultima_leitura = leitura
    estado.historico.append(leitura)

    await enviar_para_todos(montar_mensagem())


async def ponte_tbeam() -> None:
    """Roda para sempre em background: conecta, escuta, reconecta se cair."""
    while True:
        try:
            log.info("Conectando no T-Beam: %s", TBEAM_URI)
            async with websockets.connect(
                TBEAM_URI,
                open_timeout=5,
                ping_interval=20,
                ping_timeout=20,
            ) as conexao:
                estado.conexao_tbeam = conexao
                estado.offline = False
                log.info("T-Beam CONECTADO")
                await enviar_para_todos(montar_mensagem())

                async for bruta in conexao:
                    await processar_leitura(bruta)

        except asyncio.CancelledError:
            raise
        except Exception as erro:
            # Qualquer falha (host fora do ar, timeout, conexao fechada) cai
            # aqui: logamos de forma curta e tentamos de novo. O backend
            # NUNCA morre por causa disso.
            log.warning("T-Beam indisponivel (%s: %s)", type(erro).__name__, erro)

        # Saiu do "async with" -> a conexao acabou de um jeito ou de outro.
        estado.conexao_tbeam = None
        if not estado.offline:
            estado.offline = True
            log.warning("T-Beam DESCONECTADO - avisando o frontend")

        # Reavisa o frontend a cada tentativa frustrada, e nao so na primeira:
        # enquanto a placa estiver fora, este broadcast se repete a cada
        # INTERVALO_RECONEXAO_S, mantendo a flag "offline" viva na tela.
        await enviar_para_todos(montar_mensagem())
        await asyncio.sleep(INTERVALO_RECONEXAO_S)


# --------------------------------------------------------------------------
# Aplicacao FastAPI
# --------------------------------------------------------------------------
@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    tarefas = [
        asyncio.create_task(ponte_tbeam(), name="ponte_tbeam"),
    ]
    log.info("Backend no ar. Frontend deve conectar em /ws/temperatura")
    try:
        yield
    finally:
        for tarefa in tarefas:
            tarefa.cancel()
        await asyncio.gather(*tarefas, return_exceptions=True)


app = FastAPI(title="Torrador de Cafe - Backend T-Beam", lifespan=ciclo_de_vida)

# O frontend Vite roda em outra porta (5173), entao precisa de CORS liberado.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class Comando(BaseModel):
    """Comando vindo do frontend. Os dois campos sao opcionais."""

    potencia: Optional[int] = Field(default=None, ge=0, le=100)
    motor: Optional[Literal["on", "off"]] = None


@app.get("/status")
async def status() -> dict[str, Any]:
    return {
        "offline": estado.offline,
        "tbeam_uri": TBEAM_URI,
        "leituras_no_historico": len(estado.historico),
        "frontends_conectados": len(estado.clientes),
        "ultima_leitura": estado.ultima_leitura,
    }


@app.get("/historico")
async def historico() -> dict[str, Any]:
    """Curva completa da sessao atual (para montar o grafico de uma vez)."""
    return {"total": len(estado.historico), "leituras": estado.historico}


@app.post("/comando")
async def comando(comando: Comando) -> dict[str, Any]:
    """Recebe o comando do frontend e repassa para o T-Beam.

    O repasse vai pela MESMA conexao WebSocket ja aberta com a placa - por
    isso o firmware aceita comandos por WebSocket (ver o comentario no topo
    de firmware/src/main.cpp).
    """
    payload = comando.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(
            status_code=422,
            detail="Envie ao menos um campo: 'potencia' (0-100) ou 'motor' (on/off).",
        )

    if estado.offline or estado.conexao_tbeam is None:
        raise HTTPException(
            status_code=503,
            detail=f"T-Beam offline ({TBEAM_URI}). Comando nao foi entregue.",
        )

    try:
        await estado.conexao_tbeam.send(json.dumps(payload))
    except Exception as erro:
        raise HTTPException(
            status_code=503,
            detail=f"Falha ao enviar comando para o T-Beam: {erro}",
        ) from erro

    log.info("Comando repassado ao T-Beam: %s", payload)
    return {"ok": True, "enviado": payload}


@app.websocket("/ws/temperatura")
async def ws_temperatura(websocket: WebSocket) -> None:
    """WebSocket que o FRONTEND consome."""
    await websocket.accept()
    estado.clientes.add(websocket)
    log.info("Frontend conectado (%d no total)", len(estado.clientes))

    try:
        # Assim que conecta, manda o estado atual + o historico da sessao,
        # para o grafico ja nascer com a curva inteira em vez de vazio.
        await websocket.send_text(
            json.dumps({**montar_mensagem(), "historico": estado.historico})
        )
        # Nao esperamos nada do frontend por aqui (comandos vao por POST),
        # mas mantemos a leitura viva para detectar a desconexao.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as erro:
        log.warning("Frontend caiu: %s", erro)
    finally:
        estado.clientes.discard(websocket)
        log.info("Frontend desconectado (%d restantes)", len(estado.clientes))
