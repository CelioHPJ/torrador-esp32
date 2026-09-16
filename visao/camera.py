"""
============================================================================
 CAMERA COM CONTROLES TRAVADOS
============================================================================

 Modulo unico de acesso a camera. Usado tanto pela coleta de calibracao
 (calibracao/capturar_amostras.py) quanto pela medicao em producao
 (visao/agtron.py).

 E UM MODULO SO DE PROPOSITO. Se cada um tivesse a sua copia dos controles
 e da ROI, as copias divergiriam -- e a ferramenta de calibrar passaria
 a medir uma condicao diferente da que roda na torra, sem ninguem perceber.
 Calibracao e medicao PRECISAM enxergar exatamente a mesma coisa, ou o modelo
 treinado nao vale para o que ele mede depois.

 Por que travar: deixada no automatico, a camera compensa a cena. Cafe escuro
 enche o quadro, ela clareia a imagem, e a medida anda ao contrario da
 realidade -- o proprio sinal que se quer medir e cancelado pelo controle
 automatico. Com os automaticos ligados, nenhum dado coletado presta.

 ---------------------------------------------------------------------------
 DUAS FONTES DE FRAME
 ---------------------------------------------------------------------------

 "v4l2"  -- OpenCV + ioctl em /dev/video0. Serve quando a camera esta ligada
            num Linux de verdade.

 "dshow" -- ffmpeg.exe do Windows, chamado daqui pelo interop do WSL, com os
            controles ajustados por PowerShell. E o caminho usado hoje.

 Por que existe o "dshow": sob WSL a camera e exportada por usbipd, e video
 isocrono nao atravessa aquele tunel. A camera enumera em Full Speed, o driver
 corta o payload de 3060 para 1023 bytes, e chegam ~780 de 614400 bytes por
 frame -- 0,1%. O buffer nao preenchido vira verde uniforme, que passa por
 qualquer media e produz numero com cara de dado. Nenhum ajuste de formato ou
 resolucao alcanca essa causa. O jeito e capturar onde a camera esta ligada de
 fato, no Windows, sem por isso portar o resto do projeto para la: so os bytes
 vem de la, todo o processamento continua aqui.

 A ESCOLHA E AUTOMATICA: existe /dev/video0 -> v4l2; nao existe -> dshow.
 Force com a variavel de ambiente TORRADOR_CAMERA=v4l2|dshow.
============================================================================
"""

import fcntl
import os
import struct
import subprocess

import cv2
import numpy as np

# --------------------------------------------------------------------------
# Condicao optica. Vale para calibracao E para producao: mexer aqui invalida
# qualquer modelo treinado antes da mudanca.
# --------------------------------------------------------------------------
DISPOSITIVO = "/dev/video0"
LARGURA, ALTURA = 640, 480

TEMPERATURA_COR = 4600   # faixa desta camera: 2800..6500 (igual nos dois caminhos)
GANHO = 4                # faixa desta camera: 4..8 (so v4l2)

# A EXPOSICAO NAO E A MESMA GRANDEZA NOS DOIS CAMINHOS, e nao da para
# converter um valor no outro por conta propria:
EXPOSICAO_V4L2 = 250     # unidade do UVC via V4L2, faixa 10..2500
EXPOSICAO_DSHOW = -10    # log2 de segundo, faixa -10..-2 (-10 = 1/1024 s)

# Miras no formato (x_inicial, y_inicial, x_final, y_final).
MIRA_AMOSTRA = (220, 160, 420, 320)      # onde ficam os graos
MIRA_REFERENCIA = (40, 40, 110, 100)     # onde fica o cartao cinza

FRAMES_AQUECIMENTO = 15
FRAMES_POR_CAPTURA = 10

# Frames lidos e jogados fora antes de medir. No caminho dshow o ffmpeg fica
# transmitindo continuamente: se ninguem le por um tempo, o que esta no cano
# e frame velho. Sem descartar, a medida seria do instante em que o pipe
# encheu, nao de agora.
FRAMES_DESCARTE = 5

# --------------------------------------------------------------------------
# Caminho dshow
# --------------------------------------------------------------------------
FFMPEG_WINDOWS = "/mnt/c/ffmpeg/bin/ffmpeg.exe"
NOME_DSHOW = "Integrated Camera"   # o nome legivel; o "alternative name" do
                                   # pnp NAO funciona nesta camera (o dshow
                                   # responde "Could not RenderStream").

