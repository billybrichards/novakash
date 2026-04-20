/**
 * WindowResults.jsx — Window Results page (/windows)
 *
 * Two views on the same route:
 *
 *  1. "Lineup View" (default, post-2026-04-20 redesign) — consumes
 *     /v58/strategy-windows. One card per 5-min window, one row per
 *     strategy in the current lineup (v6_sniper LIVE + v5_ensemble /
 *     v4_fusion / v5_fresh GHOST). LIVE rows highlighted; GHOST rows
 *     show would-have-been outcomes so you can see if a shadow beat
 *     the LIVE strategy over the last N windows.
 *
 *  2. "Legacy View" — the original v5.7c / v5.8 / v7.1 aggregate from
 *     /v58/outcomes. Kept behind a toggle so historical reasoning
 *     remains auditable.
 *
 * See docs/window-results-redesign-2026-04-20.md for the full design
 * rationale, API contract, and roadmap for endpoint enrichment
 * (fill_price / pnl_usd / conviction_bucket) not yet surfaced.
 */

import { useState, useEffect, useCallback, useMemo } from 'react';
import { useApi } from '../hooks/useApi.js';
import {
  CURRENT_LINEUP_IDS,
  LEGACY_LINEUP_IDS,
  getStrategyMeta,
} from '../constants/strategies.js';

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
  amber: '#f59e0b',
  label: 'rgba(255,255,255,0.35)',
  label2: 'rgba(255,255,255,0.55)',
  mono: "'IBM Plex Mono', monospace",
};

// Default view on page mount. Flip to 'legacy' to ship the old view first.
const DEFAULT_VIEW = 'lineup';

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

function fmtDeltaPct(openPrice, closePrice) {
  if (!openPrice || !closePrice) return null;
  return (closePrice - openPrice) / openPrice;
}

function dirColor(dir) {
  if (!dir) return T.label;
  return dir === 'UP' ? T.profit : T.loss;
}

function dirLabel(dir) {
  if (!dir) return '—';
  return dir === 'UP' ? '▲ UP' : '▼ DOWN';
}

