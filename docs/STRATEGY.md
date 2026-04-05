# Trading Strategy

## Overview

The active strategy is **v7.1 — Five-Minute VPIN with TimesFM Gate**. It trades Polymarket's ephemeral 5-minute BTC Up/Down prediction markets using a multi-signal pipeline with strict gates.

---

## Market Structure

Polymarket creates a new BTC Up/Down market every 5 minutes. Each market asks: "Will BTC be higher than the opening price when this window closes?"

- **Window duration:** 300 seconds (5 minutes)
- **Outcome:** YES (UP) or NO (DOWN)
- **Resolution:** Polymarket oracle resolves approximately **4 minutes** after window close
- **Prices:** UP token + DOWN token, each priced 0–1 (sum ≈ 1.0 after fees)
- **Fees:** ~2% Polymarket fee on wins

---

## v7.1 Configuration

| Parameter | Value | Notes |
|-----------|-------|-------|
| `vpin_gate` | **0.45** | Skip window if VPIN < 0.45 |
| `delta_threshold_normal` | **0.02%** | Min price move in NORMAL/TRANSITION regime |
| `delta_threshold_cascade` | **0.01%** | Min price move in CASCADE regime |
| `cascade_threshold` | **0.65** | VPIN ≥ 0.65 = CASCADE regime |
| `informed_threshold` | **0.55** | VPIN ≥ 0.55 = TRANSITION regime |
| `entry_cap` | **$0.70** | Max entry price — skip if token costs more than $0.70 |
| `signal_evaluation_time` | **T-60s** | Evaluate 60 seconds before window close |
| `bet_fraction` | **2.5%** | Kelly-fraction stake sizing |
| `min_bet_usd` | **$2.00** | Minimum stake |
| `max_open_exposure_pct` | **30%** | Max % of bankroll in open positions |

---

## Signal Pipeline

### 1. VPIN — Volume-Synchronized Probability of Informed Trading

VPIN measures the presence of informed traders in the order flow. High VPIN means smart money is actively trading one direction.

**Method:** Easley et al. bulk-volume classification using fixed USD-volume buckets
- Bucket size: $50,000 USD notional
- Lookback: 50 buckets (rolling window)
- Classification: Binance `aggTrade.is_buyer_maker` flag
  - `is_buyer_maker=True` → taker SOLD (sell-initiated)
  - `is_buyer_maker=False` → taker BOUGHT (buy-initiated)
- VPIN = mean imbalance `|buy_vol - sell_vol| / total_vol` over last 50 buckets

**Gate:** If VPIN < 0.45, skip the window. Low VPIN means the market is noise-dominated.

### 2. Regime Classification

Three regimes based on VPIN value at evaluation time:

| Regime | VPIN Range | Delta Threshold | Behaviour |
|--------|-----------|-----------------|-----------|
| **NORMAL** | < 0.55 | 0.02% | Standard conditions |
| **TRANSITION** | 0.55–0.65 | 0.02% | Elevated informed flow |
| **CASCADE** | ≥ 0.65 | 0.01% | High informed flow, looser delta needed |

In CASCADE regime, the engine uses a looser delta threshold (0.01% vs 0.02%) because large informed moves come fast and a tighter filter would skip them.

### 3. Window Delta

The price change from window open to T-60s:

```
delta_pct = (current_price - open_price) / open_price * 100
```

**Gate:** If `|delta_pct| < regime_threshold`, skip. Small price moves are not confident enough for directional bets.

**Direction signal:** `delta_pct > 0 → UP`, `delta_pct < 0 → DOWN`

### 4. TWAP Delta

A time-weighted average price tracker that builds a directional picture across the entire window (not just open→T-60s).

- Computes multi-segment price evolution
- Produces `twap_direction` (UP/DOWN) and `twap_agreement_score` (0–10)
- High agreement score = price has been consistently moving one direction throughout the window
- Used as a confirmation signal, not a primary gate

