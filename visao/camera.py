"""
============================================================================
 CAMERA COM CONTROLES TRAVADOS
============================================================================

 Modulo unico de acesso a camera. Usado tanto pela coleta de calibracao
 (calibracao/capturar_amostras.py) quanto pela medicao em producao
 (visao/agtron.py).

 E UM MODULO SO DE PROPOSITO. Se cada um tivesse a sua copia dos controles
 V4L2 e da ROI, as copias divergiriam -- e a ferramenta de calibrar passaria
 a medir uma condicao diferente da que roda na torra, sem ninguem perceber.
 Calibracao e medicao PRECISAM enxergar exatamente a mesma coisa, ou o modelo
 treinado nao vale para o que ele mede depois.

 Por que travar: deixada no automatico, a camera compensa a cena. Cafe escuro
 enche o quadro, ela clareia a imagem, e a medida anda ao contrario da
 realidade -- o proprio sinal que se quer medir e cancelado pelo controle
 automatico. Com os automaticos ligados, nenhum dado coletado presta.
============================================================================
"""

import fcntl
import struct

import cv2
import numpy as np

# --------------------------------------------------------------------------
# Condicao optica. Vale para calibracao E para producao: mexer aqui invalida
# qualquer modelo treinado antes da mudanca.
# --------------------------------------------------------------------------
DISPOSITIVO = "/dev/video0"
LARGURA, ALTURA = 640, 480

EXPOSICAO = 250          # faixa desta camera: 10..2500
TEMPERATURA_COR = 4600   # faixa desta camera: 2800..6500
GANHO = 4                # faixa desta camera: 4..8

# Miras no formato (x_inicial, y_inicial, x_final, y_final).
MIRA_AMOSTRA = (220, 160, 420, 320)      # onde ficam os graos
MIRA_REFERENCIA = (40, 40, 110, 100)     # onde fica o cartao cinza

FRAMES_AQUECIMENTO = 15
FRAMES_POR_CAPTURA = 10

# Identifica a condicao optica inteira. Vai gravado dentro do modelo treinado:
# se alguem mudar a exposicao ou a mira e esquecer de retreinar, da para
# detectar em vez de descobrir por um resultado estranho meses depois.
def assinatura_optica():
    return {
        "dispositivo": DISPOSITIVO,
        "resolucao": f"{LARGURA}x{ALTURA}",
        "exposicao": EXPOSICAO,
        "temperatura_cor": TEMPERATURA_COR,
        "ganho": GANHO,
        "mira_amostra": list(MIRA_AMOSTRA),
        "mira_referencia": list(MIRA_REFERENCIA),
    }


# --------------------------------------------------------------------------
# Controles V4L2 por ioctl
#
# Na mao porque o v4l-utils nao esta instalado e isto evita uma dependencia
# de sistema. Sao dois ioctls e cinco constantes.
# --------------------------------------------------------------------------
VIDIOC_S_CTRL = 0xC008561C
VIDIOC_G_CTRL = 0xC008561B

CID_EXPOSICAO_AUTO = 0x009A0901   # 1 = Manual Mode, 3 = Aperture Priority
CID_EXPOSICAO_ABS = 0x009A0902
CID_WB_AUTO = 0x0098090C
CID_WB_TEMPERATURA = 0x0098091A
CID_GANHO = 0x00980913


class CameraNaoTravou(RuntimeError):
    """Os automaticos nao aceitaram ir para manual. Nao capture nada assim."""


class CameraSemImagem(RuntimeError):
    """A camera respondeu, mas nao entregou imagem utilizavel."""


def _ctrl(fd, requisicao, cid, valor=0):
    buf = bytearray(struct.pack("<Ii", cid, valor))
    fcntl.ioctl(fd, requisicao, buf)
    return struct.unpack("<Ii", bytes(buf))[1]


def travar(dispositivo=DISPOSITIVO):
    """Poe exposicao, white balance e ganho em manual, com valores fixos.

    A ORDEM IMPORTA: exposicao_auto e wb_auto vao para manual primeiro, porque
    enquanto o automatico esta ligado os controles de valor ficam inativos e a
    escrita e silenciosamente ignorada.
    """
    with open(dispositivo, "rb+", buffering=0) as fd:
        _ctrl(fd, VIDIOC_S_CTRL, CID_EXPOSICAO_AUTO, 1)
        _ctrl(fd, VIDIOC_S_CTRL, CID_WB_AUTO, 0)
        _ctrl(fd, VIDIOC_S_CTRL, CID_EXPOSICAO_ABS, EXPOSICAO)
        _ctrl(fd, VIDIOC_S_CTRL, CID_WB_TEMPERATURA, TEMPERATURA_COR)
        _ctrl(fd, VIDIOC_S_CTRL, CID_GANHO, GANHO)

        lidos = {
            "exposicao_auto": _ctrl(fd, VIDIOC_G_CTRL, CID_EXPOSICAO_AUTO),
            "exposicao": _ctrl(fd, VIDIOC_G_CTRL, CID_EXPOSICAO_ABS),
            "wb_auto": _ctrl(fd, VIDIOC_G_CTRL, CID_WB_AUTO),
            "temp_cor": _ctrl(fd, VIDIOC_G_CTRL, CID_WB_TEMPERATURA),
            "ganho": _ctrl(fd, VIDIOC_G_CTRL, CID_GANHO),
        }

    if lidos["exposicao_auto"] != 1 or lidos["wb_auto"] != 0:
        raise CameraNaoTravou(
            f"Os automaticos nao foram para manual: {lidos}. "
            "Capturar assim produziria dados que medem a reacao da camera, "
            "nao o cafe."
        )
    return lidos


