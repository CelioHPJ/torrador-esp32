"""
============================================================================
 TREINO DO MODELO COR -> AGTRON
============================================================================

 Le o CSV produzido por capturar_amostras.py, ajusta uma regressao que prediz
 Agtron a partir de L*a*b*, valida de forma honesta e grava o modelo em
 visao/modelo_agtron.pkl -- de onde visao/agtron.py o carrega.

 Uso:

     python treinar.py                # treina e valida
     python treinar.py --simular      # gera dados falsos e exercita o codigo

 O modo --simular existe para o codigo poder ser testado antes de as amostras
 reais terem sido fotografadas e medidas. Ele NAO grava modelo.

 ---------------------------------------------------------------------------
 SOBRE A VALIDACAO

 A validacao e "deixa uma AMOSTRA de fora", nao "deixa uma LINHA de fora".
 As repeticoes de uma mesma amostra sao fotos do mesmo cafe: se algumas
 ficassem no treino e outras no teste, o modelo estaria sendo avaliado em
 dados que ja viu, e o erro sairia otimista demais. Deixando a amostra
 inteira de fora, o numero responde a pergunta que interessa: "diante de um
 cafe que nunca vi, quanto erro?"

 O resultado sai em UNIDADES DE AGTRON, nao em R^2. R^2 diz o quanto da
 variacao foi explicada; erro em Agtron diz o quanto voce erra. Para escrever
 num relatorio, o segundo e verificavel e o primeiro impressiona.
============================================================================
"""

import argparse
import csv
import hashlib
import os
import pickle
import sys
from collections import defaultdict
from datetime import datetime

import numpy as np
from sklearn.linear_model import LinearRegression

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from visao import camera  # noqa: E402

DIR_BASE = os.path.dirname(os.path.abspath(__file__))
CSV_ENTRADA = os.path.join(DIR_BASE, "dados", "amostras.csv")
CAMINHO_MODELO = os.path.join(os.path.dirname(DIR_BASE), "visao", "modelo_agtron.pkl")

MINIMO_AMOSTRAS = 4


def ler_amostras(caminho):
    """Le o CSV e agrupa por amostra, descartando linhas sem Agtron preenchido."""
    if not os.path.exists(caminho):
        raise SystemExit(
            f"'{caminho}' nao existe.\n"
            "Rode capturar_amostras.py antes de treinar."
        )
    with open(caminho, newline="", encoding="utf-8") as f:
        linhas = list(csv.DictReader(f))

    por_amostra = defaultdict(list)
    sem_rotulo = set()
    for linha in linhas:
        rotulo = (linha.get("agtron") or "").strip()
        if not rotulo:
            sem_rotulo.add(linha["amostra"])
            continue
        por_amostra[linha["amostra"]].append({
            "L": float(linha["L"]), "a": float(linha["a"]), "b": float(linha["b"]),
            "agtron": float(rotulo),
        })

    if sem_rotulo:
        print(f"Ignorando {len(sem_rotulo)} amostra(s) sem Agtron preenchido: "
              f"{', '.join(sorted(sem_rotulo))}")

    # Um Agtron por amostra: se as repeticoes divergirem, e erro de digitacao.
    for nome, registros in por_amostra.items():
        valores = {r["agtron"] for r in registros}
        if len(valores) > 1:
            raise SystemExit(
                f"A amostra '{nome}' tem valores de Agtron diferentes entre as "
                f"repeticoes: {sorted(valores)}. Corrija o CSV -- todas as "
                "repeticoes de uma amostra sao o mesmo cafe."
            )
    return por_amostra


def matriz(registros):
    X = np.array([[r["L"], r["a"], r["b"]] for r in registros], dtype=float)
    y = np.array([r["agtron"] for r in registros], dtype=float)
    return X, y


def validar_deixando_uma_fora(por_amostra):
    """Erro por amostra, cada uma predita por um modelo que nunca a viu."""
    nomes = sorted(por_amostra)
    resultados = []
    for fora in nomes:
        treino = [r for nome in nomes if nome != fora for r in por_amostra[nome]]
        X, y = matriz(treino)
        modelo = LinearRegression().fit(X, y)

        Xf, yf = matriz(por_amostra[fora])
        previsto = float(np.mean(modelo.predict(Xf)))
        real = float(yf[0])
        resultados.append((fora, real, previsto, abs(previsto - real)))
    return resultados


