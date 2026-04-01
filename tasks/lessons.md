# tasks/lessons.md — BTC Trader Hub

Lessons learned from corrections. Review at session start.

## Format

```
### [Lesson Title] — YYYY-MM-DD
**What happened:** Brief description of the mistake or correction
**Root cause:** Why it happened
**Rule:** The rule to follow to prevent recurrence
```

---

### Subagent code must be audited — 2026-04-01
**What happened:** Subagent wrote polymarket_5min.py with hardcoded $45K open price, five_min_vpin.py with 25% hardcoded stake, and orchestrator that logged signals but didn't forward them to strategy.
**Root cause:** Subagent didn't have full context of how components wire together. No audit before deploy.
**Rule:** ALWAYS audit subagent output against the full system before deploying. Check the data flow end-to-end.

### Paper mode must match backtest exactly — 2026-04-01
**What happened:** Backtest showed 82% win rate. Live paper showed 17% (1 win, 5 losses). Multiple mismatches: VPIN scale, token pricing, open price, min delta threshold, resolution logic.
**Root cause:** Backtest and live system evolved separately with different assumptions baked into each.
**Rule:** After writing a backtest, diff every parameter/logic path against the live code. Create a checklist: pricing model, thresholds, VPIN calculation, resolution, risk limits.

### VPIN scales differ between implementations — 2026-04-01
**What happened:** Backtest VPIN (simple buy_pct ratio, range 0.10-0.55) vs live VPIN (academic bucket-based, range 0.90-0.99). Using backtest thresholds on live VPIN meant every trade got HIGH confidence.
**Root cause:** Two different VPIN implementations measuring fundamentally different things.
**Rule:** When using a metric across systems, verify the SCALE first. Log the range and mean before writing threshold logic.

### Resolution logic must match the oracle — 2026-04-01
**What happened:** Paper trades resolved by checking "did BTC move >0.1% from entry price?" In 5-min windows BTC moves 0.01-0.05%, so everything was a LOSS.
**Root cause:** Resolution logic was written for cascade strategy (large moves) and reused for 5-min markets (tiny moves) without adapting.
**Rule:** Each strategy needs its own resolution logic matching the actual market oracle. For 5-min: close >= open → UP wins. That's it.

### Risk manager and strategy must agree on sizing — 2026-04-01
**What happened:** Strategy calculated 25% of bankroll ($25). Risk manager capped at BET_FRACTION (2.5% = $0.62). Every trade blocked for 90 minutes.
**Root cause:** Strategy hardcoded its own sizing mode ("safe" = 25%) that conflicted with the global risk cap.
**Rule:** Strategy stake calculation MUST use the same BET_FRACTION the risk manager enforces. One source of truth.

### Railway: check service root directory — 2026-04-01
**What happened:** Engine deploys kept failing with "no associated build". The service was configured for GitHub deploy but root directory wasn't set to /engine.
**Root cause:** Railway needs to know which subdirectory contains the Dockerfile.
**Rule:** For monorepo with subdirectory Dockerfiles, set Root Directory in Railway dashboard or deploy from the subdirectory.

### Always build-test frontend locally before pushing — 2026-04-01
**What happened:** Learn.jsx had an unterminated string literal + mismatched tags. Failed Railway build twice.
**Root cause:** Pushed without running `npx vite build` locally.
**Rule:** Run `npx vite build` before any frontend push. 3 seconds saves 3 minutes of failed deploys.

### Environment variables reset on deploy — 2026-04-01
**What happened:** Changed STARTING_BANKROLL from 25→100 and BET_FRACTION, but strategy still used hardcoded values.
**Root cause:** Some code read env vars at import time, some at runtime, some were hardcoded.
**Rule:** All tuneable parameters must come from constants.py which reads env vars. Never hardcode values that should be configurable.
