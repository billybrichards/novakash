# Macro surface audit — 2026-04-11

## Summary

24-hour audit of the margin engine's paper-mode behaviour since the v4 fusion surface went live (2026-04-10 21:20 UTC through 2026-04-11 13:09 UTC) found that the engine opened zero positions in the ~15 hours between 22:10 UTC yesterday and the audit moment. The v4 gate stack was active and the skip-reason distribution looked "healthy" at first glance — 95% CHOPPY/NO_EDGE regime, 73% macro BEAR — but drilling into the actual predictive quality of each source showed three broken wiring decisions bleeding the engine.

This doc records the findings, maps each to the Phase A / B / C remediation work, and captures the one suspicious edge that needs a 7-day replay before it gets promoted.

## The numbers that matter

| Source | 15m directional hit rate | Average actual 15m move | Edge |
|---|---:|---:|---|
| **Qwen macro BEAR** (n=286) | **30.1%** | **+4.6 bps UP** | **Anti-predictive** |
| Qwen macro BEAR — 4h horizon (n=142) | 20.4% | +16.6 bps UP | Worse at longer horizons |
| v2 `p_up >= 0.65` "strong LONG" (n=2,730) | 40.7% | -2.9 bps | No edge |
| v2 `p_up < 0.35` "strong SHORT" (n=3,234) | 36.9% | +2.1 bps | No edge |
| **TimesFM `expected_move > 3 bps`** (n=1,176) | **76.6%** | **+6.5 bps** | **Real edge** |
| TimesFM `exp_move > 3` + NO_EDGE + BEAR (n=74) | **100%** | **+22.8 bps** | **Real edge (needs 7d replay)** |

A coin flip is 50%. Qwen's BEAR calls hit the correct direction 20-30% of the time across three different horizons — **systematically wrong, not uninformative**.

## Root cause 1 — Qwen cannot do numeric fusion

LLMs pattern-match text. They are not calibrated probability assessors and do not compute on raw numeric features like `funding_rate = -0.007712`. When Qwen sees the same input vector that a gradient-boosted tree would consume correctly (VPIN, funding rate, oracle ratios, price deltas, OI delta, taker ratio, top-trader long %, etc.), it synthesises a "bearish setup" narrative from its text-training latent space — losing the 3 decimal places of precision that actually matter for 15m scalping.

Three sub-failures compound:

1. **Training-corpus bias toward "safe" answers.** The prompt allows NEUTRAL, but Qwen emits directional calls 75% of the time. Observed distribution: 73% BEAR / 25% NEUTRAL / 1% BULL. That's not market reality — it's mode-seeking behaviour against a schema enum.
2. **Feature precision loss.** A raw `funding_rate: -0.007712` becomes "the funding rate is slightly negative" in Qwen's latent space. The sign survives; the magnitude does not.
3. **Horizon confusion.** The prompt asks Qwen to reason about 5m/15m/1h/4h simultaneously from one prompt. In practice Qwen pattern-matches to medium-term financial news sentiment (days to weeks) and then project those onto every horizon.

**Remediation:** Phase C — replace Qwen's bias-producer role with a per-horizon LightGBM classifier (`MacroV2Classifier`) trained on the exact same numeric inputs. Keep Qwen running on the observer box for audit comparison. Full Phase C plan in `/Users/billyrichards/.claude/plans/sleepy-forging-cerf.md`.

## Root cause 2 — the 15m model the engine consumes is 3 days stale

Three parallel writers are active in `ticks_v2_probability`:

| model_version | Source | Age |
|---|---|---|
| `15a4e3e@v2/btc/btc_5m/15a4e3e/2026-04-10T13-21-30Z` | **Sequoia v5 5m** — live, auto-retrained 4h cadence, ECE 0.0643, skill +7.23pp | fresh |
| `CEDAR/nogit@v2/btc/btc_5m/nogit/2026-04-08T04-01-23Z` | Cedar 5m shadow | 3 days |
| `15m/nogit@v2/btc/btc_15m/nogit/2026-04-08T15-52-17Z` | **15m slot the engine is reading via `MARGIN_V4_PRIMARY_TIMESCALE=15m`** | **3 days** |

`/v2/health` correctly reports `model_version: 15a4e3e` (Sequoia v5 5m), but the margin engine's `primary_timescale` is `15m`, so it consumes the other slot — `current_15m.json` pointing at a manually-loaded artifact from 2026-04-08, never touched by any auto-retrain cycle.

The Phase 4 matrix in `.github/workflows/retrain.yml` already has a `15m/polymarket → current_15m` cell. It has either never run to completion, never passed the promotion gate, or uploaded candidates without flipping `current_15m.json`. Audit in Phase B will identify which.

