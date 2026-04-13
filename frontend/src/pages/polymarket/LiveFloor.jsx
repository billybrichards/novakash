import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useApi } from '../../hooks/useApi.js';
import { T, fmt, utcHHMM } from './components/theme.js';
import WindowAnalysisModal from './components/WindowAnalysisModal.jsx';
import { STRATEGIES } from '../../constants/strategies.js';

/**
 * LiveFloor -- Active trading view with live price chart,
 * strategy decisions, and recent windows.
 *
 * Replaces the old Factory Floor as the primary "live trading" page.
 *
 * Sections:
 *   1. Live Price Chart (last 5 min = current window, SVG)
 *   2. Active Strategy Decisions (V10 + V4 side-by-side)
 *   3. Recent Windows Table (last 20 resolved)
 *
 * Polls 4 endpoints every 10s.
 */

// ── Inject keyframes ────────────────────────────────────────────────────────
if (typeof document !== 'undefined' && !document.getElementById('pm-floor-styles')) {
  const style = document.createElement('style');
  style.id = 'pm-floor-styles';
  style.textContent = `
    @keyframes floor-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
    @keyframes floor-fade { from { opacity: 0; } to { opacity: 1; } }
  `;
  document.head.appendChild(style);
}

// ── Styles ──────────────────────────────────────────────────────────────────

const S = {
  page: {
    minHeight: '100vh', background: T.bg, color: T.text,
    padding: '16px 20px', fontFamily: T.mono,
    display: 'flex', flexDirection: 'column', gap: 16,
  },
  pageTitle: {
    fontSize: 16, fontWeight: 800, color: T.text,
    display: 'flex', alignItems: 'center', gap: 10,
  },
  liveDot: {
    width: 8, height: 8, borderRadius: '50%', background: T.green,
    animation: 'floor-pulse 2s infinite',
  },
  card: {
    background: T.card, border: `1px solid ${T.cardBorder}`,
    borderRadius: 6, padding: 14,
  },
  cardTitle: {
    fontSize: 10, fontWeight: 700, color: T.cyan,
    letterSpacing: '0.06em', textTransform: 'uppercase',
    marginBottom: 10, fontFamily: T.mono,
  },
  row: {
    display: 'flex', gap: 12, flexWrap: 'wrap',
  },
  stratCard: (isLive) => ({
    flex: '1 1 300px',
    background: T.card, border: `1px solid ${isLive ? T.cyan : T.purple}`,
    borderRadius: 6, padding: 14, minWidth: 280,
  }),
  stratLabel: (color) => ({
    fontSize: 9, fontWeight: 700, color,
    letterSpacing: '0.06em', textTransform: 'uppercase',
    marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6,
  }),
  modeBadge: (color) => ({
    display: 'inline-block', padding: '1px 6px', borderRadius: 3,
    fontSize: 8, fontWeight: 700, background: `${color}22`, color,
  }),
  actionPill: (action) => ({
    display: 'inline-block', padding: '3px 12px', borderRadius: 4,
    fontSize: 14, fontWeight: 800,
    background: action === 'TRADE' ? 'rgba(16,185,129,0.15)' : 'rgba(71,85,105,0.15)',
    color: action === 'TRADE' ? T.green : T.textMuted,
    fontFamily: T.mono,
  }),
  metaRow: {
    display: 'flex', gap: 16, marginTop: 6, flexWrap: 'wrap',
  },
  metaItem: {
    fontSize: 9, color: T.textMuted, fontFamily: T.mono,
  },
  metaValue: {
    color: T.text, fontWeight: 600,
  },
  td: {
    padding: '5px 8px', fontSize: 10, fontFamily: T.mono,
    borderBottom: `1px solid ${T.border}`, whiteSpace: 'nowrap',
  },
  th: {
    padding: '5px 8px', fontSize: 9, fontFamily: T.mono,
    borderBottom: `1px solid ${T.border}`, whiteSpace: 'nowrap',
    color: T.textMuted, fontWeight: 700, letterSpacing: '0.05em',
    textTransform: 'uppercase', position: 'sticky', top: 0,
    background: T.headerBg, zIndex: 1,
  },
  pill: (bg, color) => ({
    display: 'inline-block', padding: '1px 6px', borderRadius: 3,
    fontSize: 9, fontWeight: 700, background: bg, color,
    fontFamily: T.mono,
  }),
};

