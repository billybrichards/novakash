# Changelog — 6 April 2026

---

## v8.0 Phase 2 — FOK Execution Ladder (20:39 UTC)

### Summary
Replaced the single GTC/GTD order at stale Gamma price with a Fill-or-Kill
execution ladder that queries the live CLOB book directly for pricing. FOK
orders fill immediately or are cancelled — no resting on the book at bad prices.
The GTC path is fully preserved as a feature-flag fallback (`FOK_ENABLED=false`).

### Changes

#### feat: FOK Ladder (`engine/execution/fok_ladder.py`) — NEW FILE
- Class `FOKLadder` with async `execute()` method
- Parameters: `token_id`, `direction`, `stake_usd`, `max_price` (default $0.73), `min_price` (default $0.30)
- Config env vars: `FOK_ATTEMPTS` (default 5), `FOK_INTERVAL_S` (default 2s), `FOK_BUMP` (default $0.01)
- Flow:
  1. Query CLOB book → get live best ask via `PolymarketClient.get_clob_best_ask()`
  2. If best_ask < floor or > cap → abort with reason logged
  3. Submit FOK at best_ask price via `PolymarketClient.place_fok_order()`
  4. If filled → return `FOKResult(filled=True, fill_price, fill_step, shares, attempts)`
  5. If killed → wait `FOK_INTERVAL_S`, query fresh book, set next price = `fresh_ask + FOK_BUMP`
  6. Repeat up to `FOK_ATTEMPTS` times
  7. If all attempts exhausted → return `FOKResult(filled=False, ...)`
- Each attempt logs: attempt number, price, size, result
- Paper mode: `get_clob_best_ask` returns simulated price; `place_fok_order` always fills
- Returns `FOKResult` dataclass with: `filled`, `fill_price`, `fill_step`, `shares`, `attempts`, `order_id`, `attempted_prices`, `abort_reason`

#### feat: CLOB helpers (`engine/execution/polymarket_client.py`)
- `async get_clob_best_ask(token_id) → float`
  - Queries `client.get_order_book(token_id)` via `asyncio.to_thread`
  - Sorts asks ascending, returns lowest ask price
  - Paper: returns simulated `random.uniform(0.40, 0.65)`
  - Raises `ValueError` if no ask-side liquidity
- `async place_fok_order(token_id, price, size) → dict`
  - Builds `OrderArgs` + `client.post_order(signed, OrderType.FOK)`
  - Returns `{filled: bool, size_matched: float, order_id: str}`
  - Paper: always fills at requested price (simulated)
  - Respects `LIVE_MAX_TRADE_USD` safety cap
  - Emits first-live-trade warning on first call

#### feat: FOK_ENABLED flag (`engine/config/runtime_config.py`)
- Added `self.fok_enabled: bool` — reads `FOK_ENABLED` env var, default `"true"`
- Env-only, not DB-synced (structural execution flag, not a tunable parameter)
- Included in `snapshot()` dict for logging

#### feat: FOK execution path in strategy (`engine/strategies/five_min_vpin.py`)
- Added import: `from execution.fok_ladder import FOKLadder, FOKResult`
- When `runtime.fok_enabled = True`:
  - Instantiates `FOKLadder(self._poly)` and calls `ladder.execute()`
  - On FOK miss: logs `FOK_LADDER_EXHAUSTED` with all attempted prices, sends Telegram alert, returns
  - On FOK fill: uses `fok_result.fill_price` as actual price for order record
  - Records `clob_fill_price`, `fok_attempts`, `fok_fill_step` to `order.metadata`
- When `runtime.fok_enabled = False`:
  - Falls back to original `self._poly.place_order()` GTC/GTD path (unchanged)
- Post-trade fill verification poll (60s, 3s first check) retained for BOTH paths
- FOK fill step shown in Telegram fill notification

### Feature Flags

| Variable | Default | Effect |
|---|---|---|
| `FOK_ENABLED` | `true` | Use FOK ladder instead of GTC |
| `FOK_ATTEMPTS` | `5` | Max FOK attempts per signal |
| `FOK_INTERVAL_S` | `2` | Seconds between FOK attempts |
| `FOK_BUMP` | `0.01` | Price bump per attempt ($0.01 = 1¢) |
| `FOK_PRICE_CAP` | `0.73` | Maximum fill price (hard cap) |
| `PRICE_FLOOR` | `0.30` | Minimum fill price (hard floor) |

### What Was NOT Changed
- GTC/GTD order placement code — preserved as fallback
- Fill detection poll (60s, 5s interval) — shared by both paths
- Risk checks, stake calculation, order registration
- Telegram fill notifications (FOK step added to message)

### Branch
`develop` — commit pushed, not deployed to Montreal

---

## Frontend AWS Deployment + UI Improvements (21:30 UTC)

### Summary
Deployed frontend to AWS EC2 (Montreal, ca-central-1) with nginx reverse proxy to Railway hub.
Added Signal vs Actual direction columns to both trade tables for clearer outcome visibility.

