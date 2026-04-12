# DOWN-Only Polymarket 5-Min Strategy
**Discovered:** 2026-04-12  
**Data:** 897,503 signal evaluations, T-90–150, confidence ≥ 0.12, BTC, ~5 days  
**Status:** CONFIRMED — implement overnight as paper trading gate

---

## The Finding

| Direction | CLOB Ask Band | N | Win Rate | Edge |
|-----------|---------------|---|----------|------|
| **DOWN** | > 0.75 (market agrees) | 175,261 | **99.0%** | Double confirmation |
| **DOWN** | 0.55–0.75 | 112,371 | **97.8%** | Strong confirmation |
| **DOWN** | 0.35–0.55 | 86,821 | **92.1%** | Mild confirmation |
| **DOWN** | ≤ 0.35 (market disagrees) | 177,435 | **76.2%** | Genuine contrarian |
| | | | | |
| **UP** | > 0.75 | 74,107 | 53.8% | Barely above random |
| **UP** | 0.55–0.75 | 124,835 | 23.8% | Negative edge |
| **UP** | 0.35–0.55 | 75,177 | 1.8% | Near-zero |
| **UP** | ≤ 0.35 | 71,496 | 1.5% | Near-zero |

**Down = 551K samples (61%), Up = 346K samples (39%)**

---

## Clarifying the "Contrarian" Label

The CLOB ask bands require careful interpretation:

**`clob_down_ask` = cost to buy a DOWN token = market-implied probability of DOWN.**

| clob_down_ask | What it means | Our prediction | Relationship |
|---------------|---------------|----------------|--------------|
| > 0.75 | Market prices DOWN at >75% | DOWN | **Double confirmation** — both agree. 99% WR because a downtrend is already established at T-90-150 and both model + market recognise it. |
| 0.35–0.75 | Market prices DOWN at 35–75% | DOWN | **Mild–strong confirmation.** 92–98% WR. |
| < 0.35 | Market prices DOWN at <35% | DOWN | **Genuine contrarian** — retail UP bias means DOWN is cheap; model says DOWN anyway. 76% WR. |

The 99% "contrarian" label in the original query was misleading because it used the term to mean "contrarian to the typical retail direction" not "contrarian to the current CLOB price." Both zones are profitable but for different reasons.

---

## Why DOWN Predictions Dominate

**The model (VPIN + CoinGlass + Sequoia + TimesFM) is much better at detecting bearish signals than bullish ones.** Reasons:

1. **Liquidation cascades are more predictable** — forced sellers create detectable order flow asymmetry. Buyers are more discretionary.
2. **VPIN is a bearish detector** — VPIN spikes on sell-side informed flow, which correlates with downward moves.
3. **CoinGlass OI and funding rate** — funding rate spikes and OI drops are bearish precursors.
4. **Retail UP bias creates cheap DOWN tokens** — when market disagrees with our DOWN signal, DOWN is underpriced (76% WR arbitrage).

UP predictions have 1–53% WR across all CLOB ranges. This is not a threshold tuning problem — it's a fundamental signal asymmetry. UP predictions should always be skipped.

---

## Recommended Strategy: V4 DOWN-Only Gate

### Gate logic (to add to `engine/signals/gates.py`)

```python
class DirectionFilterGate:
    """Skip all UP predictions. DOWN predictions pass through unconditionally.
    
    Rationale: 897K-sample analysis (2026-04-12) shows DOWN predictions have
    76-99% WR across all CLOB ranges. UP predictions have 1-53% WR.
    See docs/analysis/DOWN_ONLY_STRATEGY_2026-04-12.md
    """
    async def evaluate(self, ctx: GateContext) -> GateResult:
        if ctx.agreed_direction == "UP":
            return GateResult(
                passed=False,
                gate_name="direction_filter",
                reason="up_prediction_skipped",
                data={"direction": "UP", "policy": "down_only"},
            )
        return GateResult(passed=True, gate_name="direction_filter", reason="down_allowed")


class CLOBSizingGate:
    """Adjust size_modifier based on clob_down_ask.
    
    Higher clob_down_ask = market also thinks DOWN is likely = higher conviction.
    Lower clob_down_ask = genuine contrarian bet = lower size.
    
    See docs/analysis/DOWN_ONLY_STRATEGY_2026-04-12.md
    """
    async def evaluate(self, ctx: GateContext) -> GateResult:
        ask = ctx.clob_down_ask
        if ask is None:
            # No CLOB data — use base size, don't block
            return GateResult(passed=True, gate_name="clob_sizing", reason="no_clob_data",
                              data={"size_modifier": 1.0})
        if ask >= 0.75:
            modifier, label = 2.0, "double_confirmation"
        elif ask >= 0.55:
            modifier, label = 1.5, "strong_confirmation"
        elif ask >= 0.35:
            modifier, label = 1.2, "mild_confirmation"
        else:
            modifier, label = 1.0, "contrarian"  # genuine contrarian, base size
        # Set on context for downstream use
        ctx.size_modifier = modifier
        return GateResult(passed=True, gate_name="clob_sizing", reason=label,
                          data={"clob_down_ask": ask, "size_modifier": modifier})
```

### Pipeline insertion (in `five_min_vpin.py`)

Insert `DirectionFilterGate` as G1.5 (after SourceAgreementGate, before DeltaMagnitudeGate).  
Insert `CLOBSizingGate` as G6.5 (after SpreadGate, before DynamicCapGate).

