# Checklist da coleta para calibração Agtron

Procedimento para produzir `dados/amostras.csv` e treinar `visao/modelo_agtron.pkl`.

**Dimensionamento:** 8 amostras (Agtron 95, 85, 75, 65, 55, 45, 35, 25) × 5
repetições = 40 capturas. Mínimo aceito pelo `treinar.py`: 4 amostras e faixa de
30 pontos de Agtron. Com os 8 níveis a faixa é 70 — folga confortável.

**Tempo:** reserve a sessão inteira. As 8 amostras têm de ser capturadas sob a
mesma condição óptica, e isso é mais fácil de garantir num período só do que
retomando no dia seguinte.

---

## 0. Antes de começar

- [ ] As 8 amostras de café estão separadas, identificadas e com o nível já
      determinado por comparação com a escala de referência.
- [ ] Anotado **por escrito** como o nível foi atribuído (quem comparou, com qual
      escala, sob qual luz). Isso vai para o relatório: o rótulo é casamento
      visual contra padrão físico, com incerteza de cerca de ±5 Agtron, e essa
      incerteza é o piso do erro do modelo.
- [ ] `calibracao/.venv` ativado e a câmera da bancada ligada e reconhecida pelo
      Windows.

---

## 1. Montagem física — uma vez, depois congelada

A regra que manda em tudo: **as 8 amostras precisam ser fotografadas sob
exatamente a mesma condição.** Diferença de luz entre amostras entra no modelo
disfarçada de diferença de torra.

- [ ] Anteparo fechado em volta da cena (caixa de papelão, fosca e escura por
      dentro), para a luz ambiente não entrar. Sem ele a mesma amostra lê
      diferente às 9h e às 17h.
- [ ] Luz própria fixa, idealmente duas fontes difusas a ~45° dos lados. Luz de
      frente e reta produz brilho especular no topo dos grãos, bem no meio da
      mira.
- [ ] Câmera presa, apontando de cima para baixo, altura fixa. Posição marcada
      com fita.
- [ ] Cartão cinza fixo no quadro, na região da `MIRA_REFERENCIA`. **Ele fica no
      quadro nas 40 fotos.** É o que permite comparar esta calibração com
      capturas futuras, já com a câmera montada no torrador.
- [ ] Porta-amostra sempre o mesmo recipiente, raso, de fundo fosco e escuro,
      sempre na mesma marca de fita.
- [ ] Camada de grãos com 2 a 3 grãos de espessura no mínimo. Se o fundo do
      recipiente aparecer dentro da mira, a medida é do recipiente.

---

## 2. Ajuste óptico — uma vez, antes da primeira amostra

- [ ] `NOME_DSHOW` em `visao/camera.py` trocado pelo nome da câmera da bancada.
      O valor de hoje, `"Integrated Camera"`, é a webcam do notebook.
- [ ] `python capturar_amostras.py preview` rodou sem erro.
- [ ] Abrir `dados/preview.png` e conferir: a mira **AMOSTRA** (verde) cai só
      sobre grãos — sem borda do recipiente, sem sombra — e a **REFERENCIA**
      (azul) cai só sobre o cartão cinza. Ajustar `MIRA_AMOSTRA` e
      `MIRA_REFERENCIA` em `visao/camera.py` e repetir até ficar certo.
- [ ] Com a amostra **95** (a mais clara, a que estoura primeiro) no lugar:
      `preview` não acusa `[MIRA ESTOURADA DE LUZ]`. Se acusar, feche a exposição
      em `EXPOSICAO_DSHOW` (hoje `-10`; faixa `-10..-2`; mais negativo é mais
      escuro) ou reduza a luz. O limite é 2% de pixels saturados, e a recusa é
      deliberada: pixel em 255 erra sempre para o lado claro, que é exatamente a
      direção em que o Agtron varia.
- [ ] Com a amostra **25** (a mais escura): ainda sobra sinal, `L*` não está
      colado no chão.
- [ ] `desvio_L` abaixo de 12 nos dois casos. Acima disso a mira está pegando
      algo que não é só grão.

> **A exposição tem de servir para as duas pontas.** Ache um valor que não
> estoura na 95 nem afunda na 25 — e a partir daí **não mexa mais**. Mudar
> exposição, luz, altura da câmera ou mira no meio da coleta invalida tudo que
> foi capturado antes.

- [ ] Depois de fechar o ajuste, **apagar qualquer `dados/amostras.csv` de teste**.
      Linha capturada sob outra condição óptica contamina o treino em silêncio.

---

## 3. Captura — repetir para cada uma das 8 amostras

```bash
python capturar_amostras.py capturar --amostra agtron_95
```

Use o nome `agtron_<nível>` — `agtron_95`, `agtron_85`, … `agtron_25`. **O
rótulo é deduzido do nome**, e a coluna `agtron` já sai preenchida: não há nada
para digitar depois. O script confirma na primeira linha:

```
[ROTULO] Agtron 95 (deduzido do nome da amostra)
```

Se preferir outro esquema de nomes, passe `--agtron 95`. Com os dois presentes
e discordando, nada é capturado — é o erro de troca de amostra, pego antes de
fotografar.

A cada uma das 5 repetições o script para e pede ENTER. Nesse intervalo:

- [ ] **Mexer nos grãos**, virando outros para cima. Não é formalidade: a
      variação entre as 5 repetições é a medida de quanto o próprio leito
      oscila, e ela é o piso do erro do modelo. Cinco fotos do leito parado
      seriam cinco cópias da mesma foto.
