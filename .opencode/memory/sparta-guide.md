# Sparta Agent Guide - Novakash

## Quick Start

**You are a Sparta agent working on the Novakash BTC trading system.**

### Before You Begin

1. **Read this guide first** - It contains critical context about the codebase
2. **Check `tasks/lessons.md`** - Learn from past corrections
3. **Review `tasks/todo.md`** - See current work items
4. **Use plan mode** - For ANY non-trivial task (3+ steps)

### Key Principles

- **Plan First** - Write detailed spec before coding
- **Subagent Strategy** - Offload research/analysis to subagents
- **Verify Before Done** - Never mark complete without proof
- **Self-Improve** - Update lessons after corrections

---

## Critical Domain Knowledge

### TimesFM v5.2 Model (2026-04-13 Fix)

**Model:** LightGBM with 25 features for 5-minute BTC direction prediction  
**Output:** `P(UP)` probability (0-1, calibrated)  
**Serving:** Push-mode POST `/v2/probability` with full 25-feature body  
**Location:** `http://3.98.114.0:8080` (TimesFM service on Montreal)

**Known Issue (FIXED):** Missing `chainlink_price` caused constant 0.606 output
- **Before:** 13,096 evals in 12h, all UP at 10.2% conviction, 0 trades
- **After:** P(UP) varies 0.3-0.9, conviction 15-25%, ~43 trades/day

**Feature Body:** `V5FeatureBody` dataclass in `engine/signals/v2_feature_body.py`
- 25 features total
- `chainlink_price` is required (not optional)
- Missing features → NaN → model returns default leaf 0.606

**Key Files:**
- `engine/signals/timesfm_v2_client.py` - Push-mode client
- `engine/signals/v2_feature_body.py` - 25-feature definition
- `engine/strategies/five_min_vpin.py` - Strategy with v5 calls
- `engine/signals/gates.py` - Gate evaluation with v5 features

### 12 Evaluations Per Window (Critical!)

**Each 5-minute window is evaluated 12 times** (T-140 to T-90, every 5s)

**Filter Cascade:**
```
18,701 total evaluations (all directions)
  ↓
11,621 DOWN evaluations (direction=DOWN, offset 90-140, conv≥12%)
  ↓
36 unique windows with DOWN signal
  ↓
40 actual trades (V4_DOWN_ONLY + CLOB + risk)
```

**Filter rate:** 40/18,701 = 0.2% (only 0.2% of evaluations become trades)

