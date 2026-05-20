# Dedup audit — 2026-05-20

After the ETH `v9_2_eth_raw_lgb` 3-fire incident on window 1779312000
(−$21.91 net), here's every dedup mechanism in the engine and why each
one failed today.

## Incident summary

| time | direction | fill | stake | DB row | notes |
|---|---|---|---|---|---|
| 21:22:49 | DOWN | $0.78 | $6.12 | 8743 OPEN | first fire |
| 21:23:29 | DOWN | $0.89 | $4.88 | 8744 (DB says WIN, actually LOSS) | 40s later — TTL gap |
| (unknown) | DOWN | $0.82 | $10.91 | **NO DB ROW** | sub-fill writer regression |

Window resolved UP. Total LOSS = ~$21.91.

## Existing mechanisms — what each is and why it missed

| Mechanism | Source | Scope | TTL | Failed today because |
|---|---|---|---|---|
| `_in_flight_keys` set | `execute_trade.py:~440` | `(sid, window_ts, direction)` in-process | one `execute()` call | released in `finally` after first fire — second invocation 40s later saw empty set |
| `try_claim_trade` lease | `execute_trade.py:~660` | `(window, sid)` per-process | 15s | released on fill, 15s &lt; 40s |
| `try_claim_fill_slot` placeholder | `execute_trade.py:~900` | `(window, sid)` DB row | 25s stale-takeover | 25s &lt; 40s |
| `mark_traded` UNIQUE constraint | `strategy_window_fills` table | `(asset, window_ts, timeframe, sid)` | permanent | per-(sid, window), NOT per-direction. Also depends on `mark_traded` actually being called |
| `has_filled` check | `execute_trade.py:~585` | per `strategy_window_fills` row | permanent | depends on marker being written; today's first fill apparently didn't write the marker (or it got released without promoting) |
| `min_consecutive_pass_ticks` | strategy YAML | confirmation only | N/A | NOT a dedup mechanism. Memory: `feedback_consec_ticks_not_dedup.md` |
| `_active_gtc` cache (PR #556) | strategies | per-strategy in-process | engine lifetime | process-local, doesn't survive restart, doesn't catch FAK rebound |
| PR #561 per-window total cap ($25) | risk path | across-strategy stake | per window | cumulative $21.91 &lt; $25 → cap allowed all 3 |
| PR #561 sub-fill writer | execute_order | one `execute_order` call | within one FAK ladder | today's 3 fires were 3 SEPARATE `execute_trade.execute()` invocations |
| Per-strategy `_last_order_time` (PR #556) | guardrails | per-strategy clock | 30s `MIN_ORDER_INTERVAL_S` | 30s &lt; 40s |

## Root cause synthesis

Every existing mechanism is either:
- **Time-bounded** (TTL-based) — clears after 15-30s. 40s gap clears all of them.
- **Marker-conditional** — depends on `strategy_window_fills` being correctly populated. Any writer regression silently disables it.

We had **no authoritative dedup that survives both TTL expiry AND marker-write failures**.

## The fix (this PR)

Add a new gate at Step -0.5 of `execute_trade._execute_locked` that queries the **trades table directly**:

```sql
SELECT 1 FROM trades
WHERE strategy_id = $1
  AND direction = $2
  AND COALESCE(metadata->>'window_ts','')::text = $3
  AND COALESCE(metadata->>'asset','BTC') = $4
  AND COALESCE(metadata->>'timeframe','5m') = $5
  AND is_live = $6
  AND status NOT IN ('CANCELLED','SKIPPED','FAILED_EXECUTION')
LIMIT 1
```

Properties:
1. **Per-direction** — UP and DOWN are independent cells (conviction flip still allowed)
2. **Per-strategy** — sibling strategies on same window still fire independently
3. **Per is_live** — paper and live separate domains
4. **Permanent** — no TTL; once a trade row exists, lock is on until status flips to CANCELLED
5. **Marker-independent** — reads from `trades` table, the canonical artefact of "a fill happened"
6. **Fail-closed** — any DB error returns True (block). Worst case: 1 legitimate skip; best case: prevents another $22 loss.

All other dedup mechanisms remain as defence-in-depth.

## Related memory
- `feedback_single_strategy_double_fire.md` — same pattern on v12_combo, 2026-05-01
- `feedback_payoff_math.md` — why multi-fire bleeds even at high WR
- `feedback_wallet_truth_authority.md` — DB outcome column unreliable

## Prior PRs (all incomplete)
- PR #556 — per-strategy guardrail + `_active_gtc` cache (~2026-04-30)
- PR #561 — per-window total cap + FAK sub-fill writer (2026-05-20)
- This PR — strict (sid, window, direction) HARD lock backed by trades table

3rd recurrence in 3 weeks. This is the LAST one.
