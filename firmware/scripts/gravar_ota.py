"""Envia o firmware para a rota /update da placa, pela rede.

Chamado pelo PlatformIO (env "ota"), mas roda sozinho tambem:

    python scripts/gravar_ota.py .pio/build/ota/firmware.bin 192.168.0.50

A senha e lida de OTA_SENHA em include/credenciais.h, para existir num lugar
so e fora do git.
"""

import base64
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

CREDENCIAIS = Path(__file__).resolve().parent.parent / "include" / "credenciais.h"
USUARIO = "ota"  # igual a OTA_USUARIO em src/main.cpp


def ler_senha() -> str:
    texto = CREDENCIAIS.read_text(encoding="utf-8")
    achado = re.search(r'^\s*#define\s+OTA_SENHA\s+"([^"]*)"', texto, re.MULTILINE)
    if not achado:
        sys.exit(f"OTA_SENHA nao encontrada em {CREDENCIAIS}")
    return achado.group(1)


def main() -> None:
    if len(sys.argv) != 3 or not sys.argv[2]:
        sys.exit("uso: gravar_ota.py <firmware.bin> <ip-da-placa>  "
                 "(no pio: --upload-port <ip>)")
    firmware = Path(sys.argv[1]).read_bytes()
    url = f"http://{sys.argv[2]}/update"

    fronteira = "----torrador-ota"
    corpo = (
        f"--{fronteira}\r\n"
        'Content-Disposition: form-data; name="firmware"; filename="firmware.bin"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + firmware + f"\r\n--{fronteira}--\r\n".encode()

    credencial = base64.b64encode(f"{USUARIO}:{ler_senha()}".encode()).decode()
    pedido = urllib.request.Request(url, data=corpo, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={fronteira}",
        "Authorization": f"Basic {credencial}",
    })

    print(f"enviando {len(firmware)} bytes para {url} ...")
    try:
        with urllib.request.urlopen(pedido, timeout=120) as resposta:
            print(resposta.read().decode().strip())
    except urllib.error.HTTPError as erro:
        detalhe = "senha errada (OTA_SENHA)" if erro.code == 401 else erro.read().decode().strip()
        sys.exit(f"falhou: HTTP {erro.code} - {detalhe}")
    except urllib.error.URLError as erro:
        sys.exit(f"nao alcancou a placa em {url}: {erro.reason}")


if __name__ == "__main__":
    main()
