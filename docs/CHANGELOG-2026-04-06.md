# Changelog — 6 April 2026

---

## v8.0 Phase 3 — TWAP Override Removal + TimesFM Gate Disable (20:25 UTC)

### Summary
Feature-flagged off three harmful gates: TWAP direction override, TWAP gamma gate,
and TimesFM agreement gating. Today's data showed TWAP blocked 12 windows, 8 of
which were winners (net harmful). TimesFM accuracy is 47.8% — worse than coin flip.
All code remains in place for re-enablement; flags default to false.
Also fixed macro observer "missing price deltas" bug (TIMESTAMPTZ column mismatch).

### Changes

#### feat: gate feature flags (`engine/config/runtime_config.py`)
- Added `twap_override_enabled` (env: `TWAP_OVERRIDE_ENABLED`, default `false`)
- Added `twap_gamma_gate_enabled` (env: `TWAP_GAMMA_GATE_ENABLED`, default `false`)
- Added `timesfm_agreement_enabled` (env: `TIMESFM_AGREEMENT_ENABLED`, default `false`)
- All three are env-only, not DB-synced (structural flags, not tunable parameters)

#### feat: TWAP gamma gate feature-flagged (`engine/strategies/five_min_vpin.py`)
- Wrapped TWAP `should_skip` early-return in `evaluate_signal` with `if runtime.twap_gamma_gate_enabled:`
- When disabled: logs `evaluate.twap_gate_would_block` for monitoring (values visible but no skip)
- When enabled: original v5.7c behaviour — returns `None` if TWAP says skip

#### feat: TWAP direction override feature-flagged (`engine/strategies/five_min_vpin.py`)
- Wrapped TWAP `all_agree` / `twap_gamma_agree` direction override with `if runtime.twap_override_enabled:`
- When disabled: logs `evaluate.twap_direction_info` with TWAP direction for monitoring
- When enabled: original v5.7 behaviour — overrides `direction` when TWAP+Gamma agree
- TWAP confidence boost/penalty also wrapped — only applies when `twap_override_enabled=true`

#### feat: TimesFM gate feature-flagged (`engine/strategies/five_min_vpin.py`)
- Wrapped TimesFM agreement skip/suppress logic with `if runtime.timesfm_agreement_enabled:`
- When disabled: logs `evaluate.timesfm_agrees/disagrees` for monitoring with `monitoring only` note
- When enabled: restores pre-v5.8.1 gating (high-conf disagree = skip, disagree = suppress HIGH→MODERATE)
- TimesFM fetch in `_evaluate_window` now guarded by `runtime.timesfm_enabled` to skip entirely when off

#### feat: gate status in window_snapshot (`engine/strategies/five_min_vpin.py`)
- Added `twap_override_active: bool` — tracks whether TWAP override flag was on (False = Phase 3)
- Added `twap_gamma_gate_active: bool` — tracks whether gamma gate was on
- Added `timesfm_gate_active: bool` — tracks whether TimesFM gate was on
- Enables post-hoc "what would have happened with gates on?" analysis

#### feat: gate_audit enhancements (`engine/strategies/five_min_vpin.py`)
- Added `gate_twap_gamma` — actual gate result (True = passed)
- Added `gate_twap_gamma_shadow` — what TWAP gate WOULD have done (regardless of flag)
- Added `gate_timesfm` — actual TimesFM gate result
- Added `gate_timesfm_shadow` — what TimesFM gate would have done
- Added `twap_override_active`, `twap_gamma_gate_active`, `timesfm_gate_active` flag values

#### fix: macro observer price deltas bug (`macro-observer/observer.py`)
- `fetch_btc_deltas` was passing Unix int to `TIMESTAMPTZ` column → silent exception → fallback with no deltas
- Fixed: SQL now uses `NOW() - INTERVAL '24 hours'` (proper timestamp arithmetic)
- Fixed: `_delta()` helper now compares `datetime` objects (asyncpg returns tz-aware datetime from TIMESTAMPTZ)
- Result: macro observer will now correctly compute BTC delta_15m/1h/4h/24h fields

### Feature Flags
| Variable | Default | Effect |
|---|---|---|
| `TWAP_OVERRIDE_ENABLED` | `false` | Allow TWAP+Gamma to override point-delta direction |
| `TWAP_GAMMA_GATE_ENABLED` | `false` | Allow TWAP should_skip to block trades |
| `TIMESFM_AGREEMENT_ENABLED` | `false` | Allow TimesFM to gate/suppress trades |

### What Was NOT Changed
- TWAP/TimesFM code is preserved — can be re-enabled via env vars
- CoinGlass veto system (v7.1) — unchanged, working correctly
- Execution / order placement code
- Tiingo delta source (Phase 1)

### Branch
`develop` — commit pushed, not deployed to Montreal

---

