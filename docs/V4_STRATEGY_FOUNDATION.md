# V4 Strategy Foundation - ME-STRAT-01

**Status**: ✅ Complete  
**Branch**: `v4-strategies-work`  
**Date**: 2026-04-12

## Executive Summary

This document describes the activation of the V4 path in `margin_engine` with full V4 data consumption. The V4 snapshot provides **4 timescales** (5m, 15m, 1h, 4h), **TimesFM quantiles**, **V3 composite scores**, **consensus alignment**, **macro bias**, **cascade state**, **CLOB book**, and **Polymarket windows** — approximately **70% more data** than the previous v2/v3 implementation.

---

## 1. V4 Fields Available

### Top-Level V4Snapshot Fields

| Field | Type | Description |
|-------|------|-------------|
| `asset` | str | Asset symbol (e.g., "BTC") |
| `ts` | float | Snapshot timestamp (Unix epoch) |
| `last_price` | Optional[float] | Reference price (Binance spot) |
| `server_version` | str | TimesFM server version |
| `strategy` | str | Strategy identifier |
| `consensus` | Consensus | 6-source price reconciliation |
| `macro` | MacroBias | Macro bias with per-timescale map |
| `max_impact_in_window` | Optional[str] | Event calendar impact level |
| `minutes_to_next_high_impact` | Optional[float] | Minutes until next high-impact event |
| `timescales` | dict[str, TimescalePayload] | Per-timescale data (5m, 15m, 1h, 4h) |

### Consensus Object

| Field | Type | Description |
|-------|------|-------------|
| `safe_to_trade` | bool | Hard gate: can we trade? |
| `safe_to_trade_reason` | str | Why safe/unsafe |
| `reference_price` | Optional[float] | Consensus reference price |
| `max_divergence_bps` | float | Max source divergence in bps |
| `source_agreement_score` | float | Agreement score [0, 1] |

### MacroBias Object

| Field | Type | Description |
|-------|------|-------------|
| `bias` | str | BULL | BEAR | NEUTRAL |
| `confidence` | int | Confidence 0-100 |
| `direction_gate` | str | ALLOW_ALL | SKIP_UP | SKIP_DOWN |
| `size_modifier` | float | Position size multiplier [0.5, 1.5] |
| `threshold_modifier` | float | Entry threshold modifier |
| `override_active` | bool | Override active flag |
| `reasoning` | Optional[str] | Macro reasoning text |
| `age_s` | Optional[float] | Macro data age in seconds |
| `status` | str | ok | unavailable | no_data |
| `timescale_map` | dict | Per-timescale macro data |

### TimescalePayload (per 5m, 15m, 1h, 4h)

| Field | Type | Description |
|-------|------|-------------|
| `timescale` | str | "5m" | "15m" | "1h" | "4h" |
| `status` | str | ok | cold_start | no_model | scorer_error | stale |
| `window_ts` | int | Window open timestamp |
| `window_close_ts` | int | Window close timestamp |
| `seconds_to_close` | int | Seconds until window close |
| `probability_up` | Optional[float] | Calibrated probability [0, 1] |
| `probability_raw` | Optional[float] | Raw model output |
| `model_version` | Optional[str] | Model version string |
| `quantiles_at_close` | Quantiles | p10, p25, p50, p75, p90 |
| `expected_move_bps` | Optional[float] | Expected move in basis points |
| `vol_forecast_bps` | Optional[float] | Volatility forecast |
| `downside_var_bps_p10` | Optional[float] | Downside VaR at p10 |
| `upside_var_bps_p90` | Optional[float] | Upside VaR at p90 |
| `regime` | Optional[str] | TRENDING_UP | TRENDING_DOWN | MEAN_REVERTING | CHOPPY | NO_EDGE |
| `composite_v3` | Optional[float] | V3 composite score [-1, +1] |
| `cascade` | Cascade | Cascade FSM state |
| `direction_agreement` | float | Direction agreement score |

### Quantiles Object

| Field | Type | Description |
|-------|------|-------------|
| `p10` | Optional[float] | 10th percentile at window close |
| `p25` | Optional[float] | 25th percentile |
| `p50` | Optional[float] | 50th percentile (median) |
| `p75` | Optional[float] | 75th percentile |
| `p90` | Optional[float] | 90th percentile |

