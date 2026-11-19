/* ==========================================================================
 *  TORRADOR DE CAFE - Firmware do modulo sensor
 *  Placa: LilyGO T-Beam (ESP32)  |  PlatformIO board = ttgo-t-beam
 *  UFCI-MG Varginha - Projeto de extensao
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
 *        "timestamp":  154320,   // millis() do ESP32
 *        "picos":      0         // picos descartados desde o boot
 *                                // (include/filtro_picos.h)
 *      }
 *
 *  SO MEDE, NAO ACIONA NADA. O torrador e uma pipoqueira eletrica de ar
 *  quente, ligada direto na tomada; a placa nao controla a resistencia nem
 *  a ventoinha, e por isso nao recebe comando nenhum.
 * ========================================================================== */
#include <Arduino.h>
#include <WiFi.h>
#include <DNSServer.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include <AsyncTCP.h>
#include <ESPAsyncWebServer.h>
#include <ArduinoJson.h>
#include <Update.h>
#include <SPI.h>
#include <Adafruit_MAX31855.h>
#include "filtro_picos.h"

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
/*  CREDENCIAIS DA REDE (NVS / Portal Cativo + credenciais.h opcional)       */
/* ------------------------------------------------------------------------ */
/*  A placa aceita credenciais de duas fontes:                               */
/*    1. Salvas pelo navegador no portal Wi-Fi (http://192.168.4.1/wifi ou   */
/*       http://<ip>/wifi), gravadas na memoria flash (Preferences / NVS).   */
/*    2. Opcionalmente, de include/credenciais.h (fora do git), como         */
/*       fallback caso exista na compilacao.                                 */
/*                                                                           */
/*  Quando nao encontra nenhuma rede conhecida, a placa abre o Access Point  */
/*  "esp32" (senha "torrador", IP 192.168.4.1) com portal cativo, sem        */
/*  parar de amostrar o termopar.                                            */
/* ------------------------------------------------------------------------ */
#if __has_include("credenciais.h")
  #include "credenciais.h"
#endif

#ifndef WIFI_SSID
  #define WIFI_SSID     "torrador"
  #define WIFI_PASSWORD "torrador"
#endif

#ifndef OTA_SENHA
  #define OTA_SENHA "torrador"
#endif

#if defined(WIFI_SSID_2) && !defined(WIFI_IP_2)
  #define WIFI_IP_2 nullptr
#endif

struct Rede { const char *ssid; const char *senha; const char *ipFixo; };

Preferences preferencias;
String nvsSsid;
String nvsSenha;
String nvsIpFixo;

Rede   redes[3];
size_t qtdRedes  = 0;
size_t redeAtual = 0;   // indice em redes[] da ultima tentativa

const char    *AP_SSID           = "esp32";
const char    *AP_SENHA          = "torrador";
const uint16_t PORTA_DNS         = 53;
DNSServer      dnsServer;
bool           apAtivo           = false;
bool           conectarAposSalvar = false;
uint32_t       momentoSalvarWifi = 0;
uint32_t       momentoConectouSta = 0;

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

// GRAVACAO PELA REDE (OTA por HTTP). E HTTP, e nao o espota do ArduinoOTA,
// por causa do sentido da conexao: no espota a PLACA conecta de volta no PC,
// e o PC aqui e o WSL atras de NAT (172.20.x), que a placa nao alcanca. No
// HTTP quem conecta e so o PC, o mesmo sentido do backend. Quem grava e o
// scripts/gravar_ota.py, via "pio run -e ota -t upload --upload-port <ip>".
const char *OTA_USUARIO = "ota";
bool     reiniciarAposOta = false;   // a gravacao terminou bem: reinicia no loop
bool     otaGravou        = false;   // Update.end() deu certo nesta requisicao
uint32_t momentoOta       = 0;

// Quanto o setup() espera por CADA rede ANTES DE SEGUIR SEM ELA. Existe porque
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

