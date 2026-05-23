// StrategiesScorecard — operator scorecard table for /monitor/strategies.
//
// Dashboard PR 1 (note #596). Read-only table:
//   * One row per registered strategy (LIVE + GHOST).
//   * Two stat windows side-by-side: today (24h) + week (7d).
//   * LIVE block sorts to the top; GHOST follows beneath a divider.
//   * Click a row -> drill-down stub (PR 2 will wire the detail page).
//
// Backend: GET /api/monitor/strategies/scorecard (hub/api/monitor_scorecard.py).
// Theme: pure tokens from theme/tokens.js — matches the rest of the new shell.

import React, { useMemo, useState } from 'react';
import { useApi } from '../../hooks/useApi.js';
import { useApiLoader } from '../../hooks/useApiLoader.js';
import PageHeader from '../../components/shared/PageHeader.jsx';
import EmptyState from '../../components/shared/EmptyState.jsx';
import Loading from '../../components/shared/Loading.jsx';
import { T, wrColor } from '../../theme/tokens.js';

// ─── Formatters ──────────────────────────────────────────────────────────────

function fmtPnl(n) {
  if (n == null || !Number.isFinite(Number(n))) return { text: '—', color: T.label2 };
  const v = Number(n);
  const sign = v > 0 ? '+' : (v < 0 ? '-' : '');
  const color = v > 0 ? T.profit : (v < 0 ? T.loss : T.label2);
  return { text: `${sign}$${Math.abs(v).toFixed(2)}`, color };
}

function fmtWr(pct) {
  if (pct == null || !Number.isFinite(Number(pct))) return { text: '—', color: T.label };
  const v = Number(pct);
  // wrColor expects a 0-1 fraction; the API returns 0-100.
  return { text: `${v.toFixed(1)}%`, color: wrColor(v / 100) };
}

function fmtWL(wins, losses, pending) {
  const w = Number(wins || 0);
  const l = Number(losses || 0);
  const p = Number(pending || 0);
  // Returns an array of {text, color} segments so the row can render colored runs.
  const out = [{ text: `${w}W`, color: T.profit }, { text: ' / ', color: T.label }];
  out.push({ text: `${l}L`, color: T.loss });
  if (p > 0) {
    out.push({ text: ' / ', color: T.label });
    out.push({ text: `${p}P`, color: T.warn });
  }
  return out;
}

function fmtLastFire(iso) {
  if (!iso) return { text: '—', color: T.label2 };
  const ms = new Date(iso).getTime();
  if (!Number.isFinite(ms)) return { text: '—', color: T.label2 };
  const dSec = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  let text;
  if (dSec < 60) text = `${dSec}s ago`;
  else if (dSec < 3600) text = `${Math.floor(dSec / 60)}m ago`;
  else if (dSec < 86400) text = `${Math.floor(dSec / 3600)}h ago`;
  else text = `${Math.floor(dSec / 86400)}d ago`;
  let color = T.profit;
  if (dSec > 180 * 60) color = T.loss;
  else if (dSec > 60 * 60) color = T.warn;
  else if (dSec > 10 * 60) color = T.label2;
  return { text, color };
}

function fmtThreshold(v) {
  if (v == null || !Number.isFinite(Number(v))) return '—';
  return Number(v).toFixed(3);
}

// ─── Mode pill ────────────────────────────────────────────────────────────────

function ModePill({ mode }) {
  const isLive = mode === 'LIVE';
  const isGhost = mode === 'GHOST';
  const color = isLive ? T.profit : (isGhost ? T.label2 : T.warn);
  const bg = isLive
    ? 'rgba(74, 222, 128, 0.08)'
    : (isGhost ? 'rgba(255,255,255,0.04)' : 'rgba(245, 158, 11, 0.08)');
  return (
    <span style={{
      display: 'inline-block',
      fontSize: 10,
      letterSpacing: '0.12em',
      fontWeight: 600,
      color,
      background: bg,
      border: `1px solid ${color}`,
      padding: '2px 6px',
      borderRadius: 2,
    }}>{mode}</span>
  );
}

// ─── Header + sortable column definitions ────────────────────────────────────

