# Changelog — 6 April 2026

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
