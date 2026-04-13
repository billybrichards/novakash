# SPARTA Agent Guide & Analysis Runbook — Memory Memo

**Generated:** 2026-04-13  
**Primary Docs:** `docs/SPARTA_AGENT_GUIDE.md` (896 lines) + `docs/analysis/SIGNAL_EVAL_RUNBOOK.md` (678 lines)

---

## SPARTA Agent Guide — Executive Summary

**SPARTA** = "disciplined, minimal, and unforgiving of hand-waving" methodology for the novakash trading system

### The Three Laws (Mandatory)

1. **Engine is live and trading real money** — Every change must be either (a) behind a default-off feature flag (`V10_6_ENABLED`, `MARGIN_ENGINE_USE_V4_ACTIONS`), or (b) verified against a clean error-signature baseline in a CI deploy gate
2. **Evidence before assertions** — Run verification commands before claiming anything works. The `/audit` checklist enforces this — every `DONE` task has a dated `progressNotes` entry citing PR number, commit SHA, or log line
3. **Branch conventions are not optional** — 
   - `novakash` deploys from **`develop`** (main is audit/release)
   - `novakash-timesfm-repo` deploys from **`main`**

### Core Principles

**Audit checklist is the single source of truth:**
- Location: `frontend/src/pages/AuditChecklist.jsx` + `docs/AUDIT_PROGRESS.md`
- Every task has: `id` (e.g., `DQ-01`), `severity` (CRITICAL/HIGH/MEDIUM/LOW), `status` (OPEN/IN_PROGRESS/DONE/BLOCKED/INFO), `files[]` (file:line pointers), `evidence[]`, `fix`, `progressNotes[]`
- **MANDATORY discipline:** Every PR that ships code MUST include audit updates in the same commit
- Session start: Read `AUDIT_PROGRESS.md` and check for stale entries
- Session end (or every 5 PRs): Update all tasks, add dated section to AUDIT_PROGRESS.md

**Access Stack:**

| Component | Access Method | Key Details |
|-----------|--------------|-------------|
| **Frontend** | http://99.79.41.246 | Login: billy/novakash2026 |
| **Hub API** | http://3.98.114.0:8091 | **USE THIS, NOT RAILWAY** — JWT auth required |
| **TimesFM** | http://3.98.114.0:8080 | No auth: `/v4/snapshot`, `/v3/composite`, `/v2/probability` |
| **Database** | Railway PostgreSQL | `DATABASE_PUBLIC_URL` from Railway dashboard |
| **Engine (Montreal)** | EC2 Instance Connect | 60s key TTL: `i-0785ed930423ae9fd` @ 15.223.247.178 |

**EC2 Instance Connect (60s key):**
```bash
ssh-keygen -t ed25519 -f /tmp/ec2_temp_key -N "" -q
aws ec2-instance-connect send-ssh-public-key \
  --instance-id i-0785ed930423ae9fd \
  --instance-os-user ubuntu \
  --ssh-public-key file:///tmp/ec2_temp_key.pub \
  --availability-zone ca-central-1b \
  --region ca-central-1
ssh -i /tmp/ec2_temp_key -o StrictHostKeyChecking=no ubuntu@15.223.247.178
```

**Two-Repo Architecture:**

| Repo | Deploy Branch | Contains | Communication |
|------|--------------|----------|---------------|
| **novakash** | `develop` | `engine/`, `margin_engine/`, `hub/`, `frontend/`, `macro-observer/`, `data-collector/` | `TIMESFM_URL` env var → http://3.98.114.0:8080 |
| **novakash-timesfm-repo** | `main` | `app/` (model service), `training/` (auto-retrain) | Writes to shared Postgres: `ticks_v2_probability`, `ticks_v3_composite`, `ticks_v4_decision` |

**Cross-repo PR coordination:** timesfm-repo `main` → wait for CI → novakash `develop` (never backwards)

---

## Data Stack — Top to Bottom

| Layer | Location | Returns | Consumed By |
|-------|----------|---------|-------------|
| **v1 (legacy)** | timesfm-repo | TimesFM 2.5 200M forecast | Legacy widgets only |
| **v2 (LightGBM)** | timesfm-repo | `P(UP)` + quantile bands (Sequoia v5.2) | Engine, margin_engine |
| **v3 (composite)** | timesfm-repo | Multi-timescale score + regime/cascade FSM | Engine (cascade signals) |
| **v4 (fusion)** | timesfm-repo | 10-gate fusion: `recommended_action`, `consensus`, `macro`, `clob`, `regime` | margin_engine (gated), frontend |
| **signal_evaluations** | Engine | Per-eval row with all gate decisions + PnL | Auto-retrain, V10.6 evidence base |
| **macro_signals** | macro-observer | 60s Qwen 122B bias classifications | v4_macro_store |
| **clob_book_snapshots** | Engine | Polymarket CLOB book (PE-01: was 0% coverage for 4 days) | Polymarket engine |

