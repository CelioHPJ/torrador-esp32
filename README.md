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

## Estado atual: temperatura REAL, só leitura

O firmware lê um **termopar tipo K** por um módulo **MAX31855**, com a média de 4
amostras por segundo. Foi testado ponta a ponta em 29/09/2026: placa, backend e
navegador conversando por Wi-Fi com a temperatura real.

O torrador é uma **pipoqueira elétrica de ar quente**, ligada direto na tomada.
A placa **só mede**: não controla a resistência nem a ventoinha, e por isso não
recebe comando nenhum. Não existe controle de potência nem de motor em nenhuma
das três partes.

---

## Arquitetura

```
   [ T-Beam / ESP32 ]            [ backend FastAPI ]            [ frontend React ]
    servidor WebSocket   ---->    cliente + servidor    ---->     cliente WebSocket
      ws://<ip>/ws                 /ws/temperatura                 (navegador)
```

Os dados andam num sentido só: da placa para a tela.

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
  "picos": 0
}
```

`temp` vem `null` quando o sensor falhou o segundo inteiro (termopar solto, fio
partido, módulo sem comunicação). A placa **não repete o último valor bom**: um
número congelado com cara de medida é pior que um buraco na curva. O backend
descarta essas leituras com um aviso no log.

`picos` é quantas amostras o filtro de picos já descartou desde o boot (ver
[Filtro de picos](#filtro-de-picos)). Vai pela rede porque a placa roda longe
do USB: se ele crescer sem parar durante uma torra, há ruído no cabo.

`timestamp` é o `millis()` do ESP32 (tempo desde o boot da placa). O backend
acrescenta três campos antes de repassar ao frontend:

| Campo | De onde vem |
|---|---|
| `recebido_em` | relógio real do backend (epoch). É o que o gráfico usa no eixo X, porque `millis()` volta a zero se a placa reiniciar |
| `tempo_torra` | segundos desde a primeira leitura da sessão (o "CHARGE") |
| `agtron_estimado` | **estimativa**, não medição — ver a seção abaixo |

---

## Estrutura da pasta

```
firmware/                  projeto PlatformIO (board = ttgo-t-beam)
  platformio.ini           env de USB (ttgo-t-beam) e env de rede (ota)
  src/main.cpp             leitura do termopar + Wi-Fi + WebSocket + OTA
  include/filtro_picos.h   descarte de amostras isoladas absurdas
  include/credenciais.exemplo.h  modelo; o credenciais.h real fica fora do git
  scripts/gravar_ota.py    envia o firmware para a placa pela rede
  teste_termopar/          sketch isolado de teste do MAX31855
backend/
  main.py                  ponte T-Beam <-> frontend (FastAPI)
  requirements.txt
  simulador_tbeam.py       T-Beam FALSO, para testar sem a placa
frontend/
  package.json
  index.html               fontes (Fraunces + Inter)
  src/App.jsx              a tela: conexão, leitura e curva
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

Ocupação resultante: **RAM 3,5%** e **Flash 66,9%** (876 KB dos 1,25 MB de
cada partição de aplicativo — são duas, por causa da gravação pela rede).

Antes de gravar, crie o arquivo de credenciais a partir do modelo e preencha:

```bash
cd firmware
cp include/credenciais.exemplo.h include/credenciais.h
```

| Constante | O que é |
|---|---|
| `WIFI_SSID` / `WIFI_PASSWORD` | rede principal, tentada primeiro |
| `WIFI_SSID_2` / `WIFI_PASSWORD_2` | segunda rede, **opcional** (apague as duas linhas se não usar) |
| `OTA_SENHA` | senha da gravação pela rede; invente uma |

O `credenciais.h` está no `.gitignore`: as senhas nunca vão para o
repositório. O ESP32 só fala Wi-Fi de **2,4 GHz**.

Sem rede, a placa **não para de medir**: tenta a principal e a segunda no boot,
e depois fica alternando entre as duas a cada 15 s até uma responder.