### 5. CoinGlass Veto System

CoinGlass provides derivatives market context: open interest, liquidations, long/short ratios, funding rates.

**Signals used:**
- `oi_delta_pct` — sudden OI drop signals forced liquidations
- `liq_long_usd` / `liq_short_usd` — recent liquidation volumes
- `long_short_ratio` — top-trader positioning
- `funding_rate` — directional bias from funding

**Veto:** If CoinGlass data strongly contradicts the window delta direction (e.g., heavy long liquidations but signal is UP), the CG modifier reduces confidence. Extreme signals can veto the trade entirely.

**Note:** CoinGlass data is optional — the engine trades without it if the feed is disconnected.

### 6. TimesFM Forecast Gate

TimesFM is a Google time-series foundation model running on the dedicated AWS EC2 instance. It ingests recent BTC price history and outputs:

- `direction` — predicted UP or DOWN for the window close
- `confidence` — derived from quantile spread (P10–P90): tighter spread = higher confidence
- `predicted_close` — point estimate for window close price
- `p10/p50/p90` — uncertainty bounds

**Gate:** TimesFM must **agree** with the primary direction signal. If TimesFM disagrees, the window is skipped.

Queried at T-60s. If the service is unreachable, falls back to TWAP-only direction.

### 7. Gamma Best Price (Entry Cap)

Gamma API provides real-time Polymarket prices for UP and DOWN tokens.

**Gate:** Entry price must be ≤ $0.70. If UP token costs $0.75, expected value is negative even if correct.

**Order type:** GTC (Good Till Cancelled) limit order at Gamma best price.

---

## Signal Evaluation Flow (T-60s)

```
Window opens (T=0)
    │
    ├─ VPIN accumulates from Binance trades
    ├─ TWAP tracks price through window
    ├─ CoinGlass polls every 5s
    │
T-60s: EVALUATE
    │
    ├─ Gate 1: VPIN ≥ 0.45?  ──► NO → skip (TIMESFM_ONLY regime label)
    │
    ├─ Gate 2: |delta_pct| ≥ threshold?  ──► NO → skip
    │
    ├─ Gate 3: TimesFM agrees with delta direction?  ──► NO → skip
    │
    ├─ Gate 4: Entry price ≤ $0.70?  ──► NO → skip
    │
    ├─ Gate 5: Risk manager approval?  ──► NO → skip (exposure, drawdown)
    │
    └─ TRADE: GTC limit order at Gamma best price
```

---

## Order Execution

- **Venue:** Polymarket CLOB (Central Limit Order Book)
- **Order type:** GTC (Good Till Cancelled) limit order
- **Price:** Gamma best price at T-60s
- **FOK kill:** If order not filled within timeout, cancelled
- **Stake sizing:** 2.5% Kelly fraction of current bankroll

---

## Resolution

Polymarket uses Chainlink oracle prices to resolve UP/DOWN markets. Resolution happens approximately **4 minutes after window close** (not immediately). The engine's `redeemer.py` monitors for winning positions and automatically redeems them.

---

## Risk Management

| Parameter | Value |
|-----------|-------|
| Max drawdown kill switch | 45% of peak balance |
| Daily loss limit | 10% of bankroll |
| Consecutive loss cooldown | 3 losses → 15 min pause |
| Max open exposure | 30% of bankroll |
| Kelly fraction | 2.5% |

---

## Version History

| Version | Key Change |
|---------|-----------|
| v5.7c | TWAP + window delta baseline |
| v5.8 | Added TimesFM agreement gate |
| v6.0 | TimesFM-only experiment |
| **v7.1** | VPIN gate (0.45), regime-aware delta thresholds, CASCADE/TRANSITION/NORMAL |

---

## Backtest Performance

See `BACKTEST_RESULTS.md` and the backtest PNG files in the repo root for historical performance data. The v7.1 config was tuned on 7-day and 14-day backtests using real Polymarket resolution data (not just Binance directional match).
