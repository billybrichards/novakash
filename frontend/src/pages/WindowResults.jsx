/**
 * WindowResults.jsx — Window Results page
 *
 * Shows the last 100 windows as expandable cards with:
 * - Time, open→close, actual direction, signal values
 * - Expandable detail: price ticks, what-if P&L, gate badge
 * - Color coding: green=correct, red=wrong, gray=pending
 */

import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { useApi } from '../hooks/useApi.js';

// ─── Theme (match V58Monitor) ─────────────────────────────────────────────────
const T = {
  bg: '#07070c',
  card: 'rgba(255,255,255,0.018)',
  cardHover: 'rgba(255,255,255,0.032)',
  border: 'rgba(255,255,255,0.07)',
  purple: '#a855f7',
  cyan: '#06b6d4',
  profit: '#4ade80',
  loss: '#f87171',
  warning: '#f59e0b',
  label: 'rgba(255,255,255,0.35)',
  label2: 'rgba(255,255,255,0.55)',
  mono: "'IBM Plex Mono', monospace",
};

// ─── Helpers ──────────────────────────────────────────────────────────────────
function fmt(price) {
  if (price == null) return '—';
  return '$' + Math.round(price).toLocaleString('en-US');
}

function fmtPct(pct) {
  if (pct == null) return '—';
  const v = pct * 100;
  return (v >= 0 ? '+' : '') + v.toFixed(3) + '%';
}

function fmtPnl(pnl) {
  if (pnl == null) return '—';
  return (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(2);
}

function fmtConf(c) {
  if (c == null) return '—';
  return Math.round(c * 100) + '%';
}

function dirColor(dir) {
  if (!dir) return T.label;
  return dir === 'UP' ? T.profit : T.loss;
}

function dirLabel(dir) {
  if (!dir) return '—';
  return dir === 'UP' ? '▲ UP' : '▼ DOWN';
}

// ─── Mini confidence bar ──────────────────────────────────────────────────────
function ConfBar({ value, color }) {
  const pct = Math.round((value ?? 0) * 100);
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
      <div style={{
        width: 48, height: 3,
        background: 'rgba(255,255,255,0.08)',
        borderRadius: 2, overflow: 'hidden',
      }}>
        <div style={{
          height: '100%', width: `${pct}%`,
          background: color, borderRadius: 2,
        }} />
      </div>
      <span style={{ fontSize: 9, color, fontFamily: T.mono }}>{pct}%</span>
    </div>
  );
}

// ─── Gate Badge ───────────────────────────────────────────────────────────────
function GateBadge({ gateStatus }) {
  const cfg = {
    PASSED: { color: T.profit, bg: 'rgba(74,222,128,0.12)', label: '✅ GATE PASSED' },
    BLOCKED: { color: T.loss, bg: 'rgba(248,113,113,0.12)', label: '🚫 GATE BLOCKED' },
    SKIPPED: { color: T.label, bg: 'rgba(255,255,255,0.06)', label: '⏭ SKIPPED' },
  }[gateStatus] ?? { color: T.label, bg: 'rgba(255,255,255,0.06)', label: gateStatus };

  return (
    <span style={{
      padding: '2px 8px',
      borderRadius: 5,
      background: cfg.bg,
      color: cfg.color,
      fontSize: 9,
      fontWeight: 700,
      letterSpacing: '0.05em',
      fontFamily: T.mono,
    }}>
      {cfg.label}
    </span>
  );
}

// ─── What-if Scenario Row ─────────────────────────────────────────────────────
function ScenarioRow({ label, icon, color, scenario }) {
  if (!scenario) return null;
  const { direction, entry_price, correct, pnl_usd, actual_direction } = scenario;
  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 10,
      padding: '8px 12px',
      borderRadius: 8,
      background: 'rgba(0,0,0,0.2)',
      border: `1px solid ${color}22`,
      flexWrap: 'wrap',
    }}>
      <span style={{ fontSize: 13 }}>{icon}</span>
      <span style={{ fontSize: 10, color, fontWeight: 600, fontFamily: T.mono, minWidth: 80 }}>{label}</span>
      <span style={{
        fontSize: 10, color: dirColor(direction),
        fontWeight: 700, fontFamily: T.mono, minWidth: 56,
      }}>
        {dirLabel(direction)}
      </span>
      {entry_price != null && (
        <span style={{ fontSize: 9, color: T.label, fontFamily: T.mono }}>
          @${entry_price.toFixed(3)} → $4
        </span>
      )}
      <span style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8 }}>
        {correct !== null && correct !== undefined && (
          <span style={{
            fontSize: 11,
            color: correct ? T.profit : T.loss,
            fontWeight: 700,
          }}>
            {correct ? '✅ WON' : '❌ LOST'}
          </span>
        )}
        {pnl_usd !== null && pnl_usd !== undefined && (
          <span style={{
            fontSize: 13, fontWeight: 700,
            color: pnl_usd >= 0 ? T.profit : T.loss,
            fontFamily: T.mono,
          }}>
            {fmtPnl(pnl_usd)}
          </span>
        )}
        {(correct === null || correct === undefined) && (
          <span style={{ color: T.label, fontSize: 9 }}>No price data</span>
        )}
      </span>
    </div>
  );
}

