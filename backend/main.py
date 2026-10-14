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
   4. Nunca travar nem cair se o T-Beam sumir: marca "offline": true nas
      mensagens enviadas ao frontend e segue tentando reconectar em background.
   5. Zerar a sessao sob demanda (POST /sessao/nova), sem reiniciar o processo.
   6. Gravar cada torra num CSV em torras/, linha a linha, e marcar eventos
      (1o crack, descarga...) por POST /evento.

 O frontend NUNCA fala direto com o T-Beam - so com este backend.

 SO LEITURA: nada vai do backend para a placa. O torrador e uma pipoqueira
 de ar quente que a placa nao aciona - ela so mede.

 DUAS FORMAS DE MENSAGEM saem daqui, separadas pelo campo "tipo":
   "leitura" - uma leitura nova do T-Beam. Tem "temp" e "recebido_em", e e
               isto (e so isto) que o frontend plota no grafico.
   "tick"    - aviso de estado da conexao, SEM leitura nenhuma. Enquanto a
               placa esta fora, e o tick que mantem a flag "offline" viva na
               tela; como ele nao carrega "temp" nem "recebido_em", o frontend
               nao o confunde com dado novo nem conta um ponto a mais.

 Configuracao por variavel de ambiente (todas opcionais):
   TBEAM_HOST     IP do T-Beam na rede        (padrao: 192.168.0.50)
   TBEAM_PORT     porta HTTP/WebSocket        (padrao: 80)
   TBEAM_WS_PATH  caminho do WebSocket        (padrao: /ws)
   TORRADOR_TORRAS_DIR  pasta dos CSVs das torras (padrao: torras/ na raiz)