**Remediation:**
- **Phase A (this PR):** flip `MARGIN_V4_PRIMARY_TIMESCALE=5m` so the engine consumes the known-good Sequoia v5 5m model immediately. No code change, no new model, one env var.
- **Phase B:** fix the `15m/polymarket` retrain matrix cell so the 15m slot gets fresh auto-promoted models, then decide whether to flip the engine back to 15m or keep 5m as primary.

## Root cause 3 — the engine drops per-horizon macro bias entirely

`margin_engine/domain/value_objects.py:_parse_macro()` reads only the 8 scalar fields from the `overall` block of `/v4/macro`. The `timescale_map` key — which carries the per-horizon BEAR/BULL/NEUTRAL breakdown Qwen produces (5m/15m/1h/4h) — is silently dropped at parse time.

Consequence: the engine applies `overall` as a hard veto on every timescale, even when the 15m map entry is NEUTRAL and the 1h entry is BULL. The frontend's V4Panel shows the divergence correctly because it reads `/v4/macro` directly; the engine does not.

**Remediation (Phase A, this PR):**
- Extend `MacroBias` with a `timescale_map: dict` field and a `for_timescale(ts)` accessor.
- Extend `_parse_macro()` to pull `d.get("timescale_map")`.
- Do NOT yet change the gate logic to read from the map — that's Phase C's job once the macro_v2 classifier produces per-horizon biases directly.
- Phase A only makes the field available so Phase C can consume it without another engine deploy.

## What Phase A actually changes

1. **`MARGIN_V4_PRIMARY_TIMESCALE=5m`** — `.env` templating on every deploy (idempotent). Engine consumes Sequoia v5 5m instead of the stale 15m slot.
2. **Macro advisory mode** — Qwen's `direction_gate` is demoted from hard veto to advisory by default. `MARGIN_ENGINE_USE_V4_MACRO_MODE=advisory` (new setting). Two axes control the gate:
   - `status == "ok"`: unavailable macro is always a no-op
   - `confidence >= 80`: below-floor macro is always a no-op (prevents a flat NEUTRAL/0 fallback row from silently scaling every trade)
   - Above both: `mode="veto"` → hard skip with `macro_skip_*_veto` reason; `mode="advisory"` → size multiplier haircut of 0.75x on conflict, continue gate walk
3. **Parallel advisory logic on the continuation path** in `ManagePositionsUseCase` so existing open positions are NOT force-closed on a macro flip.
4. **`timescale_map` parse passthrough** in `MacroBias` and `_parse_macro()`. Not yet read by gate logic.
5. **NO_EDGE experimental entry path (SHIPPED OFF)** — `MARGIN_V4_ALLOW_NO_EDGE_IF_EXP_MOVE_BPS_GTE` setting, disabled by default. Exists so the code is ready when the 7-day replay confirms the NO_EDGE+exp_move>3 edge is real.
6. **14 unit tests** covering advisory/veto dispatch, confidence floor, size haircut, continuation-path advisory, `MacroBias.for_timescale()`, and `_parse_macro()` with/without `timescale_map`.

## Rollback

Any one of:
- Revert this PR on `novakash` develop
- `MARGIN_ENGINE_USE_V4_MACRO_MODE=veto` on the host `.env` + restart
- `MARGIN_V4_PRIMARY_TIMESCALE=15m` on the host `.env` + restart (reverts to the stale-model pre-Phase-A behaviour)

## Monitor: NO_EDGE + BEAR + exp_move > 3

**Do NOT flip `MARGIN_V4_ALLOW_NO_EDGE_IF_EXP_MOVE_BPS_GTE` until the 7-day replay confirms the edge persists.**

The 2026-04-11 audit found a 74-sample bucket — margin-engine 24h window, v4 entry-skip log rows where `regime=NO_EDGE` AND `macro=BEAR` AND `expected_move > 3 bps` — with:

- **100.0% directional hit rate** (every single sample had BTC move up at +15m)
- **Average actual 15m move: +22.8 bps**
- **After 18 bps round-trip fees: +4.8 bps net per trade**
- Sample size: n=74 (~3 samples per hour during the observation window)

This is suspiciously clean. A 100% hit rate on any finite sample is either a real durable edge or a selection-biased window where BTC happened to drift up while TimesFM happened to predict up. The audit was a 24h window — BTC was recovering from a spike-liquidation cascade during most of it, which is exactly the regime where TimesFM's p90 tail forecast pulls upward while v2's regime classifier reports NO_EDGE (because recent 5m autocorrelation is low) and Qwen reports BEAR (because funding is negative and VPIN is elevated). All three signals can be simultaneously wrong in a single direction.

### Monitoring plan

Phase A ships the code (the `MARGIN_V4_ALLOW_NO_EDGE_IF_EXP_MOVE_BPS_GTE` setting and its `open_position.py` implementation) but leaves the flag at `None` (off). After Phase B lands and the 15m model is fresh:

