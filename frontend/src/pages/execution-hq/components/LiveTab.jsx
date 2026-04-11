import React, { useState, useEffect, useRef } from 'react';
import { Activity, Crosshair, BarChart2, Database, Radio, ShieldCheck, Sliders, Server, AlertTriangle, Zap, ChevronLeft, ChevronRight, Plus } from 'lucide-react';
import Panel from './Panel.jsx';
import ContinuousFeed from './ContinuousFeed.jsx';
import CanvasPriceChart from './CanvasPriceChart.jsx';
import CanvasRiskSurface from './CanvasRiskSurface.jsx';
import GateAuditMatrix from './GateAuditMatrix.jsx';
import GateHeartbeat from './GateHeartbeat.jsx';
import { getEntryCap, getDuneEntryCap, getCapWithPi, PI_BONUS_CENTS, DUNE_MIN_P, DUNE_CAP_MARGIN, V10_MIN_EVAL_OFFSET, T } from './constants.js';

const INITIAL_CANDLES = [
  { open: 0.620, high: 0.640, low: 0.615, close: 0.635 },
  { open: 0.635, high: 0.638, low: 0.625, close: 0.628 },
  { open: 0.628, high: 0.650, low: 0.625, close: 0.645 },
  { open: 0.645, high: 0.648, low: 0.630, close: 0.632 },
  { open: 0.632, high: 0.642, low: 0.630, close: 0.640 },
];

/**
 * LiveTab — Real-time execution monitoring with collapsible sidebars.
 *
 * Props:
 *   hqData — Data from /api/v58/execution-hq (system, recent_trades, windows)
 *   tick   — Incrementing counter for animation
 */