# O cwd precisa ser um caminho do Windows: chamado de dentro do repo, que mora
# no sistema de arquivos do WSL, o processo Windows reclama de UNC.
CWD_WINDOWS = "/mnt/c"

# Quantas vezes insistir para abrir a camera. NAO REDUZIR: quando outro
# processo acabou de soltar o dispositivo, a abertura falha por alguns
# segundos -- e o erro MENTE, diz "Could not find video device with name
# [Integrated Camera]" como se o nome estivesse errado. O nome esta certo,
# ja foi conferido byte a byte; o que falhou foi ligar no dispositivo.
TENTATIVAS_ABERTURA = 6
ESPERA_ENTRE_TENTATIVAS = 2.0


# Identifica a condicao optica inteira. Vai gravado dentro do modelo treinado:
# se alguem mudar a exposicao ou a mira e esquecer de retreinar, da para
# detectar em vez de descobrir por um resultado estranho meses depois.
#
# O BACKEND FAZ PARTE DA ASSINATURA. Um modelo treinado com frames do v4l2 nao
# vale para frames do dshow: alem da exposicao estar em outra unidade, o
# processamento de cor do driver e outro.
def assinatura_optica():
    origem = backend()
    return {
        "backend": origem,
        "dispositivo": DISPOSITIVO if origem == "v4l2" else NOME_DSHOW,
        "resolucao": f"{LARGURA}x{ALTURA}",
        "exposicao": EXPOSICAO_V4L2 if origem == "v4l2" else EXPOSICAO_DSHOW,
        "temperatura_cor": TEMPERATURA_COR,
        "ganho": GANHO if origem == "v4l2" else None,
        "mira_amostra": list(MIRA_AMOSTRA),
        "mira_referencia": list(MIRA_REFERENCIA),
    }


def backend():
    """Qual fonte de frame usar. Automatico, com escape por ambiente."""
    escolha = os.environ.get("TORRADOR_CAMERA", "auto").strip().lower()
    if escolha in ("v4l2", "dshow"):
        return escolha
    if escolha != "auto":
        raise ValueError(
            f"TORRADOR_CAMERA={escolha!r} nao e valido. Use v4l2, dshow ou auto."
        )
    return "v4l2" if os.path.exists(DISPOSITIVO) else "dshow"


class CameraNaoTravou(RuntimeError):
    """Os automaticos nao aceitaram ir para manual. Nao capture nada assim."""


class CameraSemImagem(RuntimeError):
    """A camera respondeu, mas nao entregou imagem utilizavel."""


class MiraSaturada(CameraSemImagem):
    """A mira esta estourada de luz. O frame e real, a medida de cor nao.

    O DIAGNOSTICO e diferente do de CameraSemImagem -- ali a camera nao
    entregou imagem, aqui entregou e a imagem esta clipada -- e por isso a
    mensagem e outra: confundir os dois manda quem le depurar a coisa errada.

    Mas HERDA de CameraSemImagem porque, para quem consome, a consequencia e a
    mesma: nao da para medir agora, e o certo e dizer isso em vez de devolver
    um numero. Assim todo tratamento que ja existe (visao/agtron.py) continua
    valendo, e quem quiser distinguir captura MiraSaturada antes.
    """


# ==========================================================================
# TRAVAMENTO DOS AUTOMATICOS
# ==========================================================================

# --------------------------------------------------------------------------
# v4l2: controles por ioctl
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


def _ctrl(fd, requisicao, cid, valor=0):
    buf = bytearray(struct.pack("<Ii", cid, valor))
    fcntl.ioctl(fd, requisicao, buf)
    return struct.unpack("<Ii", bytes(buf))[1]