1. **Nightly backtest job** — replay the last rolling 7 days of `v4_snapshots` × `ticks_binance` entries through a synthetic filter matching the `NO_EDGE + BEAR + exp_move > 3` gate. Compute: (a) sample count, (b) 15m directional hit rate, (c) average actual move, (d) PnL net of 18 bps fees.
2. **New `edge_audit` table** — one row per nightly run. Columns: `date, sample_n, hit_rate_pct, avg_actual_bps, net_pnl_bps, window_start, window_end`.
3. **Promotion criterion** — if the rolling 7-day hit rate stays **above 65%** AND sample size stays **above 200** AND the net PnL bps stays **positive** for 3 consecutive nights, flip `MARGIN_V4_ALLOW_NO_EDGE_IF_EXP_MOVE_BPS_GTE=3.0` in the `deploy-margin-engine.yml` `.env` templating step.
4. **Demotion criterion** — if any of the above fails for 2 consecutive nights after flip, revert.

**The hit rate will not stay at 100%.** Anything above 60% with positive net PnL is a keeper; anything at/below 50% is a regime-specific artifact that should stay off.

## Appendix — raw audit query outputs

### Qwen bias hit rate vs actual BTC moves (last 24h)

```
bias    | direction_gate | n   | avg_15m_bps | dir_hit_15m | avg_4h_bps | dir_hit_4h
--------+----------------+-----+-------------+-------------+------------+-----------
BEAR    | SKIP_UP        | 286 | +4.6 bps    | 30.1%       | +16.6 bps  | 20.4%
BULL    | SKIP_DOWN      |   4 | +0.4 bps    | 50.0%       |  n/a       | n/a
NEUTRAL | ALLOW_ALL      |  67 | +3.7 bps    | n/a         |  -3.0 bps  | n/a
```

### v2 probability bucket hit rate (last 24h, from margin engine's v4 skip logs)

```
p_up_bucket                | n    | avg_15m_bps | long_hit_pct | short_hit_pct
---------------------------+------+-------------+--------------+--------------
p_up < 0.35 (strong SHORT) | 3234 | +2.1        |              | 36.9
p_up 0.35-0.45             | 8844 | -1.4        |              | 51.7
p_up 0.45-0.55 (flat)      | 6144 | -1.3        | 49.3         | 42.2
p_up 0.55-0.65             | 5143 | -1.2        | 50.5         |
p_up >= 0.65 (strong LONG) | 2730 | -2.9        | 40.7         |
```

The "strong LONG" bucket — where the v2 scorer is most confident — has a **40.7% directional hit rate and negative expected value.** This is the 15m/nogit stale-model consequence.

### TimesFM expected_move bucket (the one source with real edge)

```
bucket              | n     | avg_predicted | avg_actual_15m | dir_hit_pct
--------------------+-------+---------------+----------------+------------
exp_move < -3       | 3050  | -5.1          | -1.1           | 45.1
exp_move -3 to -1   | 7774  | -1.8          | -1.7           | 50.5
exp_move -1 to 1    | 11417 | -0.2          | -1.8           | 51.0
exp_move 1 to 3     | 2678  | +1.7          | +0.7           | 53.2
exp_move > 3        | 1176  | +4.6          | +6.5           | 76.6
```

`exp_move > 3` is the only bucket with skill materially above coin-flip. This is what Phase C will expose to the gate stack as a primary entry trigger.

### The suspiciously-clean 74-sample bucket breakdown

```
regime         | macro_bias | n   | avg_predicted | avg_actual_15m | long_hit | net_pnl_after_fees
---------------+------------+-----+---------------+----------------+----------+-------------------
NO_EDGE        | BEAR       |  74 | +3.6          | +22.8          | 100.0%   | +4.8
CHOPPY         | BEAR       |  48 | +3.8          | +14.0          |  81.3%   | -4.0
MEAN_REVERTING | NEUTRAL    | 127 | +4.6          | +11.3          |  89.8%   | -6.7
CHOPPY         | NEUTRAL    | 839 | +4.8          | +4.9           |  74.3%   | -13.1
NO_EDGE        | NEUTRAL    |  35 | +3.5          | -5.6           |  71.4%   | -23.6
TRENDING_DOWN  | BEAR       |   9 | +5.1          | -11.9          |   0.0%   | -29.9
TRENDING_DOWN  | NEUTRAL    |   6 | +5.4          | -19.3          |   0.0%   | -37.3
```

Only **NO_EDGE + BEAR** is net-profitable after fees. The other 6 buckets have positive average moves but the variance plus the 18 bps fee cost eats the edge. This is why Phase A ships the code but leaves the flag off — we need the 7-day replay to confirm the NO_EDGE+BEAR bucket survives across regime changes.
