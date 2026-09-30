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
 *        "temp":       23.7,     // float, graus C; null se o sensor falhou
 *        "timestamp":  154320    // millis() do ESP32
 *      }
 *
 *  SO MEDE, NAO ACIONA NADA. O torrador e uma pipoqueira eletrica de ar
 *  quente, ligada direto na tomada; a placa nao controla a resistencia nem
 *  a ventoinha, e por isso nao recebe comando nenhum.
 * ========================================================================== */

#include <Arduino.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <AsyncTCP.h>
#include <ESPAsyncWebServer.h>
#include <ArduinoJson.h>
#include <SPI.h>
#include <Adafruit_MAX31855.h>

/* ------------------------------------------------------------------------ */
/*  TERMOPAR (MAX31855 + tipo K)                                             */
/* ------------------------------------------------------------------------ */
/*  OS MESMOS PINOS DE firmware/teste_termopar, onde a fiacao foi provada.   */
/*  Mudar aqui sem mudar la joga fora a unica coisa que aquele teste provou. */
/*                                                                           */
/*  NAO USE O GPIO 12 PARA O DO: e pino de strapping (lido no boot, pode     */
/*  impedir a placa de ligar) e no T-Beam ainda e usado pelo GPS. Evite      */
/*  tambem 0, 2 e 15, e os pinos de LoRa/GPS/AXP192.                         */
/* ------------------------------------------------------------------------ */
#define PINO_MAX_CLK 13
#define PINO_MAX_CS  14
#define PINO_MAX_DO  25

// SPI por software: (clock, chip select, data out)
Adafruit_MAX31855 termopar(PINO_MAX_CLK, PINO_MAX_CS, PINO_MAX_DO);

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

// Nome da placa na rede. Vira "torrador.local" por mDNS, e e tambem o nome
// que aparece na lista de aparelhos conectados do roteador/celular. Existe
// para a placa poder rodar LONGE do USB: sem a serial ninguem ve o IP que o
// DHCP deu, e o nome continua o mesmo quando o IP muda.
const char    *NOME_NA_REDE      = "torrador";
const char    *CAMINHO_WEBSOCKET = "/ws";  // ws://<ip-do-tbeam>/ws
const uint32_t INTERVALO_ENVIO_MS = 1000;  // periodo de transmissao (1 s)

// Superamostragem: o sensor e lido varias vezes entre dois envios e o que vai
// para a rede e a MEDIA das amostras. Reduz o ruido de leitura por um fator de
// raiz(N) - 4 amostras derrubam o ruido pela metade - sem aumentar o trafego,
// o tamanho da serie no grafico nem o custo de render do frontend.
// 250 ms respeita com folga o tempo de conversao do MAX31855 (~100 ms).
const uint32_t INTERVALO_AMOSTRAGEM_MS = 250;   // 4 amostras por envio

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

/* ------------------------------------------------------------------------ */
/*  LEITURA DO SENSOR                                                        */
/* ------------------------------------------------------------------------ */
uint32_t totalFalhasSensor = 0;
uint32_t ultimoAvisoSensor = 0;
const uint32_t INTERVALO_AVISO_SENSOR_MS = 5000;

// Uma leitura crua do termopar, em C. Devolve NAN quando a leitura nao vale.
//
// NA FALHA NAO SE REPETE O ULTIMO VALOR BOM. Com o termopar soltando no meio
// da torra, repetir o ultimo valor manteria a tela mostrando, digamos, 185 C
// parados -- com cara de medida, enquanto o cafe continua esquentando. NAN
// vira "temp": null na rede, e o backend ja descarta isso com aviso.
float lerTemperatura()
{
    double celsius = termopar.readCelsius();
    double interna = termopar.readInternal();

    const char *motivo = nullptr;
    if (isnan(celsius)) {
        uint8_t erro = termopar.readError();
        if      (erro & MAX31855_FAULT_OPEN)      motivo = "termopar aberto (solto / fio partido)";
        else if (erro & MAX31855_FAULT_SHORT_GND) motivo = "termopar em curto com o GND";
        else if (erro & MAX31855_FAULT_SHORT_VCC) motivo = "termopar em curto com o VCC";
        else                                      motivo = "leitura invalida";
    } else if (celsius == 0.0 && interna == 0.0) {
        // 32 bits zerados: e o DO sem sinal, nao 0 grau. A junta fria mede o
        // proprio chip e nunca marca 0,00 numa bancada. Ver teste_termopar.
        motivo = "SEM COMUNICACAO COM O CHIP (32 bits zerados) - confira DO no GPIO 25";
    }

    if (motivo == nullptr) {
        return (float) celsius;
    }

    totalFalhasSensor++;
    uint32_t agora = millis();
    if (agora - ultimoAvisoSensor >= INTERVALO_AVISO_SENSOR_MS) {
        ultimoAvisoSensor = agora;
        Serial.printf("[SENSOR] %s | junta fria %.2f C | falhas ate agora: %lu\n",
                      motivo, interna, totalFalhasSensor);
    }
    return NAN;
}