### AWS Frontend Instance
- **Instance:** `i-0fe72a610900b5cca` / `99.79.41.246` / t3.small Ubuntu 24.04
- **Key pair:** `novakash-local-rsa` (`~/.ssh/novakash-local-rsa.pem`)
- **Security group:** `sg-05606e7fee858ca86`
- Nginx proxies `/api/`, `/auth/`, `/ws/` → `hub-develop-0433.up.railway.app`
- Montreal region chosen per Polymarket geo-block rules (Rule 2)
- Old broken instance (`i-097ee664dda263be0`) terminated

### feat: Signal vs Actual direction columns
**Files:** `frontend/src/pages/FactoryFloor.jsx`, `frontend/src/pages/LiveTrading.jsx`

**Flow Timeline (Factory Floor):**
- Split `DIR` column into `SIGNAL` (our v5.7c call) + `ACTUAL` (oracle resolution)
- Mismatches highlighted with red background on ACTUAL cell

**Recent Trades (Live Trading):**
- Renamed `Dir` → `Signal`, added new `Actual` column
- Oracle direction derived from `direction` + `outcome` (no backend change needed)
- YES+WIN or NO+LOSS = market went UP; YES+LOSS or NO+WIN = market went DOWN

### Docs Updated
- `docs/INFRASTRUCTURE.md` — added Frontend (AWS) section
- `docs/DEPLOYMENT.md` — added frontend redeploy steps

### Branch
`claude/review-docs-and-setup-uGPRs` → merged to `develop`

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

---

## v8.0 Phase 4 — Execution Audit & Fixes (21:00-22:00 UTC)

### Critical Bugs Fixed

1. **CLOB feed sort bug** — `asks[0]` on descending list returned $0.99 (worst ask) instead of best ask. Every CLOB price in the DB and notifications was wrong. Fixed: sort ascending, take first.

2. **`_get_runtime_config()` crash** — method doesn't exist on strategy object. Every trade execution crashed silently after signal evaluation. Fixed: `from config.runtime_config import runtime`.

3. **FOK wired to dead method** — FOK ladder was added to `_execute_from_signal()` (never called) instead of `_execute_trade()` (the actual execution path). Fixed: moved to correct method.

4. **`runtime` referenced before assignment** — variable defined at line 2374 but used at line 2178. Fixed: moved to method top.

5. **Gamma pricing block blocking FOK** — 250 lines of stale Gamma API pricing ran before FOK. Removed entirely — CLOB-first execution.

6. **T-60 retry never fired** — multi-offset eval block was inside `ACTIVE`-only state handler. After T-70 set state to `CLOSING`, T-60 block never executed. Fixed: run for both ACTIVE and CLOSING.

7. **Feed `break` prevented dual offset** — `break` after first offset emission prevented T-60 from firing when both became eligible between ticks. Removed.

8. **FOK exhausted = abort** — FOK failure returned instead of falling through to GTC. Fixed: fall through to GTC with CLOB DB price → Gamma API → window price cascade.

9. **Cap/floor only in notification, not execution** — gate showed ❌CAP but engine still placed order. Fixed: added cap/floor check before execution using Gamma + fresh CLOB prices.

10. **AI evaluators using Opus** — $0.03/call. Switched to Sonnet: $0.003/call (10x cheaper). Fed v8.0 data (delta source, multi-source deltas, confidence tier).

### Improvements

- CLOB poll interval: 10s → 2s (configurable via `CLOB_POLL_INTERVAL`)
- Staggered execution queue bypassed — direct eval for instant FOK
- GTC fallback price cascade: CLOB DB (2s fresh) → Gamma API → window price
- Dual eval: T-70 first chance, T-60 retry with fresh data
- All notifications updated to v8.0 format
- FOK → GTC notification shows real abort reason
- Session running totals in outcome cards
- Dead Gamma pricing block removed (-248 lines)
- Old 30s poll + bump retry removed (-194 lines)

### DB Verification

CLOB prices now correct in `ticks_clob`:
```
UP ask:   $0.19-$0.43 (was $0.99)
DOWN ask: $0.58-$0.82 (was $0.99)
```

### What's Working

- ✅ T-70/T-60 dual evaluation firing correctly
- ✅ FOK ladder queries real CLOB book
- ✅ GTC fallback with fresh CLOB DB prices
- ✅ Cap/floor blocks before execution
- ✅ All notification cards v8.0 format
- ✅ Sonnet AI evaluators with v8.0 context
- ✅ CLOB feed recording correct best asks every 2s

---

## First v8.0 Fill (22:14 UTC)

**Milestone:** First successful v8.0 trade execution and fill.

- Window BTC-1775513400, UP direction, 6.85 shares MATCHED in 5s
- FOK decimal precision error fixed (CLOB needs 2-decimal size)
- ORDER_PRICING_MODE switched from `bestask` to `cap`
  - `bestask`: submit at Gamma price + bump → doesn't fill on thin books
  - `cap`: submit at $0.73 cap → CLOB fills at market price → works
- Cap pricing is NOT overpaying: CLOB fills at best available ask, cap is just the maximum
- Three failed fills (22:00, 22:05, 22:10 windows) all caused by bestask pricing
- Shadow resolution system live — checks oracle outcome for skipped windows
- CLOB feed sort bug was root cause of most today's issues (showing $0.99 instead of real $0.34-$0.67)
