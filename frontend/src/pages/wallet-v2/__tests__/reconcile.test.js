import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  pickAuthoritative,
  isDbFallback,
  isPresent,
  mapPositionsSnapshotToBalance,
  mapWalletSnapshotToBalance,
  deriveUnredeemedRows,
  deriveHistoryRows,
  summarizeUnredeemed,
  summarizeHistory,
  fmtUsd,
  fmtDuration,
} from '../reconcile.js';

describe('isPresent', () => {
  it('treats null, undefined, NaN as absent', () => {
    expect(isPresent(null)).toBe(false);
    expect(isPresent(undefined)).toBe(false);
    expect(isPresent(NaN)).toBe(false);
    expect(isPresent(Infinity)).toBe(false);
  });
  it('treats 0 and empty string and false as present', () => {
    expect(isPresent(0)).toBe(true);
    expect(isPresent('')).toBe(true);
    expect(isPresent(false)).toBe(true);
  });
});

describe('pickAuthoritative', () => {
  it('returns the highest-rank non-null candidate', () => {
    const out = pickAuthoritative([
      { rank: 'db-only', value: 100 },
      { rank: 'onchain', value: 200 },
      { rank: 'data-api', value: 150 },
    ]);
    expect(out).toEqual({ rank: 'onchain', value: 200 });
  });

  it('falls through when higher rank is null', () => {
    const out = pickAuthoritative([
      { rank: 'onchain', value: null },
      { rank: 'data-api', value: 150 },
    ]);
    expect(out).toEqual({ rank: 'data-api', value: 150 });
  });

  it('returns null for empty input', () => {
    expect(pickAuthoritative([])).toBe(null);
    expect(pickAuthoritative(null)).toBe(null);
  });

  it('ignores unknown ranks', () => {
    const out = pickAuthoritative([{ rank: 'mystery', value: 999 }]);
    expect(out).toBe(null);
  });
});

describe('isDbFallback', () => {
  it('true for db-only and db-enriched ranks', () => {
    expect(isDbFallback({ rank: 'db-only', value: 1 })).toBe(true);
    expect(isDbFallback({ rank: 'db-enriched', value: 1 })).toBe(true);
  });
  it('false for trusted ranks', () => {
    expect(isDbFallback({ rank: 'onchain', value: 1 })).toBe(false);
    expect(isDbFallback({ rank: 'data-api', value: 1 })).toBe(false);
  });
  it('false for null', () => {
    expect(isDbFallback(null)).toBe(false);
  });
});

describe('mapPositionsSnapshotToBalance (DB fallback)', () => {
  it('maps the observed /api/positions/snapshot shape', () => {
    const resp = {
      now_utc: '2026-04-20T15:00:00Z',
      wallet_usdc: 66.9,
      pending_wins: [],
      pending_count: 0,
      pending_total_usd: 0,
      overdue_count: 0,
      effective_balance: 66.9,
      daily_quota_limit: 80,
      quota_used_today: 2,
      cooldown: { active: false },
      _meta: { data_stale: false },
    };
    const b = mapPositionsSnapshotToBalance(resp);
    expect(b.rank).toBe('db-only');
    expect(b.cash_usdc).toBe(66.9);
    expect(b.effective_total).toBe(66.9);
    expect(b.redeemable_now).toBe(0);
    expect(b.quota_used_today).toBe(2);
    expect(b.quota_limit).toBe(80);
    expect(b.pending_wins).toEqual([]);
  });

  it('sums pending_wins array when effective_balance absent', () => {
    const resp = {
      wallet_usdc: 50,
      pending_wins: [
        { current_value_usd: 5 },
        { current_value_usd: 3 },
      ],
    };
    const b = mapPositionsSnapshotToBalance(resp);
    expect(b.pending_wins_value).toBe(8);
    expect(b.effective_total).toBe(58);
  });

  it('returns null for bad input', () => {
    expect(mapPositionsSnapshotToBalance(null)).toBe(null);
    expect(mapPositionsSnapshotToBalance('bad')).toBe(null);
  });
});

