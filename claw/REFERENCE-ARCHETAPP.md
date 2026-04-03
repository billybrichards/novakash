# Reference: Archetapp's Polymarket Bot

**Source:** https://gist.github.com/Archetapp/7680adabc48f812a561ca79d73cbac69  
**Purpose:** Reference implementation for Playwright auto-claim integration

---

## What It Is

A 5-minute BTC Up/Down trading bot for Polymarket with 6 files:

| File | Purpose |
|------|---------|
| `bot.py` | Main engine — timing, order placement, bankroll |
| `strategy.py` | 7-indicator composite signal |
| `auto_claim.py` | **Playwright auto-claimer for winning positions** |
| `compare_runs.py` | Backtesting across 27 configs |
| `backtest.py` | Historical candle fetcher |
| `setup_creds.py` | API credential derivation from private key |

---

## Key Functions We Want to Integrate

### From `auto_claim.py` (Playwright)
- **Auto-claim winning positions** via browser automation
- Uses headless Chrome to navigate Polymarket
- Finds settled markets and clicks "Redeem"
- Background process, runs alongside trading

### From `bot.py` (Strategy Reference)
- **Window delta** as dominant signal (weight 5-7)
- **T-10s entry timing** (we use T-60s)
- **Fill-or-Kill market buy** with GTC limit fallback at $0.95
- **3 modes:** flat (25%), safe (profits only), degen (all-in)

### From `strategy.py` (Signal Reference)
- 7 weighted indicators:
  1. Window Delta (weight 5-7) — THE dominant signal
  2. Micro Momentum (weight 2) — last 2 candles
  3. Acceleration (weight 1.5) — momentum building/fading
  4. EMA Crossover 9/21 (weight 1)
  5. RSI 14-period (weight 1-2)
  6. Volume Surge (weight 1) — 1.5x average
  7. Real-Time Tick Trend (weight 2) — 2-sec polling micro-trends
- Confidence: `min(abs(score) / 7.0, 1.0)`

---

## What We're Taking

1. **Playwright auto-claim pattern** → Adapt for Novakash's `engine/playwright/` module
2. **Gmail 2FA login flow** → Novakash already has GMAIL_APP_PASSWORD configured
3. **Portfolio scraping** → Balance, orders, history extraction from Polymarket UI
4. **Screenshot preview** → For dashboard browser preview tab

---

## What We're NOT Taking

- Their strategy (we have our own VPIN + window delta)
- Their timing (T-10s — we use T-60s which works better for us)
- Their bankroll modes (we have Kelly sizing + risk manager)
