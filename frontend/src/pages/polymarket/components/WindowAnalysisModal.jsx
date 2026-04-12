import React, { useState, useEffect, useCallback } from 'react';
import { useApi } from '../../../hooks/useApi.js';
import { T, fmt } from './theme.js';

/**
 * WindowAnalysisModal -- Per-window evaluation timeline drilldown.
 *
 * Shows all signal_evaluations + strategy_decisions for a single window:
 *   A. Confidence Timeline (inline SVG area chart)
 *   B. Strategy Action Strip (V10 + V4 heatmap)
 *   C. Signal Detail Table (scrollable)
 *   D. Best Entry Card
 *
 * Props:
 *   windowTs  - epoch seconds (null = closed)
 *   onClose   - callback
 *   asset     - default "btc"
 *   timeframe - default "5m"
 */

// ── Styles ──────────────────────────────────────────────────────────────────

const S = {
  overlay: {
    position: 'fixed', inset: 0, zIndex: 9999,
    background: 'rgba(0,0,0,0.75)',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    backdropFilter: 'blur(4px)',
  },
  modal: {
    background: T.bg,
    border: `1px solid ${T.cardBorder}`,
    borderRadius: 8,
    width: '92vw', maxWidth: 1100,
    maxHeight: '92vh',
    display: 'flex', flexDirection: 'column',
    overflow: 'hidden',
  },
  header: {
    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
    padding: '14px 20px',
    borderBottom: `1px solid ${T.border}`,
    background: T.headerBg,
  },
  title: {
    fontSize: 14, fontWeight: 700, color: T.text,
    fontFamily: T.mono,
  },
  subtitle: {
    fontSize: 10, color: T.textMuted, marginTop: 2,
    fontFamily: T.mono, display: 'flex', gap: 12,
  },
  closeBtn: {
    background: 'none', border: `1px solid ${T.border}`, borderRadius: 4,
    color: T.textMuted, cursor: 'pointer', padding: '4px 10px',
    fontSize: 11, fontFamily: T.mono,
  },
  body: {
    overflowY: 'auto', padding: 20,
    display: 'flex', flexDirection: 'column', gap: 20,
  },
  section: {
    background: T.card, border: `1px solid ${T.cardBorder}`,
    borderRadius: 6, padding: 14,
  },
  sectionTitle: {
    fontSize: 10, fontWeight: 700, color: T.cyan,
    letterSpacing: '0.06em', textTransform: 'uppercase',
    marginBottom: 10, fontFamily: T.mono,
  },
  pill: (bg, color) => ({
    display: 'inline-block', padding: '1px 7px', borderRadius: 3,
    fontSize: 9, fontWeight: 700, background: bg, color,
    fontFamily: T.mono,
  }),
  td: {
    padding: '4px 8px', fontSize: 10, fontFamily: T.mono,
    borderBottom: `1px solid ${T.border}`, whiteSpace: 'nowrap',
  },
  th: {
    padding: '4px 8px', fontSize: 9, fontFamily: T.mono,
    borderBottom: `1px solid ${T.border}`, whiteSpace: 'nowrap',
    color: T.textMuted, fontWeight: 700, letterSpacing: '0.05em',
    textTransform: 'uppercase', position: 'sticky', top: 0,
    background: T.headerBg, zIndex: 1,
  },
};

// ── Helpers ──────────────────────────────────────────────────────────────────

function fmtTime(epochSec) {
  if (!epochSec) return '--';
  const d = new Date(epochSec * 1000);
  return d.toISOString().replace('T', ' ').slice(0, 19) + ' UTC';
}

function dirColor(dir) {
  if (!dir) return T.textDim;
  return dir === 'UP' ? T.green : T.red;
}

function pctStr(v) {
  if (v == null) return '--';
  return (v * 100).toFixed(2) + '%';
}

// ── Confidence Timeline SVG ─────────────────────────────────────────────────