def frame_degenerado(frame):
    """Detecta frame que nao contem imagem de verdade.

    Buffer nunca preenchido chega como YUV todo zero, que convertido para BGR
    vira verde saturado e uniforme -- e passaria batido por qualquer media,
    virando um numero com cara de dado. Desvio global proximo de zero denuncia:
    qualquer cena real, ate um fundo liso, tem ruido de sensor.
    """
    return float(np.asarray(frame).std()) < 1.0


def abrir(dispositivo=DISPOSITIVO):
    """Abre a camera preferindo YUYV, com queda para o formato padrao.

    YUYV e preferivel porque MJPG e compressao com perda e mexe justamente na
    cor, que e a unica coisa que medimos aqui. Mas forcar o FOURCC pelo OpenCV
    falha silenciosamente em algumas combinacoes de driver e deixa o fluxo
    devolvendo buffers vazios -- por isso a captura e VERIFICADA antes de o
    objeto ser devolvido, e nao apenas configurada.
    """
    def _tentar(forcar_yuyv):
        cap = cv2.VideoCapture(dispositivo, cv2.CAP_V4L2)
        if not cap.isOpened():
            return None
        # FOURCC antes da resolucao: o backend V4L2 do OpenCV renegocia o
        # formato ao mudar o tamanho, e a ordem inversa desfaz este ajuste.
        if forcar_yuyv:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"YUYV"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, LARGURA)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, ALTURA)
        for _ in range(FRAMES_AQUECIMENTO):
            cap.read()
        ok, frame = cap.read()
        if not ok or frame_degenerado(frame):
            cap.release()
            return None
        return cap

    cap = _tentar(forcar_yuyv=True)
    if cap is not None:
        return cap, "YUYV"

    cap = _tentar(forcar_yuyv=False)
    if cap is not None:
        return cap, "padrao"

    raise CameraSemImagem(
        f"Nao consegui capturar imagem valida de {dispositivo}. "
        "Confira o attach do usbipd, o grupo 'video' e se a camera esta plugada."
    )


def media_de_frames(cap, n=FRAMES_POR_CAPTURA):
    """Media de n frames, para reduzir ruido do sensor."""
    soma = None
    for _ in range(n):
        ok, frame = cap.read()
        if not ok:
            raise CameraSemImagem("Falha ao capturar frame da camera.")
        if frame_degenerado(frame):
            raise CameraSemImagem("Frame sem imagem no meio da captura.")
        f = frame.astype(np.float64)
        soma = f if soma is None else soma + f
    return (soma / n).astype(np.float32)


def lab_da_mira(frame_float, mira):
    """CIELAB real (L 0..100, a/b centrados em 0) da regiao indicada.

    Converte para float normalizado antes do cvtColor: em uint8 o OpenCV
    devolve L reescalado para 0..255 e a/b deslocados de 128, que serve para
    regressao mas atrapalha na hora de interpretar e de normalizar pela
    referencia de cinza.

    Devolve (L, a, b, desvio_de_L).
    """
    x0, y0, x1, y1 = mira
    recorte = frame_float[y0:y1, x0:x1]
    if recorte.size == 0:
        raise CameraSemImagem(f"Mira {mira} cai fora do frame {LARGURA}x{ALTURA}.")
    lab = cv2.cvtColor(recorte / 255.0, cv2.COLOR_BGR2LAB)
    L, a, b = lab[:, :, 0], lab[:, :, 1], lab[:, :, 2]
    return float(np.mean(L)), float(np.mean(a)), float(np.mean(b)), float(np.std(L))


def medir(frame_float):
    """Extrai de um frame tudo que interessa: amostra, referencia e normalizado.

    L_norm reescala o L da amostra pela leitura do cartao de cinza. E o que
    permite comparar uma captura feita na bancada com outra feita depois, com
    a camera montada no torrador e outra iluminacao. Sem o cartao no quadro,
    a calibracao de hoje nao transfere e tudo precisa ser refeito.
    """
    L, a, b, desvio = lab_da_mira(frame_float, MIRA_AMOSTRA)
    L_ref, a_ref, b_ref, _ = lab_da_mira(frame_float, MIRA_REFERENCIA)
    L_norm = L * (50.0 / L_ref) if L_ref > 1e-6 else float("nan")
    return {
        "L": L, "a": a, "b": b,
        "L_ref": L_ref, "a_ref": a_ref, "b_ref": b_ref,
        "L_norm": L_norm, "desvio_L": desvio,
    }