def _travar_v4l2(dispositivo):
    """A ORDEM IMPORTA: exposicao_auto e wb_auto vao para manual primeiro,
    porque enquanto o automatico esta ligado os controles de valor ficam
    inativos e a escrita e silenciosamente ignorada.
    """
    with open(dispositivo, "rb+", buffering=0) as fd:
        _ctrl(fd, VIDIOC_S_CTRL, CID_EXPOSICAO_AUTO, 1)
        _ctrl(fd, VIDIOC_S_CTRL, CID_WB_AUTO, 0)
        _ctrl(fd, VIDIOC_S_CTRL, CID_EXPOSICAO_ABS, EXPOSICAO_V4L2)
        _ctrl(fd, VIDIOC_S_CTRL, CID_WB_TEMPERATURA, TEMPERATURA_COR)
        _ctrl(fd, VIDIOC_S_CTRL, CID_GANHO, GANHO)

        lidos = {
            "backend": "v4l2",
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


# --------------------------------------------------------------------------
# dshow: controles pela API de captura do Windows, via PowerShell
#
# O script vai embutido aqui e entra pelo stdin do powershell.exe, em vez de
# morar num .ps1 solto no disco do Windows. Assim ele e versionado junto com
# as constantes que ele aplica -- um .ps1 perdido em C:\ poderia divergir das
# miras e da exposicao deste arquivo, que e exatamente o tipo de divergencia
# silenciosa que este modulo existe para impedir.
#
# POR QUE NAO O CAMINHO OBVIO: o ffmpeg tem -show_video_device_dialog e
# -video_device_save/-video_device_load, que PARECEM resolver. Nao resolvem,
# e ja foram testados:
#   - o painel grafico abre com todos os controles cinzas, porque o ffmpeg o
#     mostra antes de iniciar a captura e este driver so libera os controles
#     com stream rodando;
#   - o arquivo salvo tem 264 bytes e contem so o endereco do dispositivo, em
#     UTF-16. Dois arquivos salvos com 20 minutos de diferenca saem identicos
#     byte a byte. Parametro nenhum e guardado ali.
# --------------------------------------------------------------------------
_PS_CONTROLES = r"""
param([string]$Acao = "ler", [double]$Exposicao = 0, [double]$WB = 0)
$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
                   $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $tipo) {
    $t = $asTaskGeneric.MakeGenericMethod($tipo).Invoke($null, @($op))
    $t.Wait(-1) | Out-Null; $t.Result
}
function AwaitAction($act) {
    $m = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
                       -not $_.IsGenericMethod })[0]
    $t = $m.Invoke($null, @($act)); $t.Wait(-1) | Out-Null
}

[Windows.Devices.Enumeration.DeviceInformation,Windows.Devices.Enumeration,ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Capture.MediaCapture,Windows.Media.Capture,ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Capture.MediaCaptureInitializationSettings,Windows.Media.Capture,ContentType=WindowsRuntime] | Out-Null

$devs = Await ([Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync(
    [Windows.Devices.Enumeration.DeviceClass]::VideoCapture)) ([Windows.Devices.Enumeration.DeviceInformationCollection])
$alvo = $devs | Where-Object { $_.Id -match 'vid_05e3.*pid_f12a' } | Select-Object -First 1
if (-not $alvo) { Write-Output "ERRO camera nao encontrada"; exit 1 }

# RETENTATIVA OBRIGATORIA. Quando outro processo acabou de soltar a camera,
# InitializeAsync devolve um objeto cujo VideoDeviceController e NULO, sem
# lancar excecao. Sem este laco o script roda inteiro, nao ajusta nada e
# ainda assim termina como se tivesse dado certo -- falha silenciosa.
$vdc = $null
for ($i = 1; $i -le 8; $i++) {
    try {
        $mc = New-Object Windows.Media.Capture.MediaCapture
        $cfg = New-Object Windows.Media.Capture.MediaCaptureInitializationSettings
        $cfg.VideoDeviceId = $alvo.Id
        $cfg.StreamingCaptureMode = [Windows.Media.Capture.StreamingCaptureMode]::Video
        AwaitAction ($mc.InitializeAsync($cfg))
        if ($null -ne $mc.VideoDeviceController) { $vdc = $mc.VideoDeviceController; break }
    } catch { }
    Start-Sleep -Milliseconds 1500
}
if ($null -eq $vdc) { Write-Output "ERRO nao abriu em 8 tentativas"; exit 2 }

if ($Acao -eq "travar") {
    $vdc.Exposure.TrySetAuto($false)     | Out-Null
    $vdc.Exposure.TrySetValue($Exposicao) | Out-Null
    $vdc.WhiteBalance.TrySetAuto($false) | Out-Null
    $vdc.WhiteBalance.TrySetValue($WB)   | Out-Null
}

# Relê SEMPRE do driver, inclusive depois de gravar: o que importa nao e o que
# mandamos, e o que a camera aceitou.
$e = 0.0; $w = 0.0; $ae = $false; $aw = $false
$vdc.Exposure.TryGetValue([ref]$e)     | Out-Null
$vdc.WhiteBalance.TryGetValue([ref]$w) | Out-Null
$vdc.Exposure.TryGetAuto([ref]$ae)     | Out-Null
$vdc.WhiteBalance.TryGetAuto([ref]$aw) | Out-Null
Write-Output ("FINAL exposicao={0} wb={1} exposicao_auto={2} wb_auto={3}" -f $e, $w, $ae, $aw)
$mc.Dispose()
"""


def _powershell(acao, exposicao=0, wb=0, timeout=90):
    """Roda o script de controles e devolve a linha FINAL ja destrinchada."""
    if acao == "travar":
        cabecalho = f"$Acao='travar'; $Exposicao={exposicao}; $WB={wb}\n"
    else:
        cabecalho = "$Acao='ler'; $Exposicao=0; $WB=0\n"
    # O param() do script nao vale quando ele entra por stdin, entao as
    # variaveis sao definidas antes e a linha do param e descartada.
    corpo = "\n".join(
        l for l in _PS_CONTROLES.splitlines() if not l.startswith("param(")
    )
    try:
        p = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "-"],
            input=(cabecalho + corpo).encode("utf-8"),
            capture_output=True, cwd=CWD_WINDOWS, timeout=timeout,
        )
    except FileNotFoundError:
        raise CameraNaoTravou(
            "powershell.exe nao encontrado. O caminho dshow depende do interop "
            "do WSL para falar com o Windows. Confira se o interop esta ligado "
            "(deve existir /proc/sys/fs/binfmt_misc/WSLInterop)."
        )
    except subprocess.TimeoutExpired:
        raise CameraNaoTravou(
            f"O ajuste dos controles nao respondeu em {timeout}s. A camera pode "
            "estar presa por outro processo."
        )

    saida = p.stdout.decode("utf-8", errors="replace")
    for linha in saida.splitlines():
        if linha.startswith("FINAL "):
            campos = dict(par.split("=", 1) for par in linha.split()[1:])
            return {
                "backend": "dshow",
                "exposicao": float(campos["exposicao"]),
                "temp_cor": float(campos["wb"]),
                "exposicao_auto": campos["exposicao_auto"] == "True",
                "wb_auto": campos["wb_auto"] == "True",
            }

    erro = saida.strip() or p.stderr.decode("utf-8", errors="replace").strip()
    raise CameraNaoTravou(f"Nao consegui ler os controles da camera. Saida:\n{erro}")