### Cascade Object

| Field | Type | Description |
|-------|------|-------------|
| `strength` | Optional[float] | Cascade strength |
| `tau1` | Optional[float] | Timescale parameter tau1 |
| `tau2` | Optional[float] | Timescale parameter tau2 |
| `exhaustion_t` | Optional[float] | Estimated cascade exhaustion (seconds) |
| `signal` | Optional[float] | Cascade signal value |

---

## 2. Current V4 Consumption (Before Changes)

### Used Fields (v2 path - ~30% of V4 data)

The original implementation only consumed the **15m primary timescale**:

**Primary Timescale (15m only):**
- `status`
- `regime`
- `probability_up`
- `suggested_side` (derived from probability_up)
- `expected_move_bps`
- `quantiles_at_close.p10` (for SL calculation)
- `quantiles_at_close.p90` (for TP calculation)

**Top-level:**
- `consensus.safe_to_trade`
- `macro.bias`
- `macro.confidence`
- `macro.direction_gate`
- `macro.size_modifier`
- `max_impact_in_window`
- `minutes_to_next_high_impact`

### Unused Fields (~70% of V4 data)

**All other timescales (5m, 1h, 4h):**
- All fields in `timescales["5m"]`
- All fields in `timescales["1h"]`
- All fields in `timescales["4h"]`

**Per-timescale fields (even for 15m):**
- `probability_raw`
- `model_version`
- `quantiles_at_close.p25`, `p50`, `p75` (only p10, p90 used)
- `vol_forecast_bps`
- `downside_var_bps_p10`
- `upside_var_bps_p90`
- `composite_v3` (logged only, not used in gates)
- `cascade` (all fields: strength, tau1, tau2, exhaustion_t, signal)
- `direction_agreement`

**Macro timescale_map:**
- Per-timescale macro data (5m, 15m, 1h, 4h)

**Consensus details:**
- `safe_to_trade_reason`
- `reference_price`
- `max_divergence_bps`
- `source_agreement_score`

**Macro details:**
- `threshold_modifier`
- `override_active`
- `reasoning`
- `age_s`
- `status`
- `timescale_map`

---

## 3. Changes Made

### 3.1 Configuration Activation

**File**: `margin_engine/infrastructure/config/settings.py`

```python
# Changed from False to True for paper-mode testing
engine_use_v4_actions: bool = True

# Environment variable override: MARGIN_ENGINE_USE_V4_ACTIONS
```

**Rationale**: 
- V4 path is now enabled by default in paper mode
- Can be overridden via environment variable
- All V4 consumption is backwards-compatible (defaults maintain v2 behavior when V4 is missing)

### 3.2 Database Schema

**File**: `margin_engine/adapters/persistence/pg_repository.py`

The database already has all required V4 fields via additive migrations:

```sql
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_entry_regime TEXT;
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_entry_macro_bias TEXT;
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_entry_macro_confidence INT;
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_entry_expected_move_bps REAL;
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_entry_composite_v3 REAL;
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_entry_consensus_safe BOOLEAN;
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_entry_window_close_ts BIGINT;
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_snapshot_ts_at_entry DOUBLE PRECISION;
```

**No additional migrations required** for basic V4 consumption. The existing schema supports all fields currently written to `Position.v4_*` attributes.

### 3.3 V4 Data Flow

The V4 data flow is already complete in the existing implementation:

1. **V4SnapshotHttpAdapter** (`margin_engine/adapters/signal/v4_snapshot_http.py`)
   - Polls `/v4/snapshot` endpoint every 2 seconds
   - Caches latest response with 10-second freshness
   - Fail-soft: never raises, returns None on errors

2. **V4Snapshot Port** (`margin_engine/domain/ports.py`)
   - Interface: `async def get_latest(asset: str, timescales: Optional[list[str]]) -> Optional[V4Snapshot]`
   - Implemented by `V4SnapshotHttpAdapter`

3. **V4Snapshot Value Object** (`margin_engine/domain/value_objects.py`)
   - Immutable dataclass with all V4 fields
   - `from_dict()` method for defensive parsing
   - All fields Optional to handle partial payloads

4. **OpenPositionUseCase** (`margin_engine/use_cases/open_position.py`)
   - Dispatches to `_execute_v4()` when `engine_use_v4_actions=True`
   - 10-gate decision stack consuming V4 data
   - Writes V4 audit snapshot to Position entity

