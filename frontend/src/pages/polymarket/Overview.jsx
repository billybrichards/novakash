import React, { useState, useEffect, useCallback } from 'react';
import { useApi } from '../../hooks/useApi.js';
import { useTheme } from '../../contexts/ThemeContext.jsx';
import { getTheme, fmt, utcHHMM, pct } from './components/theme.js';

/**
 * Polymarket Overview — Prediction-at-time vs Outcome Surface
 *
 * Shows strategy performance across eval_offsets:
 *   1. Strategy Performance Cards — V10 / V4 summary
 *   2. Prediction Accuracy Surface — SVG line chart by eval_offset
 *   3. Strategy Decision Surface — per-strategy trade/win overlay
 *   4. Recent Windows Quick View — last 10 resolved windows
 */

const DAYS_OPTIONS = [1, 3, 7, 14, 30];

// ── SVG Line Chart ──────────────────────────────────────────────────────────

function AccuracyChart({ offsets, theme }) {
  if (!offsets || offsets.length === 0) {
    return (
      <div style={{ padding: 40, textAlign: 'center', color: theme.textMuted }}>
        No prediction data available
      </div>
    );
  }

  // Sort by offset descending (T-180 on left, T-10 on right)
  const sorted = [...offsets].sort((a, b) => b.offset - a.offset);

  const W = 700, H = 280, PAD = { top: 20, right: 30, bottom: 40, left: 50 };
  const chartW = W - PAD.left - PAD.right;
  const chartH = H - PAD.top - PAD.bottom;

  const minOffset = Math.min(...sorted.map(o => o.offset));
  const maxOffset = Math.max(...sorted.map(o => o.offset));
  const range = maxOffset - minOffset || 1;

  // Y axis: 40% to 100%
  const yMin = 40, yMax = 100;
  const yRange = yMax - yMin;

  const x = (offset) => PAD.left + ((maxOffset - offset) / range) * chartW;
  const y = (val) => PAD.top + ((yMax - val) / yRange) * chartH;

  // Accuracy line
  const accPoints = sorted
    .filter(o => o.accuracy_pct != null)
    .map(o => `${x(o.offset)},${y(o.accuracy_pct)}`);
  const accLine = accPoints.length > 1 ? `M${accPoints.join(' L')}` : '';

  // Confidence line (scaled to %)
  const confPoints = sorted
    .filter(o => o.avg_confidence != null)
    .map(o => `${x(o.offset)},${y(o.avg_confidence * 100)}`);
  const confLine = confPoints.length > 1 ? `M${confPoints.join(' L')}` : '';

  // 50% reference line
  const y50 = y(50);

  // Grid lines
  const gridLines = [50, 60, 70, 80, 90, 100];

  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', maxWidth: 700 }}>
      {/* Grid */}
      {gridLines.map(v => (
        <g key={v}>
          <line
            x1={PAD.left} y1={y(v)} x2={W - PAD.right} y2={y(v)}
            stroke={theme.border} strokeWidth={v === 50 ? 1 : 0.5}
            strokeDasharray={v === 50 ? 'none' : '3,3'}
          />
          <text x={PAD.left - 6} y={y(v) + 3} textAnchor="end"
            fill={theme.textMuted} fontSize={10} fontFamily={theme.mono}>
            {v}%
          </text>
        </g>
      ))}

      {/* X axis labels */}
      {sorted.filter((_, i) => i % 3 === 0).map(o => (
        <text key={o.offset} x={x(o.offset)} y={H - 8} textAnchor="middle"
          fill={theme.textMuted} fontSize={9} fontFamily={theme.mono}>
          T-{o.offset}
        </text>
      ))}

      {/* 50% baseline */}
      <line x1={PAD.left} y1={y50} x2={W - PAD.right} y2={y50}
        stroke={theme.red} strokeWidth={1} opacity={0.3} />

      {/* Confidence line */}
      {confLine && (
        <path d={confLine} fill="none" stroke={theme.purple} strokeWidth={1.5}
          opacity={0.5} strokeDasharray="4,3" />
      )}

      {/* Accuracy line */}
      {accLine && (
        <path d={accLine} fill="none" stroke={theme.cyan} strokeWidth={2} />
      )}

      {/* Accuracy dots */}
      {sorted.filter(o => o.accuracy_pct != null).map(o => (
        <circle key={o.offset} cx={x(o.offset)} cy={y(o.accuracy_pct)}
          r={3} fill={theme.cyan} stroke={theme.card} strokeWidth={1} />
      ))}

      {/* Legend */}
      <g transform={`translate(${PAD.left + 10}, ${PAD.top + 10})`}>
        <line x1={0} y1={0} x2={16} y2={0} stroke={theme.cyan} strokeWidth={2} />
        <text x={20} y={4} fill={theme.text} fontSize={10} fontFamily={theme.mono}>
          Accuracy
        </text>
        <line x1={80} y1={0} x2={96} y2={0} stroke={theme.purple} strokeWidth={1.5}
          strokeDasharray="4,3" opacity={0.5} />
        <text x={100} y={4} fill={theme.text} fontSize={10} fontFamily={theme.mono}>
          Confidence
        </text>
      </g>
    </svg>
  );
}

