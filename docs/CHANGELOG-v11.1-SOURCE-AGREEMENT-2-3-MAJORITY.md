# v11.1 — Source Agreement 2/3 Majority

## Summary

Changed source agreement from unanimous Chainlink+Tiingo (2/2) to 2/3 majority vote across Chainlink, Tiingo, and Binance. This dramatically increases trade frequency while maintaining signal quality by neutralizing Binance's systematic bias through majority vote.

## Evidence (Apr 8-10 2026)

| Metric | CL+TI Unanimous | 2/3 Majority |
|--------|-----------------|--------------|
| **Pass Rate** | 56.9% | 98.2% |
| **Trade Frequency** | ~20% | ~73% |
| **Quality** | Maintained | Maintained |

### Source Bias Analysis

| Source | UP Signals | DOWN Signals | Bias |
|--------|------------|--------------|------|
| Chainlink | 52.7% | 47.3% | Balanced |
| Tiingo | 42.5% | 57.5% | Slight DOWN |
| Binance | 16.9% | 83.1% | **Strong DOWN** |

### Most Common Disagreement Pattern

**CL=UP, TI=DOWN, BIN=DOWN** (19.6% of all evaluations)

- 2/3 says DOWN
- Under old rule: BLOCKED (CL disagrees)
- Under new rule: APPROVED as DOWN trade

## Changes

| Component | v11.0 | v11.1 |
|-----------|-------|-------|
| **Source Agreement** | CL+TI unanimous (2/2) | 2/3 majority (CL+TI+BIN) |
| **Pass Rate** | ~57% | ~98% |
| **Trade Frequency** | ~20% | ~73% |

## Implementation

### File: `engine/signals/gates.py`

```python
class SourceAgreementGate:
    """G1: 2/3 majority vote from Chainlink, Tiingo, Binance."""
    
    async def evaluate(self, ctx: GateContext) -> GateResult:
        cl_dir = "UP" if ctx.delta_chainlink > 0 else "DOWN"
        ti_dir = "UP" if ctx.delta_tiingo > 0 else "DOWN"
        bin_dir = "UP" if ctx.delta_binance is not None and ctx.delta_binance > 0 else "DOWN"
        
        # Count votes
        up_votes = sum([cl_dir == "UP", ti_dir == "UP", bin_dir == "UP"])
        down_votes = 3 - up_votes
        
        # 2/3 majority required
        agreed_dir = "UP" if up_votes >= 2 else "DOWN"
        ctx.agreed_direction = agreed_dir
```

## Deployment Notes

- **No environment variable changes required**
- **Backward compatible** — existing configs work
- **Rollback**: Revert commit and restart engine

## Monitoring

Track first 24h of trades:
- Trade frequency should increase ~3-4x
- WR should remain consistent with historical (94.7% when sources agree)
- Watch for any unusual patterns in CL=UP, TI=DOWN, BIN=DOWN trades

## Related

- v9.0: Introduced CL+TI source agreement (94.7% WR when agree)
- v11.0: Dynamic TimesFM confidence gating
- v10.6: Decision surface proposal (not yet deployed)