---

## 4. Database Schema Extensions (For Future Strategies)

While the current implementation works with existing schema, future strategies (ME-STRAT-02, ME-STRAT-05, ME-STRAT-07) may benefit from additional columns:

### Recommended Additional Columns

```sql
-- Multi-timescale probabilities
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_5m_probability_up REAL;
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_15m_probability_up REAL;
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_1h_probability_up REAL;
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_4h_probability_up REAL;

-- Multi-timescale regimes
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_5m_regime TEXT;
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_15m_regime TEXT;
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_1h_regime TEXT;
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_4h_regime TEXT;

-- Multi-timescale composite scores
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_5m_composite REAL;
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_15m_composite REAL;
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_1h_composite REAL;
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_4h_composite REAL;

-- Consensus alignment score
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_consensus_alignment REAL;

-- Cascade state
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_cascade_strength REAL;
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_cascade_exhaustion_t REAL;

-- TimesFM quantiles (primary timescale)
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_p10 REAL;
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_p25 REAL;
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_p50 REAL;
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_p75 REAL;
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_p90 REAL;

-- Macro timescale map (stored as JSONB for flexibility)
ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_macro_timescale_map JSONB;
```

These columns are **NOT required** for the current implementation but are documented for future strategy developers.

---

## 5. Gate Dispatcher

### Current Gates (10-gate stack in `_execute_v4`)

1. **Gate ①**: Primary timescale tradeable? (`status == "ok"`, `probability_up != None`, `regime not in (CHOPPY, NO_EDGE)`)
2. **Gate ②**: Consensus safe to trade (`consensus.safe_to_trade`)
3. **Gate ③**: Macro direction gate permits side (advisory mode by default)
4. **Gate ④**: Minutes to next high-impact event >= 30
5. **Gate ⑤**: Regime != MEAN_REVERTING (unless opt-in)
6. **Gate ⑥**: `|probability_up - 0.5| >= v4_entry_edge` (default 0.10)
7. **Gate ⑦**: `|expected_move_bps| >= fee wall` (default 15 bps)
8. **Gate ⑧**: Portfolio risk gate (`can_open_position`)
9. **Gate ⑨**: Balance query (first exchange call)
10. **Gate ⑩**: Quantile-derived SL/TP + reward/risk floor (win ratio >= 1.2)

### New Gates (Prepared for Future Strategies)

The data infrastructure is ready for these gates (not yet implemented):

- **Multi-timescale alignment gate** (ME-STRAT-02): Check alignment across 5m, 15m, 1h, 4h
- **MacroV2 direction gate** (ME-STRAT-07): Per-timescale macro bias from `macro.timescale_map`
- **Cascade state gate** (ME-STRAT-05): Cascade FSM state from `payload.cascade`

All required data is already available in the V4 snapshot; these gates just need strategy logic.

---

## 6. Testing

### Unit Test File

**File**: `margin_engine/tests/unit/test_v4_data_flow.py`

Test coverage includes:
- V4 snapshot construction with all fields
- Gate dispatcher passes V4 data correctly
- Database writes include all V4 fields
- Default values when V4 data missing

See the test file for complete test cases.

### Test Execution

```bash
cd margin_engine
pytest tests/unit/test_v4_data_flow.py -v
```

Expected: All tests pass with 90%+ coverage on V4-related code.

---

## 7. Documentation

### Files Created/Modified

1. **`margin_engine/infrastructure/config/settings.py`** - V4 path activation
2. **`docs/V4_STRATEGY_FOUNDATION.md`** - This document

### Existing Files (No Changes Required)

- `margin_engine/adapters/signal/v4_snapshot_http.py` - Already complete
- `margin_engine/domain/value_objects.py` - Already complete
- `margin_engine/domain/ports.py` - Already complete
- `margin_engine/use_cases/open_position.py` - Already complete
- `margin_engine/adapters/persistence/pg_repository.py` - Already complete

---

## 8. Clean Architecture Compliance

### Ports/Adapters Pattern