// ── Helpers ──────────────────────────────────────────────────────────────────

function dirColor(dir) {
  if (!dir) return T.textDim;
  return dir === 'UP' ? T.green : T.red;
}

function pctStr(v) {
  if (v == null) return '--';
  return (v * 100).toFixed(3) + '%';
}

function fmtCountdown(sec) {
  if (sec == null || sec < 0) return '--:--';
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

function epochToHHMM(ts) {
  if (!ts) return '--';
  const d = new Date(typeof ts === 'number' ? ts * 1000 : ts);
  return d.toISOString().slice(11, 16);
}

// ── Live Price Chart (SVG) ──────────────────────────────────────────────────

function PriceChart({ hqData, v4Snapshot }) {
  // Extract price history from hq data
  const prices = [];

  // Use the most recent window's open/close prices.
  // gate_heartbeat is an array (not an object) so avoid accessing .binance_price on it.
  const latestWindow = hqData?.windows?.[0];
  const currentPrice = latestWindow?.close_price
    || v4Snapshot?.timescales?.['5m']?.binance_price;
  const openPrice = latestWindow?.open_price
    || hqData?.current_window?.open_price;

  // Build price array from recent evaluations if available
  const recentEvals = hqData?.recent_evaluations || [];
  for (const ev of recentEvals) {
    const p = ev.binance_price || ev.chainlink_price;
    const offset = ev.eval_offset;
    if (p && offset != null) {
      prices.push({ offset, price: p });
    }
  }

  // If we have current price but no history, show a single point
  if (prices.length === 0 && currentPrice) {
    prices.push({ offset: 0, price: currentPrice });
  }

  if (prices.length === 0) {
    return (
      <div style={{ textAlign: 'center', padding: 30, color: T.textDim, fontSize: 11 }}>
        No price data available
      </div>
    );
  }

  prices.sort((a, b) => b.offset - a.offset);

  const W = 800, H = 180;
  const padL = 70, padR = 15, padT = 15, padB = 25;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;

  const allPrices = prices.map(p => p.price);
  if (openPrice) allPrices.push(openPrice);
  const minP = Math.min(...allPrices);
  const maxP = Math.max(...allPrices);
  const range = maxP - minP || 1;

  const maxOffset = Math.max(...prices.map(p => p.offset));
  const offsetRange = maxOffset || 300;

  const toX = (offset) => padL + ((maxOffset - offset) / offsetRange) * plotW;
  const toY = (price) => padT + plotH - ((price - minP) / range) * plotH;

  const linePath = prices.map((p, i) =>
    `${i === 0 ? 'M' : 'L'}${toX(p.offset).toFixed(1)},${toY(p.price).toFixed(1)}`
  ).join(' ');

  const delta = openPrice && currentPrice ? ((currentPrice - openPrice) / openPrice) : null;

  return (
    <div>
      {/* Delta display */}
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, marginBottom: 8 }}>
        {currentPrice && (
          <span style={{ fontSize: 22, fontWeight: 800, color: T.text, fontFamily: T.mono }}>
            ${currentPrice.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </span>
        )}
        {delta != null && (
          <span style={{
            fontSize: 14, fontWeight: 700, fontFamily: T.mono,
            color: delta > 0 ? T.green : delta < 0 ? T.red : T.textMuted,
          }}>
            {delta > 0 ? '+' : ''}{(delta * 100).toFixed(4)}%
          </span>
        )}
      </div>

      <svg width={W} height={H} style={{ display: 'block', maxWidth: '100%' }}
        viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet">
        {/* Open price dashed line */}
        {openPrice && (
          <>
            <line x1={padL} y1={toY(openPrice)} x2={W - padR} y2={toY(openPrice)}
              stroke={T.amber} strokeWidth={1} strokeDasharray="6,4" opacity={0.6} />
            <text x={padL - 4} y={toY(openPrice) + 3}
              fill={T.amber} fontSize={8} textAnchor="end" fontFamily={T.mono}>
              Open
            </text>
          </>
        )}

        {/* Price line */}
        <path d={linePath} fill="none" stroke={T.cyan} strokeWidth={2} />

        {/* Current price dot */}
        {prices.length > 0 && (
          <circle
            cx={toX(prices[prices.length - 1].offset)}
            cy={toY(prices[prices.length - 1].price)}
            r={4} fill={T.cyan} stroke="#000" strokeWidth={1}
          />
        )}

        {/* Y-axis price labels */}
        {[minP, (minP + maxP) / 2, maxP].map((v, i) => (
          <text key={i} x={padL - 4} y={toY(v) + 3}
            fill={T.textDim} fontSize={8} textAnchor="end" fontFamily={T.mono}>
            ${v.toFixed(0)}
          </text>
        ))}

        {/* X-axis time labels */}
        {[300, 240, 180, 120, 60, 0].filter(v => v <= offsetRange).map(v => (
          <text key={v} x={toX(v)} y={H - 6}
            fill={T.textDim} fontSize={8} textAnchor="middle" fontFamily={T.mono}>
            T-{v}
          </text>
        ))}
      </svg>
    </div>
  );
}

