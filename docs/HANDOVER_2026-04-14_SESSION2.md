# Session 2 Handover — 2026-04-14

## What happened this session

Long debugging + go-live session. Engine went live briefly, placed 3 real trades (v4_down_only NO at $2.95 each), then back to paper due to bugs. Multiple fixes deployed. TimesFM keeps OOMing. Infrastructure split needed.

## Current State

- **Engine:** LIVE mode on Montreal (15.223.247.178), commit `c3b468b`
- **TimesFM:** DOWN again (container OOM killed, needs restart or infra split)
- **Wallet:** ~$57 USDC on Polymarket CLOB
- **3 real live trades:** ids 4463-4465, v4_down_only NO $2.95 each, status OPEN (awaiting resolution)
- **Paper reconciliation:** Working now (resolve_trade implemented)
- **Dedup:** Fixed (window_states table exists)

## Fixes Deployed Today (12 commits on develop)

| Commit | Fix |
|---|---|
| `4085b98` | Runtime caps + correct trade persistence |
| `ff07708` | Defer ExecuteTradeUseCase wiring until after DB init |
| `4291cb5` | Bankroll defaults $500→$29, max_position $500→$5, min_bet $2→$1 |
| `67e1d00` | **is_live=True bug** — fixed in db_client.write_trade (correct execution path) |
| `081670c` | Dynamic bankroll vars in CI (GitHub Actions Variables) |
| `6e24125` | Remove PAPER_MODE from CI .env (was resetting live mode on every deploy) |
| `0d3a8e4` | resolve_trade + window_states table creation |
| `61e2293` | Revert window_states startup call (was blocking engine startup) |
| `bb3f7f8` | Stake calc uses runtime.bet_fraction, not hardcoded 0.025 |
| `a4180be` | runtime.bet_fraction used unconditionally (YAML fraction no longer overrides) |
| `6cd16f7` | v4_up_basic min_dist 0.10→0.15 + analysis docs + architecture roadmap |
| `c3b468b` | Clearer Telegram window summary format (model line + strategy decisions) |

## Infrastructure — URGENT

### Problem
3.98.114.0 runs TimesFM + Hub + data-collector + macro-observer on a c6a.xlarge (8GB). TimesFM ML model needs 5GB+ and keeps OOMing the container. Memory limits (5GB cap) prevent full instance OOM but the container dies and doesn't reliably restart. The engine loses v2_probability + v4 snapshot → all strategies skip.

### Plan: Split into 2 boxes
Full plan at: `docs/superpowers/plans/2026-04-14-infra-split.md`

```
NEW: c6a.2xlarge (16GB) — TimesFM only, with Elastic IP
NEW: t3.medium (4GB) — Hub + data-collector + macro-observer, with Elastic IP  
KEEP: 15.223.247.178 — Engine (unchanged)
```

**Elastic IPs** prevent the TIMESFM_URL drift problem we hit today.

### Steps
1. Provision 2 new EC2 instances in ca-central-1
2. Set up TimesFM on 16GB box with docker + swap
3. Set up Hub on 4GB box with docker
4. Update CI/CD workflows (deploy-hub.yml, deploy-engine.yml)
5. Update GitHub Secrets (HUB_HOST, TIMESFM_HOST, SSH keys)
6. Update Montreal engine .env TIMESFM_URL
7. Verify end-to-end, decommission old box

### CI/CD Workflows to Update
- `.github/workflows/deploy-hub.yml` — `HUB_HOST` secret → new Hub IP
- `.github/workflows/deploy-engine.yml` — `TIMESFM_URL` in .env template → new TimesFM IP
- GitHub Secrets: `HUB_HOST`, `HUB_SSH_KEY`, `HUB_URL`, `TIMESFM_HOST`

## Agent Ops System — NEXT AFTER INFRA

Full plan at: `docs/superpowers/plans/2026-04-14-agent-ops-command-center.md` (updated with Claude Agent SDK approach)

**Architecture:** Hub endpoint spawns Claude Agent SDK agents with custom MCP tools (DB query, code analysis). Agents can read code, query DB, make edits, and open PRs.