// Maior salto aceito entre duas amostras seguidas (250 ms). Os picos vistos
// foram de ~+256 C; o termopar mergulhado em agua fervendo sobe ~15 C nos
// primeiros 250 ms, e uma torra de ar quente nao chega perto disso. 25 C
// separa os dois com folga. Ver include/filtro_picos.h.
const float   LIMITE_SALTO_C     = 25.0f;
const uint8_t CONFIRMACOES_SALTO = 3;   // amostras seguidas para aceitar nivel novo
FiltroPicos   filtroPicos(LIMITE_SALTO_C, CONFIRMACOES_SALTO);

// Chamada pelo loop() a cada INTERVALO_AMOSTRAGEM_MS. So acumula, nao envia.
// Amostra que falhou fica FORA da media: uma so leitura invalida envenenaria
// o segundo inteiro. Pico isolado tambem, pelo mesmo motivo.
void amostrarTemperatura()
{
    float c = lerTemperatura();
    if (isnan(c)) {
        return;
    }
    uint32_t niveisAntes = filtroPicos.niveisNovos();
    if (!filtroPicos.aceitar(c)) {
        Serial.printf("[SENSOR] pico descartado: %.2f C | picos ate agora: %lu\n",
                      c, filtroPicos.picos());
        return;
    }
    if (filtroPicos.niveisNovos() != niveisAntes) {
        Serial.printf("[SENSOR] nivel novo confirmado em %.2f C (nao era pico)\n", c);
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
    // Total de picos descartados desde o boot. Vai pela rede porque a placa
    // roda longe do USB, e sem isso nao ha como saber se o filtro esta
    // trabalhando -- ou se o cabo esta gerando ruido o tempo todo.
    leitura["picos"] = filtroPicos.picos();

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
/*  WI-FI (NVS + Portal Cativo "esp32")                                     */
/* ------------------------------------------------------------------------ */
void carregarRedes()
{
    preferencias.begin("wifi", true);
    nvsSsid   = preferencias.getString("ssid", "");
    nvsSenha  = preferencias.getString("senha", "");
    nvsIpFixo = preferencias.getString("ip_fixo", "");
    preferencias.end();

    qtdRedes = 0;
    if (nvsSsid.length() > 0) {
        redes[qtdRedes++] = {
            nvsSsid.c_str(),
            nvsSenha.c_str(),
            nvsIpFixo.length() > 0 ? nvsIpFixo.c_str() : nullptr
        };
    }
#ifdef WIFI_SSID
    if (nvsSsid != WIFI_SSID) {
        redes[qtdRedes++] = { WIFI_SSID, WIFI_PASSWORD, nullptr };
    }
#endif
#ifdef WIFI_SSID_2
    if (nvsSsid != WIFI_SSID_2) {
        redes[qtdRedes++] = { WIFI_SSID_2, WIFI_PASSWORD_2, WIFI_IP_2 };
    }
#endif
    if (redeAtual >= qtdRedes) {
        redeAtual = 0;
    }
}

void salvarRedeNvs(const String &ssid, const String &senha, const String &ipFixo)
{
    preferencias.begin("wifi", false);
    if (ssid.length() == 0) {
        preferencias.clear();
    } else {
        preferencias.putString("ssid", ssid);
        preferencias.putString("senha", senha);
        preferencias.putString("ip_fixo", ipFixo);
    }
    preferencias.end();
}

void ligarPortalAp()
{
    if (apAtivo) {
        return;
    }
    WiFi.mode(WIFI_AP_STA);
    WiFi.softAP(AP_SSID, AP_SENHA);
    dnsServer.setErrorReplyCode(DNSReplyCode::NoError);
    dnsServer.start(PORTA_DNS, "*", WiFi.softAPIP());
    apAtivo = true;
    Serial.printf("[WIFI] Portal AP ativo: conecte no Wi-Fi \"%s\" (senha \"%s\") e abra http://%s/wifi\n",
                  AP_SSID, AP_SENHA, WiFi.softAPIP().toString().c_str());
}

void desligarPortalAp()
{
    if (!apAtivo || WiFi.softAPgetStationNum() > 0) {
        return;
    }
    dnsServer.stop();
    WiFi.softAPdisconnect(true);
    WiFi.mode(WIFI_STA);
    apAtivo = false;
    Serial.println("[WIFI] Portal AP desligado (conectado na rede principal).");
}

// Imprime como chegar na placa. Sai no primeiro sucesso E a cada reconexao,
// porque o IP pode mudar quando o DHCP renova -- e e esse IP que o backend
// precisa ter em TBEAM_HOST. Anunciar so no boot esconderia a troca.
void anunciarRede()
{
    Serial.printf ("[WIFI] conectado em \"%s\"!\n", WiFi.SSID().c_str());
    Serial.print  ("[WIFI] IP do T-Beam: ");
    Serial.println(WiFi.localIP());
    Serial.printf ("[WIFI] WebSocket:    ws://%s%s\n",
                   WiFi.localIP().toString().c_str(), CAMINHO_WEBSOCKET);
    Serial.printf ("[WIFI] Config Wi-Fi: http://%s/wifi\n",
                   WiFi.localIP().toString().c_str());
    Serial.printf ("[WIFI] pelo nome:    http://%s.local/\n", NOME_NA_REDE);
    Serial.println("       ^^^ o IP (ou o nome, onde .local resolver) e o TBEAM_HOST ^^^");
}

// Comeca a conectar na rede redes[i], com IP fixo ou DHCP conforme ela pedir.
// A config vale ate a proxima chamada: sem voltar ao DHCP aqui, o IP fixo de
// uma rede vazaria para a outra na alternancia do loop.
void iniciarRede(size_t i)
{
    if (i >= qtdRedes) {
        return;
    }
    if (WiFi.status() == WL_CONNECTED) {
        WiFi.disconnect(false);
    }
    IPAddress ip;
    if (redes[i].ipFixo != nullptr && ip.fromString(redes[i].ipFixo)) {
        IPAddress gateway(ip[0], ip[1], ip[2], 1);
        WiFi.config(ip, gateway, IPAddress(255, 255, 255, 0), gateway);
    } else {
        WiFi.config(INADDR_NONE, INADDR_NONE, INADDR_NONE);   // volta ao DHCP
    }
    WiFi.begin(redes[i].ssid, redes[i].senha);
}

// Tenta cada rede conhecida por no maximo LIMITE_CONEXAO_MS. Devolve se
// conseguiu. NAO trava esperando: se falhar, abre o portal "Torrador-Config"
// e segue a vida amostrando o sensor.
bool conectarWiFi()
{
    carregarRedes();
    WiFi.mode(WIFI_STA);
    WiFi.setHostname(NOME_NA_REDE);       // antes do begin(), ou o DHCP nao leva
    WiFi.setSleep(false);                 // evita latencia no WebSocket

    for (redeAtual = 0; redeAtual < qtdRedes; redeAtual++) {
        Serial.printf("[WIFI] conectando em \"%s\"", redes[redeAtual].ssid);
        iniciarRede(redeAtual);

        uint32_t comeco = millis();
        while (WiFi.status() != WL_CONNECTED &&
               millis() - comeco < LIMITE_CONEXAO_MS) {
            delay(500);
            Serial.print(".");
        }
        Serial.println();

        if (WiFi.status() == WL_CONNECTED) {
            momentoConectouSta = millis();
            anunciarRede();
            return true;
        }
        Serial.printf("[WIFI] \"%s\" nao respondeu em %lu s.\n",
                      redes[redeAtual].ssid, LIMITE_CONEXAO_MS / 1000);
    }
    redeAtual = 0;

    Serial.println("[WIFI] nenhuma rede conectou. Abrindo portal de configuracao.");
    ligarPortalAp();
    return false;
}

String escaparHtml(const String &s)
{
    String out = s;
    out.replace("&", "&amp;");
    out.replace("<", "&lt;");
    out.replace(">", "&gt;");
    out.replace("\"", "&quot;");
    return out;
}

void responderPaginaWifi(AsyncWebServerRequest *req, const String &aviso = "")
{
    bool conectado = (WiFi.status() == WL_CONNECTED);
    String html = "<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'>"
                  "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                  "<title>Torrador - Configurar Wi-Fi</title>"
                  "<style>"
                  "body{font-family:system-ui,sans-serif;background:#fcf9f4;color:#2e2621;"
                  "max-width:460px;margin:0 auto;padding:20px;line-height:1.5}"
                  "h1{font-size:1.35rem;margin:0 0 8px}"
                  ".box{border:1px solid #d8cec0;border-radius:8px;padding:14px;margin:14px 0;background:#fff}"
                  "label{display:block;font-size:.85rem;color:#6e6259;margin-top:10px}"
                  "input{width:100%;box-sizing:border-box;padding:9px;margin-top:4px;"
                  "border:1px solid #d8cec0;border-radius:6px;font-size:1rem}"
                  "button{width:100%;padding:11px;margin-top:14px;border:0;border-radius:6px;"
                  "background:#a9502f;color:#fff;font-size:1rem;font-weight:600;cursor:pointer}"
                  "button.sec{background:#f4efe7;color:#2e2621;border:1px solid #d8cec0;font-weight:500}"
                  "ul{list-style:none;padding:0;margin:8px 0 0}"
                  "li{padding:8px;border-bottom:1px solid #e7e0d5;cursor:pointer;display:flex;justify-content:space-between}"
                  "li:hover{background:#f4efe7}"
                  ".ok{color:#4f5b3c;font-weight:600}.aviso{background:#f4efe7;border-left:3px solid #a9502f;padding:10px;margin:10px 0}"
                  "code{background:#f4efe7;padding:2px 5px;border-radius:4px}"
                  "</style></head><body>"
                  "<h1>Torrador T-Beam &middot; Wi-Fi</h1>";

    if (aviso.length() > 0) {
        html += "<div class='aviso'>" + aviso + "</div>";
    }

    html += "<div class='box'>";
    if (conectado) {
        html += "<div class='ok'>Conectado em \"" + escaparHtml(WiFi.SSID()) + "\"</div>";
        html += "<div>IP na rede (<code>TBEAM_HOST</code>): <code>" + WiFi.localIP().toString() + "</code></div>";
        html += "<div>WebSocket: <code>ws://" + WiFi.localIP().toString() + "/ws</code></div>";
    } else {
        html += "<div><b>Sem rede Wi-Fi conectada</b></div>";
        html += "<div>Modo AP direto (<code>TBEAM_HOST</code>): <code>" + WiFi.softAPIP().toString() + "</code></div>";
    }
    if (nvsSsid.length() > 0) {
        html += "<div style='font-size:.85rem;color:#6e6259;margin-top:6px'>Salvo na memoria: <b>" +
                escaparHtml(nvsSsid) + "</b></div>";
    }
    html += "</div>";

    html += "<div class='box'>"
            "<button type='button' class='sec' onclick='buscar()'>Buscar redes 2,4 GHz proximas</button>"
            "<ul id='lista'></ul>"
            "<form method='POST' action='/wifi'>"
            "<label>Nome da rede (SSID 2,4 GHz)"
            "<input id='ssid' name='ssid' required value='" + escaparHtml(nvsSsid) + "' placeholder='Ex.: Hotspot-Celular'></label>"
            "<label>Senha"
            "<input id='senha' name='senha' type='password' value='" + escaparHtml(nvsSenha) + "' placeholder='Senha do Wi-Fi'></label>"
            "<label>IP fixo opcional (deixe vazio para DHCP automatico)"
            "<input name='ip_fixo' value='" + escaparHtml(nvsIpFixo) + "' placeholder='Ex.: 192.168.137.50'></label>"
            "<button type='submit'>Salvar e conectar</button>"
            "</form></div>"
            "<script>"
            "let tentativasScan=0;"
            "async function buscar(){"
            "const ul=document.getElementById('lista');"
            "if(tentativasScan===0)ul.innerHTML='<li>Procurando redes 2,4 GHz...</li>';"
            "try{const r=await fetch('/wifi/scan');if(!r.ok)throw new Error('HTTP '+r.status);"
            "const d=await r.json();"
            "if(d.escaneando){if(++tentativasScan>8){tentativasScan=0;ul.innerHTML='<li>Tempo esgotado. Digite o nome da rede abaixo.</li>';return;}setTimeout(buscar,1500);return;}"
            "tentativasScan=0;ul.innerHTML='';"
            "if(d.erro){ul.innerHTML='<li>'+d.erro+'</li>';return;}"
            "d.redes.forEach(n=>{const li=document.createElement('li');"
            "li.textContent=n.ssid+' ('+n.rssi+' dBm)';"
            "li.onclick=()=>{document.getElementById('ssid').value=n.ssid;document.getElementById('senha').focus();};"
            "ul.appendChild(li);});"
            "if(!d.redes.length)ul.innerHTML='<li>Nenhuma rede 2,4 GHz encontrada</li>';"
            "}catch(e){tentativasScan=0;ul.innerHTML='<li>Erro ao buscar redes ('+e.message+')</li>';}}"
            "</script></body></html>";

    req->send(200, "text/html; charset=utf-8", html);
}

/* ------------------------------------------------------------------------ */
/*  GRAVACAO PELA REDE (OTA)                                                 */
/* ------------------------------------------------------------------------ */
// Corpo do POST /update, chamado pedaco a pedaco enquanto o .bin chega. Grava
// na particao OTA INATIVA: se a transferencia cair no meio, a placa continua
// bootando o firmware atual, que nao foi tocado.
void aoReceberFirmware(AsyncWebServerRequest *req, const String &nome,
                       size_t indice, uint8_t *dados, size_t tamanho, bool final)
{
    // Sem senha, o corpo e descartado; a resposta 401 sai em aoFimDoUpload().
    if (!req->authenticate(OTA_USUARIO, OTA_SENHA)) {
        return;
    }
    if (indice == 0) {
        otaGravou = false;
        Serial.printf("[OTA] recebendo %s\n", nome.c_str());
        // Uma gravacao anterior que caiu no meio deixa o Update aberto, e o
        // begin() recusa enquanto ele estiver: sem isto, toda OTA seguinte
        // falharia ate alguem reiniciar a placa na mao.
        if (Update.isRunning()) {
            Serial.println("[OTA] descartando gravacao anterior interrompida");
            Update.abort();
        }
        if (!Update.begin(UPDATE_SIZE_UNKNOWN)) {
            Update.printError(Serial);
        }
    }
    if (Update.isRunning() && !Update.hasError()) {
        if (Update.write(dados, tamanho) != tamanho) {
            Update.printError(Serial);
        }
    }
    if (final && Update.isRunning()) {
        otaGravou = Update.end(true);
        if (otaGravou) {
            Serial.printf("[OTA] %u bytes gravados\n", indice + tamanho);
        } else {
            Update.printError(Serial);
        }
    }
}

void aoFimDoUpload(AsyncWebServerRequest *req)
{
    if (!req->authenticate(OTA_USUARIO, OTA_SENHA)) {
        return req->requestAuthentication(AsyncAuthType::AUTH_BASIC);
    }
    AsyncWebServerResponse *resp = otaGravou
        ? req->beginResponse(200, "text/plain", "OK - gravado, reiniciando\n")
        : req->beginResponse(500, "text/plain", "FALHOU - firmware atual mantido, veja a serial\n");
    resp->addHeader("Connection", "close");
    req->send(resp);

    // Nao reinicia aqui: a resposta ainda nao saiu. O loop reinicia depois.
    if (otaGravou) {
        reiniciarAposOta = true;
        momentoOta = millis();
    }
    otaGravou = false;
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

    // Rota de sanidade: em modo AP sem rede principal, abre direto o portal /wifi.
    servidor.on("/", HTTP_GET, [](AsyncWebServerRequest *req) {
        if (apAtivo && WiFi.status() != WL_CONNECTED) {
            responderPaginaWifi(req);
            return;
        }
        req->send(200, "text/plain",
                  "T-Beam torrador ok. Leituras no WebSocket em /ws | Configurar Wi-Fi em /wifi");
    });

    // IMPORTANTE: no ESPAsyncWebServer, "/wifi/scan" precisa vir ANTES de "/wifi",
    // pois o matcher de rota compara por prefixo se "/wifi" vier primeiro.
    servidor.on("/wifi/scan", HTTP_GET, [](AsyncWebServerRequest *req) {
        int16_t n = WiFi.scanComplete();
        if (n == WIFI_SCAN_FAILED) {
            // Se o STA estiver preso tentando conectar, libera o radio antes de escanear.
            if (WiFi.status() != WL_CONNECTED) {
                WiFi.disconnect(false);
            }
            int16_t ret = WiFi.scanNetworks(true);   // assincrono: nao trava o loop de 250 ms
            if (ret == WIFI_SCAN_FAILED) {
                req->send(200, "application/json",
                          "{\"escaneando\":false,\"erro\":\"Falha ao iniciar busca. Digite o SSID manualmente.\",\"redes\":[]}");
                return;
            }
            req->send(200, "application/json", "{\"escaneando\":true,\"redes\":[]}");
            return;
        }
        if (n == WIFI_SCAN_RUNNING) {
            req->send(200, "application/json", "{\"escaneando\":true,\"redes\":[]}");
            return;
        }
        JsonDocument doc;
        doc["escaneando"] = false;
        JsonArray arr = doc["redes"].to<JsonArray>();
        for (int16_t i = 0; i < n; i++) {
            JsonObject obj = arr.add<JsonObject>();
            obj["ssid"]   = WiFi.SSID(i);
            obj["rssi"]   = WiFi.RSSI(i);
            obj["aberta"] = (WiFi.encryptionType(i) == WIFI_AUTH_OPEN);
        }
        WiFi.scanDelete();
        String json;
        serializeJson(doc, json);
        req->send(200, "application/json", json);
    });

    servidor.on("/wifi", HTTP_GET, [](AsyncWebServerRequest *req) {
        responderPaginaWifi(req);
    });

    servidor.on("/wifi", HTTP_POST, [](AsyncWebServerRequest *req) {
        String ssid   = req->hasParam("ssid", true)    ? req->getParam("ssid", true)->value()    : "";
        String senha  = req->hasParam("senha", true)   ? req->getParam("senha", true)->value()   : "";
        String ipFixo = req->hasParam("ip_fixo", true) ? req->getParam("ip_fixo", true)->value() : "";
        ssid.trim();
        ipFixo.trim();

        salvarRedeNvs(ssid, senha, ipFixo);
        redeAtual = 0;
        conectarAposSalvar = true;
        momentoSalvarWifi  = millis();

        Serial.printf("[WIFI] nova rede salva na NVS: \"%s\" (IP fixo: \"%s\")\n",
                      ssid.c_str(), ipFixo.c_str());
        responderPaginaWifi(req,
            "Rede <b>" + escaparHtml(ssid) + "</b> salva! Conectando agora... "
            "Confira o IP atribuido na lista do seu hotspot/roteador (aparelho <code>torrador</code>).");
    });

    servidor.on("/update", HTTP_POST, aoFimDoUpload, aoReceberFirmware);

    // Portal cativo: quando alguem conecta no AP "esp32", o celular
    // ou notebook abre uma URL de teste de conectividade; servimos /wifi direto.
    servidor.onNotFound([](AsyncWebServerRequest *req) {
        if (apAtivo) {
            responderPaginaWifi(req);
        } else {
            req->send(404, "text/plain", "Nao encontrado. Use /ws ou /wifi");
        }
    });

    servidor.begin();
    Serial.println("[HTTP] servidor iniciado");
}

void loop()
{
    ws.cleanupClients();
    if (apAtivo) {
        dnsServer.processNextRequest();
    }

    uint32_t agora = millis();

    // Meio segundo para a resposta do /update chegar ao PC antes do reboot.
    if (reiniciarAposOta && agora - momentoOta >= 500) {
        Serial.println("[OTA] reiniciando no firmware novo");
        delay(100);
        ESP.restart();
    }

    // Dispara a conexao logo apos o usuario salvar no formulario /wifi.
    // carregarRedes() roda aqui na loopTask para evitar concorrencia com AsyncTCP.
    if (conectarAposSalvar && agora - momentoSalvarWifi >= 500) {
        conectarAposSalvar = false;
        carregarRedes();
        redeAtual = 0;
        ultimaTentativaWiFi = agora;
        if (qtdRedes > 0) {
            Serial.printf("[WIFI] conectando na rede recem-salva \"%s\"...\n",
                          redes[0].ssid);
            iniciarRede(0);
        }
    }

    bool conectado = (WiFi.status() == WL_CONNECTED);

    // A RECONEXAO NAO INTERROMPE MAIS A AMOSTRAGEM.
    // Enquanto houver alguem conectado no AP "esp32", nao fica
    // trocando de canal a cada 15 s para nao derrubar quem esta preenchendo a
    // pagina /wifi (ou lendo o WebSocket direto em 192.168.4.1).
    if (!conectado) {
        if (!apAtivo) {
            ligarPortalAp();
        }
        bool clienteNoAp = (WiFi.softAPgetStationNum() > 0);
        if (!clienteNoAp && qtdRedes > 0 &&
            agora - ultimaTentativaWiFi >= INTERVALO_RECONEXAO_MS) {
            ultimaTentativaWiFi = agora;
            redeAtual = (redeAtual + 1) % qtdRedes;
            Serial.printf("[WIFI] sem rede - tentando \"%s\"...\n",
                          redes[redeAtual].ssid);
            iniciarRede(redeAtual);
        }
    } else {
        if (!estavaConectado) {
            momentoConectouSta = agora;
            anunciarRede();
        }
        // Desliga o AP 30 s apos conectar na rede principal (se ninguem estiver nele).
        if (apAtivo && agora - momentoConectouSta >= 30000) {
            desligarPortalAp();
        }
    }
    estavaConectado = conectado;

    // Amostragem rapida: colhe e acumula, sem mandar nada para a rede.
    if (agora - ultimaAmostra >= INTERVALO_AMOSTRAGEM_MS) {
        ultimaAmostra = agora;
        amostrarTemperatura();
    }

    // Envio: manda a MEDIA das amostras colhidas desde o envio anterior.
    // Tambem transmite se houver cliente WebSocket conectado direto pelo AP (192.168.4.1).
    if (agora - ultimoEnvio >= INTERVALO_ENVIO_MS) {
        ultimoEnvio = agora;

        if (conectado || ws.count() > 0) {
            transmitirLeitura();
        } else {
            float descartada = temperaturaMedia();
            if (agora - ultimoAvisoOffline >= INTERVALO_AVISO_OFFLINE_MS) {
                ultimoAvisoOffline = agora;
                Serial.printf("[WIFI] offline (AP \"%s\" em 192.168.4.1): "
                              "amostrando (ultima media %.2f C)\n",
                              AP_SSID, descartada);
            }
        }
    }

    delay(10);
}