- [ ] Nivelar a superfície de novo — mesma altura, mesma distância da câmera,
      mesma sombra.
- [ ] Tirar a mão de cima do quadro antes de apertar ENTER.
- [ ] Conferir na hora o `L*` impresso: ele deve cair perto do das outras
      repetições da mesma amostra. Um valor fora da curva é melhor descoberto
      agora do que no resumo.

Se a captura for interrompida por mira estourada, **o que já foi capturado é
gravado antes de o erro subir**. Corrija e recapture só a amostra interrompida.

- [ ] As 8 amostras capturadas, 5 repetições cada.

---

## 4. Conferência

Se as amostras foram nomeadas `agtron_<nível>`, a coluna já está preenchida e
não há rotulagem a fazer. Só para o caso de o nome ter sido outro, o preenchimento
a partir do nome, à mão:

```bash
python - <<'EOF'
import csv
p = "dados/amostras.csv"
linhas = list(csv.DictReader(open(p, encoding="utf-8")))
for l in linhas:
    if l["amostra"].startswith("agtron_"):
        l["agtron"] = l["amostra"].split("_")[1]
w = csv.DictWriter(open(p, "w", newline="", encoding="utf-8"), fieldnames=linhas[0])
w.writeheader(); w.writerows(linhas)
EOF
```

- [ ] `python capturar_amostras.py resumo` roda e mostra 8 amostras, 40 capturas,
      8/8 com Agtron preenchido.
- [ ] **A ordem por `L*` medido sai 95, 85, 75, 65, 55, 45, 35, 25 em sequência.**
      Esta é a conferência mais importante do procedimento inteiro. Se duas
      trocarem de lugar: ou houve erro de rotulagem, ou aquelas duas amostras são
      indistinguíveis nesta montagem. Resolver antes de treinar.
- [ ] A dispersão entre repetições (coluna `disp.`) é pequena comparada à
      diferença de `L*` entre amostras vizinhas. Se o ruído de uma amostra for do
      tamanho do passo entre níveis, o modelo não tem como separar os níveis.

---

## 5. Treino

```bash
python treinar.py
```

O script faz duas coisas, nesta ordem:

1. **Escolhe as entradas.** Submete `L_norm`, `L`, `L_norm,a,b` e `L,a,b` à mesma
   validação e fica com o **mais simples** que empata com o melhor dentro de
   1 Agtron. Diferença menor que essa é ruído diante de um rótulo que vale ±5, e
   parâmetro a mais só se paga com erro a menos que sobreviva ao rótulo.
2. **Valida.** Separa uma amostra inteira, ajusta nas outras 7, prevê a que ficou
   de fora, e repete para as 8.

Saem dois números. O **erro médio**, que responde "diante de um café que nunca vi,
quanto erro?" — e não R². E o **acerto de nível**: quantas das 8 caem no degrau
certo, que é a pergunta que um rótulo de escala discreta sustenta.

Se `L_norm` ganhar, melhor ainda: é o único conjunto que sobrevive à câmera mudar
de lugar, porque normaliza pelo cartão cinza.

Como ler o resultado:

| erro médio | leitura |
|---|---|
| abaixo de ~5 Agtron | Reproduz a classificação visual dentro da incerteza da própria classificação. É o teto do que dá para afirmar com rótulo casado a olho. |
| entre 5 e 10 | Distingue as pontas, confunde níveis vizinhos. Reportar como faixa, não como número. |
| acima de 10 | Não é o modelo, é a montagem. Suspeito nº 1: mira pegando o que não é grão. Suspeito nº 2: condição óptica mudou no meio da coleta. |

- [ ] `visao/modelo_agtron.pkl` gravado.
- [ ] Erro médio, erro máximo e acerto de nível anotados, junto da ressalva do rótulo: o modelo
      reproduz a classificação visual com esse erro, e a classificação visual em
      si vale ±5 Agtron. As duas incertezas são coisas diferentes e as duas
      precisam aparecer.

Se algo saiu estranho, `dados/frames/` tem o PNG de cada captura — dá para abrir
e ver exatamente o que a câmera viu em cada linha do CSV.

---

## 6. Depois de treinar

- [ ] `agtron.ler()` parou de devolver `None` e devolve medida.
- [ ] `dados/amostras.csv`, `dados/frames/` e o `.pkl` commitados juntos. O
      `treinar.py` grava o SHA-256 do CSV dentro do modelo: com os três no mesmo
      commit, dá para provar depois qual dado gerou qual modelo.
- [ ] Nada mais tocado em `visao/camera.py`. A condição óptica está gravada no
      modelo, e `agtron.py` avisa no startup se ela mudar.

---

## O que invalida a coleta e obriga a recomeçar

- Mexer em exposição, temperatura de cor, ganho ou miras no meio da coleta.
- Mudar a altura ou a posição da câmera.
- Trocar ou mover a fonte de luz, ou abrir o anteparo e deixar luz ambiente entrar.
- Capturar parte das amostras sem o cartão cinza no quadro.
- Misturar no mesmo CSV linhas capturadas antes e depois de qualquer um dos itens
  acima.

Nenhum desses aparece como erro na tela. Todos aparecem como um modelo que valida
bem e mede errado — que é o modo de falha que este projeto existe para evitar.
