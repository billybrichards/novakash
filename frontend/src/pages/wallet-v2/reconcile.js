// Pure helpers for reconciling numbers across sources per spec #189 §3.
//
// Spec rule (verbatim):
//   "every number shown on-page must be derivable from sources 1–3. DB is
//    allowed only to enrich (names, strategy, stake context), never to gate
//    visibility."
//
// Source ranks:
//   1 onchain (2-of-3 RPC consensus)
//   2 data-api.polymarket.com
//   3 activity API (redeem history)
//   4 local trades DB
//   5 local wallet_snapshots / pending_wins / redeemer_state DB tables
//
// Given a set of candidate values (each tagged with a rank), `pickAuthoritative`
// returns the value from the highest-ranking non-null source, plus a tag that
// the badge uses to colour the cell.

export const RANK_ORDER = ['onchain', 'data-api', 'db-enriched', 'db-only'];

export function isPresent(v) {
  if (v == null) return false;
  if (typeof v === 'number' && !Number.isFinite(v)) return false;
  return true;
}

/**
 * Pick the best candidate for a single scalar value.
 *
 * @param {Array<{rank: string, value: any}>} candidates
 * @returns {{rank: string, value: any} | null}
 */
export function pickAuthoritative(candidates) {
  if (!Array.isArray(candidates) || candidates.length === 0) return null;
  const byRank = new Map(candidates.filter(c => c && RANK_ORDER.includes(c.rank)).map(c => [c.rank, c]));
  for (const rank of RANK_ORDER) {
    const c = byRank.get(rank);
    if (c && isPresent(c.value)) return c;
  }
  return null;
}

/**
 * True when the best source we could find is DB-tier.
 * Used to drive the "trust warning" banner state.
 */
export function isDbFallback(result) {
  if (!result) return false;
  return result.rank === 'db-only' || result.rank === 'db-enriched';
}

/**
 * Map `/api/positions/snapshot` response → wallet-v2 balance-section shape.
 * This is the DB-only fallback used when `/api/wallet/snapshot` (audit #217)
 * is missing. Every field gets rank="db-only" so the UI flags it as low-trust.
 */
export function mapPositionsSnapshotToBalance(resp) {
  if (!resp || typeof resp !== 'object') return null;
  const pendingMark = Array.isArray(resp.pending_wins)
    ? resp.pending_wins.reduce((s, p) => {
        const v = Number(p?.current_value_usd ?? p?.payout_usd ?? p?.value_usd);
        return Number.isFinite(v) ? s + v : s;
      }, 0)
    : Number(resp.pending_total_usd ?? 0);
  const cash = Number(resp.wallet_usdc);
  const effective = Number(resp.effective_balance ?? (Number.isFinite(cash) ? cash + pendingMark : null));
  return {
    rank: 'db-only',
    cash_usdc: Number.isFinite(cash) ? cash : null,
    pending_wins_value: Number.isFinite(pendingMark) ? pendingMark : null,
    open_positions_mark: null, // positions/snapshot doesn't separate pending-wins from open bets
    effective_total: Number.isFinite(effective) ? effective : null,
    redeemable_now: Number(resp.pending_count ?? 0),
    overdue_count: Number(resp.overdue_count ?? 0),
    quota_used_today: Number(resp.quota_used_today ?? 0),
    quota_limit: Number(resp.daily_quota_limit ?? 0),
    cooldown: resp.cooldown ?? null,
    meta: resp._meta ?? null,
    now_utc: resp.now_utc ?? null,
    pending_wins: Array.isArray(resp.pending_wins) ? resp.pending_wins : [],
  };
}

/**
 * Map a /api/wallet/snapshot response (spec #189 §4.2) to the FE balance shape.
 * This is the "good" path — rank="onchain" when hub confirms 2-of-3, else
 * "data-api" fallback.
 */
export function mapWalletSnapshotToBalance(resp) {
  if (!resp || typeof resp !== 'object') return null;
  const b = resp.balance ?? {};
  const u = resp.unredeemed ?? {};
  const o = resp.open_positions ?? {};
  const h = resp.redeemer_health ?? {};
  const src = String(b.cash_usdc_source || '').toLowerCase();
  const rank = src.includes('onchain') ? 'onchain'
    : src.includes('data-api') ? 'data-api'
    : 'db-enriched';
  return {
    rank,
    cash_usdc: numOrNull(b.cash_usdc_onchain ?? b.cash_usdc),
    pending_wins_value: numOrNull(b.pending_wins_value),
    open_positions_mark: numOrNull(b.open_positions_mark),
    effective_total: numOrNull(b.effective_total),
    delta_24h: numOrNull(b.deltas?.vs_24h_ago),
    delta_session: numOrNull(b.deltas?.vs_session_start),
    redeemable_now: numOrNull(u.redeemable_now),
    redeemable_value_usd: numOrNull(u.redeemable_value_usd),
    negrisk_lag_count: numOrNull(u.negrisk_lag_count),
    overdue_5min: numOrNull(u.overdue_5min),
    overdue_1h: numOrNull(u.overdue_1h),
    overdue_count: numOrNull(u.overdue_5min),
    open_count: numOrNull(o.count),
    open_mark_value: numOrNull(o.mark_value),
    next_resolve_utc: o.next_resolve_utc ?? null,
    redeemer_health: {
      last_sweep_utc: h.last_successful_sweep_utc ?? null,
      sweeps_last_hour: numOrNull(h.sweeps_last_hour),
      wins_missed_24h: numOrNull(h.wins_missed_by_engine_24h),
      quota_used_today: numOrNull(h.quota_used_today),
      quota_limit: numOrNull(h.quota_limit),
      cooldown: h.cooldown ?? null,
    },
    meta: resp._meta ?? null,
    now_utc: resp.now_utc ?? null,
  };
}

