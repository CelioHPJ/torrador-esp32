import csv, sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

arq, saida = sys.argv[1], sys.argv[2]
t, T, ev = [], [], []
for l in csv.DictReader(open(arq)):
    if l["evento"]:
        ev.append((float(l["tempo_torra_s"]), float(l["temp_c"]), l["evento"]))
    else:
        t.append(float(l["tempo_torra_s"])); T.append(float(l["temp_c"]))
desc = next((s for s, _, e in ev if e == "descarga"), t[-1])

TINTA, SUAVE, LINHA, COR, APAGADO = "#2e2621", "#6e6259", "#e7e0d5", "#a9502f", "#c9b9a8"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "text.color": TINTA,
                     "axes.edgecolor": LINHA, "axes.labelcolor": SUAVE,
                     "xtick.color": SUAVE, "ytick.color": SUAVE})
fig, a1 = plt.subplots(figsize=(10, 5.5))
fig.patch.set_facecolor("#fcf9f4")
mmss = lambda s, _=None: f"{int(s // 60)}:{int(s) % 60:02d}"

def split(xs, ys):
    a = [(x, y) for x, y in zip(xs, ys) if x <= desc]
    b = [(x, y) for x, y in zip(xs, ys) if x >= desc]
    return list(zip(*a)), list(zip(*b))

for ax, xs, ys in ((a1, t, T),):
    ax.set_facecolor("#fcf9f4")
    (x1, y1), rest = split(xs, ys)
    ax.plot(x1, y1, color=COR, lw=2)
    if rest: ax.plot(rest[0], rest[1], color=APAGADO, lw=1.5)
    ax.grid(axis="y", color=LINHA, lw=0.8); ax.set_axisbelow(True)
    for s in ("top", "right", "left"): ax.spines[s].set_visible(False)
    ax.tick_params(length=0)
    for s, temp, e in ev:
        ax.axvline(s, color=SUAVE, lw=1, ls=(0, (3, 3)))

nomes = {"crack1": "1º crack", "crack2": "2º crack",
         "amarelamento": "amarelou", "descarga": "descarga"}
for s, temp, e in ev:
    a1.annotate(f"{nomes.get(e, e)}\n{mmss(s)} · {temp:.0f} °C", (s, temp),
                xytext=(-8, 14), textcoords="offset points", ha="right",
                fontsize=9, color=TINTA)
    a1.plot(s, temp, "o", ms=8, color=COR, mec="#fcf9f4", mew=2, zorder=5)

imax = max(range(len(T)), key=T.__getitem__)
a1.annotate(f"máx {T[imax]:.0f} °C", (t[imax], T[imax]), xytext=(8, 4),
            textcoords="offset points", fontsize=9, color=SUAVE)
if any(x > desc for x in t):
    a1.text(desc + 8, 60, "resfriamento\n(pipoqueira desligada)", color=SUAVE, fontsize=9)

a1.set_ylabel("temperatura da sonda (°C)"); a1.set_ylim(0, 250)
from matplotlib.ticker import MultipleLocator; a1.xaxis.set_major_locator(MultipleLocator(30)); a1.xaxis.set_major_formatter(mmss); a1.set_xlabel("tempo de torra (min:s)")
a1.set_title("Torra 06/10/2026 · 08:37 · pipoqueira de ar quente — descarga em "
             f"{mmss(desc)} (café começou a queimar)", loc="left", fontsize=12, pad=12)
fig.text(0.125, -0.02, "Sonda tipo K no ar quente da câmara (mede ar + grão, não o interior do grão). "
         "Dados: " + arq.split("/")[-1], fontsize=8, color=SUAVE)
fig.savefig(saida, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
print("ok", saida, "pontos", len(t), "eventos", ev)
