/* ==========================================================================
 *  Painel do torrador - "caderno de torra"
 *
 *  Painel de tela cheia para acompanhar a torra ao vivo: leitura na coluna
 *  da esquerda, curva ocupando todo o resto. So acompanha, nao comanda: a
 *  pipoqueira nao e acionada pela placa, entao nao ha o que comandar daqui.
 *  Os botoes so ANOTAM: marcam eventos (1o crack...) e abrem torra nova.
 *  O tratamento visual esta em estilos.css.
 * ========================================================================== */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from 'recharts'
import './estilos.css'

// Endereco do BACKEND (nunca do T-Beam).
const BACKEND = import.meta.env.VITE_BACKEND_HOST || 'localhost:8000'
const URL_WS = `ws://${BACKEND}/ws/temperatura`
const URL_HTTP = `http://${BACKEND}`

// Botoes de anotacao, na ordem em que acontecem numa torra.
const EVENTOS = [
  ['amarelamento', 'amarelou'],
  ['crack1', '1º crack'],
  ['crack2', '2º crack'],
  ['descarga', 'descarga'],
]

const MAX_PONTOS = 1800 // ~30 min a 1 leitura/s

// RoR (rate of rise): quantos graus a temperatura sobe por minuto. Medido
// contra a leitura de JANELA_ROR_S atras, e nao contra a do segundo
// anterior: o termopar oscila uns decimos entre leituras, e um segundo de
// diferenca vezes 60 faria esse ruido pular dezenas de graus por minuto.
const JANELA_ROR_S = 30

// Anexa "ror" (°C/min) a cada ponto. Os primeiros JANELA_ROR_S segundos
// ficam sem valor: ainda nao ha leitura antiga o bastante para comparar.
// Dois ponteiros andando juntos, entao a serie inteira custa O(n).
function comRoR(pontos) {
  let j = 0
  return pontos.map((p) => {
    while (j + 1 < pontos.length && pontos[j + 1].s <= p.s - JANELA_ROR_S) j++
    const antigo = pontos[j]
    if (p.s - antigo.s < JANELA_ROR_S) return { ...p, ror: null }
    return { ...p, ror: Number((((p.temp - antigo.temp) / (p.s - antigo.s)) * 60).toFixed(1)) }
  })
}

// Eixo do RoR em degraus de 20, sempre mostrando o zero: depois da descarga
// o RoR fica negativo, e e bom ver em que momento ele cruza a linha.
const dominioRoR = [
  (min) => Math.min(0, Math.floor(min / 20) * 20),
  (max) => Math.max(20, Math.ceil(max / 20) * 20),
]

// Faixa da regua termica sob o numero: do ambiente ao fim da torra.
const TEMP_PISO = 25
const TEMP_TETO = 230

const numero = (v, casas = 1) =>
  v.toLocaleString('pt-BR', { minimumFractionDigits: casas, maximumFractionDigits: casas })

// Leitura qualitativa da escala Agtron, so para dar sentido ao numero em
// tela. A escala e invertida: valor ALTO = grao CLARO.
const faixaAgtron = (a) =>
  a > 85 ? 'ainda cru'
  : a > 70 ? 'torra clara'
  : a > 60 ? 'média-clara'
  : a > 50 ? 'torra média'
  : a > 45 ? 'média-escura'
  : 'torra escura'

// Eixo do tempo em m:ss - e assim que se anota o tempo de uma torra.
const relogio = (s) => `${Math.floor(s / 60)}:${String(Math.round(s) % 60).padStart(2, '0')}`

function Dica({ active, payload, label }) {
  if (!active || !payload?.length) return null
  const { temp, ror } = payload[0].payload
  return (
    <div className="dica">
      <div className="dica-temp">{numero(temp)} °C</div>
      {ror != null && <div className="dica-temp">RoR {numero(ror)} °C/min</div>}
      <div>{relogio(label)} de torra</div>
    </div>
  )
}

