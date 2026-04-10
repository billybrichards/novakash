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

### Win rate sources must be unified — 2026-04-06
**What happened:** Audit found 3 independent win rate sources (trades.outcome, window_snapshots.v71_correct, backtest JSONs) giving different numbers. Paper mode fallback resolves from Binance price instead of Polymarket oracle, corrupting win rate history.
**Root cause:** Resolution paths diverged over time — live mode uses oracle, paper mode has Binance fallback, v71_correct has separate logic, backtests use simulated data.
**Rule:** ONE source of truth for trade outcomes: Polymarket oracle. If oracle unavailable, mark PENDING and retry — never guess from Binance price. Document all metrics that intentionally differ (directional WR vs P&L WR).

### Verify agent findings before reporting — 2026-04-06
**What happened:** Subagent audit incorrectly reported /v58/*, /playwright/*, /trading-config/* endpoints as "missing/broken" and api('GET', url) callable syntax as "broken". All were working correctly.
**Root cause:** Agent searched frontend but didn't cross-check with hub/api/ backend routes. Made assumptions without verification.
**Rule:** Cross-verify frontend→backend endpoint claims by checking BOTH sides. Never report an endpoint as missing without grepping the backend router definitions.

### Large files need dedicated audits — 2026-04-06
**What happened:** V58Monitor.jsx is 122KB — too large for a general audit pass to assess properly. Needs dedicated deep-read in chunks.
**Root cause:** File grew organically without decomposition. Single components shouldn't exceed ~500 lines.
**Rule:** Flag any component >500 lines for decomposition. Audit large files in dedicated passes with chunked reads.

### v3 composite is noise at 5m/15m horizons — 2026-04-10
**What happened:** Margin engine overnight: 116 trades, 0% WR, −$26.99. Every trade lost exactly the fee cost (−$0.23 on $125 notional = 0.184% round-trip). All exits were SIGNAL_REVERSAL. Retrospective on margin_logs: avg signal swing during hold = 0.97, median 0.99, max 1.38. Avg first-seen score = −0.01, avg last-seen = −0.01. The composite signal oscillates ±1.0 around zero on a ~4-minute cycle.
**Root cause:** The v3 multiscale composite is not forward-looking at sub-15m horizons. The entry threshold (0.30) gets crossed often due to amplitude, but sign doesn't persist, so trades always exit via reversal at the fee cost.
**Rule:** Before trading on ANY signal, passively record it for N hours and check: (a) autocorrelation at intended hold horizon, (b) forward return conditional on signal > threshold, (c) sign persistence rate. Don't trust backtest WR — instrument and measure the live signal distribution first.

### Option A — Margin engine tuning (parked, do not apply until Option B proves signal has edge) — 2026-04-10
Ranked by expected impact if signal turns out to have edge at longer horizons:
1. **Disable 5m timescale** — 50% of trades, 50% of losses, too noisy.
2. **Raise entry threshold 0.30 → 0.50** — only trade strongest signals (tail of the distribution).
3. **Widen reversal threshold −0.20 → −0.50** — let positions breathe through oscillation.
4. **Add minimum hold time (10 min)** — prevents same-cycle entry/exit churning.
5. **Add take-profit +0.8% / stop-loss −0.8%** — currently reversal always fires first, so other exits never trigger.
**Rule:** Do not apply these until passive signal recording (margin_signals table) has collected ≥24h of data and forward-return analysis shows positive EV at some threshold × timescale combination.

### Polymarket v9.0 is below breakeven live — 2026-04-10
**What happened:** Overnight 212 trades, 48W/36L resolved (57.1% WR), total −$64.65. Backtest claimed 82% WR. Breakeven for YES/NO at 0.68 entry with 1.60 payout is ~68% WR. 57% loses money.
**Root cause:** Single-day backtest regime (the "86% WR day" noted in memory) doesn't generalize. Fee/payoff ratio is unforgiving — 10 percentage points below backtest WR turns profit into significant loss.
**Rule:** For any strategy with asymmetric payoffs (like binary markets), compute breakeven WR explicitly and require live WR to clear breakeven + buffer over ≥500 trades before claiming edge.

### Railway env vars must be set per-environment, not just in .env.example — 2026-04-10
**What happened:** Frontend showed 502 on /api/v3/snapshot and /api/margin/status for hours. I first misdiagnosed it as an AWS security group issue, then as a v3-routes-not-deployed issue. Actual cause: `TIMESFM_URL` and `MARGIN_ENGINE_URL` were in `.env.example` with localhost defaults, but never set as actual env variables on the Railway `hub` service in the `develop` environment. The Hub proxy fell through to `http://localhost:8001`, httpx raised ConnectError, the proxy returned 502. TimesFM itself was healthy the whole time.
**Root cause:** `.env.example` is a template — writing a value there does nothing until it's set on the actual deployment platform. The defaults (`http://localhost:8001`) silently work in dev but fail in prod.
**Rule:** (1) Any cross-service URL in `.env.example` must have a comment warning it MUST be set in prod, naming the specific platform (Railway/AWS) and the consequence of falling through. (2) Before blaming "the other service", probe the failing service's OWN env vars first — especially anything with a localhost default. (3) When adding a new Hub proxy route, add the target URL to Railway at PR time, not as a follow-up.

### Redundant CI workflows silently rot — 2026-04-10
**What happened:** `.github/workflows/railway-deploy.yml` had been failing on every push/PR to develop for weeks because it used old Railway CLI v3 syntax (`railway login --token`) that v4+ rejects. Nobody noticed because Railway's native GitHub integration was deploying the Hub anyway — the workflow was doubly-redundant AND broken, but the product kept working.
**Root cause:** When Railway's GitHub integration was enabled, the old CI workflow wasn't deleted. It continued running, continued failing, and its FAILURE status made every PR check "UNSTABLE" — masking real CI failures and training us to ignore the red mark.
**Rule:** When replacing a deploy mechanism with a platform-native one, DELETE the old workflow immediately. A red check everyone ignores is worse than no check. Audit `.github/workflows/` for "always-failing" workflows periodically.

### Probe the path the production caller uses, not a path you assume exists — 2026-04-10
**What happened:** Diagnosing the 502, I probed `GET http://3.98.114.0:8080/v3/probability` and got 404, concluded "/v3 routes don't exist on that instance". Actually the Hub proxy calls `/v3/snapshot`, not `/v3/probability`. `/v3/snapshot` returns 200 with full composite data. I wasted a cycle on the wrong theory because I probed a path I'd guessed at instead of reading `hub/api/margin.py` first.
**Root cause:** Shortcut — assumed "TimesFM has /v2/probability, so /v3 must have /v3/probability". The actual v3 API surface is `/v3/snapshot` + `/v3/health` + WS `/v3/signal`. No `/v3/probability` exists (never did).
**Rule:** When diagnosing an upstream from a proxy, ALWAYS grep the proxy code first to see what path it actually calls. Never probe the upstream from memory about "what endpoints it should have".

### Production runs off-branch — Montreal TimesFM is on feat/v2.1-calibration, not main — 2026-04-10
**What happened:** The Montreal TimesFM instance at i-0785ed930423ae9fd serves `model version 11191d7@v2/btc/btc_5m/11191d7/...`. Commit `11191d7` is the SEQUOIA v4 commit, which lives only on `feat/v2.1-calibration` — it's NOT on main. So prod runs ahead of main. Anyone redeploying "latest main" onto Montreal would silently downgrade from Sequoia v4 back to whatever v2 scorer main has.
**Root cause:** Deployed a feature branch straight to prod "temporarily" and never merged it back. The branch now has 5 commits ahead of main including the production-running model. Main has been sitting stale.
**Rule:** If you deploy a branch to prod, set a calendar item (or todo.md entry) to merge it within 48h. Prod running off a non-default branch is a deploy-time footgun — and for models specifically, it means a rollback of the deploy = a rollback of the MODEL, which may silently break signal semantics downstream.

### Stale names in schemas are cross-repo tech debt — 2026-04-10
**What happened:** The v3 composite scorer has a signal named `"elm"` in its weight profiles, its output payload, and in the passive-recording DB column (`margin_signals.elm REAL`). The actual model feeding that signal has been Sequoia v4 since Montreal was redeployed. Every time anyone looks at the v3 dashboard they wonder "wait, do we use ELM or Sequoia?". This question came up twice in one session.
**Root cause:** When SEQUOIA v4 replaced the old ELM model as the v2 scorer, nobody renamed the downstream `"elm"` references because they were buried in several places (timesfm python + novakash frontend + DB column + JSON schema). The rename is cross-repo and cross-service — expensive enough to defer but cheap enough individually that it just never got scheduled.
**Rule:** When swapping a model's implementation, also schedule the rename of any schema/column/variable that still refers to the old model. Either do it at swap time, or add an explicit "rename-debt" line in lessons.md with a target date. Cross-repo renames are best handled via dual-emit: backend emits both old and new keys, frontend reads new with fallback to old, old key removed one release later.

### FillResult: exchange adapter owns "how much money moved" — 2026-04-10
**What happened:** The margin engine's Position entity was computing P&L from a hardcoded `0.075%` fee rate and `0.008%` daily borrow rate, neither of which matched Binance's actual fees. The engine was reporting trades as +$0.156 raw when they were actually −$0.059 net — the fee cost trap that caused the 116-trade overnight loss.
**Root cause:** Position was trying to be a calculator when it should have been a record. The Binance adapter had `fills[]` with real commission in the order response but threw it away, returning just `(order_id, price)`.
**Rule:** The exchange adapter is the authority on anything involving money. Adapters should return a rich `FillResult` with actual filled notional, real commission (parsed from `fills[]`), and a `commission_is_actual` flag. Entities store these values and use them as-is; they never re-estimate. Paper-mode adapters must return the same shape so paper and live never drift.
