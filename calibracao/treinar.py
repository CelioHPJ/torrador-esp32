"""
============================================================================
 TREINO DO MODELO COR -> AGTRON
============================================================================

 Le o CSV produzido por capturar_amostras.py, ajusta uma regressao que prediz
 Agtron a partir da cor, valida de forma honesta e grava o modelo em
 visao/modelo_agtron.pkl -- de onde visao/agtron.py o carrega.

 Uso:

     python treinar.py                # treina e valida
     python treinar.py --simular      # gera dados falsos e exercita o codigo
     python treinar.py --entradas L   # forca um conjunto de entradas

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

 ---------------------------------------------------------------------------
 SOBRE A ESCOLHA DAS ENTRADAS

 Quais colunas alimentam a regressao NAO e decidido por argumento: cada
 candidato e submetido a MESMA validacao e o que erra menos ganha. O motivo
 de isso existir:

   - 8 amostras sao 8 observacoes independentes, nao 40 -- as repeticoes sao
     fotos do mesmo cafe. Ajustar L, a, b mais intercepto gasta 4 parametros
     em 8 observacoes. L sozinho gasta 2, e o Agtron e essencialmente
     claridade, entao pode muito bem bastar.

   - L_norm reescala o L da amostra pelo cartao de cinza. Se ele validar tao
     bem quanto o L cru, e preferivel: e o unico caminho que sobrevive a
     camera mudar de lugar (bancada agora, torrador depois). Enquanto o
     treino ignorava L_norm, o cartao de cinza era coletado e nao servia
     para nada.

 ---------------------------------------------------------------------------
 SOBRE O ACERTO DE NIVEL

 Alem do erro em Agtron, sai quantas amostras caem no nivel certo. Como os
 rotulos vem de comparacao com uma escala de passo 10, essa e a pergunta que
 o dado sustenta: o rotulo acerta o degrau, mas nao resolve dentro dele. Um
 erro medio de 2 Agtron nao significa "medimos com 2 de erro" -- significa
 "reproduzimos a classificacao visual com 2 de erro", e a classificacao em si
 vale cerca de +-5.
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

# Conjuntos de entrada disputados na validacao. A ordem importa so no desempate:
# com erro igual, ganha o primeiro -- e eles estao do mais simples para o mais
# complexo de proposito, porque parametro a mais sem erro a menos e so risco.
CANDIDATOS = (
    ("L_norm", ("L_norm",)),
    ("L", ("L",)),
    ("L_norm,a,b", ("L_norm", "a", "b")),
    ("L,a,b", ("L", "a", "b")),
)

COLUNAS_COR = ("L", "a", "b", "L_norm")

# Diferenca de erro medio abaixo da qual dois candidatos sao considerados
# empatados, e vence o mais simples. Nao e arbitrario: o rotulo vem de
# casamento visual contra uma escala de passo 10 e vale cerca de +-5 Agtron.
# Escolher um modelo com o dobro dos parametros por meio Agtron de vantagem
# seria escolher por ruido -- e parametro a mais so se paga com erro a menos
# que sobreviva ao rotulo.
TOLERANCIA_EMPATE = 1.0


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
        registro = {"agtron": float(rotulo)}
        for coluna in COLUNAS_COR:
            bruto = (linha.get(coluna) or "").strip()
            # L_norm pode faltar em CSV antigo, gravado antes de a coluna
            # existir. Fica como None e o candidato que depende dela e
            # descartado com o motivo dito, em vez de quebrar o treino todo.
            # "nan" conta como ausente tambem: e o que a captura grava em
            # L_norm quando o cartao de cinza saiu do quadro, e um NaN na
            # matriz derrubaria o LinearRegression com o treino inteiro junto.
            valor = float(bruto) if bruto else None
            registro[coluna] = valor if valor is not None and np.isfinite(valor) else None
        por_amostra[linha["amostra"]].append(registro)

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


def entradas_disponiveis(por_amostra, entradas):
    """Diz se toda captura tem as colunas deste candidato preenchidas."""
    return all(r[coluna] is not None
               for registros in por_amostra.values()
               for r in registros
               for coluna in entradas)


def matriz(registros, entradas):
    X = np.array([[r[coluna] for coluna in entradas] for r in registros],
                 dtype=float)
    y = np.array([r["agtron"] for r in registros], dtype=float)
    return X, y


def validar_deixando_uma_fora(por_amostra, entradas):
    """Erro por amostra, cada uma predita por um modelo que nunca a viu."""
    nomes = sorted(por_amostra)
    resultados = []
    for fora in nomes:
        treino = [r for nome in nomes if nome != fora for r in por_amostra[nome]]
        X, y = matriz(treino, entradas)
        modelo = LinearRegression().fit(X, y)

        Xf, yf = matriz(por_amostra[fora], entradas)
        previsto = float(np.mean(modelo.predict(Xf)))
        real = float(yf[0])
        resultados.append((fora, real, previsto, abs(previsto - real)))
    return resultados


def acerto_de_nivel(resultados, niveis):
    """Quantas amostras caem no nivel certo ao arredondar para o rotulo vizinho.

    Existe porque os rotulos vem de uma escala discreta: comparar com cartela
    acerta o degrau e nao resolve dentro dele. Perguntar "caiu no nivel certo?"
    e a pergunta que o dado sustenta; perguntar "errou por quanto?" ja assume
    resolucao que o rotulo nao tem. Os dois numeros saem, lado a lado.
    """
    acertos = 0
    for _nome, real, previsto, _erro in resultados:
        vizinho = min(niveis, key=lambda v: abs(v - previsto))
        if abs(vizinho - real) < 1e-9:
            acertos += 1
    return acertos


def resumir(resultados, niveis):
    erros = np.array([r[3] for r in resultados])
    return {
        "erro_medio": float(erros.mean()),
        "erro_maximo": float(erros.max()),
        "pior": max(resultados, key=lambda r: r[3])[0],
        "acertos": acerto_de_nivel(resultados, niveis),
        "total": len(resultados),
    }


def gerar_simulado():
    """Dados falsos, so para exercitar o codigo sem as amostras reais.

    Inventa uma relacao plausivel (Agtron sobe com L*) mais ruido, para que o
    caminho inteiro -- leitura, ajuste, validacao, relatorio -- possa ser
    testado antes de existir cafe fotografado.

    O cartao de cinza tambem e simulado, com uma leitura que oscila de captura
    para captura: e assim que L_norm ganha sentido, ja que ele existe para
    absorver justamente essa oscilacao.
    """
    rng = np.random.default_rng(7)
    por_amostra = {}
    for agtron in (95, 85, 75, 65, 55, 45, 35, 25):
        L_base = 18.0 + (agtron - 25) * 0.62
        a_base = 8.0 - (agtron - 25) * 0.03
        b_base = 12.0 + (agtron - 25) * 0.10
        registros = []
        for _ in range(5):
            L_ref = 50.0 + rng.normal(0, 1.5)
            L = L_base + rng.normal(0, 0.8)
            registros.append({
                "L": L,
                "a": a_base + rng.normal(0, 0.4),
                "b": b_base + rng.normal(0, 0.5),
                "L_norm": L * (50.0 / L_ref),
                "agtron": float(agtron),
            })
        por_amostra[f"sim_{agtron:02d}"] = registros
    return por_amostra


def comparar_candidatos(por_amostra, niveis, apenas=None):
    """Roda a MESMA validacao em cada conjunto de entradas e devolve o ranking."""
    avaliados = []
    for rotulo, entradas in CANDIDATOS:
        if apenas is not None and rotulo != apenas:
            continue
        if not entradas_disponiveis(por_amostra, entradas):
            print(f"  {rotulo:<12} descartado: coluna ausente ou nan no CSV "
                  "(capturado antes de a coluna existir, ou cartao de cinza "
                  "fora do quadro?)")
            continue
        resultados = validar_deixando_uma_fora(por_amostra, entradas)
        resumo = resumir(resultados, niveis)
        resumo.update(rotulo=rotulo, entradas=entradas, resultados=resultados)
        avaliados.append(resumo)
    return avaliados


def main():
    p = argparse.ArgumentParser(description="Treina o modelo cor -> Agtron.")
    p.add_argument("--entrada", default=CSV_ENTRADA)
    p.add_argument("--entradas", default=None,
                   choices=[rotulo for rotulo, _ in CANDIDATOS],
                   help="forca um conjunto de entradas em vez de comparar todos")
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

    niveis = sorted({r["agtron"] for r in todos})
    print(f"Faixa de Agtron coberta: {niveis[0]:.0f} a {niveis[-1]:.0f}")
    if niveis[-1] - niveis[0] < 30:
        print("AVISO: faixa estreita. O modelo so vale onde houve amostra --")
        print("       fora dela ele extrapola, e extrapolacao linear de cor erra feio.")

    # --- comparacao das entradas: mesma validacao para todos os candidatos ---
    print("\nEntradas disputando a vaga (mesma validacao para todas):\n")
    print(f"  {'entradas':<12} {'params':>6} {'erro medio':>11} {'erro max':>9} "
          f"{'nivel certo':>12}")
    print("  " + "-" * 54)

    avaliados = comparar_candidatos(por_amostra, niveis, apenas=args.entradas)
    if not avaliados:
        raise SystemExit("Nenhum conjunto de entradas pode ser avaliado.")

    for resumo in avaliados:
        n_params = len(resumo["entradas"]) + 1
        print(f"  {resumo['rotulo']:<12} {n_params:>6} {resumo['erro_medio']:>11.2f} "
              f"{resumo['erro_maximo']:>9.2f} "
              f"{resumo['acertos']:>7}/{resumo['total']:<4}")

    # Vence o MAIS SIMPLES que empata com o melhor dentro da tolerancia, nao o
    # de menor erro absoluto. CANDIDATOS esta ordenado do mais simples para o
    # mais complexo, entao o primeiro que passar no corte e o escolhido.
    melhor = min(r["erro_medio"] for r in avaliados)
    vencedor = next(r for r in avaliados
                    if r["erro_medio"] <= melhor + TOLERANCIA_EMPATE)
    entradas = vencedor["entradas"]
    print(f"\nEscolhido: {vencedor['rotulo']} "
          f"({len(entradas) + 1} parametros em {len(por_amostra)} observacoes "
          "independentes).")
    if vencedor["erro_medio"] > melhor:
        print(f"         Nao e o de menor erro ({melhor:.2f}), e sim o mais "
              f"simples dentro de {TOLERANCIA_EMPATE:.1f} Agtron dele.")
        print("         Diferenca menor que essa e ruido diante de um rotulo")
        print("         que vale +-5.")

    if len(entradas) + 1 > len(por_amostra) - 1:
        print("AVISO: o conjunto escolhido tem tantos parametros quanto a")
        print("       validacao tem observacoes. O erro reportado e fragil.")

    # --- validacao detalhada do vencedor: o numero honesto vem daqui ---
    print("\nValidacao (cada amostra predita por um modelo que nao a viu):\n")
    print(f"  {'amostra':<16} {'real':>6} {'previsto':>9} {'erro':>7} {'nivel':>7}")
    print("  " + "-" * 50)
    for nome, real, previsto, erro in vencedor["resultados"]:
        vizinho = min(niveis, key=lambda v: abs(v - previsto))
        marca = "ok" if abs(vizinho - real) < 1e-9 else f"-> {vizinho:.0f}"
        print(f"  {nome:<16} {real:>6.1f} {previsto:>9.1f} {erro:>7.1f} {marca:>7}")

    print(f"\n  erro medio  = {vencedor['erro_medio']:.2f} Agtron")
    print(f"  erro maximo = {vencedor['erro_maximo']:.2f} Agtron "
          f"(amostra {vencedor['pior']})")
    print(f"  nivel certo = {vencedor['acertos']} de {vencedor['total']} amostras")
    print("\n  Os dois numeros vao para o relatorio, e com o rotulo junto: o")
    print("  modelo reproduz a classificacao visual com esse erro, e a")
    print("  classificacao visual contra escala de passo 10 vale cerca de +-5.")

    # --- modelo final: treinado em tudo ---
    X, y = matriz(todos, entradas)
    modelo = LinearRegression().fit(X, y)
    termos = " ".join(f"{c:+.4f}*{nome}" for c, nome in zip(modelo.coef_, entradas))
    print(f"\nModelo final: Agtron = {modelo.intercept_:.3f} {termos}")

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
            # visao/agtron.py monta o vetor de entrada a partir DESTA lista.
            # Sem ela, um modelo treinado em L_norm receberia L cru na hora de
            # medir, e o erro seria silencioso.
            "entradas": list(entradas),
            "erro_medio_agtron": round(vencedor["erro_medio"], 3),
            "erro_maximo_agtron": round(vencedor["erro_maximo"], 3),
            "acerto_de_nivel": f"{vencedor['acertos']}/{vencedor['total']}",
            "niveis": niveis,
            "tolerancia_empate": TOLERANCIA_EMPATE,
            "candidatos": [
                {"entradas": list(r["entradas"]),
                 "erro_medio_agtron": round(r["erro_medio"], 3),
                 "acerto_de_nivel": f"{r['acertos']}/{r['total']}"}
                for r in avaliados
            ],
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