describe('mapWalletSnapshotToBalance (spec endpoint)', () => {
  it('picks onchain rank when source reports onchain_2of3', () => {
    const resp = {
      now_utc: '2026-04-20T15:00:00Z',
      balance: {
        cash_usdc_onchain: 70.13,
        cash_usdc_source: 'onchain_2of3',
        pending_wins_value: 5.03,
        open_positions_mark: 4.40,
        effective_total: 79.56,
        deltas: { vs_24h_ago: 12.34, vs_session_start: 8.91 },
      },
      unredeemed: { redeemable_now: 1, redeemable_value_usd: 5.03, overdue_5min: 0, negrisk_lag_count: 0 },
      open_positions: { count: 1, mark_value: 4.40, next_resolve_utc: '2026-04-20T14:10:00Z' },
      redeemer_health: {
        last_successful_sweep_utc: '2026-04-20T13:50:12Z',
        sweeps_last_hour: 4,
        wins_missed_by_engine_24h: 1,
        quota_used_today: 0,
        quota_limit: 80,
      },
      _meta: { onchain_rpcs_healthy: 3 },
    };
    const b = mapWalletSnapshotToBalance(resp);
    expect(b.rank).toBe('onchain');
    expect(b.cash_usdc).toBe(70.13);
    expect(b.effective_total).toBe(79.56);
    expect(b.delta_24h).toBe(12.34);
    expect(b.delta_session).toBe(8.91);
    expect(b.open_count).toBe(1);
    expect(b.redeemer_health.wins_missed_24h).toBe(1);
    expect(b.redeemer_health.sweeps_last_hour).toBe(4);
  });

  it('falls back to data-api when source hints it', () => {
    const resp = {
      balance: { cash_usdc_onchain: 10, cash_usdc_source: 'data-api' },
    };
    expect(mapWalletSnapshotToBalance(resp).rank).toBe('data-api');
  });

  it('uses db-enriched rank when source is unknown', () => {
    const resp = {
      balance: { cash_usdc_onchain: 10, cash_usdc_source: 'mystery' },
    };
    expect(mapWalletSnapshotToBalance(resp).rank).toBe('db-enriched');
  });

  it('returns null for bad input', () => {
    expect(mapWalletSnapshotToBalance(null)).toBe(null);
  });
});

describe('deriveUnredeemedRows', () => {
  it('uses walletPending when present (data-api rank)', () => {
    const wp = {
      rows: [
        {
          condition_id: '0xabc',
          title: 'BTC 5m',
          side: 'Up',
          size: 5,
          cost_usd: 3.5,
          current_value_usd: 5,
          redeemable: true,
          window_end_utc: '2026-04-20T13:10:00Z',
          overdue_seconds: 900,
          strategy: 'v4_fusion',
          stuck_reason: null,
        },
      ],
      stuck_reason_legend: { negrisk_unresolved: 'CTF denom=0' },
    };
    const out = deriveUnredeemedRows({ walletPending: wp });
    expect(out.rank).toBe('data-api');
    expect(out.rows).toHaveLength(1);
    expect(out.rows[0].condition_id).toBe('0xabc');
    expect(out.rows[0].redeemable).toBe(true);
    expect(out.legend).toEqual({ negrisk_unresolved: 'CTF denom=0' });
  });

  it('falls back to positionsSnap.pending_wins when walletPending missing', () => {
    const posSnap = {
      pending_wins: [
        { condition_id: '0xdef', title: 'X', side: 'Down', size: 10, stake_usd: 2, payout_usd: 4, status: 'REDEEMABLE' },
      ],
    };
    const out = deriveUnredeemedRows({ positionsSnap: posSnap });
    expect(out.rank).toBe('db-only');
    expect(out.rows).toHaveLength(1);
    expect(out.rows[0].redeemable).toBe(true);
    expect(out.rows[0].cost_usd).toBe(2);
    expect(out.rows[0].current_value_usd).toBe(4);
  });

  it('falls back to trades with WIN outcome and filters redeemed=true', () => {
    const trades = [
      { id: 1, outcome: 'WIN', redeemed: true, market_slug: 'm1', direction: 'YES' },
      { id: 2, outcome: 'WIN', redeemed: false, market_slug: 'm2', direction: 'NO', stake_usd: 3, payout_usd: 6 },
      { id: 3, outcome: 'LOSS', market_slug: 'm3' },
    ];
    const out = deriveUnredeemedRows({ trades });
    expect(out.rank).toBe('db-only');
    expect(out.rows).toHaveLength(1);
    expect(out.rows[0].key).toBe(2);
    expect(out.rows[0].cost_usd).toBe(3);
    expect(out.rows[0].current_value_usd).toBe(6);
  });

  it('returns empty when no source has data', () => {
    const out = deriveUnredeemedRows({});
    expect(out.rows).toEqual([]);
  });
});

