"""
============================================================================
 CAPTURA DE AMOSTRAS PARA CALIBRACAO DA ESCALA AGTRON
============================================================================

 Fotografa amostras de cafe torrado em condicao CONTROLADA e grava L*a*b*
 num CSV, para depois ajustar a regressao cor -> Agtron com treinar.py.

 A camera e os controles vem de visao/camera.py -- o MESMO modulo que a
 medicao em producao usa. E de proposito: se a calibracao e a medicao nao
 enxergarem exatamente a mesma condicao optica, o modelo treinado aqui nao
 vale para o que roda na torra.

 Uso tipico:

     # 1) ver o enquadramento e ajustar as miras
     python capturar_amostras.py preview

     # 2) capturar cada amostra (repete 5x, mexendo nos graos entre elas)
     python capturar_amostras.py capturar --amostra agtron_95

 NOMEIE A AMOSTRA COM O NIVEL -- agtron_95, agtron_85, ... agtron_25. O
 rotulo e deduzido do nome e a coluna 'agtron' ja sai preenchida, o que
 dispensa editar 40 linhas a mao depois. Para nome em outro formato, use
 --agtron 95. Se os dois existirem e discordarem, nada e capturado.

     # 3) conferir o que ja foi coletado
     python capturar_amostras.py resumo

 As miras (ROI) e os valores de exposicao ficam em visao/camera.py.

 SOBRE A REFERENCIA DE CINZA: mantenha um cartao cinza fixo no quadro, na
 mira MIRA_REFERENCIA. Ele e o que permite comparar capturas feitas em
 montagens diferentes (bancada agora, torrador depois). Sem ele, a
 calibracao feita hoje nao transfere quando a camera mudar de lugar.
============================================================================
"""

import argparse
import csv
import os
import re
import sys
from datetime import datetime

import cv2
import numpy as np

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from visao import camera  # noqa: E402

REPETICOES_PADRAO = 5

DIR_BASE = os.path.dirname(os.path.abspath(__file__))
DIR_DADOS = os.path.join(DIR_BASE, "dados")
DIR_FRAMES = os.path.join(DIR_DADOS, "frames")
CSV_SAIDA = os.path.join(DIR_DADOS, "amostras.csv")

COLUNAS = [
    "amostra", "repeticao", "datahora",
    "L", "a", "b",                      # CIELAB da mira da amostra
    "L_ref", "a_ref", "b_ref",          # CIELAB do cartao de referencia
    "L_norm",                           # L normalizado pela referencia
    "desvio_L",                         # dispersao dentro da mira
    "exposicao", "temp_cor", "ganho",
    "arquivo_frame",
    "agtron",                           # do nome da amostra, de --agtron, ou
                                        # preenchida a mao depois
]


def _preparar_camera():
    travados = camera.travar()
    print(f"[CAMERA] travada: {travados}")
    cap, formato = camera.abrir()
    print(f"[CAMERA] formato: {formato}")
    return cap, travados


