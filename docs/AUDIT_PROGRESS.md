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

## Next up (ordered)

1. **Build FE-07** (`/data/v4`) — highest leverage; the richest surface deserves the best UI and the existing `V4Panel.jsx` is 80% of the component library.
2. **Open PR for CI-01** once FE-07 lands — ~200-line YAML port of `deploy-macro-observer.yml`, zero runtime risk until it fires, deploys the Phase 0 fixes automatically on first run.
3. **DS-01 Phase 2a** — `EvalOffsetBoundsGate` (hardblock outside `[90, 180]`). Single new gate in `engine/signals/gates.py`, two env vars. Lowest-risk piece of V10.6.
4. **DQ-01** — drop `delta_binance` from the source-agreement consensus vote behind `V11_USE_SPOT_ONLY_CONSENSUS=true`. Deploy behind flag, monitor 4h, then make default.
5. **FE-04 / FE-05 / FE-06** — v1, v2, v3 data surfaces in order of descending diagnostic value.
6. **V4-01** — retrofit `V4SnapshotPort` into `engine/`. Path A (extend god class, fast) vs Path B (build new use case on `margin_engine/` substrate) still an open decision.

## Conventions

- **Update checklist + this log in the same commit.** The `progressNotes` entries should mirror a bullet here.
- **Keep entries terse — one sentence per action, one paragraph per decision.**
- **Cite file:line or PR numbers** so future-you can retrace without re-reading the whole thread.
