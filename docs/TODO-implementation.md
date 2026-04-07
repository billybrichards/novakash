# Implementation TODO — Phase 2

**Priority:** P0 = do now, P1 = this week, P2 = next week

---

## P0 — Data Accuracy (Montreal engine)

- [ ] **Create `window_predictions` table** on Railway DB
  - Schema in `PLAN-phase2-evaluation.md`
  - Captures Tiingo + Chainlink close prices at T-0 each window
  - Tracks predicted vs actual direction per source

- [ ] **Capture Tiingo close at T-0** in `five_min_vpin.py`
  - At window close (T-0), record tiingo_close price
  - Calculate predicted direction (close > open = UP)
  - Write to window_predictions

- [ ] **Capture Chainlink close at T-0**
  - Query Chainlink contract at window close
  - Same predicted direction calc
  - Write to window_predictions alongside Tiingo

- [ ] **After oracle resolution: update accuracy**
  - When poly_winner arrives, update window_predictions.oracle_winner
  - Set tiingo_correct, chainlink_correct, our_signal_correct booleans

## P0 — Notification Fixes (telegram.py + orchestrator.py)

- [ ] **Kill `window_open` notification**
  - Remove the send call entirely (Gamma 50/50 is useless)

- [ ] **Distinct indicators for SKIP vs UNFILLED BID vs TRADED**
  - SKIP: 🚫 signal not strong enough / gates blocked
  - UNFILLED: ⏳ signal passed gates, bid placed on CLOB, no counterparty
  - TRADED: ✅ bid placed AND filled
  - Show in both per-window card and SITREP

- [ ] **SITREP every 15 min (not 5)**
  - Change `_sitrep_counter >= 30` to `>= 90` (90 × 10s = 15 min)
  - Or: only send SITREP when a trade resolves

- [ ] **Remove duplicate resolution cards**
  - Position monitor (orchestrator) AND outcome_v8 (telegram) both fire
  - Keep ONE source of truth — prefer telegram.py with DB data

## P1 — Reconciliation Service (Montreal, new process)

- [ ] **Build CLOB poller** (`engine/reconciliation/clob_poller.py`)
  - Poll `/data/orders` every 30s for our wallet's orders
  - Match to DB trades by clob_order_id
  - Update trades with real fill status from CLOB
  - Design doc: `RECONCILIATION-SERVICE-DESIGN.md`

- [ ] **Wallet balance history** — new table `wallet_balance_history`
  - Record wallet USDC balance every 5 min
  - Enables accurate P&L timeline
  - Query: `get_balance_allowance` (already have this)

- [ ] **Orphan detector**
  - Find DB trades marked OPEN where CLOB shows MATCHED or EXPIRED
  - Auto-fix status
  - Alert on discrepancies

- [ ] **Deploy as systemd service on Montreal**
  - Runs alongside engine, shares DB connection
  - `sudo systemctl start novakash-recon`

## P1 — AI Window Evaluator (macro-observer on Railway)

- [ ] **Add `/evaluate-window` endpoint** to macro-observer
  - Accepts window_ts, queries all relevant DB tables
  - Builds structured prompt for Claude Sonnet
  - Returns evaluation text

- [ ] **Structured prompt** (see `PLAN-phase2-evaluation.md` section 3)
  - Gate audit (19 checkpoints)
  - Trade/skip details
  - Tiingo vs Chainlink predictions
  - Macro context (OI, funding, L/S)
  - Previous 3 windows for streak context

- [ ] **Telegram delivery**
  - After evaluation, send card via Telegram bot
  - Rate limit: 1 per 60s
  - Only for windows that resolved (not pending)

- [ ] **Deploy updated macro-observer to Railway**
  - Add DB connection to Railway DB
  - Add Anthropic API key for Sonnet calls
  - Add Telegram bot token for sending

## P1 — Notification Merge (telegram.py)

- [ ] **One card per window** replacing current 3-4 cards
  - Format from plan: Signal → Gate decision → Order → Fill → Status
  - If skipped: show why + what outcome would have been
  - If unfilled: show cap, CLOB ask, and "no liquidity"

- [ ] **Resolution card shows full context**
  - Oracle result + our signal + Tiingo/Chainlink prediction
  - Actual fill price + shares + P&L
  - Running session totals + "gates saved" counter

- [ ] **"Gates saved" running counter**
  - Track skipped windows that would have lost
  - Show cumulative $ saved in SITREP
  - Query: window_snapshots where trade_placed=false AND poly_winner != direction

## P2 — Stale Code Removal

- [ ] **Remove `_execute_from_signal`** (521 dead lines in five_min_vpin.py)
- [ ] **Remove Gamma API fallback** in GTC path (~20 lines + aiohttp call)
- [ ] **Remove RFQ path** (~120 lines, 404s on every call, 2s wasted per trade)
- [ ] **Remove TWAP v1 code** (~130 lines, TWAP_ENABLED=false permanently)
- [ ] **Remove TimesFM v1 code** (~78 lines, replaced by v2.2)
- [ ] **Remove opinion_connected** references (~30 refs)
- [ ] **Clean up ORDER_PRICING_MODE** dead references

## P2 — Testing & Validation

- [ ] **Backtest v8.1.2 gates** on historical window_snapshots
  - How many wins would NORMAL gate at <120 have blocked?
  - What's the net impact across 500+ windows?

- [ ] **Monitor fill rate** at different caps
  - Track: how often does $0.55 cap fill vs $0.65 vs $0.73?
  - Are we leaving money on the table with low caps?

- [ ] **Tiingo vs Chainlink accuracy tracking**
  - After 200+ windows: which source predicts oracle better?
  - By regime: does one source do better in CASCADE vs NORMAL?

---

## Files Reference

| Doc | Location |
|-----|----------|
| Changelog | `docs/CHANGELOG-apr7.md` |
| Trade Analysis | `docs/TRADE-ANALYSIS-apr7.md` + PDF |
| Phase 2 Plan | `docs/PLAN-phase2-evaluation.md` + PDF |
| Notification TODOs | `docs/TODO-notifications.md` |
| API Audit | `docs/POLYMARKET-API-AUDIT.md` |
| Recon Service Design | `docs/RECONCILIATION-SERVICE-DESIGN.md` |
| Execution Audit | `docs/8x-pricing-execution.md` |
| Live Data Rules | `docs/LIVE_DATA_RULES.md` |
| Monitor | `docs/MONITOR-apr7.md` |
