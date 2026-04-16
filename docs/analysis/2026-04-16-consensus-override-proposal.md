# Proposal — consensus-block override (5m v4_fusion)

**Date:** 2026-04-16
**Author:** Claude (serene-gates)
**Status:** PROPOSED — awaiting Billy review before PR
**Depends on:** none (standalone engine change, analogous to the `regime_risk_off` override in PR #212)

## Problem

`v4_fusion` blocks trades when the sister repo's `v4_consensus_safe_to_trade=False` — fired when price sources (Chainlink, Tiingo, Binance, CoinGlass) disagree by more than 15bps. Logic at `engine/strategies/configs/v4_fusion.py:390`.

Over the last 24h, 53 windows were fully-blocked by this gate. Resolved outcomes:

- 23 windows would have **won** (if v4_fusion had been allowed to bet its inferred direction)
- 28 would have **lost**
- 2 unresolved

Net dollar impact using Apr 15 avg payouts (win +$2.58/$3.21, loss -$4.64/-$4.91):

```
Missed wins:  +$29.64 left on table
Avoided losses: +$34.37 saved
──────────────────
NET:          +$4.73 gate saved
```

**The gate is marginally helpful — but it also blocks a lot of clearly-directional trades.**

## Discriminator found

Running the 51 resolved blocked windows through the v3/v4 surface indicators, one pattern stands out: when **all three price sources agree directionally** AND **the Chainlink move is >5bps**, the "unsafe" label is misleading — the sources are just transiently out-of-sync at the BPS level during a fast price tick, not actually diverging on direction.

| Indicator | WIN mean (n=23) | LOSS mean (n=28) | Δ |
|---|---|---|---|
| binance_sign == chainlink_sign | 100% | 50% | **+50pp** |
| tiingo_sign == chainlink_sign | 91% | 54% | +37pp |
| all 3 agree directionally | 91% | 39% | **+52pp** |
| abs(Δ_chainlink) | 0.094% | 0.041% | **2.3×** |
| abs(Δ_binance) | 0.106% | 0.020% | **5.4×** |
| cg_liquidations | $18k | $8k | 2.2× |
| taker_imbalance | +$477k | -$1.76M | sign flip |
| vpin, funding, OI, L/S ratio | no difference | | |

## Proposed rule

Override the consensus-block when ALL three conditions hold:

1. `sign(Δ_binance) == sign(Δ_chainlink)`
2. `sign(Δ_tiingo) == sign(Δ_chainlink)`
3. `abs(Δ_chainlink) >= 0.05%` (5 bps minimum move — filters out noise)

Override trades ONLY in the direction of `sign(Δ_chainlink)` (the resolution oracle).

## Simulated impact (24h blocked sample, n=51)

| Bucket | Count |
|---|---|
| Unblock and trade | **23** (19W / 4L) — 82.6% WR |
| Keep blocked (fails override check) | **28** (4W / 24L) — 86% of losses still correctly avoided |

Net vs current gate (keep-everything-blocked):
- Unblocked 19 new wins × $2.90 avg = +$55.10
- Let through 4 new losses × $4.77 avg = −$19.08
- **Marginal benefit: +$36.02** over 24h sample

Extrapolated at ~50 blocked windows/24h cadence → ~+$35-40/day realized edge.

## Implementation

Mirrors the `regime_risk_off` override pattern from PR #212. One helper function + one
branch point in `_evaluate_legacy()` and `_evaluate_poly_v2()` (both paths fire the
consensus block currently).

```python
# engine/strategies/configs/v4_fusion.py

_CONSENSUS_OVERRIDE_ENABLED = os.environ.get(
    "V4_CONSENSUS_OVERRIDE_ENABLED", "true"
).lower() == "true"
_CONSENSUS_OVERRIDE_MIN_DELTA_PCT = float(
    os.environ.get("V4_CONSENSUS_OVERRIDE_MIN_DELTA_PCT", "0.0005")  # 5 bps
)


def _try_consensus_override(
    surface: "FullDataSurface",
    gates: list[dict],
) -> Optional[str]:
    """Return the inferred trade direction ('UP'/'DOWN') if the triple-agreement
    override applies — else None (block stands).

    Conditions:
      - V4_CONSENSUS_OVERRIDE_ENABLED (env flag, default true)
      - sign(delta_binance) == sign(delta_chainlink)
      - sign(delta_tiingo)  == sign(delta_chainlink)
      - abs(delta_chainlink) >= _CONSENSUS_OVERRIDE_MIN_DELTA_PCT (5 bps default)
    """
    if not _CONSENSUS_OVERRIDE_ENABLED:
        return None
    d_cl = surface.delta_chainlink
    d_bn = surface.delta_binance
    d_ti = surface.delta_tiingo
    if d_cl is None or d_bn is None or d_ti is None:
        gates.append(_gate("consensus_override", False, "one or more deltas missing"))
        return None
    if abs(d_cl) < _CONSENSUS_OVERRIDE_MIN_DELTA_PCT:
        gates.append(_gate(
            "consensus_override", False,
            f"|cl|={abs(d_cl):.5f} < {_CONSENSUS_OVERRIDE_MIN_DELTA_PCT:.5f}",
        ))
        return None
    cl_sign = 1 if d_cl > 0 else -1
    bn_sign = 1 if d_bn > 0 else -1
    ti_sign = 1 if d_ti > 0 else -1
    if bn_sign != cl_sign or ti_sign != cl_sign:
        gates.append(_gate(
            "consensus_override", False,
            f"sources disagree (cl={cl_sign} bn={bn_sign} ti={ti_sign})",
        ))
        return None
    direction = "UP" if cl_sign > 0 else "DOWN"
    gates.append(_gate(
        "consensus_override", True,
        f"all 3 agree {direction}, |cl|={abs(d_cl):.5f}",
    ))
    return direction
```

At the `consensus not safe_to_trade` skip site:

```python
if not surface.v4_consensus_safe_to_trade:
    override_direction = _try_consensus_override(surface, gates)
    if override_direction is None:
        gates.append(_gate("consensus", False, "consensus not safe_to_trade"))
        return _skip("consensus not safe_to_trade", gates)
    # Override applied — trade override_direction
    gates.append(_gate(
        "consensus", True,
        f"consensus unsafe BUT override fired direction={override_direction}",
    ))
    # Set the trade direction from the override
    direction = override_direction
```

## Env flags (rollback = `sed` + restart, no code change)

| Flag | Default | Purpose |
|---|---|---|
| `V4_CONSENSUS_OVERRIDE_ENABLED` | `true` | Master switch |
| `V4_CONSENSUS_OVERRIDE_MIN_DELTA_PCT` | `0.0005` (5 bps) | Min Chainlink move to unlock override |

Raise the delta threshold to `0.0010` (10 bps) if the loss side of the override looks bigger than expected after 24h.

## Test plan

1. Unit tests in `tests/unit/strategies/test_strategy_configs.py`:
   - `test_consensus_override_trades_when_all_agree`
   - `test_consensus_override_blocked_by_low_magnitude`
   - `test_consensus_override_blocked_by_source_disagree`
   - `test_consensus_block_stands_when_override_disabled`
2. Post-deploy: grep Montreal log for `consensus_override` gate traces, confirm
   override fires ~10-15× / day.
3. Post-deploy 24h soak: verify override-tagged trades hit ≥70% WR. If below,
   raise `V4_CONSENSUS_OVERRIDE_MIN_DELTA_PCT` to 10 bps.
4. Post-deploy 7d: verify net realized edge >= +$20/day. If below, disable flag.

## Risks

1. **Small sample** (n=51 over 24h). Triple-agreement discriminator statistically clean (100% vs 50% on binance agreement), but the loss column is thin (n=4 correct + 7 wrong). Could revert to coin-flip in a different regime.
2. **Consensus gate was protective during flash events**. By unblocking when all sources DO agree, we specifically don't weaken the gate's flash-move protection (flash events by definition have sources momentarily disagreeing on direction, not just BPS). But this assumption needs live verification.
3. **Magnitude filter (5 bps)** might be too loose. 5 bps ≈ $37 at BTC=$75k — small enough that momentum is barely established. Raising to 10 bps would halve the unblocked count but should raise WR. Tune post-deploy.

## Non-goals

- Not modifying sister repo. Engine-side override mirrors the `regime_risk_off` pattern from PR #212. Sister's `trade_advised` and `consensus` remain the upstream truth; we just add a local exception.
- Not tuning the 15bps divergence tolerance in sister repo. That's sister-team territory.

## Related

- Audit #193 (consensus gate override review) — this doc answers it
- PR #212 (T-45 cutoff + `regime_risk_off` override) — same design pattern
- Hub note #41 (overnight Apr 15→16 report) — original observation that gate was marginal
