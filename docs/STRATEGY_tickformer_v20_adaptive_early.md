# tickformer_v20_adaptive_early — Adaptive-K Early-Entry Strategy

**Status:** SHADOW (Billy promotes manually per `feedback_no_auto_promote.md`)
**Asset:** ANY (5m timescale)
**Hook:** `engine/strategies/configs/tickformer_v20_adaptive_early.py::evaluate_tickformer_v20_adaptive_early`
**YAML:** `engine/strategies/configs/tickformer_v20_adaptive_early.yaml`
**Migration:** `migrations/add_tickformer_v20_strategy.sql`
**Rollback:** `migrations/rollback_tickformer_v20_strategy.sql`

## Profile — adaptive early-entry @ t-217s

TickFormer v20 is the **adaptive-K-aware retrain** (K=6 multi-step
loop) with a custom training loss that rewards firing early at high
conviction (and penalises late churn). Its defining property is that
at thr 0.90 K=6 the model fires at mean `eval_offset_remaining`
**~217.3s** — i.e. ~22-23s into the 5min window — at **91% WR**.

This is the **earliest entry of any tickformer variant** by a wide
margin:

| Variant | Mean fire-eo | Typical thr | Typical WR |
|---------|-------------:|------------:|-----------:|
| v17 sniper            | ~ t-60  | 0.85 | ~98% |
| v18 t180              | ~ t-180 | 0.90 | ~92% |
| **v20 adaptive-early** | **~ t-217** | **0.90** | **91%** |

Expected density at the recommended tier: **~15.9 trades/day** combined
UP+DN at ~91% WR.

## Conviction tier table (v20 backtest — RDS hub note #726)

| Tier | up_threshold | K | n_fired | mean_k | mean_fire_eo | WR    | Trades/day |
|------|--------------|---|---------|--------|--------------|-------|------------|
| Loose  | 0.85 | 6  | 227 | 1.20 | 217.6 | 85.9% | 32.6 |
| Mid    | 0.88 | 6  | 156 | 1.16 | 217.7 | 87.8% | 22.5 |
| **Recommended** | **0.90** | **6** | **111** | **1.36** | **217.3** | **91.0%** | **15.9** |
| Tight  | 0.92 | 6  | 61  | 1.33 | 217.3 | 90.2% |  8.7 |
| K=15   | 0.85 | 15 | 230 | 1.30 | 217.4 | 86.1% | 32.7 |

DOWN side is symmetric: `down_threshold = 1 - up_threshold` (default 0.10).
The trade signal cross-check (`tickformer_trade_signal`) only blocks on
an explicit *opposite* label.

## DO NOT raise `up_threshold` above 0.90 (WR ceiling)

The v20 backtest data shows that **WR saturates at thr 0.90**. Pushing
to thr 0.92 *reduces* trade volume by nearly half (15.9 -> 8.7 fires/day)
while WR slightly *drops* (91.0% -> 90.2%). There is no WR benefit to
tightening further — only volume loss.

If a runtime override raises `up_threshold` above 0.90 without
fresh backtest evidence, that change should be reverted. The hard
ceiling for this strategy is **0.90**.

## K=6 multi-step loop — warm-up caveat

v20's K=6 adaptive loop benefits from buffer warm-up — the same caveat
that applies to v18. The first few ticks after engine boot may emit
`tickformer_v20_not_available` SKIPs until the K-step buffer is
primed. This is **expected and not an error** — the symptom is
identical to the v18 warm-up window.

## SHADOW kill switch (two layers)

1. **`mode: SHADOW`** in YAML and `strategy_configs` row.
2. **`gate_params.shadow_only=1`** in the hook. Forces
   `SKIP(reason="shadow_only_no_trade")` even if mode is mis-set.

Both must be flipped before any real fire emits.

## Shadow -> ghost -> live promotion plan

1. **SHADOW (this PR).** Recommended thr 0.90, broad band `[60, 240]`,
   `shadow_only=1`. Observe 5-10 days.
2. **GHOST.** After 50+ shadow fires with WR >= 88% across both
   directions, flip `shadow_only=0` and promote to GHOST.
3. **LIVE @ $5.** After 100+ GHOST fires sustaining ~90% WR, promote
   to LIVE with `max_position_usd=5`. Compare side-by-side with v18
   (t-180); v20 should yield earlier fills at comparable WR — the
   key promotion question is whether the t-217s entry materially
   improves fill price vs v18's t-180 entry.

## Cross-strategy interaction (v16 / v17 / v18 / v20)

All four tickformer strategies can fire on the same window. They are
designed to be **additive** — each emits an independent SHADOW decision
record. Because all default `shadow_only=1`, there is no
competing-for-fill risk at PR-merge time.

The mutex-group `tickformer` (PR #619 FIX 2) gates LIVE promotion so
that only the highest-`confidence_score` sibling fires when multiple
converge.

## Cross-repo dependency

Forward-compatible with a (future) timesfm sister PR. If
`probability_tickformer_v20` is missing from the surface, the hook
short-circuits to a clean `SKIP(reason="tickformer_v20_not_available")`.

The v20 model is **currently a research checkpoint** (not deployed to
classifier-gpu yet). Until the v20 emission PR lands on
timesfm-service, this strategy will SKIP-clean every window in prod
with `reason="tickformer_v20_not_available"`. That is the intended
behaviour — the engine PR ships in lock-step with the eventual
emission so no further engine-side change is required at promotion.

## Monitoring queries

See `docs/TICKFORMER_GHOST_PROTOCOL.md` for the canonical rollup
patterns (window_snapshots JOIN, CLOB-aware shadow PnL). Quick spot
check:

```sql
SELECT
    DATE_TRUNC('hour', created_at) AS hr,
    COUNT(*) FILTER (WHERE metadata->>'would_trade' = 'true') AS would_fires,
    skip_reason,
    COUNT(*)
FROM strategy_decisions
WHERE strategy_id = 'tickformer_v20_adaptive_early'
  AND created_at > NOW() - INTERVAL '24 hours'
GROUP BY 1, 3
ORDER BY 1 DESC, 4 DESC;
```

Volume + mean fire-eo sanity check (should match the ~217s headline):

```sql
SELECT
    DATE_TRUNC('day', created_at) AS d,
    COUNT(*) FILTER (WHERE metadata->>'would_trade' = 'true') AS would_fires,
    AVG((metadata->>'eval_offset_remaining')::numeric)
        FILTER (WHERE metadata->>'would_trade' = 'true') AS mean_eo_remaining
FROM strategy_decisions
WHERE strategy_id = 'tickformer_v20_adaptive_early'
  AND created_at > NOW() - INTERVAL '7 days'
GROUP BY 1
ORDER BY 1 DESC;
```

## References

- `magic-model/V20_RESULTS.md` — full backtest report
- RDS hub note #726 — backtest summary
- `docs/STRATEGY_tickformer_v18_t180.md` — sibling profile (t-180 anchor)
- `docs/TICKFORMER_GHOST_PROTOCOL.md` — promotion + monitoring playbook
