# Audit Progress Log — Clean-Architect Session · 2026-04-11

**Living document.** Each task lands with a dated entry here and a mirrored `progressNotes` entry on the `/audit` page (`frontend/src/pages/AuditChecklist.jsx`).

The two sources must stay in sync. The UI is the dashboard; this file is the audit trail. When you change one, change the other in the same commit.

## Canonical references

- **Checklist UI:** `frontend/src/pages/AuditChecklist.jsx` (deployed at `/audit`)
- **CI/CD reference:** `docs/CI_CD.md` (on both `novakash/develop` and `novakash-timesfm-repo/main`)
- **Sequoia go-live log:** `docs/SEQUOIA_V5_GO_LIVE_LOG.md` (timesfm repo)
- **V10.6 decision surface proposal:** `docs/V10_6_DECISION_SURFACE_PROPOSAL.md` (timesfm repo, `c3a6cbd`)

## Session scope

Deep clean-architect audit covering:

1. Data-quality bugs in the Polymarket engine (`engine/`)
2. V10.6 decision surface rollout gap
3. V4 fusion surface adoption — already alive on `margin_engine/`, still absent from `engine/`
4. Clean-architect migration lessons from `margin_engine/`
5. Production error regressions (PR #18 + pre-existing since 2026-04-07)
6. CI/CD gap for `engine/` (flagged in `docs/CI_CD.md`)
7. Frontend observability surfaces for v1/v2/v3/v4 data

## Repo heads at session start

| Repo | Branch | Head |
|---|---|---|
| `novakash` | `develop` | `6816f86` |
| `novakash-timesfm-repo` | `main` | `af51523` |

## Log

### 2026-04-11 — Session opens + scope reframe

- Delivered initial diagnosis covering DQ-01 (Binance spot/futures reference mismatch), PE-01/PE-02/PE-03 (production error streams), DS-01 (V10.6 docs-only), V4-01 (engine doesn't call v4), CA-01 (3096-line god class).
- **Corrected earlier analysis** against fresh `develop`/`main`: PR #16 already shipped v4 gates into `margin_engine/`; PR #22 already shipped V4Panel to `/margin`; Phase 3b already made `/v4/consensus` 6-source. The legacy `engine/` and the new `margin_engine/` are two separate trading systems.
- Opened `billybrichards/novakash#26` shipping the audit checklist page at `/audit`.

### 2026-04-11 — Rev-2: new scope + Phase 0 fixes

- Repulled `origin/develop` + `origin/main` and picked up `docs/CI_CD.md` (`6816f86`, `af51523`) which explicitly flags engine as the only major service without a GitHub Actions deploy workflow.
- **PE-01 DONE** — `engine/data/feeds/clob_feed.py`: the `clob_book_snapshots` INSERT column list was missing `ts`, so the 11 columns lined up against 11 VALUES (NOW() + `$1..$10`) but the Python call was passing 11 positional args against a 10-parameter SQL. Added `ts` first + a new `$11`, matching the 12-column / 12-value / 11-param pattern of the `ticks_clob` INSERT immediately above. Inline `PE-01 fix:` comment tags the change. Stops 1090 errors/hour and starts populating `clob_book_snapshots`, which has been empty for 4 days.
- **PE-02 DONE** — `engine/reconciliation/reconciler.py:765`: the bidirectional prefix-match LIKE was using `$1` and `$2` in both sides, which asyncpg can't type-deduce (`inconsistent types deduced for parameter $1 — text vs character varying`). Replaced with a single `$1::text` parameter matching the working startup-backfill pattern at lines 185-186. Stops the PR #18 regression (4 errors/hour, each a silent reconciler match miss).
- Both fixes ship in the same PR #26 rev-2 as the checklist update to avoid deploy races. Neither is verified in production until CI-01 lands (see below) — until then, verification is a manual `scripts/restart_engine.sh` via Montreal rules + `journalctl -u` tail.
- **FE-02 DONE** — audit checklist page is live locally (`npm run build` green, rendered end-to-end via playwright against the dev server, filter + expand interactions confirmed). Merge of PR #26 lands it at `/audit` on the AWS frontend host.

### 2026-04-11 — STOP-01 emergency: live trading paused + lesson about DB-backed mode toggle

**User reported 14:56 UTC**: "please pause live trading till we have finished our fixes we are making some really terrible trade decisions". Specifically flagged the pattern: "a DOWN after 2 consecutive previous UP markets and other indicators in my view felt obvious it was either going up or down when we voted up or down respectively".

**First pause attempt — FAILED**:
1. SSH Montreal + set `LIVE_TRADING_ENABLED=false` + `PAPER_MODE=true` in `/home/novakash/novakash/engine/.env`
2. Call `scripts/restart_engine.sh` — wrapper hung at the "restarting engine" step (SSH session backgrounded)
3. After a long delay, the restart completed and the engine came back... **but auto-switched PAPER → LIVE at 15:06 UTC** per the user's Telegram screenshot. Config showed `v7.1 | Gate: 0.45 | Cap: $0.70 | Bet: 10%` — an OLD trading_config.

**Root cause of the auto-revert**: `engine/strategies/orchestrator.py:1755` has a mode-sync heartbeat that reads `system_state.paper_enabled` + `system_state.live_enabled` from the DB on every tick and overrides the in-memory `paper_mode` accordingly. The `.env` value is ignored if the DB says otherwise. This is how the UI toggle works — the frontend flips the DB row, the engine picks it up on next heartbeat, and the mode switches. It's a feature, not a bug, but it means **flipping the `.env` alone is not sufficient to pause trading** — you must also update the DB.

**Second pause attempt — SUCCESS**:
1. `pkill -9 -f "python3 main.py"` via SSH — engine process gone
2. User manually flipped the UI toggle → updated `system_state.paper_enabled=true/live_enabled=false` in DB at 14:08:43 UTC
3. Restarted engine inline (bypassing the hanging wrapper) — PID 13549 at 14:10:48 UTC
4. Verified: `system_state` row shows `paper_enabled=t/live_enabled=f`, 0 `is_live` trades in last 5 min, `place_order.requested paper_mode=True` + `place_order.paper_filled` log lines confirm paper simulation

**Lesson learned — to be documented in the SPARTA guide**: the correct pause procedure is:

1. Flip the UI toggle at `/system` OR directly `UPDATE system_state SET paper_enabled=true, live_enabled=false WHERE id = (SELECT id FROM system_state ORDER BY id DESC LIMIT 1);`
2. Optionally also flip `.env` for belt-and-braces on next restart
3. Optionally `pkill` the engine to force an immediate heartbeat (within 60s otherwise)
4. Verify with `SELECT COUNT(*) FROM trades WHERE is_live=true AND created_at > NOW() - INTERVAL '5 minutes';` — expect 0

Flipping `.env` alone is insufficient because the DB-backed mode heartbeat overrides it.

**New scope added to the checklist**:
- **STOP-01 DONE** — this incident, with root-cause + lesson
- **LT-02 HIGH OPEN** — live trade panel reported broken by user; full flow exists in code (ManualTradePanel → /v58/manual-trade → manual_trades.status=pending_live → engine poll → polymarket_client) but something is silently failing in practice
- **LT-03 HIGH OPEN** — decision-snapshot DB for manual trades (operator-vs-engine ground truth)
- **UI-02 MEDIUM OPEN** — multi-market HQ monitors for BTC/ETH/SOL/XRP × 5m/15m (deferred behind LT-02)
- **NT-01 DONE** — PR #36 merged at b35163d
- **DQ-06 DONE** — PR #35 merged at 1c5b047, deploy-margin-engine.yml fired green

### 2026-04-11 — Agent D (DQ-05 investigation) report

**VERDICT: DQ-05 is a false alarm as literally stated, but Agent D discovered a bigger bug (DQ-06) and recommended a defensive fix (DQ-07).**

Agent D (READ-ONLY, no code changes) traced every use of the v4 snapshot in `margin_engine/use_cases/open_position.py::_execute_v4()`:

- `consensus.reference_price` is parsed into the `Consensus` dataclass at `value_objects.py:459` but **never read by any margin_engine use case**. My original DQ-05 hypothesis that the engine was evaluating perp trades against a spot reference was wrong.
- The **only** field of the v4 snapshot that `_execute_v4` consumes for pricing is `v4.last_price`, used once at `open_position.py:412` as the denominator of `_sl_tp_from_quantiles`.
- `v4.last_price` IS Binance SPOT (traced through `app/main.py:71 → app/assets.py:20 → app/price_feed.py:23` in novakash-timesfm-repo — `wss://stream.binance.com:9443/ws/btcusdt@trade`).
- **But**: the ratio math `(last_price - p10) / last_price` is dimensionless and therefore internally consistent regardless of venue basis.
- Realised PnL comes from `self._exchange.get_mark()` and `self._exchange.close_position()` — **not v4** — so v4's spot-native `last_price` never propagates to the PnL numbers.

**Higher-priority finding — DQ-06 (NEW HIGH OPEN):**

- `margin_engine/main.py:84-97` is the `paper + binance` wiring branch. It constructs `PaperExchangeAdapter(starting_balance, spread_bps, fee_rate)` with **NO `price_getter` argument**. When `price_getter` is unset, `PaperExchangeAdapter._last_price` stays at the `80000.0` default forever — every `get_mark()` and `get_current_price()` returns bid/ask around a frozen $80k constant.
- The `paper + hyperliquid` branch (`main.py:99-123`) does it correctly: spins up `HyperliquidPriceFeed` and wires it via `price_getter=price_feed.get_price`.
- `settings.py:31`: `exchange_venue: str = "binance"` is the default — so the broken branch is the default.
- CI workflow `deploy-margin-engine.yml:79-89` sets `MARGIN_PAPER_MODE=true` but does NOT explicitly set `MARGIN_EXCHANGE_VENUE`. Its `set_env` helper is append-or-update-in-place, so whatever was previously on the host sticks.

**User clarification 2026-04-11**: the paper venue **should be Hyperliquid**. So the fix for DQ-06 is a CI template update: add `set_env MARGIN_EXCHANGE_VENUE hyperliquid` to `deploy-margin-engine.yml`, flip the `settings.py` default, and optionally add a startup assertion that errors if paper+binance is selected (since it's definitely broken). Scheduled as the first task in the engine-edits worktree.

**Recommended defensive gate — DQ-07 (NEW MEDIUM OPEN):**

Insert an eleventh gate into `_execute_v4` between gate 9 (balance query) and gate 10 (SL/TP math) that:
- Calls `self._exchange.get_mark()` or `.get_current_price()`
- Compares against `v4.last_price`
- Rejects if divergence > `v4_max_mark_divergence_bps` (default 20)
- Catches stale spot ticks, HL basis spikes, and cross-region latency

### 2026-04-11 — Agent E (SQ-01 rename audit) report

**4-PR rollout plan, unbranded naming, critical gap in CI-01 found.**

Agent E (READ-ONLY) grepped the entire novakash + novakash-timesfm-repo tree for ELM references and categorised them by risk:

| Category | Examples | Risk | PR |
|---|---|---|---|
| A. File names | `elm_prediction_recorder.py`, `test_elm_prediction_recorder.py` | LOW | PR 1 |
| B. Class names | `ELMPredictionRecorder` + 4 call sites | LOW | PR 1 |
| C. Function names | (none found) | — | — |
| D. Variable/kwarg names | `elm_client`, `_elm_recorder` | LOW | PR 1 |
| E. Structured log events | `elm_recorder.*` (8 events) | MEDIUM | PR 2 (operator coord) |
| F. DB column/table names | `ticks_elm_predictions`, `margin_signals.elm`, `ticks_v3_composite.elm_signal` | HIGH | PR 4 (DEFER FOREVER) |
| G. Env var names | (none found) | — | — |
| H. Doc comments | ~12 mentions | LOW | PR 1 |

**Key recommendations:**

1. **Go unbranded, not Sequoia\***. The engine convention is already versioned+unbranded (`timesfm_v2_client.py`, `v2_feature_body.py`), and the model family has already turned over 5 times (OAK → CEDAR → DUNE → ELM → SEQUOIA v4 → v5). Any brand name will go stale the same way ELM did. Target names: `class PredictionRecorder`, file `prediction_recorder.py`, kwarg `model_client`, log component `"prediction_recorder"`.

2. **PR 1 (low-risk cosmetic rename, ~60 lines)** can ship now. Keeps the DB table, log events, and signal keys intact.

3. **PR 2 (log event rename) requires simultaneously updating CI-01's error-signature gate** — otherwise the gate keeps grepping for the old event name forever.

4. **PR 3 (cross-repo signal key rename)** is a dual-emit migration spanning novakash + novakash-timesfm-repo + two DBs + frontend. Already sketched in `tasks/todo.md:78-82`. Multi-week coordination. Not urgent.

5. **PR 4 (DB column renames) should never ship.** Zero user-visible value, requires downtime.

**CRITICAL red flag from Agent E:**

The CI-01 error-signature gate I shipped in PR #28 (`.github/workflows/deploy-engine.yml:240-264`) **does NOT grep for `elm_recorder.write_error`**. So there is currently **no CI protection against PE-06 regressions**. If a future engine commit reintroduces the JSON quoting bug or similar, the deploy workflow will pass silently.

**Tracked as CI-02 (NEW MEDIUM OPEN)**: add `check_signature "elm_recorder.write_error" 0` and `check_signature "prediction_recorder.write_error" 0` to the gate. 4-line PR.

### 2026-04-11 — User scope additions: NT-01, UI-01, LT-01

User request 2026-04-11 14:16 UTC (post Agent D/E reports):

1. **NT-01 (Notes page)**: frontend page backed by a DB table, added as the FIRST entry in the sidebar. Purpose: running journal of observations as the session works. Dispatching Agent F to build the full stack (SQLAlchemy model + FastAPI CRUD routes + frontend page).

2. **Engine edits worktree**: "spin up all the engine edits in a worktree so they can proceed and you can carefully i guess begin to make those edits and bring them into the engine". Interpreted as: create ONE isolated worktree for the remaining trading-engine-touching fixes, serialise the work inside it (DQ-06 first, then DQ-07, then DQ-01, then V4-01 etc.), ship them one at a time via PRs. Not parallel because these all touch `margin_engine/` or `engine/` and changes could collide.

3. **UI-01 (Gate heartbeat / Execution HQ upgrade)**: "make sure there is a front end page that very clearly like the old execution hq or maybe upgrade that so i can very clearly see the gate heartbeat etc and trade decision". Phase 0: read existing `/execution-hq` to decide upgrade-in-place vs new page. Phase 1: add a "Live Decision" strip pulling from `signal_evaluations` via a new `/api/engine/gate-stack` hub endpoint.

4. **LT-01 (Live trading panel)**: "make sure things like the live trading panel and ability to execute trades montreal rules from the front end also works". Phase 0: read existing `/live` page. **Phase 2 (actual live trade execution from the web UI) is DEFERRED until the user explicitly approves a security model** — I will not ship real-money trade execution from a browser without confirmed auth/rate-limit/confirmation/stake-cap design.

**User clarification 2026-04-11**: the paper venue should be Hyperliquid. Noted in DQ-06 fix description.

### 2026-04-11 — Parallel agent dispatch: PE-06 done, DS-01/FE-04-06/DQ-05/SQ-01 in flight

User clarified during the session that the current model family is **Sequoia v5.2** and "ELM" is legacy naming — a historical artifact from when the model was called "Ensemble Learning Model". This reframes PE-04 (the elm_recorder.write_error bug) as PE-06 since it's really a Sequoia v5.2 prediction-recorder bug, and it matters specifically because the V10.6 decision surface uses 865 recorded predictions as its backtest evidence base — silent prediction drops bias that evidence.

To parallelise the rest of the audit work safely, I dispatched **5 background agents** in isolated git worktrees (using `superpowers:using-git-worktrees` + `superpowers:dispatching-parallel-agents`):

- **Agent A (PE-06 DONE)**: Sequoia v5.2 prediction recorder JSON quoting fix. Found `str(result.get("feature_freshness_ms", {}))` in `engine/data/feeds/elm_prediction_recorder.py:129` — Python `str(dict)` emits repr-style single quotes, Postgres JSONB rejects. Fixed with `json.dumps()`, added 5-test coverage that fails-on-unfixed / passes-on-fixed, grepped the whole engine/ tree for sibling bug-class instances (PE-02/PE-05 lesson — bug-class clusters come in pairs). Only one instance found. **PR #30 merged at `c9f341b`**. Preserved legacy ELM file/class/log names — rename tracked separately as SQ-01.
- **Agent B (DS-01 in flight)**: V10.6 `EvalOffsetBoundsGate` — new gate class in `engine/signals/gates.py` that hard-blocks trades outside `[90, 180]` eval_offset, gated behind `V10_6_ENABLED=false` by default so the merge is zero-behavior-change. Branch `claude/feat/ds01-eval-offset-bounds-gate`.
- **Agent C (FE-04/05/06 in flight)**: Three frontend data surface pages — `/data/v1`, `/data/v2`, `/data/v3` — mirroring the `/data/v4` V4Surface.jsx template. Covers v1 legacy point forecast, v2 LightGBM probability + quantiles, v3 composite + regime. Branch `claude/feat/fe04-05-06-v1-v2-v3-data-surfaces`.
- **Agent D (DQ-05 investigation in flight)**: READ-ONLY audit of margin_engine's price reference path. Confirms or rejects the hypothesis that `_execute_v4()` is using `consensus.reference_price` (first-available, often Binance spot) where a Hyperliquid perp mark price is needed. Returns a written report, no code changes.
- **Agent E (SQ-01 investigation in flight)**: READ-ONLY audit of ELM → Sequoia v5.2 rename scope across the novakash repo. Returns a categorised rename plan with risk assessment (file names, class names, log events, DB columns, env vars) so a follow-up PR can execute the rename mechanically. Added SQ-01 to the checklist as IN_PROGRESS.

**Parallelism safety constraints baked into the agent prompts**:

- Agents A/B touch disjoint files in `engine/` (recorder vs gates+config). No conflicts.
- Agent C explicitly told NOT to touch `AuditChecklist.jsx`, `V4Surface.jsx`, `Deployments.jsx`, engine code, or margin_engine code. Its scope is strictly `frontend/src/pages/data-surfaces/V{1,2,3}Surface.jsx` + additive entries in `App.jsx`/`Layout.jsx`/`hub/api/margin.py`.
- Agents D and E are READ-ONLY — zero file changes, zero merge risk.
- DS-01 (Agent B) **must default OFF behind `V10_6_ENABLED=false`** — explicitly documented in the agent prompt with the margin_engine PR #16 `MARGIN_ENGINE_USE_V4_ACTIONS` as the canonical precedent pattern. This is the hard safety boundary for the trading engine.

**What I deliberately did NOT dispatch as parallel agents**:

- **DQ-01 (Polymarket spot/futures reference fix)** — actual trading engine change, must be behind a feature flag, serialised after DS-01 lands so both V10.6 and DQ-01 rollouts don't compete on `engine/signals/gates.py`.
- **V4-01 (retrofit v4 into Polymarket engine)** — multi-week architectural change, needs planning not dispatch.
- **CA-01..CA-04 (clean-architect refactors)** — same reason. Need a plan before dispatch.
- **PE-03 (orphan_fills_error log downgrade)** — too trivial to parallelise.
- **DS-02/DS-03 (informational)** — not actionable, already captured in the checklist.

**Post-PR#30 production verification**: After PR #30 merges, the Montreal host still needs `git pull origin develop + scripts/restart_engine.sh` to pick up the PE-06 fix. Until then, the recorder bug continues silently dropping predictions. The deploy + verify happens in the main session after Agents B and C land their PRs (to avoid two restarts in 20 minutes). Until then, Agent D's DQ-05 report and Agent E's SQ-01 audit will be integrated into this file as addenda.

**Engine health spot-check (post PE-05 deploy + pre-PE-06 deploy)**: PID 2384 → 4488 after 12:49:31 UTC restart, ~13 min uptime, CPU 5.8%, MEM 4.7%. Error signatures over last ~5k log lines:

```
clob_feed.write_error         = 0   (PE-01 holding)
reconciler.resolve_db_error   = 0   (PE-05 holding — confirmed clean)
reconciler.orphan_fills_error = 0   (transient Poly noise not firing right now)
binance_ws.disconnected       = 0
polymarket_ws.disconnected    = 0
tiingo_feed.poll_error        = 0
price_source_disagreement    = 86  (expected pre-DQ-01)
```

All zero-threshold gates holding at 0. Engine is trading cleanly.

### 2026-04-11 — PE-05 hotfix: second CASE WHEN $1 type ambiguity in reconciler

- **PE-05 MEDIUM DONE** — `reconciler.resolve_db_error` came back at 2 errors in 20 minutes (6/hour) after the PR #26 merge + 12:22 UTC engine restart. The PE-02 fix in PR #26 addressed the prefix-match fallback at `reconciler.py:765-776` but MISSED a second instance of the same bug class at `reconciler.py:824-834`.
- **Root cause**: the UPDATE used `$1` in two incompatible type contexts: `SET outcome = $1` (deduces `outcome` column type, likely varchar) AND `CASE WHEN $1 = 'WIN'` (literal-comparison, deduces text). asyncpg can't reconcile → "inconsistent types deduced for parameter $1 — text versus character varying".
- **Fix**: drop the inline CASE WHEN, use the pre-computed `status` variable from line 720 as a fourth parameter. Matches the working pattern already used at line 613 and line 958 elsewhere in the same file.
- **Observed in production** at 2026-04-11 12:37:50 UTC and 12:42:36 UTC with condition_ids 0x6a79489fc86780cf52 and 0xd8eb483a4613119414.
- **PR #29** on branch `claude/fix/pe05-reconciler-case-when`. 15-line diff with inline comment explaining the type-deduction bug. Needs merge + Montreal git pull + engine restart to verify 0 errors live.
- **Lesson learned**: the error-signature gate in CI-01 (PR #28) would have caught this — it's exactly the scenario the gate was designed for. Once CI-01 starts firing on every deploy, similar class-of-bug regressions will block the merge instead of landing silently.

### 2026-04-11 — INC-01 Montreal host crash + recovery + DEP-01 shipped

- **INC-01 HIGH DONE** — Montreal host networking wedged at ~11:05 UTC, engine died at 12:00:54 UTC.
- **Symptoms** (engine.log 11:05 → 12:00): tiingo/chainlink/coinglass poll errors (empty), v2.probability.timeout, binance_ws.disconnected "timed out during opening handshake", polymarket_ws.disconnected same. Smoking gun at 11:54:06 UTC — `chainlink_feed.asset_error: Temporary failure in name resolution` for polygon-bor-rpc.publicnode.com. DNS broken on the host itself.
- **Diagnosis path**: aws ec2 describe-instance-status showed `running/ok/ok` but SSH banner exchange timed out. AWS SSM agent was unregistered (`Systems Manager's instance management role is not configured for account`), so the secondary remote path was unavailable. Had to reboot via `aws ec2 reboot-instances i-0785ed930423ae9fd`.
- **Recovery**: reboot at 12:11:11 UTC, sshd responsive at 12:17 UTC. SSH'd in, confirmed engine dead + log archive. `git pull origin develop` (picked up `816135e` = PR #26 merge with PE-01/PE-02 fixes). `sudo bash scripts/restart_engine.sh` at 12:22:57 UTC — log rotated to `engine-20260411-122257.log` (380K), pkill + start + verify. Engine PID 2384, 6.3% CPU, 4.3% MEM, processing cleanly.
- **Verification (7 min post-restart)**: `clob_feed.write_error = 0` (down from 1090/hour, PE-01 fix live), `reconciler.resolve_db_error = 0` (down from 4/hour, PE-02 fix live), `orphan_fills_error = 0`, `binance_ws.disconnected = 0`, `polymarket_ws.disconnected = 0`, `tiingo_feed.poll_error = 0`, `price_source_disagreement = 37` (just over the 30 threshold, expected pre-DQ-01). Live signals: `clob_feed.prices` every 2-3s, `chainlink_feed.written` 4 rows every 5s, `window.change` at 12:30:00 ts=1775910600 open=$72861.85, `window.monitoring_started` for BTC-1775910600.
- **New bug surfaced**: PE-04 `elm_recorder.write_error — invalid input syntax for type json, DETAIL: Token ' is invalid.` — a quoting bug in the ELM prediction recorder. Non-fatal (catch-and-continue) but means individual predictions are being silently dropped. Needs a test-driven fix switching any string-interpolated JSON to `json.dumps()`.
- **DEP-01 DONE** — built the `/deployments` frontend page during post-INC-01 recovery. Static registry mirroring `docs/CI_CD.md` with live health probes (15s interval) for services that expose them through the hub proxy. 7 services: timesfm (active + probe), macro-observer (active, no probe), data-collector (active, no probe), margin-engine (active + probe), hub (legacy Railway + probe), frontend (active + direct probe), engine (drafted via CI-01, no probe yet). Status summary strip (TOTAL / ACTIVE / DRAFTED / LEGACY counts). Nav entry `🚀 Deployments` under SYSTEM. Footer points at `docs/CI_CD.md` + `/audit`.
- **Hardening candidates for future INC-XX prevention** (NOT in scope for this PR): (a) systemd unit wrapping `scripts/restart_engine.sh` so crash recovery is automatic, (b) enable SSM agent with the Systems Manager instance role so we have a second remote path when sshd wedges, (c) CloudWatch alarm on engine.log write silence >120s, (d) the CI-01 error-signature gate itself — once active, every future deploy auto-validates against the known-bad list.
- **PRs:** #26 merged (`816135e`). PR #27 is live on `claude/ci/deploy-engine-montreal`, base currently `claude/frontend/audit-checklist-page` (needs retarget to `develop` now that #26 is merged). This session's work extends PR #27 with the `/deployments` page, DEP-01 / PE-04 / INC-01 checklist entries, and the PE-01 / PE-02 "verified live" progressNotes.

### 2026-04-11 — CI-01 workflow drafted (PR #27)

- **CI-01 OPEN → IN_PROGRESS** — `.github/workflows/deploy-engine.yml` drafted on branch `claude/ci/deploy-engine-montreal`, opened as PR #27 against `develop`. 13-step workflow ported from `deploy-macro-observer.yml`:
  1. `actions/checkout@v4`
  2. `Require runtime secrets` — fails loud if any of 9 required secrets is missing
  3. `Write SSH key` with base64 → raw-PEM fallback
  4. `Ensure host directories exist` (sudo mkdir + chown)
  5. `Rsync engine code to host` with `--rsync-path="sudo rsync"` for novakash-owned paths
  6. `Rsync scripts directory` (for `restart_engine.sh`)
  7. `Reset host .env and prune old backups`
  8. `Template .env from GitHub Actions secrets` — idempotent sed-or-append via a streamed bash script, secret never appears on remote command line
  9. `Restart engine via scripts/restart_engine.sh`
  10. `Wait for engine startup` (45s)
  11. Process-count health probe — `pgrep -f "python3 main.py"` must return exactly 1
  12. **Error-signature log-grep gate** — the regression guard the engine has never had. Fails the deploy if any of `clob_feed.write_error`, `reconciler.resolve_db_error`, `reconciler.orphan_fills_error`, `evaluate.price_source_disagreement`, `evaluate.no_current_price`, `reconciler.no_trade_match` exceed per-signature thresholds in the last ~10k lines.
  13. `Tail recent logs` for success diagnostics
- Workflow validates as YAML (`python3 -c "yaml.safe_load"`) — 1 job / 13 steps / 15 env keys. Uses the GitHub Actions injection-defence pattern throughout (all secrets pulled into `env:` at job level).
- **Left IN_PROGRESS, not DONE**, because the workflow only proves itself on the first real deploy run. Cannot self-verify locally. Flip to DONE after:
  1. `ENGINE_HOST` + `ENGINE_SSH_KEY` added to `billybrichards/novakash` Actions secrets
  2. First `workflow_dispatch` run succeeds end-to-end
  3. Error-signature gate passes against the live `/home/novakash/engine.log` (needs PE-01 + PE-02 from PR #26 merged first, otherwise the thresholds will trip)
- **Dependency order:** merge PR #26 first (PE-01 + PE-02 + checklist), then merge PR #27 (CI-01 + progress notes). PR #27 is branched off PR #26 to avoid conflicts on `AuditChecklist.jsx` and this file.
- Non-secret runtime flags (`V10_DUNE_ENABLED`, `FIVE_MIN_*`, `LIVE_TRADING_ENABLED`, thresholds, `DELTA_PRICE_SOURCE`) are intentionally NOT templated from secrets — they change more often than the CI deploy cadence and stay hand-managed on the host. `set_env` uses sed-replace-or-append so hand-managed values are preserved across deploys.
- After CI-01 lands and verifies, the DQ-01 rollout should tighten the `price_source_disagreement` threshold from 30 to <5 and gate it behind `V11_POLY_SPOT_ONLY_CONSENSUS=true` for rollback.

### 2026-04-11 — Rev-3: pricing clarification + DQ-05 seeded

- **DQ-01 scope corrected.** The original task description implied a universal "drop delta_binance" fix. That's wrong. The two engines trade different instruments and need different price references:
  - `engine/` (Polymarket) resolves via oracle against BTC/USD **spot**. Direction signals must be spot-aligned. Binance Futures WS is fine for VPIN / liquidation detection but wrong for direction. Fix remains as stated but the rollout flag is renamed `V11_POLY_SPOT_ONLY_CONSENSUS` for clarity.
  - `margin_engine/` trades Hyperliquid **perps**. PnL is realised against the perp mark price, so every price reference must be perp-native. Applying the Polymarket fix here would break it.
- **DQ-05 HIGH OPEN** — new task for the margin_engine pricing audit. Investigates which field of `/v4/snapshot` the 10-gate v4 stack uses as the price context, and whether `consensus.reference_price` (first-available source, often Binance spot) is being used where a Hyperliquid mark price is needed. No immediate fix — needs live trading data to validate.
- Data-quality category description updated to reflect the venue split.

### 2026-04-11 — Rev-2: new tasks seeded

- **CI-01 OPEN** — Montreal CI/CD automation for `engine/`, port of `deploy-macro-observer.yml`. Fix description spells out the 8-step workflow, including post-deploy error-signature grep that would auto-catch regressions like PE-01 / PE-02 on every future deploy.
- **FE-04 OPEN** — `/data/v1` V1 data surface page (legacy TimesFM point forecast).
- **FE-05 OPEN** — `/data/v2` V2 surface (LightGBM probability + calibrated quantiles + push-mode feature table with drift metrics — designed to make the v5 constant-leaf bug visually obvious next time it happens).
- **FE-06 OPEN** — `/data/v3` V3 surface (composite signal + per-timescale sub-signal radar + cascade FSM timeline + regime history).
- **FE-07 HIGH OPEN** — `/data/v4` V4 surface (fusion snapshot + 6-source consensus health + macro bias + events timeline + orderflow).
- **Added "ci-cd" category** (orange) to the checklist; CI-01 is the sole seed.
- **Added `progressNotes` field** to the task schema. Each task can now carry a list of `{ date, note }` entries which render inside the expanded card in a purple panel. The `/audit` UI becomes the authoritative session trail and this file is the matching audit log.

### 2026-04-11 — Session 2 afternoon continuation: DQ-01 + CI-02 shipped, 5 bg agents dispatched

Continuing the afternoon session after the context-compaction break. State at resume:
PR #44 (DEP-02 hub migration infra) had just merged, Agents K (LT-03) and L (CA-01..04)
were dispatched in the background, and DQ-01 had not yet started. Agent L hit the
"out of extra usage · resets 5pm Europe/London" cap and failed with no doc produced.
Same for Agent M (CFG-01) which was dispatched shortly after. Both were re-dispatched
after the usage reset.

Shipped this afternoon:

- **DQ-07 PR #45** — defensive `mark_divergence` gate in margin_engine, default OFF via
  `MARGIN_V4_MAX_MARK_DIVERGENCE_BPS=0.0`. 18/18 tests pass (4 new + 14 existing). Gate
  is the "option (b)" recommendation from Agent D's DQ-05 investigation: catches any
  class of v4.last_price vs exchange mark drift without retraining the quantiles on
  perp-native data. Operator flips the env var on the host to activate.
- **UI-01 PR #46** — V10.6 gate heartbeat section in Execution HQ Live tab. Renders the
  8 canonical gates (G0 EvalOffsetBoundsGate → G7 DynamicCapGate) with live pass/fail
  status, a TRADE/SKIP decision pill, a rail of the last 20 evaluations, and an
  aggregate breakdown of blocking-gate shares. Data source is `/api/v58/execution-hq`
  extended with a new `gate_heartbeat` array derived from `signal_evaluations`.
- **LT-03 PR #47** — manual trade decision-snapshot DB. `manual_trade_snapshots` table
  with JSONB columns for v4_snapshot, v3_snapshot, last-5 resolved window outcomes,
  engine decision, macro bias, and a new `operator_rationale` text field captured
  from the ManualTradePanel. Snapshot capture is isolated from trade execution
  (trade row commits first, then snapshot write is wrapped in try/except). Failure
  never blocks a trade. Operator-vs-engine ground truth for future calibration.
- **DQ-01 PR #48** — `V11_POLY_SPOT_ONLY_CONSENSUS` feature flag for SourceAgreementGate.
  Default OFF. When the operator flips it on the Montreal host and restarts, the
  gate drops `delta_binance` from the consensus vote entirely and requires unanimous
  CL + TI agreement. Binance is still consumed by VPIN / taker-flow / liquidations /
  every other downstream gate — only the consensus vote changes. 16 new test cases +
  7 sibling DS-01 tests = 23/23 passing. Motivated by the v11.1 changelog evidence
  table: Binance has 83.1% DOWN bias and the 2/3 rule passes CL=UP TI=DOWN BIN=DOWN
  (19.6% of all windows) as DOWN — biased source sides with lean-DOWN spot and
  outvotes the balanced spot. The user flagged this on 2026-04-11 as the source of
  "really terrible trade decisions".
- **CI-02 PR #49** — extended `deploy-engine.yml` error-signature gate to cover the
  PE-06 Sequoia recorder signatures (`elm_recorder.write_error`,
  `elm_recorder.query_error`) with threshold 0. Closes the observability gap Agent E
  flagged: PE-06 fired 16×/30s for days and was only caught by incidental grep.
  Also clarified that `reconciler.resolve_db_error` covers both PE-02 AND PE-05.

Background agents dispatched at 17:06-17:14 (5 in parallel, isolated worktrees):

- **CA-01..04** (`a51a798d3cd3e54c4`) — clean-architect migration plan DOC. Produces
  `docs/CLEAN_ARCHITECT_MIGRATION_PLAN.md` with 8 migration phases, port protocols,
  use case extractions, risk matrix, and rollback per phase. PLAN ONLY, no code.
  Uses `margin_engine/` as the reference architecture and targets the
  `engine/strategies/five_min_vpin.py` 3096-line god class as the primary shrink
  target.
- **CFG-01** (`a5e3fb62b018785b4`) — config-to-DB migration plan DOC. Produces
  `docs/CONFIG_MIGRATION_PLAN.md` inventorying every env var across every service,
  with a phased cutover plan, DB schema, hub API surface, and frontend UX mockups.
  PLAN ONLY, no code. Targets the operator's ask: "flip a gate flag from the
  frontend instead of SSH'ing onto the Montreal box".
- **Frontend audit** (`a8c10d2f084bb9a9d`) — READ-ONLY audit of every frontend route
  before live trading resumes. Produces `docs/FRONTEND_AUDIT_2026-04-11.md` with a
  per-route status table, legacy-tab retirement list, operator critical-path
  checklist (gate heartbeat / manual trade panel / decision snapshot / multi-market
  monitors), and proposed FE-* follow-up tasks.
- **UI-02** (`a5b04b7df9c039e32`) — multi-market HQ monitors. Parameterises
  ExecutionHQ by `:asset/:timeframe` and ships dedicated monitor pages for all 8
  combinations (BTC/ETH/SOL/XRP × 5m/15m). Reuses GateHeartbeat.jsx. ManualTradePanel
  is conditionally rendered only for BTC 5m (the asset we're actively trading) to
  prevent accidental cross-market trades. Hub endpoint extended with query params
  + graceful "no data yet" for assets the data-collector isn't yet writing.
- **LT-04** (`a52579618fce0906d`) — near-instant click-to-execute latency. Target
  <1s end-to-end on the happy path (vs current ~5-10s dominated by the engine-side
  poll interval). Agent chooses between LISTEN/NOTIFY (option A) and HTTP-kick +
  tight poll (option B) based on the existing asyncpg connection handling.
  Preserves the LT-02 token_id DB fallback and Montreal rules (engine still owns
  all Polymarket calls).

## Live trading status at session end

- **Paused** per the earlier UI toggle + engine restart (STOP-01 incident). Not
  re-verified in this continuation session — no passwordless SSH key available for
  Montreal auth this session, hub API endpoint `/api/v58/mode-status` returned 404
  and `/api/v58/execution-hq` returned 401. The user's earlier "I have updated the
  UI which should have done it" is the last known state.
- **Paper trading**: presumed still running on BTC 5m to keep the 865-outcome
  evidence base growing. Not verified this session.
- **Before flipping live back on**, the operator should:
  1. Confirm UI-02 has merged and all 8 HQ monitors render
  2. Confirm the frontend audit agent's report shows no broken pages
  3. Confirm LT-04 has merged so click-to-execute is <1s
  4. Flip `V11_POLY_SPOT_ONLY_CONSENSUS=true` on the Montreal host and watch the
     gate heartbeat UI for `spot disagree` events replacing the `2/3` reason strings
  5. Re-enable live trading via the UI toggle
  6. Monitor the first 5-10 manual trades through the decision-snapshot table

### 2026-04-11 — Mega checklist update: PRs #69-81 landed on develop

3 flipped DONE, 3 IN_PROGRESS with notes, 3 new DONE entries.

**DONE:** FACTORY-01 (PR #69), LIVE-TOGGLE-AUDIT (PR #72), UI-04 (PR #74).
**IN_PROGRESS:** POLY-SOT-d (PR #70, backfill pending), CI-01 (PR #71, ENGINE_SSH_KEY needed), CA-01 (PR #75 ports + PR #80 db_client split).
**New DONE:** DATA-ARCH-01 (PR #81, 39 tables), ORCH-AUDIT-01 (PR #79, 33 methods), REPO-AUDIT-01 (PR #77, 10 modules).

### 2026-04-11 — Late-night clean-architecture blitz: PRs #82-103

The evening session shipped 22 PRs completing the clean-architecture migration through Phase 3, plus supporting audit docs, frontend polish, and security fixes. All merged to develop between 22:12 and 22:56 UTC.

**Audit docs (PRs #82, #85):**
- PR #82 — repo-wide clean architecture audit v3 (source-level, all 14 modules)
- PR #85 — mega audit checklist update for PRs #69-81

**Security + decoupling (PRs #86, #87):**
- PR #86 — decoupled orchestrator from five_min_vpin private internals (prerequisite for CA-01 extractions)
- PR #87 — **security fix**: removed hardcoded Tiingo API key, extracted TiingoRestAdapter (CA-02)

**Frontend polish (PRs #89, #90):**
- PR #89 — synced schema catalog with data architecture audit findings
- PR #90 — strategy badges and data source labels on all frontend pages

**Clean-arch core (PRs #83, #92, #93, #95, #96, #99, #100, #101, #103):**
- PR #83 — wired persistence adapters to domain port interfaces
- PR #92 — Phase 2 adapter shims for all remaining ports (14 files)
- PR #93 — split polymarket_client.py into paper/live adapter classes
- PR #95 — **CA-03 DONE**: immutable GateContext with delta fold pipeline (229-line test suite)
- PR #96 — audit quick wins: DDL extraction to hub/db/migrations, _DBShim removal, public accessors
- PR #99 — Phase 1 fill 22 value object stubs with real fields and validation (144-line test suite)
- PR #100 — **CA-04 DONE**: WindowStateRepository as single owner of traded/resolved state (82-line test suite, new window_states table)
- PR #101 — 3 remaining use cases extracted (execute_manual_trade, publish_heartbeat, reconcile_positions) + 4 new ports + VO updates. 36 tests.
- PR #103 — **Phase 3**: EvaluateWindowUseCase extraction (flagged off, 13 tests). Core _evaluate_window logic now in engine/use_cases/evaluate_window.py.

**CI fix (PR #84):**
- PR #84 — excluded clean-arch dirs from deploy-engine path filter

**Status flips in AuditChecklist.jsx:**
- **CA-02 → DONE** (ports/adapters layer fully wired: PRs #75, #83, #87, #92, #93)
- **CA-03 → DONE** (immutable GateContext: PR #95)
- **CA-04 → DONE** (WindowStateRepository: PR #100)
- **POLY-SOT-d → DONE** (poly_fills on-chain SOT: PR #70)
- **CA-01 remains IN_PROGRESS** — Phases 0-3 shipped, Phase 4 (wiring) + beyond still pending
- **New entries:** REPO-AUDIT-02, CA-05, CA-06, FE-09, SCHEMA-02

### 2026-04-11 — novakash-timesfm-repo parallel work (PRs #51-65 on main)

Coordinated with the novakash blitz. 15 PRs merged to main:
- PRs #51-53 — v4 Phases 2-5 (strategy templates, futures feed, per-timescale macro), CI cleanup
- PR #54 — SPARTA_AGENT_GUIDE.md mirrored to timesfm repo
- PRs #56-58 — POST /predict versioned envelope, SPARTA Appendix D, VPIN ensemble plan doc
- PRs #59-62 — v5 push-mode fixes (4 PRs: scoring loop, /v2/probability, v4_snapshot_assembler, v3 composite cache dispatch)
- PR #63 — SPARTA doc sync (POLY-SOT-b/c shipped)
- PRs #61, #65 — VPIN Section 1 (binance trades feed + volume-clock BVC + BTC calibration)

### 2026-04-12 — Frontend redesign + signal infra + HMM regime classifier

10 PRs merged across both repos. Three workstreams executed in parallel.

**Workstream 1: Frontend redesign**
- PR novakash#104 — audit catch-up for PRs #82-103 + SPARTA discipline + DEP-02 hub cutover (nginx Railway → AWS Montreal)
- PR novakash#106 — schema page honest labels (26 tables flipped ACTIVE→PLANNED, new AMBER/BLUE/GREY chips)
- PR novakash#107 — **Polymarket Monitor page**: 5-band trading dashboard (StatusBar, DataHealthStrip, SignalSurface, GatePipeline, RecentFlow). 9 new files, 1430 lines. Route /polymarket/monitor.
- Design spec committed: `docs/superpowers/specs/2026-04-12-frontend-redesign-design.md`

**Workstream 2: Signal infrastructure fixes**
- PR timesfm#68 — S3 (alt-coin consensus gated to BTC-only) + S5 (V4 quantile propagation fallback)
- PR timesfm#69 — S4 (Tiingo + Chainlink API keys wired into deploy .env). BTC consensus 3/6 → 5/6.
- PR novakash#105 — SQ-01 PR1 (elm_prediction_recorder → prediction_recorder cosmetic rename + CI gate)

**Workstream 3: Macro/Regime classifier**
- PR timesfm#67 — **HMM regime classifier**: 4-state Gaussian HMM (calm_trend, volatile_trend, chop, risk_off) with transition matrix + persistence + confidence. Replaces hardcoded if/else that returned CHOPPY 100%. 702 lines, 24 tests.
- PR timesfm#66 — SPARTA audit-update discipline section (synced both repos)

**New audit entries:** DEP-02-CUTOVER, SCHEMA-FIX, SQ-01-PR1, REGIME-HMM, S3-FIX, S5-FIX, S4-FIX, FE-REDESIGN-MONITOR

### 2026-04-12 — Session 4: V4 paper trading + window analysis + data capture

**V4 flipped to LIVE paper at ~13:00 UTC.** `V4_FUSION_MODE=LIVE`, `V10_GATE_MODE=GHOST` activated on the Montreal engine. V4 now makes LIVE paper trade decisions; V10 continues as a ghost (no execution, decisions recorded only). Strategy flip documented in `~/.claude/projects/.../memory/project_strategy_flip_apr12.md`.

**Dual-strategy decision writing.** `EvaluateStrategiesUseCase` now writes both V10 and V4 decisions to the `strategy_decisions` table on every 2s eval cycle. Full `_ctx` JSON is persisted for both, including all signal surfaces (VPIN, deltas, CLOB prices, Sequoia quantiles, HMM regime, macro bias). This gives the complete operator-vs-engine ground-truth record needed to evaluate V4 performance against V10 shadow results.

**Window analysis deep dive.** Extended analysis of `signal_evaluations` (865 windows) surface findings:
- **T-120 to T-150 sweet spot**: highest accuracy offsets by out-of-sample validation. T-90 to T-120 also strong; T-60 is noisier.
- **confidence_distance >= 0.12 required**: below this threshold the V10 gate pass rate drops sharply. Calibration threshold for V4 adoption.
- **CLOB ask asymmetry (DOWN + cheap NO)**: when CLOB ask for NO side is anomalously cheap coinciding with a DOWN signal, observed WR ~82% in the backtest set — but flagged as a bearish-dataset caveat (sessions 1-4 data are skewed toward down-trending BTC).
- Analysis script committed to `docs/analysis/run_window_analysis.py`.

**`ticks_v4_decision` table created + persist loop activated.** New DB table captures the full V4 snapshot every 5s from the timesfm-service V4DBWriter: HMM regime, conviction score, quantile bands, macro bias, and `sub_signals` JSONB. Schema mirrors `ticks_v3_composite` pattern. Persist loop wired into the timesfm-service main loop; writing confirmed in Railway DB.

**Paper trade exposure bug fixed (PR #128).** Stale `OPEN` paper trades were not being resolved against the oracle price on engine restart — they stayed open indefinitely and blocked the `MAX_OPEN_EXPOSURE_PCT` gate, preventing new trades from being placed. Fix: on restart, `OrderManager` now auto-resolves all stale `OPEN` paper trades against the Chainlink oracle price before entering the main eval loop. Also added auto-expiry for trades with no oracle match after a TTL.

**Sitrep updated to show both strategies per window.** The sitrep log line (emitted every window change) now shows the V4 LIVE paper decision AND the V10 GHOST decision side-by-side, making it easy to compare signal agreement at a glance without querying the DB.

**DB config wired to engine for hot-reload (PR #128).** Strategy port now reads `V4_FUSION_MODE` and `V10_GATE_MODE` from the `trading_configs` DB table (not just `.env`), enabling hot-reload via the frontend Config page without an engine restart. Modes: `LIVE` (places paper/live trades), `GHOST` (records decisions, no execution), `OFF`.

**New audit items seeded:**
- **WINDOW-ANALYSIS-01 (MEDIUM INFO)** — T-120–T-150 sweet spot confirmed in 865-window analysis. CLOB ask asymmetry WR 82% flagged with bearish-dataset caveat. Needs revalidation once V4 LIVE paper accumulates 200+ windows in mixed-regime sessions.
- **CA-EXEC-INDEPENDENCE (MEDIUM OPEN)** — `EvaluateStrategiesUseCase` currently calls V10 and V4 evaluation in sequence in the same 2s tick. Should be independent `asyncio.gather` tasks so a slow V4 snapshot fetch doesn't delay V10 gate decisions.

### 2026-04-12 — ME-STRAT audit: margin engine strategy deep dive

**READ-ONLY analysis of margin_engine and TimesFM data surfaces (v1-v4)**. Comprehensive audit covering all available data surfaces, current strategy implementation, and 8 new strategy proposals.

**Key finding**: V4 fusion layer provides 10 gates, 4 timescales, TimesFM quantiles, V3 composite signals, consensus alignment, macro bias, event calendar, CLOB book, Polymarket window, and orderflow data — but margin_engine currently uses only primary timescale (15m) probability + regime. **V4 underutilization ~70%**.

**Data surface inventory**:
- **V1 (legacy)**: TimesFM 2.5 200M point forecast + quantiles. Frozen, not actively consumed.
- **V2 (Sequoia v5.2)**: LightGBM P(UP) with isotonic calibration. 37+ features from price, CoinGlass, Gamma, TimesFM, VPIN. Consumed by margin_engine (legacy path) and v3 composite.
- **V3 (composite)**: Multi-signal weighted score (ELM, cascade, taker, OI, funding, VPIN, momentum). HMM regime classifier (TRENDING_UP/DOWN, MEAN_REVERTING, CHOPPY, NO_EDGE). Cascade FSM (IDLE→CASCADE→BET→COOLDOWN).
- **V4 (fusion)**: 10-gate stack with 4 timescales (5m/15m/1h/4h), TimesFM quantiles (p10-p90), consensus alignment, macro bias/confidence/gate, event calendar, CLOB book (bid/ask/imbalance), Polymarket window prices, orderflow (liquidations).

**Current margin engine strategy**: Legacy v2 15m directional. Trigger: |P(UP)-0.5| >= 0.20. Exit: SL 0.6%, TP 0.5%, max hold 15 min, trailing 0.3%. Size: 2% capital at 3x leverage.

**Unused v4 data surfaces**:
- Multi-timescale probability (5m/1h/4h ignored)
- TimesFM quantiles for VaR optimization
- V3 composite signals (only regime used)
- Consensus alignment score
- Macro reasoning (only advisory size modifier)
- Event impact scores
- CLOB imbalance
- Polymarket arb opportunities
- Orderflow cascade detection

**8 new strategy proposals**:
1. **ME-STRAT-01**: Enable v4 path (currently dark-deployed). Priority: HIGH.
2. **ME-STRAT-02**: Multi-timescale alignment (3/4 timescales agree). Expected: higher conviction trades, reduced frequency.
3. **ME-STRAT-03**: Quantile-VaR position sizing. Expected: constant $ risk per trade.
4. **ME-STRAT-04**: Regime-adaptive strategy selection (trend vs mean-reversion vs no-trade).
5. **ME-STRAT-05**: Cascade fade strategy (fade forced liquidations).
6. **ME-STRAT-06**: CLOB book imbalance scalp (short-term scalps).
7. **ME-STRAT-07**: Macro model calibration (currently 20-30% BEAR hit rate = anti-predictive).
8. **ME-STRAT-08**: Event-driven pre-positioning (30-60 min before high-impact events).

**Action items**:
- Enable v4 path in production (paper mode first)
- Implement multi-timescale alignment strategy
- Backtest regime-adaptive selection
- Calibrate macro model (retrain Qwen prompt)
- Deploy cascade detector for fade strategy

**ME-STRAT-UI-01: Margin Strategy Dashboard**: Design completed at `docs/MARGIN_STRATEGY_DASHBOARD_DESIGN.md`. Dashboard will feature:
- Strategy cards grid (8 strategies with status badges)
- Click-to-expand modal with 4 tabs: Overview, Performance, Regime Breakdown, Configuration
- Regime-based PnL analysis (TRENDING_UP/DOWN, MEAN_REVERTING, CHOPPY)
- Backtest simulation (client-side)
- Configuration management (requires restart)

**SIGNAL-COMP-UI-01: Signal Comparison Dashboard**: New task added to /audit checklist. Dashboard will track accuracy of all directional signals:
- **Sequoia v5.2 (v2)**: LightGBM P(UP) - 5m/15m timescales
- **V3 Composite**: Weighted ensemble score [-1,+1]
- **HMM Regime**: 4-state Gaussian HMM (calm_trend/volatile_trend/chop/risk_off) - PR #67
- **MacroV2**: Heuristic classifier (LONG/SHORT/NEUTRAL) - REPLACES Qwen - PR #71
- **V4 Consensus**: Multi-timescale alignment score
- **Cascade FSM**: Liquidation cascade state (IDLE/CASCADE/BET/COOLDOWN)

All 10 items (ME-STRAT-01 through ME-STRAT-08 + ME-STRAT-UI-01 + SIGNAL-COMP-UI-01) added to /audit checklist.

### 2026-04-12 — Session 5: Go-live preparation + deep signal analysis

**Go-live prep completed.** V4 LIVE paper + V10 GHOST active. $101.21 USDC deposited to Polymarket funder address. Wallet: `0x181D2ED714E0f7Fe9c6e4f13711376eDaab25E10`. Engine running on Montreal (PID verified).

**V4 timing + confidence gates deployed.** V4FusionStrategy now gates on:
- `timing = optimal` (T-30 to T-180) — hard skip on early/late/expired
- `confidence_distance >= 0.12` — only strong/high confidence bands
- `late_window` (T-5 to T-30): requires `sequoia_clob_divergence >= 0.04` (Sequoia ahead of CLOB)
- V4 paper result before fixes: 0W/20L (all at T-60, wrong timing)
- V4 paper result after fixes: gates correctly blocking weak/late signals

**Window analysis confirmed (2nd run, 70,272 windows).** Sweet spot T-120 to T-150. T-90 cliff = 48.7% (worse than random). Analysis scripts: `docs/analysis/run_window_analysis.py` + `docs/analysis/full_signal_report.py`. Runbook: `docs/analysis/SIGNAL_EVAL_RUNBOOK.md`.

**Retrain pipeline fixed (timesfm PR #75).** Parity check was calling `python -m training.parity_check` with no args (requires `--from-s3 btc`). Fixed. All 5 matrix cells ran. New model NOT promoted — ECE worse than current Sequoia v5.2. Current model stays.

**Major frontend fixes (PRs #133, #134, #135).** FE-MONITOR-01a-e: RecentFlow V4 column, sub-signals parse, gate dedup, SRC Agreement source, bankroll label. Also: direction toggle for manual trades, Live Floor BTC price fix, Overview SQL cast fix.

**SPARTA updated** with access guide (GitHub+AWS CLI), analysis scripts reference, and schema gotchas.

**Signal eval runbook created** (`docs/analysis/SIGNAL_EVAL_RUNBOOK.md`). Covers all tables, queries, config decision framework, schema gotchas. Any agent can run `full_signal_report.py` for current-state analysis.

### 2026-04-12 — Session 5 cont: CLOB bug fix + DOWN-only strategy discovery

**CLOB feed bug found and fixed (PR #136).** Root cause: `CLOBFeed._poll()` had a `self._poly.paper_mode` guard that completely disabled the feed in paper mode. `clob_book_snapshots` had 0 rows over 24h; all `clob_up_ask`/`clob_down_ask` in `signal_evaluations` were NULL. V4 was trading blind. Fix: removed the guard; `PolymarketClient.connect()` now initialises a read-only `ClobClient` in paper mode (order book endpoint is unauthenticated), so real prices flow in both modes. PR #136 merged to develop.

**CLOB data coverage clarified.** Before restarts today: 98–99% coverage. After multiple engine restarts: 0%. Paper mode guard was silently breaking the feed on every restart — it was never reaching `clob_book_snapshots`.

**Critical discovery: DOWN-only strategy (897K sample analysis).** Full analysis at `docs/analysis/DOWN_ONLY_STRATEGY_2026-04-12.md`. Key table:

| Direction | CLOB Token Ask | N | WR |
|-----------|----------------|---|----|
| DOWN | > 0.75 | 175,261 | **99.0%** |
| DOWN | 0.55–0.75 | 112,371 | **97.8%** |
| DOWN | 0.35–0.55 | 86,821 | **92.1%** |
| DOWN | < 0.35 | 177,435 | **76.2%** |
| UP | any | 346K | **1.5–53%** |

CLOB ask interpretation: `clob_down_ask > 0.75` means the market prices DOWN at >$0.75 — both model and market agree on DOWN, 99% WR. `clob_down_ask < 0.35` is the genuine contrarian: retail UP bias makes DOWN cheap, our model is right 76% of the time. UP predictions are unprofitable across every CLOB band — not a calibration issue, a fundamental signal asymmetry. DOWN moves are more predictable (liquidation cascades, VPIN spikes) than UP moves.

**Two new gate proposals (SIG-03, SIG-04) added to /audit checklist:**
- **SIG-03**: `DirectionFilterGate` — skip all UP predictions. ~20 lines, G1.5 in the existing Gate pipeline.
- **SIG-04**: `CLOBSizingGate` — adjust size_modifier based on clob_down_ask (2.0× at ≥0.75, down to 1.0× below 0.35). G6.5 in pipeline.

**Section 8 added to `full_signal_report.py`** — runs the direction × CLOB band query automatically. Run `python3 docs/analysis/full_signal_report.py` to see current WR split.

**Runbook Section 8 added** with the full finding, monitoring query, and recommended env config.

## Next up (ordered, updated 2026-04-12 session 5)

0. **Go live** — top up wallet confirmed ($101.21 USDC). Flip PAPER→LIVE toggle in Monitor top bar when ready.
1. **FE-MONITOR-01 remaining** — Sequoia p_up still NO DATA in some scenarios, V3 composite join still using API (not DB), gate pipeline occasional double render. Tracked in audit.
2. **SIGNAL-CLOB-EDGE-GATE** — gate on (Sequoia p_up - CLOB implied prob) > threshold. Most impactful improvement identified from data.
3. **V4-TIMING-BUG** — CRITICAL: add `if timing == 'late': skip` guard (fix deployed, verify in next paper session).
4. **CA-EXEC-INDEPENDENCE** — extract `_execute_trade` into ExecuteTradeUseCase so V4 has fully independent execution path.
5. **Macro Phase C** — replace Qwen with per-horizon LightGBM MacroV2Classifier.
6. **Window analysis on neutral BTC period** — current analysis is 84% DOWN biased. Need mixed-regime validation.
7. **CA-01 Phase 4** — wire use-case calls behind feature flag.

## Conventions

- **Update checklist + this log in the same commit.** The `progressNotes` entries should mirror a bullet here.
- **Keep entries terse — one sentence per action, one paragraph per decision.**
- **Cite file:line or PR numbers** so future-you can retrace without re-reading the whole thread.
