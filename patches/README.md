# Patches para o repositório de visão

Correções preparadas para **`JpCobraC/ProjetoTorrefacaoIA_Robo`**, branch
**`branch-clip`**. Não se aplicam a este repositório.

Foram gerados contra o commit `d4f6449` ("Atualiza README com pinagem do
hardware e comandos do PlatformIO").

## Como aplicar

```bash
cd <onde estiver o ProjetoTorrefacaoIA_Robo>
git checkout branch-clip

# confira antes de escrever qualquer coisa
git apply --check ../torrador-esp32/patches/0001-mira-e-modelo-em-config.patch
git apply --check ../torrador-esp32/patches/0002-dataset-modo-ancoras.patch

# aplique
git apply ../torrador-esp32/patches/0001-mira-e-modelo-em-config.patch
git apply ../torrador-esp32/patches/0002-dataset-modo-ancoras.patch
```

Se o `--check` reclamar, o arquivo mudou desde o `d4f6449` e o trecho precisa ser
reconciliado à mão — o patch diz em qual linha.

---

## 0001 — mira e modelo vêm do config.py

**Problema.** Três constantes viviam duplicadas, e as cópias já tinham divergido:

| | mira (ROI) | modelo |
|---|---|---|
| `config.py` + `validar_sistema.py` (produção) | x 245..355 / y 265..355 | `modelo_agtron_linear_v2_producao.pkl` |
| `analise_torra.py` (ferramenta de conferir) | x 260..420 / y 70..220 | `modelo_agtron_linear.pkl` |

O `analise_torra.py` existe para você conferir visualmente o que o pipeline mede.
Com mira e modelo próprios, ele conferia outra coisa — região diferente do quadro
e modelo diferente. Quem usasse essa tela para validar o enquadramento estaria
validando algo que não é o que roda.

**O que muda.** `config.py` ganha `MODELO_AGTRON` e passa a ser a fonte única;
`analise_torra.py` e `validar_sistema.py` leem de lá.

**Atenção ao aplicar:** a mira de produção passa a valer também para o
`analise_torra.py`. Se a mira antiga dele era a certa para o seu vídeo, o ajuste
agora é em `config.py` — e vale para os dois de uma vez, que é o objetivo.

---

## 0002 — modo `ancoras` no gerador de dataset

**Problema.** Os rótulos de Agtron eram interpolados linearmente no tempo entre
marcações manuais de fase. Duas suposições embutidas:

1. que "isto parece média" corresponde a Agtron exatamente 55;
2. que o grão escurece **em linha reta no tempo** entre duas marcações.

A segunda é falsa — o escurecimento acelera. E como `identificar_ancoras` usa só
o **primeiro** frame de cada fase, o número de observações reais é o número de
fases marcadas, não o de linhas do CSV:

```
dataset_real.csv     →  4 âncoras (Cru, Clara, Media, Escura) em 91 s
dataset_real_v2.csv  →  2 âncoras (Media, Escura) separadas por 3,8 s
```

Um R² alto contra esses alvos mede o quanto a cor reproduz a reta inventada, não
o quanto ela prevê a torra.

**O que muda.** `--modo` passa a ser obrigatório, com duas opções:

- **`ancoras`** — só entram frames a até `--janela` segundos (padrão 2,0) de uma
  marcação, rotulados com o valor daquela marcação. Nenhum rótulo é inventado
  entre marcações. O dataset fica menor e honesto:
  `dataset_real.csv` → 492 frames com 4 rótulos distintos.
- **`linear`** — comportamento anterior, preservado para reproduzir resultados
  antigos. `dataset_real.csv` → 2878 frames.

A saída ganha a coluna `modo`, para que um CSV nunca seja confundido com o outro.

**Por que não há modo com curva exponencial.** Foi uma decisão deliberada.
Trocar a reta por uma curva escolhida a dedo substituiria um chute por outro
chute de aparência mais sofisticada. Enquanto não houver Agtron medido, o único
caminho honesto é treinar onde existe observação.

**Por que `--modo` é obrigatório.** A escolha muda o significado do dataset. Um
padrão silencioso faria alguém gerar dados de um tipo achando que são do outro —
que é a categoria de erro que estes dois patches existem para eliminar.

---

## O que estes patches **não** resolvem

**A proveniência do modelo em produção não fecha.** O
`modelo_agtron_linear_v2_producao.pkl` está documentado como "treinado
exclusivamente com torra_v2.mp4", mas:

- `dataset_real_v2.csv` tem 3.093 linhas e **nenhuma** marcação `Cru`
- `dataset_interpolado_v2_apenas.csv` tem 5.007 linhas e **contém** Agtron 95,0 (= Cru)

O arquivo derivado não pode ter saído dessa fonte: tem mais linhas do que ela e
contém uma fase que ela não marca. Pelo repositório, não dá para estabelecer qual
dado treinou o modelo que está rodando. Isso precisa ser esclarecido por quem
gerou os arquivos — nenhum patch resolve.

**A escala continua não ancorada em medição.** Cru=95 / Clara=75 / Media=55 /
Escura=35 é convenção aplicada a julgamento visual. Só cartela de referência ou
colorímetro resolve.
