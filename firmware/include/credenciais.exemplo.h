/* ==========================================================================
 *  MODELO DE CREDENCIAIS - este arquivo VAI para o git, o outro NAO
 * ==========================================================================
 *
 *  Copie para credenciais.h e preencha com os dados reais da sua rede:
 *
 *      cp include/credenciais.exemplo.h include/credenciais.h
 *
 *  credenciais.h esta no .gitignore, entao a senha preenchida ali nunca
 *  chega ao repositorio. Cada pessoa que for gravar a placa cria o seu.
 *
 *  O ESP32 so fala Wi-Fi de 2,4 GHz: se o seu roteador tiver redes separadas
 *  para 2,4 e 5 GHz, use o SSID da de 2,4.
 * ========================================================================== */

#pragma once

#define WIFI_SSID     "nome-da-sua-rede"
#define WIFI_PASSWORD "senha-da-sua-rede"