// ─── Mini Spark Chart (SVG) ───────────────────────────────────────────────────
function SparkChart({ ticks }) {
  if (!ticks?.length) return (
    <div style={{ color: T.label, fontSize: 10, fontFamily: T.mono, padding: '8px 0' }}>
      No price tick data available
    </div>
  );

  const prices = ticks.map(t => t.price).filter(p => p != null);
  if (!prices.length) return null;

  const min = Math.min(...prices);
  const max = Math.max(...prices);
  const range = max - min || 1;
  const W = 280, H = 60;
  const pad = 4;

  const pts = ticks
    .filter(t => t.price != null)
    .map((t, i, arr) => {
      const x = pad + (i / Math.max(arr.length - 1, 1)) * (W - pad * 2);
      const y = H - pad - ((t.price - min) / range) * (H - pad * 2);
      return `${x},${y}`;
    })
    .join(' ');

  const startPrice = prices[0];
  const endPrice = prices[prices.length - 1];
  const color = endPrice >= startPrice ? T.profit : T.loss;

  return (
    <div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        style={{ width: '100%', height: 60, display: 'block' }}
        preserveAspectRatio="none"
      >
        <polyline
          points={pts}
          fill="none"
          stroke={color}
          strokeWidth="1.5"
          strokeLinejoin="round"
          strokeLinecap="round"
        />
      </svg>
      <div style={{
        display: 'flex', justifyContent: 'space-between',
        fontSize: 9, color: T.label, fontFamily: T.mono, marginTop: 2,
      }}>
        <span>{fmt(startPrice)}</span>
        <span style={{ color }}>{fmt(endPrice)}</span>
      </div>
    </div>
  );
}

