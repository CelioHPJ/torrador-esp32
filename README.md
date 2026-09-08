# Torrador de café — módulo T-Beam (temperatura por hardware)

Projeto de extensão — **CEFET-MG Varginha**.

Esta pasta é a **base da nova frente de hardware** do projeto de automação de
torra de café: em vez de depender só da câmera + visão computacional, a torra
passa a ser monitorada por um **sensor de temperatura físico** ligado a uma
placa **LilyGO T-Beam (ESP32)**.

Ela é **isolada de propósito** do repositório principal do projeto (aquele que
tem o pipeline de visão computacional do índice Agtron e o frontend React
completo). Nada aqui depende daquele repositório, e o repositório principal não
deve ser tocado enquanto esta frente estiver sendo desenvolvida. A integração
entre os dois acontece numa etapa futura: o frontend "de verdade" vai consumir
o **backend** desta pasta.

---

## Estado atual: temperatura SIMULADA

O módulo **MAX31855** (conversor do termopar tipo K) ainda não chegou. Por isso
**toda a estrutura de software já está pronta e funcionando**, alimentada por um
valor de temperatura simulado dentro do firmware.

Quando o MAX31855 chegar, a expectativa é que a **única** alteração necessária
seja a função `lerTemperatura()` do firmware — ver a seção
[O que trocar quando o MAX31855 chegar](#o-que-trocar-quando-o-max31855-chegar).
Backend e frontend não mudam nada.

O acionamento real do **SSR** (relé de estado sólido que controla a resistência
de aquecimento em corrente alternada) e do **motor de agitação** também ainda
não existe: os comandos de potência e motor são recebidos e guardados em
memória, e voltam refletidos na próxima leitura transmitida. É o suficiente para
validar toda a cadeia de comunicação antes de ligar qualquer coisa na tomada.

---

## Arquitetura

```
   [ T-Beam / ESP32 ]            [ backend FastAPI ]            [ frontend React ]
    servidor WebSocket   ---->    cliente + servidor    ---->     cliente WebSocket
      ws://<ip>/ws                 /ws/temperatura                 (navegador)
                                   POST /comando
        ^                                 |
        +------- comando (WebSocket) -----+
```

Regras da arquitetura:

- O **T-Beam é o SERVIDOR** WebSocket. Quem abre a conexão é o backend.
- O **frontend nunca fala direto com o T-Beam** — só com o backend.
- O backend guarda o **histórico da sessão em memória** (lista Python, sem banco).
- Se o T-Beam cair, o backend **não trava nem morre**: reconecta em segundo
  plano e marca `"offline": true` nas mensagens enviadas ao frontend.

### Como o frontend fica sabendo que a placa caiu

Existe **uma única fonte** desse aviso: o próprio laço de reconexão do backend
(`ponte_tbeam()` em `backend/main.py`). A cada tentativa frustrada — ou seja, a
cada `INTERVALO_RECONEXAO_S`, hoje 3 segundos — ele reenvia o estado com
`"offline": true` para todos os frontends conectados. Enquanto a placa estiver
fora, o aviso se repete nesse ritmo.

O caso do **frontend que abre no meio da queda** é resolvido em outro ponto, e
não por repetição de aviso: assim que o navegador conecta em `/ws/temperatura`,
o backend já envia um retrato do estado atual (mais o histórico da sessão).
Então a flag `offline` chega na **primeira** mensagem, sem esperar nada.

> Houve, até a validação de 15 minutos, uma segunda fonte de aviso: uma tarefa
> de *heartbeat* que reavisava a cada 5 s enquanto estivesse offline. Ela foi
> **removida** por ser redundante — as duas fontes juntas mandavam ~34
> mensagens durante uma queda de 60 s, contra ~20 agora, e o caso que ela dizia
> cobrir já estava coberto pelo retrato inicial descrito acima.

### JSON que o T-Beam transmite (1x por segundo)

```json
{
  "temp": 187.4,
  "timestamp": 154320,
  "potencia_atual": 80,
  "motor_ligado": true
}
```

`timestamp` é o `millis()` do ESP32 (tempo desde o boot da placa). O backend
acrescenta três campos antes de repassar ao frontend:

| Campo | De onde vem |
|---|---|
| `recebido_em` | relógio real do backend (epoch). É o que o gráfico usa no eixo X, porque `millis()` volta a zero se a placa reiniciar |
| `tempo_torra` | segundos desde a primeira leitura da sessão (o "CHARGE") |
| `agtron_estimado` | **estimativa**, não medição — ver a seção abaixo |

### Comandos — decisão de projeto

O firmware aceita comandos por **duas vias**, e as duas fazem exatamente a mesma
coisa:

1. **Mensagem JSON no mesmo WebSocket** — é a via usada pelo backend. Escolhida
   por ser a mais simples: o backend já tem a conexão aberta com a placa, então
   não precisa abrir uma conexão HTTP nova a cada comando nem saber de uma porta
   separada.
2. **`POST /comando` no próprio T-Beam** — via de conveniência, só para testar na
   mão com `curl`, sem precisar de um cliente WebSocket.

Corpo aceito nas duas vias (os campos são opcionais e independentes):

```json
{ "potencia": 75 }
{ "motor": "on" }
{ "potencia": 75, "motor": "on" }
```

---

## Estrutura da pasta

```
firmware/                  projeto PlatformIO (board = ttgo-t-beam)
  platformio.ini
  src/main.cpp             <- lerTemperatura() vive aqui
  teste_termopar/          sketch isolado de teste do MAX31855 (descartável)
backend/
  main.py                  ponte T-Beam <-> frontend (FastAPI)
  requirements.txt
  simulador_tbeam.py       T-Beam FALSO, para testar sem a placa
frontend/
  package.json
  index.html               fontes (Fraunces + Inter)
  src/App.jsx              a tela: conexão, curva e comandos
  src/estilos.css          sistema visual "caderno de torra"
README.md
```

---

## Como rodar

São **três terminais**, um para cada parte. Todos os comandos abaixo partem da
raiz desta pasta.

### 1. Firmware (T-Beam)

Precisa do PlatformIO instalado. Se ainda não tiver:

```bash
python3 -m venv ~/.venvs/platformio
~/.venvs/platformio/bin/pip install platformio
```

Isso deixa o comando em `~/.venvs/platformio/bin/pio`. Para poder chamar só
`pio`, adicione ao `~/.bashrc`:

```bash
export PATH="$HOME/.venvs/platformio/bin:$PATH"
```

> Neste Debian, `pip install --user platformio` **não funciona**: o Python do
> sistema é protegido (PEP 668) e o pip recusa a instalação. Daí o venv
> dedicado. Alternativa: instalar a extensão *PlatformIO IDE* no VS Code, que
> traz o Core junto.

A primeira compilação baixa a plataforma `espressif32` e o toolchain do ESP32
(cerca de 1 GB) — pode demorar vários minutos. As seguintes levam segundos.

O firmware já foi compilado com sucesso nesta configuração:

| Componente | Versão |
|---|---|
| PlatformIO Core | 6.2.0 |
| Plataforma | Espressif 32 @ 7.1.0 |
| AsyncTCP | 3.5.0 |
| ESPAsyncWebServer | 3.12.0 |
| ArduinoJson | 7.4.3 |

Ocupação resultante: **RAM 3,4%** (43.976 B) e **Flash 63,5%** (832.705 B de
4 MB) — bastante folga para o código do MAX31855 e do SSR depois.

Antes de gravar, abra `firmware/src/main.cpp` e troque as duas constantes do
topo pelos dados da sua rede Wi-Fi:

```cpp
const char *WIFI_SSID     = "SUA_REDE_AQUI";
const char *WIFI_PASSWORD = "SUA_SENHA_AQUI";
```

Com o T-Beam ligado no USB:

```bash
cd firmware
pio run --target upload        # compila e grava
pio device monitor             # abre o monitor serial (115200)
```

No monitor serial vai aparecer o IP da placa:

```
[WIFI] conectado!
[WIFI] IP do T-Beam: 192.168.0.42
[WIFI] WebSocket:    ws://192.168.0.42/ws
       ^^^ anote este IP: é o TBEAM_HOST do backend ^^^
```

> **Atenção:** o T-Beam precisa estar **na mesma rede Wi-Fi** do computador que
> roda o backend. Redes de faculdade com isolamento entre clientes costumam
> bloquear essa conexão — nesse caso, use um roteador próprio ou o hotspot do
> celular.

### 2. Backend (FastAPI)

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

TBEAM_HOST=192.168.0.42 uvicorn main:app --reload --port 8000
```

Troque `192.168.0.42` pelo IP que apareceu no monitor serial. Variáveis
aceitas: `TBEAM_HOST` (padrão `192.168.0.50`), `TBEAM_PORT` (padrão `80`),
`TBEAM_WS_PATH` (padrão `/ws`).

Rotas do backend:

| Rota | O que faz |
|---|---|
| `GET /status` | diagnóstico rápido: online/offline, nº de leituras, última leitura |
| `GET /historico` | curva completa da sessão atual |
| `POST /comando` | recebe `{"potencia":0-100}` e/ou `{"motor":"on"\|"off"}` e repassa ao T-Beam |
| `WS /ws/temperatura` | stream que o frontend consome |

### 3. Frontend (React + Vite)

```bash
cd frontend
npm install
npm run dev
```

Abre em <http://localhost:5173>. Se o backend não estiver em `localhost:8000`,
copie `.env.example` para `.env` e ajuste `VITE_BACKEND_HOST`.

---

## Como confirmar que a cadeia inteira está funcionando

Com os três rodando, abra <http://localhost:5173>. A confirmação é visual:

1. **Faixa verde** no topo: `CADEIA COMPLETA OK — T-Beam -> backend -> navegador`.
   Se estiver **laranja**, o backend está no ar mas a placa não responde
   (flag `offline`). Se estiver **vermelha**, o backend é que não está no ar.
2. **Temperatura atual** mudando **a cada segundo**, partindo de ~25 °C.
3. **Curva** subindo rápido no começo, desacelerando e estabilizando perto de
   ~215 °C (a placa inicia com potência 100%).
4. **Teste do comando**: coloque a potência em `30` e clique em
   *Enviar potência*. Em poucos segundos a curva deve **virar para baixo** e se
   estabilizar num patamar mais baixo — isso prova o caminho de volta completo
   (navegador → backend → T-Beam). Clique em *Motor ON* e o campo *Motor* deve
   passar a `ligado` na leitura seguinte, provando o mesmo caminho.
5. **Teste da queda**: desligue o T-Beam da tomada. Em poucos segundos a faixa
   fica **laranja** e o backend passa a logar uma tentativa de reconexão a cada
   3 segundos, sem travar. Religue a placa: a faixa volta a verde sozinha.
   Abrir uma aba nova **durante** a queda também deve mostrar a faixa laranja
   de imediato, e não uma tela vazia esperando dados.

Checagem pelo terminal, se quiser confirmar sem o navegador:

```bash
curl localhost:8000/status
curl -X POST localhost:8000/comando -H "Content-Type: application/json" -d '{"potencia":30}'
```

### Testar sem a placa (`simulador_tbeam.py`)

Dá para validar backend + frontend **sem ter o T-Beam gravado ou ligado**. O
simulador sobe no PC um WebSocket que fala o mesmo protocolo do firmware, com a
mesma curva de temperatura. Num quarto terminal:

```bash
cd backend
source .venv/bin/activate
python simulador_tbeam.py                 # ws://127.0.0.1:8080/ws
```

E aponte o backend para ele:

```bash
TBEAM_HOST=127.0.0.1 TBEAM_PORT=8080 uvicorn main:app --reload --port 8000
```

O simulador é **ferramenta de teste** — não faz parte do produto final. Quando a
placa estiver na rede, use o IP dela e esqueça este arquivo.

---

## Agtron estimado — o que é e o que não é

O painel mostra um **Agtron estimado**. Ele **não é medido**: é um chute
plausível calculado a partir de duas coisas que o sistema realmente sabe — a
temperatura atual e há quanto tempo a torra começou.

O Agtron de verdade é uma medida **óptica**, lida da cor do grão. Numa fase
futura ele virá do pipeline de visão computacional que já existe no repositório
principal (câmera + modelo que estima o índice pela cor). O rótulo na tela diz
"Agtron estimado", e não "Agtron", justamente para essa diferença não se perder
durante uma apresentação.

> **Arquivo:** `backend/main.py`
> **Função:** `estimar_agtron(temperatura, tempo_decorrido)`

Como o chute é feito: dois fatores entre 0 e 1, multiplicados.

1. **Fator de temperatura** — o quanto passamos de 140 °C (onde o grão começa a
   escurecer; abaixo disso ele está secando, não dourando) rumo aos 215 °C do
   patamar. Este fator porteia o resto: sem calor, não há cor.
2. **Fator de tempo** — saturação exponencial `1 - e^(-t/240s)`, que traduz o
   fato de o grão escurecer só por permanecer no calor, mesmo com a temperatura
   parada. É o "tempo de desenvolvimento" que o torrador controla no fim.

```
progresso = fator_temperatura × (0,65 + 0,35 × fator_tempo)
Agtron    = 98 − (98 − 42) × progresso
```

Chegar ao patamar leva o grão a cerca de dois terços do caminho (Agtron ~62,
torra média); o terço final só vem de **permanecer** lá — que é o comportamento
certo, já que quem decide entre torra média e escura é o tempo de permanência e
não o pico de temperatura. Numa torra de 12 minutos, o valor fica parado em 98
durante os primeiros ~5 minutos (a fase de secagem) e só então começa a cair.

Há ainda uma **trava de sentido único** em `processar_leitura()`: o valor nunca
sobe durante a sessão. Torrar é irreversível — sem a trava, baixar a potência
faria o grão "clarear" na tela. A trava fica fora de `estimar_agtron()` de
propósito, para que aquela função continue sendo um cálculo puro e fácil de
trocar.

Nada fora dessa função depende de como o número é calculado: o frontend só
recebe o campo `agtron_estimado` pronto.

---

## O que trocar quando o MAX31855 chegar

Só um lugar:

> **Arquivo:** `firmware/src/main.cpp`
> **Função:** `lerTemperatura()`

É a única função que gera o valor de temperatura, e ela é chamada em um único
ponto do código (`transmitirLeitura()`, uma vez por ciclo de transmissão). Todo
o resto — conexão Wi-Fi, servidor WebSocket, tratamento de comandos, formato do
JSON, backend e frontend — **continua igual**.

### Passo 0: teste o sensor isolado ANTES de integrar

Não integre de cara. Existe um sketch separado que **só lê o sensor e imprime
no serial** — sem Wi-Fi, sem WebSocket, sem backend:

```bash
cd firmware/teste_termopar
pio run --target upload
pio device monitor
```

Se você juntar tudo de uma vez e o valor vier errado, fica a dúvida: é o
módulo, a solda, o pino escolhido ou o código? Testando isolado sobra uma
variável só. O sketch já identifica os erros que o MAX31855 reporta (termopar
aberto, curto para GND, curto para VCC) e mostra a temperatura da junta fria
do chip, que serve de sanidade — ela deve ficar perto da temperatura da sala.

Dois pontos de referência para conferir se a leitura está correta:

| Referência | Esperado |
|---|---|
| Água com gelo derretendo | ~0 °C |
| Água fervendo | **~97 °C** em Varginha (a ~940 m, a água ferve abaixo de 100 °C) |

Se a temperatura **cai** quando você esquenta a ponta, os fios do termopar
estão invertidos (no padrão americano, amarelo = positivo) — é o sintoma
clássico, não um defeito do módulo. E alimente o MAX31855 em **3,3 V**, nunca
em 5 V.

O sketch traz a tabela de ligação e, importante, a lista dos GPIOs que o
T-Beam **já usa** para LoRa, GPS e AXP192 (mesmo sem esses periféricos estarem
ativos nesta fase), além dos pinos de *strapping* que podem impedir a placa de
dar boot. Confira o pinout da sua revisão antes de soldar.

### Depois que o teste isolado passar

O `main.cpp` principal tem o passo a passo detalhado num comentário em cima da
função. Em resumo:

1. Em `firmware/platformio.ini`, descomente a dependência
   `adafruit/Adafruit MAX31855 library`.
2. No topo de `main.cpp`, inclua `<SPI.h>` e `<Adafruit_MAX31855.h>`, defina os
   GPIOs de `CLK`/`CS`/`DO` e instancie o objeto do termopar.
3. Chame `termopar.begin()` no `setup()`.
4. Substitua o corpo de `lerTemperatura()` pela leitura real
   (`termopar.readCelsius()`), tratando `NaN` — que é o que a biblioteca
   devolve quando o termopar está solto ou com mau contato.

> **Cuidado ao escolher os GPIOs:** o T-Beam já usa vários pinos para LoRa, GPS e
> para o gerenciador de energia AXP192, mesmo que esses periféricos não estejam
> sendo usados nesta fase. Confira o pinout da sua revisão de placa antes de
> soldar.

---

## Próximos passos (fora do escopo desta etapa)

- Leitura real do termopar (MAX31855) — a troca descrita acima.
- Acionamento do SSR para controle real de potência da resistência (a carga é
  AC, então um PWM comum do microcontrolador não serve).
- Acionamento do motor de agitação.
- Integração do backend desta pasta com o frontend do repositório principal,
  juntando a curva de temperatura com o índice Agtron estimado pela câmera.