// ── Strategy Decision Chart ─────────────────────────────────────────────────

function StrategyDecisionChart({ offsets, strategyIds, theme }) {
  if (!offsets || offsets.length === 0 || strategyIds.length === 0) {
    return (
      <div style={{ padding: 40, textAlign: 'center', color: theme.textMuted }}>
        No strategy decision data available
      </div>
    );
  }

  const sorted = [...offsets].sort((a, b) => b.offset - a.offset);

  const W = 700, H = 260, PAD = { top: 20, right: 30, bottom: 40, left: 50 };
  const chartW = W - PAD.left - PAD.right;
  const chartH = H - PAD.top - PAD.bottom;

  const minOffset = Math.min(...sorted.map(o => o.offset));
  const maxOffset = Math.max(...sorted.map(o => o.offset));
  const range = maxOffset - minOffset || 1;

  const x = (offset) => PAD.left + ((maxOffset - offset) / range) * chartW;

  // For each strategy, compute max trades at any offset for Y scale
  const colors = { v10_gate: theme.green, v4_fusion: theme.amber };
  const labels = { v10_gate: 'V10', v4_fusion: 'V4' };

  let maxTrades = 1;
  for (const sid of strategyIds) {
    const prefix = sid.replace(/-/g, '_');
    for (const o of sorted) {
      const t = o[`${prefix}_trades`] || 0;
      if (t > maxTrades) maxTrades = t;
    }
  }

  const y = (val) => PAD.top + ((maxTrades - val) / maxTrades) * chartH;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', maxWidth: 700 }}>
      {/* Grid */}
      {[0, Math.round(maxTrades / 4), Math.round(maxTrades / 2),
        Math.round(maxTrades * 3 / 4), maxTrades].filter((v, i, a) => a.indexOf(v) === i).map(v => (
        <g key={v}>
          <line x1={PAD.left} y1={y(v)} x2={W - PAD.right} y2={y(v)}
            stroke={theme.border} strokeWidth={0.5} strokeDasharray="3,3" />
          <text x={PAD.left - 6} y={y(v) + 3} textAnchor="end"
            fill={theme.textMuted} fontSize={10} fontFamily={theme.mono}>
            {v}
          </text>
        </g>
      ))}

      {/* X axis labels */}
      {sorted.filter((_, i) => i % 3 === 0).map(o => (
        <text key={o.offset} x={x(o.offset)} y={H - 8} textAnchor="middle"
          fill={theme.textMuted} fontSize={9} fontFamily={theme.mono}>
          T-{o.offset}
        </text>
      ))}

      {/* Strategy lines — trades count as bars */}
      {strategyIds.map((sid, idx) => {
        const prefix = sid.replace(/-/g, '_');
        const color = colors[sid] || (idx === 0 ? theme.green : theme.amber);
        const points = sorted
          .filter(o => (o[`${prefix}_trades`] || 0) > 0)
          .map(o => `${x(o.offset)},${y(o[`${prefix}_trades`] || 0)}`);
        const line = points.length > 1 ? `M${points.join(' L')}` : '';

        return (
          <g key={sid}>
            {line && <path d={line} fill="none" stroke={color} strokeWidth={2} />}
            {sorted.filter(o => (o[`${prefix}_trades`] || 0) > 0).map(o => {
              const trades = o[`${prefix}_trades`] || 0;
              const wr = o[`${prefix}_wr_pct`] || 0;
              return (
                <circle key={o.offset} cx={x(o.offset)} cy={y(trades)}
                  r={3} fill={wr >= 60 ? color : theme.red}
                  stroke={theme.card} strokeWidth={1}
                  opacity={wr >= 50 ? 1 : 0.6} />
              );
            })}
          </g>
        );
      })}

      {/* Legend */}
      <g transform={`translate(${PAD.left + 10}, ${PAD.top + 10})`}>
        {strategyIds.map((sid, idx) => {
          const color = colors[sid] || (idx === 0 ? theme.green : theme.amber);
          const label = labels[sid] || sid;
          return (
            <g key={sid} transform={`translate(${idx * 80}, 0)`}>
              <line x1={0} y1={0} x2={16} y2={0} stroke={color} strokeWidth={2} />
              <text x={20} y={4} fill={theme.text} fontSize={10} fontFamily={theme.mono}>
                {label} trades
              </text>
            </g>
          );
        })}
      </g>
    </svg>
  );
}

