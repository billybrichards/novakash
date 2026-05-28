# tickformer_v16_pure — Magic Model Strategy

**Status:** SHADOW (Billy promotes manually per `feedback_no_auto_promote.md`)
**Asset:** ANY (5m timescale)
**Hook:** `engine/strategies/configs/tickformer_v16_pure.py::evaluate_tickformer_v16_pure`
**YAML:** `engine/strategies/configs/tickformer_v16_pure.yaml`
**Migration:** `migrations/add_tickformer_v16_pure_strategy.sql`

## What the magic model is

TickFormer v16 is a hybrid transformer that combines per-tick attention
with a multi-step autoregressive inference loop — internally referred to
as the "magic model" because the multi-step loop reconstructs the
residual price trajectory rather than predicting a single scalar.
Surfaces high-WR pockets that single-step LGB heads miss.

The model is trained and served by the **timesfm sister repo**
(magic-model branch). Inference produces:

- `probability_tickformer_v16` (NUMERIC, 0..1): P(window outcome = UP)
- `tickformer_trade_signal` (text: `UP` / `DOWN` / `HOLD`): coarse
  label derived service-side from probability + a residual-volatility
  filter

Both are emitted on `/v4/snapshot.timescales.5m` and via the new
`GET /v5/probability` + `GET /v5/trade_signal` endpoints. Strategies
consume them via `FullDataSurface.probability_tickformer_v16` and
`FullDataSurface.tickformer_trade_signal`.

## Conviction tier table

Offline backtest pockets from the v16 spec. `eval_offset_remaining` is
seconds left in the 5m window when the tick was scored (the magic model
degrades sharply as the window approaches expiry — late ticks lose
attention context).

| Tier | up_threshold | rem_min | rem_max | WR    | Notes                              |
|------|--------------|---------|---------|-------|------------------------------------|
| A    | 0.85         | 60      | 60      | ~95%  | tightest, late-window only         |
| B    | 0.90         | 60      | 60      | ~96%  | highest WR, very low fire density  |
| C    | 0.85         | 60      | 180     | ~94%  | **default** — best WR/density mix  |
| D    | 0.85         | 60      | 220     | ~88%  | dream tier — admits early ticks    |

DOWN side is symmetric: `down_threshold = 1 - up_threshold`. The trade
signal cross-check (`tickformer_trade_signal`) only blocks on an
explicit *opposite* label — `HOLD` or `None` pass through, since the
probability is the primary edge.

## Gate state requirements

- `chainlink_freshness` only (final-mile execution filter). The
  registry honours `StrategyDecision.action == "TRADE"` only when
  `gate_passed` is True.
- No cohort gates, no sister-pair veto, no `v9_ensemble` base
  delegation. The magic-model edge is independent of the LGB stack;
  clean shadow telemetry before stacking gates.

## SHADOW kill switch

Two independent layers prevent any live fire while we're collecting
data:

1. **`mode: SHADOW`** in YAML (and strategy_configs row).
2. **`gate_params.shadow_only=1`** in the hook itself. Even if mode is
   mis-set to LIVE, the hook returns `SKIP` with a decision-record
   metadata payload (`skip_reason="shadow_only_no_trade"`,
   `metadata.would_trade=True`).

Both must be flipped before any real fire emits. `shadow_only` is the
explicit go-live switch.

## Shadow -> live promotion plan

1. **SHADOW (this PR).** Tier C defaults, `shadow_only=1`. Strategy
   emits decision records on every qualifying tick but never trades.
   Monitor `metadata.would_direction` + outcome to compute paper WR.
2. **GHOST.** After 50+ shadow fires with WR >= 90% across both
   directions, flip `shadow_only=0` and bump mode to GHOST. Still no
   real money — the registry mode controls live execution
   independently.
3. **LIVE @ $5.** After 100+ GHOST fires sustaining 90%+ WR, promote
   to LIVE with `max_position_usd=5` and Tier A or B parameters per
   Billy's call. Raise position size only after another 100+ LIVE
   fires confirm the live-vs-paper delta is < 2pp.

## Expected trade density per day

Rough estimates from the v16 spec (subject to live-noise drift):

| Tier | UP+DOWN fires/day |
|------|-------------------|
| A    | ~5-15             |
| B    | ~2-8              |
| C    | ~20-40            |
| D    | ~30-60            |

Default Tier C targets ~30 paper fires/day — enough to hit the 50-fire
promotion threshold within 2 days.

## Monitoring queries

```sql
-- Shadow fire density + paper WR (after outcomes resolve)
SELECT
    DATE_TRUNC('hour', created_at) AS hr,
    COUNT(*) FILTER (WHERE metadata->>'would_trade' = 'true') AS would_fires,
    COUNT(*) FILTER (
        WHERE metadata->>'would_direction' = 'UP'
          AND outcome = 'UP'
    ) AS up_wins,
    COUNT(*) FILTER (
        WHERE metadata->>'would_direction' = 'DOWN'
          AND outcome = 'DOWN'
    ) AS down_wins
FROM strategy_decisions
WHERE strategy_id = 'tickformer_v16_pure'
  AND created_at > NOW() - INTERVAL '24 hours'
GROUP BY 1
ORDER BY 1;

-- SKIP reason distribution
SELECT
    skip_reason,
    COUNT(*)
FROM strategy_decisions
WHERE strategy_id = 'tickformer_v16_pure'
  AND created_at > NOW() - INTERVAL '24 hours'
GROUP BY 1
ORDER BY 2 DESC;
```

## Cross-repo dependency

This engine PR is **forward-compatible** with the timesfm sister PR.
If the timesfm-side emission isn't live yet:

- `probability_tickformer_v16` will be `None` on every surface.
- The hook short-circuits to a clean
  `SKIP(reason="tickformer_v16_not_available")` — no exceptions.
- No engine impact; merging is safe at any time.

Once the timesfm-side emission lands and the columns populate, this
strategy starts producing shadow decisions immediately.
