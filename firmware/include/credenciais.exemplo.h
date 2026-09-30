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

/* Rede principal, tentada primeiro. */
#define WIFI_SSID     "nome-da-sua-rede"
#define WIFI_PASSWORD "senha-da-sua-rede"

/* Segunda rede, OPCIONAL (ex.: o hotspot do celular). Se a primeira nao
 * responder, a placa tenta esta, e fica alternando entre as duas enquanto
 * estiver sem rede. Apague as duas linhas se so houver uma rede. */
#define WIFI_SSID_2     "hotspot-do-celular"
#define WIFI_PASSWORD_2 "senha-do-hotspot"

/* Senha da gravacao pela rede (POST /update, usuario "ota"). Obrigatoria:
 * sem ela, qualquer um na mesma rede poderia gravar firmware na placa.
 * Invente uma; o scripts/gravar_ota.py le daqui mesmo. */
#define OTA_SENHA "troque-esta-senha"