// ── Card Helpers ─────────────────────────────────────────────────────────────

function Card({ title, badge, theme, children }) {
  return (
    <div style={{
      background: theme.card,
      border: `1px solid ${theme.cardBorder}`,
      borderRadius: 10,
      overflow: 'hidden',
    }}>
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '10px 16px',
        borderBottom: `1px solid ${theme.border}`,
        background: theme.headerBg,
      }}>
        <span style={{
          color: theme.cyan, fontSize: 10, fontWeight: 700,
          fontFamily: theme.mono, letterSpacing: '0.08em',
        }}>{title}</span>
        {badge}
      </div>
      <div style={{ padding: '12px 16px' }}>
        {children}
      </div>
    </div>
  );
}

function StatBlock({ label, value, sub, color, theme }) {
  return (
    <div style={{ textAlign: 'center' }}>
      <div style={{
        color: theme.textMuted, fontSize: 9, fontFamily: theme.mono,
        letterSpacing: '0.06em', marginBottom: 4,
      }}>{label}</div>
      <div style={{
        color: color || theme.text, fontSize: 20, fontWeight: 700,
        fontFamily: theme.mono, lineHeight: 1,
      }}>{value}</div>
      {sub && (
        <div style={{
          color: theme.textDim, fontSize: 9, fontFamily: theme.mono, marginTop: 3,
        }}>{sub}</div>
      )}
    </div>
  );
}

function ModeBadge({ mode, theme }) {
  const isLive = mode === 'LIVE';
  return (
    <span style={{
      display: 'inline-block',
      padding: '2px 8px',
      borderRadius: 4,
      fontSize: 9,
      fontWeight: 700,
      fontFamily: theme.mono,
      letterSpacing: '0.08em',
      background: isLive ? 'rgba(239, 68, 68, 0.15)' : 'rgba(6, 182, 212, 0.15)',
      color: isLive ? theme.red : theme.cyan,
      border: `1px solid ${isLive ? 'rgba(239, 68, 68, 0.3)' : 'rgba(6, 182, 212, 0.3)'}`,
    }}>
      {mode}
    </span>
  );
}

// ── Strategy Performance Card ───────────────────────────────────────────────

