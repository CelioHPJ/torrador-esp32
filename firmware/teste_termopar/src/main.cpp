/* ==========================================================================
 *  TESTE ISOLADO DO TERMOPAR  -  MAX31855 + termopar tipo K
 *  LilyGO T-Beam (ESP32)
 *  CEFET-MG Varginha - Projeto de extensao (torrador de cafe)
 * ==========================================================================
 *
 *  PARA QUE SERVE ESTE ARQUIVO
 *
 *  Este sketch NAO e o firmware do torrador. Ele nao conecta no Wi-Fi, nao
 *  sobe servidor nenhum e nao fala com o backend. Ele so le o sensor e
 *  imprime no monitor serial.
 *
 *  A ideia e provar que o MODULO e a FIACAO estao corretos ANTES de mexer no
 *  firmware principal. Se voce integrar tudo de uma vez e o valor vier
 *  errado, fica a duvida: e o sensor, a solda, o pino escolhido ou o codigo?
 *  Testando isolado, sobra uma variavel so.
 *
 *  Depois que este teste passar, va em firmware/src/main.cpp e troque a
 *  funcao lerTemperatura() - o passo a passo esta comentado la.
 *
 *  --------------------------------------------------------------------
 *  LIGACAO (MAX31855 -> T-Beam)
 *
 *      MAX31855        T-Beam
 *      --------        ------
 *      VIN     ->      3V3      <<< 3,3 V. NAO ligue em 5 V.
 *      GND     ->      GND
 *      SCK/CLK ->      GPIO 13  (PINO_MAX_CLK abaixo)
 *      CS      ->      GPIO 14  (PINO_MAX_CS)
 *      SO/DO   ->      GPIO 25  (PINO_MAX_DO)
 *
 *  O termopar tipo K tem POLARIDADE. No padrao americano o fio amarelo e o
 *  positivo e o vermelho e o negativo. Se voce inverter, a leitura CAI
 *  quando voce esquenta a ponta - e o sintoma classico de polaridade
 *  trocada, e nao um defeito do modulo.
 *
 *  --------------------------------------------------------------------
 *  ATENCAO AOS PINOS ESCOLHIDOS
 *
 *  Os GPIOs abaixo sao uma SUGESTAO INICIAL. Confira o pinout da SUA
 *  revisao de T-Beam antes de soldar - elas mudam entre versoes.
 *
 *  No T-Beam ja estao OCUPADOS (mesmo sem usarmos LoRa e GPS nesta fase):
 *      LoRa SX1276 : 5 (SCK), 19 (MISO), 27 (MOSI), 18 (CS), 23 (RST), 26 (DIO0)
 *      AXP192 (I2C): 21 (SDA), 22 (SCL)
 *      GPS         : 12 e 34 (variam conforme a revisao)
 *
 *  E evite os pinos de "strapping", que sao lidos durante o boot e podem
 *  impedir a placa de ligar se estiverem presos em nivel errado:
 *      GPIO 0, 2, 12 e 15.
 *
 *  Como usamos SPI por software (a biblioteca faz o protocolo "na mao"),
 *  qualquer GPIO livre serve - nao precisa ser um pino de SPI dedicado.
 * ========================================================================== */

#include <Arduino.h>
#include <SPI.h>
#include <Adafruit_MAX31855.h>

// ---- pinos (ver aviso acima antes de soldar) ----
#define PINO_MAX_CLK 13
#define PINO_MAX_CS  14
#define PINO_MAX_DO  25

// SPI por software: (clock, chip select, data out)
Adafruit_MAX31855 termopar(PINO_MAX_CLK, PINO_MAX_CS, PINO_MAX_DO);

const uint32_t INTERVALO_LEITURA_MS = 1000;

uint32_t totalLeituras = 0;
uint32_t totalFalhas   = 0;