A **primeira** gravação é pelo USB, com o T-Beam ligado no computador:

```bash
pio run --target upload        # (dentro de firmware/) compila e grava pelo USB
pio device monitor             # abre o monitor serial (115200)
```

No monitor serial vai aparecer o IP da placa:

```
[WIFI] conectado em "minha-rede"!
[WIFI] IP do T-Beam: 192.168.0.42
[WIFI] WebSocket:    ws://192.168.0.42/ws
       ^^^ anote este IP: é o TBEAM_HOST do backend ^^^
```

Sem o USB não há serial para ler o IP. Ele aparece na lista de aparelhos
conectados do roteador ou do hotspot, com o nome **`torrador`**.

#### Gravar pela rede, sem cabo (OTA)

Depois da primeira gravação pelo USB, as seguintes vão pelo Wi-Fi:

```bash
cd firmware
pio run -e ota -t upload --upload-port 192.168.0.42
```

O `scripts/gravar_ota.py` envia o firmware num `POST /update` para a placa,
com a `OTA_SENHA` lida do `credenciais.h`. O firmware novo é gravado na
partição que **não** está em uso: se a transferência cair no meio, a placa
continua bootando o firmware anterior. Sem a senha, a placa responde 401.

> **Por que HTTP e não o OTA padrão do Arduino (`espota`)?** No `espota` é a
> placa que abre a conexão de volta para o computador. Com o backend no WSL2
> em modo NAT (IP `172.20.x`), a placa não alcança esse endereço e a gravação
> falha. No HTTP quem conecta é só o computador — o mesmo sentido que o
> backend já usa para ler a temperatura.

> **Grave a rede do lugar para onde vai ANTES de ir.** A gravação pela rede
> precisa de uma rede que a placa já conheça. Se ela chegar num lugar sem
> nenhuma das duas redes gravadas, a única saída volta a ser o cabo USB.

#### Redes de faculdade: use o hotspot do notebook

O T-Beam precisa estar **na mesma rede** do computador que roda o backend.
Redes institucionais costumam impedir isso de três formas: login com usuário
(que o firmware não faz), isolamento entre aparelhos e firewall.

A saída é o **Hotspot Móvel do Windows**: o notebook entra na rede da
faculdade (fazendo o login, se houver) e cria a própria rede, onde a placa
entra como `WIFI_SSID_2`. A placa conversa direto com o notebook e nem
precisa de internet. O IP dela fica na faixa `192.168.137.x`, listado na
própria tela do hotspot.

Na configuração do hotspot, escolha a banda de **2,4 GHz**. Muitas placas
Wi-Fi só criam o hotspot na mesma banda da rede em que estão conectadas: se o
notebook estiver em 5 GHz, a de 2,4 pode não funcionar — conecte-o à rede
2,4 GHz da faculdade ou por cabo. O hotspot do celular também serve.

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
2. **Temperatura atual** chegando **a cada segundo**, perto da temperatura da
   sala com a pipoqueira desligada.
3. **Curva** subindo quando você segura a ponta do termopar na mão. Se ela
   **cair**, os fios do termopar estão invertidos.
4. **Teste da queda**: desligue o T-Beam da tomada. Em poucos segundos a faixa
   fica **laranja** e o backend passa a logar uma tentativa de reconexão a cada
   3 segundos, sem travar. Religue a placa: a faixa volta a verde sozinha.
   Abrir uma aba nova **durante** a queda também deve mostrar a faixa laranja
   de imediato, e não uma tela vazia esperando dados.

Checagem pelo terminal, se quiser confirmar sem o navegador:

```bash
curl localhost:8000/status
```

### Testar sem a placa (`simulador_tbeam.py`)

