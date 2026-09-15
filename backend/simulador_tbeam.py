"""
============================================================================
 SIMULADOR DO T-BEAM  (ferramenta de teste - NAO faz parte do produto final)
============================================================================

 Sobe no PC um servidor WebSocket que fala EXATAMENTE o mesmo protocolo do
 firmware (mesmo JSON, mesma curva de temperatura, mesmos comandos), para
 dar para validar a cadeia backend -> frontend sem ter a placa gravada,
 ligada ou na mesma rede.

 Uso:
     python simulador_tbeam.py                 # sobe em ws://127.0.0.1:8080/ws

 E, no terminal do backend:
     TBEAM_HOST=127.0.0.1 TBEAM_PORT=8080 uvicorn main:app --reload

 Quando o T-Beam de verdade estiver na rede, esqueca este arquivo e use o
 IP da placa em TBEAM_HOST.
============================================================================
"""

import asyncio
import json
import random
import time

from websockets.asyncio.server import serve

HOST = "127.0.0.1"
PORTA = 8080

TEMP_AMBIENTE = 25.0
TEMP_MAX_TORRA = 215.0
FATOR_APROXIMACAO = 0.05
INTERVALO_ENVIO_S = 1.0
# Espelha INTERVALO_AMOSTRAGEM_MS do firmware: o sensor e lido 4x entre dois
# envios e o que vai para a rede e a media. O protocolo nao muda - continua
# saindo uma mensagem por segundo.
INTERVALO_AMOSTRAGEM_S = 0.25

# Mesmo estado que o firmware mantem em memoria.
temperatura = TEMP_AMBIENTE
potencia_atual = 100
motor_ligado = False
boot = time.monotonic()


# Fator POR AMOSTRA, derivado do fator por segundo - mesma conta que o
# firmware faz com powf(). Mantem a curva identica a qualquer taxa.
FATOR_POR_AMOSTRA = 1.0 - (1.0 - FATOR_APROXIMACAO) ** INTERVALO_AMOSTRAGEM_S


def ler_temperatura() -> float:
    """Espelho da funcao lerTemperatura() do firmware. Uma amostra crua."""
    global temperatura
    alvo = TEMP_AMBIENTE + (TEMP_MAX_TORRA - TEMP_AMBIENTE) * (potencia_atual / 100.0)
    temperatura += (alvo - temperatura) * FATOR_POR_AMOSTRA
    # Ruido de MEDICAO: fora do estado, para que a media consiga atenua-lo.
    return temperatura + random.uniform(-0.15, 0.15)


async def temperatura_media() -> float:
    """Colhe as amostras de um ciclo de envio e devolve a media."""
    n = max(1, int(round(INTERVALO_ENVIO_S / INTERVALO_AMOSTRAGEM_S)))
    soma = 0.0
    for _ in range(n):
        soma += ler_temperatura()
        await asyncio.sleep(INTERVALO_AMOSTRAGEM_S)
    return soma / n


async def transmitir(conexao) -> None:
    while True:
        # A espera do ciclo acontece dentro de temperatura_media(), que dorme
        # INTERVALO_AMOSTRAGEM_S entre cada amostra.
        media = await temperatura_media()
        leitura = {
            "temp": round(media, 2),
            "timestamp": int((time.monotonic() - boot) * 1000),  # millis()
            "potencia_atual": potencia_atual,
            "motor_ligado": motor_ligado,
        }
        await conexao.send(json.dumps(leitura))


async def atender(conexao) -> None:
    global potencia_atual, motor_ligado
    print("[SIM] backend conectado")
    tarefa = asyncio.create_task(transmitir(conexao))
    try:
        async for bruta in conexao:
            try:
                comando = json.loads(bruta)
            except json.JSONDecodeError:
                print("[SIM] JSON invalido:", bruta[:80])
                continue
            if "potencia" in comando:
                potencia_atual = max(0, min(100, int(comando["potencia"])))
                print(f"[SIM] potencia -> {potencia_atual}%")
            if "motor" in comando:
                motor_ligado = comando["motor"] == "on"
                print(f"[SIM] motor -> {'ON' if motor_ligado else 'OFF'}")
    finally:
        tarefa.cancel()
        print("[SIM] backend desconectado")


async def main() -> None:
    print(f"[SIM] T-Beam simulado em ws://{HOST}:{PORTA}/ws")
    async with serve(atender, HOST, PORTA):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
