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
     python capturar_amostras.py capturar --amostra clara_01

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
    "agtron",                           # PREENCHER DEPOIS, com o valor medido
]


def _preparar_camera():
    travados = camera.travar()
    print(f"[CAMERA] travada: {travados}")
    cap, formato = camera.abrir()
    print(f"[CAMERA] formato: {formato}")
    return cap, travados


# --------------------------------------------------------------------------
def cmd_preview(args):
    cap, _ = _preparar_camera()
    frame = camera.media_de_frames(cap)
    cap.release()

    m = camera.medir(frame)

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

    print(f"\namostra     L*={m['L']:6.2f}  a*={m['a']:6.2f}  b*={m['b']:6.2f}"
          f"   desvio_L={m['desvio_L']:.2f}")
    print(f"referencia  L*={m['L_ref']:6.2f}  a*={m['a_ref']:6.2f}  b*={m['b_ref']:6.2f}")
    print(f"\nPreview salvo em: {destino}")
    print("Abra, confira se as miras estao no lugar e ajuste MIRA_AMOSTRA /")
    print("MIRA_REFERENCIA em visao/camera.py se precisar.")
    if m["desvio_L"] > 12:
        print(f"\nATENCAO: desvio_L={m['desvio_L']:.1f} esta alto. A mira pode estar")
        print("pegando fundo, borda do recipiente ou sombra, nao so grao.")


# --------------------------------------------------------------------------
def cmd_capturar(args):
    cap, travados = _preparar_camera()

    os.makedirs(DIR_FRAMES, exist_ok=True)
    novo = not os.path.exists(CSV_SAIDA)

    linhas = []
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
                "agtron": "",
            })
            print(f"  L*={m['L']:6.2f}  a*={m['a']:6.2f}  b*={m['b']:6.2f}  "
                  f"desvio_L={m['desvio_L']:5.2f}  L_norm={m['L_norm']:6.2f}")
    finally:
        cap.release()

    with open(CSV_SAIDA, "a", newline="", encoding="utf-8") as f:
        escritor = csv.DictWriter(f, fieldnames=COLUNAS)
        if novo:
            escritor.writeheader()
        escritor.writerows(linhas)

    Ls = [linha["L"] for linha in linhas]
    print(f"\n{len(linhas)} capturas gravadas em {CSV_SAIDA}")
    print(f"L* medio da amostra: {np.mean(Ls):.2f}  (dispersao entre "
          f"repeticoes: {np.std(Ls):.2f})")
    print("\nA coluna 'agtron' ficou VAZIA de proposito: preencha quando tiver")
    print("o valor medido desta amostra.")


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
                   help="identificador da amostra (ex.: clara_01)")
    c.add_argument("--repeticoes", type=int, default=REPETICOES_PADRAO)

    sub.add_parser("resumo", help="lista o que ja foi coletado")

    args = p.parse_args()
    {"preview": cmd_preview, "capturar": cmd_capturar, "resumo": cmd_resumo}[args.comando](args)


if __name__ == "__main__":
    main()
