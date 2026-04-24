# Frontend agent onboarding — novakash

**Audience:** a fresh FE-focused coding agent with no prior context on this repo.
**Scope:** everything you need to ship a FE PR without asking 20 questions.
**Last updated:** 2026-04-20
**Keep current:** when conventions change, edit this file.

---

## 1. What this is

Novakash is a BTC/prediction-market trading system. Three big parts:

- **Engine** (`engine/`) — Python asyncio. Lives on Montreal (15.223.247.178). Writes trades to Postgres, executes on Polymarket CLOB.
- **Hub** (`hub/`) — Python FastAPI. Lives on AWS (16.54.141.121:8091). Read-only projection over Postgres + proxy to the ML box for `/v4/*` endpoints. This is what the frontend talks to.
- **Frontend** (`frontend/`) — React 18 + Vite SPA. Deployed as static files on AWS nginx box (99.79.41.246). Talks to hub via nginx `/api/*` proxy.

Billy is the operator. He trades real money on this; be careful with anything that could break live trading. FE changes never directly touch engine but they can mislead the operator — **prefer honest "degraded" states over silently-wrong UI**.

---

## 2. Environment

### 2.1 Checkout

You're in a worktree. Likely at `/Users/billyrichards/code/novakash/.claude/worktrees/mystifying-mcclintock-cfff09/`. If not, adjust paths.

```bash
cd /Users/billyrichards/code/novakash/.claude/worktrees/mystifying-mcclintock-cfff09
git fetch origin
git checkout -b feat/your-feature origin/develop
```

**Primary branch is `develop`** (not `main`). PRs target `develop`. No direct pushes — memory rule. `main` is a release-ish branch; don't touch unless told.

### 2.2 Node version

**Use node 20.** CI runs node 20. If you build with node 22+ and touch `package-lock.json` you'll trigger the lock-drift CI gate (PR 271 added it) and the whole FE deploy will fail with cryptic EUSAGE errors about missing esbuild binaries.

```bash
export NVM_DIR="$HOME/.nvm"
source "$NVM_DIR/nvm.sh"
nvm use 20 >/dev/null 2>&1 || nvm install 20
node --version   # should print v20.x
```

### 2.3 Install + build

```bash
cd frontend
npm ci            # respects existing lock
npm run build     # vite build; runs `node scripts/gen-release-notes.mjs` in prebuild
npm run dev       # local dev server; predev also regenerates release notes
npm test          # vitest
```

**Build must be clean before you push.** Warnings that didn't exist before this branch = fix them.

### 2.4 The lock-drift trap

If you add or bump a package:
1. Run `nvm use 20` first.
2. Then `npm install` (this regenerates `package-lock.json`).
3. Then `git add frontend/package.json frontend/package-lock.json`.
4. Commit both.

CI runs `npm ci --dry-run` as a standalone step before install. If lock is out of sync (very common with subagent work that touches `package.json` under a different node version) → the step fails with `frontend/package.json and package-lock.json are out of sync` and the build job halts. Fix is always the three-line sequence above.

---

## 3. Stack + conventions

### 3.1 What's in `frontend/`