function dirArrow(dir) {
  if (!dir) return '—';
  return dir === 'UP' ? '▲' : '▼';
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

// ─── Gate Badge (legacy) ──────────────────────────────────────────────────────
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

// ─── What-if Scenario Row (legacy) ────────────────────────────────────────────
function ScenarioRow({ label, icon, color, scenario }) {
  if (!scenario) return null;
  const { direction, entry_price, correct, pnl_usd } = scenario;
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
// Shared between Lineup View and Legacy View. Hits /v58/window-detail/{ts}
// which returns snapshot + evaluations + price_ticks + what_if + entry_timing
// for the legacy signal-source drilldown. The "gates per strategy" and
// "execution" tabs from the design doc are placeholders until the endpoint
// exposes per-strategy gate_results / fill_price / pnl_usd.
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
    // `api` is a stable useMemo'd value from useApi — fine to omit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
        <div>
          <div style={{ fontSize: 9, color: T.label, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 8, fontFamily: T.mono }}>
            Price Through Window
          </div>
          <SparkChart ticks={price_ticks} />
        </div>

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

      {/* Legacy decision reasoning (VPIN / Delta / Gamma / v7.1) — surfaces */}
      {/* historical gate state. In lineup view we also show this, but once  */}
      {/* the per-strategy gates endpoint lands this block will be replaced   */}
      {/* by the "Gates per strategy" tab described in the design doc.        */}
      {snapshot && (
        <div style={{
          background: 'rgba(168,85,247,0.04)', border: '1px solid rgba(168,85,247,0.15)',
          borderRadius: 10, padding: '12px 16px',
        }}>
          <div style={{ fontSize: 9, color: '#a855f7', letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 10, fontWeight: 700, fontFamily: T.mono }}>
            DECISION REASONING
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 8, marginBottom: 10 }}>
            <div style={{ background: 'rgba(0,0,0,0.2)', borderRadius: 6, padding: '8px 10px' }}>
              <div style={{ fontSize: 8, color: T.label }}>VPIN</div>
              <div style={{
                fontSize: 16, fontWeight: 800, fontFamily: T.mono,
                color: (snapshot.vpin || 0) >= 0.45 ? T.profit : T.loss,
              }}>
                {(snapshot.vpin || 0).toFixed(3)}
              </div>
              <div style={{ fontSize: 8, color: T.label2 }}>
                {(snapshot.vpin || 0) >= 0.65 ? '🔴 CASCADE' : (snapshot.vpin || 0) >= 0.55 ? '🟡 TRANSITION' : (snapshot.vpin || 0) >= 0.45 ? '🟢 NORMAL' : '⛔ BELOW GATE'}
              </div>
            </div>
            <div style={{ background: 'rgba(0,0,0,0.2)', borderRadius: 6, padding: '8px 10px' }}>
              <div style={{ fontSize: 8, color: T.label }}>Delta (T-60)</div>
              <div style={{
                fontSize: 16, fontWeight: 800, fontFamily: T.mono,
                color: Math.abs(snapshot.delta_pct || 0) >= 0.02 ? T.profit : T.loss,
              }}>
                {(snapshot.delta_pct || 0) >= 0 ? '+' : ''}{(snapshot.delta_pct || 0).toFixed(4)}%
              </div>
              <div style={{ fontSize: 8, color: T.label2 }}>
                {Math.abs(snapshot.delta_pct || 0) >= 0.02 ? '✓ above threshold' : '✗ below 0.02%'}
              </div>
            </div>
            <div style={{ background: 'rgba(0,0,0,0.2)', borderRadius: 6, padding: '8px 10px' }}>
              <div style={{ fontSize: 8, color: T.label }}>Gamma Prices</div>
              <div style={{ fontSize: 12, fontWeight: 700, fontFamily: T.mono, color: '#fff' }}>
                ↑${(snapshot.gamma_up_price || 0).toFixed(2)} ↓${(snapshot.gamma_down_price || 0).toFixed(2)}
              </div>
              <div style={{ fontSize: 8, color: T.label2 }}>
                Spread: {snapshot.gamma_up_price && snapshot.gamma_down_price
                  ? `${Math.abs(snapshot.gamma_up_price - snapshot.gamma_down_price).toFixed(2)}¢`
                  : '—'}
              </div>
            </div>
            <div style={{
              background: snapshot.v71_would_trade ? 'rgba(74,222,128,0.08)' : 'rgba(248,113,113,0.08)',
              borderRadius: 6, padding: '8px 10px',
              border: `1px solid ${snapshot.v71_would_trade ? 'rgba(74,222,128,0.2)' : 'rgba(248,113,113,0.2)'}`,
            }}>
              <div style={{ fontSize: 8, color: T.label }}>v7.1 (legacy)</div>
              <div style={{
                fontSize: 14, fontWeight: 800, fontFamily: T.mono,
                color: snapshot.v71_would_trade ? T.profit : T.loss,
              }}>
                {snapshot.v71_would_trade ? '✅ TRADE' : '🚫 SKIP'}
              </div>
              <div style={{ fontSize: 8, color: T.label2 }}>
                {snapshot.v71_skip_reason || (snapshot.v71_regime ? `${snapshot.v71_regime} regime` : '')}
              </div>
            </div>
          </div>

          {snapshot.skip_reason && (
            <div style={{
              padding: '6px 10px', borderRadius: 6, marginBottom: 6,
              background: snapshot.skip_reason.includes('CG VETO') ? 'rgba(234,179,8,0.08)' : 'rgba(248,113,113,0.06)',
              border: `1px solid ${snapshot.skip_reason.includes('CG VETO') ? 'rgba(234,179,8,0.2)' : 'rgba(248,113,113,0.15)'}`,
              fontSize: 9, color: snapshot.skip_reason.includes('CG VETO') ? T.warning : T.label2, fontFamily: T.mono,
            }}>
              {snapshot.skip_reason.includes('CG VETO') ? '🛡️ ' : '⏭ '}{snapshot.skip_reason}
            </div>
          )}

          {snapshot.trade_placed && snapshot.poly_outcome && (
            <div style={{
              padding: '6px 10px', borderRadius: 6,
              background: snapshot.poly_outcome === 'WIN' ? 'rgba(74,222,128,0.1)' : 'rgba(248,113,113,0.1)',
              border: `1px solid ${snapshot.poly_outcome === 'WIN' ? 'rgba(74,222,128,0.3)' : 'rgba(248,113,113,0.3)'}`,
              fontSize: 10, fontWeight: 700, fontFamily: T.mono,
              color: snapshot.poly_outcome === 'WIN' ? T.profit : T.loss,
            }}>
              {snapshot.poly_outcome === 'WIN' ? '✅' : '❌'} Polymarket: {snapshot.poly_outcome}
              {snapshot.v58_pnl != null && ` — P&L: ${snapshot.v58_pnl >= 0 ? '+' : ''}$${snapshot.v58_pnl.toFixed(2)}`}
            </div>
          )}
        </div>
      )}

      {what_if && (
        <div>
          <div style={{
            display: 'flex', alignItems: 'center', gap: 10,
            marginBottom: 10,
          }}>
            <div style={{ fontSize: 9, color: T.label, letterSpacing: '0.1em', textTransform: 'uppercase', fontFamily: T.mono }}>
              What-If P&amp;L ($10 bet)
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

      {/* Entry timing band (T-240 → T-60) — preserved from legacy view */}
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
            {entry_timing.map((et) => {
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
                  <div style={{ fontSize: 10, fontWeight: 700, color: T.cyan, marginBottom: 6, letterSpacing: '0.04em' }}>
                    {et.stage}
                  </div>
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
                  {entry != null && (
                    <div style={{ fontSize: 9, color: T.label, marginBottom: 2 }}>
                      entry {entry.toFixed(3)}
                    </div>
                  )}
                  {pnl != null ? (
                    <div style={{
                      fontSize: 12, fontWeight: 700,
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


// ═══════════════════════════════════════════════════════════════════════════
//  LINEUP VIEW — per-strategy rows from /v58/strategy-windows
// ═══════════════════════════════════════════════════════════════════════════

// Pill shown to the right of a strategy row showing TRADE/SKIP + direction.
function ActionPill({ action, direction, outcome, isLive }) {
  let bg = 'rgba(255,255,255,0.04)';
  let color = T.label;
  let label = '—';

  if (action === 'TRADE' && direction) {
    if (outcome === 'WIN') {
      bg = 'rgba(74,222,128,0.18)'; color = T.profit;
    } else if (outcome === 'LOSS') {
      bg = 'rgba(248,113,113,0.18)'; color = T.loss;
    } else {
      bg = 'rgba(168,85,247,0.14)'; color = T.purple;
    }
    label = `${dirArrow(direction)} ${direction}`;
  } else if (action === 'SKIP') {
    bg = 'rgba(255,255,255,0.04)'; color = T.label2;
    label = 'SKIP';
  }

  return (
    <span style={{
      padding: '2px 8px',
      borderRadius: 4,
      background: bg,
      color,
      fontSize: 10,
      fontWeight: isLive ? 800 : 600,
      fontFamily: T.mono,
      letterSpacing: '0.05em',
      minWidth: 70,
      textAlign: 'center',
      display: 'inline-block',
    }}>
      {label}
    </span>
  );
}

// Right-side outcome cell: "WIN +$X" for LIVE fills, "would-WIN / would-LOSE"
// for GHOST shadow outcomes. Until the endpoint exposes fill_price/pnl_usd
// per strategy we only render the text form.
function OutcomeCell({ strat, isLive }) {
  const { action, outcome } = strat;
  if (action === 'SKIP') {
    return <span style={{ color: T.label, fontSize: 10, fontFamily: T.mono }}>—</span>;
  }
  if (!outcome || outcome === 'SKIP') {
    return <span style={{ color: T.label, fontSize: 10, fontFamily: T.mono }}>pending</span>;
  }
  const win = outcome === 'WIN';
  const prefix = isLive ? (win ? '✅ WIN' : '❌ LOSS') : (win ? 'would-WIN' : 'would-LOSE');
  return (
    <span style={{
      fontSize: isLive ? 11 : 10,
      fontWeight: isLive ? 800 : 600,
      color: win ? T.profit : T.loss,
      fontFamily: T.mono,
      letterSpacing: '0.04em',
    }}>
      {prefix}
    </span>
  );
}

function LineupStrategyRow({ strat, meta, isLast }) {
  const isLive = strat?.mode === 'LIVE';
  const accent = meta.color;

  if (!strat) {
    return (
      <div style={{
        display: 'grid',
        gridTemplateColumns: '140px 90px 1fr 120px 120px',
        gap: 10,
        padding: '7px 14px',
        alignItems: 'center',
        borderLeft: `3px solid rgba(100,116,139,0.2)`,
        opacity: 0.45,
        borderBottom: isLast ? 'none' : `1px dashed ${T.border}`,
      }}>
        <span style={{ fontSize: 11, fontFamily: T.mono, color: T.label }}>{meta.label}</span>
        <span style={{ fontSize: 9, fontFamily: T.mono, color: T.label }}>—</span>
        <span style={{ fontSize: 9, fontFamily: T.mono, color: T.label }}>no decision recorded</span>
        <span />
        <span style={{ fontSize: 10, fontFamily: T.mono, color: T.label }}>—</span>
      </div>
    );
  }

  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: '140px 90px 1fr 120px 120px',
      gap: 10,
      padding: '7px 14px',
      alignItems: 'center',
      borderLeft: `3px solid ${isLive ? accent : `${accent}44`}`,
      background: isLive ? `${accent}0d` : 'transparent',
      opacity: isLive ? 1 : 0.78,
      borderBottom: isLast ? 'none' : `1px dashed ${T.border}`,
    }}>
      {/* Strategy label */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <span style={{
          fontSize: 11,
          fontFamily: T.mono,
          color: isLive ? accent : T.label2,
          fontWeight: isLive ? 700 : 500,
        }}>
          {meta.label}
        </span>
      </div>

      {/* Mode tag */}
      <span style={{
        padding: '1px 6px',
        borderRadius: 3,
        fontSize: 8,
        fontWeight: 700,
        fontFamily: T.mono,
        letterSpacing: '0.08em',
        background: isLive ? 'rgba(74,222,128,0.14)' : 'rgba(255,255,255,0.05)',
        color: isLive ? T.profit : T.label2,
        textAlign: 'center',
      }}>
        {strat.mode}
      </span>

      {/* Skip reason / entry reason — truncate */}
      <span style={{
        fontSize: 9,
        fontFamily: T.mono,
        color: T.label2,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
      }} title={strat.skip_reason || strat.entry_reason || ''}>
        {strat.action === 'SKIP' ? (strat.skip_reason || 'skipped') : (strat.entry_reason || '')}
        {strat.eval_offset != null && (
          <span style={{ color: T.label, marginLeft: 8 }}>
            · T-{strat.eval_offset}
          </span>
        )}
        {strat.confidence_score != null && (
          <span style={{ color: T.label, marginLeft: 8 }}>
            · conf {Math.round(strat.confidence_score * 100)}%
          </span>
        )}
      </span>

      {/* Action pill (TRADE dir / SKIP) */}
      <ActionPill
        action={strat.action}
        direction={strat.direction}
        outcome={strat.outcome}
        isLive={isLive}
      />

      {/* Outcome */}
      <div style={{ textAlign: 'right' }}>
        <OutcomeCell strat={strat} isLive={isLive} />
      </div>
    </div>
  );
}

function LineupWindowCard({ window: w, isExpanded, onToggle, lineupIds, showLegacy }) {
  const time = w.window_ts
    ? new Date(w.window_ts * 1000).toLocaleString('en-GB', {
        month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit',
      })
    : '—';

  const delta = fmtDeltaPct(w.open_price, w.close_price);
  const actual = w.actual_direction;
  const hasPending = !actual;

  // Primary row = v6_sniper if present, else first LIVE, else first in
  // lineup. Drives the card-level border colour.
  const primaryId = lineupIds.find(id => w.strategies?.[id]?.mode === 'LIVE')
    ?? lineupIds[0];
  const primary = w.strategies?.[primaryId];
  const primaryOutcome = primary?.outcome;

  let cardBorderColor = T.border;
  let cardBg = T.card;
  if (!hasPending && primary?.action === 'TRADE') {
    if (primaryOutcome === 'WIN') {
      cardBorderColor = 'rgba(74,222,128,0.45)';
      cardBg = 'rgba(74,222,128,0.05)';
    } else if (primaryOutcome === 'LOSS') {
      cardBorderColor = 'rgba(248,113,113,0.45)';
      cardBg = 'rgba(248,113,113,0.05)';
    }
  }

  const visibleIds = showLegacy
    ? [...lineupIds, ...LEGACY_LINEUP_IDS]
    : lineupIds;

  return (
    <div style={{
      background: isExpanded ? 'rgba(168,85,247,0.06)' : cardBg,
      border: `1px solid ${isExpanded ? 'rgba(168,85,247,0.3)' : cardBorderColor}`,
      borderRadius: 12,
      overflow: 'hidden',
      transition: 'all 200ms ease-out',
    }}>
      {/* Header row — click to expand */}
      <div
        onClick={onToggle}
        style={{
          display: 'grid',
          gridTemplateColumns: '160px 1fr auto auto',
          gap: 12,
          padding: '12px 16px',
          alignItems: 'center',
          cursor: 'pointer',
        }}
      >
        <div style={{ fontFamily: T.mono }}>
          <div style={{ fontSize: 11, color: '#fff' }}>{time}</div>
          <div style={{ fontSize: 8, color: T.label, marginTop: 2, letterSpacing: '0.05em' }}>
            UTC · {w.asset}
          </div>
        </div>

        <div style={{ fontFamily: T.mono }}>
          <div style={{ fontSize: 11 }}>
            <span style={{ color: T.label2 }}>{fmt(w.open_price)}</span>
            <span style={{ color: T.label, margin: '0 6px' }}>→</span>
            <span style={{ color: '#fff' }}>{fmt(w.close_price)}</span>
            {delta != null && (
              <span style={{
                marginLeft: 8, fontSize: 10,
                color: delta >= 0 ? T.profit : T.loss,
                fontWeight: 700,
              }}>
                {fmtPct(delta)}
              </span>
            )}
          </div>
          <div style={{ fontSize: 9, color: T.label, marginTop: 3 }}>
            {w.vpin != null && <span>vpin {w.vpin.toFixed(3)}</span>}
            {w.regime && <span> · regime {w.regime}</span>}
            {w.delta_source && <span> · src {w.delta_source}</span>}
          </div>
        </div>

        {/* Actual direction */}
        <div>
          <div style={{ fontSize: 8, color: T.label, letterSpacing: '0.08em', marginBottom: 2 }}>
            ACTUAL
          </div>
          {actual ? (
            <span style={{
              padding: '3px 10px',
              borderRadius: 6,
              background: actual === 'UP' ? 'rgba(74,222,128,0.12)' : 'rgba(248,113,113,0.12)',
              color: actual === 'UP' ? T.profit : T.loss,
              fontSize: 11,
              fontWeight: 700,
              fontFamily: T.mono,
              whiteSpace: 'nowrap',
            }}>
              {dirLabel(actual)}
            </span>
          ) : (
            <span style={{ color: T.label, fontSize: 10, fontFamily: T.mono }}>pending</span>
          )}
        </div>

        <div style={{
          color: T.label,
          fontSize: 12,
          transition: 'transform 200ms ease-out',
          transform: isExpanded ? 'rotate(180deg)' : 'rotate(0deg)',
        }}>
          ▾
        </div>
      </div>

      {/* Per-strategy rows */}
      <div style={{ borderTop: `1px solid ${T.border}`, background: 'rgba(0,0,0,0.15)' }}>
        {visibleIds.map((id, idx) => {
          const strat = w.strategies?.[id];
          const meta = getStrategyMeta(id, idx);
          return (
            <LineupStrategyRow
              key={id}
              strat={strat}
              meta={meta}
              isLast={idx === visibleIds.length - 1}
            />
          );
        })}
      </div>

      {/* Expanded detail */}
      {isExpanded && <WindowDetail windowTs={w.window_ts} />}
    </div>
  );
}

// Compute per-strategy WR for the header.
function computeStrategyWR(windows, strategyId) {
  let wins = 0;
  let losses = 0;
  for (const w of windows) {
    const s = w.strategies?.[strategyId];
    if (!s || s.action !== 'TRADE') continue;
    if (s.outcome === 'WIN') wins++;
    else if (s.outcome === 'LOSS') losses++;
  }
  const resolved = wins + losses;
  return {
    wins, losses, resolved,
    wr: resolved > 0 ? Math.round((wins / resolved) * 100) : null,
  };
}

function LineupHeader({ windows, lineupIds, lastRefresh, loading }) {
  const stats = useMemo(() => {
    const perStrategy = {};
    for (const id of lineupIds) {
      perStrategy[id] = computeStrategyWR(windows, id);
    }
    const resolved = windows.filter(w => w.actual_direction).length;
    return { total: windows.length, resolved, perStrategy };
  }, [windows, lineupIds]);

  return (
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
        📊 WINDOW HISTORY
      </span>

      <div style={{
        background: T.card,
        border: `1px solid ${T.border}`,
        borderRadius: 6,
        padding: '4px 12px',
        display: 'flex', alignItems: 'center', gap: 8,
      }}>
        <span style={{ fontSize: 9, color: T.label, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Windows</span>
        <span style={{ fontSize: 12, fontWeight: 700, color: '#fff' }}>{stats.total}</span>
        <span style={{ fontSize: 9, color: T.label }}>({stats.resolved} resolved)</span>
      </div>

      {lineupIds.map((id, i) => {
        const meta = getStrategyMeta(id, i);
        const s = stats.perStrategy[id];
        const isLive = meta.defaultMode === 'LIVE';
        return (
          <div key={id} title={`${meta.label} — ${isLive ? 'LIVE' : 'GHOST shadow'}`} style={{
            background: isLive ? `${meta.color}14` : T.card,
            border: `1px solid ${isLive ? meta.color : `${meta.color}44`}`,
            borderRadius: 6,
            padding: '4px 12px',
            display: 'flex', alignItems: 'center', gap: 8,
          }}>
            <span style={{ fontSize: 9, color: meta.color, textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: isLive ? 700 : 500 }}>
              {meta.shortLabel || meta.label} {isLive ? '·LIVE' : ''}
            </span>
            <span style={{ fontSize: 12, fontWeight: 700, color: s.wr != null ? (s.wr >= 65 ? T.profit : s.wr >= 50 ? T.amber : T.loss) : T.label }}>
              {s.wr != null ? `${s.wr}%` : '—'}
            </span>
            <span style={{ fontSize: 9, color: T.label }}>
              ({s.wins}W/{s.losses}L)
            </span>
          </div>
        );
      })}

      {lastRefresh && (
        <span style={{ fontSize: 9, color: T.label, marginLeft: 'auto' }}>
          {lastRefresh.toLocaleTimeString('en-GB')}
        </span>
      )}
      {loading && <span style={{ fontSize: 10, color: T.purple }}>loading…</span>}
    </div>
  );
}

function LineupFilterBar({
  outcomeFilter, setOutcomeFilter,
  focusStrategy, setFocusStrategy,
  showLegacy, setShowLegacy,
  lineupIds,
  count,
  view, setView,
}) {
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
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 16 }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap',
      }}>
        <span style={{ fontSize: 10, color: T.label, fontFamily: T.mono, minWidth: 72 }}>
          {count} windows
        </span>

        {/* View toggle */}
        <div style={{ display: 'flex', gap: 4, borderRight: `1px solid ${T.border}`, paddingRight: 12 }}>
          <button style={btnStyle(view === 'lineup')} onClick={() => setView('lineup')}>
            LINEUP
          </button>
          <button style={btnStyle(view === 'legacy')} onClick={() => setView('legacy')}>
            LEGACY
          </button>
        </div>

        {/* Outcome filter — drives on primary (LIVE) row */}
        <div style={{ display: 'flex', gap: 4 }}>
          {['ALL', 'WIN', 'LOSS', 'SKIP', 'PENDING'].map(f => (
            <button key={f} style={btnStyle(outcomeFilter === f)} onClick={() => setOutcomeFilter(f)}>
              {f}
            </button>
          ))}
        </div>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 9, color: T.label, fontFamily: T.mono, minWidth: 72 }}>
          Focus:
        </span>
        <div style={{ display: 'flex', gap: 4 }}>
          <button
            style={btnStyle(focusStrategy === 'ALL')}
            onClick={() => setFocusStrategy('ALL')}
          >
            ALL
          </button>
          {lineupIds.map((id, i) => {
            const meta = getStrategyMeta(id, i);
            const isLive = meta.defaultMode === 'LIVE';
            return (
              <button
                key={id}
                style={{
                  ...btnStyle(focusStrategy === id),
                  borderColor: focusStrategy === id ? meta.color : (isLive ? meta.color : T.border),
                  color: focusStrategy === id ? meta.color : (isLive ? meta.color : T.label2),
                }}
                onClick={() => setFocusStrategy(id)}
                title={meta.description}
              >
                {meta.label} {isLive ? '·LIVE' : ''}
              </button>
            );
          })}
        </div>

        <label style={{
          marginLeft: 'auto',
          fontSize: 9, color: T.label2, fontFamily: T.mono,
          display: 'flex', alignItems: 'center', gap: 6,
          cursor: 'pointer',
        }}>
          <input
            type="checkbox"
            checked={showLegacy}
            onChange={e => setShowLegacy(e.target.checked)}
            style={{ accentColor: T.purple }}
          />
          show legacy (v4_down_only, v4_up_basic, v4_up_asian, v10_gate)
        </label>
      </div>
    </div>
  );
}

function LineupView() {
  const api = useApi();
  const [windows, setWindows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [lastRefresh, setLastRefresh] = useState(null);
  const [expandedTs, setExpandedTs] = useState(null);
  const [outcomeFilter, setOutcomeFilter] = useState('ALL');
  const [focusStrategy, setFocusStrategy] = useState('ALL');
  const [showLegacy, setShowLegacy] = useState(false);
  const [view, setView] = useState('lineup');

  const lineupIds = CURRENT_LINEUP_IDS;

  const fetchData = useCallback(async () => {
    try {
      const r = await api('GET', '/v58/strategy-windows?limit=100&asset=BTC');
      setWindows(r?.data?.windows ?? []);
      setLastRefresh(new Date());
    } catch (err) {
      console.error('[WindowResults.Lineup] fetch error:', err);
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    fetchData();
    const id = setInterval(fetchData, 30000);
    return () => clearInterval(id);
  }, [fetchData]);

  const filtered = useMemo(() => {
    const items = [...windows];
    const keyStrategy = focusStrategy === 'ALL'
      ? (lineupIds.find(id => items[0]?.strategies?.[id]?.mode === 'LIVE') || lineupIds[0])
      : focusStrategy;

    return items.filter(w => {
      const s = w.strategies?.[keyStrategy];
      if (outcomeFilter === 'ALL') return true;
      if (outcomeFilter === 'PENDING') return !w.actual_direction;
      if (outcomeFilter === 'SKIP') return !s || s.action === 'SKIP';
      if (!s || s.action !== 'TRADE') return false;
      if (outcomeFilter === 'WIN') return s.outcome === 'WIN';
      if (outcomeFilter === 'LOSS') return s.outcome === 'LOSS';
      return true;
    });
  }, [windows, outcomeFilter, focusStrategy, lineupIds]);

  const toggleExpand = (ts) => {
    setExpandedTs(prev => prev === ts ? null : ts);
  };

  return (
    <>
      <LineupHeader
        windows={windows}
        lineupIds={lineupIds}
        lastRefresh={lastRefresh}
        loading={loading}
      />

      <div style={{ padding: '20px' }}>
        <LineupFilterBar
          outcomeFilter={outcomeFilter}
          setOutcomeFilter={setOutcomeFilter}
          focusStrategy={focusStrategy}
          setFocusStrategy={setFocusStrategy}
          showLegacy={showLegacy}
          setShowLegacy={setShowLegacy}
          lineupIds={lineupIds}
          count={filtered.length}
          view={view}
          setView={setView}
        />

        {view === 'legacy' ? (
          <LegacyView />
        ) : loading && !windows.length ? (
          <div style={{ color: T.label, fontSize: 12, padding: 24, textAlign: 'center' }}>
            Loading window data…
          </div>
        ) : !filtered.length ? (
          <div style={{ color: T.label, fontSize: 12, padding: 24, textAlign: 'center' }}>
            No windows match this filter.
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {filtered.map(w => (
              <LineupWindowCard
                key={w.window_ts}
                window={w}
                isExpanded={expandedTs === w.window_ts}
                onToggle={() => toggleExpand(w.window_ts)}
                lineupIds={lineupIds}
                showLegacy={showLegacy}
              />
            ))}
          </div>
        )}
      </div>
    </>
  );
}


// ═══════════════════════════════════════════════════════════════════════════
//  LEGACY VIEW — v5.7c / v5.8 / v7.1 aggregate from /v58/outcomes
// ═══════════════════════════════════════════════════════════════════════════

function LegacyWindowCard({ outcome, isExpanded, onToggle }) {
  const {
    window_ts, open_price, close_price, delta_pct,
    actual_direction, direction, skip_reason, trade_placed,
    timesfm_direction, timesfm_correct,
    twap_direction, twap_correct,
    v57c_correct,
    v58_would_trade, v58_correct, v58_pnl,
    engine_version,
    v71_would_trade, v71_correct, v71_regime, v71_skip_reason,
    poly_outcome,
  } = outcome;

  const time = window_ts
    ? new Date(window_ts).toLocaleString('en-GB', {
        month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit',
      })
    : '—';

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
        </div>

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
          <div style={{ display: 'flex', gap: 8, marginTop: 4, flexWrap: 'wrap' }}>
            {direction && (
              <span style={{ fontSize: 9, color: T.purple, fontFamily: T.mono }}>
                v57c:{' '}
                <span style={{ color: dirColor(direction), fontWeight: 700 }}>
                  {dirArrow(direction)}
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
                  {dirArrow(timesfm_direction)}
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
                  {dirArrow(twap_direction)}
                </span>
                {twap_correct !== null && twap_correct !== undefined && (
                  <span> {twap_correct ? '✅' : '❌'}</span>
                )}
              </span>
            )}
          </div>
        </div>

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
              {dirLabel(actual_direction)}
            </span>
          ) : (
            <span style={{ color: T.label, fontSize: 10, fontFamily: T.mono }}>pending</span>
          )}
        </div>

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

        <GateBadge gateStatus={gateStatus} />

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

        <div style={{
          color: T.label,
          fontSize: 12,
          transition: 'transform 200ms ease-out',
          transform: isExpanded ? 'rotate(180deg)' : 'rotate(0deg)',
        }}>
          ▾
        </div>
      </div>

      {isExpanded && <WindowDetail windowTs={window_ts} />}
    </div>
  );
}

function LegacyFilterBar({ filter, onFilter, sortBy, onSort, count, versionFilter, setVersionFilter }) {
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
      display: 'flex', alignItems: 'center', gap: 8,
      flexWrap: 'wrap', marginBottom: 16,
    }}>
      <span style={{ fontSize: 10, color: T.label, fontFamily: T.mono }}>{count} windows</span>
      <div style={{ display: 'flex', gap: 4 }}>
        {['ALL', 'WINS', 'LOSSES', 'SKIPPED', 'PENDING'].map(f => (
          <button key={f} style={btnStyle(filter === f)} onClick={() => onFilter(f)}>{f}</button>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
        <span style={{ fontSize: 9, color: T.label, fontFamily: T.mono }}>Version:</span>
        {['ALL', 'v7.1', 'v5.8', 'v5.7c', 'v5.7', 'v5.0'].map(v => (
          <button
            key={v}
            style={btnStyle(versionFilter === v)}
            onClick={() => setVersionFilter(v)}
          >
            {v}
          </button>
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

function LegacyView() {
  const api = useApi();
  const [outcomes, setOutcomes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [expandedTs, setExpandedTs] = useState(null);
  const [filter, setFilter] = useState('ALL');
  const [versionFilter, setVersionFilter] = useState('ALL');
  const [sortBy, setSortBy] = useState('newest');

  const fetchData = useCallback(async () => {
    try {
      const r = await api('GET', '/v58/outcomes?limit=100');
      setOutcomes(r?.data?.outcomes ?? []);
    } catch (err) {
      console.error('[WindowResults.Legacy] fetch error:', err);
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    fetchData();
    const id = setInterval(fetchData, 30000);
    return () => clearInterval(id);
  }, [fetchData]);

  const filtered = useMemo(() => {
    let items = [...outcomes];
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
    if (sortBy === 'oldest') items = [...items].reverse();
    return items;
  }, [outcomes, filter, versionFilter, sortBy]);

  const toggleExpand = (ts) => {
    setExpandedTs(prev => prev === ts ? null : ts);
  };

  return (
    <>
      <LegacyFilterBar
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
          {filtered.map(o => (
            <LegacyWindowCard
              key={o.window_ts}
              outcome={o}
              isExpanded={expandedTs === o.window_ts}
              onToggle={() => toggleExpand(o.window_ts)}
            />
          ))}
        </div>
      )}
    </>
  );
}


// ═══════════════════════════════════════════════════════════════════════════
//  PAGE ROOT
// ═══════════════════════════════════════════════════════════════════════════

export default function WindowResults() {
  // View state lives in LineupView (the default). If the user switches to
  // 'legacy' we still render LineupView's filter bar — simpler than lifting
  // both views to share a parent router.
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
      {DEFAULT_VIEW === 'legacy' ? (
        <>
          <div style={{
            background: 'rgba(255,255,255,0.018)',
            borderBottom: `1px solid ${T.border}`,
            padding: '12px 20px',
          }}>
            <span style={{ color: T.purple, fontSize: 13, fontWeight: 700, letterSpacing: '0.08em' }}>
              📊 WINDOW RESULTS — LEGACY VIEW
            </span>
          </div>
          <div style={{ padding: '20px' }}><LegacyView /></div>
        </>
      ) : (
        <LineupView />
      )}
    </div>
  );
}