## v8.0 Phase 1 — Tiingo Delta Swap (20:09 UTC)

### Summary
Engine version bumped to v8.0. Tiingo REST 5m candle is now the primary delta source,
replacing Binance spot (71.6% oracle accuracy → 96.9%). Fully additive with feature flag.
Gate audit table added for per-window pass/fail analysis.

### Changes

#### feat: Tiingo REST 5m candle delta (`engine/strategies/five_min_vpin.py`)
- **New**: `_fetch_tiingo_candle` logic inline in `_evaluate_window()` (~line 245)
- Queries `https://api.tiingo.com/tiingo/crypto/prices?tickers=btcusd&resampleFreq=5min` for the exact window (window_ts → window_ts+300)
- Computes `delta_tiingo = (tiingo_close - tiingo_open) / tiingo_open * 100` from candle open/close
- 3-second timeout on REST call; falls back to DB `ticks_tiingo` latest tick if unavailable
- Second fallback: Binance price (unchanged legacy path)
- Feature flag: `DELTA_PRICE_SOURCE` env var (`tiingo` | `chainlink` | `binance`), default `tiingo`
- Stores `delta_source`, `tiingo_open`, `tiingo_close`, `delta_tiingo` in window snapshot

#### feat: DELTA_PRICE_SOURCE runtime config (`engine/config/runtime_config.py`)
- Added `self.delta_price_source` field (env-only, not DB-synced)
- Reads `DELTA_PRICE_SOURCE` env var, default `"tiingo"`
- Documented as v8.0 price source feature flag

#### feat: gate_audit DB writes (`engine/strategies/five_min_vpin.py`, `engine/persistence/db_client.py`)
- `db_client.write_gate_audit()` — upserts per-window gate audit record
- Written for EVERY window evaluation (non-blocking `asyncio.create_task`)
- Records: `gate_vpin`, `gate_delta`, `gate_cg`, `gate_passed`, `gate_failed`, `gates_passed_list`, `decision`, `skip_reason`
- Enables offline "which gate is blocking wins?" analysis

#### migration: gate_audit table (`migrations/add_gate_audit_table.sql`)
- Creates `gate_audit` table with all gate result columns
- Includes indexes on window_ts, asset, decision

#### chore: bump engine_version to "v8.0"
- Changed `"engine_version": "v7.1"` → `"v8.0"` in window snapshot dict

### Feature Flags
| Variable | Value | Effect |
|---|---|---|
| `DELTA_PRICE_SOURCE` | `tiingo` (default) | Use Tiingo REST candle |
| `DELTA_PRICE_SOURCE` | `chainlink` | Use Chainlink DB price (v7.1 behaviour) |
| `DELTA_PRICE_SOURCE` | `binance` | Use Binance spot (legacy) |

### What Was NOT Changed
- Execution / order placement code (Phase 2 FOK)
- Macro observer wiring (data collection only)
- TWAP removal (Phase 3)

### Branch
`develop` — commit pushed, not deployed to Montreal

---

## Overview

Major analysis and architecture day. No code deployed to live engine.
Built the macro-observer as a standalone Railway service (not yet deployed).
Key discoveries: oracle mismatch root cause, WR analysis, entry price problems.

---

## Key Discoveries

### Oracle Mismatch — ROOT CAUSE FOUND
- Polymarket uses **Chainlink Data Streams** (`data.chain.link/streams/btc-usd`)
- NOT Binance spot price
- 15+ oracle nodes, each sampling 3+ USD-settled CEXes (Coinbase, Kraken, Gemini, OKX)
- Aggregates to LWBA (Liquidity-Weighted Bid/Ask) median mid-price
- Reads price at **exact millisecond** of window open/close timestamps
- **57% mismatch rate** — Binance showed DOWN but oracle resolved UP
- Root causes: Binance USDT premium ($10-50 vs USD-settled), LWBA vs last-trade, timestamp precision
- Fix: use Tiingo (listed oracle node, free) for signal price instead of Binance

### WR Analysis — 18h Live Trading (21:30 Apr5 → 15:28 Apr6)
- 20 resolved trades | 8W/12L | **40% WR** | -$52.51 net
- BTC was +3.4% uptrend — engine bet DOWN into bull run all session
- bestAsk fills at $0.73 require **73% WR to break even** — unachievable
- Cheap fills ($0.31-0.40) have excellent R/R (30-40% break-even WR)
- 30-day backfill WR: 71.4% overall, 65.2% DOWN-only

### Entry Price Problem
- bestAsk > $0.60 = negative EV (need >60% WR just to break even)
- Current $0.73 fills = need 73% WR, we're hitting 40-50%
- Solution: entry price gate (not yet implemented — monitoring first)

---

## Built This Session