**Agent types:**
- System Health (Bash + logs)
- Trade Analysis (DB query)
- Signal Quality (DB query)
- Clean Architect (Read + Edit + Bash → opens PRs)
- Error Analyzer (logs + DB → opens fix PRs)
- Data Surface Audit (DB query)
- Frontend Fixer (Read + Edit → opens PRs)
- Sitrep (spawns all above)

**Frontend:** `/ops` page with button grid + report cards with PR links.

**Requires:** `pip install claude-agent-sdk` on Hub, `ANTHROPIC_API_KEY` (already set).

## Telegram Dashboard — AFTER AGENT OPS

Full plan at: `docs/superpowers/plans/2026-04-14-telegram-dashboard.md`

New `/telegram` page reading from existing `telegram_notifications` table. Color-coded cards, type filters, auto-refresh.

## Strategy Analysis Findings (saved to memory + docs)

Source: `signal_evaluations` (111K rows), validated 2026-04-14

### Model accuracy at T-90..150

| Direction | Confidence | WR |
|---|---|---|
| DOWN STRONG (≥0.15) | 78.3% | 53,845 evals |
| DOWN MODERATE (0.12-0.15) | 99.0% | 9,311 evals |
| UP STRONG (≥0.15) | **66.6%** | 19,315 evals |
| UP MODERATE (0.12-0.15) | **26.3%** | 8,003 evals ← anti-predictive |

### Config changes applied
- v4_up_basic: min_dist 0.10 → 0.15 (filters anti-predictive MODERATE band)
- v4_down_only: kept at 0.10 (strong across all bands)
- v4_up_asian: kept at 0.15-0.20 (already optimal)
- Stake calc: runtime.bet_fraction used unconditionally

### Analysis doc
`docs/analysis/SIGNAL_ANALYSIS_2026-04-14.md` — full breakdown

### Architecture improvements doc
`docs/analysis/ARCHITECTURE_IMPROVEMENTS_2026-04-14.md` — 8 priority items

## GitHub Actions Variables Set

| Variable | Value |
|---|---|
| STARTING_BANKROLL | 30 |
| BET_FRACTION | 0.07 |
| MAX_POSITION_USD | 5.0 |

## GitHub Secrets Added

| Secret | Purpose |
|---|---|
| HUB_URL | http://3.98.114.0:8091 (UPDATE after infra split) |
| HUB_ADMIN_USERNAME | billy |
| HUB_ADMIN_PASSWORD | novakash2026 |
| BUILDER_API_KEY | Polymarket redemption signing |
| BUILDER_SECRET | Polymarket redemption |
| BUILDER_PASSPHRASE | Polymarket redemption |

## DB Configs (Railway PostgreSQL)

### Live Config v7.1 (id=24)
- bankroll: 57.48
- bet_fraction: 0.07
- absolute_max_bet: 5.0

### Paper Config v7.1 (id=26)
- bankroll: 130.82 (stale — needs update to match wallet)
- bet_fraction: 0.07

## Lessons Learned Today

1. Engine startup takes 10-20s — don't assume hung at `db.connected`
2. PAPER_MODE in .env resets live mode on every deploy — removed
3. YAML `fraction: 0.025` overrides `runtime.bet_fraction` via `decision.collateral_pct` — fixed
4. `db_client.write_trade` was the actual is_live write path, not `pg_trade_repo` — fixed
5. `window_snapshots.v2_direction` is unreliable for accuracy analysis — always use `signal_evaluations`
6. TimesFM OOMs every few hours on 8GB box — needs 16GB dedicated

## Key File Locations

- Engine: `/home/novakash/novakash/engine/` on 15.223.247.178
- TimesFM: `/home/ubuntu/timesfm-service/` on 3.98.114.0
- Hub: Docker container on 3.98.114.0 (port 8091)
- Plans: `docs/superpowers/plans/2026-04-14-*.md`
- Analysis: `docs/analysis/SIGNAL_ANALYSIS_2026-04-14.md`
- Architecture: `docs/analysis/ARCHITECTURE_IMPROVEMENTS_2026-04-14.md`
- Memory: `~/.claude/projects/-Users-billyrichards-Code-novakash/memory/`
