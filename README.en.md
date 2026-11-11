# Coffee Roaster — Thermal Telemetry (T-Beam) & Computer Vision

**[Português](README.md) | [English](README.en.md)**

Extension project — **CEFET-MG Varginha**.

Experimental automation of a fluidized-bed coffee roaster (hot-air popcorn popper, **read-only**, with no heater or fan actuation) organized across three integrated fronts:

- **Real-time thermal telemetry:** Type-K thermocouple via **MAX31855** on a **LilyGO T-Beam (ESP32)** board, with the probe placed inside the roasting chamber amidst the beans (4 samples/s every 250 ms + spike filter).
- **Computer vision (`visao/` & `calibracao/`):** camera capture with locked exposure and white balance, conversion to the **CIELAB** ($L^*, a^*, b^*$) color space, and supervised linear regression (`scikit-learn`) to measure the **Agtron** roast index in real time.
- **Web dashboard ("Caderno de torra"):** **FastAPI** backend + **React** frontend displaying the roast temperature curve, **RoR** (*Rate of Rise*, °C/min), development time (**DTR**), event markers, automatic CSV logging (`torras/`), and Agtron index.

> **Current status:** end-to-end thermal telemetry validated in real roasts (`torras/`). In the first real roast (`2026-10-06`), **1st crack** was marked at **1:24 (158.7 °C)** and **drop/discharge** at **2:54 (224.2 °C, peak ~225 °C)** with stable Wi-Fi throughout. The vision loop is integrated into the backend, awaiting physical camera mounting and calibration (`visao/modelo_agtron.pkl`).

---

## Architecture & Protocol

```
   [ T-Beam / ESP32 ]            [ FastAPI backend ]            [ React frontend ]
    WebSocket server    ---->     client + server     ---->      WebSocket client
     ws://<ip>:80/ws               /ws/temperatura                   (browser)
```

- **One-way data flow:** the T-Beam is the WebSocket **server** (`ws://<ip>/ws`, mDNS `torrador.local`); the backend initiates the connection. The frontend never talks directly to the board.
- **Persistence:** the backend keeps the current session in memory and writes each roast line-by-line to `torras/torra_YYYY-MM-DD_HHMMSS.csv`. Upon connecting, the browser immediately receives the current snapshot plus the full session `historico`.
- **Message types (`"tipo"`) and disconnect detection:**
  - `"leitura"`: new reading from the T-Beam (carries `temp` and `recebido_em`; the only type plotted on the chart).
  - `"tick"`: connection status notice (`offline`, `visao_motivo`) **without** `temp` or `recebido_em`, preventing duplicate chart points during outages.
  - Two independent watchdogs set `"offline": true`: **`ponte_tbeam()`** (reconnects and emits a `tick` every `3 s` when the socket closes) and **`vigia_dado_parado()`** (marks offline after `5 s` without valid data, even if the TCP socket stays open).

### T-Beam JSON (1x/s) & Backend Fields

```json
{ "temp": 187.4, "timestamp": 154320, "picos": 0 }
```

- `temp`: degrees °C or `null` if the sensor failed for the entire second (the board never repeats stale values; the backend logs and discards `null`).
- `timestamp`: ESP32 `millis()` since boot. `picos`: samples discarded by the spike filter since boot.

Fields added by the backend on `"tipo": "leitura"` messages:

| Field | Description |
|---|---|
| `recebido_em` | Backend wall-clock epoch (used on the chart X-axis so it never resets if the board reboots) |
| `tempo_torra` | Seconds elapsed since the first reading of the session (*CHARGE*) |
| `agtron_estimado` | Thermodynamic estimate based on temperature and elapsed time |
| `agtron_medido` | Optical measurement from the camera (`visao/agtron.py`), or `null` if unavailable/stale (> 10 s) |
| `visao_motivo` | Reason string when `agtron_medido` is `null` (camera disabled, missing model, overexposed ROI, etc.) |
| `offline` / `tbeam_uri` | Board connection state and target WebSocket URI |
| `eventos` | Session event markers (`amarelamento`, `crack1`, `crack2`, `descarga`) with roast time and temperature |

---

## Repository Structure

