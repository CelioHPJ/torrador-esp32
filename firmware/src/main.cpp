/* ==========================================================================
 *  TORRADOR DE CAFE - Firmware do modulo sensor
 *  Placa: LilyGO T-Beam (ESP32)  |  PlatformIO board = ttgo-t-beam
 *  CEFET-MG Varginha - Projeto de extensao
 * ==========================================================================
 *
 *  PAPEL DESTA PLACA NA ARQUITETURA:
 *
 *      [T-Beam]  --WebSocket-->  [backend FastAPI]  --WebSocket-->  [React]
 *       servidor                     cliente/ponte                   cliente
 *
 *  O T-Beam e o SERVIDOR WebSocket. Quem inicia a conexao e o backend.
 *  O frontend NUNCA fala direto com esta placa.
 *
 *  O QUE ELE TRANSMITE (1x por segundo, para todos os clientes conectados):
 *      {
 *        "temp":            23.7,     // float, graus Celsius
 *        "timestamp":       154320,   // millis() do ESP32
 *        "potencia_atual":  80,       // int 0-100
 *        "motor_ligado":    true      // bool
 *      }
 *
 *  COMO ELE RECEBE COMANDOS  (as duas vias fazem exatamente a mesma coisa):
 *
 *    1) VIA PRINCIPAL - mensagem de texto JSON no MESMO WebSocket.
 *       E a via usada pelo backend, porque ele ja tem a conexao aberta:
 *       nao precisa abrir um socket HTTP novo a cada comando, nao precisa
 *       saber a porta HTTP separadamente e o comando chega na hora.
 *
 *    2) VIA DE CONVENIENCIA - HTTP POST /comando, para testar na unha com
 *       curl sem precisar de um cliente WebSocket. Mesmo corpo JSON.
 *
 *       Corpo aceito (os dois campos sao opcionais e independentes):
 *           { "potencia": 0..100 }
 *           { "motor": "on" | "off" }
 *           { "potencia": 75, "motor": "on" }
 *
 *  ATENCAO: NADA de hardware e acionado ainda. Comandos apenas atualizam as
 *  variaveis internas, que voltam refletidas na proxima leitura transmitida.
 *  O acionamento real (SSR da resistencia + motor de agitacao) entra depois.
 * ========================================================================== */

#include <Arduino.h>
#include <WiFi.h>
#include <AsyncTCP.h>
#include <ESPAsyncWebServer.h>
#include <ArduinoJson.h>

/* ------------------------------------------------------------------------ */
/*  CREDENCIAIS DA REDE                                                      */
/* ------------------------------------------------------------------------ */
/*  Ficam FORA do git, em include/credenciais.h. Antes de gravar a placa:    */
/*                                                                           */
/*      cp include/credenciais.exemplo.h include/credenciais.h               */
/*      (e preencha o SSID e a senha no arquivo copiado)                     */
/*                                                                           */
/*  O arquivo copiado esta no .gitignore. Este esquema existe para que uma   */
/*  senha preenchida as pressas para testar nunca acabe publicada junto com  */
/*  um commit de outra coisa.                                                */
/* ------------------------------------------------------------------------ */
#if __has_include("credenciais.h")
  #include "credenciais.h"
#else
  #error "Falta include/credenciais.h -- copie de include/credenciais.exemplo.h e preencha."
#endif

const uint16_t PORTA_HTTP        = 80;     // servidor HTTP + WebSocket
const char    *CAMINHO_WEBSOCKET = "/ws";  // ws://<ip-do-tbeam>/ws
const uint32_t INTERVALO_ENVIO_MS = 1000;  // periodo de transmissao (1 s)

// Superamostragem: o sensor e lido varias vezes entre dois envios e o que vai
// para a rede e a MEDIA das amostras. Reduz o ruido de leitura por um fator de
// raiz(N) - 4 amostras derrubam o ruido pela metade - sem aumentar o trafego,
// o tamanho da serie no grafico nem o custo de render do frontend.
// 250 ms respeita com folga o tempo de conversao do MAX31855 (~100 ms).
const uint32_t INTERVALO_AMOSTRAGEM_MS = 250;   // 4 amostras por envio

/* ------------------------------------------------------------------------ */
/*  ESTADO DA TORRA (o "modelo" que a placa mantem em memoria)               */
/* ------------------------------------------------------------------------ */
int  potenciaAtual = 100;    // 0-100. Comeca em 100 para que a curva suba
                             // sozinha assim que a placa liga e da para
                             // validar a cadeia inteira sem mandar comando.
