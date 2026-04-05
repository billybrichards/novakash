# Claude / AI Integration

Novakash uses a dual-AI system for trade evaluation and decision analysis.

---

## Overview

| Role | Model | Purpose |
|------|-------|---------|
| **Primary evaluator** | Claude Opus 4.6 (Anthropic) | Trade decision analysis, skip/trade reasoning |
| **Fallback** | Qwen 122B | Used when Claude is unavailable or times out |

The AI evaluator is called at each trade decision point (placed or skipped) and on resolution (win/loss). Output is sent to Telegram and optionally saved to the `ai_analyses` DB table.

---

## Claude Evaluator

**Module:** `engine/evaluation/claude_evaluator.py`

### When It's Called

1. **Trade decision** — Every window at T-60s, whether a trade is placed or skipped
2. **Fill confirmation** — When a GTC order fills (or fails to fill)
3. **Resolution** — When Polymarket resolves the market (WIN or LOSS)

### Timeout

`CLAUDE_TIMEOUT = 60` seconds (1 minute). If Claude doesn't respond in 60 seconds, the engine falls back to the Qwen evaluator and continues. The trade decision itself is NOT blocked by the AI call — the trade decision is made synchronously, then Claude is called async for post-hoc analysis.

### What Claude Receives

The evaluator sends all available context:
- Asset, timeframe, direction, confidence level
- Whether the trade was placed or skipped (and skip reason)
- Fill status: `FILLED`, `UNFILLED`, or `FOK_KILLED`
- Price data: window open/close, current BTC price, Gamma token prices
- VPIN value and regime
- CoinGlass snapshot (OI, liquidations, L/S ratio, funding)
- TWAP direction and agreement score
- TimesFM direction and confidence

### Claude's Output

Claude's analysis is:
1. Stripped of Telegram-breaking markdown
2. Sent as a Telegram message to the trading chat
3. Saved to `ai_analyses` table (if DB client available)

The analysis typically covers:
- Was the trade/skip decision correct given the data?
- What does the CoinGlass context suggest?
- Does TimesFM agreement increase or decrease confidence?
- Any red flags or patterns worth noting?

---

## Telegram Alerts

**Module:** `engine/alerts/telegram.py` and `engine/alerts/telegram_v2.py`

### Alert Types

| Type | Trigger | Content |
|------|---------|---------|
| `trade_placed` | GTC order submitted | Direction, price, stake, VPIN, regime |
| `trade_filled` | Order filled on CLOB | Fill price, shares, vs expected |
| `trade_skipped` | Window evaluated, no trade | Skip reason, VPIN, delta |
| `trade_won` | Polymarket resolution WIN | P&L, entry price, outcome |
| `trade_lost` | Polymarket resolution LOSS | P&L, entry price, what went wrong |
| `kill_switch` | Max drawdown triggered | Current balance, drawdown % |
| `cooldown` | 3 consecutive losses | Pause duration |
| `feed_error` | Data feed disconnected | Which feed, reconnect status |
| `window_chart` | Per-window signal chart | Visual VPIN, delta, TimesFM chart |

### Chart Generation

`window_chart.py` generates matplotlib charts showing:
- BTC price through the window
- VPIN evolution
- TimesFM forecast band (P10–P90)
- TWAP line
- Trade entry point (if placed)
- Outcome marker (WIN/LOSS)

Charts are attached to Telegram messages as images.

### Telegram Config

```env
TELEGRAM_BOT_TOKEN=<bot_token>
TELEGRAM_CHAT_ID=<chat_id>
TELEGRAM_ALERTS_PAPER=true    # Send alerts for paper trades
TELEGRAM_ALERTS_LIVE=false    # Send alerts for live trades (set true in prod)
```

---

## Dual-AI Fallback System

### Primary: Claude Opus 4.6

Called via Anthropic API using `ANTHROPIC_API_KEY`. High-quality analysis but has a 1-minute timeout. If the API is down, key is invalid, or response takes too long, the fallback kicks in.

### Fallback: Qwen 122B

A larger open-weight model used when Claude is unavailable. Configuration is not exposed via settings — the fallback is handled internally in the evaluator. When Qwen is used, the Telegram alert notes "AI fallback (Qwen)" so you know the analysis quality may differ.

### No AI Available

If both Claude and Qwen fail, the engine continues trading normally. The AI evaluation is purely advisory — it never blocks trade execution. A Telegram message is sent noting "AI evaluation unavailable."

---

## AI in the Hub

The Hub also exposes AI-related endpoints:

- `GET /api/analysis` — Stored AI analyses
- `POST /api/analysis` — Upload external AI analysis documents
- `GET /api/forecast/timesfm` — TimesFM forecast (not Claude, but the ML model)

The `ai_analyses` table stores Claude's per-trade assessments for review.

---

## CLAUDE.md (Root)

The root `CLAUDE.md` in the repo is a different file — it's instructions for AI coding agents (Claude, Qwen) working on this codebase. It covers:

- Planning protocol (write plan before coding)
- Subagent strategy
- Self-improvement loop (lessons.md)
- Verification requirements before marking complete
- Key file locations and architecture
- All constants and configuration values

When an AI agent works on this repo, it reads root `CLAUDE.md` first for context.

---

## Agent Workflow Notes

When using AI agents (via OpenClaw) to work on this repo:

1. **Push from OpenClaw VPS** — never from Montreal
2. **Railway auto-deploys** from `main` branch only
3. **Engine restarts** require SSH to Montreal
4. **Polymarket API calls** must only run on Montreal (15.223.247.178)
5. **DB writes** go to Railway PostgreSQL — accessible from anywhere
6. **Analysis tasks** (read DB, review code, write docs) are safe to run on OpenClaw VPS
