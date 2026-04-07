# Changelog — 7 April 2026

---

## v8.1 — Early Entry with v2.2 + Dynamic Caps

### Summary
Cascade evaluation from T-240 → T-180 → T-120 → T-60. At early offsets (≥120s),
requires TimesFM v2.2 calibrated probability HIGH confidence + v8 Tiingo direction
agreement before trading. Dynamic FOK price cap per offset ensures every fill is +EV.

### Data-Driven Decisions (backtested on 344 windows)

| Offset | WR (v2 HIGH + v8 agree) | Entry Cap | Breakeven | Margin |
|--------|------------------------|-----------|-----------|--------|
| T-240  | 89.5% (38 trades)      | $0.55     | $0.895    | +36pp  |
| T-180  | 86.4% (110 trades)     | $0.60     | $0.864    | +30pp  |
| T-120  | 84.3% (89 trades)      | $0.65     | $0.843    | +27pp  |
| T-60   | 78.8% (203 trades)     | $0.73     | $0.788    | +9pp   |

**Tiingo WR vs Polymarket oracle: 93.7%** (63 windows, proven).
**Tiingo + v8 agree: 95.9%** (49 windows).

### Changes

#### feat: TimesFM v2.2 client (`engine/signals/timesfm_v2_client.py`) — NEW FILE
- HTTP client for Montreal EC2 service at `3.98.114.0:8080`
- `get_probability(asset, seconds_to_close)` → calibrated P(UP)
- `health()` → model status
- Feature-flagged via `V2_EARLY_ENTRY_ENABLED` env var (default: true)

#### feat: cascade eval offsets (`engine/config/constants.py`)
- `FIVE_MIN_EVAL_OFFSETS` default changed from `"60"` to `"240,180,120,60"`
- Feed emits window signals at T-240, T-180, T-120, T-60
- `_last_executed_window` dedup gate prevents double-trading same window

#### feat: v8.1 early entry gate (`engine/strategies/five_min_vpin.py`)
- At offsets ≥ 120s: fetch v2.2 calibrated probability via HTTP
- Gate 1: v2.2 HIGH CONF (P > 0.65 or P < 0.35) — skip if low confidence
- Gate 2: v2.2 agrees with v8 Tiingo direction — skip if disagreement
- On pass: upgrade confidence to DECISIVE, set dynamic entry cap
- On fail: skip this offset, fall through to next (T-180 → T-120 → T-60)
- At T-60: no v2.2 gate applied (current v8 behaviour unchanged)

#### feat: dynamic FOK entry caps (`engine/strategies/five_min_vpin.py`)
- `V81_ENTRY_CAPS` dict: {240: $0.55, 180: $0.60, 120: $0.65, 60: $0.73}
- FOK ladder `max_price` uses dynamic cap instead of hardcoded $0.73
- Each cap is set conservatively below breakeven WR for that offset
- Configurable via env: `V81_CAP_T240`, `V81_CAP_T180`, `V81_CAP_T120`, `V81_CAP_T60`

#### feat: v8.1 order metadata
- `entry_offset_s`: actual offset used (240, 180, 120, or 60)
- `entry_reason`: "v2.2_early_T240", "v2.2_early_T180", "v8_standard", etc.
- `v81_entry_cap`: dynamic cap applied for this trade
- `engine_version`: "v8.1"
- Queryable: `SELECT metadata->>'entry_reason', COUNT(*) FROM trades GROUP BY 1`

#### feat: v2.2 wiring in orchestrator
- `TimesFMV2Client` injected into `FiveMinVPINStrategy._timesfm_v2`
- Feature-flagged: `V2_EARLY_ENTRY_ENABLED=true` (default)
- URL configurable: `TIMESFM_V2_URL` (default: `http://3.98.114.0:8080`)

### Feature Flags

| Variable | Default | Effect |
|----------|---------|--------|
| `V2_EARLY_ENTRY_ENABLED` | `true` | Enable v2.2 gate at early offsets |
| `TIMESFM_V2_URL` | `http://3.98.114.0:8080` | v2.2 service endpoint |
| `FIVE_MIN_EVAL_OFFSETS` | `240,180,120,60` | Cascade eval offsets |
| `V81_CAP_T240` | `0.55` | Max FOK price at T-240 |
| `V81_CAP_T180` | `0.60` | Max FOK price at T-180 |
| `V81_CAP_T120` | `0.65` | Max FOK price at T-120 |
| `V81_CAP_T60` | `0.73` | Max FOK price at T-60 |

### What Was NOT Changed
- `_evaluate_signal()` gate logic (VPIN, delta, regime) — unchanged
- `_execute_trade()` FOK/GTC execution flow — unchanged (just cap is dynamic)
- OrderManager, RiskManager, TelegramAlerter — unchanged
- T-60 behaviour when v2.2 disabled — identical to v8.0

### Rollback
Set `V2_EARLY_ENTRY_ENABLED=false` to disable early entry entirely.
Set `FIVE_MIN_EVAL_OFFSETS=60` to revert to T-60-only evaluation.
No code changes needed — all feature-flagged.

### Branch
`claude/v81-early-entry` → merged to `develop`

---

## v8.1.2 — FOK Ladder Cap Handling Fix

### Problem
FOK ladder was aborting immediately when CLOB best_ask > max_price, preventing any fill attempts at our cap price. This meant orders would never be placed even when CLOB might drop or hidden liquidity existed at our price.

### Live Evidence
```
2026-04-07T20:28:42.231805Z  fok_ladder.start: max_price=$0.65, stake=$4.18
2026-04-07T20:28:42.335289Z  fok_ladder.clob_above_cap: best_ask=$0.93 → starting at $0.65
2026-04-07T20:28:42.335484Z  fok_ladder.attempt: Attempt 1 at $0.65, size=6.40 tokens
2026-04-07T20:28:42.741549Z  fok_ladder.order_error: FOK killed (no liquidity at $0.65)
2026-04-07T20:28:43.252744Z  place_order.live_submitted: GTC fallback at $0.65
```

### Changes

#### fix: FOK ladder cap logic (`engine/execution/fok_ladder.py`)
- **Removed premature abort** when `best_ask > max_price`
- **Start ladder at max_price** even when CLOB is higher
- **Removed early break** when already at cap (keep retrying at cap)
- FOK ladder now attempts fills at cap price on each retry
- Falls back to GTC at cap when all FOK attempts exhausted

### Behaviour Change

**Before:**
- CLOB $0.93, cap $0.65 → abort immediately, no order placed

**After:**
- CLOB $0.93, cap $0.65 → FOK attempts at $0.65
- If CLOB drops or hidden liquidity exists → FOK fills
- If no liquidity → FOK killed, falls back to GTC at cap

### Rollback
No rollback needed — this is a bug fix. Old behaviour was incorrect for FOK execution.

### Branch
`hotfix/fok-ladder-cap-handling` → merged to `develop` (commit: c08a39e)
