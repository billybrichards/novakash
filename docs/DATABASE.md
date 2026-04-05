# Database

**Host:** `hopper.proxy.rlwy.net:35772`  
**Database:** `railway`  
**User:** `postgres`  
**Engine:** PostgreSQL 16

All services (engine, hub, data-collector) connect to this single shared database.

---

## Tables

### `window_snapshots`

One row per 5-minute window evaluated by the engine. The core analytical table — used by the v5.8/v7.1 monitor dashboard.

| Column | Type | Description |
|--------|------|-------------|
| `id` | integer | Primary key |
| `window_ts` | bigint | Unix timestamp (seconds) of window open |
| `asset` | varchar | Asset symbol (e.g., `BTC`) |
| `timeframe` | varchar | Window duration (e.g., `5m`) |
| `open_price` | float | BTC price at window open |
| `close_price` | float | BTC price at T-60s (when signal fires) |
| `delta_pct` | float | Price change open→T-60s as decimal (0.0005 = 0.05%) |
| `vpin` | float | VPIN value at evaluation time (0–1) |
| `regime` | varchar | `NORMAL`, `TRANSITION`, or `CASCADE` |
| `direction` | varchar | Primary signal direction: `UP` or `DOWN` |
| `confidence` | float | Overall signal confidence (0–1) |
| `cg_modifier` | float | CoinGlass confidence modifier applied |
| `trade_placed` | boolean | Whether a trade was actually placed |
| `skip_reason` | varchar | Reason for skip if no trade placed |
| `outcome` | varchar | `WIN`, `LOSS`, or null (pending) |
| `pnl_usd` | float | Realised P&L for this window |
| `poly_winner` | varchar | Polymarket resolution: `UP` or `DOWN` |
| `btc_price` | float | BTC spot price at evaluation |
| `twap_delta_pct` | float | TWAP price delta over window |
| `twap_direction` | varchar | TWAP directional signal |
| `twap_agreement_score` | integer | TWAP agreement score (0–10) |
| `twap_gamma_gate` | varchar | TWAP gate result: `OK` or reason for fail |
| `twap_gamma_agree` | boolean | Whether TWAP agrees with Gamma market direction |
| `twap_confidence_boost` | float | Confidence adjustment from TWAP |
| `twap_n_ticks` | integer | Number of TWAP ticks used |
| `twap_stability` | float | Price stability metric |
| `twap_trend_pct` | float | Trend strength percentage |
| `twap_momentum_pct` | float | Momentum percentage |
| `twap_should_skip` | boolean | TWAP gate recommendation |
| `twap_skip_reason` | varchar | Why TWAP recommends skipping |
| `timesfm_direction` | varchar | TimesFM forecast direction |
| `timesfm_confidence` | float | TimesFM confidence (derived from quantile spread) |
| `timesfm_predicted_close` | float | TimesFM predicted close price |
| `timesfm_delta_vs_open` | float | TimesFM predicted delta vs open |
| `timesfm_spread` | float | TimesFM P90–P10 uncertainty range |
| `timesfm_p10` | float | TimesFM 10th percentile forecast |
| `timesfm_p50` | float | TimesFM 50th percentile (median) forecast |
| `timesfm_p90` | float | TimesFM 90th percentile forecast |
| `timesfm_agreement` | boolean | Whether TimesFM agrees with primary direction |
| `gamma_up_price` | float | Polymarket UP token price at evaluation |
| `gamma_down_price` | float | Polymarket DOWN token price at evaluation |
| `gamma_mid_price` | float | Mid-price between UP and DOWN tokens |
| `gamma_spread` | float | Spread between UP and DOWN tokens |
| `gamma_price_quality` | varchar | `LIVE`, `STALE`, or `SYN` |
| `market_best_bid` | float | CLOB best bid |
| `market_best_ask` | float | CLOB best ask |
| `market_spread` | float | CLOB spread |
| `market_mid_price` | float | CLOB mid price |
| `market_volume` | float | CLOB market volume |
| `market_liquidity` | float | CLOB liquidity |
| `cg_connected` | boolean | Whether CoinGlass feed was active |
| `cg_oi_usd` | float | Open interest in USD |
| `cg_oi_delta_pct` | float | OI change percentage |
| `cg_liq_long_usd` | float | Long liquidations in last interval |
| `cg_liq_short_usd` | float | Short liquidations in last interval |
| `cg_liq_total_usd` | float | Total liquidations |
| `cg_long_pct` | float | Long position percentage |
| `cg_short_pct` | float | Short position percentage |
| `cg_long_short_ratio` | float | Long/short ratio |
| `cg_top_long_pct` | float | Top trader long percentage |
| `cg_top_short_pct` | float | Top trader short percentage |
| `cg_top_ratio` | float | Top trader L/S ratio |
| `cg_taker_buy_usd` | float | Taker buy volume |
| `cg_taker_sell_usd` | float | Taker sell volume |
| `cg_funding_rate` | float | Perpetual funding rate |
| `engine_version` | varchar | Engine version string (e.g., `v7.1`) |
| `v71_would_trade` | boolean | Would v7.1 config have traded this window? (retroactive) |
| `v71_skip_reason` | text | Why v7.1 would have skipped |
| `v71_regime` | varchar | Regime classification under v7.1 |
| `v71_correct` | boolean | Was v7.1 direction correct? (from Polymarket resolution) |
| `v71_pnl` | numeric | Hypothetical v7.1 P&L for this window |
| `created_at` | timestamptz | Row creation time |