def _travar_dshow():
    lidos = _powershell("travar", EXPOSICAO_DSHOW, TEMPERATURA_COR)

    if lidos["exposicao_auto"] or lidos["wb_auto"]:
        raise CameraNaoTravou(
            f"Os automaticos nao foram para manual: {lidos}. "
            "Capturar assim produziria dados que medem a reacao da camera, "
            "nao o cafe."
        )
    if lidos["exposicao"] != EXPOSICAO_DSHOW:
        raise CameraNaoTravou(
            f"Pedi exposicao {EXPOSICAO_DSHOW} e a camera ficou em "
            f"{lidos['exposicao']}. A faixa desta camera e -10..-2, em log2 de "
            "segundo. Se o valor pedido cair fora, o driver ignora sem avisar."
        )
    return lidos


def travar(dispositivo=DISPOSITIVO):
    """Poe exposicao e white balance em manual, com valores fixos, e CONFERE.

    Escreve e rele: o que vale e o que a camera aceitou, nao o que mandamos.

    NO CAMINHO DSHOW A TRAVA NAO FICA GUARDADA EM LUGAR NENHUM. Ela vive no
    dispositivo e, ficando ocioso, ele volta sozinho ao padrao -- ja foi
    observado voltando para exposicao -6 e WB 4000 depois de alguns minutos
    parado. Por isso: travar imediatamente antes de capturar, e usar
    conferir_trava() se a sessao for longa. Nao existe "travar uma vez e
    confiar".
    """
    if backend() == "v4l2":
        if not os.path.exists(dispositivo):
            raise CameraSemImagem(
                f"{dispositivo} nao existe, mas o backend escolhido e v4l2 "
                "(TORRADOR_CAMERA=v4l2).\n"
                "Se a camera esta com o Windows, use TORRADOR_CAMERA=dshow ou "
                "deixe a escolha automatica.\n"
                "CUIDADO com a saida 'obvia': anexar a camera ao WSL "
                "(usbipd attach) faz o /dev/video0 aparecer E QUEBRA A CAPTURA "
                "-- video isocrono nao atravessa o usbipd, chegam ~0,1% de cada "
                "frame. Ter o dispositivo nao e o mesmo que ter imagem."
            )
        return _travar_v4l2(dispositivo)
    return _travar_dshow()