# --------------------------------------------------------------------------
def _salvar_preview(frame):
    """Grava o frame com as miras desenhadas e devolve o caminho."""
    vis = frame.astype(np.uint8).copy()
    for mira, cor, rotulo in (
        (camera.MIRA_AMOSTRA, (0, 220, 0), "AMOSTRA"),
        (camera.MIRA_REFERENCIA, (220, 160, 0), "REFERENCIA"),
    ):
        x0, y0, x1, y1 = mira
        cv2.rectangle(vis, (x0, y0), (x1, y1), cor, 2)
        cv2.putText(vis, rotulo, (x0, max(14, y0 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, cor, 1, cv2.LINE_AA)

    os.makedirs(DIR_DADOS, exist_ok=True)
    destino = os.path.join(DIR_DADOS, "preview.png")
    cv2.imwrite(destino, vis)
    return destino


def cmd_preview(args):
    cap, _ = _preparar_camera()
    try:
        frame = camera.media_de_frames(cap)
    finally:
        cap.release()

    # A IMAGEM E SALVA ANTES DE MEDIR, DE PROPOSITO. Quando a medida falha por
    # mira estourada ou mal posicionada, e exatamente quando voce mais precisa
    # ver o quadro para corrigir. Medir primeiro faria o preview morrer sem
    # deixar justamente a imagem que explica o problema.
    destino = _salvar_preview(frame)
    print(f"\nPreview salvo em: {destino}")
    print("Abra, confira se as miras estao no lugar e ajuste MIRA_AMOSTRA /")
    print("MIRA_REFERENCIA em visao/camera.py se precisar.")

    try:
        m = camera.medir(frame)
    except camera.CameraSemImagem as e:
        print(f"\nNAO DEU PARA MEDIR ESTE QUADRO:\n{e}")
        print("\nO preview acima continua valendo para ajustar o enquadramento.")
        # Sai com erro mesmo tendo salvo o PNG: o preview cumpriu a parte de
        # mostrar o quadro, mas medida nao houve, e quem chamar isto de um
        # script precisa saber disso pelo codigo de saida, nao so pelo texto.
        raise SystemExit(1)

    print(f"\namostra     L*={m['L']:6.2f}  a*={m['a']:6.2f}  b*={m['b']:6.2f}"
          f"   desvio_L={m['desvio_L']:.2f}")
    print(f"referencia  L*={m['L_ref']:6.2f}  a*={m['a_ref']:6.2f}  b*={m['b_ref']:6.2f}")
    if m["desvio_L"] > 12:
        print(f"\nATENCAO: desvio_L={m['desvio_L']:.1f} esta alto. A mira pode estar")
        print("pegando fundo, borda do recipiente ou sombra, nao so grao.")


# Nome no formato agtron_95 / agtron-95 / agtron95. E a convencao do
# CHECKLIST.md justamente para o rotulo poder ser deduzido daqui.
PADRAO_NOME_ROTULADO = re.compile(r"^agtron[_-]?(\d+(?:[.,]\d+)?)$",
                                  re.IGNORECASE)


def rotulo_da_amostra(nome, explicito):
    """Decide o Agtron desta amostra: o informado, ou o que o nome diz.

    Preencher a coluna depois, a mao, e 40 linhas de digitacao num arquivo em
    que uma celula trocada nao da erro nenhum -- so um modelo pior. Quando o
    nome carrega o nivel, o rotulo vem de graca e a digitacao some.

    Se os dois existirem e discordarem, isso PARA. Uma amostra chamada
    agtron_95 gravada como 85 e exatamente o erro que a conferencia de ordem
    do resumo existe para pegar, e e melhor pegar antes de fotografar.
    """
    casou = PADRAO_NOME_ROTULADO.match(nome)
    do_nome = float(casou.group(1).replace(",", ".")) if casou else None

    if explicito is not None and do_nome is not None and explicito != do_nome:
        raise SystemExit(
            f"\nO nome da amostra diz Agtron {do_nome:g} e --agtron diz "
            f"{explicito:g}. Nada foi capturado.\nCorrija um dos dois -- e "
            "confira qual amostra esta de fato na bancada."
        )

    valor = explicito if explicito is not None else do_nome
    if valor is None:
        return "", None
    origem = "informado por --agtron" if explicito is not None else \
             "deduzido do nome da amostra"
    return f"{valor:g}", origem


# --------------------------------------------------------------------------
def cmd_capturar(args):
    rotulo, origem_rotulo = rotulo_da_amostra(args.amostra, args.agtron)
    if origem_rotulo:
        print(f"[ROTULO] Agtron {rotulo} ({origem_rotulo})")

    cap, travados = _preparar_camera()

    os.makedirs(DIR_FRAMES, exist_ok=True)
    novo = not os.path.exists(CSV_SAIDA)

    linhas = []
    interrompido = None
    try:
        for i in range(1, args.repeticoes + 1):
            print(f"\n--- {args.amostra}, repeticao {i}/{args.repeticoes} ---")
            if i > 1:
                input("Mexa nos graos para virar outros para cima e tecle ENTER... ")
            else:
                input("Posicione a amostra e tecle ENTER para capturar... ")

            frame = camera.media_de_frames(cap)
            m = camera.medir(frame)

            agora = datetime.now()
            nome = f"{args.amostra}_r{i:02d}_{agora:%Y%m%d_%H%M%S}.png"
            # PNG, nunca JPEG: compressao com perda arruinaria a medida de cor.
            cv2.imwrite(os.path.join(DIR_FRAMES, nome), frame.astype(np.uint8))

            linhas.append({
                "amostra": args.amostra, "repeticao": i,
                "datahora": agora.isoformat(timespec="seconds"),
                "L": round(m["L"], 4), "a": round(m["a"], 4), "b": round(m["b"], 4),
                "L_ref": round(m["L_ref"], 4), "a_ref": round(m["a_ref"], 4),
                "b_ref": round(m["b_ref"], 4),
                "L_norm": round(m["L_norm"], 4), "desvio_L": round(m["desvio_L"], 4),
                "exposicao": travados["exposicao"], "temp_cor": travados["temp_cor"],
                "ganho": travados["ganho"], "arquivo_frame": nome,
                "agtron": rotulo,
            })
            print(f"  L*={m['L']:6.2f}  a*={m['a']:6.2f}  b*={m['b']:6.2f}  "
                  f"desvio_L={m['desvio_L']:5.2f}  L_norm={m['L_norm']:6.2f}")
    except (camera.CameraSemImagem, KeyboardInterrupt) as e:
        # NAO PERDER O QUE JA FOI COLETADO. Cada repeticao custa mexer nos graos
        # e esperar; deixar a ultima repeticao estourada apagar as anteriores
        # seria o pior jeito possivel de falhar. Grava o que tem e so DEPOIS
        # propaga o erro.
        interrompido = e
    finally:
        cap.release()

    if linhas:
        with open(CSV_SAIDA, "a", newline="", encoding="utf-8") as f:
            escritor = csv.DictWriter(f, fieldnames=COLUNAS)
            if novo:
                escritor.writeheader()
            escritor.writerows(linhas)

        Ls = [linha["L"] for linha in linhas]
        print(f"\n{len(linhas)} capturas gravadas em {CSV_SAIDA}")
        print(f"L* medio da amostra: {np.mean(Ls):.2f}  (dispersao entre "
              f"repeticoes: {np.std(Ls):.2f})")
        if rotulo:
            print(f"\nColuna 'agtron' preenchida com {rotulo} nas "
                  f"{len(linhas)} linhas.")
        else:
            print("\nA coluna 'agtron' ficou VAZIA: preencha quando tiver o valor")
            print("desta amostra, ou use --agtron / nomeie a amostra agtron_<nivel>.")

    if interrompido is not None:
        if isinstance(interrompido, KeyboardInterrupt):
            raise SystemExit(
                f"\nInterrompido por voce depois de {len(linhas)} de "
                f"{args.repeticoes} repeticoes.")
        raise interrompido


# --------------------------------------------------------------------------
def cmd_resumo(args):
    if not os.path.exists(CSV_SAIDA):
        raise SystemExit(f"Nada coletado ainda ({CSV_SAIDA} nao existe).")

    with open(CSV_SAIDA, newline="", encoding="utf-8") as f:
        linhas = list(csv.DictReader(f))
    if not linhas:
        raise SystemExit("CSV vazio.")

    por_amostra = {}
    for linha in linhas:
        por_amostra.setdefault(linha["amostra"], []).append(linha)

    print(f"{len(linhas)} capturas em {len(por_amostra)} amostras\n")
    print(f"{'amostra':<18} {'n':>3}  {'L* medio':>9} {'disp.':>7} "
          f"{'L_norm':>8}  {'agtron':>7}")
    print("-" * 62)

    ordenadas = sorted(por_amostra.items(),
                       key=lambda kv: np.mean([float(r["L"]) for r in kv[1]]))
    com_agtron = 0
    for nome, rs in ordenadas:
        Ls = [float(r["L"]) for r in rs]
        Ln = [float(r["L_norm"]) for r in rs]
        agtrons = {r["agtron"] for r in rs if r["agtron"].strip()}
        rotulo = agtrons.pop() if len(agtrons) == 1 else ("?" if not agtrons else "CONFLITO")
        if rotulo not in ("?", "CONFLITO"):
            com_agtron += 1
        print(f"{nome:<18} {len(rs):>3}  {np.mean(Ls):>9.2f} {np.std(Ls):>7.2f} "
              f"{np.mean(Ln):>8.2f}  {rotulo:>7}")

    print("\nA lista esta ordenada por L* medido (mais escuro primeiro). Ela DEVE")
    print("bater com a ordem visual das suas amostras -- se alguma estiver fora de")
    print("lugar, ha erro de captura ou de rotulagem, e e muito melhor descobrir")
    print("agora do que depois do modelo treinado.")

    print(f"\n{com_agtron}/{len(por_amostra)} amostras com Agtron preenchido.")
    if com_agtron < 4:
        print("Faltam valores medidos para conseguir treinar (minimo 4).")
    else:
        print("Ja da para rodar treinar.py.")


# --------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(
        description="Captura amostras de cafe com a camera travada em manual.")
    sub = p.add_subparsers(dest="comando", required=True)

    sub.add_parser("preview", help="mostra as miras e os valores atuais")

    c = sub.add_parser("capturar", help="captura uma amostra varias vezes")
    c.add_argument("--amostra", required=True,
                   help="identificador da amostra; agtron_<nivel> ja rotula (ex.: agtron_95)")
    c.add_argument("--repeticoes", type=int, default=REPETICOES_PADRAO)
    c.add_argument("--agtron", type=float, default=None,
                   help="valor de Agtron desta amostra; dispensa preencher o "
                        "CSV depois. Deduzido do nome se ele for agtron_<nivel>")

    sub.add_parser("resumo", help="lista o que ja foi coletado")

    args = p.parse_args()
    comandos = {"preview": cmd_preview, "capturar": cmd_capturar, "resumo": cmd_resumo}

    # As falhas de camera sao PREVISTAS e cada uma pede uma acao diferente do
    # operador, entao saem como mensagem, nao como traceback. O que NAO for
    # falha de camera continua estourando com traceback inteiro de proposito:
    # erro inesperado tem de parecer erro inesperado.
    #
    # MiraSaturada vem antes de CameraSemImagem porque herda dela.
    try:
        comandos[args.comando](args)
    except camera.CameraNaoTravou as e:
        raise SystemExit(
            f"\n[A CAMERA NAO TRAVOU]\n{e}\n\n"
            "Nada foi medido, de proposito: com os automaticos ligados, o que "
            "fosse coletado mediria a reacao da camera ao cafe, nao o cafe."
        )
    except camera.MiraSaturada as e:
        raise SystemExit(f"\n[MIRA ESTOURADA DE LUZ]\n{e}")
    except camera.CameraSemImagem as e:
        raise SystemExit(f"\n[A CAMERA NAO ENTREGOU IMAGEM]\n{e}")


if __name__ == "__main__":
    main()