export default function App() {
  const [leitura, setLeitura] = useState(null)      // ultima mensagem recebida
  const [pontos, setPontos] = useState([])          // serie do grafico
  const [offline, setOffline] = useState(true)      // T-Beam fora do ar?
  const [backendOk, setBackendOk] = useState(false) // WebSocket do backend vivo?
  const [eventos, setEventos] = useState([])        // marcas da torra atual
  const [erroAcao, setErroAcao] = useState(null)    // falha de um botao

  const tZeroRef = useRef(null) // instante da primeira leitura, vira o "0 s"

  // ---- normaliza uma leitura crua do backend em um ponto do grafico ------
  // Usamos "recebido_em" (relogio real do backend) e nao o "timestamp" da
  // placa: millis() volta a zero se o T-Beam reiniciar, o que faria o eixo X
  // andar para tras no meio do grafico.
  const paraPonto = useCallback((l) => {
    if (l?.temp == null || l?.recebido_em == null) return null
    if (tZeroRef.current === null) tZeroRef.current = l.recebido_em
    return {
      s: Math.round(l.recebido_em - tZeroRef.current), // segundos desde o inicio
      temp: Number(l.temp.toFixed(2)),
    }
  }, [])

  // ---- WebSocket do backend, com reconexao automatica --------------------
  useEffect(() => {
    let ws
    let timer
    let vivo = true

    const conectar = () => {
      ws = new WebSocket(URL_WS)

      ws.onopen = () => setBackendOk(true)

      ws.onmessage = (evento) => {
        // O unico emissor deste canal e o nosso backend, mas uma mensagem
        // malformada nao pode derrubar o handler e levar junto as leituras
        // seguintes: registra e ignora so aquela.
        let msg
        try {
          msg = JSON.parse(evento.data)
        } catch (erro) {
          console.warn('[torrador] mensagem ignorada (JSON invalido):', evento.data)
          return
        }

        setOffline(Boolean(msg.offline))
        if (Array.isArray(msg.eventos)) setEventos(msg.eventos)

        // Tick de conexao: o backend manda isto enquanto o T-Beam esta fora
        // (e no instante em que ele volta) so para manter o estado da tela em
        // dia. Nao ha leitura nova num tick - sair agora e o que impede a
        // curva de ganhar pontos repetidos e o contador de leituras de subir
        // sozinho durante uma queda. A ultima temperatura recebida continua
        // na tela: ela esta em "leitura", que nao e tocado aqui.
        if (msg.tipo === 'tick') return

        if (msg.temp != null) setLeitura(msg)

        // A primeira mensagem traz o historico da sessao: o grafico ja nasce
        // com a curva inteira em vez de comecar vazio.
        //
        // Um historico que chega e sempre a versao correta da sessao, entao
        // ele SUBSTITUI a serie e refaz o zero do eixo. O reset importa
        // quando o backend reinicia com a pagina aberta: a sessao dele volta
        // ao zero e, sem isto, o grafico continuaria contando desde a sessao
        // anterior e comecaria em "15:00" em vez de "0:00".
        if (Array.isArray(msg.historico)) {
          tZeroRef.current = null
          setPontos(msg.historico.map(paraPonto).filter(Boolean).slice(-MAX_PONTOS))
          return
        }

        const ponto = paraPonto(msg)
        if (ponto) setPontos((atual) => [...atual, ponto].slice(-MAX_PONTOS))
      }

      ws.onclose = () => {
        setBackendOk(false)
        if (vivo) timer = setTimeout(conectar, 2000) // backend caiu: tenta de novo
      }

      ws.onerror = () => ws.close()
    }

    conectar()
    return () => {
      vivo = false
      clearTimeout(timer)
      ws?.close()
    }
  }, [paraPonto])

  // ---- botoes: o backend confirma pelo WebSocket, nada muda aqui direto --
  const acionar = useCallback(async (caminho) => {
    setErroAcao(null)
    try {
      const r = await fetch(`${URL_HTTP}${caminho}`, { method: 'POST' })
      if (!r.ok) setErroAcao((await r.json().catch(() => null))?.detail || `erro ${r.status}`)
    } catch {
      setErroAcao('backend não respondeu')
    }
  }, [])

  const novaTorra = () => {
    if (pontos.length && !window.confirm('Começar torra nova? A curva atual sai da tela (o CSV dela fica salvo).')) return
    acionar('/sessao/nova')
  }

  // ---- estado da cadeia --------------------------------------------------
  const cadeia = !backendOk ? 'morto' : offline ? 'offline' : 'ok'
  const textoCadeia = {
    ok: ['cadeia completa', 'T-Beam, backend e navegador conectados.'],
    offline: ['T-Beam desconectado', 'o backend está no ar e tentando reconectar.'],
    morto: ['backend fora do ar', `sem conexão com ${URL_WS}`],
  }[cadeia]

  const serie = useMemo(() => comRoR(pontos), [pontos])
  const ror = serie.length ? serie[serie.length - 1].ror : null

  const temp = leitura?.temp
  const agtron = leitura?.agtron_estimado
  const preenchimento = temp == null
    ? 0
    : Math.min(100, Math.max(0, ((temp - TEMP_PISO) / (TEMP_TETO - TEMP_PISO)) * 100))

  return (
    <div className="painel">
      <header className="cabecalho">
        <h1 className="titulo">Caderno de torra</h1>
        <p className="subtitulo">
          sessão local · backend <code>{BACKEND}</code>
        </p>
      </header>

      <div className="corpo">
        {/* ======================= coluna de leitura ====================== */}
        <section className="coluna-leitura" aria-label="Leitura">
          <p className={`status status--${cadeia}`} role="status">
            <span className="ponto" aria-hidden="true" />
            <span>
              <b>{textoCadeia[0]}</b>
              <span className="status-detalhe">{textoCadeia[1]}</span>
            </span>
          </p>

          <div>
            <span className="rotulo">
              {offline && temp != null ? 'última temperatura recebida' : 'temperatura'}
            </span>
            <div className="termometro">
              <div
                className={`valor-grande${temp == null ? ' vazio' : ''}${
                  offline && temp != null ? ' parado' : ''
                }`}
              >
                {temp == null ? '—' : numero(temp)}
                <span className="unidade">°C</span>
              </div>
              <div className={`regua${offline ? ' parada' : ''}`}>
                <i style={{ transform: `scaleX(${preenchimento / 100})` }} />
              </div>
            </div>
          </div>

          <div>
            <span className="rotulo">Agtron estimado</span>
            <div
              className={`valor-medio${agtron == null ? ' vazio' : ''}${
                offline && agtron != null ? ' parado' : ''
              }`}
            >
              {agtron == null ? '—' : numero(agtron)}
              {agtron != null && <span className="faixa">{faixaAgtron(agtron)}</span>}
            </div>
            <p className="aviso-estimativa">
              estimativa a partir de tempo e temperatura — não é medição de cor.
              A leitura real virá da câmera numa fase futura.
            </p>
          </div>

          <div className="campos">
            <div>
              <span className="rotulo">leituras</span>
              <div className={`campo-valor${pontos.length === 0 ? ' vazio' : ''}`}>
                {pontos.length || '—'}
              </div>
            </div>
            <div>
              <span className="rotulo">tempo</span>
              <div className={`campo-valor${pontos.length === 0 ? ' vazio' : ''}`}>
                {pontos.length ? relogio(pontos[pontos.length - 1].s) : '—'}
              </div>
            </div>
            <div>
              <span className="rotulo">RoR °C/min</span>
              <div className={`campo-valor${ror == null ? ' vazio' : ''}`}>
                {ror == null ? '—' : numero(ror, 0)}
              </div>
            </div>
          </div>

          <div className="anotacoes">
            <span className="rotulo">anotar</span>
            <div className="botoes">
              {EVENTOS.map(([id, texto]) => {
                const marcado = eventos.find((e) => e.evento === id)
                return (
                  <button
                    key={id}
                    type="button"
                    className={`botao${marcado ? ' marcado' : ''}`}
                    disabled={!backendOk || pontos.length === 0}
                    onClick={() => acionar(`/evento/${id}`)}
                  >
                    {texto}
                    {marcado && (
                      <span className="botao-marca">
                        {relogio(marcado.tempo_torra)} · {numero(marcado.temp, 0)}°
                      </span>
                    )}
                  </button>
                )
              })}
            </div>
            <button type="button" className="botao botao--nova" disabled={!backendOk} onClick={novaTorra}>
              nova torra
            </button>
            {erroAcao && <p className="erro-acao" role="alert">{erroAcao}</p>}
          </div>
        </section>

        {/* ========================== curva ============================== */}
        <section className="area-grafico" aria-label="Curva da torra">
          <div className="grafico-topo">
            <h2 className="secao-titulo">Curva da torra</h2>
            <p className="legenda">
              <span className="legenda-item legenda-item--temp">temperatura · °C</span>
              <span className="legenda-item legenda-item--ror">RoR · °C/min, eixo da direita</span>
            </p>
          </div>
          {pontos.length === 0 ? (
            <div className="grafico-vazio">
              aguardando a primeira leitura do T-Beam — a curva se desenha sozinha a partir daqui
            </div>
          ) : (
            <div className="grafico">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={serie} margin={{ top: 8, right: 4, bottom: 4, left: 0 }}>
                  <CartesianGrid stroke="#ede6db" strokeWidth={1} vertical={false} />
                  <XAxis
                    dataKey="s"
                    type="number"
                    domain={['dataMin', 'dataMax']}
                    tickFormatter={relogio}
                    tick={{ fill: '#6e6259', fontSize: 12, fontFamily: 'Inter, sans-serif' }}
                    axisLine={{ stroke: '#d8cec0' }}
                    tickLine={false}
                    tickMargin={10}
                  />
                  <YAxis
                    yAxisId="temp"
                    domain={[0, 240]}
                    ticks={[0, 60, 120, 180, 240]}
                    tickFormatter={(v) => `${v}°`}
                    tick={{ fill: '#6e6259', fontSize: 12, fontFamily: 'Inter, sans-serif' }}
                    axisLine={false}
                    tickLine={false}
                    width={44}
                  />
                  <YAxis
                    yAxisId="ror"
                    orientation="right"
                    domain={dominioRoR}
                    tickFormatter={(v) => `${v}°/min`}
                    tick={{ fill: '#4f5b3c', fontSize: 12, fontFamily: 'Inter, sans-serif' }}
                    axisLine={false}
                    tickLine={false}
                    width={60}
                  />
                  {eventos.map((e, i) => (
                    <ReferenceLine
                      key={i}
                      yAxisId="temp"
                      x={Math.round(e.tempo_torra)}
                      stroke="#6e6259"
                      strokeDasharray="3 3"
                      label={{ value: e.rotulo, position: 'insideTopLeft', fill: '#6e6259', fontSize: 12 }}
                    />
                  ))}
                  <Tooltip content={<Dica />} cursor={{ stroke: '#d8cec0', strokeWidth: 1 }} />
                  <Line
                    yAxisId="temp"
                    type="monotone"
                    dataKey="temp"
                    stroke="#a9502f"
                    strokeWidth={1.75}
                    dot={false}
                    isAnimationActive={false}
                  />
                  <Line
                    yAxisId="ror"
                    type="monotone"
                    dataKey="ror"
                    stroke="#4f5b3c"
                    strokeWidth={1.25}
                    dot={false}
                    connectNulls={false}
                    isAnimationActive={false}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          )}
        </section>
      </div>
    </div>
  )
}
