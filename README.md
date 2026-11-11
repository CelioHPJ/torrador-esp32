# Torrador de café — telemetria térmica (T-Beam) e visão computacional

**[Português](README.md) | [English](README.en.md)**

Projeto de extensão — **CEFET-MG Varginha**.

Automação experimental de um torrador de café de leito fluidizado (pipoqueira elétrica de ar quente, **somente leitura**, sem controle de resistência ou ventoinha) dividida em três frentes:

- **Telemetria térmica:** termopar tipo K via **MAX31855** em uma **LilyGO T-Beam (ESP32)**, com sonda no meio dos grãos (4 amostras/s a cada 250 ms + filtro de picos).
- **Visão computacional (`visao/` e `calibracao/`):** captura com exposição e balanço de branco travados, conversão para **CIELAB** ($L^*, a^*, b^*$) e regressão linear (`scikit-learn`) para medir o índice **Agtron** em tempo real.
- **Painel web ("Caderno de torra"):** backend **FastAPI** + frontend **React** exibindo curva térmica, **RoR** (°C/min), tempo de desenvolvimento (**DTR**), marcação de eventos, gravação automática em CSV (`torras/`) e Agtron.

> **Estado atual:** telemetria validada ponta a ponta em torras reais (`torras/`). Na primeira torra (`06/10/2026`), o **1º crack** foi marcado em **1:24 (158,7 °C)** e a **descarga** em **2:54 (224,2 °C, pico ~225 °C)** com Wi-Fi estável. O laço de visão já está integrado ao backend, aguardando a fixação da câmera e calibração (`visao/modelo_agtron.pkl`).

---

## Arquitetura e Protocolo

```
   [ T-Beam / ESP32 ]            [ backend FastAPI ]            [ frontend React ]
    servidor WebSocket   ---->    cliente + servidor    ---->     cliente WebSocket
     ws://<ip>:80/ws               /ws/temperatura                  (navegador)
```

- **Sentido único:** o T-Beam é o **servidor** WebSocket (`ws://<ip>/ws`, mDNS `torrador.local`); quem abre a conexão é o backend. O frontend nunca acessa a placa diretamente.
- **Persistência:** o backend mantém a sessão em memória e grava cada torra linha a linha em `torras/torra_YYYY-MM-DD_HHMMSS.csv`. Ao conectar, o navegador recebe imediatamente o retrato atual mais o `historico` completo.
- **Tipos de mensagem (`"tipo"`) e detecção de queda:**
  - `"leitura"`: dado novo do T-Beam (contém `temp` e `recebido_em`; único plotado no gráfico).
  - `"tick"`: aviso de estado (`offline`, `visao_motivo`) **sem** `temp` nem `recebido_em`, evitando pontos duplicados durante quedas.
  - Duas proteções marcam `"offline": true`: **`ponte_tbeam()`** (reconecta e emite `tick` a cada `3 s` quando o socket fecha) e **`vigia_dado_parado()`** (marca offline após `5 s` sem leitura válida, mesmo se o socket TCP continuar aberto).

### JSON do T-Beam (1x/s) e campos do Backend

```json
{ "temp": 187.4, "timestamp": 154320, "picos": 0 }
```

- `temp`: graus °C ou `null` se o sensor falhou no segundo inteiro (a placa nunca repete valor antigo; o backend descarta `null` no log).
- `timestamp`: `millis()` desde o boot. `picos`: amostras descartadas pelo filtro de picos desde o boot.

Campos adicionados pelo backend na mensagem `"tipo": "leitura"`:

| Campo | Descrição |
|---|---|
| `recebido_em` | Epoch real do backend (usado no eixo X do gráfico para não zerar se a placa reiniciar) |
| `tempo_torra` | Segundos desde a primeira leitura da sessão (*CHARGE*) |
| `agtron_estimado` | Estimativa termodinâmica por temperatura e tempo |
| `agtron_medido` | Medição óptica da câmera (`visao/agtron.py`), ou `null` se indisponível/expirada (> 10 s) |
| `visao_motivo` | Motivo quando `agtron_medido` é `null` (câmera desligada, sem modelo, mira estourada etc.) |
| `offline` / `tbeam_uri` | Estado da conexão com a placa e URI alvo |
| `eventos` | Marcas anotadas na sessão (`amarelamento`, `crack1`, `crack2`, `descarga`) com tempo e temperatura |

---

## Estrutura da Pasta

