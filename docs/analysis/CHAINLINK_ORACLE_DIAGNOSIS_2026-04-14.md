# Chainlink Oracle Alignment Diagnosis

**Date:** 2026-04-14
**Severity:** HIGH -- root cause of direction disagreement on small-delta windows

---

## Executive Summary

Polymarket 5-minute BTC UP/DOWN markets resolve using the **Chainlink oracle on Polygon**, not Binance spot. Our engine uses **Tiingo (primary) / Binance (fallback)** for direction decisions. On small delta windows (<0.05%), Chainlink's discrete update mechanism (only on >0.1% deviation or heartbeat) causes it to disagree with continuous feeds. This misalignment is the primary source of losses on low-confidence windows.

---

## 1. Root Cause Analysis

### 1.1 The Decision Chain (where direction is determined)

There are **two independent decision paths**, both affected:

#### Path A: v4_fusion (LIVE strategy) -- TimesFM probability_up

1. **TimesFM `/v4/snapshot`** endpoint calculates `probability_up` via LightGBM scorer
2. The scorer's features include `chainlink_price` and `chainlink_vs_binance`, but both are **hardcoded to `NaN`** at serve time (`v2_scorer.py` lines 510-511: `"chainlink_price": float("nan")`)
3. The Chainlink price IS polled via `V2ChainlinkPoller` into the `V2FeatureCache`, but the `_assemble_features()` method **never reads it** -- the feature dict just emits NaN
4. `_build_polymarket_outcome()` derives direction purely from `probability_up`: `direction = "UP" if p_up > 0.5 else "DOWN"` (line 2162)
5. This `poly_direction` flows to the engine's `FullDataSurface.poly_direction` and drives all strategy decisions

**Impact:** The ML model's probability_up is trained on historical data that included Chainlink features, but at inference time those features are NaN. The model's direction call is therefore driven by Binance/CoinGlass/Gamma features, not Chainlink.

#### Path B: five_min_vpin (legacy, used by v10_gate) -- delta_pct

1. `five_min_vpin.py` line 517: `_delta_source = _rt_cfg.delta_price_source` defaults to `"tiingo"`
2. Delta priority chain (lines 673-723): **tiingo -> chainlink fallback -> binance fallback**
3. Chainlink delta is fetched from DB (`get_latest_chainlink_price`), not from the in-memory feed cache
4. When Tiingo is available (most of the time), Chainlink is never the primary delta source

**Impact:** Direction decisions use Tiingo/Binance delta, not the Chainlink price that Polymarket actually resolves on.

### 1.2 The open_price Problem

Both decision paths calculate delta as `(current_price - open_price) / open_price`.

The `open_price` for each window is fetched from **Binance spot REST API** (`polymarket_5min.py` lines 500-534):
- Live: `https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT`
- Paper: `https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT`

Despite the comment on line 50 saying `"Opening price (Chainlink oracle)"`, the actual code fetches from **Binance**, not Chainlink.

Polymarket's resolution compares the Chainlink oracle price at window OPEN vs window CLOSE. Our engine compares a **Binance open** against a **Tiingo/Binance close** -- double mismatch.

### 1.3 The DataSurface Path (Strategy Engine v2)

The `DataSurfaceManager.get_surface()` (line 314-319) selects primary delta with priority: **tiingo > chainlink > binance**:

```python
for src, val in [
    ("tiingo_rest_candle", delta_tiingo),
    ("chainlink", delta_chainlink),
    ("binance", delta_binance),
]:
```

Chainlink IS read from the in-memory `latest_prices` cache here (lines 305-309), but only as a fallback when Tiingo is unavailable. It never drives the primary decision.

### 1.4 The SourceAgreementGate

The `SourceAgreementGate` (v10_gate config) checks if 2+ sources agree on direction. It uses all three deltas (tiingo, chainlink, binance), but the agreement vote is majority-rules -- Chainlink gets equal weight to the other two, not priority weight as the resolution oracle.

### 1.5 Resolution Path (correctly uses Polymarket)

The `OrderManager._resolve_from_polymarket()` (line 594-671) correctly queries the Gamma API for actual market resolution. So the engine **knows** the right answer after the fact -- the mismatch is purely in the **direction prediction** that drives trade entry.

---

## 2. Impact Quantification

### 2.1 Known Accuracy Numbers

From the codebase comments:
- **Tiingo delta accuracy:** 96.9% direction agreement with oracle
- **Binance delta accuracy:** 71.6% direction agreement with oracle

The ~3% Tiingo disagreement rate and ~28% Binance disagreement rate concentrate in **small-delta windows** where Chainlink hasn't updated (its heartbeat is 1 hour on Polygon, deviation threshold ~0.1%).

### 2.2 Where Disagreement Occurs

On windows where `|delta| < 0.05%`:
- Chainlink may show 0% delta (no update since window open) while Tiingo/Binance show a small positive/negative move
- The engine predicts UP/DOWN based on Tiingo/Binance's tiny move
- Chainlink resolves FLAT or in the opposite direction (based on its last update timestamp vs window boundary)

### 2.3 TimesFM Model Feature Gap