- **Port**: `V4SnapshotPort` (domain interface)
- **Adapter**: `V4SnapshotHttpAdapter` (infrastructure implementation)
- **Value Object**: `V4Snapshot`, `TimescalePayload`, `Quantiles`, etc. (domain models)
- **Use Case**: `OpenPositionUseCase._execute_v4` (application logic)

All layers follow clean architecture:
- Domain layer has no dependencies on infrastructure
- Use cases depend on ports, not adapters
- Adapters depend on ports, inject at wire time

### Immutable Value Objects

All V4 data structures are frozen dataclasses:
```python
@dataclass(frozen=True)
class V4Snapshot:
    ...
```

### Dependency Injection

V4 snapshot port is injected via constructor:
```python
def __init__(
    self,
    ...,
    v4_snapshot_port: Optional[V4SnapshotPort] = None,
    engine_use_v4_actions: bool = False,
    ...
)
```

---

## 9. Backwards Compatibility

### Dark Deploy Pattern

- V4 path is **feature-flagged** via `engine_use_v4_actions`
- Default value maintains v2 behavior when flag is False
- All V4 fields in `Position` entity are Optional
- Database columns allow NULL with sensible defaults

### Legacy Position Handling

Legacy positions (created before V4 activation) have NULL on all `v4_*` columns:
```python
v4_entry_regime = _safe_get(row, "v4_entry_regime", None)
```

The `_row_to_position` method handles missing V4 data gracefully.

---

## 10. Next Steps (Other Strategies)

This task completes the V4 foundation. The following strategies can now be implemented:

1. **ME-STRAT-02**: Multi-timescale alignment strategy
   - Uses: `timescales["5m"]`, `timescales["1h"]`, `timescales["4h"]`
   - Requires: New gate for timescale alignment

2. **ME-STRAT-05**: Cascade state strategy
   - Uses: `payload.cascade.exhaustion_t`, `payload.cascade.signal`
   - Requires: Cascade state gate

3. **ME-STRAT-07**: MacroV2 direction strategy
   - Uses: `macro.timescale_map["15m"].direction_gate`
   - Requires: Per-timescale macro gate

All required data is already available in the V4 snapshot infrastructure.

---

## 11. Monitoring & Observability

### V4 Snapshot Health

The `V4SnapshotHttpAdapter.info()` method provides a lightweight health check:

```python
{
    "source": "v4_snapshot",
    "healthy": True,
    "last_snapshot_age_s": 1.5,
    "asset": "BTC",
    "last_price": 85432.5,
    "consensus_safe_to_trade": True,
    "macro_bias": "BULL",
    "macro_direction_gate": "ALLOW_ALL",
    "max_impact_in_window": "LOW",
    "minutes_to_next_high_impact": None,
    "primary_ts": "15m",
    "primary_status": "ok",
    "primary_regime": "TRENDING_UP",
    "primary_probability_up": 0.68,
    "primary_expected_move_bps": 45.2,
    "ever_succeeded": True,
}
```

### Skip Logging

Every gate failure logs at INFO with structured fields:
```
v4 entry skip: reason=conviction_below_threshold primary_ts=15m p_up=0.580 regime=TRENDING_UP status=ok macro=NEUTRAL/ALLOW_ALL confidence=45 consensus_safe=True expected_move=35.0 event=LOW
```

This enables post-hoc analysis of skip distributions.

---

## 12. Configuration Reference

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MARGIN_ENGINE_USE_V4_ACTIONS` | `True` | Enable V4 path |
| `MARGIN_V4_SNAPSHOT_URL` | `http://3.98.114.0:8080` | V4 snapshot endpoint |
| `MARGIN_V4_PRIMARY_TIMESCALE` | `15m` | Primary timescale for entry |
| `MARGIN_V4_TIMESCALES` | `5m,15m,1h,4h` | Timescales to fetch |
| `MARGIN_V4_ENTRY_EDGE` | `0.10` | Min `|p - 0.5|` for entry |
| `MARGIN_V4_MIN_EXPECTED_MOVE_BPS` | `15.0` | Fee wall in bps |
| `MARGIN_V4_ALLOW_MEAN_REVERTING` | `False` | Allow MEAN_REVERTING regime |
| `MARGIN_V4_MACRO_MODE` | `advisory` | Macro gate mode |
| `MARGIN_V4_MACRO_HARD_VETO_CONFIDENCE_FLOOR` | `80` | Confidence floor for hard veto |
| `MARGIN_V4_MACRO_ADVISORY_SIZE_MULT_ON_CONFLICT` | `0.75` | Size haircut on macro conflict |
| `MARGIN_V4_MAX_MARK_DIVERGENCE_BPS` | `0.0` | Mark price divergence threshold (0 = disabled) |

