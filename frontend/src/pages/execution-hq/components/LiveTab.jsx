import React, { useState, useEffect, useRef } from 'react';
import { Activity, Crosshair, BarChart2, Database, Radio, ShieldCheck, Sliders, Server, AlertTriangle, Zap, ChevronLeft, ChevronRight, Plus } from 'lucide-react';
import Panel from './Panel.jsx';
import ContinuousFeed from './ContinuousFeed.jsx';
import CanvasPriceChart from './CanvasPriceChart.jsx';
import CanvasRiskSurface from './CanvasRiskSurface.jsx';
import GateAuditMatrix from './GateAuditMatrix.jsx';
import { getEntryCap, T } from './constants.js';

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
export default function LiveTab({ hqData, tick }) {
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

  const currentCap = getEntryCap(currentT);
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
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginTop: 12 }}>
              <div style={{ background: 'rgba(30,41,59,0.5)', padding: 8, borderRadius: 4, border: `1px solid ${T.cardBorder}50`, textAlign: 'center' }}>
                <div style={{ fontSize: 9, color: T.textMuted, fontFamily: 'monospace' }}>ENTRY CAP</div>
                <div style={{ fontSize: 18, fontFamily: 'monospace', color: T.amber }}>${currentCap.toFixed(2)}</div>
              </div>
              <div style={{ background: 'rgba(30,41,59,0.5)', padding: 8, borderRadius: 4, border: `1px solid ${T.cardBorder}50`, textAlign: 'center' }}>
                <div style={{ fontSize: 9, color: T.textMuted, fontFamily: 'monospace' }}>V2.2 DIRECTIVE</div>
                <div style={{ fontSize: 18, fontFamily: 'monospace', color: T.green }}>UP</div>
              </div>
            </div>
            <div style={{
              marginTop: 12, background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.2)',
              padding: 8, borderRadius: 4, fontSize: 10, fontFamily: 'monospace', color: '#fca5a5',
              display: 'flex', alignItems: 'flex-start', gap: 8,
            }}>
              <AlertTriangle size={12} style={{ flexShrink: 0, marginTop: 2 }} />
              <span>MACRO OBSERVER: Polling active. Bias updates every 60s.</span>
            </div>
          </Panel>

          <Panel title="6 Continuous Feeds" icon={Radio} style={{ flex: 1, minHeight: 0 }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4, overflowY: 'auto', paddingRight: 4 }}>
              <ContinuousFeed name="Chainlink" hz="5s" latency={120 + (tick % 3) * 10} val={(0.64 + Math.sin(tick) * 0.01).toFixed(4)} change={0.12} status="ok" />
              <ContinuousFeed name="Tiingo" hz="2s" latency={45} val={(0.642 + Math.cos(tick) * 0.01).toFixed(4)} change={0.15} status="ok" />
              <ContinuousFeed name="CLOB (Poly)" hz="10s" latency={350} val="0.6350" change={-0.05} status={currentT === 70 ? 'warn' : 'ok'} />
              <ContinuousFeed name="Binance" hz="~1Hz" latency={12} val={(0.641 + Math.sin(tick * 2) * 0.02).toFixed(4)} change={0.14} status="ok" />
              <ContinuousFeed name="CoinGlass" hz="15s" latency={800} val="OI: +2.4%" change={2.4} status="warn" />
              <ContinuousFeed name="TimesFM v2" hz="1s" latency={85} val="PRED: 0.68" change={5.2} status="ok" />
            </div>
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
        <Panel title="gate_audit — 19 Checkpoints (T-240 to T-60)" icon={Database} style={{ flex: 1.5, minHeight: 0 }}>
          <GateAuditMatrix currentT={currentT} />
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

        <Panel title="Risk Surface & ODE Parametrization (VPIN x Delta)" icon={Activity} style={{ flex: 1.5, minHeight: 0 }}>
          <div style={{ position: 'relative', width: '100%', height: '100%' }}>
            <CanvasRiskSurface currentT={currentT} />
            <div style={{
              position: 'absolute', top: 8, right: 8, background: 'rgba(15,23,42,0.8)',
              border: `1px solid ${T.cardBorder}`, padding: 8, borderRadius: 4, fontSize: 9, fontFamily: 'monospace',
            }}>
              <div style={{ color: T.cyan, marginBottom: 4 }}>SURFACE_VARS</div>
              <div>Z: {(0.45 + Math.sin(tick * 0.1) * 0.1).toFixed(3)} (VPIN)</div>
              <div>X: {(0.01 + Math.cos(tick) * 0.01).toFixed(4)} (delta)</div>
              <div>OPT: LOCAL_MIN</div>
            </div>
          </div>
        </Panel>
      </div>

      {/* RIGHT COLUMN */}
      {rightExpanded ? (
        <div style={{ gridColumn: 'span 3', gridRow: 'span 6', display: 'flex', flexDirection: 'column', gap: 8, minHeight: 0 }}>
          <Panel
            title="v2.2 AI — THE GATEKEEPER"
            icon={ShieldCheck}
            style={{ flexShrink: 0, background: 'linear-gradient(to bottom, rgba(15,23,42,1), rgba(6,182,212,0.05))', borderColor: 'rgba(6,182,212,0.3)' }}
            headerRight={
              <button onClick={() => setRightExpanded(false)} style={{ background: 'none', border: 'none', color: T.textMuted, cursor: 'pointer' }}>
                <ChevronRight size={14} />
              </button>
            }
          >
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end' }}>
                <span style={{ fontSize: 10, fontFamily: 'monospace', color: T.cyan }}>v2_probability_up</span>
                <span style={{ fontSize: 28, fontFamily: 'monospace', fontWeight: 700, color: T.cyan }}>87.4%</span>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 4, fontSize: 10, fontFamily: 'monospace' }}>
                {[
                  ['Direction', 'LONG', T.green],
                  ['Agrees', 'TRUE', T.green],
                  ['TWAP_gamma', '0.92', T.cyan],
                  ['CG_Funding', '0.01%', T.amber],
                ].map(([label, val, color]) => (
                  <div key={label} style={{ background: 'rgba(30,41,59,1)', padding: 6, borderRadius: 4, display: 'flex', justifyContent: 'space-between' }}>
                    <span style={{ color: T.textMuted }}>{label}</span>
                    <span style={{ color }}>{val}</span>
                  </div>
                ))}
              </div>
            </div>
          </Panel>

          <Panel title="Configuration Toggles" icon={Sliders} style={{ flex: 1, minHeight: 0 }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 2, overflowY: 'auto', paddingRight: 4 }}>
              <div style={{ fontSize: 9, color: T.cyan, fontWeight: 700, marginBottom: 4, borderBottom: `1px solid ${T.cardBorder}`, paddingBottom: 4 }}>VPIN THRESHOLDS</div>
              {[['VPIN_GATE', '0.45'], ['VPIN_CASCADE', '0.70'], ['VPIN_INFORMED', '0.55']].map(([label, val]) => (
                <div key={label} style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', fontSize: 12, fontFamily: 'monospace' }}>
                  <span style={{ color: T.textMuted }}>{label}</span>
                  <span style={{ color: T.text }}>{val}</span>
                </div>
              ))}
              <div style={{ fontSize: 9, color: T.cyan, fontWeight: 700, marginTop: 12, marginBottom: 4, borderBottom: `1px solid ${T.cardBorder}`, paddingBottom: 4 }}>EXECUTION / FOK</div>
              {[['FOK_BUMP_STEP', '$0.02'], ['FOK_MAX_ATTEMPT', '3'], ['PRICING_MODE', 'CAP']].map(([label, val]) => (
                <div key={label} style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', fontSize: 12, fontFamily: 'monospace' }}>
                  <span style={{ color: T.textMuted }}>{label}</span>
                  <span style={{ color: T.text }}>{val}</span>
                </div>
              ))}
            </div>
          </Panel>

          <Panel title="Execution Log (trades)" icon={Server} style={{ flexShrink: 0, height: 120 }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 9, fontFamily: 'monospace', overflowY: 'auto', paddingRight: 4 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', color: T.textMuted, borderBottom: `1px solid ${T.cardBorder}`, paddingBottom: 4 }}>
                <span>ORDER_ID</span><span>DIR</span><span>FILL</span><span>PNL</span>
              </div>
              {recentTrades.slice(0, 5).map((t, i) => (
                <div key={t.id || i} style={{ display: 'flex', justifyContent: 'space-between', color: T.text }}>
                  <span>#{String(t.id).slice(-4)}...</span>
                  <span style={{ color: t.direction === 'UP' || t.direction === 'YES' ? T.green : T.red }}>{t.direction}</span>
                  <span>{t.entry_price?.toFixed(2) ?? '—'}</span>
                  <span style={{ color: (t.pnl_usd || 0) >= 0 ? T.green : T.red }}>
                    {t.pnl_usd != null ? `${t.pnl_usd >= 0 ? '+' : ''}$${t.pnl_usd.toFixed(2)}` : '—'}
                  </span>
                </div>
              ))}
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
  );
}