function ConfidenceChart({ timeline, bestEntry, outcomeDir }) {
  if (!timeline || timeline.length === 0) return null;

  const W = 900, H = 220;
  const padL = 45, padR = 15, padT = 15, padB = 30;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;

  // timeline is sorted eval_offset DESC (298 -> 2)
  // We want to plot left=T-300, right=T-0
  // So higher offsets go left, lower offsets go right
  const maxOffset = Math.max(...timeline.map(t => t.eval_offset));
  const minOffset = Math.min(...timeline.map(t => t.eval_offset));
  const offsetRange = maxOffset - minOffset || 1;

  const toX = (offset) => padL + ((maxOffset - offset) / offsetRange) * plotW;
  const toY = (pUp) => padT + (1 - (pUp ?? 0.5)) * plotH;

  // Sort by offset DESC for area path (left to right = high offset to low)
  const sorted = [...timeline].sort((a, b) => b.eval_offset - a.eval_offset);

  // Area path
  const linePoints = sorted.map(t => {
    const pUp = t.prediction?.p_up ?? 0.5;
    return `${toX(t.eval_offset).toFixed(1)},${toY(pUp).toFixed(1)}`;
  });
  const linePath = 'M' + linePoints.join(' L');
  const areaPath = linePath +
    ` L${toX(sorted[sorted.length - 1].eval_offset).toFixed(1)},${(padT + plotH).toFixed(1)}` +
    ` L${toX(sorted[0].eval_offset).toFixed(1)},${(padT + plotH).toFixed(1)} Z`;

  // X-axis labels
  const xLabels = [300, 240, 180, 120, 60, 0].filter(v => v >= minOffset && v <= maxOffset);

  // Best entry marker
  const bestX = bestEntry ? toX(bestEntry.eval_offset) : null;
  const bestY = bestEntry ? toY(bestEntry.p_up ?? 0.5) : null;

  return (
    <svg width={W} height={H} style={{ display: 'block', maxWidth: '100%' }}
      viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet">
      {/* Outcome band at bottom */}
      <rect x={padL} y={H - 6} width={plotW} height={4} rx={2}
        fill={outcomeDir === 'UP' ? T.green : outcomeDir === 'DOWN' ? T.red : T.textDim}
        opacity={0.5} />

      {/* 0.5 threshold line */}
      <line x1={padL} y1={toY(0.5)} x2={W - padR} y2={toY(0.5)}
        stroke={T.red} strokeWidth={1} strokeDasharray="6,4" opacity={0.6} />

      {/* DUNE gate lines at 0.65 and 0.35 */}
      <line x1={padL} y1={toY(0.65)} x2={W - padR} y2={toY(0.65)}
        stroke={T.green} strokeWidth={0.8} strokeDasharray="4,4" opacity={0.4} />
      <line x1={padL} y1={toY(0.35)} x2={W - padR} y2={toY(0.35)}
        stroke={T.green} strokeWidth={0.8} strokeDasharray="4,4" opacity={0.4} />

      {/* Area fill */}
      <path d={areaPath} fill={T.cyan} opacity={0.15} />
      {/* Line */}
      <path d={linePath} fill="none" stroke={T.cyan} strokeWidth={1.5} />

      {/* Y-axis labels */}
      {[0, 0.25, 0.5, 0.75, 1.0].map(v => (
        <text key={v} x={padL - 4} y={toY(v) + 3}
          fill={T.textDim} fontSize={8} textAnchor="end" fontFamily={T.mono}>
          {v.toFixed(2)}
        </text>
      ))}

      {/* X-axis labels */}
      {xLabels.map(v => (
        <text key={v} x={toX(v)} y={H - 10}
          fill={T.textDim} fontSize={8} textAnchor="middle" fontFamily={T.mono}>
          T-{v}
        </text>
      ))}

      {/* Best entry marker */}
      {bestX != null && bestY != null && (
        <>
          <circle cx={bestX} cy={bestY} r={5}
            fill={T.amber} stroke="#000" strokeWidth={1} />
          <text x={bestX} y={bestY - 10}
            fill={T.amber} fontSize={8} textAnchor="middle" fontFamily={T.mono}
            fontWeight={700}>
            BEST
          </text>
        </>
      )}

      {/* Threshold labels */}
      <text x={W - padR + 2} y={toY(0.5) + 3}
        fill={T.red} fontSize={7} opacity={0.6} fontFamily={T.mono}>0.50</text>
      <text x={W - padR + 2} y={toY(0.65) + 3}
        fill={T.green} fontSize={7} opacity={0.4} fontFamily={T.mono}>0.65</text>
      <text x={W - padR + 2} y={toY(0.35) + 3}
        fill={T.green} fontSize={7} opacity={0.4} fontFamily={T.mono}>0.35</text>
    </svg>
  );
}