```
firmware/                  projeto PlatformIO (board = ttgo-t-beam, Espressif32 @ 7.1.0)
  platformio.ini           envs: ttgo-t-beam (USB) e ota (HTTP POST /update)
  src/main.cpp             termopar + Wi-Fi duplo + WebSocket + mDNS + OTA
  include/filtro_picos.h   descarte de saltos espúrios do MAX31855
  include/credenciais.exemplo.h  modelo para criar include/credenciais.h (ignorado no git)
  scripts/gravar_ota.py    upload de firmware via HTTP
  teste_termopar/          sketch isolado para testar fiação e diagnóstico do MAX31855
backend/
  main.py                  ponte WebSocket, vigia, laço de visão e gravação CSV (FastAPI)
  simulador_tbeam.py       servidor WebSocket falso (curva até ~215 °C) para testar sem placa
  requirements.txt         FastAPI, uvicorn, websockets, opencv-python-headless, numpy, scikit-learn
visao/
  camera.py                captura com exposição/WB travados (v4l2 ou dshow/WSL) + ROIs
  agtron.py                recorte da mira -> CIELAB -> modelo_agtron.pkl -> média móvel (5 frames)
calibracao/
  capturar_amostras.py     coleta de amostras com trava óptica e checagem de saturação
  treinar.py               seleção de variáveis e treino LinearRegression (leave-one-sample-out)
  CHECKLIST.md             procedimento completo de bancada (8 amostras × 5 repetições)
torras/                    CSVs das torras reais, notas e script gerar_curva.py
frontend/                  painel React + Vite + Recharts (src/App.jsx, src/estilos.css)
patches/                   patches de referência para o repositório legado de visão
```

---

## Como Rodar

### 1. Firmware (T-Beam)

Se não tiver o PlatformIO instalado (em Debian/Ubuntu com PEP 668, use um venv dedicado):

```bash
python3 -m venv ~/.venvs/platformio && ~/.venvs/platformio/bin/pip install platformio
export PATH="$HOME/.venvs/platformio/bin:$PATH"   # adicione ao ~/.bashrc
```

> Compilado e validado com PlatformIO 6.2.0, `espressif32 @ 7.1.0`, `AsyncTCP 3.5.0`, `ESPAsyncWebServer 3.12.0`, `ArduinoJson 7.4.3` e `Adafruit MAX31855 ^1.4.2` (ocupação: RAM 3,5%, Flash 66,9%).

Configure as credenciais (Wi-Fi **2,4 GHz** apenas; sem rede no boot, a placa segue medindo e alterna entre a rede 1 e 2 a cada 15 s):

```bash
cd firmware
cp include/credenciais.exemplo.h include/credenciais.h
```

| Constante em `credenciais.h` | Descrição |
|---|---|
| `WIFI_SSID` / `WIFI_PASSWORD` | Rede principal (tentada primeiro) |
| `WIFI_SSID_2` / `WIFI_PASSWORD_2` | Segunda rede opcional (ex.: hotspot; apague se não usar) |
| `WIFI_IP_2` | IP fixo opcional na 2ª rede (ex.: `192.168.137.50` para `netsh portproxy` no Hotspot do Windows) |
| `OTA_SENHA` | Senha obrigatória para gravação via rede (`POST /update`, usuário `ota`) |

- **1ª gravação (via cabo USB):**
  ```bash
  pio run --target upload
  pio device monitor         # 115200 baud: anote o IP impresso (ou procure "torrador" no roteador)
  ```
- **Gravações seguintes (OTA via Wi-Fi, sem cabo):** usa `POST /update` (em vez de `espota`, para funcionar mesmo com PC atrás de NAT no WSL2) e grava na partição inativa com *rollback* seguro:
  ```bash
  pio run -e ota -t upload --upload-port 192.168.0.42
  ```
- **Redes de faculdade:** crie um **Hotspot Móvel em 2,4 GHz** no notebook/celular e cadastre-o como `WIFI_SSID_2` antes de ir (se o Wi-Fi do notebook estiver em 5 GHz, conecte-o a uma rede 2,4 GHz ou cabo para liberar o hotspot em 2,4 GHz).

### 2. Backend (FastAPI) e Simulador

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Com a placa real:
TBEAM_HOST=192.168.0.42 uvicorn main:app --reload --port 8000

