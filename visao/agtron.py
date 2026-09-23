"""
============================================================================
 ESTIMATIVA DE AGTRON POR COR  (substitui a heuristica do backend)
============================================================================

 Le a cor dos graos pela camera e devolve o indice Agtron, usando o modelo
 treinado por calibracao/treinar.py.

 Fluxo de cada leitura:

     captura -> recorta a mira -> CIELAB -> modelo -> media movel -> Agtron

 DIFERENCA PARA O QUE EXISTE HOJE: a funcao estimar_agtron() do backend
 deriva a cor da temperatura e do tempo decorrido -- ela nao olha para o
 cafe. Este modulo mede. Quando nao da para medir (sem camera, sem modelo,
 sem calibracao), ele devolve None e diz por que, em vez de devolver um
 numero plausivel. Um instrumento que finge medir e pior que um que admite
 nao saber.

 Uso no backend:

     from visao import agtron
     agtron.iniciar()                  # uma vez, no startup
     leitura = agtron.ler()            # a cada ciclo; None se indisponivel

 A captura e bloqueante (~30 ms). Num backend assincrono, chame ler() por
 asyncio.to_thread() ou run_in_executor -- segurar o laco de eventos por
 30 ms estragaria a regularidade de 1 s das mensagens de temperatura.
============================================================================
"""

import os
import pickle
import threading
from collections import deque

import numpy as np

from . import camera

# --------------------------------------------------------------------------
CAMINHO_MODELO = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "modelo_agtron.pkl")

# Suavizacao: mesmo com exposicao travada e mira correta, sobra ruido frame a
# frame -- grao girando, reflexo momentaneo, desfoque de movimento. A media
# movel amortece o que passa. Ela COMPLEMENTA a trava de exposicao, nao
# substitui: sem a trava, o que ela suavizaria seria um sinal ja invertido.
JANELA_SUAVIZACAO = 5

# Entradas assumidas quando o modelo nao diz quais usou. So modelo gravado
# antes de o treino passar a escolher o conjunto cai aqui -- era o que o
# treinar.py fazia fixo naquela epoca.
ENTRADAS_PADRAO = ("L", "a", "b")

# Faixas usadas so para rotular a fase na tela. A escala Agtron e invertida:
# valor ALTO e grao CLARO.
FAIXAS_FASE = ((85, "Cru"), (65, "Clara"), (45, "Media"), (-1, "Escura"))

# O que camera.medir() devolve, e portanto o que um modelo pode pedir como
# entrada. Serve para recusar um modelo incompativel no startup.
_COLUNAS_MEDIDAS = ("L", "a", "b", "L_ref", "a_ref", "b_ref", "L_norm",
                    "desvio_L")

_estado = {
    "modelo": None,
    "meta": None,
    "entradas": ENTRADAS_PADRAO,
    "cap": None,
    "formato": None,
    "indisponivel": "nao iniciado",
}
_buffer = deque(maxlen=JANELA_SUAVIZACAO)
_trava = threading.Lock()


def classificar_fase(valor):
    for piso, nome in FAIXAS_FASE:
        if valor >= piso:
            return nome
    return "Escura"


def _carregar_modelo():
    if not os.path.exists(CAMINHO_MODELO):
        return None, (
            f"nenhum modelo treinado em {os.path.basename(CAMINHO_MODELO)} "
            "(rode calibracao/treinar.py depois de coletar as amostras)"
        )
    with open(CAMINHO_MODELO, "rb") as f:
        payload = pickle.load(f)
    return payload, None


def _conferir_condicao_optica(meta):
    """Avisa se a camera hoje nao esta na condicao em que o modelo foi treinado.

    O modelo mapeia cor -> Agtron para UMA condicao optica. Mudar a exposicao,
    o ganho ou a mira move o ponto de operacao e invalida o mapeamento. Sem
    esta conferencia, a mudanca passaria despercebida e o sistema seguiria
    devolvendo numeros com confianca -- que foi exatamente o que aconteceu no
    projeto anterior, onde nao da para saber qual dado treinou o modelo que
    roda.
    """
    atual = camera.assinatura_optica()
    treino = meta.get("optica", {})
    diferencas = [
        f"{chave}: treinado={treino.get(chave)!r} agora={valor!r}"
        for chave, valor in atual.items()
        if chave in treino and treino[chave] != valor
    ]
    return diferencas