**When to Use Each:**
- **signal_evaluations:** Signal quality analysis (does model predict well?)
- **unique_windows:** Strategy design (how many windows have valid signal?)
- **trade_bible:** Performance tracking (what's my real PnL?)

### Strategy Performance Expectations

| Strategy | Trades/Day | Win Rate | Hourly Rate |
|----------|-----------|----------|-------------|
| DOWN-Only | 40 | 75-80% | ~1.7/hour |
| Asian UP | 3 | 80-90% | ~0.1/hour |
| Combined | 43 | 78-82% | ~1.8/hour |

---

## Data Layer

### Key Tables

**signal_evaluations**
- EVERY model evaluation (every 2s tick)
- NOT just trades
- 12 rows per window (T-140 to T-90)

**strategy_decisions**
- Gate-level decisions (TRADE/SKIP)
- Each evaluation has a decision
- Contains skip_reason

**window_snapshots**
- Per-window outcome (ground truth)
- `close_price > open_price` = UP
- `close_price < open_price` = DOWN

**trade_bible**
- Definitive live trade record
- Actual PnL, win/loss outcomes
- One row per executed trade

**v5_features**
- Push-mode feature body
- All 25 fields must be present
- `chainlink_price` is required

### Standard Query Patterns

**Get signal accuracy by offset:**
```sql
SELECT 
    FLOOR(eval_offset / 15.0) * 15 AS offset_bucket,
    COUNT(*) AS n,
    100.0 * SUM(CASE WHEN correct THEN 1 ELSE 0 END) / COUNT(*) AS accuracy
FROM signal_evaluations
WHERE asset='BTC' AND eval_offset BETWEEN 90 AND 140
GROUP BY 1
ORDER BY 1;
```

**Get unique windows (first eval per window):**
```sql
WITH first_eval AS (
    SELECT window_ts, eval_offset,
           ROW_NUMBER() OVER (PARTITION BY window_ts ORDER BY eval_offset DESC) as rn
    FROM signal_evaluations
    WHERE asset='BTC' AND eval_offset BETWEEN 90 AND 140
)
SELECT * FROM first_eval WHERE rn = 1;
```

**Get actual trades:**
```sql
SELECT strategy, direction, outcome, pnl
FROM trade_bible
WHERE strategy='v4_down_only'
  AND created_at >= NOW() - INTERVAL '24 hours';
```

---

## Sparta Workflow

### Entering Plan Mode

**For any task with 3+ steps or architectural decisions:**

```
1. Understand requirements
2. Research existing code
3. Design solution
4. Write implementation plan
5. Get user approval
6. Implement
7. Test/verify
8. Document results
```

**Example:**
```markdown
## Plan: Add volatility filter for Asian chop

1. Query 03:00-05:00 UTC losses in trade_bible
2. Analyze VPIN/delta patterns during those windows
3. Design volatility gate (threshold based on data)
4. Implement in v4_down_only_strategy.py
5. Add to DuneConfidenceGate
6. Test with historical data
7. Document in docs/analysis/
```

### Subagent Strategy

**Use subagents for:**
- Codebase exploration
- Parallel data analysis
- Research/documentation
- Complex multi-file changes

**Example:**
```
1. Main agent: Design solution architecture
2. Subagent A: Analyze signal_evaluations for pattern X
3. Subagent B: Analyze trade_bible for pattern Y
4. Subagent C: Research similar implementations
5. Main agent: Synthesize results, implement
```

### Verification Checklist

**Before marking task complete:**

- [ ] Code compiles/runs without errors
- [ ] Tests pass (if applicable)
- [ ] Database queries return expected results
- [ ] Logs show correct behavior
- [ ] Documentation updated
- [ ] Lessons learned captured (if applicable)

---

## Codebase Navigation

### Engine Structure

```
engine/
├── main.py                    # Entry point
├── config/
│   ├── constants.py          # All thresholds, limits
│   └── runtime_config.py     # Runtime config via API
├── data/
│   ├── feeds/                # Data feed adapters
│   └── aggregator.py         # Multi-source aggregation
├── signals/
│   ├── v2_feature_body.py    # V5 25-feature definition
│   ├── timesfm_v2_client.py  # Push-mode client
│   └── gates.py              # Gate evaluation
├── strategies/
│   ├── five_min_vpin.py      # DOWN-only + Asian UP
│   └── orchestrator.py       # Strategy registration
├── execution/
│   ├── polymarket_client.py  # CLOB execution
│   └── risk_manager.py       # Kelly sizing, kill switch
└── persistence/
    └── db_client.py          # PostgreSQL writes
```

### Key Constants (engine/config/constants.py)

```python
# VPIN
VPIN_BUCKET_SIZE_USD = 50_000
VPIN_LOOKBACK_BUCKTS = 50
VPIN_INFORMED_THRESHOLD = 0.55
VPIN_CASCADE_THRESHOLD = 0.70

# Risk
MAX_DRAWDOWN_KILL = 0.45  # 45% kill switch
BET_FRACTION = 0.025      # 2.5% Kelly
MIN_BET_USD = 2.0
MAX_OPEN_EXPOSURE_PCT = 0.30

# Arb
ARB_MIN_SPREAD = 0.015
ARB_MAX_POSITION = 50.0

# V5 Model
V5_FEATURE_COUNT = 25
```

---

## Common Tasks

### Querying Model Performance

**Check if model is working:**
```sql
SELECT 
    MIN(v2_probability_up) as min_p,
    MAX(v2_probability_up) as max_p,
    ROUND(AVG(ABS(COALESCE(v2_probability_up, 0.5) - 0.5) * 100), 2) as avg_conv
FROM signal_evaluations
WHERE evaluated_at >= NOW() - INTERVAL '1 hour'
  AND asset = 'BTC';
```

**Expected:**
- `min_p < 0.5` (some DOWN predictions)
- `max_p > 0.7` (some high-conviction predictions)
- `avg_conv > 12%` (above trading threshold)

### Adding New Strategy

1. Create `engine/strategies/vX_new_strategy.py`
2. Register in `engine/strategies/orchestrator.py`
3. Add config in `engine/config/constants.py`
4. Add API endpoints in `hub/api/strategies.py`
5. Add frontend page in `frontend/src/pages/`
6. Document in `docs/analysis/`

### Debugging Model Issues

**Symptom:** Constant 0.606 output
- **Check:** `chainlink_price` in feature body
- **Fix:** Ensure all 25 features populated

**Symptom:** All UP predictions
- **Check:** Model stuck in default leaf
- **Fix:** Verify push-mode active, feature_coverage >= 0.80

**Symptom:** Win rate < 70%
- **Check:** Regime classifier accuracy
- **Fix:** Review gate thresholds

---

## Testing Guidelines

### Unit Tests

**Location:** `engine/tests/`

**Pattern:**
```python
async def test_vpin_bucket_accumulation():
    """Test VPIN correctly accumulates volume buckets."""
    # Arrange
    vpin_calculator = VPINCalculator(bucket_size_usd=50_000, lookback=50)
    
    # Act
    vpin = await vpin_calculator.calculate(ticks=...)
    
    # Assert
    assert 0.0 <= vpin <= 1.0
```

### Integration Tests

**Pattern:**
```python
async def test_down_only_strategy_trade_decision():
    """Test DOWN-only strategy makes correct trade decisions."""
    # Arrange
    strategy = FiveMinVPINStrategy(...)
    
    # Act
    decision = await strategy.evaluate(window=..., state=...)
    
    # Assert
    assert decision.direction == "DOWN"
    assert decision.confidence == "HIGH"
```

---

## Documentation Standards

### New Analysis Document

**Location:** `docs/analysis/2026-MM-DD_topic.md`

**Structure:**
```markdown
# Title

**Date:** 2026-04-13  
**Status:** ✅ Complete / ⚠️ In Progress / 🔴 Blocked

## Executive Summary

## Background

## Analysis

## Findings

## Recommendations

## Related Documents
```

### Code Comments

- Explain **why**, not **what**
- Document assumptions
- Reference related docs
- Include examples when helpful

---

## Lessons Learned

### Common Mistakes

**Mistake:** Treating signal_evaluations as trades
- **Fix:** Use unique_windows or trade_bible for performance tracking
- **Lesson:** 12 evaluations per window, only 1 trade executes

**Mistake:** Missing features in v5 feature body
- **Fix:** Always use `build_v5_feature_body()` helper
- **Lesson:** Missing features → NaN → constant 0.606 output

**Mistake:** Not verifying before marking complete
- **Fix:** Always check logs, queries, tests
- **Lesson:** Assume nothing, verify everything

---

## Quick Reference

### API Endpoints

- `GET /api/dashboard` - Full system snapshot
- `GET /api/trades` - Trade history
- `GET /api/signals/vpin` - VPIN history
- `GET /api/pnl/daily` - Daily PnL
- `POST /api/system/kill` - Kill switch
- `PUT /api/config` - Update config

### WebSocket Events

- `tick` - Price + VPIN
- `trade` - Execution
- `signal` - VPIN/cascade/arb/regime
- `system` - Status updates

### Environment Variables

```bash
TIMESFM_URL=http://3.98.114.0:8080
TIMESFM_V2_URL=http://3.98.114.0:8080
V10_DUNE_MODEL=oak
PAPER_MODE=true
```

---

**Last Updated:** 2026-04-13  
**Version:** 1.0 (Sparta Agent Guide)

</content>
<parameter=filePath>
/Users/billyrichards/Code/novakash/.opencode/memory/sparta-guide.md