function StrategyCard({ strategyId, data, theme }) {
  const label = strategyId === 'v10_gate' ? 'V10 Gate' : strategyId === 'v4_fusion' ? 'V4 Fusion' : strategyId;
  const mode = strategyId === 'v10_gate' ? 'LIVE' : 'GHOST';
  const wins = data?.wins || 0;
  const losses = data?.losses || 0;
  const total = data?.total_trades || 0;
  const wr = data?.wr_pct || 0;

  return (
    <div style={{
      flex: '1 1 280px',
      background: theme.card,
      border: `1px solid ${theme.cardBorder}`,
      borderRadius: 10,
      padding: 16,
    }}>
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        marginBottom: 12,
      }}>
        <span style={{
          color: theme.text, fontSize: 14, fontWeight: 700, fontFamily: theme.mono,
        }}>{label}</span>
        <ModeBadge mode={mode} theme={theme} />
      </div>
      <div style={{ display: 'flex', gap: 20, justifyContent: 'space-around' }}>
        <StatBlock label="TRADES" value={total} theme={theme} />
        <StatBlock label="W / L" value={`${wins} / ${losses}`}
          color={wins > losses ? theme.green : wins < losses ? theme.red : theme.text}
          theme={theme} />
        <StatBlock label="WIN RATE" value={`${wr}%`}
          color={wr >= 60 ? theme.green : wr >= 50 ? theme.amber : theme.red}
          theme={theme} />
      </div>
    </div>
  );
}

// ── Recent Window Card ──────────────────────────────────────────────────────

function RecentWindowCard({ win, theme }) {
  const ts = new Date(win.window_ts * 1000);
  const timeStr = ts.toISOString().slice(11, 16);
  const dateStr = ts.toISOString().slice(5, 10);
  const isWin = win.outcome === 'WIN';

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 12,
      padding: '8px 12px',
      borderRadius: 6,
      background: isWin ? 'rgba(16, 185, 129, 0.06)' : 'rgba(239, 68, 68, 0.06)',
      border: `1px solid ${isWin ? 'rgba(16, 185, 129, 0.15)' : 'rgba(239, 68, 68, 0.15)'}`,
    }}>
      {/* Outcome badge */}
      <div style={{
        width: 44, textAlign: 'center',
        padding: '3px 0', borderRadius: 4,
        fontSize: 10, fontWeight: 700, fontFamily: theme.mono,
        background: isWin ? 'rgba(16, 185, 129, 0.15)' : 'rgba(239, 68, 68, 0.15)',
        color: isWin ? theme.green : theme.red,
      }}>
        {win.outcome}
      </div>

      {/* Time */}
      <div style={{ minWidth: 60 }}>
        <div style={{ color: theme.text, fontSize: 12, fontFamily: theme.mono, fontWeight: 600 }}>
          {timeStr}
        </div>
        <div style={{ color: theme.textDim, fontSize: 9, fontFamily: theme.mono }}>
          {dateStr}
        </div>
      </div>

      {/* Direction at T-120 */}
      <div style={{ minWidth: 50, textAlign: 'center' }}>
        <div style={{ color: theme.textMuted, fontSize: 8, fontFamily: theme.mono, marginBottom: 2 }}>
          T-120
        </div>
        <div style={{
          color: win.direction_at_t120 === 'UP' ? theme.green
               : win.direction_at_t120 === 'DOWN' ? theme.red
               : theme.textDim,
          fontSize: 11, fontFamily: theme.mono, fontWeight: 600,
        }}>
          {win.direction_at_t120 || '--'}
        </div>
      </div>

      {/* Actual direction */}
      <div style={{ minWidth: 50, textAlign: 'center' }}>
        <div style={{ color: theme.textMuted, fontSize: 8, fontFamily: theme.mono, marginBottom: 2 }}>
          ACTUAL
        </div>
        <div style={{
          color: win.actual_direction === 'UP' ? theme.green
               : win.actual_direction === 'DOWN' ? theme.red
               : theme.textDim,
          fontSize: 11, fontFamily: theme.mono, fontWeight: 600,
        }}>
          {win.actual_direction || '--'}
        </div>
      </div>

      {/* Strategy decisions */}
      <div style={{ flex: 1, display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
        <span style={{
          padding: '2px 6px', borderRadius: 3, fontSize: 9,
          fontFamily: theme.mono, fontWeight: 600,
          background: win.v10_decision === 'TRADE' ? 'rgba(16, 185, 129, 0.12)' : 'rgba(100, 116, 139, 0.1)',
          color: win.v10_decision === 'TRADE' ? theme.green : theme.textDim,
        }}>
          V10: {win.v10_decision}
        </span>
        <span style={{
          padding: '2px 6px', borderRadius: 3, fontSize: 9,
          fontFamily: theme.mono, fontWeight: 600,
          background: win.v4_decision === 'TRADE' ? 'rgba(245, 158, 11, 0.12)' : 'rgba(100, 116, 139, 0.1)',
          color: win.v4_decision === 'TRADE' ? theme.amber : theme.textDim,
        }}>
          V4: {win.v4_decision}
        </span>
      </div>
    </div>
  );
}