bool motorLigado   = false;  // desligado ate alguem mandar {"motor":"on"}

AsyncWebServer servidor(PORTA_HTTP);
AsyncWebSocket ws(CAMINHO_WEBSOCKET);

uint32_t ultimoEnvio   = 0;
uint32_t ultimaAmostra = 0;

// Quanto o setup() espera pelo Wi-Fi ANTES DE SEGUIR SEM ELE. Existe porque
// ficar presto esperando rede e pior do que rodar sem ela: sem terminar o
// setup(), a placa nao amostra, nao responde e nao diz o que houve -- ela so
// parece morta. Um roteador reiniciando na hora errada bastava para isso.
const uint32_t LIMITE_CONEXAO_MS = 20000;

// De quanto em quanto tempo tentar de novo depois de desistir. Nao encoste
// em valores pequenos: cada tentativa mexe no radio e atrapalha a amostragem.
const uint32_t INTERVALO_RECONEXAO_MS = 15000;

// Periodo do aviso de "estou offline". Sem isto a serial vira uma parede de
// mensagens repetidas e esconde o que importa.
const uint32_t INTERVALO_AVISO_OFFLINE_MS = 5000;

uint32_t ultimaTentativaWiFi = 0;
uint32_t ultimoAvisoOffline  = 0;
bool     estavaConectado     = false;

/* ==========================================================================
 *  #############   ATENCAO - PONTO DE TROCA DO SENSOR   ####################
 * ==========================================================================
 *
 *      ESTA E A UNICA FUNCAO QUE SERA SUBSTITUIDA QUANDO O MODULO
 *      MAX31855 (+ termopar tipo K) CHEGAR FISICAMENTE.
 *
 *  Hoje ela devolve um valor SIMULADO. Amanha ela devolve a leitura real
 *  via SPI usando a biblioteca "Adafruit MAX31855". A troca e pontual:
 *
 *      NENHUMA outra parte deste arquivo precisa mudar.
 *      - conexao Wi-Fi:            nao muda
 *      - servidor WebSocket:       nao muda
 *      - tratamento de comandos:   nao muda
 *      - formato do JSON enviado:  nao muda
 *      - backend e frontend:       nao mudam
 *
 *  ---------------------------------------------------------------------
 *  COMO SERA A SUBSTITUICAO (passo a passo, quando o modulo chegar):
 *
 *    1) platformio.ini -> descomente:
 *           adafruit/Adafruit MAX31855 library @ ^1.4.2
 *
 *    2) No topo deste arquivo:
 *           #include <SPI.h>
 *           #include <Adafruit_MAX31855.h>
 *           #define PINO_MAX_CLK  13
 *           #define PINO_MAX_CS   14
 *           #define PINO_MAX_DO   25
 *           Adafruit_MAX31855 termopar(PINO_MAX_CLK, PINO_MAX_CS, PINO_MAX_DO);
 *
 *       USE EXATAMENTE OS MESMOS PINOS DE firmware/teste_termopar, que sao
 *       os acima. Se o teste isolado passou com uma fiacao, integrar com
 *       outra joga fora a unica coisa que aquele teste provou.
 *
 *       EM ESPECIAL, NAO USE O GPIO 12 PARA O DO. Este comentario ja mandou
 *       usar 12 e estava errado em dois sentidos: 12 e pino de strapping
 *       (lido durante o boot, pode impedir a placa de ligar) e no T-Beam ele
 *       ainda e usado pelo GPS. O erro so apareceria DEPOIS da integracao,
 *       com o sensor ja provado funcionando -- e a culpa cairia na integracao.
 *
 *       (varios pinos do T-Beam ja sao usados por LoRa/GPS/AXP192 mesmo sem
 *        usarmos esses perifericos nesta fase; evite tambem GPIO 0, 2 e 15)
 *
 *    3) Dentro de setup(), antes de conectarWiFi():
 *           termopar.begin();
 *
 *    4) Troque TODO o corpo desta funcao por:
 *           double c = termopar.readCelsius();
 *           if (isnan(c)) {              // termopar solto / falha de leitura
 *               return temperaturaAtual; // repete o ultimo valor valido
 *           }
 *           temperaturaAtual = (float) c;
 *           return temperaturaAtual;
 *
 *       OBS: esta funcao agora e chamada a cada INTERVALO_AMOSTRAGEM_MS
 *       (250 ms) e o que vai para a rede e a MEDIA das amostras do segundo
 *       - ver amostrarTemperatura() / temperaturaMedia() mais abaixo. Nao ha
 *       nada a fazer para isso funcionar: basta a funcao devolver UMA leitura
 *       crua por chamada. O tratamento de isnan acima ja evita que uma falha
 *       de leitura envenene a media.
 *
 *  ---------------------------------------------------------------------
 *  COMO A SIMULACAO ATUAL FUNCIONA:
 *
 *  Aproximacao assintotica (resfriamento/aquecimento de Newton simplificado),
 *  chamada uma vez por AMOSTRA (4x por ciclo de transmissao):
 *
 *      temperatura += (alvo - temperatura) * FATOR_APROXIMACAO;
 *
 *  Isso da exatamente o formato de uma torra real: sobe rapido no comeco
 *  (diferenca grande para o alvo), vai desacelerando conforme se aproxima
 *  e estabiliza no patamar como se estivesse em equilibrio termico.
 *  O alvo depende da potencia, entao mexer no slider do frontend muda a
 *  curva de verdade: potencia 100 -> ~215 C; potencia 0 -> volta ao ambiente.
 * ========================================================================== */