describe('deriveHistoryRows', () => {
  const now = new Date('2026-04-20T15:00:00Z').getTime();
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(now);
  });
  afterEach(() => { vi.useRealTimers(); });

  it('uses walletHistory when present (data-api rank)', () => {
    const wh = {
      rows: [
        {
          redeem_tx: '0x9c7ac44c',
          redeemed_at_utc: '2026-04-20T10:43:12Z',
          condition_id: '0x74f10aff',
          title: 'BTC 5m · 10:40–10:45',
          side: 'Up',
          cost_usd: 4.36,
          payout_usd: 4.96,
          pnl_usd: 0.60,
          transport: 'onchain_matic',
          initiator: 'manual_billy',
          strategy: 'v4_fusion',
        },
      ],
    };
    const out = deriveHistoryRows({ walletHistory: wh });
    expect(out.rank).toBe('data-api');
    expect(out.rows[0].tx).toBe('0x9c7ac44c');
    expect(out.rows[0].transport).toBe('onchain_matic');
    expect(out.rows[0].outcome).toBe('WIN');
  });

  it('trade-fallback respects days window', () => {
    const trades = [
      // within 1d
      { id: 'a', outcome: 'WIN', resolved_at: '2026-04-20T14:00:00Z', pnl_usd: 2.1, stake_usd: 5, payout_usd: 7.1, market_slug: 'm-a', direction: 'YES', strategy_id: 'v6' },
      // within 1d (LOSS)
      { id: 'b', outcome: 'LOSS', resolved_at: '2026-04-20T13:00:00Z', pnl_usd: -3, stake_usd: 3, market_slug: 'm-b', direction: 'NO' },
      // outside 1d window
      { id: 'c', outcome: 'WIN', resolved_at: '2026-04-18T14:00:00Z', pnl_usd: 1 },
    ];
    const out = deriveHistoryRows({ trades, days: 1 });
    expect(out.rank).toBe('db-only');
    expect(out.rows.map(r => r.key)).toEqual(['a', 'b']); // sorted desc
    expect(out.rows[0].outcome).toBe('WIN');
    expect(out.rows[1].outcome).toBe('LOSS');
    expect(out.rows[0].transport).toBe(null);
    expect(out.rows[0].initiator).toBe(null);
  });

  it('trade-fallback 7d includes older rows', () => {
    const trades = [
      { id: 'a', outcome: 'WIN', resolved_at: '2026-04-19T14:00:00Z', pnl_usd: 1 },
      { id: 'b', outcome: 'WIN', resolved_at: '2026-04-14T14:00:00Z', pnl_usd: 1 },
      { id: 'c', outcome: 'WIN', resolved_at: '2026-04-10T14:00:00Z', pnl_usd: 1 },
    ];
    const out = deriveHistoryRows({ trades, days: 7 });
    expect(out.rows).toHaveLength(2);
  });

  it('ignores OPEN trades', () => {
    const trades = [
      { id: 'open', outcome: null, resolved_at: null },
      { id: 'w', outcome: 'WIN', resolved_at: '2026-04-20T14:00:00Z', pnl_usd: 1 },
    ];
    const out = deriveHistoryRows({ trades });
    expect(out.rows).toHaveLength(1);
  });
});

describe('summarizeUnredeemed', () => {
  it('counts redeemable, value, and overdue buckets', () => {
    const rows = [
      { redeemable: true,  current_value_usd: 5, overdue_seconds: 4000 },
      { redeemable: true,  current_value_usd: 3, overdue_seconds: 600 },
      { redeemable: false, current_value_usd: 2, overdue_seconds: 60 },
      { redeemable: true,  current_value_usd: null, overdue_seconds: null },
    ];
    const s = summarizeUnredeemed(rows);
    expect(s.n).toBe(4);
    expect(s.redeemable).toBe(3);
    expect(s.value).toBe(10);
    expect(s.overdue5m).toBe(2);
    expect(s.overdue1h).toBe(1);
  });
  it('handles empty', () => {
    expect(summarizeUnredeemed([])).toEqual({ n: 0, redeemable: 0, value: 0, overdue5m: 0, overdue1h: 0 });
  });
});

describe('summarizeHistory', () => {
  it('computes wins, losses, net', () => {
    const rows = [
      { outcome: 'WIN',  pnl_usd: 2, payout_usd: 7 },
      { outcome: 'LOSS', pnl_usd: -3 },
      { outcome: 'WIN',  pnl_usd: 1, payout_usd: 3 },
    ];
    const s = summarizeHistory(rows);
    expect(s.wins).toBe(2);
    expect(s.losses).toBe(1);
    expect(s.net).toBe(0);
    expect(s.payout).toBe(10);
  });
});

describe('fmtUsd', () => {
  it('formats with $ prefix, 2dp', () => {
    expect(fmtUsd(12.345)).toBe('$12.35');
    expect(fmtUsd(0)).toBe('$0.00');
  });
  it('signed mode adds + or -', () => {
    expect(fmtUsd(12.3, { signed: true })).toBe('+$12.30');
    expect(fmtUsd(-4, { signed: true })).toBe('-$4.00');
  });
  it('null / nan → em-dash', () => {
    expect(fmtUsd(null)).toBe('—');
    expect(fmtUsd(undefined)).toBe('—');
    expect(fmtUsd(NaN)).toBe('—');
  });
});

describe('fmtDuration', () => {
  it('sub-minute → s', () => {
    expect(fmtDuration(5)).toBe('5s');
  });
  it('minute bucket → m', () => {
    expect(fmtDuration(180)).toBe('3m');
  });
  it('hour bucket → h', () => {
    expect(fmtDuration(7200)).toBe('2h');
  });
  it('day bucket → d h', () => {
    expect(fmtDuration(100_000)).toBe('1d 3h');
  });
  it('null / negative → em-dash', () => {
    expect(fmtDuration(null)).toBe('—');
    expect(fmtDuration(-1)).toBe('—');
  });
});
