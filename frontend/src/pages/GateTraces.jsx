import React, { useMemo, useState } from 'react';
import { useApi } from '../hooks/useApi.js';
import { useApiLoader } from '../hooks/useApiLoader.js';
import PageHeader from '../components/shared/PageHeader.jsx';
import EmptyState from '../components/shared/EmptyState.jsx';
import Loading from '../components/shared/Loading.jsx';
import FilterPills from '../components/shared/FilterPills.jsx';
import { T, wrColor } from '../theme/tokens.js';

/**
 * Gate Traces — per-gate pass/fail heatmap over the `gate_check_traces`
 * table (audit task #188).
 *
 * Layout mirrors `SignalExplorer.jsx`:
 *   PageHeader → FilterPills → Heatmap matrix → Recent-traces table
 *
 * The heatmap matrix is STRATEGIES × GATES, each cell showing pass_pct +
 * fired-count. Cell colour is driven by `wrColor(pct/100)`.
 *
 * Recent-traces table shows the latest 50 (strategy × window × offset)
 * gate chains. Clicking a row expands to show the per-gate trace +
 * observed/config JSON for drill-down.
 *
 * Data sources:
 *   /api/gate-traces/heatmap  — aggregated matrix
 *   /api/gate-traces/recent   — latest chains
 */

const TIMEFRAMES = [
  { label: '5m', value: '5m' },
  { label: '15m', value: '15m' },
];

const HOURS = [
  { label: '24h', value: 24 },
  { label: '72h', value: 72 },
  { label: '7d', value: 168 },
];

/** Format a window_ts (epoch seconds) as UTC HH:MM. */
function utcHHMM(wts) {
  if (!wts) return '—';
  const d = new Date(Number(wts) * 1000);
  if (Number.isNaN(d.getTime())) return '—';
  const h = String(d.getUTCHours()).padStart(2, '0');
  const m = String(d.getUTCMinutes()).padStart(2, '0');
  return `${h}:${m}`;
}

/**
 * Convert a raw skip_reason string into a human-readable label.
 * "vpin_too_low"            → "VPIN too low"
 * "conviction_below_threshold" → "Conviction below threshold"
 * Unknown / undefined       → "—"
 */
export function formatSkipReason(reason) {
  if (!reason) return '—';
  return reason
    .replace(/_/g, ' ')
    .replace(/\bvpin\b/gi, 'VPIN')
    .replace(/\bbtc\b/gi, 'BTC')
    .replace(/\bpnl\b/gi, 'PnL')
    .replace(/^\w/, (c) => c.toUpperCase());
}

/**
 * Return a full "why was this skipped?" sentence for a failed gate.
 * Passed gates return null — callers should fall back to their own display.
 */
export function humanSkipReason(reason) {
  if (!reason) return 'No skip reason recorded';
  return `Skipped: ${formatSkipReason(reason)}`;
}

/**
 * Build a ranked-list string for the heatmap cell tooltip's top_skip_reasons.
 * Returns an empty string when there are no skip reasons to show.
 *
 * @param {Array<{reason: string, n: number}>} topSkipReasons
 * @param {number} skippedCount  fired - passed
 */
export function formatTopSkipReasonsTooltip(topSkipReasons, skippedCount) {
  if (!Array.isArray(topSkipReasons) || topSkipReasons.length === 0) return '';
  const lines = topSkipReasons.map((r, i) => {
    const pctStr =
      skippedCount > 0 ? ` (${Math.round((100 * r.n) / skippedCount)}%)` : '';
    return `  ${i + 1}. ${formatSkipReason(r.reason)} — ${r.n}\xd7${pctStr}`;
  });
  return ['', 'Top skip reasons:', ...lines].join('\n');
}

/** Format an ISO string as YYYY-MM-DD HH:MM UTC. */
function utcDay(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const y = d.getUTCFullYear();
  const mo = String(d.getUTCMonth() + 1).padStart(2, '0');
  const dd = String(d.getUTCDate()).padStart(2, '0');
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mm = String(d.getUTCMinutes()).padStart(2, '0');
  return `${y}-${mo}-${dd} ${hh}:${mm}Z`;
}