The LightGBM model was trained with `chainlink_price` and `chainlink_vs_binance` as features, but at inference these are NaN. LightGBM handles NaN natively (splits send NaN to the learned-optimal child), so the model works but with **degraded accuracy** on exactly the windows where Chainlink would matter most -- small moves where Chainlink disagrees with spot.

---

## 3. Proposed Fixes

### Fix 1: Wire Chainlink into TimesFM scorer features (HIGH PRIORITY)

**File:** `novakash-timesfm/app/v2_scorer.py` lines 510-511

Replace NaN hardcodes with actual V2FeatureCache chainlink data:
```python
# Before:
"chainlink_price": float("nan"),
"chainlink_vs_binance": float("nan"),

# After: read from feature cache snapshot
"chainlink_price": _f(snap.chainlink.get("price")) if snap.chainlink else float("nan"),
"chainlink_vs_binance": (
    (_f(snap.chainlink.get("price")) - last_price) / last_price
    if snap.chainlink and snap.chainlink.get("price") and last_price > 0
    else float("nan")
),
```

The `V2ChainlinkPoller` already writes into the `FeatureSnapshot.chainlink` dict. The scorer just never reads it.

### Fix 2: Use Chainlink for open_price (HIGH PRIORITY)

**File:** `engine/data/feeds/polymarket_5min.py` lines 500-534

At window start, fetch the open price from Chainlink (same oracle Polymarket uses) instead of Binance:
```python
# Add Chainlink as PRIMARY open price source
if self._chainlink_feed:
    cl_price = self._chainlink_feed.latest_prices.get(window.asset)
    if cl_price:
        window.open_price = cl_price
        return
# Fall back to Binance if Chainlink unavailable
```

### Fix 3: Make Chainlink the PRIMARY delta source for 5m markets (MEDIUM PRIORITY)

**File:** `engine/strategies/data_surface.py` lines 314-319

For 5m timescale, flip priority to chainlink > tiingo > binance:
```python
if self._timescale == "5m":
    priority = [
        ("chainlink", delta_chainlink),
        ("tiingo_rest_candle", delta_tiingo),
        ("binance", delta_binance),
    ]
else:
    priority = [
        ("tiingo_rest_candle", delta_tiingo),
        ("chainlink", delta_chainlink),
        ("binance", delta_binance),
    ]
```

### Fix 4: five_min_vpin delta source override (MEDIUM PRIORITY)

**File:** `engine/config/runtime_config.py` line 214

Change default `DELTA_PRICE_SOURCE` from `"tiingo"` to `"chainlink"` for oracle alignment:
```python
# Or add a separate 5m-specific override
self.delta_price_source_5m: str = os.environ.get(
    "DELTA_PRICE_SOURCE_5M", "chainlink"
).lower()
```

### Fix 5: Add Chainlink staleness gate (LOW PRIORITY)

When Chainlink hasn't updated within the current window (on-chain `updatedAt` older than window open), flag the signal as LOW confidence. The `ChainlinkFeed` already tracks `updated_at` per poll. Expose this in the data surface so strategies can gate on oracle freshness.

### Fix 6: SourceAgreementGate oracle weighting (LOW PRIORITY)

When Chainlink and Tiingo/Binance disagree on direction, weight Chainlink as authoritative for 5m Polymarket markets since it IS the resolution oracle. Currently it gets equal vote weight.

---

## 4. Architecture Summary

```
CURRENT (broken):
  Window open_price  <-- Binance REST
  Direction signal   <-- TimesFM p_up (Chainlink features = NaN)
                     <-- OR delta_pct from Tiingo (primary) / Binance (fallback)
  Resolution         <-- Polymarket Gamma API (which uses Chainlink oracle)
  
  MISMATCH: decision uses Binance/Tiingo, resolution uses Chainlink

PROPOSED (fixed):
  Window open_price  <-- Chainlink oracle (primary), Binance (fallback)
  Direction signal   <-- TimesFM p_up (Chainlink features WIRED)
                     <-- AND delta_pct from Chainlink (primary for 5m)
  Resolution         <-- Polymarket Gamma API (Chainlink oracle)
  
  ALIGNED: same oracle for open, delta, and resolution
```

---

## 5. Files Affected

| File | Issue | Fix Priority |
|------|-------|-------------|
| `novakash-timesfm/app/v2_scorer.py:510-511` | chainlink_price hardcoded NaN | HIGH |
| `engine/data/feeds/polymarket_5min.py:500-534` | open_price from Binance, not Chainlink | HIGH |
| `engine/strategies/data_surface.py:314-319` | Delta priority: tiingo > chainlink (should flip for 5m) | MEDIUM |
| `engine/config/runtime_config.py:214` | DELTA_PRICE_SOURCE defaults to tiingo | MEDIUM |
| `engine/strategies/five_min_vpin.py:673-723` | Delta fallback chain uses tiingo-first | MEDIUM |
| `engine/strategies/gates/source_agreement.py:44-48` | Equal vote weight for all sources | LOW |
| `engine/data/feeds/chainlink_feed.py` | No staleness exposure | LOW |