def iniciar():
    """Carrega o modelo, trava a camera e deixa o fluxo aberto.

    Nao levanta excecao: um problema de visao nao pode derrubar o backend, que
    tem a telemetria de temperatura para entregar. Registra o motivo e ler()
    passa a devolver None com a explicacao.
    """
    with _trava:
        _buffer.clear()
        payload, erro = _carregar_modelo()
        if erro:
            _estado.update(modelo=None, meta=None, indisponivel=erro)
            return _estado["indisponivel"]

        _estado.update(modelo=payload["modelo"], meta=payload.get("meta", {}))

        # O modelo diz em quais colunas foi treinado, e e nelas que ler() monta
        # o vetor. Conferido AQUI, no startup, e nao na primeira leitura: um
        # modelo pedindo coluna que camera.medir() nao produz e erro de
        # instalacao, e erro de instalacao tem de aparecer quando o sistema
        # sobe, nao no meio de uma torra.
        entradas = tuple(_estado["meta"].get("entradas") or ENTRADAS_PADRAO)
        desconhecidas = [c for c in entradas if c not in _COLUNAS_MEDIDAS]
        if desconhecidas:
            motivo = (f"o modelo foi treinado em {desconhecidas}, que "
                      "camera.medir() nao produz")
            _estado.update(modelo=None, meta=None, indisponivel=motivo)
            return _estado["indisponivel"]
        _estado["entradas"] = entradas
        print(f"[VISAO] modelo treinado em {', '.join(entradas)}")

        # Conferida ANTES de mexer na camera: e uma comparacao entre o modelo e
        # visao/camera.py, nao depende de a camera estar plugada. Deixar para
        # depois esconderia o aviso justamente de quem esta configurando o
        # sistema sem hardware a mao.
        avisos = _conferir_condicao_optica(_estado["meta"])
        if avisos:
            print("[VISAO] AVISO: a condicao optica mudou desde o treino:")
            for linha in avisos:
                print(f"         {linha}")
            print("         As leituras nao sao comparaveis ao que foi calibrado.")
            print("         Recalibre (recolete + treinar.py) ou volte os valores.")

        try:
            travados = camera.travar()
            cap, formato = camera.abrir()
        except (camera.CameraNaoTravou, camera.CameraSemImagem, OSError) as e:
            _estado.update(cap=None, indisponivel=f"camera: {e}")
            return _estado["indisponivel"]

        _estado.update(cap=cap, formato=formato, indisponivel=None)
        print(f"[VISAO] pronta. formato={formato} travas={travados}")
        return None


def encerrar():
    with _trava:
        if _estado["cap"] is not None:
            _estado["cap"].release()
        _estado.update(cap=None, indisponivel="encerrado")


def ler():
    """Uma leitura de Agtron. BLOQUEIA ~30 ms -- chame fora do laco de eventos.

    Devolve dict com agtron, classe, uniformidade e o valor bruto, ou None se
    a visao nao estiver disponivel (o motivo fica em motivo_indisponivel()).
    """
    with _trava:
        if _estado["indisponivel"] or _estado["cap"] is None:
            return None
        try:
            frame = camera.media_de_frames(_estado["cap"])
            medida = camera.medir(frame)
        except camera.CameraSemImagem as e:
            _estado["indisponivel"] = f"camera: {e}"
            return None

        valores = [medida[coluna] for coluna in _estado["entradas"]]
        if not all(np.isfinite(valores)):
            # L_norm vira NaN quando a referencia de cinza le ~0 -- cartao fora
            # do quadro, ou mira de referencia na sombra. Predizer com NaN
            # devolveria NaN, que atravessaria a media movel e sairia como
            # "medida" no painel.
            _estado["indisponivel"] = (
                "medida invalida: " +
                ", ".join(f"{c}={v}" for c, v in zip(_estado["entradas"], valores)
                          if not np.isfinite(v)) +
                " (cartao de cinza fora do quadro?)"
            )
            return None

        entrada = np.array([valores], dtype=float)
        bruto = float(_estado["modelo"].predict(entrada)[0])

        _buffer.append(bruto)
        suavizado = sum(_buffer) / len(_buffer)

        return {
            "agtron": round(suavizado, 2),
            "agtron_bruto": round(bruto, 2),
            "classe": classificar_fase(suavizado),
            # Desvio de L dentro da mira: proxy da uniformidade do lote. Alto
            # pode ser torra desigual -- ou mira pegando fundo, o que vale
            # conferir antes de culpar o cafe.
            "uniformidade": round(medida["desvio_L"], 2),
            "amostras_na_media": len(_buffer),
        }


def motivo_indisponivel():
    return _estado["indisponivel"]


def resetar_suavizacao():
    """Limpa a media movel. Chame ao trocar a mira ou comecar outra torra:
    sem isso, leituras da condicao anterior continuam pesando por ate
    JANELA_SUAVIZACAO chamadas."""
    with _trava:
        _buffer.clear()