Dá para validar backend + frontend **sem ter o T-Beam gravado ou ligado**. O
simulador sobe no PC um WebSocket que fala o mesmo protocolo do firmware, com
uma curva de torra inventada (sobe rumo a ~215 °C, como a pipoqueira no
máximo). Num quarto terminal:

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
sobe durante a sessão. Torrar é irreversível — sem a trava, o calor diminuindo
faria o grão "clarear" na tela. A trava fica fora de `estimar_agtron()` de
propósito, para que aquela função continue sendo um cálculo puro e fácil de
trocar.

Nada fora dessa função depende de como o número é calculado: o frontend só
recebe o campo `agtron_estimado` pronto.

---

## Termopar (MAX31855)

A leitura real vive em `lerTemperatura()`, em `firmware/src/main.cpp`. Os pinos
são **CLK = GPIO 13, CS = GPIO 14, DO = GPIO 25**, os mesmos de
`firmware/teste_termopar`. Não use o GPIO 12 para o DO: é pino de *strapping* e,
no T-Beam, é do GPS.

### Teste o sensor isolado quando algo mudar na fiação

Existe um sketch separado que **só lê o sensor e imprime no serial** — sem
Wi-Fi, sem WebSocket, sem backend:

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

**Resultado em 30/09/2026:** no gelo o conjunto marcou **+1,5 °C** (estável em
1,44..1,50 °C por 1 min), dentro da tolerância somada do chip (±2 °C) e do
termopar (±2,2 °C). **Nenhuma correção foi aplicada**: com um ponto só não dá
para saber se o erro é fixo ou cresce com a temperatura. Falta a fervura. Um
banho com água demais e pouco gelo fica a 1–2 °C e engana o teste — encha o
copo de gelo.

Se a temperatura **cai** quando você esquenta a ponta, os fios do termopar
estão invertidos — é o sintoma clássico, não um defeito do módulo. Não confie
na cor dos fios: no termopar deste projeto, o "amarelo = positivo" do padrão
americano estava errado, e só o teste da mão mostrou. **Refaça o teste da mão
toda vez que mexer nos bornes** — a polaridade já voltou invertida depois de
um reaperto. E alimente o MAX31855 em **3,3 V**, nunca em 5 V.

### Filtro de picos

Em 30/09/2026, mexer no cabo gerou duas amostras isoladas de ~265 °C no meio de
leituras de 1 a 30 °C. O mesmo tamanho (~+256 °C) nas duas aponta para um bit
trocado na leitura do chip, não para calor — e uma amostra dessas, mesmo diluída
na média de 4, vira um degrau de dezenas de graus na curva.

`include/filtro_picos.h` descarta a amostra que salta **mais de 25 °C** da
anterior (as amostras são de 250 ms). Mudança real e rápida não é cortada: se
**3 amostras seguidas** concordam no valor novo, ele é aceito, com 0,5 s de
atraso só naquele degrau. O termopar mergulhado em água fervendo sobe ~15 °C
nos primeiros 250 ms, e uma torra de ar quente não chega perto disso.

A lógica não depende do Arduino, e foi testada no PC com os picos reais e com
os mergulhos em gelo e fervura. Cada descarte sai na serial como
`[SENSOR] pico descartado` e soma no campo `picos` do JSON.

O sketch traz a tabela de ligação e, importante, a lista dos GPIOs que o
T-Beam **já usa** para LoRa, GPS e AXP192 (mesmo sem esses periféricos estarem
ativos nesta fase), além dos pinos de *strapping* que podem impedir a placa de
dar boot. Confira o pinout da sua revisão antes de soldar.

---

## Próximos passos (fora do escopo desta etapa)

- Validar o termopar em água fervendo (~97 °C); o gelo já foi feito.
- Uma torra real: leitura na faixa de 200 °C e o contador `picos` numa torra
  inteira.
- Posicionar o termopar na câmara da pipoqueira, no meio dos grãos.
- Integração do backend desta pasta com o frontend do repositório principal,
  juntando a curva de temperatura com o índice Agtron estimado pela câmera.