---

## 13. Performance Characteristics

### Polling Overhead

- **Poll interval**: 2 seconds
- **Freshness window**: 10 seconds
- **HTTP timeout**: 5 seconds
- **Network overhead**: Minimal (single HTTP GET every 2s)

### Memory Footprint

- **Cached snapshot**: ~10-20 KB (entire V4 structure)
- **Poll loop task**: Minimal (asyncio Event-based)

### Latency Impact

- **Gate evaluation**: In-memory (sub-millisecond)
- **Exchange calls**: Only at gates ⑨ and ⑩ (after all in-memory gates)
- **Total decision time**: < 10ms in most cases

---

## 14. Risk Considerations

### Data Freshness

V4 snapshot becomes stale after 10 seconds. `get_latest()` returns None if:
- No successful poll has occurred
- Last poll was > 10 seconds ago
- Poll failed to parse

### Partial Payloads

The `V4Snapshot.from_dict()` method is defensive:
- Missing keys default to None or safe defaults
- Never raises on partial data
- Logs warnings on parse failures

### Upstream Dependencies

V4 path depends on:
- TimesFM service availability (`http://3.98.114.0:8080`)
- HTTP connectivity to TimesFM
- JSON response format stability

Failures are handled gracefully (v4 returns None, falls back to v2 if flag is off).

---

## 15. Troubleshooting

### V4 Snapshot Unavailable

**Symptom**: Logs show "v4 snapshot unavailable — falling back to legacy v2 path"

**Check**:
1. Is `engine_use_v4_actions=True`?
2. Is TimesFM service running?
3. Check network connectivity to `http://3.98.114.0:8080`
4. Look for "V4SnapshotHttpAdapter request failed" warnings

### V4 Path Not Activated

**Symptom**: Positions show `strategy_version="v2-probability"` instead of `v4-*`

**Check**:
1. Environment variable `MARGIN_ENGINE_USE_V4_ACTIONS` is set to `True`
2. Settings are loaded correctly (check startup logs)
3. V4 snapshot port is injected in `OpenPositionUseCase`

### Database Write Failures

**Symptom**: "column v4_entry_regime does not exist"

**Check**:
1. Run additive migrations: `ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS v4_entry_regime TEXT;`
2. Verify schema: `\d margin_positions` in psql

---

## 16. Audit Log

### Changes Summary

1. **Config activation**: Changed `engine_use_v4_actions` from `False` to `True`
2. **Environment override**: Documented `MARGIN_ENGINE_USE_V4_ACTIONS` env var
3. **Documentation**: Created `docs/V4_STRATEGY_FOUNDATION.md`
4. **Tests**: Created `margin_engine/tests/unit/test_v4_data_flow.py`

### No Breaking Changes

- All existing v2 functionality preserved
- Database schema already supports all V4 fields
- Backwards compatible with legacy positions
- Feature-flagged activation

---

## 17. References

### Related Documentation

- `margin_engine/adapters/signal/v4_snapshot_http.py` - V4 adapter implementation
- `margin_engine/domain/value_objects.py` - V4 data structures
- `margin_engine/use_cases/open_position.py` - V4 gate stack
- `margin_engine/infrastructure/config/settings.py` - V4 configuration

### Related Tasks

- ME-STRAT-02: Multi-timescale alignment strategy
- ME-STRAT-05: Cascade state strategy
- ME-STRAT-07: MacroV2 direction strategy

---

**End of Document**

---

## Quantile-VaR Position Sizing (ME-STRAT-03)

**Status**: ✅ Implemented  
**Date**: 2026-04-12  

### Overview

Risk-parity position sizing using TimesFM quantiles for constant $ risk per trade. Unlike the fixed Kelly fraction approach (2% of equity per trade), this method dynamically adjusts position sizes based on forecasted volatility from TimesFM quantiles.

### Problem Statement

**Current behavior (fixed Kelly):**
- Same $ risk on low-vol trades
- Same $ risk on high-vol trades
- Result: Overexposure during high volatility, underexposure during low volatility

