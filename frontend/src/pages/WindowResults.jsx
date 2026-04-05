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

  const { snapshot, evaluations, price_ticks, what_if } = detail;

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
  } = outcome;

  const time = window_ts
    ? new Date(window_ts).toLocaleString('en-GB', {
        month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit',
      })
    : '—';

  // Card color based on signal correctness (v58 preferred, else v57c)
  const isCorrect = v58_would_trade ? v58_correct : v57c_correct;
  const hasPending = !actual_direction;

  let cardBorderColor = T.border;
  let cardBg = T.card;
  if (!hasPending) {
    if (isCorrect === true) {
      cardBorderColor = 'rgba(74,222,128,0.25)';
      cardBg = 'rgba(74,222,128,0.04)';
    } else if (isCorrect === false) {
      cardBorderColor = 'rgba(248,113,113,0.25)';
      cardBg = 'rgba(248,113,113,0.04)';
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
                background: engine_version === 'v5.8' ? 'rgba(74,222,128,0.15)' : 'rgba(255,255,255,0.06)',
                color: engine_version === 'v5.8' ? '#4ade80' : T.label,
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

        {/* Gate badge */}
        <GateBadge gateStatus={gateStatus} />

        {/* v5.8 P&L */}
        <div style={{ textAlign: 'right', minWidth: 60 }}>
          {v58_pnl != null ? (
            <span style={{
              fontSize: 13, fontWeight: 700,
              color: v58_pnl >= 0 ? T.profit : T.loss,
              fontFamily: T.mono,
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
        {['ALL', 'v5.8', 'v5.7c', 'v5.7', 'v5.0'].map(v => (
          <button key={v} style={{
            ...btnStyle(versionFilter === v),
            ...(v === 'v5.8' && versionFilter !== v ? { borderColor: 'rgba(74,222,128,0.3)' } : {}),
          }} onClick={() => setVersionFilter(v)}>{v}</button>
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

    // Version filter
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

  // Summary stats
  const stats = useMemo(() => {
    const resolved = outcomes.filter(o => o.actual_direction);
    const wins = resolved.filter(o => o.v57c_correct === true).length;
    const losses = resolved.filter(o => o.v57c_correct === false).length;
    const totalPnl = outcomes
      .map(o => o.v58_pnl)
      .filter(p => p != null)
      .reduce((a, b) => a + b, 0);
    return { total: outcomes.length, resolved: resolved.length, wins, losses, totalPnl };
  }, [outcomes]);

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
            color: stats.wins / (stats.resolved || 1) >= 0.5 ? T.profit : T.loss,
          },
          {
            label: 'v5.8 P&L',
            value: stats.totalPnl >= 0 ? `+$${stats.totalPnl.toFixed(2)}` : `-$${Math.abs(stats.totalPnl).toFixed(2)}`,
            color: stats.totalPnl >= 0 ? T.profit : T.loss,
          },
        ].map(({ label, value, color }) => (
          <div key={label} style={{
            background: T.card,
            border: `1px solid ${T.border}`,
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