float lerTemperatura()
{
    const float TEMP_AMBIENTE      = 25.0f;   // ponto de partida
    const float TEMP_MAX_TORRA     = 215.0f;  // patamar com potencia 100%

    // Esta funcao passou a ser chamada a cada INTERVALO_AMOSTRAGEM_MS, e nao
    // mais uma vez por segundo - entao o fator precisa ser POR AMOSTRA, senao
    // a curva simulada subiria 4x mais rapido. Derivado do fator por segundo
    // (0.05 -> ~20 s de constante de tempo):
    //     f_amostra = 1 - (1 - f_segundo) ^ (intervalo_amostra / 1 s)
    // Assim o formato da curva fica identico a qualquer taxa de amostragem e
    // continua batendo com o simulador em backend/simulador_tbeam.py.
    static const float FATOR_APROXIMACAO =
        1.0f - powf(1.0f - 0.05f, INTERVALO_AMOSTRAGEM_MS / 1000.0f);

    static float temperaturaAtual = TEMP_AMBIENTE;

    // Alvo proporcional a potencia comandada.
    float alvo = TEMP_AMBIENTE +
                 (TEMP_MAX_TORRA - TEMP_AMBIENTE) * (potenciaAtual / 100.0f);

    temperaturaAtual += (alvo - temperaturaAtual) * FATOR_APROXIMACAO;

    // Ruido de MEDICAO: +-0.15 C aplicado apenas ao valor RETORNADO, nunca
    // acumulado em temperaturaAtual. A distincao importa agora que existe
    // superamostragem: somado ao estado o ruido viraria um passeio aleatorio
    // (deriva permanente, que media nenhuma remove); fora do estado ele e
    // exatamente o tipo de ruido que a media das amostras atenua.
    return temperaturaAtual + (random(-15, 16) / 100.0f);
}
/* ======================  FIM DO PONTO DE TROCA  ========================== */

/* ------------------------------------------------------------------------ */
/*  COMANDOS                                                                 */
/* ------------------------------------------------------------------------ */

// Aplica um comando JSON ja parseado. Usada tanto pelo WebSocket quanto
// pela rota HTTP POST /comando - as duas vias caem aqui.
void aplicarComando(const JsonDocument &comando)
{
    if (comando["potencia"].is<int>()) {
        int p = comando["potencia"].as<int>();
        potenciaAtual = constrain(p, 0, 100);
        Serial.printf("[CMD] potencia -> %d%%\n", potenciaAtual);
    }

    if (comando["motor"].is<const char *>()) {
        const char *m = comando["motor"];
        motorLigado = (strcmp(m, "on") == 0);
        Serial.printf("[CMD] motor -> %s\n", motorLigado ? "ON" : "OFF");
    }
}

// Faz o parse de um payload cru e aplica. Retorna false se o JSON for invalido.
bool tratarPayloadComando(const uint8_t *dados, size_t tamanho)
{
    JsonDocument comando;
    DeserializationError erro = deserializeJson(comando, dados, tamanho);
    if (erro) {
        Serial.printf("[CMD] JSON invalido: %s\n", erro.c_str());
        return false;
    }
    aplicarComando(comando);
    return true;
}