def conferir_trava():
    """Rele os controles sem escrever. Levanta se a trava tiver caido.

    Serve para uma sessao longa (uma torra inteira) checar de tempos em tempos
    que a condicao optica ainda e a que o modelo espera, sem pagar a escrita.
    """
    if backend() == "v4l2":
        with open(DISPOSITIVO, "rb+", buffering=0) as fd:
            lidos = {
                "backend": "v4l2",
                "exposicao_auto": _ctrl(fd, VIDIOC_G_CTRL, CID_EXPOSICAO_AUTO),
                "exposicao": _ctrl(fd, VIDIOC_G_CTRL, CID_EXPOSICAO_ABS),
                "wb_auto": _ctrl(fd, VIDIOC_G_CTRL, CID_WB_AUTO),
            }
        caiu = lidos["exposicao_auto"] != 1 or lidos["wb_auto"] != 0
        esperado = EXPOSICAO_V4L2
    else:
        lidos = _powershell("ler")
        caiu = lidos["exposicao_auto"] or lidos["wb_auto"]
        esperado = EXPOSICAO_DSHOW

    if caiu or lidos["exposicao"] != esperado:
        raise CameraNaoTravou(
            f"A trava caiu: {lidos}, esperado exposicao={esperado} com os "
            "automaticos desligados. Tudo que foi medido desde a ultima "
            "confirmacao esta sob suspeita."
        )
    return lidos


# ==========================================================================
# JULGAMENTO DO FRAME
# ==========================================================================

# Fracao do frame que pode ser ocupada por uma unica cor exata antes de o
# frame ser considerado sem imagem. Medido em 2026-09-16 com a camera
# entregando frames parciais pelo usbipd: 97,35% do quadro era a mesma cor
# (o verde do buffer zerado) e apenas 13 das 480 linhas tinham conteudo. Uma
# foto real de 8 bits nao chega perto disso -- o ruido do sensor espalha os
# valores, e mesmo um fundo liso rende dezenas de milhares de cores distintas.
FRACAO_MAXIMA_COR_UNICA = 0.30

# Fracao de pixels clipados que condena UMA MIRA. Pixel com canal em 255 perdeu
# a informacao de quanta luz havia: 255 quer dizer "pelo menos isto". Medir cor
# ali nao da erro grande, da erro SISTEMATICO na direcao do claro -- que e
# justamente a direcao em que o Agtron varia.
FRACAO_MAXIMA_SATURADA = 0.02


def _fracao_clipada(a):
    return float((np.asarray(a).max(axis=-1) >= 255).mean())


def motivo_frame_invalido(frame):
    """Devolve o motivo de o frame nao servir, ou None se ele servir.

    Detecta frame que nao contem imagem de verdade. Buffer nunca preenchido
    chega como YUV todo zero, que convertido para BGR vira verde saturado e
    uniforme -- e passaria batido por qualquer media, virando um numero com
    cara de dado.

    NAO da para detectar isso pelo desvio global: verde puro e (0, 154, 0) em
    BGR, entao a diferenca ENTRE canais sozinha ja produz desvio ~77, muito
    acima de qualquer limiar de "uniforme". O que denuncia e a REPETICAO: um
    buffer nao preenchido tem uma cor exata se repetindo pelo quadro inteiro.
    Isso tambem pega o caso parcial, que e o perigoso -- frame em que so um
    pedaco chegou e o resto ficou no valor de preenchimento. Esse tem desvio
    alto, tem variacao espacial, parece imagem, e mede a cor errada.

    CLIPPING NAO ENTRA AQUI. Quadro estourado de luz TEM imagem; o problema
    dele e outro, e mora nas miras -- quem acusa e lab_da_mira(). Sem esta
    separacao, um quadro so muito claro era reportado como "camera nao entregou
    imagem, confira a velocidade do usbipd", mandando quem le depurar o
    transporte quando o que faltava era fechar a exposicao.
    """
    a = np.asarray(frame)
    if a.size == 0:
        return "frame vazio: nenhum byte chegou"
    if float(a.std()) < 1.0:
        return "quadro chapado: um valor so, sem variacao nenhuma"

    pixels = a.reshape(-1, a.shape[-1]) if a.ndim == 3 else a.reshape(-1, 1)
    # Pixels clipados ficam de fora da contagem: branco estourado tambem se
    # repete muito, e sem excluir ele o teste acusaria "buffer vazio" numa
    # foto real superexposta.
    vivos = pixels[pixels.max(axis=1) < 255] if pixels.shape[-1] > 1 else pixels
    if len(vivos) < 0.05 * len(pixels):
        return None   # quase tudo clipado: e problema de exposicao, nao daqui

    # uint8 para que "mesma cor" signifique valor identico, e nao float vizinho
    _, contagens = np.unique(vivos.astype(np.uint8), axis=0, return_counts=True)
    fracao = float(contagens.max()) / len(vivos)
    if fracao > FRACAO_MAXIMA_COR_UNICA:
        return (f"buffer nao preenchido: {fracao * 100:.1f}% do quadro e uma "
                f"cor exata so (limite {FRACAO_MAXIMA_COR_UNICA * 100:.0f}%)")
    return None