---

### `trades`

Every trade placed by the engine (paper or live).

| Column | Type | Description |
|--------|------|-------------|
| `id` | integer | Primary key |
| `order_id` | varchar | Unique order ID |
| `strategy` | varchar | Strategy name (e.g., `five_min_vpin`) |
| `venue` | varchar | Exchange (e.g., `polymarket`) |
| `market_slug` | varchar | Polymarket market slug |
| `direction` | varchar | `YES` (UP) or `NO` (DOWN) |
| `entry_price` | numeric | Token price paid |
| `stake_usd` | numeric | USD amount staked |
| `fee_usd` | numeric | Fee paid |
| `status` | varchar | `PENDING`, `OPEN`, `RESOLVED` |
| `outcome` | varchar | `WIN`, `LOSS`, `PUSH` |
| `payout_usd` | numeric | Total payout received |
| `pnl_usd` | numeric | Net P&L (payout - stake - fee) |
| `metadata` | jsonb | Extra data: window_ts, vpin, delta, etc. |
| `mode` | varchar | `paper` or `live` |
| `engine_version` | varchar | Engine version when trade was placed |
| `vpin_at_entry` | numeric | VPIN reading at time of trade |
| `created_at` | timestamptz | Order placement time |
| `resolved_at` | timestamptz | When Polymarket resolved the market |

---

### `market_data`

Collected by the data-collector service. One row per Polymarket window, with open/close prices and resolution data.

| Column | Type | Description |
|--------|------|-------------|
| `id` | integer | Primary key |
| `window_ts` | bigint | Unix timestamp of window open |
| `asset` | varchar | Asset (BTC, ETH, SOL, XRP) |
| `timeframe` | varchar | `5m` or `15m` |
| `market_slug` | varchar | Polymarket market slug |
| `condition_id` | varchar | Polymarket condition ID |
| `question` | text | Market question text |
| `up_price` | float | UP token price |
| `down_price` | float | DOWN token price |
| `best_bid` | float | CLOB best bid |
| `best_ask` | float | CLOB best ask |
| `spread` | float | CLOB spread |
| `volume` | float | Market volume |
| `liquidity` | float | Market liquidity |
| `up_token_id` | varchar | Polymarket UP token ID |
| `down_token_id` | varchar | Polymarket DOWN token ID |
| `open_price` | float | BTC price at window open |
| `close_price` | float | BTC price at window close |
| `resolved` | boolean | Whether market has resolved |
| `outcome` | varchar | `UP` or `DOWN` (Polymarket resolution) |
| `window_start` | timestamptz | Window open time |
| `window_end` | timestamptz | Window close time |
| `collected_at` | timestamptz | When data was first collected |
| `resolved_at` | timestamptz | When Polymarket resolved |
| `snapshot_count` | integer | Number of price snapshots collected |
| `last_snapshot_at` | timestamptz | Last time data was updated |

---

### `market_snapshots`

Sub-second price snapshots within each window. Used for entry timing analysis (what was the price at T-240, T-180, T-120, T-90, T-60?).

| Column | Type | Description |
|--------|------|-------------|
| `id` | integer | Primary key |
| `window_ts` | bigint | Window unix timestamp |
| `asset` | varchar | Asset symbol |
| `timeframe` | varchar | Window duration |
| `up_price` | float | UP token price at snapshot |
| `down_price` | float | DOWN token price at snapshot |
| `best_bid` | float | CLOB best bid |
| `best_ask` | float | CLOB best ask |
| `volume` | float | Market volume |
| `seconds_remaining` | integer | Seconds left in window when snapshot taken |
| `snapshot_at` | timestamptz | Snapshot timestamp |

