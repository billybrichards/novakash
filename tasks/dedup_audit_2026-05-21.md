# Dedup / writer-gap audit — 2026-05-21

After the BTC `v9_2_*` 6-fill incident on window 1779336900 (~−$69 net) —
the third recurrence of the same bug class in 24h — here's the diagnosis
and the new closure.

## Incident

Window `btc-updown-5m-1779336900` (05-21 04:15-04:20 UTC).
Polymarket UI shows **at least 6 BUY UP fills** totalling ~$69:

| Fill | Price | Shares | Cost |
|---|---|---|---|
| 1 | $0.90 | 14.3 | $12.84 |
| 2 | $0.91 | 14.2 | $12.83 |
| 3 | $0.91 | 10.0 | $9.10 |
| 4 | $0.91 | 10.0 | $9.10 |
| 5 | $0.91 | 14.0 | $12.74 |
| 6 | $0.91 | (cut off) | ~$12.74 |
| **Total** | | | **~$69+** |

Window resolved DOWN → ~−$69.35 LOSS (per `wallet_truth.py`).

DB has only **2 trade rows totalling $26.70**:
- 8792 v9_2_iso_strict YES $13.35 (OPEN)
- 8793 v9_2_iso_expand YES $13.35 (RESOLVED_LOSS)

**Gap: $42.65 of fills landed on-chain but never wrote to `trades`.**

## Strategies that emitted TRADE on this window (LIVE-mode only)

Per `silent_strats_v2.sql`:

| Strategy | TRADE decisions | Trade rows | Notes |
|---|---|---|---|
| v9_2_iso_expand | 24 | 1 | mid-fill ($13.35 visible / >2 fills on chain) |
| v9_2_iso_strict | 19 | 1 | same |
| **v9_2_raw_lgb** | 14 | **0** | fired LIVE but NO trade row |
| **v9_2_v12_combo** | 11 | **0** | fired LIVE but NO trade row |
| v9_1_lgb_only | 16 | 0 | GHOST at incident time (no fills expected) |

Two LIVE strategies (`raw_lgb`, `v12_combo`) fired 11-14 TRADE decisions
each and produced zero trade rows. They almost certainly executed orders
that filled on-chain (matching the missing $42.65) but the writer dropped
the rows.

## Existing dedup / cap mechanisms (and why each failed today)

| Mechanism | Layer | Failed because |
|---|---|---|
| HARD lock (PR #563) | trades-table query, per-(strategy, window, direction) | Per-strategy — different strategies on same window correctly allowed |
| Per-window total cap (PR #561) | trades-table sum | Sums DB rows. Missing rows = cap can't see them. Bypassed. |
| Sub-fill writer (PR #561) | execute_trade Step 8 | Only catches SECONDARY_FILL within one `execute_order` call. Misses cross-call rapid fires and other-strategy fills. |
| `_in_flight_keys` (in-process) | per `(sid, window_ts, direction)` | Released in `finally`; 40s gap ate the lifetime |
| `try_claim_trade` 15s lease | per `(window, sid)` | TTL too short |
| `try_claim_fill_slot` 25s placeholder | per `(window, sid)` | TTL too short, stale-takeover by design |
| `mark_traded` UNIQUE on `strategy_window_fills` | per `(asset, window_ts, timeframe, sid)` | Only writes IF `mark_traded` is called; if write fails silently, this gate falls open |

**Common pattern**: every protection relies on either (a) a TTL that the
40s rapid-fire crossed, or (b) the trades/strategy_window_fills tables
being accurately populated. When the writer drops rows, EVERY protection
gets bypassed.

## The fix in this PR — on-chain authoritative cap (Step 4.6)

Add `engine/use_cases/onchain_position_cap.py`:
- Queries Polymarket data-api `/positions?user=<funder>&market=<condition_id>`
- Sums `initialValue` for the proposed direction on this market
- Compares to `RISK_MAX_STAKE_PER_WINDOW_USD_PM` (opt-in env, default OFF)
- Returns `exposure_cap_window_pm` skip if `on_chain + new_stake > cap`
- FAIL-CLOSED on timeout/error (returns `exposure_cap_window_pm_unavailable`)
- 5s in-process cache to dedupe parallel-strategy fires
- 0.8s HTTP timeout

Why this closes the gap: **Polymarket data-api positions are independent
of our writer**. The endpoint sees exactly what's on chain. If the writer
silently drops 4 of 6 fills, the data-api still reports cost from all 6.
The cap can't be bypassed by writer regressions.

## New audit/observability — `scripts/ops/check_dbpm_discrepancy.py`

Cron-friendly script that:
1. Pulls all (market_slug, condition_id) pairs with non-cancelled trades in last N hours
2. For each, fetches PM positions via data-api
3. Compares `SUM(stake_usd) FROM trades` vs `SUM(initialValue) FROM positions`
4. Exits 1 (alerts Telegram via cron `||` chain) on any gap > $1

Schedule hourly:
```
0 * * * * cd /home/novakash/novakash && set -a && . engine/.env && set +a \
  && python3 scripts/ops/check_dbpm_discrepancy.py 4 \
  || curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
       -d "chat_id=$TELEGRAM_CHAT_ID&text=DB-PM gap detected — see audit"
```

## Recommended config post-merge

```
RISK_MAX_STAKE_PER_WINDOW_USD_PM=15
```

$15 per (asset, window, direction) keeps even worst-case correlated fires
to $15 loss. Combined with existing $25 trades-cap (Step 4.5) and HARD lock
(Step -0.5), the layered defence is now:
- Step -0.5: same-strategy retry block (DB-based)
- Step 4.5: cross-strategy aggregate $25 (DB-based, can be bypassed)
- **Step 4.6: ON-CHAIN $15 absolute ceiling (un-bypassable)**

## Tests

`engine/tests/unit/use_cases/test_onchain_position_cap.py` — 7 tests:
1. Blocks when on-chain already at cap
2. Allows when room remaining
3. **Blocks even when DB is silent** (the smoking-gun test)
4. Fail-closed on fetch error
5. Cap disabled when 0/unset
6. Env parsing
7. Direction YES/Up mapping

All 7 pass + 123 existing execute_trade tests still pass.

## What this doesn't fix

This PR caps the BLAST RADIUS of the writer regression but does NOT fix
the writer itself. Trade rows are still missing from DB. Follow-up needed:
- Locate where `_recorder.record_trade()` is bypassed in execute_trade for
  rapid sub-fires (likely the FAK ladder when an in-flight order from the
  prior fire is mid-confirmation)
- Add a "post-flight reconciler" that re-syncs trades from PM positions
  every hour and inserts missing rows

But with the on-chain cap, those gaps can't cost real money anymore.