export default function LiveTab({ hqData, tick, v9Stats, v9GateData, v10Stats }) {
  const [leftExpanded, setLeftExpanded] = useState(true);
  const [rightExpanded, setRightExpanded] = useState(true);

  // Countdown: derive from system clock aligned to 5-min windows
  const [currentT, setCurrentT] = useState(240);
  const [pastCandles, setPastCandles] = useState(INITIAL_CANDLES);
  const [currentWindowPrices, setCurrentWindowPrices] = useState([{ t: 240, price: 0.640 }]);
  const lastPriceRef = useRef(0.640);

  // If we have real price data from the API, use it for candles
  useEffect(() => {
    if (hqData?.candles?.length > 0) {
      setPastCandles(hqData.candles.slice(-15));
    }
  }, [hqData?.candles]);

  // Simulated countdown + price (will be replaced by real WebSocket data)
  useEffect(() => {
    const timer = setInterval(() => {
      setCurrentT(prev => {
        const nextT = prev <= 60 ? 240 : prev - 10;

        const movement = (Math.random() - 0.5) * 0.008;
        const newPrice = lastPriceRef.current + movement;
        lastPriceRef.current = newPrice;

        if (prev <= 60) {
          setCurrentWindowPrices(prices => {
            if (prices.length > 0) {
              const open = prices[0].price;
              const close = newPrice;
              const high = Math.max(...prices.map(p => p.price), newPrice);
              const low = Math.min(...prices.map(p => p.price), newPrice);
              setPastCandles(c => [...c.slice(-14), { open, high, low, close }]);
            }
            return [{ t: 240, price: newPrice }];
          });
          return 240;
        } else {
          setCurrentWindowPrices(prices => [...prices, { t: nextT, price: newPrice }]);
          return nextT;
        }
      });
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  // v10: Use DUNE dynamic cap when available, fallback to v9 fixed cap
  const latestWindow = hqData?.windows?.[0] || {};
  const duneP = latestWindow.dune_probability_up;
  const duneCap = latestWindow.dune_cap ?? getDuneEntryCap(duneP);
  const currentCap = duneCap ?? getEntryCap(currentT);
  const recentTrades = hqData?.recent_trades || [];
  const system = hqData?.system || {};

  // Dynamic center column span
  const getCenterStyle = () => {
    if (leftExpanded && rightExpanded) return { gridColumn: 'span 6' };
    if (!leftExpanded && !rightExpanded) return { gridColumn: 'span 10' };
    return { gridColumn: 'span 8' };
  };

  return (
    <div style={{
      display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0, gap: 0,
    }}>
      {/* UI-01: V10.6 gate heartbeat — 8-gate pipeline status from signal_evaluations */}
      <GateHeartbeat gateHeartbeat={hqData?.gate_heartbeat || []} />

      <div style={{
        display: 'grid', gridTemplateColumns: 'repeat(12, 1fr)', gridTemplateRows: 'repeat(6, 1fr)',
        gap: 8, flex: 1, minHeight: 0, transition: 'all 300ms',
      }}>
      {/* LEFT COLUMN */}
      {leftExpanded ? (
        <div style={{ gridColumn: 'span 3', gridRow: 'span 6', display: 'flex', flexDirection: 'column', gap: 8, minHeight: 0 }}>
          <Panel
            title="Current Eval Window"
            icon={Crosshair}
            style={{ flexShrink: 0, position: 'relative', overflow: 'hidden' }}
            headerRight={
              <button onClick={() => setLeftExpanded(false)} style={{ background: 'none', border: 'none', color: T.textMuted, cursor: 'pointer' }}>
                <ChevronLeft size={14} />
              </button>
            }
          >
            <div style={{ position: 'absolute', top: 0, right: 0, padding: 8, opacity: 0.1 }}>
              <Zap size={64} />
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', padding: '16px 0', borderBottom: `1px solid ${T.cardBorder}` }}>
              <span style={{ fontSize: 10, color: T.textMuted, fontFamily: 'monospace', marginBottom: 4 }}>EVAL COUNTDOWN</span>
              <div style={{ fontSize: 48, fontFamily: 'monospace', fontWeight: 700, color: T.cyan, letterSpacing: '-0.05em' }}>
                T-{currentT}
              </div>
              <div style={{ width: '100%', background: T.cardBorder, height: 4, marginTop: 12, borderRadius: 4, overflow: 'hidden' }}>
                <div style={{
                  height: '100%', background: T.cyan,
                  width: `${((240 - currentT) / 180) * 100}%`,
                  transition: 'width 500ms linear',
                }} />
              </div>
            </div>
            {/* v10 DUNE dynamic cap + confidence */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginTop: 12 }}>
              <div style={{ background: 'rgba(30,41,59,0.5)', padding: 8, borderRadius: 4, border: `1px solid ${T.cardBorder}50`, textAlign: 'center' }}>
                <div style={{ fontSize: 9, color: T.textMuted, fontFamily: 'monospace' }}>v10 DUNE CAP</div>
                <div style={{ fontSize: 18, fontFamily: "'JetBrains Mono', monospace", color: duneCap != null ? T.cyan : T.amber }}>
                  ${currentCap.toFixed(2)}
                </div>
                <div style={{ fontSize: 9, color: T.textDim, marginTop: 2 }}>
                  {duneCap != null ? `P-${(DUNE_CAP_MARGIN * 100).toFixed(0)}pp` : 'v9 fallback'}
                </div>
              </div>
              <div style={{ background: 'rgba(30,41,59,0.5)', padding: 8, borderRadius: 4, border: `1px solid ${T.cardBorder}50`, textAlign: 'center' }}>
                <div style={{ fontSize: 9, color: T.textMuted, fontFamily: 'monospace' }}>DUNE P(dir)</div>
                <div style={{
                  fontSize: 18, fontFamily: "'JetBrains Mono', monospace", fontWeight: 700,
                  color: duneP != null
                    ? (Math.max(duneP, 1 - duneP) >= 0.75 ? T.green : Math.max(duneP, 1 - duneP) >= 0.60 ? T.amber : T.red)
                    : T.textDim,
                }}>
                  {duneP != null ? Math.max(duneP, 1 - duneP).toFixed(3) : '--'}
                </div>
                <div style={{ fontSize: 9, color: T.textDim, marginTop: 2 }}>
                  {duneP != null ? (Math.max(duneP, 1 - duneP) >= DUNE_MIN_P ? 'PASS' : 'BELOW MIN') : `min ${DUNE_MIN_P}`}
                </div>
              </div>
            </div>
            {/* v10 source agreement badge */}
            {(() => {
              const agree = latestWindow.source_agreement;
              return (
                <div style={{
                  marginTop: 12,
                  background: agree === true ? 'rgba(16,185,129,0.08)' : agree === false ? 'rgba(239,68,68,0.08)' : 'rgba(100,116,139,0.08)',
                  border: `1px solid ${agree === true ? 'rgba(16,185,129,0.3)' : agree === false ? 'rgba(239,68,68,0.3)' : T.cardBorder}`,
                  padding: 8, borderRadius: 4, fontSize: 10, fontFamily: 'monospace',
                  display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                }}>
                  <span style={{ color: agree === true ? T.green : agree === false ? T.red : T.textMuted }}>
                    {agree === true ? 'CL+TI AGREE' : agree === false ? 'CL+TI DISAGREE' : 'SOURCE AGREEMENT: --'}
                    {agree === true ? ' (94.7% WR)' : agree === false ? ' (9.1% WR)' : ''}
                  </span>
                  <span style={{
                    fontSize: 14, fontWeight: 700,
                    color: agree === true ? T.green : agree === false ? T.red : T.textDim,
                  }}>{agree === true ? '\u2713' : agree === false ? '\u2717' : '--'}</span>
                </div>
              );
            })()}
            {/* v10 min eval offset indicator */}
            <div style={{
              marginTop: 8, background: 'rgba(6,182,212,0.08)', border: '1px solid rgba(6,182,212,0.2)',
              padding: 8, borderRadius: 4, fontSize: 10, fontFamily: 'monospace', color: T.cyan,
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            }}>
              <span>MIN EVAL: T-{V10_MIN_EVAL_OFFSET} | FAK</span>
              <span style={{ fontSize: 9, color: T.textDim }}>v10.0</span>
            </div>
          </Panel>

          <Panel title="6 Continuous Feeds" icon={Radio} style={{ flex: 1, minHeight: 0 }}>
            {(() => {
              // Wire feeds to real data from latest window when available
              const w = hqData?.windows?.[0] || {};
              const dcl = w.delta_chainlink;
              const dti = w.delta_tiingo;
              const dp = w.delta_pct;
              const vpin = w.vpin;
              const fillPrice = w.clob_fill_price;
              return (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4, overflowY: 'auto', paddingRight: 4 }}>
                  <ContinuousFeed name="Chainlink" hz="5s" latency={120 + (tick % 3) * 10}
                    val={dcl != null ? `${dcl >= 0 ? '+' : ''}${(dcl * 100).toFixed(3)}%` : '--'}
                    change={dcl != null ? +(dcl * 100).toFixed(2) : 0}
                    status={dcl != null ? 'ok' : 'warn'} />
                  <ContinuousFeed name="Tiingo" hz="2s" latency={45 + (tick % 2) * 5}
                    val={dti != null ? `${dti >= 0 ? '+' : ''}${(dti * 100).toFixed(3)}%` : '--'}
                    change={dti != null ? +(dti * 100).toFixed(2) : 0}
                    status={dti != null ? 'ok' : 'warn'} />
                  <ContinuousFeed name="CLOB (Poly)" hz="10s" latency={350}
                    val={fillPrice != null ? `$${fillPrice.toFixed(4)}` : (w.gamma_up_price != null ? `$${w.gamma_up_price.toFixed(4)}` : '--')}
                    change={0} status={fillPrice != null ? 'ok' : 'warn'} />
                  <ContinuousFeed name="Binance" hz="~1Hz" latency={12}
                    val={dp != null ? `${dp >= 0 ? '+' : ''}${(dp * 100).toFixed(3)}%` : '--'}
                    change={dp != null ? +(dp * 100).toFixed(2) : 0}
                    status="ok" />
                  <ContinuousFeed name="CoinGlass" hz="15s" latency={800}
                    val={w.regime || '--'} change={0}
                    status={w.regime === 'CASCADE' ? 'warn' : 'ok'} />
                  <ContinuousFeed name="DUNE (Cedar)" hz="eval"
                    val={w.dune_probability_up != null ? `P=${Math.max(w.dune_probability_up, 1-w.dune_probability_up).toFixed(3)}` : (vpin != null ? `VPIN=${vpin.toFixed(3)}` : '--')}
                    change={w.dune_probability_up != null ? +(Math.max(w.dune_probability_up, 1-w.dune_probability_up) * 100).toFixed(0) : 0}
                    latency={0}
                    status={w.dune_probability_up != null && Math.max(w.dune_probability_up, 1-w.dune_probability_up) >= 0.75 ? 'ok' : (w.dune_probability_up != null && Math.max(w.dune_probability_up, 1-w.dune_probability_up) < 0.60 ? 'err' : 'warn')} />
                </div>
              );
            })()}
          </Panel>
        </div>
      ) : (
        <div
          onClick={() => setLeftExpanded(true)}
          style={{
            gridColumn: 'span 1', gridRow: 'span 6', display: 'flex', flexDirection: 'column',
            gap: 8, minHeight: 0, cursor: 'pointer',
          }}
        >
          <Panel style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 24, opacity: 0.5 }}>
              <ChevronRight size={18} style={{ color: T.cyan }} />
              <div style={{
                color: T.cyan, fontFamily: 'monospace', fontSize: 10, letterSpacing: '0.1em', whiteSpace: 'nowrap',
                writingMode: 'vertical-rl', transform: 'rotate(180deg)',
              }}>LIVE FEEDS & STATUS</div>
            </div>
          </Panel>
        </div>
      )}

      {/* CENTER COLUMN */}
      <div style={{ ...getCenterStyle(), gridRow: 'span 6', display: 'flex', flexDirection: 'column', gap: 8, minHeight: 0, transition: 'all 300ms' }}>
        <Panel title="v10 DUNE Gate Pipeline — 19 Checkpoints (T-240 to T-60)" icon={Database} style={{ flex: 1.5, minHeight: 0 }}>
          <GateAuditMatrix currentT={currentT} v9GateData={v9GateData} />
        </Panel>

        <Panel
          title="Real-Time Price & Window History"
          icon={BarChart2}
          style={{ flex: 2, minHeight: 0 }}
          headerRight={
            <button
              onClick={() => setCurrentT(60)}
              style={{
                fontSize: 9, background: 'rgba(6,182,212,0.15)', color: T.cyan,
                padding: '2px 8px', borderRadius: 2, border: `1px solid rgba(6,182,212,0.3)`,
                cursor: 'pointer', fontFamily: 'monospace', textTransform: 'uppercase', letterSpacing: '0.05em',
                display: 'flex', alignItems: 'center', gap: 4,
              }}
            >
              <Plus size={10} /> Force Close Window
            </button>
          }
        >
          <CanvasPriceChart currentT={currentT} currentPrices={currentWindowPrices} pastCandles={pastCandles} />
        </Panel>

        <Panel title="Risk Surface (DUNE P x Delta)" icon={Activity} style={{ flex: 1.5, minHeight: 0 }}>
          <div style={{ position: 'relative', width: '100%', height: '100%' }}>
            <CanvasRiskSurface currentT={currentT} />
            <div style={{
              position: 'absolute', top: 8, right: 8, background: 'rgba(15,23,42,0.8)',
              border: `1px solid ${T.cardBorder}`, padding: 8, borderRadius: 4, fontSize: 9, fontFamily: 'monospace',
            }}>
              <div style={{ color: T.cyan, marginBottom: 4 }}>SURFACE_VARS</div>
              <div>Z: {duneP != null ? Math.max(duneP, 1-duneP).toFixed(3) : (0.45 + Math.sin(tick * 0.1) * 0.1).toFixed(3)} (DUNE P)</div>
              <div>X: {(0.01 + Math.cos(tick) * 0.01).toFixed(4)} (delta)</div>
              <div>CAP: ${currentCap.toFixed(2)}</div>
            </div>
          </div>
        </Panel>
      </div>

      {/* RIGHT COLUMN */}
      {rightExpanded ? (
        <div style={{ gridColumn: 'span 3', gridRow: 'span 6', display: 'flex', flexDirection: 'column', gap: 8, minHeight: 0 }}>
          <Panel
            title="v10 DUNE GATE PIPELINE"
            icon={ShieldCheck}
            style={{ flexShrink: 0, background: 'linear-gradient(to bottom, rgba(15,23,42,1), rgba(168,85,247,0.05))', borderColor: 'rgba(168,85,247,0.3)' }}
            headerRight={
              <button onClick={() => setRightExpanded(false)} style={{ background: 'none', border: 'none', color: T.textMuted, cursor: 'pointer' }}>
                <ChevronRight size={14} />
              </button>
            }
          >
            {/* v10 gate pipeline: Agreement -> DUNE P>=0.65 -> CG Veto -> Dynamic Cap -> FAK */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {(() => {
                const lw = latestWindow;
                const lwDuneP = lw.dune_probability_up;
                const lwDuneDir = lw.dune_direction;
                const lwDunePDir = lwDuneP != null && lw.source_agreement === true
                  ? (lw.direction === 'DOWN' ? (1 - lwDuneP) : lwDuneP)
                  : null;
                const dunePass = lwDunePDir != null && lwDunePDir >= DUNE_MIN_P;
                const lwDuneCap = lw.dune_cap ?? getDuneEntryCap(lwDunePDir);
                const pipelineSteps = [
                  {
                    label: 'Agreement',
                    key: 'source_agreement',
                    pass: lw.source_agreement === true,
                    detail: lw.source_agreement === true ? 'CL+TI' : lw.source_agreement === false ? 'DISAGREE' : '--',
                  },
                  {
                    label: `DUNE P\u2265${DUNE_MIN_P}`,
                    key: 'dune_confidence',
                    pass: dunePass,
                    detail: lwDunePDir != null ? `P=${lwDunePDir.toFixed(3)}` : '--',
                  },
                  {
                    label: 'CG Veto',
                    key: 'gate_cg',
                    pass: !lw.gate_failed || lw.gate_failed !== 'cg_veto',
                    detail: lw.gate_failed === 'cg_veto' ? 'VETOED' : 'CLEAR',
                  },
                  {
                    label: 'Dynamic Cap',
                    key: 'dune_cap',
                    pass: lwDuneCap != null,
                    detail: lwDuneCap != null ? `$${lwDuneCap.toFixed(2)}` : '--',
                  },
                  {
                    label: 'FAK Result',
                    key: 'fak_result',
                    pass: lw.trade_placed,
                    detail: lw.order_type || 'FAK',
                  },
                ];
                return pipelineSteps.map((step, i) => (
                  <div key={step.key} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <div style={{
                      width: 20, height: 20, borderRadius: '50%', flexShrink: 0,
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      fontSize: 11, fontWeight: 700,
                      background: step.pass ? 'rgba(16,185,129,0.2)' : 'rgba(239,68,68,0.15)',
                      color: step.pass ? T.green : T.red,
                      border: `1px solid ${step.pass ? 'rgba(16,185,129,0.4)' : 'rgba(239,68,68,0.3)'}`,
                    }}>{step.pass ? '\u2713' : '\u2717'}</div>
                    <div style={{ flex: 1, display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '4px 8px', background: 'rgba(30,41,59,0.6)', borderRadius: 4 }}>
                      <span style={{ fontSize: 10, fontFamily: 'monospace', color: T.textMuted }}>{step.label}</span>
                      <span style={{ fontSize: 10, fontFamily: "'JetBrains Mono', monospace", color: step.pass ? T.green : T.red }}>{step.detail}</span>
                    </div>
                    {i < pipelineSteps.length - 1 && (
                      <div style={{ position: 'absolute', left: 21, marginTop: 28, width: 1, height: 6, background: T.cardBorder }} />
                    )}
                  </div>
                ));
              })()}
              {/* Fill price + DUNE cap display */}
              {(() => {
                if (!latestWindow.clob_fill_price) return null;
                const capVal = latestWindow.dune_cap ?? latestWindow.v9_cap;
                const piCap = capVal != null ? getCapWithPi(capVal) : null;
                return (
                  <div style={{ marginTop: 4, padding: 8, background: 'rgba(168,85,247,0.08)', border: '1px solid rgba(168,85,247,0.2)', borderRadius: 4, fontSize: 10, fontFamily: "'JetBrains Mono', monospace" }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', color: T.text }}>
                      <span>Fill Price</span>
                      <span style={{ color: T.cyan }}>${latestWindow.clob_fill_price.toFixed(4)}</span>
                    </div>
                    {piCap && (
                      <div style={{ display: 'flex', justifyContent: 'space-between', color: T.textMuted, marginTop: 2 }}>
                        <span>Cap + pi</span>
                        <span>${piCap.toFixed(2)}</span>
                      </div>
                    )}
                    {latestWindow.partial_fill && (
                      <div style={{ color: T.amber, marginTop: 2 }}>PARTIAL FILL (FAK)</div>
                    )}
                  </div>
                );
              })()}
            </div>
          </Panel>

          <Panel title="v10 Configuration" icon={Sliders} style={{ flex: 1, minHeight: 0 }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 2, overflowY: 'auto', paddingRight: 4 }}>
              <div style={{ fontSize: 9, color: T.cyan, fontWeight: 700, marginBottom: 4, borderBottom: `1px solid ${T.cardBorder}`, paddingBottom: 4 }}>DUNE THRESHOLDS</div>
              {[['DUNE_MIN_P', `${DUNE_MIN_P}`], ['CAP_MARGIN', `${(DUNE_CAP_MARGIN * 100).toFixed(0)}pp`], ['CAP_FLOOR', `$${(0.30).toFixed(2)}`], ['CAP_CEILING', `$${(0.75).toFixed(2)}`]].map(([label, val]) => (
                <div key={label} style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', fontSize: 12, fontFamily: 'monospace' }}>
                  <span style={{ color: T.textMuted }}>{label}</span>
                  <span style={{ color: T.text }}>{val}</span>
                </div>
              ))}
              <div style={{ fontSize: 9, color: T.purple, fontWeight: 700, marginTop: 12, marginBottom: 4, borderBottom: `1px solid ${T.cardBorder}`, paddingBottom: 4 }}>v10 EXECUTION</div>
              {[['ORDER_TYPE', 'FAK'], ['MIN_EVAL', `T-${V10_MIN_EVAL_OFFSET}`], ['CAP_SOURCE', 'DUNE P-5pp'], ['PI_BONUS', '+3.14c']].map(([label, val]) => (
                <div key={label} style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', fontSize: 12, fontFamily: 'monospace' }}>
                  <span style={{ color: T.textMuted }}>{label}</span>
                  <span style={{ color: T.text }}>{val}</span>
                </div>
              ))}
            </div>
          </Panel>

          <Panel title="Execution Log (trades)" icon={Server} style={{ flexShrink: 0, height: 140 }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 9, fontFamily: "'JetBrains Mono', monospace", overflowY: 'auto', paddingRight: 4 }}>
              <div style={{ display: 'grid', gridTemplateColumns: '48px 32px 32px 48px 56px', gap: 4, color: T.textMuted, borderBottom: `1px solid ${T.cardBorder}`, paddingBottom: 4 }}>
                <span>ID</span><span>DIR</span><span>TYPE</span><span>FILL</span><span>PNL</span>
              </div>
              {recentTrades.slice(0, 6).map((t, i) => {
                // Find matching window for this trade to get order_type
                const matchW = hqData?.windows?.find(w => w.trade_placed && Math.abs(new Date(w.window_ts) - new Date(t.created_at)) < 600000);
                const orderType = matchW?.order_type || 'GTC';
                return (
                  <div key={t.id || i} style={{ display: 'grid', gridTemplateColumns: '48px 32px 32px 48px 56px', gap: 4, color: T.text }}>
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>#{String(t.id).slice(-4)}</span>
                    <span style={{ color: t.direction === 'UP' || t.direction === 'YES' ? T.green : T.red }}>{t.direction === 'YES' ? 'UP' : t.direction === 'NO' ? 'DN' : (t.direction || '--').slice(0, 2)}</span>
                    <span style={{ color: orderType === 'FAK' ? T.purple : T.textMuted }}>{orderType}</span>
                    <span>{t.entry_price?.toFixed(2) ?? '\u2014'}</span>
                    <span style={{ color: (t.pnl_usd || 0) >= 0 ? T.green : T.red, textAlign: 'right' }}>
                      {t.pnl_usd != null ? `${t.pnl_usd >= 0 ? '+' : ''}$${t.pnl_usd.toFixed(2)}` : '\u2014'}
                    </span>
                  </div>
                );
              })}
              {recentTrades.length === 0 && (
                <div style={{ color: T.textDim, textAlign: 'center', padding: 8 }}>No recent trades</div>
              )}
            </div>
          </Panel>
        </div>
      ) : (
        <div
          onClick={() => setRightExpanded(true)}
          style={{
            gridColumn: 'span 1', gridRow: 'span 6', display: 'flex', flexDirection: 'column',
            gap: 8, minHeight: 0, cursor: 'pointer',
          }}
        >
          <Panel style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 24, opacity: 0.5 }}>
              <ChevronLeft size={18} style={{ color: T.cyan }} />
              <div style={{
                color: T.cyan, fontFamily: 'monospace', fontSize: 10, letterSpacing: '0.1em', whiteSpace: 'nowrap',
                writingMode: 'vertical-rl', transform: 'rotate(180deg)',
              }}>AI GATEKEEPER & CONFIG</div>
            </div>
          </Panel>
        </div>
      )}
      </div>
    </div>
  );
}
