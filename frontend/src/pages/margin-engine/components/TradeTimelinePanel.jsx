import React, { useEffect, useState, useMemo } from 'react';
import { T } from './constants.js';

/**
 * TradeTimelinePanel — paginated, filterable per-trade history.
 *
 * Distinct from PositionsPanel:
 *  - PositionsPanel = compact table for the live & history tabs (session memory)
 *  - TradeTimelinePanel = rich per-trade cards backed by /api/margin/positions/history
 *    (full DB history, not just the current session)
 *
 * Each card surfaces the entry/exit timings AND the conditions that triggered
 * each side of the trade — entry signal score, timescale, exit reason, hold
 * duration, fees paid, venue, and strategy version. Designed so an operator
 * can scan a list of trades and tell at a glance WHY each one opened and
 * closed without drilling into logs.
 *
 * No polling. Refetches only on filter or page change. The Trade Timeline
 * is "history" in the literal sense — it doesn't need 5s updates because
 * closed positions never change.
 */

const PAGE_SIZE = 25;

const SIDE_OPTIONS = [
  { value: '', label: 'All sides' },
  { value: 'LONG', label: 'LONG only' },
  { value: 'SHORT', label: 'SHORT only' },
];

const OUTCOME_OPTIONS = [
  { value: '', label: 'All outcomes' },
  { value: 'win', label: 'Wins only' },
  { value: 'loss', label: 'Losses only' },
];

const EXIT_REASON_OPTIONS = [
  { value: '', label: 'All exit reasons' },
  { value: 'TAKE_PROFIT', label: 'Take Profit' },
  { value: 'STOP_LOSS', label: 'Stop Loss' },
  { value: 'TRAILING_STOP', label: 'Trailing Stop' },
  { value: 'MAX_HOLD_TIME', label: 'Max Hold' },
  { value: 'SIGNAL_REVERSAL', label: 'Signal Reversal (legacy)' },
  // ── v4-aware exit reasons (PR B) ──
  { value: 'PROBABILITY_REVERSAL', label: 'Probability Reversal (v4)' },
  { value: 'REGIME_DETERIORATED', label: 'Regime Deteriorated (v4)' },
  { value: 'CONSENSUS_FAIL', label: 'Consensus Fail (v4)' },
  { value: 'MACRO_GATE_FLIP', label: 'Macro Gate Flip (v4)' },
  { value: 'EVENT_GUARD', label: 'Event Guard (v4)' },
  { value: 'CASCADE_EXHAUSTED', label: 'Cascade Exhausted (v4)' },
  { value: 'MANUAL', label: 'Manual' },
  { value: 'KILL_SWITCH', label: 'Kill Switch' },
];

// Color map for exit reason chips — extended for PR B v4 exits.
// Legacy and v4 reasons that share semantic meaning get the same color
// (e.g., STOP_LOSS and CONSENSUS_FAIL both red), so the color alone
// communicates "was this a risk-gate trip or a signal-based exit".
// Orange for REGIME_DETERIORATED is the only novel color — the T theme
// doesn't export T.orange by default, so we fall back to a literal hex
// that sits between amber and red on the warning spectrum.
const T_ORANGE = '#f97316';
const EXIT_REASON_COLOR = {
  TAKE_PROFIT: T.green,           // winner exit
  STOP_LOSS: T.red,               // loser exit
  TRAILING_STOP: T.green,         // locked-in winner
  MAX_HOLD_TIME: T.amber,         // time-based exit, not signal-driven
  SIGNAL_REVERSAL: T.purple,      // legacy composite flip
  PROBABILITY_REVERSAL: T.purple, // v4 ML signal flip
  REGIME_DETERIORATED: T_ORANGE,  // market state change
  CONSENSUS_FAIL: T.red,          // risk/infra gate
  MACRO_GATE_FLIP: T.purple,      // Claude flipped
  EVENT_GUARD: T.amber,           // preemptive, not loss-driven
  CASCADE_EXHAUSTED: T.red,       // preemptive but usually after a run
  MANUAL: T.textMuted,
  KILL_SWITCH: T.red,
};