/**
 * Build pending-tab rows from either:
 *   (a) /api/wallet/pending  → `rows` key, spec #189 §4.3 shape.
 *   (b) /api/positions/snapshot → `pending_wins` array (DB-only fallback).
 *   (c) /api/trades?outcome=WIN  → legacy last-resort fallback.
 *
 * Returns `{ rank, rows }`. Every row shares a normalized shape.
 */
export function deriveUnredeemedRows({ walletPending, positionsSnap, trades }) {
  if (walletPending && Array.isArray(walletPending.rows)) {
    return {
      rank: 'data-api',
      rows: walletPending.rows.map(normalizeSpecRow),
      legend: walletPending.stuck_reason_legend ?? null,
    };
  }
  if (positionsSnap && Array.isArray(positionsSnap.pending_wins) && positionsSnap.pending_wins.length > 0) {
    return {
      rank: 'db-only',
      rows: positionsSnap.pending_wins.map(normalizePositionsRow),
      legend: null,
    };
  }
  if (Array.isArray(trades)) {
    const rows = trades
      .filter(t => t && (t.outcome === 'WIN' || t.status === 'RESOLVED_WIN'))
      .filter(t => t.redeemed !== true) // defensive; column rarely serialized
      .map(normalizeTradeRow);
    if (rows.length === 0) {
      return { rank: 'db-only', rows: [], legend: null };
    }
    return { rank: 'db-only', rows, legend: null };
  }
  return { rank: 'db-only', rows: [], legend: null };
}

function normalizeSpecRow(r) {
  return {
    key: r.condition_id || r.id,
    condition_id: r.condition_id ?? null,
    title: r.title || '—',
    side: r.side || '—',
    size: numOrNull(r.size),
    cost_usd: numOrNull(r.cost_usd),
    current_value_usd: numOrNull(r.current_value_usd),
    redeemable: r.redeemable === true,
    window_end_utc: r.window_end_utc ?? null,
    overdue_seconds: numOrNull(r.overdue_seconds),
    strategy: r.strategy || null,
    stuck_reason: r.stuck_reason ?? null,
  };
}

function normalizePositionsRow(p) {
  const end = p.window_end_utc ?? p.resolved_at ?? null;
  return {
    key: p.condition_id || p.position_id || p.id,
    condition_id: p.condition_id ?? null,
    title: p.title || p.market_slug || '—',
    side: p.side || p.direction || '—',
    size: numOrNull(p.size),
    cost_usd: numOrNull(p.cost_usd ?? p.stake_usd),
    current_value_usd: numOrNull(p.current_value_usd ?? p.payout_usd ?? p.value_usd),
    redeemable: p.redeemable === true || p.status === 'REDEEMABLE',
    window_end_utc: end,
    overdue_seconds: deriveOverdue(end),
    strategy: p.strategy || p.strategy_id || null,
    stuck_reason: null,
  };
}

function normalizeTradeRow(t) {
  const end = t.resolved_at ?? null;
  return {
    key: t.id,
    condition_id: t.condition_id ?? null,
    title: t.market_slug || '—',
    side: t.direction || t.side || '—',
    size: numOrNull(t.fill_size ?? t.size),
    cost_usd: numOrNull(t.stake_usd),
    current_value_usd: numOrNull(t.payout_usd ?? t.pnl_usd),
    redeemable: true, // unknown — trades table doesn't say
    window_end_utc: end,
    overdue_seconds: deriveOverdue(end),
    strategy: t.strategy_id || t.strategy || null,
    stuck_reason: null,
  };
}

function deriveOverdue(iso) {
  if (!iso) return null;
  const ts = new Date(iso).getTime();
  if (!Number.isFinite(ts)) return null;
  return Math.max(0, Math.floor((Date.now() - ts) / 1000));
}

/**
 * Collapse /api/trades rows into redemption-history shape — used as fallback
 * when `/api/wallet/history` does not exist.
 *
 * A WIN trade is treated as a REDEEM event; a LOSS as a settled bet.
 * `transport`/`initiator` are unknown from the trades table alone (those
 * live in the `wallet/history` spec endpoint), so we fill with null and the
 * UI renders them as "—".
 */