```
firmware/                  PlatformIO project (board = ttgo-t-beam, Espressif32 @ 7.1.0)
  platformio.ini           envs: ttgo-t-beam (USB) and ota (HTTP POST /update)
  src/main.cpp             thermocouple + dual Wi-Fi + WebSocket + mDNS + OTA
  include/filtro_picos.h   spike filter for spurious MAX31855 jumps
  include/credenciais.exemplo.h  template to create include/credenciais.h (git-ignored)
  scripts/gravar_ota.py    HTTP firmware uploader
  teste_termopar/          isolated sketch to test wiring and MAX31855 fault flags
backend/
  main.py                  WebSocket bridge, watchdog, vision loop & CSV logger (FastAPI)
  simulador_tbeam.py       fake WebSocket server (curve up to ~215 °C) for boardless testing
  requirements.txt         FastAPI, uvicorn, websockets, opencv-python-headless, numpy, scikit-learn
visao/
  camera.py                locked exposure/WB capture (v4l2 or dshow/WSL) + ROIs
  agtron.py                ROI crop -> CIELAB -> modelo_agtron.pkl -> moving average (5 frames)
calibracao/
  capturar_amostras.py     sample collection with optical lock and saturation check
  treinar.py               feature selection & LinearRegression training (leave-one-sample-out)
  CHECKLIST.md             benchtop calibration procedure (8 samples × 5 repetitions)
torras/                    real roast CSV logs, notes, and gerar_curva.py plot script
frontend/                  React + Vite + Recharts dashboard (src/App.jsx, src/estilos.css)
patches/                   reference patches for the legacy vision repository
```

---

## Getting Started

### 1. Firmware (T-Beam)

If PlatformIO is not installed yet (on Debian/Ubuntu with PEP 668, use a dedicated venv):

```bash
python3 -m venv ~/.venvs/platformio && ~/.venvs/platformio/bin/pip install platformio
export PATH="$HOME/.venvs/platformio/bin:$PATH"   # add to ~/.bashrc
```

> Tested with PlatformIO 6.2.0, `espressif32 @ 7.1.0`, `AsyncTCP 3.5.0`, `ESPAsyncWebServer 3.12.0`, `ArduinoJson 7.4.3`, and `Adafruit MAX31855 ^1.4.2` (usage: RAM 3.5%, Flash 66.9%).

Configure network credentials (**2.4 GHz** Wi-Fi only; without network at boot, the board keeps sampling and alternates between networks 1 and 2 every 15 s):

```bash
cd firmware
cp include/credenciais.exemplo.h include/credenciais.h
```

| Constant in `credenciais.h` | Description |
|---|---|
| `WIFI_SSID` / `WIFI_PASSWORD` | Primary network (tried first) |
| `WIFI_SSID_2` / `WIFI_PASSWORD_2` | Optional secondary network (e.g., mobile hotspot; delete lines if unused) |
| `WIFI_IP_2` | Optional static IP on the 2nd network (e.g., `192.168.137.50` for Windows Hotspot `netsh portproxy`) |
| `OTA_SENHA` | Mandatory password for network flashing (`POST /update`, user `ota`) |

- **1st flash (via USB cable):**
  ```bash
  pio run --target upload
  pio device monitor         # 115200 baud: note the printed IP (or look up "torrador" on the router)
  ```
- **Subsequent flashes (OTA over Wi-Fi, cable-free):** uses `POST /update` (instead of `espota`, so it works even when the PC is behind WSL2 NAT) and flashes the inactive partition with safe rollback:
  ```bash
  pio run -e ota -t upload --upload-port 192.168.0.42
  ```
- **Campus / Enterprise networks:** enable a **2.4 GHz Mobile Hotspot** on your laptop or phone and set it as `WIFI_SSID_2` beforehand (if your laptop Wi-Fi is on 5 GHz, connect it to a 2.4 GHz network or Ethernet so the adapter can broadcast a 2.4 GHz hotspot).

### 2. Backend (FastAPI) & Simulator

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# With the real board:
TBEAM_HOST=192.168.0.42 uvicorn main:app --reload --port 8000