// ── Helpers ───────────────────────────────────────────────────────────────

function formatTimestamp(iso) {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString('en-GB', {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hour12: false,
    });
  } catch {
    return iso;
  }
}

function formatHoldDuration(seconds) {
  if (seconds == null) return '—';
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rs = s % 60;
  if (m < 60) return `${m}m ${rs}s`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return `${h}h ${rm}m`;
}

function formatPrice(p) {
  if (p == null) return '—';
  return `$${Number(p).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`;
}

function formatBps(notional, fee) {
  if (!notional || notional <= 0) return '—';
  const bps = (fee / notional) * 10000;
  return `${bps.toFixed(1)}bp`;
}

// ── Sub-components ────────────────────────────────────────────────────────

function FilterBar({ filters, onChange, loading }) {
  const wrap = {
    display: 'flex', gap: 8, padding: 12, flexWrap: 'wrap',
    background: T.headerBg, borderBottom: `1px solid ${T.cardBorder}`,
  };
  const select = {
    fontSize: 10, padding: '5px 8px', borderRadius: 4,
    background: T.bg, color: T.text,
    border: `1px solid ${T.cardBorder}`, fontFamily: T.mono,
    cursor: loading ? 'not-allowed' : 'pointer', opacity: loading ? 0.6 : 1,
  };
  return (
    <div style={wrap}>
      <select
        value={filters.side || ''}
        onChange={e => onChange({ ...filters, side: e.target.value || null })}
        disabled={loading}
        style={select}
      >
        {SIDE_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
      <select
        value={filters.outcome || ''}
        onChange={e => onChange({ ...filters, outcome: e.target.value || null })}
        disabled={loading}
        style={select}
      >
        {OUTCOME_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
      <select
        value={filters.exit_reason || ''}
        onChange={e => onChange({ ...filters, exit_reason: e.target.value || null })}
        disabled={loading}
        style={select}
      >
        {EXIT_REASON_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
      {loading && (
        <span style={{ fontSize: 9, color: T.textMuted, alignSelf: 'center' }}>
          Loading…
        </span>
      )}
    </div>
  );
}

function SummaryStats({ rows, total }) {
  const stats = useMemo(() => {
    if (!rows || rows.length === 0) {
      return { count: 0, totalPnl: 0, winRate: 0, avgHold: 0, totalFees: 0 };
    }
    const wins = rows.filter(r => (r.realised_pnl || 0) > 0).length;
    const totalPnl = rows.reduce((s, r) => s + (r.realised_pnl || 0), 0);
    const totalFees = rows.reduce((s, r) => s + (r.total_commission || 0), 0);
    const avgHold = rows.reduce((s, r) => s + (r.hold_duration_s || 0), 0) / rows.length;
    return {
      count: rows.length,
      totalPnl,
      winRate: (wins / rows.length) * 100,
      avgHold,
      totalFees,
    };
  }, [rows]);

  const cell = {
    padding: '10px 14px', borderRight: `1px solid ${T.cardBorder}`, flex: 1,
  };
  const label = { fontSize: 9, color: T.textMuted, marginBottom: 4, letterSpacing: '0.05em' };
  const value = { fontSize: 13, fontFamily: T.mono, fontWeight: 700, color: T.text };

  return (
    <div style={{
      display: 'flex', background: T.card, borderBottom: `1px solid ${T.cardBorder}`,
    }}>
      <div style={cell}>
        <div style={label}>SHOWING</div>
        <div style={value}>{stats.count} of {total}</div>
      </div>
      <div style={cell}>
        <div style={label}>P&L (PAGE)</div>
        <div style={{ ...value, color: stats.totalPnl >= 0 ? T.green : T.red }}>
          {stats.totalPnl >= 0 ? '+' : ''}${stats.totalPnl.toFixed(2)}
        </div>
      </div>
      <div style={cell}>
        <div style={label}>WIN RATE (PAGE)</div>
        <div style={value}>{stats.winRate.toFixed(1)}%</div>
      </div>
      <div style={cell}>
        <div style={label}>AVG HOLD</div>
        <div style={value}>{formatHoldDuration(stats.avgHold)}</div>
      </div>
      <div style={{ ...cell, borderRight: 'none' }}>
        <div style={label}>FEES (PAGE)</div>
        <div style={value}>${stats.totalFees.toFixed(2)}</div>
      </div>
    </div>
  );
}

function TradeCard({ trade }) {
  const pnl = trade.realised_pnl || 0;
  const isWin = pnl > 0;
  const isLong = trade.side === 'LONG';

  // Compute P&L percentage of notional for context — small fee bands look
  // identical in absolute dollars but very different relative to position size.
  const pnlPct = trade.notional && trade.notional > 0
    ? (pnl / trade.notional) * 100
    : null;

  // Probability vs composite labelling — drives whether we show "p_up=" or "score="
  const isProbability = trade.strategy_version === 'v2-probability';
  const scoreLabel = isProbability ? 'p_up' : 'score';

  // Venue chip color: cyan for hyperliquid, amber for binance.
  const venueColor = trade.venue === 'hyperliquid' ? T.cyan : T.amber;
  const venueBgRgba = trade.venue === 'hyperliquid'
    ? 'rgba(6,182,212,0.15)'
    : 'rgba(245,158,11,0.15)';

  const card = {
    background: T.card, border: `1px solid ${T.cardBorder}`, borderRadius: 8,
    padding: 14, marginBottom: 10,
  };
  const headerRow = {
    display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12,
    paddingBottom: 10, borderBottom: `1px solid ${T.cardBorder}`,
  };
  const chip = (bg, color, border) => ({
    fontSize: 9, fontWeight: 800, padding: '3px 8px', borderRadius: 4,
    background: bg, color, border: `1px solid ${border}`,
    fontFamily: T.mono, letterSpacing: '0.04em', textTransform: 'uppercase',
  });
  const sideChip = chip(
    isLong ? 'rgba(16,185,129,0.15)' : 'rgba(239,68,68,0.15)',
    isLong ? T.green : T.red,
    isLong ? 'rgba(16,185,129,0.3)' : 'rgba(239,68,68,0.3)',
  );
  const venueChip = chip(venueBgRgba, venueColor, venueColor);

  const sectionLabel = {
    fontSize: 9, color: T.textMuted, fontWeight: 700, letterSpacing: '0.05em',
    marginBottom: 4,
  };
  const lineMain = {
    fontSize: 11, color: T.text, fontFamily: T.mono, marginBottom: 2,
  };
  const lineSub = {
    fontSize: 9, color: T.textMuted, fontFamily: T.mono,
  };

  return (
    <div style={card}>
      {/* Header: side · asset · strategy · pnl chip · venue chip */}
      <div style={headerRow}>
        <span style={sideChip}>{trade.side}</span>
        <span style={{ fontSize: 12, fontWeight: 700, color: T.text, fontFamily: T.mono }}>
          {trade.asset}USDT
        </span>
        <span style={{
          fontSize: 8, color: T.textMuted, fontFamily: T.mono,
          padding: '2px 6px', borderRadius: 3, background: 'rgba(100,116,139,0.15)',
        }}>
          {isProbability ? 'v2-ML' : 'v1-composite'}
        </span>
        <span style={{ flex: 1 }} />
        <span style={{
          fontSize: 13, fontFamily: T.mono, fontWeight: 800,
          color: isWin ? T.green : T.red,
        }}>
          {pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}
          {pnlPct != null && (
            <span style={{ fontSize: 10, marginLeft: 6, opacity: 0.7 }}>
              ({pnl >= 0 ? '+' : ''}{pnlPct.toFixed(2)}%)
            </span>
          )}
        </span>
        <span style={venueChip}>{trade.venue}</span>
      </div>

      {/* Two-column body: ENTRY / EXIT */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
        {/* Entry block */}
        <div>
          <div style={sectionLabel}>ENTRY</div>
          <div style={lineMain}>{formatTimestamp(trade.opened_at)}</div>
          <div style={lineMain}>@ {formatPrice(trade.entry_price)}</div>
          <div style={lineSub}>
            {scoreLabel}={trade.entry_signal_score?.toFixed(3) ?? '—'}
            {' · '}timescale={trade.entry_timescale ?? '—'}
            {' · '}lev={trade.leverage}x
          </div>
          {trade.notional != null && (
            <div style={lineSub}>
              notional ${Number(trade.notional).toFixed(2)}
              {trade.collateral != null && (
                <> {' · '}collateral ${Number(trade.collateral).toFixed(2)}</>
              )}
            </div>
          )}
          {/* v4 entry context — only rendered when the position was opened
              via the v4 gate stack (regime is the tombstone field). Legacy
              v2-path trades leave this block entirely off the card. */}
          {trade.v4_entry_regime && (
            <div style={{ ...lineSub, marginTop: 4, color: T.cyan, opacity: 0.85 }}>
              regime={trade.v4_entry_regime}
              {trade.v4_entry_macro_bias && (
                <>
                  {' · '}macro={trade.v4_entry_macro_bias}
                  {trade.v4_entry_macro_confidence != null && (
                    <>({trade.v4_entry_macro_confidence}%)</>
                  )}
                </>
              )}
              {trade.v4_entry_expected_move_bps != null && (
                <>{' · '}exp={Number(trade.v4_entry_expected_move_bps).toFixed(1)}bps</>
              )}
              {trade.v4_entry_consensus_safe != null && (
                <>{' · '}consensus={trade.v4_entry_consensus_safe ? 'safe' : 'UNSAFE'}</>
              )}
            </div>
          )}
        </div>

        {/* Exit block */}
        <div>
          <div style={sectionLabel}>EXIT</div>
          <div style={lineMain}>{formatTimestamp(trade.closed_at)}</div>
          <div style={lineMain}>@ {formatPrice(trade.exit_price)}</div>
          <div style={lineSub}>
            reason={' '}
            <span style={{
              color: EXIT_REASON_COLOR[trade.exit_reason] ?? T.textMuted,
              fontWeight: 700,
            }}>
              {trade.exit_reason ?? '—'}
            </span>
          </div>
          <div style={lineSub}>
            held {formatHoldDuration(trade.hold_duration_s)}
            {trade.continuation_count > 0 && (
              <span style={{ marginLeft: 6, color: T.cyan }}>
                · continued {trade.continuation_count}×
                {trade.last_continuation_p_up != null && (
                  <span style={{ opacity: 0.7 }}>
                    {' '}(last p_up={Number(trade.last_continuation_p_up).toFixed(3)})
                  </span>
                )}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Bottom bar: stops, fees, ids */}
      <div style={{
        marginTop: 12, paddingTop: 10,
        borderTop: `1px solid ${T.cardBorder}`,
        display: 'flex', flexWrap: 'wrap', gap: 14,
        fontSize: 9, fontFamily: T.mono, color: T.textMuted,
      }}>
        {trade.stop_loss_price != null && (
          <span>SL {formatPrice(trade.stop_loss_price)}</span>
        )}
        {trade.take_profit_price != null && (
          <span>TP {formatPrice(trade.take_profit_price)}</span>
        )}
        <span>
          fees ${trade.total_commission?.toFixed(4) ?? '0.0000'}
          {' '}({formatBps(trade.notional, trade.total_commission || 0)})
        </span>
        <span style={{ flex: 1 }} />
        <span style={{ opacity: 0.5 }}>id: {trade.id}</span>
      </div>
    </div>
  );
}

function Pagination({ page, totalPages, onPageChange, disabled }) {
  const btn = (active, disabled_) => ({
    fontSize: 10, padding: '6px 12px', borderRadius: 4,
    background: active ? 'rgba(6,182,212,0.15)' : 'transparent',
    color: active ? T.cyan : disabled_ ? T.textDim : T.text,
    border: `1px solid ${active ? 'rgba(6,182,212,0.3)' : T.cardBorder}`,
    cursor: disabled_ ? 'not-allowed' : 'pointer',
    fontFamily: T.mono, fontWeight: 700,
    opacity: disabled_ ? 0.4 : 1,
  });

  if (totalPages <= 1) return null;

  return (
    <div style={{
      display: 'flex', justifyContent: 'center', alignItems: 'center',
      gap: 6, padding: 14, borderTop: `1px solid ${T.cardBorder}`,
    }}>
      <button
        style={btn(false, disabled || page === 0)}
        onClick={() => onPageChange(page - 1)}
        disabled={disabled || page === 0}
      >
        ← prev
      </button>
      <span style={{ fontSize: 10, color: T.textMuted, fontFamily: T.mono, padding: '0 8px' }}>
        page {page + 1} of {totalPages}
      </span>
      <button
        style={btn(false, disabled || page >= totalPages - 1)}
        onClick={() => onPageChange(page + 1)}
        disabled={disabled || page >= totalPages - 1}
      >
        next →
      </button>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────

export default function TradeTimelinePanel({ api }) {
  const [filters, setFilters] = useState({
    side: null,
    outcome: null,
    exit_reason: null,
  });
  const [page, setPage] = useState(0);
  const [data, setData] = useState({ rows: [], total: 0 });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Reset to page 0 whenever filters change so users don't get stuck on
  // an out-of-bounds page after applying a more restrictive filter.
  const handleFilterChange = (newFilters) => {
    setFilters(newFilters);
    setPage(0);
  };

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    const params = { limit: PAGE_SIZE, offset: page * PAGE_SIZE };
    if (filters.side) params.side = filters.side;
    if (filters.outcome) params.outcome = filters.outcome;
    if (filters.exit_reason) params.exit_reason = filters.exit_reason;

    api('GET', '/margin/positions/history', { params })
      .then(res => {
        if (cancelled) return;
        setData(res.data || { rows: [], total: 0 });
      })
      .catch(e => {
        if (cancelled) return;
        const msg = e.response?.data?.detail || e.response?.data?.error || e.message;
        setError(msg);
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
      });

    return () => { cancelled = true; };
  }, [api, filters, page]);

  const totalPages = Math.max(1, Math.ceil((data.total || 0) / PAGE_SIZE));

  const wrap = {
    background: T.card, border: `1px solid ${T.cardBorder}`,
    borderRadius: 8, overflow: 'hidden',
  };
  const headerRow = {
    padding: '12px 16px', borderBottom: `1px solid ${T.cardBorder}`,
    background: T.headerBg,
    display: 'flex', alignItems: 'center', gap: 12,
  };

  return (
    <div style={wrap}>
      <div style={headerRow}>
        <span style={{ fontSize: 12, fontWeight: 700, color: T.text, letterSpacing: '0.05em' }}>
          TRADE TIMELINE
        </span>
        <span style={{ fontSize: 9, color: T.textMuted }}>
          per-trade entry &amp; exit conditions, full history
        </span>
        <span style={{ flex: 1 }} />
        {data.total > 0 && (
          <span style={{ fontSize: 10, color: T.textMuted, fontFamily: T.mono }}>
            {data.total} closed positions in DB
          </span>
        )}
      </div>

      <FilterBar filters={filters} onChange={handleFilterChange} loading={loading} />
      <SummaryStats rows={data.rows} total={data.total} />

      {error && (
        <div style={{
          padding: 16, fontSize: 11, color: T.red, fontFamily: T.mono,
          borderBottom: `1px solid ${T.cardBorder}`,
        }}>
          Failed to load trade history: {error}
        </div>
      )}

      {!error && data.rows.length === 0 && !loading && (
        <div style={{ padding: 24, textAlign: 'center' }}>
          <div style={{ fontSize: 11, color: T.textMuted, marginBottom: 4 }}>
            No closed trades match these filters
          </div>
          <div style={{ fontSize: 9, color: T.textDim }}>
            Try clearing filters or wait for the engine to close positions
          </div>
        </div>
      )}

      {data.rows.length > 0 && (
        <div style={{ padding: 14, maxHeight: '70vh', overflowY: 'auto' }}>
          {data.rows.map(trade => (
            <TradeCard key={trade.id} trade={trade} />
          ))}
        </div>
      )}

      <Pagination
        page={page}
        totalPages={totalPages}
        onPageChange={setPage}
        disabled={loading}
      />
    </div>
  );
}