// ── Main Component ──────────────────────────────────────────────────────────

export default function LiveFloor() {
  const api = useApi();

  const [hqData, setHqData] = useState(null);
  const [outcomes, setOutcomes] = useState([]);
  const [decisions, setDecisions] = useState([]);
  const [v4Snapshot, setV4Snapshot] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Modal state
  const [analysisWindow, setAnalysisWindow] = useState(null);

  // Page title
  useEffect(() => {
    const prev = document.title;
    document.title = 'Floor \u2014 Polymarket \u2014 Novakash';
    return () => { document.title = prev; };
  }, []);

  // Fetch data
  const fetchData = useCallback(async () => {
    try {
      const results = await Promise.allSettled([
        api('GET', '/v58/execution-hq?asset=btc&timeframe=5m'),
        api('GET', '/v58/outcomes?limit=20'),
        api('GET', '/v58/strategy-decisions?limit=40'),
        api('GET', '/v4/snapshot?asset=btc'),
      ]);

      const [hqRes, outRes, decRes, v4Res] = results;

      if (hqRes.status === 'fulfilled') setHqData(hqRes.value?.data || hqRes.value);
      if (outRes.status === 'fulfilled') {
        const d = outRes.value?.data || outRes.value;
        setOutcomes(d?.outcomes ?? (Array.isArray(d) ? d : []));
      }
      if (decRes.status === 'fulfilled') {
        const d = decRes.value?.data || decRes.value;
        setDecisions(d?.decisions ?? (Array.isArray(d) ? d : []));
      }
      if (v4Res.status === 'fulfilled') setV4Snapshot(v4Res.value?.data || v4Res.value);

      setError(null);
    } catch (err) {
      setError(err.message || 'Fetch error');
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => { fetchData(); }, [fetchData]);
  useEffect(() => {
    const iv = setInterval(fetchData, 10000);
    return () => clearInterval(iv);
  }, [fetchData]);

  // Derive current window info
  const cw = hqData?.current_window;
  const gh = hqData?.gate_heartbeat;
  const countdown = gh?.seconds_remaining ?? cw?.seconds_remaining;

  // Group decisions by window_ts — prefer sweet-spot eval_offset (90-150)
  const decisionMap = {};
  for (const d of decisions) {
    const key = d.window_ts;
    if (!decisionMap[key]) decisionMap[key] = {};
    const existing = decisionMap[key][d.strategy_id];
    if (!existing) {
      decisionMap[key][d.strategy_id] = d;
    } else {
      // Prefer offset in sweet spot (90-150), then closest to 120
      const inSweet = (o) => o >= 90 && o <= 150;
      const eNew = d.eval_offset || 0;
      const eOld = existing.eval_offset || 0;
      if (inSweet(eNew) && !inSweet(eOld)) {
        decisionMap[key][d.strategy_id] = d;
      } else if (inSweet(eNew) && inSweet(eOld) && Math.abs(eNew - 120) < Math.abs(eOld - 120)) {
        decisionMap[key][d.strategy_id] = d;
      }
    }
  }

  // Latest V10 + V4 decisions (most recent window with data)
  const latestDecisionTs = decisions.length > 0
    ? decisions.reduce((max, d) => Math.max(max, d.window_ts || 0), 0)
    : null;
  const latestV10 = latestDecisionTs ? decisionMap[latestDecisionTs]?.['v10_gate'] : null;
  const latestV4 = latestDecisionTs ? decisionMap[latestDecisionTs]?.['v4_fusion'] : null;
  const latestDown = latestDecisionTs ? decisionMap[latestDecisionTs]?.['v4_down_only'] : null;
  const latestUpAsian = latestDecisionTs ? decisionMap[latestDecisionTs]?.['v4_up_asian'] : null;

  return (
    <div style={S.page}>
      {/* Page title */}
      <div style={S.pageTitle}>
        <div style={S.liveDot} />
        Live Floor
        {countdown != null && (
          <span style={{
            fontSize: 12, fontWeight: 600, color: countdown < 30 ? T.amber : T.textMuted,
            marginLeft: 8,
          }}>
            {fmtCountdown(Math.round(countdown))} remaining
          </span>
        )}
      </div>

      {loading && (
        <div style={{ textAlign: 'center', padding: 40, color: T.textMuted }}>Loading...</div>
      )}
      {error && (
        <div style={{ textAlign: 'center', padding: 20, color: T.red, fontSize: 11 }}>{error}</div>
      )}

      {!loading && (
        <>
          {/* 1. Live Price Chart */}
          <div style={S.card}>
            <div style={S.cardTitle}>BTC Price (Current Window)</div>
            <PriceChart hqData={hqData} v4Snapshot={v4Snapshot} />
            {cw && (
              <div style={{ display: 'flex', gap: 16, marginTop: 8, flexWrap: 'wrap' }}>
                <span style={S.metaItem}>
                  Window: <span style={S.metaValue}>{epochToHHMM(cw.window_ts)}</span>
                </span>
                <span style={S.metaItem}>
                  Asset: <span style={S.metaValue}>{cw.asset || 'BTC'}</span>
                </span>
                <span style={S.metaItem}>
                  Timeframe: <span style={S.metaValue}>{cw.timeframe || '5m'}</span>
                </span>
                {cw.open_price && (
                  <span style={S.metaItem}>
                    Open: <span style={S.metaValue}>${Number(cw.open_price).toLocaleString()}</span>
                  </span>
                )}
              </div>
            )}
          </div>

          {/* 2. Active Strategy Decisions — dynamic from shared constants */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 6, marginBottom: 8 }}>
            {[
              { sid: 'v4_down_only', data: latestDown },
              { sid: 'v4_up_asian', data: latestUpAsian },
              { sid: 'v4_fusion', data: latestV4 },
              { sid: 'v10_gate', data: latestV10 },
            ].map(({ sid, data: d }) => {
              const strat = STRATEGIES[sid] || {};
              const label = strat.label || sid;
              const color = strat.color || T.textMuted;
              const mode = strat.defaultMode || 'GHOST';
              return { label, data: d, color, mode, key: sid };
            }).map(({ label, data: d, color, mode, key }) => (
              <div key={label} style={{
                ...S.card,
                borderLeft: `3px solid ${d?.action === 'TRADE' ? color : 'rgba(51,65,85,0.5)'}`,
                padding: 10,
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
                  <span style={{ fontSize: 9, fontWeight: 700, color, fontFamily: T.mono }}>{label}</span>
                  <span style={{
                    fontSize: 7, padding: '1px 5px', borderRadius: 2, fontFamily: T.mono, fontWeight: 700,
                    background: mode === 'LIVE' ? 'rgba(16,185,129,0.15)' : 'rgba(168,85,247,0.12)',
                    color: mode === 'LIVE' ? '#10b981' : '#a855f7',
                  }}>{mode}</span>
                </div>
                {d ? (
                  <>
                    <div style={S.actionPill(d.action)}>{d.action || 'SKIP'}</div>
                    <div style={S.metaRow}>
                      <span style={S.metaItem}>
                        Dir: <span style={{ ...S.metaValue, color: dirColor(d.direction) }}>{d.direction || '--'}</span>
                      </span>
                      <span style={S.metaItem}>T-{d.eval_offset}</span>
                    </div>
                    {d.skip_reason && (
                      <div style={{ fontSize: 8, color: T.textMuted, marginTop: 3 }} title={d.skip_reason}>
                        {d.skip_reason.slice(0, 40)}
                      </div>
                    )}
                  </>
                ) : (
                  <div style={{ color: T.textDim, fontSize: 10 }}>No decision</div>
                )}
              </div>
            ))}
          </div>

          {/* 3. Recent Windows Table */}
          <div style={{ ...S.card, padding: 0 }}>
            <div style={{ ...S.cardTitle, padding: '14px 14px 0' }}>
              Recent Windows ({outcomes.length})
            </div>
            <div style={{ maxHeight: 500, overflowY: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead>
                  <tr>
                    <th style={S.th}>Window</th>
                    <th style={S.th}>Actual</th>
                    <th style={{ ...S.th, textAlign: 'center', color: STRATEGIES.v4_down_only.color }}>{STRATEGIES.v4_down_only.shortLabel}</th>
                    <th style={{ ...S.th, textAlign: 'center', color: STRATEGIES.v4_up_asian.color }}>{STRATEGIES.v4_up_asian.shortLabel}</th>
                    <th style={{ ...S.th, textAlign: 'center', color: STRATEGIES.v4_fusion.color }}>{STRATEGIES.v4_fusion.shortLabel}</th>
                    <th style={{ ...S.th, textAlign: 'center', color: STRATEGIES.v10_gate.color }}>{STRATEGIES.v10_gate.shortLabel}</th>
                    <th style={S.th}>Signal</th>
                    <th style={S.th}>Confidence</th>
                    <th style={S.th}>VPIN</th>
                  </tr>
                </thead>
                <tbody>
                  {outcomes.map((o, i) => {
                    const wts = (() => {
                      if (!o.window_ts) return 0;
                      const d = new Date(o.window_ts);
                      return isNaN(d) ? 0 : Math.floor(d.getTime() / 1000);
                    })();
                    const dm = decisionMap[wts] || {};

                    // Render a compact strategy cell: action + direction in one cell
                    const stratCell = (sid, color) => {
                      const d = dm[sid];
                      if (!d) return <td style={{ ...S.td, textAlign: 'center', color: T.textDim, fontSize: 9 }}>--</td>;
                      const isTrade = d.action === 'TRADE';
                      const dir = d.direction === 'UP' ? '\u2191' : d.direction === 'DOWN' ? '\u2193' : '';
                      const label = isTrade ? `TRADE${dir}` : `SKIP`;
                      return (
                        <td style={{ ...S.td, textAlign: 'center' }} title={d.skip_reason || d.entry_reason || ''}>
                          <span style={{
                            fontSize: 9, fontWeight: isTrade ? 700 : 400,
                            color: isTrade ? color : T.textDim,
                          }}>
                            {label}
                          </span>
                        </td>
                      );
                    };

                    return (
                      <tr key={o.window_ts || i}
                        onClick={() => wts && setAnalysisWindow(wts)}
                        style={{
                          background: i % 2 === 0 ? 'transparent' : 'rgba(15,23,42,0.3)',
                          cursor: wts ? 'pointer' : 'default',
                        }}
                        title={wts ? 'Click to analyze' : ''}
                      >
                        <td style={{ ...S.td, color: T.text }}>{o.window_ts ? utcHHMM(o.window_ts) : '--'}</td>
                        <td style={{ ...S.td, color: dirColor(o.actual_direction), fontWeight: 600 }}>{o.actual_direction || '--'}</td>
                        {stratCell('v4_down_only', STRATEGIES.v4_down_only.color)}
                        {stratCell('v4_up_asian', STRATEGIES.v4_up_asian.color)}
                        {stratCell('v4_fusion', STRATEGIES.v4_fusion.color)}
                        {stratCell('v10_gate', STRATEGIES.v10_gate.color)}
                        <td style={{ ...S.td, color: dirColor(o.direction), fontWeight: 600 }}>{o.direction || '--'}</td>
                        <td style={{ ...S.td, color: T.purple }}>{o.confidence != null ? fmt(o.confidence, 3) : '--'}</td>
                        <td style={{ ...S.td, color: (o.vpin || 0) >= 0.55 ? T.green : T.text }}>{o.vpin != null ? fmt(o.vpin, 3) : '--'}</td>
                      </tr>
                    );
                  })}
                  {outcomes.length === 0 && (
                    <tr>
                      <td colSpan={10} style={{ ...S.td, textAlign: 'center', color: T.textDim, padding: 20 }}>
                        No recent windows
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}

      {/* Window Analysis Modal */}
      <WindowAnalysisModal
        windowTs={analysisWindow}
        onClose={() => setAnalysisWindow(null)}
      />
    </div>
  );
}
