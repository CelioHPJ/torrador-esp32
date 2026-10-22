/* ==========================================================================
 *  FILTRO DE PICOS - descarta amostra isolada que salta longe das vizinhas
 * ==========================================================================
 *
 *  O QUE ELE PEGA: em 30/09/2026 apareceram duas leituras isoladas de ~265 C
 *  no meio de leituras de 1 a 30 C, as duas enquanto alguem mexia no cabo.
 *  O mesmo tamanho nas duas (~+256 C) aponta para um bit trocado na leitura
 *  do chip, nao para calor. Uma amostra dessas, mesmo diluida na media de 4,
 *  vira um degrau de dezenas de graus na curva da torra.
 *
 *  O QUE ELE NAO PODE PEGAR: mudanca real e rapida. Mergulhado no gelo, o
 *  termopar caiu ~4 C por segundo. Por isso nenhum salto e rejeitado para
 *  sempre: se CONFIRMACOES amostras SEGUIDAS e parecidas entre si concordam
 *  no valor novo, ele e aceito. Pico e uma amostra so; mudanca real persiste.
 *
 *  Sem dependencia do Arduino de proposito, para poder ser testado no PC.
 * ========================================================================== */

#pragma once

#include <math.h>
#include <stdint.h>

class FiltroPicos {
public:
    // limiteSaltoC: maior diferenca aceita entre amostras seguidas.
    // confirmacoes: quantas amostras seguidas confirmam um nivel novo.
    FiltroPicos(float limiteSaltoC, uint8_t confirmacoes)
        : limite(limiteSaltoC), confirmacoes(confirmacoes) {}

    // true se a amostra vale; false se e pico e deve ficar fora da media.
    bool aceitar(float c)
    {
        if (isnan(referencia) || fabsf(c - referencia) <= limite) {
            referencia = c;
            candidatosSeguidos = 0;
            return true;
        }

        // Longe da referencia. Conta so se estiver perto do candidato
        // anterior: tres picos aleatorios nao podem se confirmar entre si.
        if (candidatosSeguidos > 0 && fabsf(c - candidato) <= limite) {
            candidatosSeguidos++;
        } else {
            candidatosSeguidos = 1;
        }
        candidato = c;

        if (candidatosSeguidos >= confirmacoes) {
            // Nivel novo de verdade. As amostras que confirmaram ficaram de
            // fora da media -- custa ~0,5 s de atraso so neste degrau.
            // As que confirmaram tinham sido contadas como pico; nao eram.
            referencia = c;
            totalPicos -= candidatosSeguidos - 1;
            candidatosSeguidos = 0;
            totalNiveisNovos++;
            return true;
        }
        totalPicos++;
        return false;
    }

    uint32_t picos() const      { return totalPicos; }
    uint32_t niveisNovos() const { return totalNiveisNovos; }

private:
    const float   limite;
    const uint8_t confirmacoes;
    float    referencia         = NAN;  // ultima amostra aceita
    float    candidato          = NAN;  // ultima amostra rejeitada
    uint8_t  candidatosSeguidos = 0;
    uint32_t totalPicos         = 0;
    uint32_t totalNiveisNovos   = 0;
};