def frame_degenerado(frame):
    """Compatibilidade: a mesma decisao, em booleano."""
    return motivo_frame_invalido(frame) is not None


# ==========================================================================
# ABERTURA E LEITURA
# ==========================================================================

class _FonteDshow:
    """Camera do Windows lida daqui, com a mesma cara de um cv2.VideoCapture.

    Imita read()/release() de proposito: assim media_de_frames(), medir() e o
    visao/agtron.py nao precisam saber de onde vem o frame.

    O processo do ffmpeg fica VIVO entre capturas, em vez de um processo por
    captura, por dois motivos: a abertura falha de forma intermitente quando o
    dispositivo acabou de ser solto, e cada abertura custa segundos.
    """

    BYTES_POR_FRAME = LARGURA * ALTURA * 3

    def __init__(self):
        self._proc = subprocess.Popen(
            [FFMPEG_WINDOWS, "-hide_banner", "-loglevel", "error",
             "-f", "dshow",
             "-video_size", f"{LARGURA}x{ALTURA}", "-framerate", "30",
             "-i", f"video={NOME_DSHOW}",
             "-pix_fmt", "bgr24", "-f", "rawvideo", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=CWD_WINDOWS, bufsize=0,
        )

    def read(self):
        if self._proc.poll() is not None:
            return False, None
        dados = bytearray()
        while len(dados) < self.BYTES_POR_FRAME:
            pedaco = self._proc.stdout.read(self.BYTES_POR_FRAME - len(dados))
            if not pedaco:                 # ffmpeg morreu no meio do frame
                return False, None
            dados.extend(pedaco)
        frame = np.frombuffer(bytes(dados), dtype=np.uint8)
        return True, frame.reshape(ALTURA, LARGURA, 3)

    def erro_do_ffmpeg(self):
        """O que o ffmpeg reclamou, para a mensagem de erro dizer algo util."""
        if self._proc.poll() is None:
            return ""
        try:
            return self._proc.stderr.read().decode("utf-8", errors="replace").strip()
        except Exception:
            return ""

    def release(self):
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        for cano in (self._proc.stdout, self._proc.stderr):
            try:
                cano.close()
            except Exception:
                pass


def _abrir_v4l2(dispositivo):
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
        f"Nao consegui capturar imagem valida de {dispositivo}. A camera ABRE e "
        "entrega frames, mas eles nao contem imagem -- quase todo o quadro esta "
        "na cor de buffer nao preenchido.\n"
        "Se isto for WSL com usbipd, a causa provavel NAO e configuracao: "
        "confira a velocidade com que a camera enumerou em\n"
        "  cat /sys/bus/usb/devices/1-1/speed\n"
        "Se disser 12 (USB Full Speed), o stream isocrono nao cabe no link e "
        "so uma fracao de cada frame chega -- verificado em 2026-09-16, 780 de "
        "614400 bytes. Nenhum ajuste de formato ou resolucao resolve; a captura "
        "precisa acontecer onde a camera esta ligada de verdade. Use o caminho "
        "dshow: desanexe a camera do WSL (usbipd detach) e rode com "
        "TORRADOR_CAMERA=dshow."
    )