const COLS = [
  { key: 'strategy_id', label: 'Strategy', align: 'left',
    sortFn: (a, b) => a.strategy_id.localeCompare(b.strategy_id) },
  { key: 'asset', label: 'Asset', align: 'left',
    sortFn: (a, b) => (a.asset || '').localeCompare(b.asset || '') },
  { key: 'timescale', label: 'TF', align: 'left',
    sortFn: (a, b) => (a.timescale || '').localeCompare(b.timescale || '') },
  { key: 'today_pnl', label: 'PnL 24h', align: 'right',
    sortFn: (a, b) => (a.today.net_pnl_usd || 0) - (b.today.net_pnl_usd || 0) },
  { key: 'today_wr', label: 'WR 24h', align: 'right',
    sortFn: (a, b) => (a.today.wr_pct || 0) - (b.today.wr_pct || 0) },
  { key: 'today_wl', label: 'W/L 24h', align: 'right',
    sortFn: (a, b) => (a.today.fires || 0) - (b.today.fires || 0) },
  { key: 'week_pnl', label: 'PnL 7d', align: 'right',
    sortFn: (a, b) => (a.week.net_pnl_usd || 0) - (b.week.net_pnl_usd || 0) },
  { key: 'week_wr', label: 'WR 7d', align: 'right',
    sortFn: (a, b) => (a.week.wr_pct || 0) - (b.week.wr_pct || 0) },
  { key: 'week_wl', label: 'W/L 7d', align: 'right',
    sortFn: (a, b) => (a.week.fires || 0) - (b.week.fires || 0) },
  { key: 'up_th', label: 'UP th', align: 'right',
    sortFn: (a, b) => (a.up_threshold || 0) - (b.up_threshold || 0) },
  { key: 'last_fire', label: 'Last fire', align: 'right',
    sortFn: (a, b) => new Date(a.last_fire_at || 0) - new Date(b.last_fire_at || 0) },
];

const ROW_PAD = '8px 12px';

// ─── Table row ────────────────────────────────────────────────────────────────