/* ------------------------------------------------------------------------ */
/*  SUPERAMOSTRAGEM                                                          */
/* ------------------------------------------------------------------------ */
// Acumulador das amostras colhidas entre dois envios.
double   somaAmostras = 0.0;
uint16_t qtdAmostras  = 0;

// Chamada pelo loop() a cada INTERVALO_AMOSTRAGEM_MS. So acumula, nao envia.
void amostrarTemperatura()
{
    somaAmostras += lerTemperatura();
    qtdAmostras++;
}

// Media das amostras desde o ultimo envio; zera o acumulador para o proximo.
float temperaturaMedia()
{
    if (qtdAmostras == 0) {       // nenhuma amostra no intervalo: le direto
        return lerTemperatura();
    }
    float media  = (float) (somaAmostras / qtdAmostras);
    somaAmostras = 0.0;
    qtdAmostras  = 0;
    return media;
}

/* ------------------------------------------------------------------------ */
/*  TRANSMISSAO                                                              */
/* ------------------------------------------------------------------------ */
void transmitirLeitura()
{
    JsonDocument leitura;
    leitura["temp"]           = temperaturaMedia();  // media das amostras
    leitura["timestamp"]      = millis();
    leitura["potencia_atual"] = potenciaAtual;
    leitura["motor_ligado"]   = motorLigado;

    char buffer[192];
    size_t n = serializeJson(leitura, buffer, sizeof(buffer));
    ws.textAll(buffer, n);
}

/* ------------------------------------------------------------------------ */
/*  WEBSOCKET                                                                */
/* ------------------------------------------------------------------------ */
void aoEventoWebSocket(AsyncWebSocket *servidorWs, AsyncWebSocketClient *cliente,
                       AwsEventType tipo, void *arg, uint8_t *dados, size_t tamanho)
{
    switch (tipo) {
    case WS_EVT_CONNECT:
        Serial.printf("[WS] cliente #%u conectado (%s)\n",
                      cliente->id(), cliente->remoteIP().toString().c_str());
        break;

    case WS_EVT_DISCONNECT:
        Serial.printf("[WS] cliente #%u desconectado\n", cliente->id());
        break;

    case WS_EVT_DATA: {
        AwsFrameInfo *info = (AwsFrameInfo *)arg;
        // So tratamos frames de texto completos - comandos sao pequenos.
        if (info->final && info->index == 0 && info->len == tamanho &&
            info->opcode == WS_TEXT) {
            tratarPayloadComando(dados, tamanho);
        }
        break;
    }

    default:
        break;
    }
}

/* ------------------------------------------------------------------------ */
/*  WI-FI                                                                    */
/* ------------------------------------------------------------------------ */
// Imprime como chegar na placa. Sai no primeiro sucesso E a cada reconexao,
// porque o IP pode mudar quando o DHCP renova -- e e esse IP que o backend
// precisa ter em TBEAM_HOST. Anunciar so no boot esconderia a troca.
void anunciarRede()
{
    Serial.println("[WIFI] conectado!");
    Serial.print  ("[WIFI] IP do T-Beam: ");
    Serial.println(WiFi.localIP());
    Serial.printf ("[WIFI] WebSocket:    ws://%s%s\n",
                   WiFi.localIP().toString().c_str(), CAMINHO_WEBSOCKET);
    Serial.printf ("[WIFI] HTTP comando: http://%s/comando\n",
                   WiFi.localIP().toString().c_str());
    Serial.println("       ^^^ anote este IP: e o TBEAM_HOST do backend ^^^");
}

// Tenta conectar por no maximo LIMITE_CONEXAO_MS. Devolve se conseguiu.
// NAO trava esperando: quem chama segue a vida de qualquer jeito.
bool conectarWiFi()
{
    Serial.printf("[WIFI] conectando em \"%s\"", WIFI_SSID);
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);                 // evita latencia no WebSocket
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    uint32_t comeco = millis();
    while (WiFi.status() != WL_CONNECTED &&
           millis() - comeco < LIMITE_CONEXAO_MS) {
        delay(500);
        Serial.print(".");
    }
    Serial.println();

    if (WiFi.status() != WL_CONNECTED) {
        Serial.printf("[WIFI] nao conectou em %lu s. SEGUINDO SEM REDE.\n",
                      LIMITE_CONEXAO_MS / 1000);
        Serial.println("       A placa continua amostrando temperatura e vai");
        Serial.println("       tentar reconectar sozinha. Confira o SSID e a");
        Serial.println("       senha em include/credenciais.h, e lembre que o");
        Serial.println("       ESP32 so fala 2,4 GHz.");
        return false;
    }

    anunciarRede();
    return true;
}