export default function GateTraces() {
  const api = useApi();
  const [tf, setTf] = useState('5m');
  const [hours, setHours] = useState(24);
  const [strategyFilter, setStrategyFilter] = useState(null);
  const [expanded, setExpanded] = useState(null); // "strategy|window|offset" key

  // Heatmap aggregation.
  const heatmap = useApiLoader(
    (signal) => {
      const params = new URLSearchParams({
        timeframe: tf,
        hours: String(hours),
      });
      if (strategyFilter) params.set('strategy_id', strategyFilter);
      return api.get(`/api/gate-traces/heatmap?${params.toString()}`, { signal });
    },
    [tf, hours, strategyFilter]
  );

  // Recent gate chains for the drill-down table below the heatmap.
  const recent = useApiLoader(
    (signal) => {
      const params = new URLSearchParams({
        timeframe: tf,
        hours: String(hours),
        limit: '50',
      });
      if (strategyFilter) params.set('strategy_id', strategyFilter);
      return api.get(`/api/gate-traces/recent?${params.toString()}`, { signal });
    },
    [tf, hours, strategyFilter]
  );

  const hm = heatmap.data && typeof heatmap.data === 'object' ? heatmap.data : {};
  const strategies = Array.isArray(hm.strategies) ? hm.strategies : [];
  const gates = Array.isArray(hm.gates) ? hm.gates : [];
  const cells = Array.isArray(hm.cells) ? hm.cells : [];
  const window = hm.window || {};

  // Lookup: {strategy: {gate: cell}}
  const cellMap = useMemo(() => {
    const m = {};
    for (const c of cells) {
      (m[c.strategy] ??= {})[c.gate] = c;
    }
    return m;
  }, [cells]);

  // Filter-pill options — "all" + every discovered strategy. The filter
  // list must come from the CURRENT unfiltered server response, so we
  // build from the heatmap strategies list but also tolerate the
  // strategyFilter case (narrows to one row).
  const strategyOptions = useMemo(() => {
    const set = new Set(strategies);
    if (strategyFilter) set.add(strategyFilter);
    const sorted = Array.from(set).sort();
    return [
      { label: 'all', value: null },
      ...sorted.map((s) => ({ label: s, value: s })),
    ];
  }, [strategies, strategyFilter]);

  // Header stats: total cells, avg pass pct, fired-sum
  const headerStats = useMemo(() => {
    if (cells.length === 0) return null;
    let firedSum = 0;
    let passedSum = 0;
    for (const c of cells) {
      firedSum += c.fired || 0;
      passedSum += c.passed || 0;
    }
    const avgPct = firedSum > 0 ? (100 * passedSum) / firedSum : null;
    return { firedSum, passedSum, avgPct };
  }, [cells]);

  const recentData = recent.data && typeof recent.data === 'object' ? recent.data : {};
  const groups = Array.isArray(recentData.groups) ? recentData.groups : [];

  return (
    <div>
      <PageHeader
        tag="GATE TRACES · /gate-traces"
        title="Gate Traces"
        subtitle="Per-gate pass/fail heatmap across strategies. Source: gate_check_traces."
        right={
          <div style={{ fontSize: 11, color: T.label2, textAlign: 'right' }}>
            {strategies.length} strategies · {gates.length} gates ·{' '}
            {(window.row_count_raw || 0).toLocaleString()} rows
            {headerStats?.avgPct != null ? (
              <span style={{ marginLeft: 8 }}>
                · avg pass{' '}
                <span style={{ color: wrColor(headerStats.avgPct / 100) }}>
                  {headerStats.avgPct.toFixed(1)}%
                </span>
              </span>
            ) : null}
          </div>
        }
      />

      <div
        style={{
          display: 'flex',
          gap: 18,
          marginBottom: 12,
          flexWrap: 'wrap',
        }}
      >
        <FilterPills
          label="timeframe"
          options={TIMEFRAMES}
          value={tf}
          onChange={setTf}
        />
        <FilterPills
          label="hours"
          options={HOURS}
          value={hours}
          onChange={setHours}
        />
        <FilterPills
          label="strategy"
          options={strategyOptions}
          value={strategyFilter}
          onChange={setStrategyFilter}
        />
      </div>

      {heatmap.error ? (
        <div style={{ color: T.loss, fontSize: 12, marginBottom: 10 }}>
          Heatmap load error: {heatmap.error}
        </div>
      ) : null}
      {hm.error ? (
        <div style={{ color: T.warn, fontSize: 12, marginBottom: 10 }}>
          Server reported: {hm.error}
        </div>
      ) : null}

      {/* ── HEATMAP ── */}
      <div
        style={{
          background: T.card,
          border: `1px solid ${T.border}`,
          padding: 14,
          borderRadius: 2,
          marginBottom: 14,
        }}
      >
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'baseline',
            marginBottom: 10,
          }}
        >
          <div style={{ fontSize: 13 }}>Pass-rate matrix · strategy × gate</div>
          <div style={{ fontSize: 10, color: T.label }}>
            {window.earliest ? `${utcDay(window.earliest)} → ${utcDay(window.latest)}` : ''}
          </div>
        </div>

        {strategies.length === 0 || gates.length === 0 ? (
          heatmap.loading ? (
            <Loading />
          ) : (
            <EmptyState
              message="No gate traces match these filters."
              hint={
                hm.error
                  ? 'Server returned an error — see banner above.'
                  : 'Increase the hours window or clear the strategy filter.'
              }
            />
          )
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table
              style={{
                width: '100%',
                borderCollapse: 'collapse',
                fontSize: 11,
                tableLayout: 'fixed',
              }}
            >
              <thead>
                <tr
                  style={{
                    color: T.label,
                    fontSize: 10,
                    letterSpacing: '0.12em',
                  }}
                >
                  <th
                    style={{
                      textAlign: 'left',
                      padding: '6px 10px',
                      width: 180,
                      position: 'sticky',
                      left: 0,
                      background: T.bg,
                      zIndex: 1,
                    }}
                  >
                    STRATEGY
                  </th>
                  {gates.map((g) => (
                    <th
                      key={g}
                      style={{
                        textAlign: 'right',
                        padding: '6px 10px',
                        textTransform: 'uppercase',
                        fontWeight: 500,
                      }}
                    >
                      {g}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {strategies.map((s) => (
                  <tr key={s} style={{ borderTop: `1px solid ${T.border}` }}>
                    <td
                      style={{
                        padding: '6px 10px',
                        color: T.text,
                        position: 'sticky',
                        left: 0,
                        background: T.bg,
                        borderRight: `1px solid ${T.border}`,
                      }}
                    >
                      {s}
                    </td>
                    {gates.map((g) => {
                      const c = cellMap[s]?.[g];
                      if (!c || !c.fired) {
                        return (
                          <td
                            key={g}
                            style={{
                              textAlign: 'right',
                              padding: '6px 10px',
                              color: T.label,
                            }}
                          >
                            —
                          </td>
                        );
                      }
                      const pct = c.pass_pct;
                      const color = pct != null ? wrColor(pct / 100) : T.label;
                      const skipped = (c.fired || 0) - (c.passed || 0);
                      const title = [
                        `fired: ${c.fired}  passed: ${c.passed}  pass_pct: ${pct != null ? pct.toFixed(1) + '%' : '—'}`,
                        formatTopSkipReasonsTooltip(c.top_skip_reasons, skipped),
                      ]
                        .filter(Boolean)
                        .join('\n');
                      return (
                        <td
                          key={g}
                          title={title}
                          style={{
                            textAlign: 'right',
                            padding: '6px 10px',
                            color,
                            fontVariantNumeric: 'tabular-nums',
                            cursor: 'help',
                          }}
                        >
                          {pct != null ? `${pct.toFixed(1)}%` : '—'}
                          <span style={{ color: T.label, fontSize: 10 }}>
                            {' '}
                            ({c.fired})
                          </span>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ── RECENT GATE CHAINS ── */}
      <div
        style={{
          background: T.card,
          border: `1px solid ${T.border}`,
          padding: 14,
          borderRadius: 2,
        }}
      >
        <div style={{ fontSize: 13, marginBottom: 8 }}>
          Recent gate chains · {groups.length} shown
        </div>
        {recent.error ? (
          <div style={{ color: T.loss, fontSize: 12, marginBottom: 10 }}>
            Recent load error: {recent.error}
          </div>
        ) : null}

        {groups.length === 0 ? (
          recent.loading ? (
            <Loading />
          ) : (
            <EmptyState message="No recent gate chains in this window." />
          )
        ) : (
          <table
            style={{
              width: '100%',
              borderCollapse: 'collapse',
              fontSize: 11.5,
            }}
          >
            <thead>
              <tr
                style={{
                  color: T.label,
                  fontSize: 10,
                  letterSpacing: '0.12em',
                }}
              >
                <th style={{ textAlign: 'left', padding: '7px 10px' }}>TIME</th>
                <th style={{ textAlign: 'left', padding: '7px 10px' }}>STRATEGY</th>
                <th style={{ textAlign: 'right', padding: '7px 10px' }}>OFFSET</th>
                <th style={{ textAlign: 'left', padding: '7px 10px' }}>ACTION</th>
                <th style={{ textAlign: 'left', padding: '7px 10px' }}>DIR</th>
                <th style={{ textAlign: 'left', padding: '7px 10px' }}>GATES</th>
              </tr>
            </thead>
            <tbody>
              {groups.map((g, i) => {
                const key = `${g.strategy_id}|${g.window_ts}|${g.eval_offset}`;
                const isOpen = expanded === key;
                const action = g.action || '';
                const actionColor =
                  action === 'TRADE'
                    ? T.profit
                    : action === 'SKIP'
                    ? T.label2
                    : T.warn;
                return (
                  <React.Fragment key={key}>
                    <tr
                      onClick={() => setExpanded(isOpen ? null : key)}
                      style={{
                        borderTop: `1px solid ${T.border}`,
                        cursor: 'pointer',
                        background: isOpen ? 'rgba(168,85,247,0.06)' : undefined,
                      }}
                    >
                      <td
                        style={{
                          padding: '7px 10px',
                          color: T.text,
                          fontVariantNumeric: 'tabular-nums',
                        }}
                      >
                        {utcHHMM(g.window_ts)}
                      </td>
                      <td style={{ padding: '7px 10px' }}>{g.strategy_id}</td>
                      <td
                        style={{
                          padding: '7px 10px',
                          textAlign: 'right',
                          color: T.label2,
                          fontVariantNumeric: 'tabular-nums',
                        }}
                      >
                        T-{g.eval_offset}
                      </td>
                      <td
                        style={{
                          padding: '7px 10px',
                          color: actionColor,
                          fontWeight: 500,
                        }}
                      >
                        {action || '—'}
                      </td>
                      <td style={{ padding: '7px 10px', color: T.label2 }}>
                        {g.direction || '—'}
                      </td>
                      <td style={{ padding: '7px 10px' }}>
                        {(g.gates || []).map((gt, gi) => (
                          <span
                            key={gi}
                            title={`${gt.gate_name}: ${gt.reason || gt.skip_reason || (gt.passed ? 'passed' : 'failed')}`}
                            style={{
                              display: 'inline-block',
                              width: 10,
                              height: 10,
                              borderRadius: 2,
                              marginRight: 3,
                              background: gt.passed ? T.profit : T.loss,
                              opacity: 0.85,
                            }}
                          />
                        ))}
                        <span
                          style={{
                            color: T.label,
                            fontSize: 10,
                            marginLeft: 6,
                          }}
                        >
                          {(g.gates || []).length} gate{(g.gates || []).length === 1 ? '' : 's'}
                        </span>
                      </td>
                    </tr>
                    {isOpen ? (
                      <tr>
                        <td
                          colSpan={6}
                          style={{
                            padding: 0,
                            background: 'rgba(0,0,0,0.25)',
                            borderTop: `1px solid ${T.border}`,
                          }}
                        >
                          <div style={{ padding: '10px 14px' }}>
                            <table
                              style={{
                                width: '100%',
                                borderCollapse: 'collapse',
                                fontSize: 11,
                              }}
                            >
                              <thead>
                                <tr
                                  style={{
                                    color: T.label,
                                    fontSize: 9,
                                    letterSpacing: '0.12em',
                                  }}
                                >
                                  <th style={{ textAlign: 'left', padding: '4px 8px' }}>#</th>
                                  <th style={{ textAlign: 'left', padding: '4px 8px' }}>GATE</th>
                                  <th style={{ textAlign: 'left', padding: '4px 8px' }}>STATUS</th>
                                  <th style={{ textAlign: 'left', padding: '4px 8px' }}>REASON</th>
                                  <th style={{ textAlign: 'left', padding: '4px 8px' }}>OBSERVED</th>
                                  <th style={{ textAlign: 'left', padding: '4px 8px' }}>CONFIG</th>
                                </tr>
                              </thead>
                              <tbody>
                                {(g.gates || []).map((gt, gi) => (
                                  <tr
                                    key={gi}
                                    style={{
                                      borderTop: `1px solid ${T.border}`,
                                    }}
                                  >
                                    <td
                                      style={{
                                        padding: '4px 8px',
                                        color: T.label,
                                        fontVariantNumeric: 'tabular-nums',
                                      }}
                                    >
                                      {gt.gate_order}
                                    </td>
                                    <td style={{ padding: '4px 8px' }}>
                                      {gt.gate_name}
                                    </td>
                                    <td
                                      style={{
                                        padding: '4px 8px',
                                        color: gt.passed ? T.profit : T.loss,
                                        fontWeight: 500,
                                      }}
                                    >
                                      {gt.passed ? 'PASS' : 'FAIL'}
                                    </td>
                                    <td
                                      style={{
                                        padding: '4px 8px',
                                        color: gt.passed ? T.label2 : T.warn,
                                        fontSize: 10,
                                      }}
                                    >
                                      {gt.passed
                                        ? (gt.reason ? formatSkipReason(gt.reason) : '—')
                                        : humanSkipReason(gt.skip_reason || gt.reason)}
                                    </td>
                                    <td
                                      style={{
                                        padding: '4px 8px',
                                        color: T.label2,
                                        fontSize: 10,
                                        fontFamily: "'IBM Plex Mono', monospace",
                                        maxWidth: 320,
                                        overflow: 'hidden',
                                        textOverflow: 'ellipsis',
                                        whiteSpace: 'nowrap',
                                      }}
                                      title={JSON.stringify(gt.observed || {}, null, 2)}
                                    >
                                      {Object.keys(gt.observed || {}).length
                                        ? JSON.stringify(gt.observed).slice(0, 120)
                                        : '—'}
                                    </td>
                                    <td
                                      style={{
                                        padding: '4px 8px',
                                        color: T.label2,
                                        fontSize: 10,
                                        fontFamily: "'IBM Plex Mono', monospace",
                                        maxWidth: 240,
                                        overflow: 'hidden',
                                        textOverflow: 'ellipsis',
                                        whiteSpace: 'nowrap',
                                      }}
                                      title={JSON.stringify(gt.config || {}, null, 2)}
                                    >
                                      {Object.keys(gt.config || {}).length
                                        ? JSON.stringify(gt.config).slice(0, 80)
                                        : '—'}
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        </td>
                      </tr>
                    ) : null}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