void setup()
{
    Serial.begin(115200);
    delay(500);

    Serial.println("\n\n===============================================");
    Serial.println(" TESTE ISOLADO DO TERMOPAR - MAX31855");
    Serial.println("===============================================");
    Serial.printf(" Pinos: CLK=%d  CS=%d  DO=%d\n",
                  PINO_MAX_CLK, PINO_MAX_CS, PINO_MAX_DO);

    if (!termopar.begin()) {
        // Cai aqui se o SPI nem inicializou. Quase sempre e fiacao:
        // confira alimentacao (3V3), GND comum e os tres pinos de dados.
        Serial.println(" FALHA ao inicializar o MAX31855. Confira a ligacao.");
        while (true) delay(1000);
    }

    Serial.println(" MAX31855 inicializado.");
    Serial.println();
    Serial.println(" COMO VALIDAR A LEITURA (dois pontos de referencia):");
    Serial.println("   1) Ponta em agua com gelo derretendo  -> deve dar ~0 C");
    Serial.println("   2) Ponta em agua fervendo             -> ~97 C em Varginha");
    Serial.println("      (a ~940 m de altitude a agua ferve abaixo de 100 C;");
    Serial.println("       se der 100 C certinho, desconfie da calibracao)");
    Serial.println();
    Serial.println(" Se a temperatura CAI quando voce esquenta a ponta, os");
    Serial.println(" fios do termopar estao invertidos (amarelo = +).");
    Serial.println("===============================================\n");

    // O chip precisa de um instante para estabilizar apos energizar.
    delay(500);
}

void loop()
{
    totalLeituras++;

    // Temperatura da junta fria (o proprio chip). Serve de sanidade: deve
    // ficar perto da temperatura da sala. Se vier absurda, o modulo esta
    // com problema de alimentacao ou de comunicacao.
    double interna = termopar.readInternal();
    double celsius = termopar.readCelsius();

    if (isnan(celsius)) {
        totalFalhas++;
        uint8_t erro = termopar.readError();
        Serial.printf("[%lu] ERRO na leitura: ", totalLeituras);

        if (erro & MAX31855_FAULT_OPEN) {
            Serial.print("termopar aberto (nao conectado / fio partido) ");
        }
        if (erro & MAX31855_FAULT_SHORT_GND) {
            Serial.print("curto para o GND ");
        }
        if (erro & MAX31855_FAULT_SHORT_VCC) {
            Serial.print("curto para o VCC ");
        }
        if (erro == 0) {
            Serial.print("(sem bit de falha - suspeite da comunicacao SPI) ");
        }

        Serial.printf("| interna=%.2f C | falhas: %lu/%lu\n",
                      interna, totalFalhas, totalLeituras);
    } else if (celsius == 0.0 && interna == 0.0) {
        // TUDO ZERO NAO E MEDIDA, E AUSENCIA DE COMUNICACAO.
        //
        // O MAX31855 devolve 32 bits de uma vez. Se o pino DO nunca sobe, a
        // biblioteca le 32 zeros -- e 32 zeros formam um resultado
        // perfeitamente coerente: temperatura 0,00, junta fria 0,00 e nenhum
        // bit de falha ligado. isnan() nao pega, readError() devolve 0, e o
        // teste imprime "0.00 C" com cara de leitura boa.
        //
        // O que denuncia e a JUNTA FRIA: ela e o sensor DENTRO do chip e mede
        // a temperatura do proprio encapsulamento. Nao existe bancada em que
        // ela marque exatamente 0,00 C. Se ela zerou, nao estamos falando com
        // o chip -- e a temperatura ao lado dela nao vale nada.
        totalFalhas++;
        Serial.printf("[%lu] SEM COMUNICACAO COM O CHIP: 32 bits zerados "
                      "(temperatura 0,00 e junta fria 0,00 ao mesmo tempo). "
                      "| falhas: %lu/%lu\n",
                      totalLeituras, totalFalhas, totalLeituras);
        if (totalFalhas == 1) {
            Serial.println("    Isto NAO e uma leitura de 0 grau. Confira, nesta ordem:");
            Serial.println("      1. VIN do modulo em 3V3 (nao em 5V, nao solto)");
            Serial.println("      2. GND do modulo no GND da placa");
            Serial.println("      3. DO do modulo no GPIO 25 - e o fio que traz o dado;");
            Serial.println("         se so ele estiver solto, da exatamente este sintoma");
            Serial.println("      4. CLK no GPIO 13 e CS no GPIO 14 (nao troque os dois)");
            Serial.println("      5. Se o modulo for MAX6675 e nao MAX31855, o protocolo");
            Serial.println("         e outro e a biblioteca esta errada");
        }
    } else {
        Serial.printf("[%lu] temperatura: %7.2f C | junta fria (chip): %6.2f C"
                      " | falhas: %lu/%lu\n",
                      totalLeituras, celsius, interna,
                      totalFalhas, totalLeituras);
    }

    delay(INTERVALO_LEITURA_MS);
}
