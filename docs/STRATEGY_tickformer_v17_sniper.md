# tickformer_v17_sniper — Precision Sniper Strategy

**Status:** SHADOW (Billy promotes manually per `feedback_no_auto_promote.md`)
**Asset:** ANY (5m timescale)
**Hook:** `engine/strategies/configs/tickformer_v17_sniper.py::evaluate_tickformer_v17_sniper`
**YAML:** `engine/strategies/configs/tickformer_v17_sniper.yaml`
**Migration:** `migrations/add_tickformer_v17_v18_strategies.sql`

## Profile

TickFormer v17 is v16's precision-tuned successor — same hybrid
transformer + multi-step autoregressive head, loss reweighted toward
late-window precision. It is the **sniper** profile: ~10x fewer fires
than v16 in exchange for sustained 92-98% WR across the entire
eval_offset_remaining band.

Trade-off vs v16: low fire density (~3-8/day combined UP+DN at the
default tier) but holds 92%+ WR all the way from t-60 to t-220.

## Conviction tier table

From the v17 val sweep (offline backtest):

| Tier | up_threshold | rem_min | rem_max | WR    | Notes                              |
|------|--------------|---------|---------|-------|------------------------------------|
| S    | 0.85         | 60      | 60      | ~98%  | tightest — very low density        |
| Sniper | 0.85       | 60      | 140     | ~94-96% | **default** — sniper sweet spot  |
| Dream | 0.85        | 60      | 220     | ~92%  | full broad band, dream tier        |

DOWN side is symmetric: `down_threshold = 1 - up_threshold` (default 0.15).
The trade signal cross-check (`tickformer_trade_signal`) only blocks on
an explicit *opposite* label — `HOLD` or `None` pass through.

## Expected trade density

At default tier (thr 0.85, rem 60-140): **~3-8 fires/day** combined
UP+DN. Sniper density is intentionally low — that's the model's
purpose. To hit a 20-fire shadow promotion bar takes 3-5 days.

## SHADOW kill switch (two layers)

1. **`mode: SHADOW`** in YAML and `strategy_configs` row.
2. **`gate_params.shadow_only=1`** in the hook. Even if mode flips to
   LIVE, the hook returns `SKIP(reason="shadow_only_no_trade")` with a
   decision-record metadata payload (`metadata.would_trade=True`).

Both must be flipped before any real fire emits.

## Shadow -> live promotion plan

1. **SHADOW (this PR).** Sniper-band defaults, `shadow_only=1`.
2. **GHOST.** After 20+ shadow fires with WR >= 95% across both
   directions (sniper density is low — 50-fire bar would take a week),
   flip `shadow_only=0` and bump mode to GHOST.
3. **LIVE @ $5.** After 50+ GHOST fires sustaining 95%+ WR, promote to
   LIVE with `max_position_usd=5`. v17 is the high-precision lead
   candidate for *first dollar* tickformer fire.

## Cross-repo dependency

Forward-compatible with the timesfm sister PR. If
`probability_tickformer_v17` is missing from the surface, the hook
short-circuits to a clean `SKIP(reason="tickformer_v17_not_available")`.
No engine impact; merging is safe at any time.

## Monitoring queries

```sql
SELECT
    DATE_TRUNC('hour', created_at) AS hr,
    COUNT(*) FILTER (WHERE metadata->>'would_trade' = 'true') AS would_fires,
    skip_reason,
    COUNT(*)
FROM strategy_decisions
WHERE strategy_id = 'tickformer_v17_sniper'
  AND created_at > NOW() - INTERVAL '24 hours'
GROUP BY 1, 3
ORDER BY 1 DESC, 4 DESC;
```
