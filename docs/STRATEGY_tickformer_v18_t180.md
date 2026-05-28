# tickformer_v18_t180 — Balanced t-180 Strategy

**Status:** SHADOW (Billy promotes manually per `feedback_no_auto_promote.md`)
**Asset:** ANY (5m timescale)
**Hook:** `engine/strategies/configs/tickformer_v18_t180.py::evaluate_tickformer_v18_t180`
**YAML:** `engine/strategies/configs/tickformer_v18_t180.yaml`
**Migration:** `migrations/add_tickformer_v17_v18_strategies.sql`

## Profile

TickFormer v18 is v17's broad-band successor — same architecture,
trained across the full t-60..t-220 window with a uniform precision
loss. Its **defining property** is that WR holds 85%+ across the
*entire* eval_offset_remaining band at thr 0.90. No sharp late-window
cliff. v18 is the production-recommended model for t-180 entries.

Expected density at the recommended tier: **~16 trades/day** combined
UP+DN at ~92% WR.

## Conviction tier table

From the v18 val sweep (offline backtest):

| Tier  | up_threshold | rem_min | rem_max | WR    | Notes                                |
|-------|--------------|---------|---------|-------|--------------------------------------|
| Tight | 0.90         | 60      | 60      | ~96%  | tightest, lowest density             |
| t-180 | 0.90         | 60      | 180     | ~92%  | mid band                             |
| Recommended | 0.90   | 60      | 220     | ~92%  | **default** — broad-band stable      |
| Wide  | 0.85         | 60      | 220     | ~85%  | trades density for WR floor          |

DOWN side is symmetric: `down_threshold = 1 - up_threshold` (default 0.10).
The trade signal cross-check (`tickformer_trade_signal`) only blocks on
an explicit *opposite* label.

## Expected trade density

At default tier (thr 0.90, rem 60-220): **~16 fires/day** combined
UP+DN at ~92% WR. This is the highest production-recommended fire
density of the three tickformer variants while still sustaining 90%+
combined WR.

## SHADOW kill switch (two layers)

1. **`mode: SHADOW`** in YAML and `strategy_configs` row.
2. **`gate_params.shadow_only=1`** in the hook. Forces
   `SKIP(reason="shadow_only_no_trade")` even if mode is mis-set.

Both must be flipped before any real fire emits.

## Shadow -> live promotion plan

1. **SHADOW (this PR).** Recommended broad-band defaults, `shadow_only=1`.
2. **GHOST.** After 50+ shadow fires with WR >= 90% across both
   directions, flip `shadow_only=0` and promote to GHOST.
3. **LIVE @ $5.** After 100+ GHOST fires sustaining 90%+ WR, promote
   to LIVE with `max_position_usd=5`. v18 is the lead candidate for
   first *production volume* tickformer fire because of its
   broad-band stability.

## Cross-strategy interaction (v16/v17/v18)

All three tickformer strategies can fire on the same window. They are
designed to be **additive** — each emits an independent SHADOW decision
record. Because all three default `shadow_only=1`, there is no
competing-for-fill risk at PR-merge time.

At live-promotion time the operator must decide:
- **Mutually exclusive** — gate on `strategy_priority` (v17 > v18 > v16)
  to fire only the highest-conviction model per window. Recommended for
  first live promotion.
- **Stacked exposure** — let all three fire independently. Higher fill
  density but compounding sizing risk; requires explicit per-window
  position cap.

## Cross-repo dependency

Forward-compatible with the timesfm sister PR. If
`probability_tickformer_v18` is missing from the surface, the hook
short-circuits to a clean `SKIP(reason="tickformer_v18_not_available")`.

## Monitoring queries

```sql
SELECT
    DATE_TRUNC('hour', created_at) AS hr,
    COUNT(*) FILTER (WHERE metadata->>'would_trade' = 'true') AS would_fires,
    skip_reason,
    COUNT(*)
FROM strategy_decisions
WHERE strategy_id = 'tickformer_v18_t180'
  AND created_at > NOW() - INTERVAL '24 hours'
GROUP BY 1, 3
ORDER BY 1 DESC, 4 DESC;
```