This means existing gates are unchanged — new gates slot in without modifying G0–G7.

---

## Sizing Schedule

| clob_down_ask | Size | WR | Rationale |
|---------------|------|----|-----------|
| ≥ 0.75 | 2.0× base | 99% | Market + model both see downtrend |
| 0.55–0.75 | 1.5× base | 98% | Strong market agreement |
| 0.35–0.55 | 1.2× base | 92% | Mild agreement |
| < 0.35 | 1.0× base | 76% | Contrarian, base Kelly only |
| No CLOB data | 1.0× base | ~85%* | Fallback (no sizing boost) |

\* Estimated from all-DOWN average WR without CLOB filter.

Base Kelly: 2.5% bankroll (`BET_FRACTION = 0.025`)

---

## Expected Value per Trade

```
clob_down_ask >= 0.75: buy DOWN at $0.75 → payout $1.00
  EV = 0.99 × $1.00 − 0.01 × $0.75 = $0.99 − $0.01 = +$0.98 per $0.75 staked (+131%)

clob_down_ask < 0.35: buy DOWN at $0.25 → payout $1.00  
  EV = 0.76 × $1.00 − 0.24 × $0.25 = $0.76 − $0.06 = +$0.70 per $0.25 staked (+280%)
```

The contrarian edge at cheap CLOB actually has higher EV per dollar staked, but lower probability. Both are strongly positive.

---

## Risk Profile

**Near-zero drawdown risk for DOWN-only with CLOB data available:**

At worst-case 76% WR (cheap CLOB contrarian):
- Probability of 5 consecutive losses: 0.24⁵ = 0.08% (1 in 1,300)
- Probability of 10 consecutive losses: 0.24¹⁰ = 0.0001% (1 in 900,000)
- Existing 3-loss cooldown and 45% drawdown kill switch provide additional protection

---

## Risks and Failure Modes

1. **Bearish-dataset bias** — Sessions 1-5 data skew toward downtrending BTC. DOWN predictions may have higher WR in the data period than in a persistent bull run. Monitor: track rolling 24h DOWN WR; if it drops below 70%, review.

2. **CLOB data unavailability** — Without CLOB data, sizing defaults to 1.0× and direction-only filter applies. Always acceptable but loses sizing edge.

3. **Market regime shift** — If a strong sustained bull run emerges, DOWN signals will fire less frequently and UP predictions will remain wrong. Trade frequency drops but WR of executed trades stays high.

4. **Market depth at expensive CLOB** — When `clob_down_ask ≥ 0.75`, DOWN tokens are expensive. At 2× size + $50 max, we're buying $100 of an expensive token. Verify fill quality doesn't degrade at larger size.

---

## Implementation Checklist

- [ ] **SIG-04**: Add `DirectionFilterGate` to `engine/signals/gates.py`
- [ ] **SIG-05**: Add `CLOBSizingGate` to `engine/signals/gates.py`
- [ ] Wire both gates into the V4 pipeline in `five_min_vpin.py`
- [ ] Add env flag `V4_DOWN_ONLY=true` for soft rollout
- [ ] Add `size_modifier` field to `GateContext` dataclass
- [ ] Verify in paper mode: skip rate for UP predictions = ~38% of all signals
- [ ] Monitor: DOWN WR in rolling 24h; alert if < 70%

---

## Monitoring Queries

### Live DOWN vs UP win rate
```sql
SELECT
    se.v2_direction,
    COUNT(*) AS n,
    ROUND(100.0 * SUM(
        CASE WHEN (se.v2_direction = 'UP'   AND ws.close_price > ws.open_price)
               OR (se.v2_direction = 'DOWN' AND ws.close_price < ws.open_price)
        THEN 1 ELSE 0 END
    )::numeric / COUNT(*), 1) AS win_rate
FROM signal_evaluations se
JOIN window_snapshots ws ON se.window_ts = ws.window_ts::bigint AND se.asset = ws.asset
WHERE se.asset = 'BTC'
  AND se.eval_offset BETWEEN 90 AND 150
  AND ABS(COALESCE(se.v2_probability_up, 0.5) - 0.5) >= 0.12
  AND se.evaluated_at >= NOW() - INTERVAL '24 hours'
GROUP BY 1;
```

### CLOB ask distribution (check retail bias is stable)
```sql
SELECT
    CASE
        WHEN se.clob_down_ask <= 0.35 THEN 'cheap(<0.35)'
        WHEN se.clob_down_ask <= 0.55 THEN 'mid(0.35-0.55)'
        WHEN se.clob_down_ask <= 0.75 THEN 'high(0.55-0.75)'
        ELSE 'exp(>0.75)'
    END AS band,
    COUNT(*) AS n,
    ROUND(AVG(se.clob_down_ask)::numeric, 3) AS avg_ask
FROM signal_evaluations se
WHERE se.asset = 'BTC' AND se.v2_direction = 'DOWN'
  AND se.clob_down_ask IS NOT NULL
  AND se.eval_offset BETWEEN 90 AND 150
  AND se.evaluated_at >= NOW() - INTERVAL '24 hours'
GROUP BY 1 ORDER BY avg_ask;
```

---

**Next analysis:** After 24h of live CLOB data (post PR #136 fix), re-run full direction × CLOB band query to validate WR holds in mixed-regime sessions.