def _abrir_dshow():
    if not os.path.exists(FFMPEG_WINDOWS):
        raise CameraSemImagem(
            f"{FFMPEG_WINDOWS} nao existe. O caminho dshow precisa do ffmpeg do "
            "WINDOWS (o do Linux nao enxerga a camera): baixe o build 'essentials' "
            "e deixe o ffmpeg.exe nesse caminho. Nao precisa instalar nada."
        )

    import time
    ultimo_erro = ""
    for tentativa in range(1, TENTATIVAS_ABERTURA + 1):
        fonte = _FonteDshow()
        for _ in range(FRAMES_AQUECIMENTO):
            ok, _frame = fonte.read()
            if not ok:
                break
        ok, frame = fonte.read()
        if ok and motivo_frame_invalido(frame) is None:
            return fonte, f"BGR24 via dshow (tentativa {tentativa})"

        ultimo_erro = fonte.erro_do_ffmpeg() or (
            motivo_frame_invalido(frame) if ok else "o ffmpeg nao entregou frame"
        )
        fonte.release()
        if tentativa < TENTATIVAS_ABERTURA:
            time.sleep(ESPERA_ENTRE_TENTATIVAS)

    raise CameraSemImagem(
        f"Nao consegui capturar imagem valida da camera '{NOME_DSHOW}' pelo "
        f"dshow, em {TENTATIVAS_ABERTURA} tentativas. Ultimo erro:\n"
        f"  {ultimo_erro}\n"
        "Se a mensagem disser 'Could not find video device with name', ela MENTE: "
        "o nome esta certo, o que falhou foi ligar no dispositivo. Confira se a "
        "camera esta com o WINDOWS e nao anexada ao WSL:\n"
        "  usbipd.exe list        # o estado tem de ser Shared, NAO Attached\n"
        "  usbipd.exe detach --busid 1-4\n"
        "e que nenhum outro programa (app Camera, navegador em chamada) a esteja "
        "segurando."
    )


def abrir(dispositivo=DISPOSITIVO):
    """Devolve (fonte, formato). A fonte tem read() e release(), como o OpenCV."""
    if backend() == "v4l2":
        return _abrir_v4l2(dispositivo)
    return _abrir_dshow()


def media_de_frames(cap, n=FRAMES_POR_CAPTURA):
    """Media de n frames, para reduzir ruido do sensor."""
    for _ in range(FRAMES_DESCARTE):
        cap.read()

    soma = None
    for _ in range(n):
        ok, frame = cap.read()
        if not ok:
            raise CameraSemImagem("Falha ao capturar frame da camera.")
        motivo = motivo_frame_invalido(frame)
        if motivo is not None:
            raise CameraSemImagem(f"Frame sem imagem no meio da captura: {motivo}")
        f = frame.astype(np.float64)
        soma = f if soma is None else soma + f
    return (soma / n).astype(np.float32)


# ==========================================================================
# MEDIDA
# ==========================================================================

def lab_da_mira(frame_float, mira, nome="mira"):
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

    clipada = _fracao_clipada(recorte)
    if clipada > FRACAO_MAXIMA_SATURADA:
        raise MiraSaturada(
            f"A mira {nome} {mira} esta estourada: {clipada * 100:.1f}% dos "
            f"pixels com canal em 255 (limite {FRACAO_MAXIMA_SATURADA * 100:.0f}%). "
            "O frame e imagem de verdade, mas pixel saturado perdeu a informacao "
            "de quanta luz havia, e o erro seria sistematico na direcao do claro "
            "-- a mesma direcao em que o Agtron varia.\n"
            "Feche a exposicao (EXPOSICAO_DSHOW, hoje "
            f"{EXPOSICAO_DSHOW}, faixa -10..-2; mais negativo e mais escuro) ou "
            "reduza a luz sobre a amostra."
        )

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
    L, a, b, desvio = lab_da_mira(frame_float, MIRA_AMOSTRA, "amostra")
    L_ref, a_ref, b_ref, _ = lab_da_mira(frame_float, MIRA_REFERENCIA, "referencia")
    L_norm = L * (50.0 / L_ref) if L_ref > 1e-6 else float("nan")
    return {
        "L": L, "a": a, "b": b,
        "L_ref": L_ref, "a_ref": a_ref, "b_ref": b_ref,
        "L_norm": L_norm, "desvio_L": desvio,
    }