// ─── Window Detail (expanded) ─────────────────────────────────────────────────
function WindowDetail({ windowTs }) {
  const api = useApi();
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!windowTs) return;
    setLoading(true);
    setDetail(null);
    api('GET', `/v58/window-detail/${encodeURIComponent(windowTs)}`)
      .then(r => setDetail(r?.data ?? null))
      .catch(() => setDetail({ error: 'Failed to load detail' }))
      .finally(() => setLoading(false));
  }, [windowTs]);

  if (loading) {
    return (
      <div style={{ padding: 16, color: T.purple, fontSize: 11, fontFamily: T.mono }}>
        Loading detail…
      </div>
    );
  }

  if (!detail || detail.error) {
    return (
      <div style={{ padding: 16, color: T.loss, fontSize: 11, fontFamily: T.mono }}>
        {detail?.error || 'No detail available'}
      </div>
    );
  }

  const { snapshot, evaluations, price_ticks, what_if, entry_timing } = detail;

  return (
    <div style={{
      borderTop: `1px solid ${T.border}`,
      padding: '16px 20px',
      display: 'flex',
      flexDirection: 'column',
      gap: 16,
    }}>
      {/* Price chart + evaluations row */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'minmax(0,1fr) minmax(0,1fr)',
        gap: 16,
      }}>
        {/* Spark chart */}
        <div>
          <div style={{ fontSize: 9, color: T.label, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 8, fontFamily: T.mono }}>
            Price Through Window
          </div>
          <SparkChart ticks={price_ticks} />
        </div>

        {/* Countdown evaluations */}
        <div>
          <div style={{ fontSize: 9, color: T.label, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 8, fontFamily: T.mono }}>
            Countdown Evaluations
          </div>
          {evaluations?.length ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {evaluations.map((ev, i) => (
                <div key={i} style={{
                  padding: '6px 10px',
                  borderRadius: 6,
                  background: 'rgba(0,0,0,0.2)',
                  border: `1px solid ${T.border}`,
                  fontFamily: T.mono,
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                    <span style={{ fontSize: 9, color: T.purple }}>{ev.stage}</span>
                    {ev.direction && (
                      <span style={{ fontSize: 10, color: dirColor(ev.direction), fontWeight: 700 }}>
                        {dirLabel(ev.direction)}
                      </span>
                    )}
                  </div>
                  {ev.confidence != null && (
                    <ConfBar value={ev.confidence} color={T.purple} />
                  )}
                  {ev.action && (
                    <div style={{ fontSize: 9, color: T.label, marginTop: 3 }}>{ev.action}</div>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <div style={{ color: T.label, fontSize: 10, fontFamily: T.mono }}>
              No countdown data
            </div>
          )}
        </div>
      </div>

      {/* What-if P&L */}
      {what_if && (
        <div>
          <div style={{
            display: 'flex', alignItems: 'center', gap: 10,
            marginBottom: 10,
          }}>
            <div style={{ fontSize: 9, color: T.label, letterSpacing: '0.1em', textTransform: 'uppercase', fontFamily: T.mono }}>
              What-If P&amp;L ($4 bet)
            </div>
            <GateBadge gateStatus={what_if.gate_status} />
            {what_if.skip_reason && (
              <span style={{ fontSize: 9, color: T.label, fontFamily: T.mono }}>
                reason: {what_if.skip_reason}
              </span>
            )}
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <ScenarioRow
              label="v5.7c Signal"
              icon="📍"
              color={T.purple}
              scenario={what_if.scenarios?.v57c}
            />
            <ScenarioRow
              label="TimesFM"
              icon="🔮"
              color="#e879f9"
              scenario={what_if.scenarios?.timesfm}
            />
            <ScenarioRow
              label="TWAP"
              icon="📊"
              color={T.cyan}
              scenario={what_if.scenarios?.twap}
            />
          </div>
        </div>
      )}

      {/* Gamma prices */}
      {snapshot && (snapshot.gamma_up_price != null || snapshot.gamma_down_price != null) && (
        <div style={{
          padding: '10px 14px',
          borderRadius: 8,
          background: 'rgba(245,158,11,0.05)',
          border: `1px solid rgba(245,158,11,0.2)`,
          fontFamily: T.mono,
        }}>
          <div style={{ fontSize: 9, color: T.warning, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 6 }}>
            ⚡ Gamma Prices
          </div>
          <div style={{ display: 'flex', gap: 20 }}>
            <div>
              <div style={{ fontSize: 9, color: T.label }}>↑ UP</div>
              <div style={{ fontSize: 12, color: T.profit, fontWeight: 700 }}>
                {snapshot.gamma_up_price != null ? `$${snapshot.gamma_up_price.toFixed(3)}` : '—'}
              </div>
            </div>
            <div>
              <div style={{ fontSize: 9, color: T.label }}>↓ DOWN</div>
              <div style={{ fontSize: 12, color: T.loss, fontWeight: 700 }}>
                {snapshot.gamma_down_price != null ? `$${snapshot.gamma_down_price.toFixed(3)}` : '—'}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ── What-If Entry Timing (v5.8.1) ─────────────────────────────────── */}
      {/* Shows Gamma prices at each countdown stage (T-240 through T-60)      */}
      {/* and what the P&L would have been if you entered at each point.       */}
      {entry_timing && entry_timing.length > 0 && !entry_timing[0]?.error && (
        <div>
          <div style={{
            fontSize: 9, color: T.label, letterSpacing: '0.1em',
            textTransform: 'uppercase', marginBottom: 10, fontFamily: T.mono,
          }}>
            ⏱ What-If Entry Timing
          </div>
          <div style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(5, 1fr)',
            gap: 6,
          }}>
            {entry_timing.map((et, i) => {
              // Get corresponding what-if data
              const wifEntry = what_if?.entry_timing?.find(w => w.stage === et.stage);
              const isBest = wifEntry?.is_best;
              const correct = wifEntry?.correct;
              const pnl = wifEntry?.pnl;
              const entry = wifEntry?.entry;

              let stageBorder = T.border;
              let stageBg = 'rgba(255,255,255,0.02)';
              if (isBest) {
                stageBorder = 'rgba(74,222,128,0.5)';
                stageBg = 'rgba(74,222,128,0.08)';
              } else if (correct === true) {
                stageBorder = 'rgba(74,222,128,0.2)';
                stageBg = 'rgba(74,222,128,0.03)';
              } else if (correct === false) {
                stageBorder = 'rgba(248,113,113,0.2)';
                stageBg = 'rgba(248,113,113,0.03)';
              }

              return (
                <div key={et.stage} style={{
                  padding: '10px 8px',
                  borderRadius: 8,
                  background: stageBg,
                  border: `1px solid ${stageBorder}`,
                  fontFamily: T.mono,
                  textAlign: 'center',
                  position: 'relative',
                  transition: 'all 200ms ease-out',
                }}>
                  {isBest && (
                    <div style={{
                      position: 'absolute',
                      top: -8,
                      left: '50%',
                      transform: 'translateX(-50%)',
                      fontSize: 8,
                      color: T.profit,
                      background: '#07070c',
                      padding: '0 4px',
                      letterSpacing: '0.06em',
                      fontWeight: 700,
                    }}>
                      BEST
                    </div>
                  )}
                  {/* Stage label */}
                  <div style={{ fontSize: 10, fontWeight: 700, color: T.cyan, marginBottom: 6, letterSpacing: '0.04em' }}>
                    {et.stage}
                  </div>
                  {/* Gamma prices at this stage */}
                  <div style={{ marginBottom: 6 }}>
                    {et.gamma_up != null ? (
                      <>
                        <div style={{ fontSize: 9, color: T.label }}>↑ {et.gamma_up.toFixed(3)}</div>
                        <div style={{ fontSize: 9, color: T.label }}>↓ {et.gamma_down != null ? et.gamma_down.toFixed(3) : '—'}</div>
                      </>
                    ) : (
                      <div style={{ fontSize: 9, color: T.label }}>No data</div>
                    )}
                  </div>
                  {/* TimesFM direction at this stage */}
                  {et.timesfm_dir && (
                    <div style={{
                      fontSize: 8,
                      color: et.timesfm_dir === 'UP' ? T.profit : T.loss,
                      marginBottom: 4,
                      letterSpacing: '0.04em',
                    }}>
                      🔮 {et.timesfm_dir} {et.timesfm_conf != null ? `${Math.round(et.timesfm_conf * 100)}%` : ''}
                    </div>
                  )}
                  {/* Entry + P&L */}
                  {entry != null && (
                    <div style={{
                      fontSize: 9,
                      color: T.label,
                      marginBottom: 2,
                    }}>
                      entry {entry.toFixed(3)}
                    </div>
                  )}
                  {pnl != null ? (
                    <div style={{
                      fontSize: 12,
                      fontWeight: 700,
                      color: pnl >= 0 ? T.profit : T.loss,
                    }}>
                      {pnl >= 0 ? '+' : ''}{pnl.toFixed(2)}
                    </div>
                  ) : (
                    <div style={{ fontSize: 10, color: T.label }}>—</div>
                  )}
                </div>
              );
            })}
          </div>
          {what_if?.best_entry_stage && (
            <div style={{ fontSize: 9, color: T.profit, marginTop: 6, fontFamily: T.mono, letterSpacing: '0.06em' }}>
              ✓ Best entry: {what_if.best_entry_stage}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Window Card ──────────────────────────────────────────────────────────────
function WindowCard({ outcome, isExpanded, onToggle }) {
  const {
    window_ts, open_price, close_price, delta_pct,
    actual_direction, direction, skip_reason, trade_placed,
    timesfm_direction, timesfm_correct, timesfm_confidence,
    twap_direction, twap_correct,
    v57c_correct,
    gamma_up_price, gamma_down_price, gamma_correct,
    vpin, confidence,
    v58_would_trade, v58_correct, v58_pnl,
    v58_skip_reason, tfm_v57c_agree,
    engine_version,
    v71_would_trade, v71_correct, v71_pnl, v71_regime, v71_skip_reason,
    poly_outcome,
  } = outcome;

  const time = window_ts
    ? new Date(window_ts).toLocaleString('en-GB', {
        month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit',
      })
    : '—';

  // Card color: traded windows get strong WIN/LOSS highlight
  const hasPending = !actual_direction;
  const tradeOutcome = poly_outcome || (trade_placed && actual_direction ? (
    direction === actual_direction ? 'WIN' : 'LOSS'
  ) : null);

  let cardBorderColor = T.border;
  let cardBg = T.card;
  if (trade_placed && tradeOutcome === 'WIN') {
    cardBorderColor = 'rgba(74,222,128,0.5)';
    cardBg = 'rgba(74,222,128,0.08)';
  } else if (trade_placed && tradeOutcome === 'LOSS') {
    cardBorderColor = 'rgba(248,113,113,0.5)';
    cardBg = 'rgba(248,113,113,0.08)';
  } else if (!hasPending) {
    const isCorrect = v58_would_trade ? v58_correct : v57c_correct;
    if (isCorrect === true) {
      cardBorderColor = 'rgba(74,222,128,0.15)';
      cardBg = 'rgba(74,222,128,0.02)';
    } else if (isCorrect === false) {
      cardBorderColor = 'rgba(248,113,113,0.15)';
      cardBg = 'rgba(248,113,113,0.02)';
    }
  }

  // Gate status
  const gateStatus = skip_reason ? 'BLOCKED' : trade_placed ? 'PASSED' : 'SKIPPED';

  return (
    <div style={{
      background: isExpanded ? 'rgba(168,85,247,0.06)' : cardBg,
      border: `1px solid ${isExpanded ? 'rgba(168,85,247,0.3)' : cardBorderColor}`,
      borderRadius: 12,
      overflow: 'hidden',
      transition: 'all 200ms ease-out',
      cursor: 'pointer',
    }}>
      {/* Header row */}
      <div
        onClick={onToggle}
        style={{
          display: 'grid',
          gridTemplateColumns: 'auto 1fr auto auto auto auto',
          gap: '0 12px',
          padding: '12px 16px',
          alignItems: 'center',
        }}
      >
        {/* Time + Version */}
        <div style={{ fontFamily: T.mono, minWidth: 120 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span style={{ fontSize: 11, color: '#fff' }}>{time}</span>
            {engine_version && (
              <span style={{
                fontSize: 8, padding: '1px 4px', borderRadius: 3,
                background: engine_version === 'v7.1' ? 'rgba(168,85,247,0.15)' : engine_version === 'v5.8' ? 'rgba(74,222,128,0.15)' : 'rgba(255,255,255,0.06)',
                color: engine_version === 'v7.1' ? '#a855f7' : engine_version === 'v5.8' ? '#4ade80' : T.label,
                fontWeight: 600,
              }}>{engine_version}</span>
            )}
          </div>
          <div style={{ fontSize: 9, color: T.label, marginTop: 2 }}>
            {window_ts ? new Date(window_ts).toLocaleTimeString('en-GB') : ''}
          </div>
        </div>

        {/* Open → Close */}
        <div style={{ fontFamily: T.mono }}>
          {(open_price || close_price) ? (
            <div style={{ fontSize: 11 }}>
              <span style={{ color: T.label2 }}>{fmt(open_price)}</span>
              <span style={{ color: T.label, margin: '0 6px' }}>→</span>
              <span style={{ color: '#fff' }}>{fmt(close_price)}</span>
              {delta_pct != null && (
                <span style={{
                  marginLeft: 8, fontSize: 10,
                  color: delta_pct >= 0 ? T.profit : T.loss,
                  fontWeight: 700,
                }}>
                  {fmtPct(delta_pct)}
                </span>
              )}
            </div>
          ) : (
            <span style={{ color: T.label, fontSize: 10 }}>No price data</span>
          )}
          {/* Signals mini row */}
          <div style={{ display: 'flex', gap: 8, marginTop: 4, flexWrap: 'wrap' }}>
            {direction && (
              <span style={{ fontSize: 9, color: T.purple, fontFamily: T.mono }}>
                v57c:{' '}
                <span style={{ color: dirColor(direction), fontWeight: 700 }}>
                  {direction === 'UP' ? '▲' : '▼'}
                </span>
                {v57c_correct !== null && v57c_correct !== undefined && (
                  <span> {v57c_correct ? '✅' : '❌'}</span>
                )}
              </span>
            )}
            {timesfm_direction && (
              <span style={{ fontSize: 9, color: '#e879f9', fontFamily: T.mono }}>
                TsFM:{' '}
                <span style={{ color: dirColor(timesfm_direction), fontWeight: 700 }}>
                  {timesfm_direction === 'UP' ? '▲' : '▼'}
                </span>
                {timesfm_correct !== null && timesfm_correct !== undefined && (
                  <span> {timesfm_correct ? '✅' : '❌'}</span>
                )}
              </span>
            )}
            {twap_direction && (
              <span style={{ fontSize: 9, color: T.cyan, fontFamily: T.mono }}>
                TWAP:{' '}
                <span style={{ color: dirColor(twap_direction), fontWeight: 700 }}>
                  {twap_direction === 'UP' ? '▲' : '▼'}
                </span>
                {twap_correct !== null && twap_correct !== undefined && (
                  <span> {twap_correct ? '✅' : '❌'}</span>
                )}
              </span>
            )}
          </div>
        </div>

        {/* Actual direction badge */}
        <div>
          {actual_direction ? (
            <span style={{
              padding: '4px 10px',
              borderRadius: 6,
              background: actual_direction === 'UP' ? 'rgba(74,222,128,0.12)' : 'rgba(248,113,113,0.12)',
              color: actual_direction === 'UP' ? T.profit : T.loss,
              fontSize: 11,
              fontWeight: 700,
              fontFamily: T.mono,
              whiteSpace: 'nowrap',
            }}>
              {actual_direction === 'UP' ? '▲ UP' : '▼ DOWN'}
            </span>
          ) : (
            <span style={{ color: T.label, fontSize: 10, fontFamily: T.mono }}>pending</span>
          )}
        </div>

        {/* Trade Outcome Badge (prominent for traded windows) */}
        {trade_placed && tradeOutcome && (
          <span style={{
            padding: '3px 10px',
            borderRadius: 6,
            background: tradeOutcome === 'WIN' ? 'rgba(74,222,128,0.2)' : 'rgba(248,113,113,0.2)',
            color: tradeOutcome === 'WIN' ? '#22c55e' : '#ef4444',
            fontSize: 11,
            fontWeight: 800,
            fontFamily: T.mono,
            letterSpacing: '0.06em',
            border: `1px solid ${tradeOutcome === 'WIN' ? 'rgba(74,222,128,0.4)' : 'rgba(248,113,113,0.4)'}`,
          }}>
            {tradeOutcome === 'WIN' ? '✅ WIN' : '❌ LOSS'}
          </span>
        )}

        {/* Gate badge */}
        <GateBadge gateStatus={gateStatus} />

        {/* v7.1 badge */}
        {v71_would_trade !== null && v71_would_trade !== undefined && (
          <span
            title={v71_skip_reason || (v71_regime ? `${v71_regime} regime` : '')}
            style={{
              padding: '2px 6px',
              borderRadius: 4,
              background: v71_would_trade 
                ? (v71_correct === true ? 'rgba(168,85,247,0.15)' : v71_correct === false ? 'rgba(248,113,113,0.1)' : 'rgba(168,85,247,0.08)')
                : 'rgba(255,255,255,0.04)',
              color: v71_would_trade ? '#a855f7' : T.label,
              fontSize: 8,
              fontWeight: 700,
              fontFamily: T.mono,
              letterSpacing: '0.04em',
              cursor: 'help',
            }}>
            7.1:{v71_would_trade ? (v71_correct === true ? '✓W' : v71_correct === false ? '✗L' : 'T') : 'S'}
          </span>
        )}

        {/* Real P&L (actual trade) or shadow P&L */}
        <div style={{ textAlign: 'right', minWidth: 60 }}>
          {trade_placed && v58_pnl != null ? (
            <span style={{
              fontSize: 14, fontWeight: 800,
              color: v58_pnl >= 0 ? T.profit : T.loss,
              fontFamily: T.mono,
            }}>
              {fmtPnl(v58_pnl)}
            </span>
          ) : v58_pnl != null ? (
            <span style={{
              fontSize: 12, fontWeight: 600,
              color: v58_pnl >= 0 ? T.profit : T.loss,
              fontFamily: T.mono,
              opacity: 0.6,
            }}>
              {fmtPnl(v58_pnl)}
            </span>
          ) : (
            <span style={{ color: T.label, fontSize: 10, fontFamily: T.mono }}>—</span>
          )}
        </div>

        {/* Expand chevron */}
        <div style={{
          color: T.label,
          fontSize: 12,
          transition: 'transform 200ms ease-out',
          transform: isExpanded ? 'rotate(180deg)' : 'rotate(0deg)',
        }}>
          ▾
        </div>
      </div>

      {/* Expanded detail */}
      {isExpanded && <WindowDetail windowTs={window_ts} />}
    </div>
  );
}

// ─── Filter / Sort bar ────────────────────────────────────────────────────────
function FilterBar({ filter, onFilter, sortBy, onSort, count, versionFilter, setVersionFilter }) {
  const btnStyle = (active) => ({
    padding: '4px 12px',
    borderRadius: 6,
    border: `1px solid ${active ? T.purple : T.border}`,
    background: active ? 'rgba(168,85,247,0.12)' : T.card,
    color: active ? T.purple : T.label2,
    fontSize: 10,
    fontFamily: T.mono,
    cursor: 'pointer',
    fontWeight: active ? 700 : 400,
  });

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      flexWrap: 'wrap',
      marginBottom: 16,
    }}>
      <span style={{ fontSize: 10, color: T.label, fontFamily: T.mono }}>{count} windows</span>
      <div style={{ display: 'flex', gap: 4 }}>
        {['ALL', 'WINS', 'LOSSES', 'SKIPPED', 'PENDING'].map(f => (
          <button key={f} style={btnStyle(filter === f)} onClick={() => onFilter(f)}>{f}</button>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
        <span style={{ fontSize: 9, color: T.label, fontFamily: T.mono }}>Version:</span>
        {[
          { v: 'ALL', color: null },
          { v: 'v7.1', color: 'rgba(168,85,247,0.3)', label: 'v7.1 ★' },
          { v: 'v5.8', color: 'rgba(74,222,128,0.3)' },
          { v: 'v5.7c', color: null },
          { v: 'v5.7', color: null },
          { v: 'v5.0', color: null },
        ].map(({ v, color, label }) => (
          <button key={v} style={{
            ...btnStyle(versionFilter === v),
            ...(color && versionFilter !== v ? { borderColor: color } : {}),
            ...(v === 'v7.1' && versionFilter !== v ? { color: 'rgba(168,85,247,0.7)' } : {}),
          }} onClick={() => setVersionFilter(v)}>{label || v}</button>
        ))}
      </div>
      <div style={{ marginLeft: 'auto', display: 'flex', gap: 4, alignItems: 'center' }}>
        <span style={{ fontSize: 9, color: T.label, fontFamily: T.mono }}>Sort:</span>
        {['newest', 'oldest'].map(s => (
          <button key={s} style={btnStyle(sortBy === s)} onClick={() => onSort(s)}>{s}</button>
        ))}
      </div>
    </div>
  );
}

// ─── Main Page ────────────────────────────────────────────────────────────────
export default function WindowResults() {
  const api = useApi();
  const [outcomes, setOutcomes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [lastRefresh, setLastRefresh] = useState(null);
  const [expandedTs, setExpandedTs] = useState(null);
  const [filter, setFilter] = useState('ALL');
  const [versionFilter, setVersionFilter] = useState('ALL');
  const [sortBy, setSortBy] = useState('newest');

  const fetchData = useCallback(async () => {
    try {
      const r = await api('GET', '/v58/outcomes?limit=100');
      setOutcomes(r?.data?.outcomes ?? []);
      setLastRefresh(new Date());
    } catch (err) {
      console.error('[WindowResults] fetch error:', err);
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    fetchData();
    const id = setInterval(fetchData, 30000);
    return () => clearInterval(id);
  }, [fetchData]);

  // Filter logic
  const filtered = useMemo(() => {
    let items = [...outcomes];

    // Version filter — v7.1 is the current engine, show it by default highlighted
    if (versionFilter !== 'ALL') {
      items = items.filter(o => o.engine_version === versionFilter);
    }

    if (filter === 'WINS') {
      items = items.filter(o => o.v57c_correct === true || o.v58_correct === true);
    } else if (filter === 'LOSSES') {
      items = items.filter(o => o.v57c_correct === false || o.v58_correct === false);
    } else if (filter === 'SKIPPED') {
      items = items.filter(o => !o.trade_placed || o.skip_reason);
    } else if (filter === 'PENDING') {
      items = items.filter(o => !o.actual_direction);
    }

    if (sortBy === 'oldest') {
      items = [...items].reverse();
    }

    return items;
  }, [outcomes, filter, versionFilter, sortBy]);

  // Summary stats — computed on filtered set so version filter affects stats
  const stats = useMemo(() => {
    const base = filtered;
    const resolved = base.filter(o => o.actual_direction);
    const wins = resolved.filter(o => o.v57c_correct === true).length;
    const losses = resolved.filter(o => o.v57c_correct === false).length;
    // Shadow P&L: what-if P&L if we had traded every signal (gated or not)
    const shadowPnl = base
      .filter(o => o.ungated_pnl != null)
      .map(o => o.ungated_pnl)
      .reduce((a, b) => a + b, 0);
    // Real P&L: only actual trades
    const realPnl = base
      .map(o => o.v58_pnl)
      .filter(p => p != null)
      .reduce((a, b) => a + b, 0);
    
    // v7.1 Retroactive analysis (only count resolved windows)
    const v71eligible = base.filter(o => o.v71_would_trade === true);
    const v71resolved = v71eligible.filter(o => o.v71_correct !== null && o.v71_correct !== undefined);
    const v71wins = v71resolved.filter(o => o.v71_correct === true).length;
    const v71losses = v71resolved.filter(o => o.v71_correct === false).length;
    const v71pnl = v71eligible
      .map(o => o.v71_pnl)
      .filter(p => p != null)
      .reduce((a, b) => a + b, 0);
    
    const v71count = base.filter(o => o.engine_version === 'v7.1').length;
    return {
      total: base.length,
      resolved: resolved.length,
      wins,
      losses,
      totalPnl: realPnl,
      shadowPnl,
      v71count,
      v71eligible: v71eligible.length,
      v71resolved: v71resolved.length,
      v71wins,
      v71losses,
      v71pnl,
    };
  }, [filtered]);

  const toggleExpand = (ts) => {
    setExpandedTs(prev => prev === ts ? null : ts);
  };

  return (
    <div style={{
      background: T.bg,
      minHeight: '100vh',
      fontFamily: T.mono,
      color: '#fff',
      padding: '0 0 40px',
    }}>
      <style>{`
        @media (max-width: 768px) {
          .wr-card-grid { grid-template-columns: auto 1fr !important; }
        }
      `}</style>

      {/* Header */}
      <div style={{
        background: 'rgba(255,255,255,0.018)',
        borderBottom: `1px solid ${T.border}`,
        padding: '12px 20px',
        display: 'flex',
        alignItems: 'center',
        gap: 16,
        flexWrap: 'wrap',
      }}>
        <span style={{ color: T.purple, fontSize: 13, fontWeight: 700, letterSpacing: '0.08em' }}>
          📊 WINDOW RESULTS
        </span>

        {/* Quick stats */}
        {[
          { label: 'Total', value: stats.total, color: '#fff' },
          { label: 'Wins', value: stats.wins, color: T.profit },
          { label: 'Losses', value: stats.losses, color: T.loss },
          {
            label: 'Accuracy',
            value: stats.resolved > 0 ? `${Math.round(stats.wins / stats.resolved * 100)}%` : '—',
            color: stats.wins / (stats.resolved || 1) >= 0.9 ? T.profit : (stats.wins / (stats.resolved || 1) >= 0.7 ? T.amber : T.loss),
          },
          {
            label: 'Real P&L',
            value: stats.totalPnl >= 0 ? `+$${stats.totalPnl.toFixed(2)}` : `-$${Math.abs(stats.totalPnl).toFixed(2)}`,
            color: stats.totalPnl >= 0 ? T.profit : T.loss,
          },
          {
            label: 'Shadow P&L',
            title: 'What P&L would have been if all signals were traded (no gate)',
            value: stats.shadowPnl !== 0 ? (stats.shadowPnl >= 0 ? `+$${stats.shadowPnl.toFixed(2)}` : `-$${Math.abs(stats.shadowPnl).toFixed(2)}`) : '—',
            color: stats.shadowPnl >= 0 ? 'rgba(74,222,128,0.6)' : T.loss,
          },
          ...(stats.v71resolved > 0 ? [{
            label: 'v7.1 Retroactive WR',
            title: `v7.1 config on resolved windows (${stats.v71eligible} eligible, ${stats.v71resolved} resolved)`,
            value: `${Math.round(stats.v71wins / stats.v71resolved * 100)}% (${stats.v71wins}W / ${stats.v71losses}L)`,
            color: stats.v71wins / stats.v71resolved >= 0.7 ? T.profit : T.loss,
          }] : []),
        ].map(({ label, value, color, title }) => (
          <div key={label} title={title || ''} style={{
            background: label.includes('v7.1') ? 'rgba(168,85,247,0.08)' : T.card,
            border: `1px solid ${label.includes('v7.1') ? 'rgba(168,85,247,0.3)' : T.border}`,
            borderRadius: 6,
            padding: '4px 12px',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}>
            <span style={{ fontSize: 9, color: T.label, textTransform: 'uppercase', letterSpacing: '0.05em' }}>{label}</span>
            <span style={{ fontSize: 12, fontWeight: 700, color }}>{value}</span>
          </div>
        ))}

        {lastRefresh && (
          <span style={{ fontSize: 9, color: T.label, marginLeft: 'auto' }}>
            {lastRefresh.toLocaleTimeString('en-GB')}
          </span>
        )}
        {loading && <span style={{ fontSize: 10, color: T.purple }}>loading…</span>}
      </div>

      {/* Body */}
      <div style={{ padding: '20px' }}>
        <FilterBar
          filter={filter}
          onFilter={setFilter}
          sortBy={sortBy}
          onSort={setSortBy}
          count={filtered.length}
          versionFilter={versionFilter}
          setVersionFilter={setVersionFilter}
        />

        {loading && !outcomes.length ? (
          <div style={{ color: T.label, fontSize: 12, padding: 24, textAlign: 'center' }}>
            Loading window data…
          </div>
        ) : !filtered.length ? (
          <div style={{ color: T.label, fontSize: 12, padding: 24, textAlign: 'center' }}>
            No windows match this filter.
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {filtered.map(outcome => (
              <WindowCard
                key={outcome.window_ts}
                outcome={outcome}
                isExpanded={expandedTs === outcome.window_ts}
                onToggle={() => toggleExpand(outcome.window_ts)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