---

### `ticks_binance`

Raw Binance aggTrade ticks. Every trade that flows through the engine is recorded here for backtesting and VPIN analysis.

| Column | Type | Description |
|--------|------|-------------|
| `id` | bigint | Primary key |
| `ts` | timestamptz | Trade timestamp |
| `asset` | varchar | Asset (e.g., `BTC`) |
| `price` | float | Trade price |
| `quantity` | float | Trade quantity |
| `is_buyer_maker` | boolean | True = sell-initiated; False = buy-initiated |
| `vpin` | float | VPIN value after this tick |
| `created_at` | timestamptz | DB insert time |

---

### `ticks_coinglass`

CoinGlass derivatives data snapshots, polled every ~5 seconds.

| Column | Type | Description |
|--------|------|-------------|
| `id` | bigint | Primary key |
| `ts` | timestamptz | Snapshot timestamp |
| `asset` | varchar | Asset symbol |
| `oi_usd` | float | Total open interest in USD |
| `oi_delta_pct` | float | OI change vs previous snapshot |
| `liq_long_usd` | float | Long liquidations in interval |
| `liq_short_usd` | float | Short liquidations in interval |
| `long_pct` | float | Overall long position % |
| `short_pct` | float | Overall short position % |
| `top_long_pct` | float | Top trader long % |
| `top_short_pct` | float | Top trader short % |
| `taker_buy_usd` | float | Taker buy volume |
| `taker_sell_usd` | float | Taker sell volume |
| `funding_rate` | float | Perpetual funding rate |
| `long_short_ratio` | float | Aggregate L/S ratio |
| `top_position_ratio` | float | Top trader position ratio |
| `created_at` | timestamptz | DB insert time |

---

### `ticks_gamma`

Polymarket Gamma API price ticks — UP/DOWN token prices as polled during active windows.

| Column | Type | Description |
|--------|------|-------------|
| `id` | bigint | Primary key |
| `ts` | timestamptz | Snapshot timestamp |
| `asset` | varchar | Asset symbol |
| `timeframe` | varchar | Window duration |
| `window_ts` | bigint | Which window this belongs to |
| `up_price` | float | UP token price |
| `down_price` | float | DOWN token price |
| `price_source` | varchar | `LIVE`, `STALE`, or `SYN` |
| `up_token_id` | varchar | Polymarket UP token ID |
| `down_token_id` | varchar | Polymarket DOWN token ID |
| `slug` | varchar | Market slug |
| `created_at` | timestamptz | DB insert time |

---

### `ticks_timesfm`

TimesFM forecast results, one row per forecast query.

| Column | Type | Description |
|--------|------|-------------|
| `id` | bigint | Primary key |
| `ts` | timestamptz | Forecast timestamp |
| `asset` | varchar | Asset symbol |
| `window_ts` | bigint | Window this forecast applies to |
| `window_close_ts` | bigint | When window closes |
| `seconds_to_close` | integer | Seconds until window closes |
| `direction` | varchar | Forecast direction: `UP` or `DOWN` |
| `confidence` | float | Confidence derived from quantile spread |
| `predicted_close` | float | Point estimate for close price |
| `spread` | float | P90 – P10 uncertainty range |
| `p10` | float | 10th percentile forecast |
| `p50` | float | Median (50th percentile) forecast |
| `p90` | float | 90th percentile forecast |
| `delta_vs_open` | float | Predicted change from window open |
| `horizon` | integer | Forecast horizon in steps |
| `fetch_latency_ms` | float | HTTP round-trip time to TimesFM service |
| `is_stale` | boolean | True if using cached forecast |
| `created_at` | timestamptz | DB insert time |

---

### `trading_configs`

Named, versioned trading configurations. The hub API allows creating/activating configs without redeploying the engine.