function ScorecardRow({ s, onClick }) {
  const todayPnl = fmtPnl(s.today.net_pnl_usd);
  const weekPnl = fmtPnl(s.week.net_pnl_usd);
  const todayWr = fmtWr(s.today.wr_pct);
  const weekWr = fmtWr(s.week.wr_pct);
  const todayWL = fmtWL(s.today.wins, s.today.losses, s.today.pending);
  const weekWL = fmtWL(s.week.wins, s.week.losses, s.week.pending);
  const last = fmtLastFire(s.last_fire_at);

  return (
    <tr
      style={{
        cursor: 'pointer',
        borderBottom: `1px solid ${T.border}`,
      }}
      onClick={() => onClick?.(s)}
      onMouseEnter={(e) => { e.currentTarget.style.background = 'rgba(255,255,255,0.02)'; }}
      onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent'; }}
    >
      <td style={{ padding: ROW_PAD, color: T.text, fontWeight: 500 }}>
        <span style={{ marginRight: 8 }}><ModePill mode={s.mode} /></span>
        {s.strategy_id}
      </td>
      <td style={{ padding: ROW_PAD, color: T.label2 }}>{s.asset || '—'}</td>
      <td style={{ padding: ROW_PAD, color: T.label2 }}>{s.timescale || '—'}</td>
      <td style={{ padding: ROW_PAD, color: todayPnl.color, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
        {todayPnl.text}
      </td>
      <td style={{ padding: ROW_PAD, color: todayWr.color, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
        {todayWr.text}
      </td>
      <td style={{ padding: ROW_PAD, textAlign: 'right', fontVariantNumeric: 'tabular-nums', fontSize: 12 }}>
        {todayWL.map((seg, i) => (
          <span key={i} style={{ color: seg.color }}>{seg.text}</span>
        ))}
      </td>
      <td style={{ padding: ROW_PAD, color: weekPnl.color, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
        {weekPnl.text}
      </td>
      <td style={{ padding: ROW_PAD, color: weekWr.color, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
        {weekWr.text}
      </td>
      <td style={{ padding: ROW_PAD, textAlign: 'right', fontVariantNumeric: 'tabular-nums', fontSize: 12 }}>
        {weekWL.map((seg, i) => (
          <span key={i} style={{ color: seg.color }}>{seg.text}</span>
        ))}
      </td>
      <td style={{ padding: ROW_PAD, color: T.label2, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
        {fmtThreshold(s.up_threshold)}
      </td>
      <td style={{ padding: ROW_PAD, color: last.color, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
        {last.text}
      </td>
    </tr>
  );
}

function SectionDivider({ label, hint }) {
  return (
    <tr>
      <td colSpan={COLS.length}
          style={{
            padding: '14px 12px 6px',
            color: T.label,
            fontSize: 10,
            letterSpacing: '0.18em',
            textTransform: 'uppercase',
            borderBottom: `1px solid ${T.borderStrong}`,
            background: T.card,
          }}>
        {label}
        {hint ? <span style={{ color: T.label, marginLeft: 8, textTransform: 'none', letterSpacing: 0, fontSize: 10 }}>
          {hint}
        </span> : null}
      </td>
    </tr>
  );
}

// ─── Page ────────────────────────────────────────────────────────────────────

export default function StrategiesScorecard() {
  const api = useApi();
  const { data, error, loading, reload } = useApiLoader(
    (signal) => api.get('/api/monitor/strategies/scorecard', { signal }),
    []
  );

  // Local sort state. Default sort is the API's natural order (LIVE-pnl,
  // then GHOST-pnl); clicking a header overrides with explicit sort.
  const [sortKey, setSortKey] = useState(null);
  const [sortDir, setSortDir] = useState('desc');

  const scorecards = useMemo(() => {
    // useApiLoader unwraps {rows|trades|decisions|items} arrays. The
    // /api/monitor/strategies/scorecard payload uses `scorecards`, so the
    // hook leaves us with the full object — pluck the array here.
    if (!data) return null;
    if (Array.isArray(data)) return data;
    if (Array.isArray(data.scorecards)) return data.scorecards;
    return [];
  }, [data]);

  const fetchedAt = useMemo(() => {
    if (data && typeof data === 'object' && data.fetched_at) {
      try {
        return new Date(data.fetched_at).toLocaleTimeString();
      } catch { return null; }
    }
    return null;
  }, [data]);

  // Split into LIVE / GHOST blocks. Within each block, apply local sort if
  // user clicked a header; otherwise keep server-supplied order.
  const { live, ghost } = useMemo(() => {
    if (!scorecards) return { live: [], ghost: [] };
    const liveRows = scorecards.filter((s) => s.mode === 'LIVE');
    const ghostRows = scorecards.filter((s) => s.mode === 'GHOST');
    const other = scorecards.filter((s) => s.mode !== 'LIVE' && s.mode !== 'GHOST');
    // Treat any non-LIVE-non-GHOST (e.g. DISABLED) as GHOST-block tail.
    const ghostPlus = [...ghostRows, ...other];

    if (sortKey) {
      const col = COLS.find((c) => c.key === sortKey);
      if (col && col.sortFn) {
        const mul = sortDir === 'asc' ? 1 : -1;
        liveRows.sort((a, b) => mul * col.sortFn(a, b));
        ghostPlus.sort((a, b) => mul * col.sortFn(a, b));
      }
    }
    return { live: liveRows, ghost: ghostPlus };
  }, [scorecards, sortKey, sortDir]);

  function handleSort(key) {
    if (sortKey === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir('desc');
    }
  }

  function handleRowClick(s) {
    // PR 2 will wire to /monitor/strategies/:id detail. For now log so
    // operators can verify the row data is reaching the click handler.
    // eslint-disable-next-line no-console
    console.log('[scorecard] drill-down (PR 2 wires this):', s);
  }

  return (
    <div>
      <PageHeader
        tag="MONITOR"
        title="Strategy Scorecard"
        subtitle={fetchedAt ? `Fetched ${fetchedAt}${data?.cache_hit ? ' (cached)' : ''}` : 'Loading…'}
        right={
          <button
            onClick={reload}
            disabled={loading}
            style={{
              background: 'transparent',
              border: `1px solid ${T.border}`,
              color: T.label2,
              padding: '6px 14px',
              fontSize: 11,
              letterSpacing: '0.08em',
              cursor: loading ? 'not-allowed' : 'pointer',
              fontFamily: T.font,
            }}
          >
            {loading ? 'Loading…' : 'Refresh'}
          </button>
        }
      />

      {loading && !scorecards ? <Loading label="Loading strategies…" /> : null}

      {error ? (
        <div style={{ color: T.loss, fontSize: 12, padding: '12px 0' }}>
          Error loading scorecard: {String(error)}
        </div>
      ) : null}

      {!loading && scorecards && scorecards.length === 0 ? (
        <EmptyState
          message="No strategies registered."
          hint="The engine will populate strategy_configs on its next startup."
        />
      ) : null}

      {scorecards && scorecards.length > 0 ? (
        <div style={{
          border: `1px solid ${T.border}`,
          borderRadius: 2,
          overflow: 'auto',
          fontFamily: T.font,
          fontSize: 12,
        }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ borderBottom: `1px solid ${T.borderStrong}` }}>
                {COLS.map((c) => (
                  <th
                    key={c.key}
                    onClick={() => handleSort(c.key)}
                    style={{
                      padding: '10px 12px',
                      color: T.label,
                      fontSize: 10,
                      letterSpacing: '0.12em',
                      textTransform: 'uppercase',
                      textAlign: c.align,
                      fontWeight: 500,
                      cursor: 'pointer',
                      userSelect: 'none',
                    }}
                  >
                    {c.label}
                    {sortKey === c.key ? (
                      <span style={{ marginLeft: 4, color: T.label2 }}>
                        {sortDir === 'asc' ? '↑' : '↓'}
                      </span>
                    ) : null}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {live.length > 0 ? (
                <>
                  <SectionDivider label={`LIVE (${live.length})`} />
                  {live.map((s) => (
                    <ScorecardRow key={s.strategy_id} s={s} onClick={handleRowClick} />
                  ))}
                </>
              ) : null}
              {ghost.length > 0 ? (
                <>
                  <SectionDivider
                    label={`GHOST (${ghost.length})`}
                    hint="notional PnL — same fee math as LIVE, not executed"
                  />
                  {ghost.map((s) => (
                    <ScorecardRow key={s.strategy_id} s={s} onClick={handleRowClick} />
                  ))}
                </>
              ) : null}
            </tbody>
          </table>
        </div>
      ) : null}

      <div style={{ marginTop: 12, color: T.label, fontSize: 10, letterSpacing: '0.06em' }}>
        Read-only view. Mode flips happen via the override CRUD (/strategies/:id) — this page observes only.
      </div>
    </div>
  );
}