### feat: macro-observer standalone Railway service
**Branch:** feat/macro-observer (pending creation + push)
**Files added:**
- `macro-observer/observer.py` — async polling loop, Anthropic call, DB writer
- `macro-observer/requirements.txt`
- `macro-observer/Dockerfile`
- `macro-observer/railway.toml`

**What it does:**
- Polls every 60s (configurable via `POLL_INTERVAL` env var)
- Gathers: resolved Polymarket outcomes (1h + 4h), BTC deltas (15m/1h/4h/24h),
  Coinbase/Kraken/Binance prices (exchange spread = oracle divergence risk),
  CoinGlass (OI, funding, L/S, taker), VPIN trend, spike detection,
  recent ai_analyses summaries, upcoming macro_events, session stats
- Calls `claude-sonnet-4-5` with 10s timeout, ~$0.007/call
- Returns structured MacroSignal: `{bias, confidence, direction_gate, threshold_modifier, size_modifier, override_active, reasoning}`
- Writes to `macro_signals` table with full payload logged
- Falls back to NEUTRAL signal if Anthropic times out — engine always has a fresh row

**Three modes:**
- Neutral (<50%): engine runs unchanged
- Trend-Aware (50-79%): gate contrarian bets, adjust delta thresholds
- Override (80%+): early entry T-120/T-180, direction flip, 1.3x sizing

**Architecture:** DB is the only interface. Observer writes, engine reads.
Engine crash ≠ observer crash. Deploy observer ≠ restart engine.

### migration: add_macro_observer_tables.sql
**File:** `migrations/add_macro_observer_tables.sql`
- Creates `macro_signals` table (29 columns, full signal + input logging)
- Creates `macro_events` table (economic calendar — Fed/CPI/FOMC etc)
- Adds columns to `window_snapshots`: macro_bias, macro_confidence, macro_override_active, macro_signal_id, coinbase_price, exchange_spread_usd

### docker-compose: add macro-observer service
Added `macro-observer` service to `docker-compose.yml` for local dev.

---

## TODO Updated

Added to `TODO.md`:
- **Macro Observer Engine Integration (Phase 2)** — engine wiring after Railway deploy
- **Tiingo Integration** — oracle-aligned price source, API key already available
- **Gamma Balance Block** — feature flag, monitor data first before implementing

---

## Fixes Applied (18:45 UTC)

### fix: floor bypass when Gamma returns None (Issue #5)
**File:** `engine/strategies/five_min_vpin.py`
**What:** Previously, if Gamma API returned `None` for bestAsk, the floor check was entirely bypassed — `if _fresh_best_ask is not None and _fresh_best_ask < _min_entry` evaluated False and fell through. This allowed $0.03 and $0.023 trades that lost $10+.
**Fix:** Added explicit `None` handling before the floor check:
1. If bestAsk is None → attempt Chainlink price fallback from `ticks_chainlink`
2. If Chainlink also unavailable → SKIP the trade with reason logged
3. If Chainlink exists but no token price → SKIP (existence confirmed but can't price)
4. Only proceeds to floor check if `_fresh_best_ask` is a real number
**Impact:** Prevents all future floor-bypass incidents. Additive — no existing behaviour changed when Gamma returns a valid price.

### fix: extend fill poll from 30s to 60s (Issue #1)
**File:** `engine/strategies/five_min_vpin.py`
**What:** YES/UP orders were filling on the CLOB (confirmed: `MATCHED`, 6.85 shares) but our fill-check loop gave up after 30 seconds. The engine recorded them as `OPEN/EXPIRED` when they were actually winning trades.
**Fix:**
1. Extended `MAX_WAIT` from 30s → 60s
2. Added `FIRST_CHECK` at 3s (catches fast fills immediately)
3. Subsequent polls every 5s as before
**Impact:** YES fills that previously went undetected will now be recorded. Fixes the $41 P&L discrepancy found in today's audit.

---

## Pending (Not Yet Done)

1. **Railway deploy** — macro-observer needs its own Railway service created (Billy to do)
2. **DB migration** — run `add_macro_observer_tables.sql` on Railway postgres
3. **Engine integration** — Phase 2: wire orchestrator to read macro_signals
4. **Tiingo** — add to data-collector once API key confirmed
5. **Entry price gate** — block bestAsk > $0.60 (waiting for Billy approval)

---

## Environment Variables Required (macro-observer)

| Variable | Description | Default |
|---|---|---|
| `DATABASE_URL` | Railway postgres URL | required |
| `ANTHROPIC_API_KEY` | Anthropic API key | required |
| `POLL_INTERVAL` | Seconds between calls | 60 |
| `ANTHROPIC_TIMEOUT` | Anthropic call timeout | 10 |

---

## Cost Estimate

- ~$0.007/call × 60 calls/hour × 24h = **~$10/day** at 60s intervals
- Reduce to 90s during quiet hours to cut to ~$7/day
- Negligible vs trading losses prevented