| Column | Type | Description |
|--------|------|-------------|
| `id` | integer | Primary key |
| `name` | varchar | Config name (e.g., `v7.1-live`) |
| `version` | integer | Version number |
| `description` | text | Human-readable description |
| `config` | jsonb | Full config JSON (bet_fraction, thresholds, etc.) |
| `mode` | varchar | `paper` or `live` |
| `is_active` | boolean | Whether this config is currently active |
| `is_approved` | boolean | Whether this config has been approved for use |
| `approved_at` | timestamptz | When approved |
| `approved_by` | varchar | Who approved it |
| `parent_id` | integer | References parent config (for forks) |
| `created_at` | timestamptz | Creation time |
| `updated_at` | timestamptz | Last update time |

---

### `system_state`

Single-row table (id=1). The engine heartbeats this row; the hub reads it for the dashboard status.

| Column | Type | Description |
|--------|------|-------------|
| `id` | integer | Always 1 (single row constraint) |
| `engine_status` | varchar | `running`, `stopped`, `error` |
| `current_balance` | numeric | Current bankroll |
| `peak_balance` | numeric | All-time high balance |
| `current_drawdown_pct` | numeric | Current drawdown from peak |
| `binance_connected` | boolean | Binance WS feed status |
| `coinglass_connected` | boolean | CoinGlass API status |
| `chainlink_connected` | boolean | Chainlink RPC status |
| `polymarket_connected` | boolean | Polymarket CLOB status |
| `opinion_connected` | boolean | Opinion exchange status |
| `last_vpin` | numeric | Most recent VPIN value |
| `last_cascade_state` | varchar | Current cascade FSM state |
| `active_positions` | integer | Number of open positions |
| `last_trade_at` | timestamptz | Last trade placement time |
| `last_heartbeat` | timestamptz | Last engine heartbeat |
| `config` | jsonb | Active runtime config snapshot |
| `paper_enabled` | boolean | Paper trading mode active |
| `live_enabled` | boolean | Live trading mode active |
| `active_paper_config_id` | integer | FK to trading_configs |
| `active_live_config_id` | integer | FK to trading_configs |

---

### `telegram_notifications`

Audit log of all Telegram messages sent by the engine.

| Column | Type | Description |
|--------|------|-------------|
| `id` | bigint | Primary key |
| `sent_at` | timestamptz | When message was sent |
| `bot_id` | varchar | Telegram bot ID |
| `location` | varchar | Where in the engine this was sent from |
| `window_id` | varchar | Associated window ID |
| `notification_type` | varchar | `trade`, `skip`, `win`, `loss`, `error`, etc. |
| `message_text` | text | Full message text |
| `has_chart` | boolean | Whether a chart image was attached |
| `engine_version` | varchar | Engine version at send time |
| `telegram_message_id` | bigint | Telegram message ID (for edits) |

---

## Other Tables

| Table | Description |
|-------|-------------|
| `signals` | General signal log (VPIN events, cascade states, tick events) |
| `daily_pnl` | Pre-aggregated daily P&L for charting |
| `backtest_runs` | Backtest run results |
| `manual_trades` | Manual trades placed via dashboard |
| `countdown_evaluations` | Per-stage evaluation logs (T-240, T-180, T-120, T-90, T-60) |
| `ai_analyses` | Claude analysis results stored for review |
| `analysis_docs` | Analysis documents uploaded to the hub |
| `hourly_reports` | Hourly performance summaries |
| `redeem_events` | Automatic position redemption events |
| `playwright_state` | Browser automation state (legacy) |
| `users` | Hub user accounts (single user) |

---

## Useful Queries

```sql
-- Recent windows with v7.1 performance
SELECT window_ts, asset, vpin, regime, direction, v71_would_trade, v71_correct, v71_pnl
FROM window_snapshots
WHERE timeframe = '5m'
ORDER BY window_ts DESC
LIMIT 50;

-- Overall v7.1 win rate
SELECT 
    COUNT(*) FILTER (WHERE v71_correct = true) as wins,
    COUNT(*) FILTER (WHERE v71_correct = false) as losses,
    ROUND(COUNT(*) FILTER (WHERE v71_correct = true)::numeric / 
          NULLIF(COUNT(*) FILTER (WHERE v71_correct IS NOT NULL), 0) * 100, 1) as win_rate_pct
FROM window_snapshots
WHERE timeframe = '5m' AND v71_would_trade = true;

-- Recent trades
SELECT order_id, direction, entry_price, stake_usd, outcome, pnl_usd, created_at
FROM trades
WHERE strategy = 'five_min_vpin'
ORDER BY created_at DESC
LIMIT 20;

-- Engine heartbeat check
SELECT engine_status, last_heartbeat, current_balance, last_vpin
FROM system_state
WHERE id = 1;
```