def gerar_simulado():
    """Dados falsos, so para exercitar o codigo sem as amostras reais.

    Inventa uma relacao plausivel (Agtron sobe com L*) mais ruido, para que o
    caminho inteiro -- leitura, ajuste, validacao, relatorio -- possa ser
    testado antes de existir cafe fotografado.
    """
    rng = np.random.default_rng(7)
    por_amostra = {}
    for agtron in (95, 85, 75, 65, 55, 45, 35, 25):
        L_base = 18.0 + (agtron - 25) * 0.62
        a_base = 8.0 - (agtron - 25) * 0.03
        b_base = 12.0 + (agtron - 25) * 0.10
        por_amostra[f"sim_{agtron:02d}"] = [{
            "L": L_base + rng.normal(0, 0.8),
            "a": a_base + rng.normal(0, 0.4),
            "b": b_base + rng.normal(0, 0.5),
            "agtron": float(agtron),
        } for _ in range(5)]
    return por_amostra


def main():
    p = argparse.ArgumentParser(description="Treina o modelo cor -> Agtron.")
    p.add_argument("--entrada", default=CSV_ENTRADA)
    p.add_argument("--simular", action="store_true",
                   help="usa dados falsos e NAO grava modelo")
    args = p.parse_args()

    if args.simular:
        print("=" * 68)
        print("MODO SIMULADO: dados inventados. Nenhum modelo sera gravado.")
        print("Serve so para conferir que o codigo roda ponta a ponta.")
        print("=" * 68)
        por_amostra = gerar_simulado()
    else:
        por_amostra = ler_amostras(args.entrada)

    if len(por_amostra) < MINIMO_AMOSTRAS:
        raise SystemExit(
            f"So {len(por_amostra)} amostra(s) com Agtron preenchido; o minimo e "
            f"{MINIMO_AMOSTRAS}. Com menos que isso a regressao passa exatamente "
            "pelos pontos e a validacao nao significa nada."
        )

    todos = [r for registros in por_amostra.values() for r in registros]
    print(f"\n{len(por_amostra)} amostras, {len(todos)} capturas no total.")

    faixa = sorted(r["agtron"] for r in todos)
    print(f"Faixa de Agtron coberta: {faixa[0]:.0f} a {faixa[-1]:.0f}")
    if faixa[-1] - faixa[0] < 30:
        print("AVISO: faixa estreita. O modelo so vale onde houve amostra --")
        print("       fora dela ele extrapola, e extrapolacao linear de cor erra feio.")

    # --- validacao antes do treino final: o numero honesto vem daqui ---
    print("\nValidacao (cada amostra predita por um modelo que nao a viu):\n")
    print(f"  {'amostra':<16} {'real':>6} {'previsto':>9} {'erro':>7}")
    print("  " + "-" * 42)
    resultados = validar_deixando_uma_fora(por_amostra)
    for nome, real, previsto, erro in resultados:
        print(f"  {nome:<16} {real:>6.1f} {previsto:>9.1f} {erro:>7.1f}")

    erros = np.array([r[3] for r in resultados])
    print(f"\n  erro medio  = {erros.mean():.2f} Agtron")
    print(f"  erro maximo = {erros.max():.2f} Agtron (amostra "
          f"{max(resultados, key=lambda r: r[3])[0]})")
    print("\n  Este erro medio e o numero para o relatorio. Ele responde")
    print("  'diante de um cafe novo, quanto erramos' -- que e a pergunta.")

    # --- modelo final: treinado em tudo ---
    X, y = matriz(todos)
    modelo = LinearRegression().fit(X, y)
    c = modelo.coef_
    print(f"\nModelo final: Agtron = {modelo.intercept_:.3f} "
          f"+ {c[0]:.4f}*L {c[1]:+.4f}*a {c[2]:+.4f}*b")

    if args.simular:
        print("\n(modo simulado: nada gravado)")
        return

    with open(args.entrada, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()[:16]

    payload = {
        "modelo": modelo,
        "meta": {
            # Rastreabilidade: no projeto anterior nao da para saber qual dado
            # treinou o modelo em producao. Tudo que responde essa pergunta
            # fica gravado junto do modelo.
            "treinado_em": datetime.now().isoformat(timespec="seconds"),
            "csv_origem": os.path.basename(args.entrada),
            "csv_sha256_16": digest,
            "amostras": sorted(por_amostra),
            "n_capturas": len(todos),
            "entradas": ["L", "a", "b"],
            "erro_medio_agtron": round(float(erros.mean()), 3),
            "erro_maximo_agtron": round(float(erros.max()), 3),
            "optica": camera.assinatura_optica(),
        },
    }
    os.makedirs(os.path.dirname(CAMINHO_MODELO), exist_ok=True)
    with open(CAMINHO_MODELO, "wb") as f:
        pickle.dump(payload, f)

    print(f"\nModelo gravado em {CAMINHO_MODELO}")
    print("A condicao optica do treino foi gravada junto: se a exposicao, o")
    print("ganho ou a mira mudarem, visao/agtron.py avisa no startup.")


if __name__ == "__main__":
    main()