# Or without the board (in another terminal run: python simulador_tbeam.py):
TBEAM_HOST=127.0.0.1 TBEAM_PORT=8080 uvicorn main:app --reload --port 8000
```

| Environment variable | Default | Description |
|---|---|---|
| `TBEAM_HOST` / `TBEAM_PORT` / `TBEAM_WS_PATH` | `192.168.0.50` / `80` / `/ws` | T-Beam WebSocket address |
| `TORRADOR_TORRAS_DIR` | `torras/` | Output directory for roast CSVs |
| `TORRADOR_VISAO` | `1` | `0` disables camera capture without uninstalling packages |
| `TORRADOR_VISAO_INTERVALO` | `2.0` | Interval (s) between optical camera measurements |
| `TORRADOR_CAMERA` | auto | Forces `v4l2` (`/dev/video0`) or `dshow` (`ffmpeg.exe` via WSL) |

| Route | Description |
|---|---|
| `GET /status` | Full diagnostics (connection, socket, active CSV, events, vision state, and latest reading) |
| `GET /historico` | Complete reading history for the current session |
| `POST /sessao/nova` | Closes the current CSV in `torras/`, resets timer/memory, and starts a new roast |
| `POST /evento/{nome}` | Marks `amarelamento`, `crack1`, `crack2`, or `descarga` on the chart and in the CSV |
| `WS /ws/temperatura` | WebSocket stream consumed by the frontend |

### 3. Frontend (React + Vite) & Verification

```bash
cd frontend
npm install && npm run dev   # opens at http://localhost:5173 (set VITE_BACKEND_HOST in .env if needed)
```

In the **"Caderno de torra"** dashboard:
- **Chain status:** green (`cadeia completa`), orange (`T-Beam desconectado` — retrying every 3 s), or red (`backend fora do ar`). CLI check: `curl localhost:8000/status`.
- **Metrics & alerts:** live temperature with thermal ruler, **RoR (°C/min)** computed over a 30 s rolling window (with high-RoR alert above `40 °C/min` after 60 s of roast), **development time and DTR (%)** after 1st crack, and event annotation buttons.

---

## Agtron: Estimated vs. Measured

1. **`agtron_estimado` (`estimar_agtron` in `backend/main.py`):** heuristic currently shown in the UI, computed from temperature ($140\text{..}215\text{ °C}$) and exposure time ($\tau = 240\text{ s}$):
   $$\text{progress} = f_{\text{temp}} \times (0.65 + 0.35 \times (1 - e^{-t/240})), \quad \text{Agtron} = 98 - (98 - 42) \times \text{progress}$$
   Enforces a one-way monotonic lock in `processar_leitura()` so the estimate never increases when temperature drops.
2. **`agtron_medido` (`visao/` & `calibracao/`):** real optical measurement. `visao/camera.py` locks exposure and white balance, crops `MIRA_AMOSTRA` (beans) and `MIRA_REFERENCIA` (gray card for `L_norm`), rejects overexposed frames (> 2% saturated pixels at 255), and converts to CIELAB. `calibracao/treinar.py` selects the best feature subset (`L_norm`, `L`, `L_norm,a,b`, `L,a,b`) via *leave-one-sample-out* validation and saves `visao/modelo_agtron.pkl` alongside the optical signature. Follow `calibracao/CHECKLIST.md` to collect samples and train.

---

## Thermocouple (MAX31855) & Spike Filter

- **Pinout (`firmware/src/main.cpp`):** `VIN -> 3V3` (**never 5 V**), `GND -> GND`, **`CLK = GPIO 13`**, **`CS = GPIO 14`**, **`DO = GPIO 25`**. Avoid strapping pins (`0, 2, 12, 15`) and pins reserved on the T-Beam for LoRa (`5, 18, 19, 23, 26, 27`), AXP192 (`21, 22`), and GPS (`12, 34`).
- **Isolated diagnostics & calibration:** use `firmware/teste_termopar` (`pio run -t upload && pio device monitor`) to test wiring, cold-junction internal temperature, and open/short fault flags without Wi-Fi.
  - Reference points: melting ice bath (~0 °C; measured **+1.5 °C** stable on 2026-09-30, within combined probe + chip tolerance) and boiling water (~97 °C in Varginha at ~940 m altitude).
  - **Polarity:** if temperature **drops** when holding the probe tip by hand, the thermocouple leads are inverted at the terminal block (do not trust wire insulation color; repeat the hand test whenever re-tightening the terminals).
- **Spike filter (`include/filtro_picos.h`):** discards 250 ms samples that jump **> 25 °C** from the previous reading (such as +256 °C bit flips caused by mechanical cable noise), while accepting real fast changes if **3 consecutive samples** agree (adding only a 0.5 s delay on step changes). Each discarded sample increments `picos` in the JSON payload.

---

## Next Steps

- Operate the camera alongside the thermocouple during roasting:
  - Physically mount the camera bracket (thermally decoupled from the hot-air outlet) and controlled LED lighting on the popper.
  - Run the calibration collection (`calibracao/CHECKLIST.md`) to generate `visao/modelo_agtron.pkl`.
  - Display `agtron_medido` alongside `agtron_estimado` in `frontend/src/App.jsx`.
  - Run real roast batches cross-referencing the thermal curve with the optical Agtron curve.