============================================================================
"""

import asyncio
import csv
import json
import logging
import math
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Optional

import websockets
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# O modulo de visao mora na raiz do repositorio, um nivel acima daqui.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# IMPORTADO ASSIM DE PROPOSITO. visao/ depende de opencv, numpy e scikit-learn;
# um backend instalado so com as dependencias de rede sobe sem eles e tem de
# continuar entregando temperatura. Um problema de visao nunca pode derrubar a
# telemetria -- e a mesma regra que agtron.iniciar() ja segue por dentro.
try:
    from visao import agtron
    ERRO_IMPORT_VISAO = None
except Exception as e:                                # noqa: BLE001
    agtron = None
    ERRO_IMPORT_VISAO = f"modulo visao nao pode ser importado: {e}"

# --------------------------------------------------------------------------
# Configuracao
# --------------------------------------------------------------------------
TBEAM_HOST = os.getenv("TBEAM_HOST", "192.168.0.50")
TBEAM_PORT = int(os.getenv("TBEAM_PORT", "80"))
TBEAM_WS_PATH = os.getenv("TBEAM_WS_PATH", "/ws")
TBEAM_URI = f"ws://{TBEAM_HOST}:{TBEAM_PORT}{TBEAM_WS_PATH}"

# Cada torra vira um CSV proprio. O historico em memoria some se o backend
# cair ou se alguem zerar a sessao; o arquivo nao. Gravado linha a linha, com
# flush, para que um travamento no meio da torra perca no maximo um segundo.
RAIZ_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TORRAS_DIR = os.getenv("TORRADOR_TORRAS_DIR", os.path.join(RAIZ_REPO, "torras"))
COLUNAS_CSV = ["tempo_torra_s", "temp_c", "hora", "agtron_estimado",
               "agtron_medido", "evento"]

# Eventos que o painel pode marcar. Lista fechada para os CSVs de torras
# diferentes continuarem comparaveis.
EVENTOS_VALIDOS = {
    "amarelamento": "amarelamento",
    "crack1": "1º crack",
    "crack2": "2º crack",
    "descarga": "descarga",
}

INTERVALO_RECONEXAO_S = 3.0   # espera entre tentativas de reconexao

# Vigia de dado parado. O T-Beam manda 1 leitura por segundo, entao 5 s de
# silencio sao cinco leituras perdidas em sequencia: isso e queda, nao atraso.
# Este vigia e INDEPENDENTE do ping/pong do WebSocket - o ping so denuncia um
# socket que morreu calado depois de ping_interval + ping_timeout (ate ~40 s),
# e ate la a tela mostraria a ultima temperatura como se estivesse tudo bem.
LIMITE_DADO_PARADO_S = 5.0    # sem leitura por mais que isto -> offline
INTERVALO_VIGIA_S = 1.0       # de quanto em quanto tempo o vigia confere

# Um frontend travado (notebook suspenso, Wi-Fi caindo sem fechar o TCP) nao
# pode prender o broadcast dos outros: quem nao aceitar a mensagem dentro
# deste prazo e descartado. Ele reconecta sozinho depois - o frontend ja tem
# reconexao automatica - e volta a receber tudo, inclusive o historico.
TIMEOUT_ENVIO_CLIENTE_S = 2.0

# Valores do campo "tipo" das mensagens enviadas ao frontend (ver docstring).
TIPO_LEITURA = "leitura"
TIPO_TICK = "tick"

# --------------------------------------------------------------------------
# Visao (Agtron MEDIDO, por camera)
# --------------------------------------------------------------------------
# TORRADOR_VISAO=0 desliga sem desinstalar nada -- util na bancada de rede,
# onde a camera nao esta e nao ha o que medir.
VISAO_LIGADA = os.getenv("TORRADOR_VISAO", "1").strip().lower() not in (
    "0", "false", "nao", "no")

# A captura roda em laco PROPRIO, e nao dentro de processar_leitura(). A razao
# e a mesma que faz o vigia de dado parado existir: a cadeia de temperatura nao
# pode ficar refem de outro subsistema. Uma captura demora ~30 ms quando tudo
# vai bem, mas o caminho dshow fala com um processo do Windows por um pipe, e
# pipe trava. Acoplado ao laco de leitura, um travamento desses seguraria a
# mensagem de temperatura de 1 s -- exatamente o que nao pode acontecer no meio
# de uma torra. Desacoplado, a temperatura segue e o campo medido envelhece.
VISAO_INTERVALO_S = float(os.getenv("TORRADOR_VISAO_INTERVALO", "2.0"))

# Depois de quanto tempo sem captura nova a ultima medida deixa de ser
# publicada. Sem isto, uma camera que parou entregaria para sempre o mesmo
# Agtron, que envelheceria em silencio enquanto o cafe escurece na tela ao lado.
VISAO_VALIDADE_S = 10.0

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
        # Conexao WebSocket aberta com o T-Beam (None enquanto nao houver).
        self.conexao_tbeam: Optional[Any] = None
        # Clientes (frontends) conectados no nosso proprio WebSocket.
        self.clientes: set[WebSocket] = set()
        # Instante da primeira leitura da sessao. E o nosso "CHARGE" (o
        # momento em que o grao entra no torrador). Por enquanto ele e
        # implicito: assume-se que a torra comeca quando o backend recebe a
        # primeira leitura. Se um dia houver um botao de "iniciar torra" no
        # painel, e este campo que ele passa a definir.
        self.inicio_torra: Optional[float] = None
        # Eventos marcados nesta sessao: [{"evento", "tempo_torra", "temp"}].
        self.eventos: list[dict[str, Any]] = []
        # CSV da torra atual. Aberto na primeira leitura, fechado ao zerar.
        self.arquivo_csv: Optional[Any] = None
        self.escritor_csv: Optional[Any] = None
        self.caminho_csv: Optional[str] = None
        # Menor Agtron ja atingido na sessao (ver processar_leitura).
        self.agtron_estimado: Optional[float] = None
        # Relogio MONOTONICO da ultima leitura valida aceita - ou do instante
        # em que a conexao abriu, como carencia para a primeira chegar. E o
        # que o vigia compara. Monotonico de proposito: um ajuste do relogio
        # do sistema no meio da torra nao pode simular uma queda.
        self.ultimo_dado_em: Optional[float] = None
        # Ultima vez que o vigia reavisou o frontend de que o dado parou.
        self.ultimo_aviso_parado: float = 0.0
        # Ultima leitura da camera (dict de visao/agtron.ler()) e o relogio
        # monotonico em que ela chegou. None enquanto nao houver medida.
        self.visao_leitura: Optional[dict[str, Any]] = None
        self.visao_lida_em: Optional[float] = None
        # Por que nao ha medida. Fica visivel no /status e em toda mensagem:
        # ausencia de medida com motivo e informacao; ausencia calada e bug.
        self.visao_motivo: Optional[str] = "visao ainda nao iniciada"


estado = Estado()


def agtron_medido_agora() -> tuple[Optional[float], Optional[str]]:
    """O Agtron MEDIDO valido neste instante, e o motivo quando nao ha um.

    Medida vencida conta como ausencia de medida. A alternativa -- seguir
    publicando o ultimo valor -- e pior do que nao publicar nada: o numero
    continuaria na tela com cara de atual enquanto o cafe escurece, e ninguem
    teria como perceber que ele congelou.
    """
    if estado.visao_leitura is None or estado.visao_lida_em is None:
        return None, estado.visao_motivo
    idade = time.monotonic() - estado.visao_lida_em
    if idade > VISAO_VALIDADE_S:
        return None, f"ultima medida ha {idade:.0f} s (limite {VISAO_VALIDADE_S:.0f} s)"
    return estado.visao_leitura["agtron"], None


def gravar_csv(linha: dict[str, Any]) -> None:
    """Acrescenta uma linha ao CSV da torra atual, abrindo-o se preciso.

    Falha de disco vira aviso no log, nunca excecao: perder o arquivo e ruim,
    mas parar de mostrar a temperatura no meio da torra e pior.
    """
    try:
        if estado.escritor_csv is None:
            os.makedirs(TORRAS_DIR, exist_ok=True)
            nome = datetime.now().strftime("torra_%Y-%m-%d_%H%M%S.csv")
            estado.caminho_csv = os.path.join(TORRAS_DIR, nome)
            estado.arquivo_csv = open(estado.caminho_csv, "w", newline="",
                                      encoding="utf-8")
            estado.escritor_csv = csv.DictWriter(estado.arquivo_csv,
                                                 fieldnames=COLUNAS_CSV)
            estado.escritor_csv.writeheader()
            log.info("Gravando a torra em %s", estado.caminho_csv)
        estado.escritor_csv.writerow({c: linha.get(c, "") for c in COLUNAS_CSV})
        estado.arquivo_csv.flush()
    except OSError as erro:
        log.warning("Nao consegui gravar o CSV da torra: %s", erro)


def fechar_csv() -> None:
    if estado.arquivo_csv is not None:
        try:
            estado.arquivo_csv.close()
        except OSError:
            pass
    estado.arquivo_csv = None
    estado.escritor_csv = None
    estado.caminho_csv = None


def montar_mensagem() -> dict[str, Any]:
    """Mensagem de LEITURA: ultima leitura + flag de conexao.

    So deve ser usada quando ha uma leitura de verdade para anunciar. Para
    avisar o frontend de mudanca de conexao sem dado novo, use montar_tick().
    """
    base: dict[str, Any] = {
        "temp": None,
        "timestamp": None,
        "agtron_estimado": None,
        # Campo NOVO, ao lado do estimado -- nunca no lugar dele. Enquanto a
        # medida nao estiver provada em torra, os dois numeros convivem na
        # tela, escritos com todas as letras. A heuristica so sai depois.
        "agtron_medido": None,
    }
    if estado.ultima_leitura:
        base.update(estado.ultima_leitura)
    base["tipo"] = TIPO_LEITURA
    base["offline"] = estado.offline
    base["tbeam_uri"] = TBEAM_URI
    base["visao_motivo"] = agtron_medido_agora()[1]
    base["eventos"] = estado.eventos
    return base


def montar_tick() -> dict[str, Any]:
    """Aviso de estado da conexao, SEM leitura nenhuma.

    Vai para o frontend enquanto a placa esta fora (mantendo a flag "offline"
    viva na tela) e no instante em que a conexao volta.

    O que ele NAO carrega e o ponto todo: nada de "temp", nada de
    "recebido_em". Reemitir a leitura velha a cada tick fazia o frontend
    plotar de novo um ponto que ele ja tinha - o contador de leituras subia
    sozinho durante uma queda e as duplicatas comiam o limite da serie do
    grafico. Sem esses campos, o frontend nao tem como confundir o tick com
    dado novo. A ultima temperatura recebida continua na tela normalmente:
    ela vive no estado do frontend, nao nesta mensagem.
    """
    return {
        "tipo": TIPO_TICK,
        "offline": estado.offline,
        "tbeam_uri": TBEAM_URI,
        # Estado da visao viaja no tick tambem: ele nao e dado de leitura, e
        # condicao do sistema. Com a placa fora, e a unica mensagem que chega.
        "visao_motivo": agtron_medido_agora()[1],
    }


# Tarefas soltas de encerramento de cliente. Guardadas numa referencia forte
# porque o asyncio so mantem weakrefs para as tasks: sem isto o coletor de
# lixo pode engolir uma delas no meio do caminho.
_encerramentos: set[asyncio.Task] = set()


async def _encerrar_cliente(cliente: WebSocket) -> None:
    """Fecha um cliente descartado, sem deixar o fechamento travar tambem."""
    try:
        await asyncio.wait_for(cliente.close(), timeout=TIMEOUT_ENVIO_CLIENTE_S)
    except Exception:
        pass


def _descartar(cliente: WebSocket, motivo: str) -> None:
    estado.clientes.discard(cliente)
    log.warning("Frontend descartado (%s) - %d restantes", motivo, len(estado.clientes))
    tarefa = asyncio.create_task(_encerrar_cliente(cliente))
    _encerramentos.add(tarefa)
    tarefa.add_done_callback(_encerramentos.discard)


async def enviar_para_todos(mensagem: dict[str, Any]) -> None:
    """Broadcast para todos os frontends, descartando os que morreram.

    Cada envio tem prazo proprio (TIMEOUT_ENVIO_CLIENTE_S). Um cliente que nao
    aceita a mensagem dentro dele nao esta necessariamente morto - um notebook
    que entrou em suspensao ou perdeu o Wi-Fi sem fechar o TCP simplesmente
    para de drenar o socket, e o "send" fica pendurado para sempre. Como este
    broadcast roda DENTRO do laco de leitura do T-Beam, esperar por ele
    congelaria o backend inteiro: os outros frontends parariam de receber e o
    historico pararia de crescer. Entao o lento e cortado e segue o jogo; o
    frontend dele reconecta sozinho e recebe o historico completo na volta.
    """
    if not estado.clientes:
        return
    try:
        # allow_nan=False: NaN/Infinity viram os literais "NaN"/"Infinity", que
        # nao sao JSON valido - o JSON.parse do navegador rejeita e a mensagem
        # some sem aviso. Melhor falhar aqui, onde da para logar.
        texto = json.dumps(mensagem, allow_nan=False)
    except ValueError as erro:
        log.warning("Mensagem nao serializavel descartada (%s)", erro)
        return

    for cliente in list(estado.clientes):
        try:
            await asyncio.wait_for(
                cliente.send_text(texto), timeout=TIMEOUT_ENVIO_CLIENTE_S
            )
        except asyncio.TimeoutError:
            _descartar(cliente, f"nao respondeu em {TIMEOUT_ENVIO_CLIENTE_S:.0f}s")
        except Exception:
            _descartar(cliente, "conexao quebrada")


# --------------------------------------------------------------------------
# Ponte: cliente WebSocket do T-Beam, com reconexao automatica
# --------------------------------------------------------------------------
def temperatura_valida(valor: Any) -> Optional[float]:
    """Devolve o "temp" como float utilizavel, ou None se nao servir.

    Esta funcao existe por um motivo bem concreto: o MAX31855 desconectado -
    que e como o termopar vai passar boa parte do tempo enquanto o hardware
    nao esta montado - nao produz um campo ausente. Ele produz LIXO com o
    campo presente: null, "nan", uma string, um NaN. Sem esta peneira, esse
    lixo chegava na aritmetica do Agtron, levantava TypeError, subia ate o
    tratamento de erro da ponte e DERRUBAVA a conexao com a placa - que
    reconectava, reencontrava o mesmo lixo e caia de novo, em loop.

    Recusa, nesta ordem:
      - bool  (True/False sao int em Python; "temp": true nao e 1 grau)
      - qualquer coisa que nao seja int/float (None, str, list, dict)
      - NaN e Infinity (numeros que existem em Python mas nao em JSON)
    """
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    numero = float(valor)
    if not math.isfinite(numero):
        return None
    return numero


async def processar_leitura(bruta: str) -> None:
    try:
        leitura = json.loads(bruta)
    except json.JSONDecodeError:
        log.warning("Leitura descartada (JSON invalido): %r", bruta[:120])
        return

    # JSON valido nao quer dizer OBJETO: "[1,2]", "42" e "null" sao todos JSON
    # perfeitamente valido. Sem esta checagem, a atribuicao logo abaixo
    # levantaria TypeError e derrubaria a conexao com a placa.
    if not isinstance(leitura, dict):
        log.warning(
            "Leitura descartada (esperava objeto JSON, veio %s): %r",
            type(leitura).__name__,
            bruta[:120],
        )
        return

    temp = temperatura_valida(leitura.get("temp"))
    if temp is None:
        log.warning("Leitura descartada (temp invalida): %r", bruta[:120])
        return
    # Normaliza: daqui para baixo (historico, frontend, grafico) "temp" e
    # sempre um float finito, nunca um int nem um numero em forma de texto.
    leitura["temp"] = temp

    # A partir daqui a leitura e valida: o vigia pode recomecar a contar.
    estado.ultimo_dado_em = time.monotonic()
    if estado.offline:
        estado.offline = False
        log.info("T-Beam voltou a enviar leituras")

    # Carimbo do backend: o timestamp do T-Beam e millis() desde o boot dele,
    # entao guardamos tambem o relogio real para o grafico e para logs.
    leitura["recebido_em"] = time.time()

    # ---- estimativa de Agtron -------------------------------------------
    if estado.inicio_torra is None:
        estado.inicio_torra = leitura["recebido_em"]
    tempo_decorrido = leitura["recebido_em"] - estado.inicio_torra

    agtron = estimar_agtron(temp, tempo_decorrido)

    # Trava de sentido unico: torrar e irreversivel - grao nao clareia. Sem
    # isto, o calor diminuindo faria o Agtron SUBIR na tela, o que nao existe
    # no mundo real. A trava vive aqui, e nao dentro de estimar_agtron, para
    # que aquela funcao continue sendo um calculo puro e facil de trocar.
    if estado.agtron_estimado is not None:
        agtron = min(agtron, estado.agtron_estimado)
    estado.agtron_estimado = agtron

    leitura["agtron_estimado"] = agtron

    # ---- Agtron MEDIDO, se a visao tiver algo a dizer --------------------
    # Sem trava de sentido unico aqui, ao contrario do estimado. A trava existe
    # porque a heuristica e monotona por construcao e subir seria um artefato
    # dela. A medida nao: se ela sobe, ou o cafe foi trocado, ou a mira saiu do
    # lugar, ou a luz mudou. Forcar monotonicidade esconderia justamente os
    # tres. Medida se publica como veio.
    medido, _motivo = agtron_medido_agora()
    leitura["agtron_medido"] = medido

    leitura["tempo_torra"] = round(tempo_decorrido, 1)
    # ---------------------------------------------------------------------

    estado.ultima_leitura = leitura
    estado.historico.append(leitura)
    gravar_csv({
        "tempo_torra_s": leitura["tempo_torra"],
        "temp_c": temp,
        "hora": datetime.fromtimestamp(leitura["recebido_em"]).strftime("%H:%M:%S"),
        "agtron_estimado": agtron,
        "agtron_medido": "" if medido is None else medido,
    })

    await enviar_para_todos(montar_mensagem())


async def laco_visao() -> None:
    """Mede o Agtron pela camera no proprio ritmo, sem tocar no laco de leitura.

    Tudo que pode falhar aqui -- modulo ausente, modelo nao treinado, camera
    fora, mira estourada -- vira um MOTIVO guardado no estado, nunca uma
    excecao que suba. O backend existe para entregar temperatura; a visao e um
    acrescimo, e acrescimo que derruba o principal nao e acrescimo.

    A inicializacao roda em thread porque trava a camera falando com um
    processo do Windows, o que pode levar dezenas de segundos. No laco de
    eventos, isso seguraria o backend inteiro no startup.

    SO HA UMA TENTATIVA DE INICIAR. Se ela falhar, o motivo fica publicado e o
    laco encerra em vez de insistir: reiniciar exige reabrir a camera, e uma
    retentativa periodica que gasta dezenas de segundos cada uma faria o
    backend passar a vida tentando. Na bancada, o gesto certo e resolver o que
    o motivo aponta e subir o backend de novo.
    """
    if not VISAO_LIGADA:
        estado.visao_motivo = "desligada por TORRADOR_VISAO=0"
        log.info("Visao desligada por variavel de ambiente.")
        return

    if agtron is None:
        estado.visao_motivo = ERRO_IMPORT_VISAO
        log.warning("Visao indisponivel: %s", ERRO_IMPORT_VISAO)
        return

    motivo = await asyncio.to_thread(agtron.iniciar)
    if motivo:
        estado.visao_motivo = motivo
        log.warning("Visao nao iniciou: %s", motivo)
        return

    estado.visao_motivo = None
    log.info("Visao iniciada. Medindo a cada %.1f s", VISAO_INTERVALO_S)
    try:
        while True:
            leitura = await asyncio.to_thread(agtron.ler)
            if leitura is None:
                estado.visao_leitura = None
                estado.visao_lida_em = None
                novo_motivo = agtron.motivo_indisponivel()
                if novo_motivo != estado.visao_motivo:
                    log.warning("Visao parou de medir: %s", novo_motivo)
                estado.visao_motivo = novo_motivo
            else:
                estado.visao_leitura = leitura
                estado.visao_lida_em = time.monotonic()
                estado.visao_motivo = None
            await asyncio.sleep(VISAO_INTERVALO_S)
    except asyncio.CancelledError:
        raise
    finally:
        await asyncio.to_thread(agtron.encerrar)


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
                # Carencia: o vigia so comeca a cobrar a primeira leitura a
                # partir de agora, e nao desde a ultima da conexao anterior.
                estado.ultimo_dado_em = time.monotonic()
                log.info("T-Beam CONECTADO")
                # Tick, e nao leitura: a conexao voltou, mas dado novo ainda
                # nao chegou. Mandar a leitura velha aqui faria o frontend
                # plotar outra vez um ponto que ele ja tem.
                await enviar_para_todos(montar_tick())

                async for bruta in conexao:
                    # Rede de seguranca: processar_leitura ja peneira payload
                    # ruim sozinha, mas um erro inesperado aqui dentro nao
                    # pode quebrar o "async for" e derrubar uma conexao que
                    # esta viva. Descarta-se a leitura, nunca a conexao.
                    try:
                        await processar_leitura(bruta)
                    except Exception as erro:
                        log.warning(
                            "Leitura descartada (%s: %s)", type(erro).__name__, erro
                        )

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
        # E um TICK - nao ha leitura nova para anunciar aqui.
        await enviar_para_todos(montar_tick())
        estado.ultimo_aviso_parado = time.monotonic()
        await asyncio.sleep(INTERVALO_RECONEXAO_S)


async def vigia_dado_parado() -> None:
    """Marca offline quando as leituras param, mesmo com o socket vivo.

    A ponte so descobre uma queda quando o socket acusa: ou o TCP fecha, ou o
    ping/pong estoura. Quando o Wi-Fi da placa cai, nada disso acontece de
    imediato - o socket fica aberto, calado, e o ping so o condena depois de
    ping_interval + ping_timeout (ate ~40 s). Nesses 40 s a tela mostrava
    "cadeia completa" com a temperatura congelada, que e exatamente o que nao
    se pode fazer na frente de quem esta acompanhando uma torra.

    Este vigia olha so para o dado: se a ultima leitura valida ja tem mais de
    LIMITE_DADO_PARADO_S, o T-Beam esta fora para todos os efeitos praticos.
    A conexao em si continua por conta da ponte; quando a leitura
    voltar, processar_leitura desmarca o offline sozinha.
    """
    while True:
        await asyncio.sleep(INTERVALO_VIGIA_S)

        if estado.ultimo_dado_em is None:
            continue  # nenhuma conexao ainda: nao ha o que vigiar
        parado_ha = time.monotonic() - estado.ultimo_dado_em
        if parado_ha < LIMITE_DADO_PARADO_S:
            continue

        if not estado.offline:
            estado.offline = True
            log.warning(
                "Sem leitura ha %.1fs - T-Beam considerado OFFLINE "
                "(socket ainda aberto: %s)",
                parado_ha,
                estado.conexao_tbeam is not None,
            )
            await enviar_para_todos(montar_tick())
            estado.ultimo_aviso_parado = time.monotonic()
        elif estado.conexao_tbeam is not None:
            # Socket aberto porem mudo: a ponte esta parada no "async for" e
            # nao reavisa ninguem, entao o tick periodico e responsabilidade
            # daqui. Se o socket ja tivesse caido, a propria ponte reavisaria
            # a cada tentativa de reconexao - e nos calariamos para nao
            # duplicar o aviso.
            if time.monotonic() - estado.ultimo_aviso_parado >= INTERVALO_RECONEXAO_S:
                await enviar_para_todos(montar_tick())
                estado.ultimo_aviso_parado = time.monotonic()


# --------------------------------------------------------------------------
# Aplicacao FastAPI
# --------------------------------------------------------------------------
@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    tarefas = [
        asyncio.create_task(ponte_tbeam(), name="ponte_tbeam"),
        asyncio.create_task(vigia_dado_parado(), name="vigia_dado_parado"),
        asyncio.create_task(laco_visao(), name="laco_visao"),
    ]
    log.info("Backend no ar. Frontend deve conectar em /ws/temperatura")
    try:
        yield
    finally:
        for tarefa in tarefas:
            tarefa.cancel()
        await asyncio.gather(*tarefas, return_exceptions=True)
        fechar_csv()


app = FastAPI(title="Torrador de Cafe - Backend T-Beam", lifespan=ciclo_de_vida)

# O frontend Vite roda em outra porta (5173), entao precisa de CORS liberado.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/status")
async def status() -> dict[str, Any]:
    sem_leitura_ha = (
        None
        if estado.ultimo_dado_em is None
        else round(time.monotonic() - estado.ultimo_dado_em, 1)
    )
    return {
        "offline": estado.offline,
        "tbeam_uri": TBEAM_URI,
        "leituras_no_historico": len(estado.historico),
        "csv_da_torra": estado.caminho_csv,
        "eventos": estado.eventos,
        "frontends_conectados": len(estado.clientes),
        # Diagnostico de bancada: com o socket aberto e este numero crescendo,
        # o problema e a placa ter emudecido, nao a rede ter caido.
        "socket_tbeam_aberto": estado.conexao_tbeam is not None,
        "sem_leitura_ha": sem_leitura_ha,
        "ultima_leitura": estado.ultima_leitura,
        "visao": {
            "ligada": VISAO_LIGADA,
            "medindo": agtron_medido_agora()[0] is not None,
            "motivo": agtron_medido_agora()[1],
            "ultima": estado.visao_leitura,
        },
    }


@app.get("/historico")
async def historico() -> dict[str, Any]:
    """Curva completa da sessao atual (para montar o grafico de uma vez)."""
    return {"total": len(estado.historico), "leituras": estado.historico}


@app.post("/sessao/nova")
async def sessao_nova() -> dict[str, Any]:
    """Zera a sessao de torra sem reiniciar o backend.

    Serve para encadear demonstracoes: entre uma torra e outra, isto devolve o
    sistema ao estado "nada torrado ainda" - historico vazio, cronometro da
    torra zerado e Agtron solto da trava de sentido unico - sem derrubar o
    processo, sem derrubar os frontends conectados e sem mexer na conexao com
    o T-Beam, que segue exatamente como estava.

    Zerar inicio_torra importa mais do que parece: ele e o CHARGE implicito da
    sessao. Com o backend ligado desde a montagem do equipamento, o "tempo de
    torra" ja chegaria saturado no cafe seguinte e a estimativa de Agtron
    escureceria rapido demais.

    O broadcast final leva "historico": [], que e exatamente o formato que o
    frontend ja usa para redesenhar a curva do zero - nenhuma mudanca do lado
    dele foi necessaria para isto funcionar.
    """
    descartadas = len(estado.historico)

    # Descartadas da MEMORIA. O CSV da torra anterior fica no disco, fechado;
    # a proxima leitura abre um arquivo novo.
    arquivo_anterior = estado.caminho_csv
    fechar_csv()

    estado.historico = []
    estado.eventos = []
    estado.inicio_torra = None
    estado.agtron_estimado = None

    await enviar_para_todos({**montar_mensagem(), "historico": []})

    log.info("Sessao zerada (%d leituras descartadas da memoria)", descartadas)
    return {"ok": True, "leituras_descartadas": descartadas,
            "csv_anterior": arquivo_anterior}


@app.post("/evento/{nome}")
async def marcar_evento(nome: str) -> dict[str, Any]:
    """Marca um evento da torra (1o crack, descarga...) no instante atual.

    O tempo e a temperatura sao os da ULTIMA LEITURA, nao os do clique: e a
    temperatura que estava na tela quando a pessoa ouviu o crack.
    """
    if nome not in EVENTOS_VALIDOS:
        raise HTTPException(404, f"evento desconhecido; use um de {list(EVENTOS_VALIDOS)}")
    if estado.ultima_leitura is None or estado.inicio_torra is None:
        raise HTTPException(409, "nenhuma leitura nesta sessao ainda")

    tempo = round(time.time() - estado.inicio_torra, 1)
    temp = estado.ultima_leitura["temp"]
    marca = {"evento": nome, "rotulo": EVENTOS_VALIDOS[nome],
             "tempo_torra": tempo, "temp": temp}
    estado.eventos.append(marca)
    gravar_csv({
        "tempo_torra_s": tempo,
        "temp_c": temp,
        "hora": datetime.now().strftime("%H:%M:%S"),
        "evento": nome,
    })
    await enviar_para_todos(montar_mensagem())

    log.info("Evento %s em %.0f s, %.1f C", nome, tempo, temp)
    return {"ok": True, **marca}


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
        # Nao esperamos nada do frontend por aqui,
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