/* ------------------------------------------------------------------------ */
/*  SETUP / LOOP                                                             */
/* ------------------------------------------------------------------------ */
void setup()
{
    Serial.begin(115200);
    delay(300);
    Serial.println("\n\n=== Torrador de Cafe - T-Beam (temperatura SIMULADA) ===");

    randomSeed(esp_random());
    // O resultado nao decide se o setup() continua: o servidor e o WebSocket
    // sao montados de qualquer jeito, prontos para quando a rede aparecer.
    estavaConectado = conectarWiFi();

    ws.onEvent(aoEventoWebSocket);
    servidor.addHandler(&ws);

    // Rota de sanidade: abra http://<ip>/ no navegador para ver se esta vivo.
    servidor.on("/", HTTP_GET, [](AsyncWebServerRequest *req) {
        req->send(200, "text/plain",
                  "T-Beam torrador ok. WebSocket em /ws, comandos em POST /comando");
    });

    // VIA DE CONVENIENCIA: POST /comando com o mesmo JSON aceito no WebSocket.
    //   curl -X POST http://<ip>/comando -H "Content-Type: application/json" \
    //        -d '{"potencia":50,"motor":"on"}'
    servidor.on(
        "/comando", HTTP_POST,
        [](AsyncWebServerRequest *req) { /* resposta sai no handler de corpo */ },
        NULL,
        [](AsyncWebServerRequest *req, uint8_t *dados, size_t tamanho,
           size_t indice, size_t total) {
            if (tratarPayloadComando(dados, tamanho)) {
                req->send(200, "application/json", "{\"ok\":true}");
            } else {
                req->send(400, "application/json",
                          "{\"ok\":false,\"erro\":\"json invalido\"}");
            }
        });

    servidor.begin();
    Serial.println("[HTTP] servidor iniciado");
}

void loop()
{
    ws.cleanupClients();

    uint32_t agora = millis();
    bool conectado = (WiFi.status() == WL_CONNECTED);

    // A RECONEXAO NAO INTERROMPE MAIS A AMOSTRAGEM. Antes, o loop dava return
    // enquanto a rede estivesse fora, e com isso a placa PARAVA DE MEDIR. Mas
    // a torra nao espera o roteador: o cafe continua esquentando. Medir e
    // local; o que pode faltar e so o envio.
    if (!conectado) {
        if (agora - ultimaTentativaWiFi >= INTERVALO_RECONEXAO_MS) {
            ultimaTentativaWiFi = agora;
            Serial.println("[WIFI] sem rede - tentando reconectar...");
            WiFi.reconnect();
        }
    } else if (!estavaConectado) {
        // Voltou. O IP pode ter mudado, entao anuncia de novo.
        anunciarRede();
    }
    estavaConectado = conectado;

    // Amostragem rapida: colhe e acumula, sem mandar nada para a rede.
    if (agora - ultimaAmostra >= INTERVALO_AMOSTRAGEM_MS) {
        ultimaAmostra = agora;
        amostrarTemperatura();
    }

    // Envio: manda a MEDIA das amostras colhidas desde o envio anterior.
    if (agora - ultimoEnvio >= INTERVALO_ENVIO_MS) {
        ultimoEnvio = agora;

        if (conectado) {
            transmitirLeitura();
        } else {
            // O TICK DE ENVIO ACONTECE MESMO OFFLINE, DE PROPOSITO.
            // temperaturaMedia() zera o acumulador; sem chama-la, as amostras
            // de todo o periodo sem rede se somariam numa unica media. Quando
            // a rede voltasse, o primeiro valor enviado seria a media de
            // minutos inteiros -- com cara de leitura instantanea, que e o
            // tipo de numero errado mais dificil de perceber. De quebra,
            // qtdAmostras e uint16_t e estouraria depois de ~4,5 h offline.
            float descartada = temperaturaMedia();
            if (agora - ultimoAvisoOffline >= INTERVALO_AVISO_OFFLINE_MS) {
                ultimoAvisoOffline = agora;
                Serial.printf("[WIFI] offline: amostrando e descartando "
                              "(ultima media %.2f C)\n", descartada);
            }
        }
    }

    delay(10);
}