- React 18, React Router v6, Vite 5
- Axios via `src/hooks/useApi.js` + `src/hooks/useApiLoader.js`
- WebSocket via `src/hooks/useWebSocket.js`
- Dark theme tokens in `src/theme/tokens.js`
- Shared components in `src/components/shared/` (PageHeader, DataTable, FilterPills, Loading, EmptyState)
- Tests: vitest + `@testing-library/react` (already in devDeps — don't add Jest)
- **No Tailwind, no CSS-in-JS lib.** Inline `style={{}}` with tokens. Look at existing pages before reinventing.

### 3.2 What NOT to add

- **No new npm deps** unless strictly required. Weigh the gzip delta.
- **No React Query / Redux / Zustand.** Polling is done with `useApiLoader` or a custom `useSnapshotStream`-style hook.
- **No Recharts / Chart.js for small charts.** Existing pages use inline SVG — look at `ClassifierHistogram.jsx`, `DisagreementPlot.jsx` for patterns.
- **No Playwright / e2e tests.** Memory rule.
- **No Polymarket direct calls from FE.** Memory rule (`/api/*.polymarket.com` is hub-perimeter-forbidden).

### 3.3 Navigation model

`src/nav/navigation.js` is single source of truth. Add an entry there → it appears in the sidebar. Archive entries live in the same file under `ARCHIVED_PAGES`.

Sections (color-coded): `TRADING` (purple), `MONITORING` (teal), `ANALYSIS` (cyan), `CONTROL` (amber), `ARCHIVE` (grey).

Routes mount in `src/App.jsx` under the `AppShell` protected route. Tier-1 pages are **eagerly imported** (critical path). Archive / heavy pages are `lazy()` imports with `<Suspense>`.

### 3.4 Tier-1 pages (active nav, as of 2026-04-20)

| Section | Path | File | Purpose |
|---|---|---|---|
| TRADING | `/` | `pages/UnifiedDashboard.jsx` | Operator summary, stats, alerts |
| TRADING | `/trades` | `pages/TradesEnhanced.jsx` | Trade list with phantom banner, deep filter |
| TRADING | `/windows` | `pages/WindowResults.jsx` | Per-strategy lineup cards per window |
| TRADING | `/wallet` | `pages/Wallet.jsx` | CLOB-first wallet v2 (see §6) |
| TRADING | `/pnl` | `pages/PnL.jsx` | PnL analytics |
| MONITORING | `/monitor` | `pages/Monitor.jsx` | Live v5_ensemble + ML-box health dashboard |
| ANALYSIS | `/signals` | `pages/SignalExplorer.jsx` | Strategy × regime WR matrix |
| ANALYSIS | `/gate-traces` | `pages/GateTraces.jsx` | Per-gate heatmap |
| ANALYSIS | `/gate-matrix` | `pages/polymarket/GatePipelineMonitor.jsx` | 4-strategy decision matrix |
| ANALYSIS | `/analysis` | `pages/StrategyAnalysis.jsx` | 30-day backtest dashboard |
| ANALYSIS | `/strategies` | `pages/Strategies.jsx` | Strategy register + WR |
| ANALYSIS | `/compare` | `pages/compare/Compare.jsx` | Side-by-side YAML knob diff |
| CONTROL | `/config` | `pages/ConfigOverrides.jsx` | Runtime config editor |
| CONTROL | `/audit` | `pages/AuditTasks.jsx` | Audit task queue |
| CONTROL | `/notes` | `pages/Notes.jsx` | Hub notes journal |
| CONTROL | `/schema` | `pages/Schema.jsx` | DB schema catalog |
| CONTROL | `/system` | `pages/System.jsx` | Engine kill/resume/paper-mode |

---

## 4. Hub API — essentials

Base URL: `http://16.54.141.121:8091` direct. Via nginx: `/api/*` on the FE origin. Use the latter in FE code.

### 4.1 Auth

```bash
TOKEN=$(curl -s -X POST http://16.54.141.121:8091/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"billy","password":"novakash2026"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
```

JWT in localStorage. Access expires 15min; refresh via `/auth/refresh`. `useApi` handles both silently.

**Known weakness:** audit task #248 tracks moving JWT to httpOnly cookie. Don't refactor auth in a feature PR — file as follow-up if it bugs you.

### 4.2 Endpoints you'll actually use

| Endpoint | What | Deduped? | Cap |
|---|---|---|---|
| `GET /api/v58/strategy-decisions?strategy_id=X&timeframe=5m&limit=1000&resolved=true` | per-strategy decisions with shadow outcome | yes (PR #299) | 1000 |
| `GET /api/v58/strategy-windows?asset=BTC&limit=100` | per-window strategy lineup | yes | — |
| `GET /api/v58/outcomes?limit=100` | window resolution (legacy aggregate) | — | — |
| `GET /api/trades?strategy=X&limit=500` | actual fills from trades table | — | 1000 |
| `GET /api/trades/stats` | aggregate WR / PnL (real + phantom split) | — | — |
| `GET /api/strategies` + `/api/strategies/{id}` | YAML configs from registry | — | — |
| `GET /api/v4/snapshot?asset=BTC&timescales=5m,15m,1h` | live ML-box ensemble surface | — | — |
| `GET /api/wallet/snapshot` · `/pending` · `/history` | wallet CLOB-first data (PR #298) | — | — |
| `GET /api/notes?limit=300` · `POST /api/notes` | hub-backed journal | — | — |
| `GET /api/audit-tasks?limit=300` · `POST` · `PATCH /{id}` | audit queue | — | — |
| `POST /api/system/kill` · `/resume` · `/paper-mode` | operator control | — | — |

### 4.3 Endpoints that look proxied but aren't (yet)

Hub does NOT proxy these ML-box endpoints (as of 2026-04-19 audit):
- `/api/health` · `/api/v4/health` · `/api/v4/regime` · `/api/v4/consensus`

If you need them, either (a) pull the equivalent subfield from `/api/v4/snapshot` (many overlap), or (b) file an audit task for a hub proxy PR. Don't hit the ML box direct from FE — CORS won't work.

### 4.4 Graceful degradation is the law

**If an endpoint is missing or 404s, degrade. Never fabricate.** Pattern used across the codebase:
- Show a red banner referencing the audit task that tracks the gap ("Tracked by #217")
- Show real data from a fallback source
- Tag fallback cells with a small badge (`db-only`, `fallback`, etc.) — see `SotBadge` in `wallet-v2/`

Hub note **#189** (Wallet v2) codifies this with a 5-tier "sot_rank" system — look there if the feature touches data-trust UX.

---

## 5. Deploy flow (FE)

GitHub Actions deploys FE via `.github/workflows/deploy-frontend.yml` on push to develop. When Actions is paid for, merging a FE PR auto-ships.

**If Actions is down** (check `gh run list --workflow=deploy-frontend.yml --limit 1`) you can ship manually from your laptop. This is the escape hatch Billy uses:

```bash
export NVM_DIR="$HOME/.nvm"; source "$NVM_DIR/nvm.sh"; nvm use 20 >/dev/null 2>&1
cd /Users/billyrichards/code/novakash/.claude/worktrees/mystifying-mcclintock-cfff09
git fetch origin develop
git reset --hard origin/develop
cd frontend && npm run build && cd ..
tar czf /tmp/frontend-dist.tar.gz -C frontend/dist .
scp -i ~/.ssh/novakash-local-rsa.pem /tmp/frontend-dist.tar.gz ubuntu@99.79.41.246:/tmp/
scp -i ~/.ssh/novakash-local-rsa.pem frontend/nginx.conf ubuntu@99.79.41.246:/tmp/nginx-frontend.conf
ssh -i ~/.ssh/novakash-local-rsa.pem ubuntu@99.79.41.246 bash -s <<'FEEOF'
set -e
sudo rm -rf /var/www/frontend   # see §5.1 macOS tar trap
sudo mkdir -p /var/www/frontend
sudo tar xzf /tmp/frontend-dist.tar.gz -C /var/www/frontend
sudo chown -R www-data:www-data /var/www/frontend
sudo rm -f /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf
sudo cp /tmp/nginx-frontend.conf /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
echo "deployed $(date -u +%H:%M)"
rm /tmp/frontend-dist.tar.gz /tmp/nginx-frontend.conf
FEEOF
curl -sI http://99.79.41.246/ | head -3    # expect HTTP/1.1 200 OK
```

### 5.1 macOS tar trap

`tar czf` on macOS leaks AppleDouble `._*` sidecar files into the tarball. Linux `rm -rf /var/www/frontend/*` glob does NOT match `._*` (they start with `.`). After extract, stale sidecars pile up and sometimes shadow real files → 403 responses.

Fix: nuke the directory entirely (`sudo rm -rf /var/www/frontend && sudo mkdir -p /var/www/frontend`) before extract. Already embedded in the snippet above. Don't skip that step.

**Alternative:** `COPYFILE_DISABLE=1 tar czf …` on mac prevents sidecars at source. Cleaner long-term fix; not yet in the deploy workflow.

---

## 6. The Wallet v2 / SotBadge pattern

`src/pages/wallet-v2/` is the best-in-repo example of honest degraded-state UX. Every data cell can tag its source:

```jsx
<SotBadge rank={snapshot._meta.sot_rank} tooltip="Data source: wallet_truth.py verified on-chain" />
```

Five rank tiers (per hub note #189):
1. On-chain 2-of-3 RPC consensus
2. Polymarket data-api verified
3. Engine cache (recent)
4. Trades table (rich but not authoritative)
5. DB-only fallback / minimum-viable

When building a new data-heavy page, copy the pattern — every number gets a rank tag.

---

## 7. Testing

```bash
npm test                              # all vitest
npm test -- src/pages/compare         # scoped
npm test -- --run                     # single-pass, no watch
```

**What to test:**
- Pure helper functions — always
- Component behaviour — only when non-obvious (e.g. `KillConfirmModal` 10s countdown)
- Integration shape — when hitting unfamiliar hub endpoint shape

**What NOT to test:**
- Vite / React internals
- Network against live hub (use mocks)
- UI snapshot tests without a reason
- Playwright (memory rule — you can't run it locally)

### 7.1 Pre-existing failures

Develop has some pre-existing vitest failures in `PnL.test.jsx`, `App.redirects.test.js`, `navigation.test.mjs`. If your PR's failures match those, confirm via `git stash && npm test && git stash pop` that they pre-exist. Don't let them block you; don't let them grow either.

---

## 8. Open PRs / in-flight work (as of 2026-04-20)

| PR/Note | Status | What | Gates you from |
|---|---|---|---|
| Note #194 / audit #257 | OPEN | `/compare` WR strip spec — add per-strategy live WR below column headers | Depends on #258 |
| Note #195 / audit #258 | OPEN | v6_sniper shadow-view fix + full comparative table | Blocks #257 |
| Audit #217 | IN_PROGRESS (PR #298 shipped partial) | `/api/wallet/snapshot` endpoint | Wallet v2 trust banner stays red until fully resolved |
| Audit #253 | IN_PROGRESS | `/api/wallet/pending` + `/history` — FE degrades to db-only currently | Wallet tabs show db-only badges |
| Audit #254 | IN_PROGRESS | `trades.transport` + `initiator` columns (schema shipped, engine writer not) | Wallet history columns show "—" for recent trades |
| Audit #248 | OPEN | JWT in localStorage → httpOnly cookie | FE security posture |

When you pick up a task, the audit task payload usually cites a hub note ID. Fetch it:

```bash
curl -s "http://16.54.141.121:8091/api/notes?limit=300" -H "Authorization: Bearer $TOKEN" \
  | python3 -c "
import sys,json
for r in json.load(sys.stdin).get('rows', []):
    if r.get('id') == YOUR_NOTE_ID:
        print(r['body']); break
"
```

Notes override memory / conventions if they explicitly say so. Respect them.

---

## 9. How you should work

1. **Read the spec (hub note) first. Every word.** If ambiguous, list the ambiguity in your output — don't guess silently.
2. **Work from origin/develop's tip.** Rebase if you branched 4 hours ago.
3. **Small PRs beat big PRs.** Billy squash-merges most things. Separate commits only when selective revert matters (e.g. multi-feature PR) — then use `gh pr merge --merge` not `--squash`.
4. **Open PR as ready-for-review, not draft, unless asked otherwise.** Do NOT merge or deploy — Billy owns that step.
5. **Caveman style for commit subject + PR title.** Body in normal English.
6. **File audit tasks for gaps you find but can't fix in scope.** Don't leave TODOs in code; leave them in the hub backlog.

---

## 10. Memory rules (quick-reference)

See `~/.claude/projects/-Users-billyrichards-Code-novakash/memory/MEMORY.md` for the full list. The FE-relevant ones:

- **No direct pushes to develop** — PRs only (`feedback_no_direct_develop.md`)
- **`develop` is the primary branch** (`project_primary_branch_develop.md`)
- **No local Playwright** — deploy to Railway only (`feedback_no_local_playwright.md`)
- **No local Polymarket** — any `*.polymarket.com` from mac/hub is forbidden (`feedback_no_local_polymarket.md`)
- **DB is Railway, Hub is AWS** (`feedback_db_is_aws.md`)
- **Verify before claiming behaviour** — no confabulation (`feedback_no_confabulation.md`)

---

## 11. Common failure modes and how to diagnose

| Symptom | Likely cause | Fix |
|---|---|---|
| `npm ci` fails EUSAGE | Lock drift, usually after subagent touched package.json under wrong node version | `nvm use 20 && npm install && git add package-lock.json` |
| FE deploy returns 403 Forbidden | macOS tar leaked `._*` sidecars, real index.html never landed | Nuke `/var/www/frontend` entirely, re-extract (see §5.1) |
| Signal Explorer matrix all "—" | Hub view `strategy_decisions_resolved` outcome=None | Usually direction/actual_direction encoding drift. See hub note #192 post-mortem |
| Endpoint returns 0 rows despite data existing | Client-side deduped sample too small (before PR #299) | Check if endpoint is PR #299-or-later; if not, push dedup to endpoint not client |
| FE can't reach `/api/v4/regime` | Hub doesn't proxy it | Use `/api/v4/snapshot` and pull `.regime` from embedded field |
| Wallet page shows all red trust banners | `/api/wallet/*` 404 — expected until audit #217/#253 done | Degrade gracefully with `db-only` SotBadges; don't suppress the banner |
| Release-notes dropdown empty | Build hook `gen-release-notes.mjs` didn't run OR `/release-notes.json` 404 | `npm run build` regenerates; deploy must ship `public/release-notes.json` |

---

## 12. What to do when stuck

1. Look for an existing page doing something similar. The codebase has ~50 pages; someone solved your problem already.
2. Check hub notes (`GET /api/notes?limit=300`) — Billy sometimes specs things before building.
3. File the ambiguity as an audit task with severity HIGH and ask Billy in the PR body.
4. Do NOT improvise. "I wasn't sure so I did X" is always the wrong answer. Show your reasoning, surface the gap.

---

## 13. Output format for your work

When your task is done, post back:

1. **PR URL** (if you opened one)
2. **Files changed** with brief reason per file
3. **Endpoints hit** (new or existing) with shape summaries if non-obvious
4. **Gaps you found** — audit tasks you filed (with IDs) for things out of scope
5. **Build + test summary** — last few lines of `npm run build` and `npm test`
6. **Ambiguities you guessed on** — flag for Billy

Keep it terse. Billy is in caveman mode.

---

## 14. If something catches fire

- **Kill switch:** `/monitor` page has it. 10-second countdown, typed "KILL" confirmation. Fires `POST /api/system/kill`. Don't add keyboard shortcut.
- **Revert last merged PR:** `gh pr list --state merged --limit 5`, find the one, `gh pr view N` for the merge commit SHA, `git revert <sha>` on a new branch, PR.
- **Rollback FE only:** re-run a prior FE deploy from GH Actions (`gh run rerun <old_run_id>`). Or manually scp a prior dist tarball if you kept it.
- **Never:** force-push to develop. Force-push to main. Bypass the kill switch. Touch the engine from a FE PR.

---

Good luck. Ship carefully.