**Critical venue distinction:**
- `engine/` (Polymarket) resolves against **BTC/USD spot** via oracle
- `margin_engine/` trades Hyperliquid perps → needs **perp/mark** references
- **DQ-01** = Polymarket-only fix, **DQ-05** = separate margin_engine audit

---

## Montreal Rules — Engine Production Access

**Instance:** `i-0785ed930423ae9fd` (15.223.247.178, ca-central-1b)  
**User:** `novakash` (app user), `ubuntu` (sudo access)  
**Layout:**
```
/home/novakash/novakash/
├── engine/
│   ├── main.py           # nohup process
│   └── .env              # runtime config (gitignored)
├── scripts/
│   └── restart_engine.sh # canonical restart + log rotation
/home/novakash/engine.log # live log (appended, never truncated)
```

**ALWAYS use `scripts/restart_engine.sh`** — never `nohup python3 main.py > engine.log` (truncates history)

**Restart script does:**
1. Fix permissions if crashed root
2. Rotate `engine.log` → `engine-YYYYMMDD-HHMMSS.log`
3. Prune archives beyond KEEP_N=20
4. Kill existing process, sleep 4s
5. Assert no stragglers
6. Start new process with `>>` append
7. Verify exactly 1 PID
8. Tail 10 lines for smoke check

**⚠️ Warning:** Restart resets in-memory state (VPIN buckets, WebSocket connections, orchestrator heartbeats). Plan for between-window moments.

---

## Engine Monitoring — Error-Signature Gate

**No default monitoring** — operators tail `engine.log` and grep for error signatures

**Canonical thresholds** (from `.github/workflows/deploy-engine.yml:211-264`):

| Signature | Threshold | Notes |
|-----------|-----------|-------|
| `clob_feed.write_error` | 0 | PE-01 fix — must stay 0 |
| `reconciler.resolve_db_error` | 0 | PE-02/PE-05 fix — must stay 0 |
| `reconciler.orphan_fills_error` | 5 | Transient Poly API noise |
| `evaluate.price_source_disagreement` | 30 | Pre-DQ-01; tighten to <5 after fix |
| `evaluate.no_current_price` | 2 | Cold-start tolerated |
| `reconciler.no_trade_match` | 5 | Orphan reconciler noise |

**Post-deploy health check:**
```bash
# SSH to Montreal
sudo tail -n 5000 /home/novakash/engine.log | grep -c "clob_feed.write_error"
sudo tail -n 5000 /home/novakash/engine.log | grep -c "reconciler.resolve_db_error"
pgrep -fa "python3 main.py"  # must be exactly 1
```

**Live write verification (run locally):**
```sql
SELECT COUNT(*) FROM signal_evaluations WHERE created_at > now() - interval '5 minutes';
SELECT COUNT(*) FROM ticks_clob WHERE ts > now() - interval '1 minute';
SELECT COUNT(*) FROM clob_book_snapshots WHERE ts > now() - interval '1 minute';
```

---

## CI/CD Conventions

**Service Deployment Map:**