# Ou sem a placa (em outro terminal rode: python simulador_tbeam.py):
TBEAM_HOST=127.0.0.1 TBEAM_PORT=8080 uvicorn main:app --reload --port 8000
```

| Variável de ambiente | Padrão | Descrição |
|---|---|---|
| `TBEAM_HOST` / `TBEAM_PORT` / `TBEAM_WS_PATH` | `192.168.0.50` / `80` / `/ws` | Endereço WebSocket do T-Beam |
| `TORRADOR_TORRAS_DIR` | `torras/` | Pasta de saída dos CSVs de torra |
| `TORRADOR_VISAO` | `1` | `0` desativa a câmera sem desinstalar pacotes |
| `TORRADOR_VISAO_INTERVALO` | `2.0` | Período (s) entre capturas da câmera |
| `TORRADOR_CAMERA` | automático | Força `v4l2` (`/dev/video0`) ou `dshow` (`ffmpeg.exe` via WSL) |

| Rota | Descrição |
|---|---|
| `GET /status` | Diagnóstico completo (conexão, socket, CSV ativo, eventos, estado da visão e última leitura) |
| `GET /historico` | Todas as leituras da sessão atual |
| `POST /sessao/nova` | Fecha o CSV atual em `torras/`, zera cronômetro/memória e inicia nova torra |
| `POST /evento/{nome}` | Registra `amarelamento`, `crack1`, `crack2` ou `descarga` no gráfico e no CSV |
| `WS /ws/temperatura` | Canal WebSocket consumido pelo frontend |

### 3. Frontend (React + Vite) e Verificação

```bash
cd frontend
npm install && npm run dev   # abre em http://localhost:5173 (ajuste VITE_BACKEND_HOST no .env se precisar)
```

No painel **"Caderno de torra"**:
- **Status da cadeia:** verde (`cadeia completa`), laranja (`T-Beam desconectado` — reconectando a cada 3 s) ou vermelho (`backend fora do ar`). Pelo terminal: `curl localhost:8000/status`.
- **Métricas e alertas:** temperatura com régua térmica, **RoR (°C/min)** calculado em janela de 30 s (com alerta acima de `40 °C/min` após 60 s de torra), **tempo de desenvolvimento e DTR (%)** pós-1º crack, e botões de anotação de eventos.

---

## Agtron: Estimado vs. Medido

1. **`agtron_estimado` (`estimar_agtron` em `backend/main.py`):** heurística exibida atualmente na tela, calculada por temperatura ($140\text{..}215\text{ °C}$) e tempo de exposição ($\tau = 240\text{ s}$):
   $$\text{progresso} = f_{\text{temp}} \times (0{,}65 + 0{,}35 \times (1 - e^{-t/240})), \quad \text{Agtron} = 98 - (98 - 42) \times \text{progresso}$$
   Possui trava monotônica em `processar_leitura()` para nunca subir quando a temperatura cai.
2. **`agtron_medido` (`visao/` e `calibracao/`):** medição óptica real. `visao/camera.py` trava exposição e balanço de branco, recorta `MIRA_AMOSTRA` (grãos) e `MIRA_REFERENCIA` (cartão cinza para `L_norm`), recusa estouro de luz (> 2% de pixels em 255) e converte para CIELAB. `calibracao/treinar.py` escolhe o melhor subconjunto (`L_norm`, `L`, `L_norm,a,b`, `L,a,b`) por validação *leave-one-sample-out* e salva `visao/modelo_agtron.pkl` com a assinatura óptica. Siga `calibracao/CHECKLIST.md` para coletar e treinar.

---

## Termopar (MAX31855) e Filtro de Picos

- **Pinagem (`firmware/src/main.cpp`):** `VIN -> 3V3` (**nunca 5 V**), `GND -> GND`, **`CLK = GPIO 13`**, **`CS = GPIO 14`**, **`DO = GPIO 25`**. Evite os pinos de *strapping* (`0, 2, 12, 15`) e os pinos já ocupados no T-Beam por LoRa (`5, 18, 19, 23, 26, 27`), AXP192 (`21, 22`) e GPS (`12, 34`).
- **Diagnóstico isolado e calibração:** use `firmware/teste_termopar` (`pio run -t upload && pio device monitor`) para testar fiação, temperatura interna (junta fria) e falhas de curto/aberto sem Wi-Fi.
  - Referências: gelo fundente (~0 °C; medido **+1,5 °C** estável em 30/09/2026, dentro da tolerância somada do par + chip) e água fervendo (~97 °C em Varginha, a ~940 m).
  - **Polaridade:** se a temperatura **cair** ao segurar a ponta na mão, os fios do termopar estão invertidos nos bornes (não confie na cor da capa; refaça o teste da mão sempre que reapertar os bornes).
- **Filtro de picos (`include/filtro_picos.h`):** descarta amostras de 250 ms que saltam **> 25 °C** em relação à anterior (como flips de bit de +256 °C por ruído mecânico no cabo), mas aceita mudanças reais rápidas se **3 amostras seguidas** concordarem (atraso de apenas 0,5 s no degrau). Cada descarte incrementa o campo `picos` no JSON.

---

## Próximos Passos

- Utilizar a câmera em conjunto com o termopar durante a torra:
  - Fixar fisicamente o suporte da câmera (desacoplada termicamente da saída de ar quente) e a iluminação LED controlada na pipoqueira.
  - Executar a coleta de calibração (`calibracao/CHECKLIST.md`) para gerar `visao/modelo_agtron.pkl`.
  - Exibir o `agtron_medido` ao lado do `agtron_estimado` em `frontend/src/App.jsx`.
  - Realizar bateladas de torra cruzando a curva térmica real com a curva de Agtron óptico.