// ── Strategy Action Strip ───────────────────────────────────────────────────

function StrategyStrip({ timeline, label, strategyId }) {
  if (!timeline || timeline.length === 0) return null;

  const W = 900, H = 16;
  const sorted = [...timeline].sort((a, b) => b.eval_offset - a.eval_offset);
  const step = W / sorted.length;

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
      <span style={{ fontSize: 9, color: T.textMuted, fontFamily: T.mono, width: 60, flexShrink: 0 }}>
        {label}
      </span>
      <svg width={W} height={H} style={{ display: 'block', maxWidth: '100%' }}
        viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet">
        {sorted.map((t, i) => {
          const sd = t.strategies?.[strategyId];
          const action = sd?.action || t.decision_v10 || 'SKIP';
          const color = action === 'TRADE' ? T.green
            : action === 'ERROR' ? T.red
            : 'rgba(71,85,105,0.4)';
          return (
            <rect key={i} x={i * step} y={0} width={Math.max(step, 1)} height={H}
              fill={color} opacity={action === 'TRADE' ? 0.8 : 0.3} />
          );
        })}
      </svg>
    </div>
  );
}

// ── Main Modal Component ────────────────────────────────────────────────────

export default function WindowAnalysisModal({
  windowTs,
  onClose,
  asset = 'btc',
  timeframe = '5m',
}) {
  const api = useApi();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const fetchAnalysis = useCallback(async () => {
    if (!windowTs) return;
    setLoading(true);
    setError(null);
    try {
      const res = await api.get(`/v58/window-analysis/${windowTs}?asset=${asset}&timeframe=${timeframe}`);
      setData(res.data || res);
    } catch (err) {
      setError(err.response?.data?.detail || err.message || 'Failed to load');
    } finally {
      setLoading(false);
    }
  }, [api, windowTs, asset, timeframe]);

  useEffect(() => {
    if (windowTs) fetchAnalysis();
  }, [windowTs, fetchAnalysis]);

  // Close on Escape
  useEffect(() => {
    const handler = (e) => { if (e.key === 'Escape') onClose?.(); };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [onClose]);

  if (!windowTs) return null;

  const timeline = data?.timeline || [];
  const outcome = data?.outcome;
  const bestEntry = data?.best_entry;
  const summary = data?.summary;

  // Gates count helper
  const gateCount = (gates) => {
    if (!gates) return '--';
    const keys = ['vpin', 'delta', 'cg', 'twap', 'timesfm'];
    const passed = keys.filter(k => gates[k] === true).length;
    const total = keys.filter(k => gates[k] != null).length;
    return `${passed}/${total}`;
  };

  return (
    <div style={S.overlay} onClick={(e) => { if (e.target === e.currentTarget) onClose?.(); }}>
      <div style={S.modal} onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div style={S.header}>
          <div>
            <div style={S.title}>
              Window Analysis: {fmtTime(windowTs)}
            </div>
            <div style={S.subtitle}>
              {outcome && (
                <>
                  <span>
                    Outcome:{' '}
                    <span style={{ color: dirColor(outcome.direction), fontWeight: 700 }}>
                      {outcome.direction}
                    </span>
                    {outcome.delta_pct != null && (
                      <span style={{ color: T.textDim }}> ({pctStr(outcome.delta_pct)})</span>
                    )}
                  </span>
                  <span>{data?.eval_count || 0} evaluations</span>
                </>
              )}
              {!outcome && <span style={{ color: T.amber }}>Unresolved</span>}
            </div>
          </div>
          <button style={S.closeBtn} onClick={onClose}>Close</button>
        </div>

        {/* Body */}
        <div style={S.body}>
          {loading && (
            <div style={{ textAlign: 'center', padding: 40, color: T.textMuted, fontSize: 12 }}>
              Loading analysis...
            </div>
          )}
          {error && (
            <div style={{ textAlign: 'center', padding: 40, color: T.red, fontSize: 12 }}>
              {error}
            </div>
          )}

          {!loading && !error && data && (
            <>
              {/* A. Confidence Timeline */}
              <div style={S.section}>
                <div style={S.sectionTitle}>Confidence Timeline</div>
                <ConfidenceChart
                  timeline={timeline}
                  bestEntry={bestEntry}
                  outcomeDir={outcome?.direction}
                />
                <div style={{ display: 'flex', gap: 16, marginTop: 8, flexWrap: 'wrap' }}>
                  <span style={{ fontSize: 9, color: T.cyan, fontFamily: T.mono }}>
                    --- P(UP)
                  </span>
                  <span style={{ fontSize: 9, color: T.red, fontFamily: T.mono }}>
                    --- 0.50 threshold
                  </span>
                  <span style={{ fontSize: 9, color: T.green, fontFamily: T.mono }}>
                    --- 0.65/0.35 DUNE gates
                  </span>
                  <span style={{ fontSize: 9, color: T.amber, fontFamily: T.mono }}>
                    * Best entry
                  </span>
                </div>
              </div>

              {/* B. Strategy Action Strip */}
              <div style={S.section}>
                <div style={S.sectionTitle}>Strategy Decisions</div>
                <StrategyStrip timeline={timeline} label="V10 Gate" strategyId="v10_gate" />
                <StrategyStrip timeline={timeline} label="V4 Fusion" strategyId="v4_fusion" />
                <div style={{ display: 'flex', gap: 12, marginTop: 6 }}>
                  <span style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono }}>
                    <span style={{ color: T.green }}>|</span> TRADE
                  </span>
                  <span style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono }}>
                    <span style={{ color: T.textDim }}>|</span> SKIP
                  </span>
                  <span style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono }}>
                    <span style={{ color: T.red }}>|</span> ERROR
                  </span>
                </div>
              </div>

              {/* C. Signal Detail Table */}
              <div style={{ ...S.section, padding: 0 }}>
                <div style={{ ...S.sectionTitle, padding: '14px 14px 0' }}>
                  Signal Detail ({timeline.length} evaluations)
                </div>
                <div style={{ maxHeight: 350, overflowY: 'auto' }}>
                  <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                    <thead>
                      <tr>
                        <th style={S.th}>Offset</th>
                        <th style={S.th}>P(UP)</th>
                        <th style={S.th}>Dir</th>
                        <th style={S.th}>Delta</th>
                        <th style={S.th}>VPIN</th>
                        <th style={S.th}>Regime</th>
                        <th style={S.th}>Gates</th>
                        <th style={S.th}>Blocking</th>
                        <th style={S.th}>V10</th>
                        <th style={S.th}>V4</th>
                      </tr>
                    </thead>
                    <tbody>
                      {timeline.map((t, i) => {
                        const pUp = t.prediction?.p_up;
                        const isBest = bestEntry && t.eval_offset === bestEntry.eval_offset;
                        const v4 = t.strategies?.v4_fusion;
                        const rowBg = isBest
                          ? 'rgba(245,158,11,0.08)'
                          : i % 2 === 0 ? 'transparent' : 'rgba(15,23,42,0.3)';

                        return (
                          <tr key={t.eval_offset} style={{
                            background: rowBg,
                            borderLeft: isBest ? `3px solid ${T.amber}` : '3px solid transparent',
                          }}>
                            <td style={{ ...S.td, color: T.text }}>T-{t.eval_offset}</td>
                            <td style={{
                              ...S.td,
                              color: pUp >= 0.65 ? T.green : pUp <= 0.35 ? T.red : T.text,
                              fontWeight: 600,
                            }}>
                              {fmt(pUp, 3)}
                            </td>
                            <td style={{ ...S.td, color: dirColor(t.prediction?.direction), fontWeight: 600 }}>
                              {t.prediction?.direction || '--'}
                            </td>
                            <td style={{ ...S.td, color: (t.signals?.delta_pct || 0) > 0 ? T.green : T.red }}>
                              {pctStr(t.signals?.delta_pct)}
                            </td>
                            <td style={{
                              ...S.td,
                              color: (t.signals?.vpin || 0) >= 0.55 ? T.green
                                : (t.signals?.vpin || 0) < 0.45 ? T.red : T.amber,
                            }}>
                              {fmt(t.signals?.vpin, 3)}
                            </td>
                            <td style={{ ...S.td, color: T.textMuted, fontSize: 9 }}>
                              {t.signals?.regime || '--'}
                            </td>
                            <td style={{ ...S.td, color: t.gates_v10?.all_passed ? T.green : T.textMuted }}>
                              {gateCount(t.gates_v10)}
                            </td>
                            <td style={{ ...S.td, fontSize: 9, color: T.textMuted, maxWidth: 100, overflow: 'hidden', textOverflow: 'ellipsis' }}
                              title={t.gates_v10?.blocking_gate || ''}>
                              {t.gates_v10?.blocking_gate || '--'}
                            </td>
                            <td style={S.td}>
                              <span style={S.pill(
                                t.decision_v10 === 'TRADE' ? 'rgba(16,185,129,0.15)' : 'rgba(71,85,105,0.2)',
                                t.decision_v10 === 'TRADE' ? T.green : T.textMuted,
                              )}>
                                {t.decision_v10}
                              </span>
                            </td>
                            <td style={S.td}>
                              {v4 ? (
                                <span style={S.pill(
                                  v4.action === 'TRADE' ? 'rgba(168,85,247,0.15)' : 'rgba(71,85,105,0.2)',
                                  v4.action === 'TRADE' ? T.purple : T.textMuted,
                                )}>
                                  {v4.action}
                                </span>
                              ) : (
                                <span style={{ color: T.textDim, fontSize: 9 }}>--</span>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* D. Best Entry Card */}
              <div style={{
                ...S.section,
                borderColor: bestEntry ? T.amber : T.cardBorder,
                borderWidth: bestEntry ? 2 : 1,
              }}>
                <div style={{ ...S.sectionTitle, color: T.amber }}>Best Entry Point</div>
                {bestEntry ? (
                  <div style={{ fontSize: 12, color: T.text, fontFamily: T.mono, lineHeight: 1.6 }}>
                    <div>
                      Best entry at <strong>T-{bestEntry.eval_offset}</strong>:{' '}
                      <span style={{ color: dirColor(bestEntry.direction), fontWeight: 700 }}>
                        {bestEntry.direction}
                      </span>{' '}
                      with <span style={{ color: T.amber, fontWeight: 700 }}>
                        {fmt(Math.max(bestEntry.p_up || 0, 1 - (bestEntry.p_up || 0)) * 100, 1)}%
                      </span>{' '}
                      confidence{' '}
                      <span style={{ color: T.green }}>(correct)</span>
                    </div>
                    <div style={{ marginTop: 4, fontSize: 10, color: T.textMuted }}>
                      {bestEntry.strategies_trading?.length > 0
                        ? `${bestEntry.strategies_trading.join(', ')} would have traded at this offset.`
                        : 'No strategy would have traded at this offset.'
                      }
                      {summary && (
                        <span>
                          {' '}Direction correct {fmt((summary.pct_time_correct_direction || 0) * 100, 1)}% of the time.
                          {summary.direction_flips > 0 && ` ${summary.direction_flips} direction flip${summary.direction_flips > 1 ? 's' : ''}.`}
                        </span>
                      )}
                    </div>
                    {bestEntry.eval_offset > 120 && (
                      <div style={{ marginTop: 4, fontSize: 10, color: T.amber }}>
                        Note: Best entry at T-{bestEntry.eval_offset} is outside the tradeable zone (T-120 to T-10).
                      </div>
                    )}
                  </div>
                ) : (
                  <div style={{ fontSize: 12, color: T.textMuted, fontFamily: T.mono }}>
                    No directionally-correct prediction in this window.
                    {summary?.peak_confidence != null && (
                      <span>
                        {' '}Peak confidence was {fmt(summary.peak_confidence * 100, 1)}% at T-{summary.peak_confidence_offset} (wrong direction).
                      </span>
                    )}
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