| Service | Repo | Deploy Branch | Workflow | Fires On |
|---------|------|---------------|----------|----------|
| timesfm-service | novakash-timesfm-repo | main | ci.yml | push to main |
| engine (Polymarket) | novakash | develop | deploy-engine.yml | push to develop (path: engine/**) |
| hub (API) | novakash | develop | Railway auto-deploy | push to develop |
| frontend | novakash | develop | deploy-frontend.yml | push to develop (path: frontend/**) |
| macro-observer | novakash | develop | deploy-macro-observer.yml | push to develop (path: macro-observer/**) |
| margin-engine | novakash | develop | deploy-margin-engine.yml | push to develop (path: margin_engine/**) |

**Deploy workflow pattern (13 steps):**
1. actions/checkout@v4
2. Require runtime secrets (fail loud if missing)
3. Write SSH key + known_hosts
4. Ensure host directories exist
5. Rsync code to host (exclude .env, .git)
6. Rsync scripts/ (if needed)
7. Template .env from GitHub Actions secrets (streamed bash over stdin)
8. Rebuild/recreate (Docker, systemd, or restart_engine.sh)
9. Wait for startup (45-90s)
10. Health probe (process-level) — pgrep returns 1 PID
11. Health probe (log-level) — error-signature gate
12. Tail recent logs (success diagnostics)
13. HTTP probe against new endpoint (optional)

**Injection-defence pattern:** Pull secrets into job-level `env:` block, use plain bash variables (`$SSH_KEY`) in `run:` blocks — never `${{ secrets.X }}` directly inside `run:`

---

## Incident Log — What We've Broken and Fixed

| Incident | Status | Summary |
|----------|--------|---------|
| **INC-01** | DONE | Montreal host networking wedged, DNS broken, SSM unregistered. Reboot via AWS EC2. Lesson: enable SSM agent, add CloudWatch alarm on engine.log write silence > 120s |
| **PE-01** | DONE | `clob_book_snapshots` INSERT missing `ts` column → 1090 errors/hour, 4-day data gap. Fix: add `ts` first + `$11` parameter |
| **PE-02** | DONE | asyncpg type deduction bug in reconciler → 4 errors/hour. Fix: single `$1::text` parameter |
| **PE-05** | DONE | Same bug class as PE-02, 60 lines below. **Lesson: bug classes come in pairs** |
| **PE-06** | DONE | JSON quoting in prediction recorder → Postgres JSONB rejects. Fix: `json.dumps()` instead of `str(dict)` |
| **DQ-05** | DONE (investigation) | False alarm — margin_engine only uses `v4.last_price` as dimensionless ratio denominator |
| **DQ-06** | OPEN | `PaperExchangeAdapter` with no `price_getter` → frozen $80k constant. Paper venue should be Hyperliquid |
| **CI-01** | IN_PROGRESS | Error-signature gate for engine deploy workflow. Waiting on ENGINE_SSH_KEY + ENGINE_HOST secrets |

---

## SPARTA Methodology — How to Work

### 1. Read before write
- Read audit checklist entry for task
- Read `files[]` array (file:line pointers)
- Read `evidence[]` bullets (why task exists)
- Read `fix` paragraph (what success looks like)
- Grep for sibling bug-class instances (bug classes come in pairs)

**Never jump to Write/Edit without at least 3 Read calls first**

### 2. Dispatch parallel agents for independent work
**Safe:** Disjoint files, READ-ONLY investigations, additive-only surfaces  
**Unsafe:** Engine trading logic, architectural refactors, AuditChecklist.jsx edits

### 3. Every code change has a task ID
Commit message pattern: `<type>(<scope>): <task-id> — <one-line description>`
- `fix(engine): PE-06 — json.dumps for feature_freshness_ms`
- `ci: CI-01 — Montreal deploy workflow for engine/`

### 4. Every DONE task has a progressNotes entry
Format: `{ date: 'YYYY-MM-DD', note: 'PR #XX merged at SH. Fix verified by ...' }`

### 5. Default-off feature flag for any trading-logic change
- `V10_6_ENABLED` — master flag for V10.6 EvalOffsetBoundsGate
- `MARGIN_ENGINE_USE_V4_ACTIONS` — master flag for 10-gate v4 stack

### 6. Verify before claiming success
- Run tests (`pytest engine/tests/test_X.py -v`)
- Build frontend (`cd frontend && npm run build`)
- For engine work: SSH to Montreal, tail log, grep error signature
- Record what you observed in `progressNotes`

**Never mark something DONE on "the code looks correct"**

---

## Signal Evaluation Runbook — Quick Start

### Hub API (preferred — no DB credentials)
```bash
# Get JWT from AWS hub (not Railway — may be stale)
TOKEN=$(curl -s -X POST http://3.98.114.0:8091/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"billy","password":"novakash2026"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('access_token',''))")

# Key endpoints
curl -s "http://3.98.114.0:8091/api/v58/accuracy?limit=100" -H "Authorization: Bearer $TOKEN"
curl -s "http://3.98.114.0:8091/api/v58/strategy-decisions?limit=50" -H "Authorization: Bearer $TOKEN"
curl -s "http://3.98.114.0:8091/api/v58/prediction-surface?days=7" -H "Authorization: Bearer $TOKEN"
```

### Analysis Scripts
```bash
export PUB_URL="postgresql://postgres:PASSWORD@hopper.proxy.rlwy.net:35772/railway"

# Full current-state report (7 sections)
python3 docs/analysis/full_signal_report.py

# Window accuracy surface
python3 docs/analysis/run_window_analysis.py
```

### `full_signal_report.py` — 7 Sections
1. **Data coverage** — windows available, date range, SE eval count
2. **Current regime** — last 4h UP% vs DOWN%, VPIN, HMM regime
3. **Ungated signal accuracy** — by eval_offset bucket (T-30 to T-240), by confidence band
4. **V4 paper trade performance** — TRADE count, W/L, skip reason distribution
5. **V10 ghost performance** — gate failure breakdown, would-have W/L
6. **CLOB divergence check** — was Sequoia ahead of CLOB at trade points?
7. **Config recommendations** — threshold changes, regime filters, timing adjustments

### Key Schema Gotchas
- `window_snapshots.actual_direction` **DOESN'T EXIST** — use `CASE WHEN close_price > open_price THEN 'UP'...`
- `window_snapshots.oracle_outcome` is NULL — not populated by reconciler
- `ROUND(double precision, int)` fails in PostgreSQL — cast to `::numeric` first
- `text()` with `::timestamptz` SQL cast confuses SQLAlchemy — use `CAST(:param AS timestamptz)`

### Window Analysis Findings (70,272 windows, 2026-04-12)
- **Sweet spot:** T-120 to T-150 (55.5% accuracy, peaks at T-135)
- **Cliff at T-90:** drops to 48.7% — CLOB has priced outcome, signal lags
- **Only trade:** `confidence_distance >= 0.12` (strong/high bands = 64-65% WR)
- **CLOB asymmetry:** DOWN + NO ask <= $0.58 = 90%+ WR (but 84% DOWN dataset — bearish bias caveat)

### Config Decision Framework

| Last 4h ungated accuracy | Action |
|--------------------------|--------|
| > 65% | Keep config, can increase position size |
| 55-65% | Keep config, reduce position size |
| 45-55% | Tighten confidence threshold |
| < 45% | Pause, investigate regime change |

---

## Data Tables — Write Rate Sanity Checks

```sql
-- V4 decision ticks (~12/min)
SELECT COUNT(*), MAX(created_at) FROM ticks_v4_decision WHERE created_at > now() - interval '5 minutes';

-- Strategy decisions (~60/min when engine running)
SELECT COUNT(*), MAX(created_at) FROM strategy_decisions WHERE created_at > now() - interval '5 minutes';

-- Chainlink oracle ticks (~60/min = 4 assets × 15/min)
SELECT COUNT(*), MAX(ts) FROM ticks_chainlink WHERE ts > now() - interval '1 minute';

-- Paper trades OPEN for > 10 minutes (should be 0 after stale-trade fix in PR #128)
SELECT COUNT(*) FROM trades WHERE status = 'OPEN' AND is_live = false AND created_at < now() - interval '10 minutes';
```

---

## Common Pitfalls

- **Branch direction:** novakash → develop, timesfm-repo → main (not the other way around)
- **Hub session staleness:** JWT expires, 401 on `/api/*` means re-login
- **The $80k paper bug (DQ-06):** Always pass `price_getter` to PaperExchangeAdapter
- **The spot-vs-perp confusion (DQ-05):** Trace the actual read path before assuming a pricing bug
- **asyncpg type deduction (PE-02/PE-05):** Bug classes come in pairs — always grep sibling patterns
- **Dual-writer races:** Remove old `railway.toml` before pushing to new host
- **Log truncation on restart:** Use `scripts/restart_engine.sh` which rotates first, then appends with `>>`
- **Update the checklist:** Every code change needs matching status flip + progressNotes in same commit

---

## Canonical Commands

**EC2 Instance Connect one-liner:**
```bash
ssh-keygen -t ed25519 -f /tmp/ec2_temp_key -N "" -q && \
aws ec2-instance-connect send-ssh-public-key \
  --instance-id i-0785ed930423ae9fd \
  --instance-os-user novakash \
  --ssh-public-key file:///tmp/ec2_temp_key.pub \
  --availability-zone ca-central-1b \
  --region ca-central-1 && \
ssh -o StrictHostKeyChecking=no -o IdentitiesOnly=yes \
  -i /tmp/ec2_temp_key novakash@15.223.247.178
```

**Deploy verification:**
```bash
gh run list -R billybrichards/novakash --workflow=deploy-engine.yml --limit 5
gh run view --log -R billybrichards/novakash <run-id>
gh workflow run deploy-engine.yml -R billybrichards/novakash --ref develop
```

**Remote engine restart:**
```bash
ssh ubuntu@15.223.247.178 'sudo bash /home/novakash/novakash/scripts/restart_engine.sh'
sudo tail -n 100 /home/novakash/engine.log
pgrep -fa 'python3 main.py'
```

---

**Last Updated:** 2026-04-13  
**Related:** AUDIT_PROGRESS.md, CI_CD.md, full_signal_report.py, run_window_analysis.py
