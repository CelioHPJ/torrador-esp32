/* ==========================================================================
 *  Painel do torrador - "caderno de torra"
 *
 *  Painel de tela cheia para acompanhar a torra ao vivo: leitura e comandos
 *  na coluna da esquerda, curva ocupando todo o resto. A logica de conexao
 *  (WebSocket, reconexao, comandos) e a mesma desde a primeira versao; o
 *  tratamento visual esta em estilos.css.
 * ========================================================================== */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  CartesianGrid, Line, LineChart, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from 'recharts'
import './estilos.css'

// Endereco do BACKEND (nunca do T-Beam).
const BACKEND = import.meta.env.VITE_BACKEND_HOST || 'localhost:8000'
const URL_WS = `ws://${BACKEND}/ws/temperatura`
const URL_COMANDO = `http://${BACKEND}/comando`

const MAX_PONTOS = 1800 // ~30 min a 1 leitura/s

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
  return (
    <div className="dica">
      <div className="dica-temp">{numero(payload[0].value)} °C</div>
      <div>{relogio(label)} de torra</div>
    </div>
  )
}

export default function App() {
  const [leitura, setLeitura] = useState(null)      // ultima mensagem recebida
  const [pontos, setPontos] = useState([])          // serie do grafico
  const [offline, setOffline] = useState(true)      // T-Beam fora do ar?
  const [backendOk, setBackendOk] = useState(false) // WebSocket do backend vivo?
  const [potencia, setPotencia] = useState(100)
  const [retorno, setRetorno] = useState(null)      // { texto, falhou }

  const tZeroRef = useRef(null) // instante da primeira leitura, vira o "0 s"
  // Enquanto o usuario nao encostou no controle, o slider acompanha o valor
  // que a placa reporta. Depois disso ele para de ser sobrescrito - senao a
  // leitura que chega a cada segundo apagaria o que voce esta digitando.
  const usuarioMexeuRef = useRef(false)

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
        if (msg.temp != null) setLeitura(msg)
        if (msg.potencia_atual != null && !usuarioMexeuRef.current) {
          setPotencia(msg.potencia_atual)
        }

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

  // A partir do primeiro ajuste manual, o slider passa a ser do usuario.
  const ajustarPotencia = (valor) => {
    usuarioMexeuRef.current = true
    setPotencia(Number(valor))
  }

  // ---- envio de comandos (POST /comando no backend) ----------------------
  const enviarComando = async (corpo, descricao) => {
    setRetorno({ texto: 'enviando…', falhou: false })
    try {
      const resposta = await fetch(URL_COMANDO, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(corpo),
      })
      const dados = await resposta.json()
      setRetorno(
        resposta.ok
          ? { texto: `${descricao} — registrado no T-Beam.`, falhou: false }
          : { texto: dados.detail ?? `falhou (erro ${resposta.status}).`, falhou: true },
      )
    } catch (erro) {
      setRetorno({ texto: `sem resposta do backend: ${erro.message}`, falhou: true })
    }
  }

  // ---- estado da cadeia --------------------------------------------------
  const cadeia = !backendOk ? 'morto' : offline ? 'offline' : 'ok'
  const textoCadeia = {
    ok: ['cadeia completa', 'T-Beam, backend e navegador conectados.'],
    offline: ['T-Beam desconectado', 'o backend está no ar e tentando reconectar.'],
    morto: ['backend fora do ar', `sem conexão com ${URL_WS}`],
  }[cadeia]

  const podeComandar = backendOk && !offline
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
        {/* ================= coluna de leitura + comandos ================= */}
        <section className="coluna-leitura" aria-label="Leitura e comandos">
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
              <span className="rotulo">potência</span>
              <div className={`campo-valor${leitura?.potencia_atual == null ? ' vazio' : ''}`}>
                {leitura?.potencia_atual ?? '—'}
                <span className="sufixo"> %</span>
              </div>
            </div>
            <div>
              <span className="rotulo">motor</span>
              <div
                className={`campo-valor${leitura?.motor_ligado ? ' ligado' : ''}${
                  leitura?.motor_ligado == null ? ' vazio' : ''
                }`}
              >
                {leitura?.motor_ligado == null ? '—' : leitura.motor_ligado ? 'ligado' : 'desligado'}
              </div>
            </div>
            <div>
              <span className="rotulo">leituras</span>
              <div className={`campo-valor${pontos.length === 0 ? ' vazio' : ''}`}>
                {pontos.length || '—'}
              </div>
            </div>
          </div>

          <div className="comandos">
            <h2 className="secao-titulo">Comandos</h2>

            <div className="linha-comando">
              <label className="rotulo" htmlFor="potencia">potência</label>
              <input
                id="potencia"
                className="deslizante"
                type="range" min="0" max="100" value={potencia}
                disabled={!podeComandar}
                onChange={(e) => ajustarPotencia(e.target.value)}
              />
              <input
                className="entrada"
                type="number" min="0" max="100" value={potencia}
                disabled={!podeComandar}
                aria-label="potência em porcentagem"
                onChange={(e) => ajustarPotencia(e.target.value)}
              />
              <button
                className="botao botao--forte"
                disabled={!podeComandar}
                onClick={() => enviarComando({ potencia }, `potência ${potencia}%`)}
              >
                registrar
              </button>
            </div>

            <div className="linha-comando">
              <span className="rotulo">motor</span>
              <button
                className="botao"
                disabled={!podeComandar}
                onClick={() => enviarComando({ motor: 'on' }, 'motor ligado')}
              >
                ligar
              </button>
              <button
                className="botao"
                disabled={!podeComandar}
                onClick={() => enviarComando({ motor: 'off' }, 'motor desligado')}
              >
                desligar
              </button>
            </div>

            <p className={`retorno${retorno?.falhou ? ' falhou' : ''}`} aria-live="polite">
              {retorno?.texto}
            </p>

            {!podeComandar && (
              <p className="nota">
                comandos indisponíveis enquanto a cadeia não estiver completa.
              </p>
            )}
          </div>
        </section>

        {/* ========================== curva ============================== */}
        <section className="area-grafico" aria-label="Curva da torra">
          <h2 className="secao-titulo">Curva da torra</h2>
          {pontos.length === 0 ? (
            <div className="grafico-vazio">
              aguardando a primeira leitura do T-Beam — a curva se desenha sozinha a partir daqui
            </div>
          ) : (
            <div className="grafico">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={pontos} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
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
                    domain={[0, 240]}
                    ticks={[0, 60, 120, 180, 240]}
                    tickFormatter={(v) => `${v}°`}
                    tick={{ fill: '#6e6259', fontSize: 12, fontFamily: 'Inter, sans-serif' }}
                    axisLine={false}
                    tickLine={false}
                    width={44}
                  />
                  <Tooltip content={<Dica />} cursor={{ stroke: '#d8cec0', strokeWidth: 1 }} />
                  <Line
                    type="monotone"
                    dataKey="temp"
                    stroke="#a9502f"
                    strokeWidth={1.75}
                    dot={false}
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
