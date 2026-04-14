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

### Don't confuse "model weights git SHA" with "deployed codebase git SHA" — 2026-04-10 (CORRECTED)
**What happened:** Earlier this session I wrote a lesson titled "Production runs off-branch — Montreal TimesFM is on feat/v2.1-calibration, not main" based on the `model_version` string in `/v2/probability` responses: `"11191d7@v2/btc/btc_5m/11191d7/..."`. I assumed the git SHA `11191d7` meant the server *codebase* was running that commit, concluded Montreal was running `feat/v2.1-calibration`, and warned that redeploying main would downgrade the model. **All of this was wrong.**

Verification revealed:
1. Montreal's `/v3/snapshot` returns real composite data from all 9 timescales (`elm, cascade, taker, oi, funding, vpin, momentum`). But `origin/feat/v2.1-calibration` has **zero** `app/v3_*.py` files — the entire v3 composite system only exists on main. Therefore Montreal cannot be running `feat/v2.1-calibration`; it must be running main-lineage code.
2. Reading `app/v2_model_registry.py` clarified the weights architecture: model artifacts (LightGBM booster + isotonic calibrator) live in S3 at `s3://<bucket>/v2/btc/btc_5m/<training_commit_sha>/<timestamp>/manifest.json`, loaded at runtime via `current.json`. The `11191d7` in the version string is the **training** commit's SHA (Sequoia v4 was trained from that commit), not the serving commit.
3. Merging `feat/v2.1-calibration → main` would have **deleted 1,434 lines of v3 infrastructure** (`v3_composite_scorer.py`, `v3_routes.py`, `v3_multiscale.py`, `v3_macro_store.py`, `v3_db_writer.py`, `v3_cascade_estimator.py`, the v3 migration SQL, and v3 wiring in `main.py`). Catastrophic regression, not a promotion.

**Root cause of the original wrong lesson:** I interpreted a version string as if it were a codebase identifier without reading the registry code. Model-version strings constructed from `git_sha@run_prefix` are provenance markers for the *weights*, which are *data files* loaded at runtime — they are orthogonal to what git ref the server code is on.

**Rule:**
1. Never infer "what branch is deployed" from a `model_version` / `artifact_version` string. Those strings describe data artifacts, not code. To know what code is deployed, SSH in and run `git rev-parse HEAD` in the service directory, or expose a `/health` endpoint that returns the server's own git SHA.
2. When model weights live in S3 (or any external artifact store) and are loaded at runtime via a registry pattern, redeploying server code does NOT change the model. Weight upgrades and code upgrades are independent operations — treat them as such.
3. If you expose a version string to downstream consumers, split it: `server_git_sha` (code), `model_git_sha` (training commit), `model_artifact_key` (S3 path). Ambiguous combined strings cause exactly the confusion that produced this lesson in the first place.

### Stale terminology in a pipeline stays dangerous even when it isn't a bug — 2026-04-10
**What happened:** The v3 composite scorer has a signal named `"elm"` in its weight profiles and output payload. The v2 scorer this signal reads from has been through five model families in sequence: **OAK → CEDAR → DUNE → ELM → SEQUOIA v4**. The `"elm"` key was coined when ELM was the active model; nobody renamed it when DUNE was replaced, or when ELM was replaced. Simultaneously, `app/main.py:99` still carries a comment "Updated to DUNE (v3.1) — genuinely different model" from even earlier. Result: looking at any single file of the codebase gives you a *different* wrong answer about what model is live, and the question "wait, do we use ELM or DUNE or Sequoia?" surfaced three times in one session and wasted ~an hour of diagnostic effort.
**Root cause:** Naming a pipeline key after a specific model family couples the abstraction to implementation identity. Every model swap becomes either (a) a cross-repo rename chore that gets deferred, or (b) silent naming rot. We chose (b) repeatedly, and documentation comments rotted in parallel.
**Rule:** Don't rename the signal key — renames are expensive and the next model will rot the new name too. Instead: (1) expose the *current* model identity where it's actually consulted (the `/v2/probability` response already has `model_version`; `/v3/snapshot` should expose it too at the top level); (2) surface the model family name in the UI so operators can see "SEQUOIA v4" at a glance without grepping code; (3) sweep stale model-family comments during unrelated edits to the same files; (4) if you ever DO rename a pipeline key, use a semantically neutral name (`v2_prob`, `directional_prob`) not another model family name.

### FillResult: exchange adapter owns "how much money moved" — 2026-04-10
**What happened:** The margin engine's Position entity was computing P&L from a hardcoded `0.075%` fee rate and `0.008%` daily borrow rate, neither of which matched Binance's actual fees. The engine was reporting trades as +$0.156 raw when they were actually −$0.059 net — the fee cost trap that caused the 116-trade overnight loss.
**Root cause:** Position was trying to be a calculator when it should have been a record. The Binance adapter had `fills[]` with real commission in the order response but threw it away, returning just `(order_id, price)`.
**Rule:** The exchange adapter is the authority on anything involving money. Adapters should return a rich `FillResult` with actual filled notional, real commission (parsed from `fills[]`), and a `commission_is_actual` flag. Entities store these values and use them as-is; they never re-estimate. Paper-mode adapters must return the same shape so paper and live never drift.

### Montreal-only verification means no local testing — 2026-04-14
**What happened:** I ran a targeted local pytest after identifying the likely production fix.
**Root cause:** I optimized for fast feedback instead of following the user's deployment rule for this engine.
**Rule:** When the user says to obey Montreal rules, do all validation on the Montreal box only. Do not run local tests or use local results as proof.

### v4_down_only must stay independent of fusion-style consensus gating — 2026-04-14
**What happened:** I treated the "consensus not safe_to_trade" behavior as generally acceptable while reviewing the production skips.
**Root cause:** I did not separate the intended behavior of `v4_down_only` from `v4_fusion` clearly enough.
**Rule:** `v4_down_only` is its own executable strategy and should not inherit `v4_fusion`-style consensus blocking unless the user explicitly asks for that. Leave stricter consensus behavior on `v4_fusion` only.

### Fix missing probability inputs at the data-surface layer, not by weakening gates — 2026-04-14
**What happened:** I started adding broader gate fallbacks when `v2_probability_up` / polymarket fields were missing in the registry surface.
**Root cause:** I moved too quickly to make downstream gates more permissive instead of fixing the upstream surface-loading contract.
**Rule:** When a strategy is engineered around the proper probability estimate, repair the data-surface fetch/population path first. Do not paper over missing surface fields by widening gate fallbacks unless the user explicitly asks for a semantic change.

### Execution caps must use the real runtime config, not stale constants — 2026-04-14
**What happened:** I claimed the system had a hard $5 max bet while the actual execution path was still using a hardcoded $50 ceiling and stale bankroll state.
**Root cause:** I checked the active config but not the exact `ExecuteTradeUseCase._calculate_stake()` path that places orders.
**Rule:** Never claim a risk cap is enforced until the exact live execution code path uses the runtime-configured value. Config presence is not proof of enforcement.