export function deriveHistoryRows({ walletHistory, trades, days = 1 }) {
  if (walletHistory && Array.isArray(walletHistory.rows)) {
    return {
      rank: 'data-api',
      rows: walletHistory.rows.map(normalizeHistoryRow),
    };
  }
  if (Array.isArray(trades)) {
    const cutoff = Date.now() - days * 24 * 60 * 60 * 1000;
    const rows = trades
      .filter(t => t && (t.outcome === 'WIN' || t.outcome === 'LOSS'))
      .map(t => {
        const tsRaw = t.resolved_at ?? t.created_at;
        const ts = tsRaw ? new Date(tsRaw).getTime() : 0;
        return {
          key: t.id,
          redeemed_at_utc: tsRaw,
          ts,
          condition_id: t.condition_id ?? null,
          market: t.market_slug || '—',
          side: t.direction || t.side || '—',
          cost_usd: numOrNull(t.stake_usd),
          payout_usd: numOrNull(t.payout_usd),
          pnl_usd: numOrNull(t.pnl_usd),
          outcome: t.outcome,
          transport: null,
          initiator: null,
          strategy: t.strategy_id || t.strategy || null,
          tx: null,
        };
      })
      .filter(r => r.ts >= cutoff)
      .sort((a, b) => b.ts - a.ts);
    return { rank: 'db-only', rows };
  }
  return { rank: 'db-only', rows: [] };
}

function normalizeHistoryRow(r) {
  const tsRaw = r.redeemed_at_utc;
  const ts = tsRaw ? new Date(tsRaw).getTime() : 0;
  return {
    key: r.redeem_tx || r.condition_id || tsRaw,
    redeemed_at_utc: tsRaw,
    ts,
    condition_id: r.condition_id ?? null,
    market: r.title || r.market || r.market_slug || '—',
    side: r.side || '—',
    cost_usd: numOrNull(r.cost_usd),
    payout_usd: numOrNull(r.payout_usd),
    pnl_usd: numOrNull(r.pnl_usd),
    outcome: numOrNull(r.pnl_usd) != null && r.pnl_usd < 0 ? 'LOSS' : 'WIN',
    transport: r.transport ?? null,
    initiator: r.initiator ?? null,
    strategy: r.strategy ?? null,
    tx: r.redeem_tx ?? null,
  };
}

/**
 * Aggregate stats shown in the History tab header.
 */
export function summarizeHistory(rows) {
  if (!Array.isArray(rows) || rows.length === 0) {
    return { n: 0, wins: 0, losses: 0, net: 0, payout: 0 };
  }
  let wins = 0;
  let losses = 0;
  let net = 0;
  let payout = 0;
  for (const r of rows) {
    const p = Number(r.pnl_usd);
    if (Number.isFinite(p)) net += p;
    const po = Number(r.payout_usd);
    if (Number.isFinite(po)) payout += po;
    if (r.outcome === 'WIN') wins += 1;
    else if (r.outcome === 'LOSS') losses += 1;
  }
  return { n: rows.length, wins, losses, net, payout };
}

/**
 * Aggregate stats shown in the Unredeemed tab header.
 */
export function summarizeUnredeemed(rows) {
  if (!Array.isArray(rows) || rows.length === 0) {
    return { n: 0, redeemable: 0, value: 0, overdue5m: 0, overdue1h: 0 };
  }
  let redeemable = 0;
  let value = 0;
  let overdue5m = 0;
  let overdue1h = 0;
  for (const r of rows) {
    const v = Number(r.current_value_usd);
    if (Number.isFinite(v)) value += v;
    if (r.redeemable) redeemable += 1;
    const od = Number(r.overdue_seconds);
    if (Number.isFinite(od)) {
      if (od >= 300) overdue5m += 1;
      if (od >= 3600) overdue1h += 1;
    }
  }
  return { n: rows.length, redeemable, value, overdue5m, overdue1h };
}

/**
 * Format a number as USD with sign. null → em-dash.
 */
export function fmtUsd(n, { signed = false } = {}) {
  if (n == null) return '—';
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  const abs = Math.abs(v).toFixed(2);
  if (!signed) return `$${abs}`;
  if (v >= 0) return `+$${abs}`;
  return `-$${abs}`;
}

/**
 * Format an overdue-seconds value as a compact duration: 5s / 3m / 2h / 1d 3h.
 */
export function fmtDuration(secs) {
  if (secs == null) return '—';
  const s = Number(secs);
  if (!Number.isFinite(s) || s < 0) return '—';
  if (s < 60) return `${Math.floor(s)}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  const d = Math.floor(h / 24);
  return `${d}d ${h % 24}h`;
}

function numOrNull(v) {
  if (v == null) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}