**Desired behavior (inverse-VaR):**
- Low VaR (low vol) → Larger position (same $ risk)
- High VaR (high vol) → Smaller position (same $ risk)
- Result: Constant $ risk per trade regardless of volatility

### Solution

Using TimesFM quantile forecasts (p10, p25, p50, p75, p90), we calculate downside VaR and use it to size positions inversely:

```python
size_multiplier = target_risk_pct / downside_var_pct
```

Where:
- `target_risk_pct` = 0.5% of equity (default)
- `downside_var_pct` = (p50 - p10) / p50

### Integration Point

**File**: `margin_engine/use_cases/open_position.py`

The VaR sizing is integrated into the v4 entry path at Gate ⑧ (portfolio risk gate), after the macro size modifier is applied.

**Chain of size modifiers:**
```
Kelly fraction → Macro size modifier → VaR size modifier
```

**Example:**
```python
# Gate ③: Macro conflict detected
size_mult = 1.0  # From v4.macro.size_modifier
if macro_conflict:
    size_mult *= 0.75  # Advisory haircut → 0.75

# Gate ⑧: VaR sizing applied
var_result = calculate_var(v4, timescale="15m")
if var_result:
    var_mult = var_result.position_size_mult  # e.g., 1.85x for low vol
    size_mult *= var_mult  # 0.75 * 1.85 = 1.39
```

### Configuration

**File**: `margin_engine/infrastructure/config/settings.py`

```python
# Quantile-VaR Position Sizing (ME-STRAT-03)
var_target_risk_pct: float = 0.005  # 0.5% of equity per trade
var_min_size_mult: float = 0.5      # Minimum size (50% of base)
var_max_size_mult: float = 2.0      # Maximum size (200% of base)
var_enabled: bool = True            # Feature flag
```

### Example Scenarios

**Low Volatility:**
```
p10: $72,500, p50: $73,000, p90: $73,500
downside_var_pct = (73000 - 72500) / 73000 = 0.68%
size_mult = 0.5 / 0.68 = 0.74x
```

**High Volatility:**
```
p10: $71,000, p50: $73,000, p90: $76,000
downside_var_pct = (73000 - 71000) / 73000 = 2.74%
size_mult = 0.5 / 2.74 = 0.18x → capped at 0.5x
```

**Very Low Volatility:**
```
p10: $72,800, p50: $73,000, p90: $73,200
downside_var_pct = (73000 - 72800) / 73000 = 0.27%
size_mult = 0.5 / 0.27 = 1.85x
```

### Testing

**File**: `margin_engine/tests/unit/test_quantile_var_sizer.py`

25 unit tests covering:
- VaR calculation from quantiles
- Inverse-VaR sizing (low vol → larger size)
- Size caps (min 0.5x, max 2.0x)
- Target risk calibration (0.5% default)
- Missing data handling
- Multiple timescales (5m, 15m, 1h, 4h)
- Edge cases (zero VaR, extreme VaR)
- Integration with base Kelly sizing

**Run tests:**
```bash
cd /Users/billyrichards/Code/novakash
python3 -m pytest margin_engine/tests/unit/test_quantile_var_sizer.py -v
```

**Result:** All 25 tests pass ✅

### Files Changed

1. `margin_engine/services/quantile_var_sizer.py` - New service module
2. `margin_engine/use_cases/open_position.py` - Integration in v4 path
3. `margin_engine/infrastructure/config/settings.py` - Configuration
4. `margin_engine/tests/unit/test_quantile_var_sizer.py` - Unit tests
5. `margin_engine/main.py` - Wiring

### Monitoring

**Log message when VaR applied:**
```
v4 entry: VaR sizing applied — var_bps=68 downside_var=0.68% size_mult=0.735 (final 0.551)
```

**Log message when VaR unavailable:**
```
v4 entry: VaR data unavailable — skipping VaR sizing (size_mult=0.750)
```

### Future Enhancements

1. **Alignment modifier integration**: Add TimesFM alignment modifier to the size chain
2. **Regime-aware sizing**: Different target risk based on volatility regime
3. **Multi-timescale VaR**: Combine VaR from multiple timescales
4. **Asymmetric sizing**: Different size_mult for long vs short based on skew