// ── Prediction Table ────────────────────────────────────────────────────────

function PredictionTable({ offsets, theme }) {
  if (!offsets || offsets.length === 0) return null;

  const sorted = [...offsets].sort((a, b) => b.offset - a.offset);
  // Show every other row to keep it compact
  const displayed = sorted.filter((_, i) => i % 2 === 0);

  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{
        width: '100%', borderCollapse: 'collapse',
        fontFamily: theme.mono, fontSize: 11,
      }}>
        <thead>
          <tr style={{ borderBottom: `1px solid ${theme.border}` }}>
            {['Offset', 'Windows', 'Correct', 'Accuracy', 'Confidence'].map(h => (
              <th key={h} style={{
                padding: '6px 10px', textAlign: 'right',
                color: theme.textMuted, fontSize: 9, fontWeight: 600,
                letterSpacing: '0.06em',
              }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {displayed.map(o => (
            <tr key={o.offset} style={{
              borderBottom: `1px solid ${theme.border}`,
              background: o.accuracy_pct >= 70 ? 'rgba(16, 185, 129, 0.04)' : 'transparent',
            }}>
              <td style={{ padding: '5px 10px', textAlign: 'right', color: theme.text }}>
                T-{o.offset}
              </td>
              <td style={{ padding: '5px 10px', textAlign: 'right', color: theme.textMuted }}>
                {o.evaluations}
              </td>
              <td style={{ padding: '5px 10px', textAlign: 'right', color: theme.text }}>
                {o.correct_predictions}
              </td>
              <td style={{
                padding: '5px 10px', textAlign: 'right', fontWeight: 600,
                color: o.accuracy_pct >= 70 ? theme.green
                     : o.accuracy_pct >= 55 ? theme.amber
                     : theme.red,
              }}>
                {o.accuracy_pct}%
              </td>
              <td style={{ padding: '5px 10px', textAlign: 'right', color: theme.purple }}>
                {o.avg_confidence != null ? (o.avg_confidence * 100).toFixed(1) + '%' : '--'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Main Component ──────────────────────────────────────────────────────────

export default function Overview() {
  const api = useApi();
  const { mode } = useTheme();
  const theme = getTheme(mode);

  const [data, setData] = useState(null);
  const [days, setDays] = useState(7);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchData = useCallback(async () => {
    try {
      setLoading(true);
      const res = await api.get(`/v58/prediction-surface?days=${days}`);
      setData(res.data);
      setError(res.data?.error || null);
    } catch (err) {
      setError(err.message || 'Failed to load');
    } finally {
      setLoading(false);
    }
  }, [api, days]);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 30000);
    return () => clearInterval(interval);
  }, [fetchData]);

  const strategyIds = data?.strategy_summary ? Object.keys(data.strategy_summary) : [];

  return (
    <div style={{
      padding: '20px 24px',
      maxWidth: 1000,
      margin: '0 auto',
      color: theme.text,
    }}>
      {/* Header */}
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        marginBottom: 20,
      }}>
        <div>
          <h1 style={{
            margin: 0, fontSize: 20, fontWeight: 700,
            fontFamily: theme.mono, color: theme.text,
          }}>
            Prediction Surface
          </h1>
          <div style={{
            color: theme.textMuted, fontSize: 11, fontFamily: theme.mono, marginTop: 4,
          }}>
            Signal accuracy + strategy decisions by eval offset
            {data && ` \u2014 ${data.total_windows} resolved windows`}
          </div>
        </div>

        {/* Period selector */}
        <div style={{ display: 'flex', gap: 4 }}>
          {DAYS_OPTIONS.map(d => (
            <button key={d} onClick={() => setDays(d)} style={{
              padding: '5px 10px', borderRadius: 5,
              border: `1px solid ${d === days ? theme.cyan : theme.border}`,
              background: d === days ? 'rgba(6, 182, 212, 0.12)' : 'transparent',
              color: d === days ? theme.cyan : theme.textMuted,
              cursor: 'pointer', fontFamily: theme.mono, fontSize: 11, fontWeight: 600,
              transition: 'all 150ms',
            }}>
              {d}d
            </button>
          ))}
        </div>
      </div>

      {loading && !data && (
        <div style={{ padding: 40, textAlign: 'center', color: theme.textMuted }}>
          Loading prediction surface...
        </div>
      )}

      {error && (
        <div style={{
          padding: '10px 16px', marginBottom: 16, borderRadius: 6,
          background: 'rgba(239, 68, 68, 0.08)', border: '1px solid rgba(239, 68, 68, 0.2)',
          color: theme.red, fontSize: 11, fontFamily: theme.mono,
        }}>
          {error}
        </div>
      )}

      {data && (
        <>
          {/* Section 1: Strategy Performance Cards */}
          <div style={{ display: 'flex', gap: 12, marginBottom: 20, flexWrap: 'wrap' }}>
            {strategyIds.length > 0 ? (
              strategyIds.map(sid => (
                <StrategyCard
                  key={sid}
                  strategyId={sid}
                  data={data.strategy_summary[sid]}
                  theme={theme}
                />
              ))
            ) : (
              <div style={{
                flex: 1, padding: 20, textAlign: 'center',
                color: theme.textMuted, fontFamily: theme.mono, fontSize: 12,
                background: theme.card, borderRadius: 10,
                border: `1px solid ${theme.cardBorder}`,
              }}>
                No strategy decisions recorded yet
              </div>
            )}
          </div>

          {/* Section 2: Prediction Accuracy Surface */}
          <Card title="PREDICTION ACCURACY SURFACE" theme={theme}
            badge={
              <span style={{
                fontSize: 9, color: theme.textMuted, fontFamily: theme.mono,
              }}>
                accuracy by eval_offset (10s buckets)
              </span>
            }>
            <AccuracyChart offsets={data.offsets} theme={theme} />
            <div style={{ marginTop: 12 }}>
              <PredictionTable offsets={data.offsets} theme={theme} />
            </div>
          </Card>

          {/* Section 3: Strategy Decision Surface */}
          <div style={{ marginTop: 16 }}>
            <Card title="STRATEGY DECISION SURFACE" theme={theme}
              badge={
                <span style={{
                  fontSize: 9, color: theme.textMuted, fontFamily: theme.mono,
                }}>
                  trades taken by offset (green dot = WR {'>'}= 60%)
                </span>
              }>
              <StrategyDecisionChart
                offsets={data.offsets}
                strategyIds={strategyIds}
                theme={theme}
              />
            </Card>
          </div>

          {/* Section 4: Recent Windows */}
          <div style={{ marginTop: 16 }}>
            <Card title="RECENT WINDOWS" theme={theme}
              badge={
                <span style={{
                  fontSize: 9, color: theme.textMuted, fontFamily: theme.mono,
                }}>
                  last 10 resolved
                </span>
              }>
              {data.recent_windows && data.recent_windows.length > 0 ? (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {data.recent_windows.map((w, i) => (
                    <RecentWindowCard key={w.window_ts || i} win={w} theme={theme} />
                  ))}
                </div>
              ) : (
                <div style={{ padding: 20, textAlign: 'center', color: theme.textMuted }}>
                  No resolved windows in this period
                </div>
              )}
            </Card>
          </div>
        </>
      )}
    </div>
  );
}