/* ------------------------------------------------------------------------ */
/*  SUPERAMOSTRAGEM                                                          */
/* ------------------------------------------------------------------------ */
// Acumulador das amostras colhidas entre dois envios.
double   somaAmostras = 0.0;
uint16_t qtdAmostras  = 0;

// Chamada pelo loop() a cada INTERVALO_AMOSTRAGEM_MS. So acumula, nao envia.
// Amostra que falhou fica FORA da media: uma so leitura invalida envenenaria
// o segundo inteiro.
void amostrarTemperatura()
{
    float c = lerTemperatura();
    if (isnan(c)) {
        return;
    }
    somaAmostras += c;
    qtdAmostras++;
}

// Media das amostras validas desde o ultimo envio; zera o acumulador para o
// proximo. NAN se nenhuma amostra do intervalo prestou.
float temperaturaMedia()
{
    if (qtdAmostras == 0) {       // nenhuma amostra valida: tenta ler direto
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
    float media = temperaturaMedia();
    if (isnan(media)) {
        leitura["temp"] = nullptr;  // sensor falhou o segundo inteiro
    } else {
        leitura["temp"] = media;    // media das amostras validas
    }
    leitura["timestamp"] = millis();

    char buffer[96];
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
    Serial.printf ("[WIFI] pelo nome:    http://%s.local/\n", NOME_NA_REDE);
    Serial.println("       ^^^ o IP (ou o nome, onde .local resolver) e o TBEAM_HOST ^^^");
}

// Tenta conectar por no maximo LIMITE_CONEXAO_MS. Devolve se conseguiu.
// NAO trava esperando: quem chama segue a vida de qualquer jeito.
bool conectarWiFi()
{
    Serial.printf("[WIFI] conectando em \"%s\"", WIFI_SSID);
    WiFi.mode(WIFI_STA);
    WiFi.setHostname(NOME_NA_REDE);       // antes do begin(), ou o DHCP nao leva
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
    Serial.println("\n\n=== Torrador de Cafe - T-Beam (termopar MAX31855) ===");

    // begin() so configura os pinos, nao conversa com o chip: "ok" aqui nao
    // prova fiacao. Quem acusa o modulo mudo e lerTemperatura().
    termopar.begin();
    delay(500);   // o chip precisa de um instante apos energizar
    Serial.printf("[SENSOR] MAX31855 em CLK=%d CS=%d DO=%d | primeira leitura: %.2f C\n",
                  PINO_MAX_CLK, PINO_MAX_CS, PINO_MAX_DO, lerTemperatura());
    // O resultado nao decide se o setup() continua: o servidor e o WebSocket
    // sao montados de qualquer jeito, prontos para quando a rede aparecer.
    estavaConectado = conectarWiFi();

    // Sobe mesmo sem rede: o mDNS acompanha a interface sozinho e passa a
    // responder quando o Wi-Fi conectar ou reconectar.
    if (MDNS.begin(NOME_NA_REDE)) {
        MDNS.addService("http", "tcp", PORTA_HTTP);
        Serial.printf("[MDNS] respondendo como %s.local\n", NOME_NA_REDE);
    } else {
        Serial.println("[MDNS] falhou - use o IP");
    }

    ws.onEvent(aoEventoWebSocket);
    servidor.addHandler(&ws);

    // Rota de sanidade: abra http://<ip>/ no navegador para ver se esta vivo.
    servidor.on("/", HTTP_GET, [](AsyncWebServerRequest *req) {
        req->send(200, "text/plain",
                  "T-Beam torrador ok. Leituras no WebSocket em /ws");
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
