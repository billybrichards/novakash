# CFG-01 — Full Config → DB Migration Plan

_Status: PROPOSAL. No code changes in this PR; this document is the plan of record for CFG-02..CFG-11._

_Authored: 2026-04-11_
_Target branch: `develop`_
_Related: `engine/config/runtime_config.py`, `hub/api/trading_config.py`, `hub/api/config.py`, `frontend/src/pages/TradingConfig.jsx`, `docs/CONFIG.md`_

---

## 1. Executive Summary

Novakash runtime configuration is currently a split-brain mess. Some keys live in DB (`trading_configs` table, partially hot-reloaded by `engine/config/runtime_config.py::sync()`). Most keys live in `.env` files on the Montreal EC2 box (`15.223.247.178`, `i-0785ed930423ae9fd`), and every time the operator wants to flip a gate flag or tune a threshold they have to SSH in, edit `/home/novakash/novakash/engine/.env`, kill the Python process, and relaunch — a multi-minute round trip where a typo breaks live trading. On top of that, `docs/CONFIG.md:44` documents that the current default is `SKIP_DB_CONFIG_SYNC=true`, meaning the DB sync path exists but is deliberately disabled on prod because the two sources drifted in the past and the operator doesn't trust DB to be authoritative. We are going to fix this by building one unified DB-backed config store, one admin UI (`/config`), one service-side loader pattern with a TTL cache + degrade-safe fallback, and a phased migration that is zero-downtime per service.

**Non-goals for this plan:**
- Do NOT move secrets (API keys, private keys, database URLs) into the DB. They stay in `.env`.
- Do NOT break existing code paths on merge. Phase 0 ships reading-only; Phase 1 dual-reads; Phase 2 flips source-of-truth per service; Phase 3 cleans up inline `os.environ.get` calls.
- Do NOT touch the `trading_configs` table's schema on-merge. CFG-02 introduces new tables alongside and consolidates later.
- Do NOT build a permission/RBAC system in this plan. The UI assumes the existing JWT auth wall (hub/auth) is sufficient gating for now; a follow-up (CFG-12, out of scope) adds admin-only editing.

---

## 2. Scope

### 2.1 In-scope services

| Service       | Repo     | Branch   | Host             | Current config source            |
|---------------|----------|----------|------------------|----------------------------------|
| engine        | novakash | develop  | EC2 Montreal     | `.env` + `RuntimeConfig` + `trading_configs` (partial, SKIP=true) |
| margin_engine | novakash | develop  | EC2 London       | `.env` + pydantic `MarginSettings` |
| hub           | novakash | develop  | Railway → AWS    | `.env` + 3-key `Settings` class   |
| data-collector| novakash | develop  | EC2 Montreal     | `.env` (DATABASE_URL only) + hardcoded |
| macro-observer| novakash | develop  | EC2 Montreal     | `.env` (11 env vars, inline reads) |
| timesfm-service| novakash-timesfm-repo | main | EC2 Montreal | Hardcoded in Python constructors + Dockerfile CMD |

The engine and margin_engine own 95% of the behavioural knobs the operator needs to flip. The other four services contribute only a handful of tunables each, but we include them in the plan so the final `/config` page is one complete surface instead of four separate holdovers.

### 2.2 Out-of-scope services

- The novakash-timesfm-repo itself deploys from its own CI and its Python services are almost entirely hardcoded. It gets a small read-only table in `/config` so the operator can _see_ what's running, but we do not add a DB-backed hot-reload loop to that service in this plan. A follow-up (CFG-13, out of scope) can port it.
- The `frontend/` itself has no runtime config — Vite builds it at deploy time with hardcoded API base URLs. Not in scope.

### 2.3 The DB-vs-.env split: explicit decision rules

A key belongs in `.env` (operator hand-edits on host, never hot-reloadable) if **ANY** of the following are true:

1. **It is a secret.** API keys, private keys, signing keys, passphrases, passwords, OAuth tokens. Storing these in the DB means any SELECT against the config table leaks them; DB backups become secret backups; the admin UI has to pretend it can't display them. Just keep them in `.env`.
2. **It is infrastructure.** `DATABASE_URL`, `SECRET_KEY`, container ports, service URLs (timesfm_url, hyperliquid_info_url, etc.), health-check paths. These are set once per deploy and rotated out-of-band.
3. **It is the bootstrap flag that enables DB config itself.** `SKIP_DB_CONFIG_SYNC`, or whatever CFG-02 names its replacement, cannot be DB-managed for obvious chicken-and-egg reasons.
4. **It is a deploy-environment toggle.** `DEBUG`, `PAPER_MODE` in the sense of "does this host ever run live trading". The DB can flip `paper_enabled`/`live_enabled` (it already does, via `system_state`) but the master "is live trading even possible on this host" stays in `.env`.

A key belongs in DB (editable via `/config` UI, hot-reloaded by a TTL cache in each service) if **ALL** of the following are true:

1. **It is trading behaviour.** A gate flag, a threshold, a min/max bound, a cooldown, a cap, a multiplier. Anything the operator tunes more than once.
2. **It does not contain a secret.**
3. **The service can consume a change between iteration ticks without a restart.** If the code reads the value in a hot loop, the TTL cache works. If the value is captured inside a long-lived class attribute set at construction time, the class either has to be rebuilt on reload or the operator still needs a restart. The `__init__`-only gates in `engine/signals/gates.py` (DeltaMagnitudeGate, TakerFlowGate, etc.) are the ones most affected here — see §6.3 for how we plan to handle them.
4. **There is a sane fallback if the DB is unreachable.** Every DB-managed key has a `.env` shadow value or a code-level default, so DB outage never fail-closes trading.

---

## 3. Existing Prior Art (READ THIS BEFORE WRITING CODE)

There is already substantial plumbing here. CFG-02..CFG-10 must **extend** it, not duplicate it.

### 3.1 `trading_configs` table (engine/margin_engine behaviour configs)

Already exists; auto-created by `hub/main.py:52-62` on hub startup. Schema:

```
trading_configs (
  id SERIAL PK,
  name VARCHAR(128) NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  description TEXT,
  config JSONB NOT NULL,          -- the actual key/value blob
  mode VARCHAR(16) NOT NULL,      -- 'paper' | 'live'
  is_active BOOLEAN,              -- which row the engine reads
  is_approved BOOLEAN,            -- required before is_active can be TRUE for mode=live
  approved_at TIMESTAMPTZ,
  approved_by VARCHAR(64),
  parent_id INTEGER REFERENCES trading_configs(id),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
)
```

The `config` JSONB blob is a flat dict of ~25 engine keys (see `hub/api/trading_config.py::CONFIG_DEFAULTS`, lines 77-338). Each default has `key/label/description/type/min/max/step/unit/category/impact/widget` — this is already a richer schema than what CFG-02 needs for v1 and we can steal the widget metadata wholesale.

### 3.2 `engine/config/runtime_config.py` — the reference hot-reload pattern

This module already implements:

- **Env-first initialization** (`__init__` calls `_env_float`/`_env_int`/`os.environ.get` on every DB-tracked key).
- **DB overlay via `async def sync(pool, paper_mode)`** (lines 235-323). On every heartbeat (~10s) the orchestrator calls `await runtime.sync(...)`, which pulls the active row from `trading_configs WHERE mode=? AND is_active=TRUE`, parses the JSONB `config`, and overlays 25 keys onto the singleton via `_DB_KEY_MAP` (lines 51-75).
- **Skip flag** — the whole sync path is gated on `SKIP_DB_CONFIG_SYNC=='true'` (line 243). When enabled, sync is a no-op and everything comes from env. **Currently set to `true` on prod** per `docs/CONFIG.md:44`.
- **Change logging** (`log.info("runtime_config.synced", changes=[...])`) on every diff.
- **Snapshot method** (`snapshot() -> dict`) that feeds `system_state.config` for the frontend.

**What's missing:** the sync path is one-way overlay (DB → runtime), not read-back. There is no audit log of who changed what when. There is no history. The operator has no way to rollback. The `_DB_KEY_MAP` only covers 25 of the ~90 keys the engine actually reads from env. And — critically — **none** of the engine `signals/gates.py` gates go through `runtime`. They each do their own `os.environ.get(...)` at `__init__` time, which means a DB config change does not reach them until the Python process restarts.

### 3.3 `system_state` table — the live mode toggle

Already exists. `hub/main.py:65-68` adds:

```
system_state.paper_enabled BOOLEAN DEFAULT TRUE
system_state.live_enabled BOOLEAN DEFAULT FALSE
system_state.active_paper_config_id INTEGER
system_state.active_live_config_id INTEGER
```

`engine/strategies/orchestrator.py:1755` reads `paper_enabled`/`live_enabled` every heartbeat and hot-switches `poly_client.paper_mode`, `risk_manager._paper_mode`, and `alerter._paper_mode` without a restart. This is the proof-of-concept that DB-backed hot-reload already works for at least one knob — CFG-07..CFG-09 extend the same pattern to every other knob.

### 3.4 `hub/api/trading_config.py` — existing CRUD API

Already has 12 routes under `/api/trading-config/*` (hub/main.py:143):

| Method | Path                              | Description                              |
|--------|-----------------------------------|------------------------------------------|
| GET    | `/api/trading-config/defaults`    | Full schema + categories for the UI      |
| GET    | `/api/trading-config/list`        | List all configs (filterable by mode)    |
| GET    | `/api/trading-config/active/{mode}` | Fetch currently-active row for a mode |
| GET    | `/api/trading-config/live-status` | Combined paper/live/engine state snapshot|
| GET    | `/api/trading-config/{config_id}` | Single config by id                      |
| POST   | `/api/trading-config`             | Create a new config row                  |
| PUT    | `/api/trading-config/{id}`        | Update an existing row                   |
| POST   | `/api/trading-config/{id}/clone`  | Clone (parent_id tracking)               |
| POST   | `/api/trading-config/{id}/activate` | Flip `is_active=TRUE`, all others off in that mode |
| POST   | `/api/trading-config/{id}/approve`| Mark `is_approved=TRUE` with password    |
| DELETE | `/api/trading-config/{id}`        | Soft-delete a config row                 |
| POST   | `/api/trading-config/toggle-mode` | Flip `paper_enabled`/`live_enabled`      |

**What's missing:** there are no `GET /schema` or `POST /config/upsert-key` or `GET /config/history` endpoints. The CRUD works on whole config rows (copy-paste-clone), not individual keys. The `defaults` endpoint hardcodes 25 key definitions in Python; there is no DB-backed schema so adding a new tunable means editing `trading_config.py` and redeploying the hub.

### 3.5 `hub/api/config.py` — a second, parallel mini-API

Separate from `trading_config.py`, this file (lines 42-92) has:

- `GET /api/config` — reads `system_state.state.config` JSONB
- `PUT /api/config` — writes back to the same JSONB with a 13-key whitelist

This is used by `frontend/src/pages/Config.jsx` (the old minimal config page) and is effectively a fourth config mechanism layered on top of `trading_configs` and `runtime_config.py` and the inline env reads. CFG-02 should plan to deprecate this whole file after CFG-05 lands.

### 3.6 `frontend/src/pages/TradingConfig.jsx`

1373 lines, the current full-featured config UI. Knows about the 5 categories from `trading_config.py::CONFIG_DEFAULTS`, renders each key through `ConfigWidget.jsx`, supports draft/approve/activate workflow, mode badges, collapsible sections. CFG-05/CFG-06 should subsume this page into `/config` (the App.jsx currently redirects `/config` → `/trading-config`; CFG-06 flips it).

### 3.7 What the existing prior art tells us

- **The DB-backed-config idea is already validated.** `system_state.paper_enabled`/`live_enabled` proves hot-reload works end-to-end. `runtime_config.py::sync()` proves a JSONB overlay mechanism works, and its change-logging is already in place.
- **The split-brain is the operator's biggest complaint.** `SKIP_DB_CONFIG_SYNC=true` is on in prod because the two sources drifted and neither is trusted. Until we build a real schema + audit trail + single-key upsert + history, flipping the skip flag is too risky. CFG-02..CFG-11 is the path to making the flag safe to flip.
- **The `signals/gates.py` gates are the hardest part.** They read env at `__init__`, which means hot-reload is structurally impossible without refactoring each gate to read from a shared runtime singleton. See §6.3 for the plan.
- **There is no secret data in any of the existing DB config rows.** `CONFIG_DEFAULTS` is 100% behavioural thresholds. We do not have to retrofit an encryption layer.

---

## 4. Full Inventory of Current Config Keys

### 4.1 Engine — keys discovered in `engine/`

The engine reads config from three places: `engine/config/settings.py` (pydantic), `engine/config/runtime_config.py` (dataclass with manual env reads + DB overlay), and inline `os.environ.get(...)` calls scattered through `strategies/` and `signals/`. This table is the union of all three.

Columns: `key` · current location · type · default · rationale · DB-managed? (Y/N) · reason

#### 4.1.1 Engine — secrets and infrastructure (STAY in `.env`)

| Key | Location | Type | Default | Rationale | DB? | Reason |
|---|---|---|---|---|---|---|
| `DATABASE_URL` | settings.py:15 | str | — | asyncpg conn string | N | infrastructure |
| `POLY_PRIVATE_KEY` | settings.py:18 | str | "" | Polymarket eth priv key | N | secret |
| `POLY_API_KEY` | settings.py:19 | str | "" | Polymarket CLOB API key | N | secret |
| `POLY_API_SECRET` | settings.py:20 | str | "" | Polymarket CLOB secret | N | secret |
| `POLY_API_PASSPHRASE` | settings.py:21 | str | "" | Polymarket CLOB passphrase | N | secret |
| `POLY_FUNDER_ADDRESS` | settings.py:22 | str | "" | funder wallet addr | N | secret-adjacent |
| `POLY_BTC_TOKEN_IDS` | settings.py:56 | str | "" | CSV of watched token IDs | N | infrastructure |
| `OPINION_API_KEY` | settings.py:25 | str | "" | Opinion exchange API key | N | secret |
| `OPINION_WALLET_KEY` | settings.py:26 | str | "" | Opinion wallet priv key | N | secret |
| `BINANCE_API_KEY` | settings.py:29 | str | "" | read-only data | N | secret |
| `BINANCE_API_SECRET` | settings.py:30 | str | "" | read-only data | N | secret |
| `COINGLASS_API_KEY` | settings.py:33 | str | "" | CG data API | N | secret |
| `ANTHROPIC_API_KEY` | settings.py:34 | str | "" | Claude evaluator | N | secret |
| `POLYGON_RPC_URL` | settings.py:37 | str | "" | Polygon chain RPC | N | infrastructure |
| `TELEGRAM_BOT_TOKEN` | settings.py:40 | str | "" | alerter bot token | N | secret |
| `TELEGRAM_CHAT_ID` | settings.py:41 | str | "" | alerter chat id | N | secret-adjacent |
| `GMAIL_ADDRESS` | settings.py:52 | str | "" | Gmail for Poly login | N | secret-adjacent |
| `GMAIL_APP_PASSWORD` | settings.py:53 | str | "" | Gmail app password | N | secret |
| `BUILDER_KEY` | settings.py:59 | str | "" | Relayer API key | N | secret |
| `TIINGO_API_KEY` | orchestrator.py:513 | str | "" | Tiingo spot feed | N | secret |
| `TIMESFM_URL` | settings.py:68 / orchestrator.py:295 | str | "http://3.98.114.0:8000" | forecast service base URL | N | infrastructure |
| `TIMESFM_V2_URL` | orchestrator.py:343 | str | "http://3.98.114.0:8080" | v2 early-entry URL | N | infrastructure |
| `SKIP_DB_CONFIG_SYNC` | runtime_config.py:243 | bool | "true" (prod) | the bootstrap flag | N | bootstrap (§7 risks) |
| `PAPER_MODE` | settings.py:47 | bool | true | master paper toggle | N | deploy-env toggle; DB already has paper_enabled/live_enabled in system_state for runtime flip |
| `PLAYWRIGHT_ENABLED` | settings.py:51 | bool | false | browser automation on/off | N | deploy-env, host-specific |

**Engine `.env`-only count: ~26 keys.**

#### 4.1.2 Engine — trading behaviour keys currently in `runtime_config.py` (already DB-aware, partially)

These 25 keys already live in `_DB_KEY_MAP` and get overlaid from `trading_configs.config` JSONB on every `runtime.sync()`. CFG-02..CFG-10 formalises them as `config_keys` rows.

| Key | runtime_config.py attr | Type | Default | Rationale | DB? | Reason |
|---|---|---|---|---|---|---|
| `STARTING_BANKROLL` | `starting_bankroll` | float | 500.0 | initial bankroll USD | Y | already DB-tracked |
| `BET_FRACTION` | `bet_fraction` | float | 0.025 | Kelly fraction | Y | already DB-tracked |
| `MAX_POSITION_USD` | `max_position_usd` | float | 500.0 | hard cap per trade | Y | already DB-tracked |
| `MAX_DRAWDOWN_KILL` | `max_drawdown_kill` | float | 0.45 | drawdown kill % | Y | already DB-tracked |
| `DAILY_LOSS_LIMIT_USD` | `daily_loss_limit_usd` | float | 50.0 | daily USD loss cap | Y | already DB-tracked |
| `DAILY_LOSS_LIMIT_PCT` | `daily_loss_limit_pct` | float | 0.10 | daily % loss cap | Y | already DB-tracked |
| `MIN_BET_USD` | `min_bet_usd` | float | 2.0 | floor on bet size | Y | tunable |
| `MAX_OPEN_EXPOSURE_PCT` | `max_open_exposure_pct` | float | 0.30 | open book cap | Y | tunable |
| `CONSECUTIVE_LOSS_COOLDOWN` | `consecutive_loss_cooldown` | int | 3 | N losses → cooldown | Y | tunable |
| `COOLDOWN_SECONDS` | `cooldown_seconds` | int | 900 | cooldown duration | Y | already DB-tracked |
| `VPIN_BUCKET_SIZE_USD` | `vpin_bucket_size_usd` | float | 500000 | VPIN bucket size | Y | already DB-tracked |
| `VPIN_LOOKBACK_BUCKETS` | `vpin_lookback_buckets` | int | 50 | VPIN window | Y | already DB-tracked |
| `VPIN_INFORMED_THRESHOLD` | `vpin_informed_threshold` | float | 0.55 | informed trader gate | Y | already DB-tracked |
| `VPIN_CASCADE_THRESHOLD` | `vpin_cascade_threshold` | float | 0.70 | cascade trigger | Y | already DB-tracked |
| `VPIN_CASCADE_DIRECTION_THRESHOLD` | `vpin_cascade_direction_threshold` | float | 0.65 | cascade dir gate | Y | already DB-tracked |
| `CASCADE_OI_DROP_THRESHOLD` | `cascade_oi_drop_threshold` | float | 0.02 | cascade OI drop % | Y | already DB-tracked |
| `CASCADE_LIQ_VOLUME_THRESHOLD` | `cascade_liq_volume_threshold` | float | 5e6 | cascade liq $ thresh | Y | already DB-tracked |
| `ARB_MIN_SPREAD` | `arb_min_spread` | float | 0.015 | min arb spread | Y | already DB-tracked |
| `ARB_MAX_POSITION` | `arb_max_position` | float | 50.0 | max arb leg USD | Y | already DB-tracked |
| `ARB_MAX_EXECUTION_MS` | `arb_max_execution_ms` | int | 500 | arb timeout | Y | already DB-tracked |
| `POLYMARKET_FEE_MULT` | `polymarket_fee_mult` | float | 0.072 | Poly fee rate | Y | already DB-tracked (read-only widget) |
| `OPINION_FEE_MULT` | `opinion_fee_mult` | float | 0.04 | Opinion fee rate | Y | already DB-tracked (read-only widget) |
| `PREFERRED_VENUE` | `preferred_venue` | str | "opinion" | tiebreak venue | Y | already DB-tracked |
| `FIVE_MIN_MIN_DELTA_PCT` | `five_min_min_delta_pct` | float | 0.08 | NORMAL/TRANSITION min delta | Y | already DB-tracked |
| `FIVE_MIN_CASCADE_MIN_DELTA_PCT` | `five_min_cascade_min_delta_pct` | float | 0.03 | CASCADE min delta | Y | already DB-tracked |
| `FIVE_MIN_VPIN_GATE` | `five_min_vpin_gate` | float | 0.45 | entry VPIN gate | Y | already DB-tracked |

#### 4.1.3 Engine — trading behaviour keys currently inline-only (NOT yet DB-tracked, CFG-07 promotes them)

These are read either inline from `os.environ.get(...)` in `five_min_vpin.py` / `gates.py` / `orchestrator.py` or via `runtime_config.py.__init__` but NOT wired into `_DB_KEY_MAP`. All are trading behaviour, all should be DB-managed once the service-side loader can handle them.

##### 4.1.3.1 Execution / sizing / guardrails

| Key | Location | Type | Default | Rationale | DB? | Reason |
|---|---|---|---|---|---|---|
| `FIVE_MIN_ENABLED` | runtime_config.py:124 | bool | false | master 5m on/off | Y | behavioural toggle |
| `FIVE_MIN_ASSETS` | runtime_config.py:135 | csv | "BTC" | which assets | Y | tunable |
| `FIVE_MIN_MODE` | runtime_config.py:136 | enum | "safe" | flat/safe/degen | Y | tunable (dropdown) |
| `FIVE_MIN_ENTRY_OFFSET` | runtime_config.py:137 | int | 10 | entry trigger offset | Y | tunable |
| `FIVE_MIN_MIN_CONFIDENCE` | runtime_config.py:138 | float | 0.30 | min conviction | Y | tunable |
| `FIVE_MIN_MAX_ENTRY_PRICE` | runtime_config.py:142 | float | 0.70 | Poly price cap | Y | tunable |
| `FIFTEEN_MIN_MAX_ENTRY_PRICE` | runtime_config.py:143 | float | 0.70 | 15m price cap | Y | tunable |
| `POLY_WINDOW_SECONDS` | runtime_config.py:146 | int | 300 | window duration | Y | structural but still tunable |
| `ORDER_STAGGER_SECONDS` | runtime_config.py:156 | float | 1.5 | G1 asset stagger | Y | tunable |
| `SINGLE_BEST_SIGNAL` | runtime_config.py:158 | bool | false | G3 top-signal-only | Y | behavioural toggle |
| `MAX_ORDERS_PER_HOUR` | runtime_config.py:160 | int | 10 | G4 rate limit | Y | tunable |
| `MIN_ORDER_INTERVAL_SECONDS` | runtime_config.py:161 | float | 4.0 | G4 interval | Y | tunable |
| `DELTA_PRICE_SOURCE` | runtime_config.py:168 | enum | "tiingo" | tiingo/binance/chainlink/consensus | Y | tunable dropdown |
| `FOK_ENABLED` | runtime_config.py:174 | bool | true | FOK ladder on/off | Y | behavioural toggle |
| `TWAP_OVERRIDE_ENABLED` | runtime_config.py:180 | bool | false | TWAP override gate | Y | behavioural toggle |
| `TWAP_GAMMA_GATE_ENABLED` | runtime_config.py:184 | bool | false | TWAP gamma gate | Y | behavioural toggle |
| `TIMESFM_AGREEMENT_ENABLED` | runtime_config.py:189 | bool | false | TimesFM agreement gate | Y | behavioural toggle |
| `TIMESFM_ENABLED` | runtime_config.py:149 | bool | false | timesfm strategy master | Y | behavioural toggle |
| `TIMESFM_MIN_CONFIDENCE` | runtime_config.py:151 | float | 0.30 | timesfm min conviction | Y | tunable |
| `TIMESFM_ASSETS` | runtime_config.py:152 | csv | "BTC" | timesfm assets | Y | tunable |
| `V2_EARLY_ENTRY_ENABLED` | orchestrator.py:340 | bool | true | v2 early entry on/off | Y | behavioural toggle |
| `FIFTEEN_MIN_ENABLED` | orchestrator.py:354 | bool | false | 15m strategy master | Y | behavioural toggle |
| `FIFTEEN_MIN_ASSETS` | orchestrator.py:355 | csv | "BTC,ETH,SOL" | 15m assets | Y | tunable |
| `V9_SOURCE_AGREEMENT` | runtime_config.py:193 | bool | false | v9 agreement gate | Y | behavioural toggle |
| `V9_CAPS_ENABLED` | runtime_config.py:195 | bool | false | v9 dynamic caps on/off | Y | behavioural toggle |
| `ORDER_TYPE` | runtime_config.py:197 | enum | "FAK" | FAK/FOK/GTC | Y | tunable dropdown |
| `RECONCILER_ENABLED` | orchestrator.py:765 | bool | true | reconciler on/off | Y | behavioural toggle |
| `POLY_FILLS_SYNC_INTERVAL_S` | orchestrator.py:1664 | float | 300 | sync interval | Y | tunable |
| `POLY_FILLS_LOOKBACK_HOURS` | orchestrator.py:1665 | float | 2 | lookback window | Y | tunable |

##### 4.1.3.2 v8.1 pricing caps (inline in `five_min_vpin.py`)

| Key | Location | Type | Default | Rationale | DB? | Reason |
|---|---|---|---|---|---|---|
| `V81_CAP_T240` | five_min_vpin.py:56 | float | 0.55 | entry cap at T-240 | Y | tunable |
| `V81_CAP_T180` | five_min_vpin.py:57 | float | 0.60 | entry cap at T-180 | Y | tunable |
| `V81_CAP_T120` | five_min_vpin.py:58 | float | 0.65 | entry cap at T-120 | Y | tunable |
| `V81_CAP_T60` | five_min_vpin.py:59 | float | 0.73 | entry cap at T-60 | Y | tunable |
| `V9_CAP_EARLY` | five_min_vpin.py:1000 | float | 0.55 | v9 early cap | Y | tunable |
| `V9_CAP_GOLDEN` | five_min_vpin.py:1001 | float | 0.65 | v9 golden cap | Y | tunable |
| `V9_VPIN_EARLY` | five_min_vpin.py:1002 | float | 0.65 | v9 early VPIN gate | Y | tunable |
| `V9_VPIN_LATE` | five_min_vpin.py:1003 | float | 0.45 | v9 late VPIN gate | Y | tunable |
| `FOK_PRICE_CAP` | five_min_vpin.py:2529 | float | 0.73 | FOK max price | Y | tunable |
| `PRICE_FLOOR` | five_min_vpin.py:2531 | float | 0.30 | FOK min price | Y | tunable |
| `FOK_PI_BONUS_CENTS` | five_min_vpin.py:2540 | float | 0.0314 | FOK price bump | Y | tunable |
| `ABSOLUTE_MAX_BET` | five_min_vpin.py:3058 | float | 32.0 | hard bet cap | Y | tunable |

##### 4.1.3.3 v10/v10.6 decision surface gates (inline in `signals/gates.py`)

All of these are read at `__init__` in each gate class. CFG-07 refactors each gate to read from the RuntimeConfig singleton instead. Until that's done, they behave like `.env` keys that require a process restart on change — the UI will surface them but show a warning badge "restart required".

| Key | Location | Type | Default | Rationale | DB? | Reason |
|---|---|---|---|---|---|---|
| `V10_6_ENABLED` | gates.py:201 (EvalOffsetBoundsGate) | bool | false | V10.6 master flag | Y | behavioural toggle (DS-01) |
| `V10_6_MIN_EVAL_OFFSET` | gates.py:202 | int | 90 | V10.6 floor | Y | tunable |
| `V10_6_MAX_EVAL_OFFSET` | gates.py:203 | int | 180 | V10.6 ceiling | Y | tunable |
| `V10_MIN_DELTA_PCT` | gates.py:350 (DeltaMagnitudeGate) | float | 0.0 | global delta floor | Y | tunable |
| `V10_TRANSITION_MIN_DELTA` | gates.py:351 | float | 0.0 | transition delta floor | Y | tunable |
| `V10_DUNE_MIN_P` | gates.py:430 (DuneConfidenceGate) | float | 0.65 | base DUNE P gate | Y | tunable |
| `V10_OFFSET_PENALTY_MAX` | gates.py:431 | float | 0.06 | offset penalty max | Y | tunable |
| `V10_OFFSET_PENALTY_EARLY_MAX` | gates.py:432 | float | 0.0 | early penalty | Y | tunable |
| `V10_EARLY_ENTRY_MIN_CONF` | gates.py:433 | float | 0.90 | early entry conf | Y | tunable |
| `V10_DOWN_PENALTY` | gates.py:434 | float | 0.0 | DOWN calibration penalty | Y | tunable |
| `V10_CASCADE_MIN_CONF` | gates.py:436 | float | 0.90 | cascade conf gate | Y | tunable |
| `V10_CASCADE_CONF_BONUS` | gates.py:437 | float | 0.05 | cascade conf bonus | Y | tunable |
| `V10_TRANSITION_MIN_P` | gates.py:421 (regime) | float | 0.70 | TRANSITION regime P | Y | tunable |
| `V10_CASCADE_MIN_P` | gates.py:422 (regime) | float | 0.72 | CASCADE regime P | Y | tunable |
| `V10_NORMAL_MIN_P` | gates.py:423 (regime) | float | 0.65 | NORMAL regime P | Y | tunable |
| `V10_LOW_VOL_MIN_P` | gates.py:424 (regime) | float | 0.65 | LOW_VOL regime P | Y | tunable |
| `V10_TRENDING_MIN_P` | gates.py:425 (regime) | float | 0.72 | TRENDING regime P | Y | tunable |
| `V10_CALM_MIN_P` | gates.py:426 (regime) | float | 0.72 | CALM regime P | Y | tunable |
| `V10_MIN_EVAL_OFFSET` | gates.py:507 | int | 200 | global offset limit | Y | tunable (⚠ name collision, §6 risks) |
| `V10_NORMAL_MIN_OFFSET` | gates.py:531 | int | 0 | NORMAL offset limit | Y | tunable |
| `V10_TRANSITION_MAX_DOWN_OFFSET` | gates.py:540 | int | 0 | TRANSITION+DOWN offset cap | Y | tunable |
| `V10_DUNE_MODEL` | gates.py:612 | str | "oak" | DUNE scorer model | Y | tunable dropdown |
| `V10_DUNE_ENABLED` | five_min_vpin.py:606 / runtime_config.py:201 | bool | false | DUNE gate master | Y | behavioural toggle |
| `V10_CG_TAKER_GATE` | gates.py:716 (TakerFlowGate) | bool | false | taker gate master | Y | behavioural toggle |
| `V10_CG_TAKER_OPPOSING_PCT` | gates.py:717 | float | 55 | taker opposing pct | Y | tunable |
| `V10_CG_SMART_OPPOSING_PCT` | gates.py:718 | float | 52 | smart opposing pct | Y | tunable |
| `V10_CG_TAKER_OPPOSING_PENALTY` | gates.py:719 | float | 0.05 | taker opposing penalty | Y | tunable |
| `V10_CG_TAKER_ALIGNED_BONUS` | gates.py:720 | float | 0.02 | taker aligned bonus | Y | tunable |
| `V10_CG_MAX_AGE_MS` | gates.py:721 | int | 120000 | CG staleness cap | Y | tunable |
| `V10_CG_CONFIRM_BONUS` | gates.py:820 (CGConfirmationGate) | float | 0.03 | 2/3 confirm bonus | Y | tunable |
| `V10_CG_ZERO_CONFIRM_PENALTY` | gates.py:821 | float | 0.02 | zero confirm penalty | Y | tunable |
| `V10_CG_CONFIRM_MIN` | gates.py:822 | int | 2 | min confirms for bonus | Y | tunable |
| `V10_MAX_SPREAD_PCT` | gates.py:894 (SpreadGate) | float | 8 | Poly spread kill | Y | tunable |
| `V10_CAP_SCALE_BASE` | gates.py:1037 (DynamicCapGate) | float | 0.48 | confidence-scaled cap base | Y | tunable |
| `V10_CAP_SCALE_CEILING` | gates.py:1038 | float | 0.72 | cap ceiling | Y | tunable |
| `V10_CAP_SCALE_MIN_CONF` | gates.py:1039 | float | 0.65 | min conf for scale | Y | tunable |
| `V10_CAP_SCALE_MAX_CONF` | gates.py:1040 | float | 0.88 | max conf for scale | Y | tunable |
| `V10_DUNE_CAP_FLOOR` | gates.py:1041 | float | 0.35 | DUNE cap floor | Y | tunable |
| `V10_EARLY_ENTRY_CAP_MAX` | gates.py:1043 | float | 0.63 | early entry cap | Y | tunable |
| `V10_EARLY_ENTRY_OFFSET` | gates.py:1044 | int | 180 | early entry offset | Y | tunable |
| `FIVE_MIN_EVAL_INTERVAL` | runtime_config.py:205 | int | 10 | eval tick interval | Y | tunable |
| `V11_POLY_SPOT_ONLY_CONSENSUS` | (DQ-01, just shipped) | bool | false | spot-only consensus mode | Y | behavioural toggle |
| `TELEGRAM_ALERTS_PAPER` | settings.py:42 | bool | true | paper alert routing | Y | tunable |
| `TELEGRAM_ALERTS_LIVE` | settings.py:43 | bool | false | live alert routing | Y | tunable |

**Engine DB-managed total (existing + to-promote): ~88 keys.**

### 4.2 margin_engine — `margin_engine/infrastructure/config/settings.py`

Clean pydantic `MarginSettings` with `env_prefix="MARGIN_"`. Every field is one of:

#### 4.2.1 margin_engine — infrastructure/secrets

| Key | Location | Type | Default | Rationale | DB? | Reason |
|---|---|---|---|---|---|---|
| `MARGIN_BINANCE_API_KEY` | settings.py:24 | str | "" | Binance spot margin key | N | secret |
| `MARGIN_BINANCE_PRIVATE_KEY_PATH` | settings.py:25 | str | `/opt/.../binance_ed25519.pem` | host path to priv key | N | infrastructure + secret |
| `MARGIN_HYPERLIQUID_INFO_URL` | settings.py:62 | str | `https://api.hyperliquid.xyz/info` | HL REST base | N | infrastructure |
| `MARGIN_V4_SNAPSHOT_URL` | settings.py:78 | str | `http://3.98.114.0:8080` | fusion service URL | N | infrastructure |
| `MARGIN_DATABASE_URL` | settings.py:201 | str | "" | asyncpg conn str | N | infrastructure |
| `MARGIN_TELEGRAM_BOT_TOKEN` | settings.py:204 | str | "" | alerter bot | N | secret |
| `MARGIN_TELEGRAM_CHAT_ID` | settings.py:205 | str | "" | alerter chat | N | secret-adjacent |
| `MARGIN_STATUS_PORT` | settings.py:209 | int | 8090 | HTTP status server port | N | infrastructure |
| `MARGIN_PAPER_MODE` | settings.py:26 | bool | true | deploy-env paper/live | N | deploy-env toggle |
| `MARGIN_EXCHANGE_VENUE` | settings.py:41 | enum | "hyperliquid" | binance/hyperliquid | N | deploy-env, affects adapter wiring at boot (DQ-06) |
| `MARGIN_ALLOW_BROKEN_PAPER_BINANCE` | main.py:~84 | bool | false | emergency override | N | bootstrap safety |

#### 4.2.2 margin_engine — trading behaviour (all DB-manageable)

| Key | Location | Type | Default | Rationale | DB? | Reason |
|---|---|---|---|---|---|---|
| `MARGIN_PAPER_FEE_RATE` | settings.py:46 | float | 0.001 | Binance paper fee | Y | tunable |
| `MARGIN_PAPER_SPREAD_BPS` | settings.py:47 | float | 2.0 | Binance paper spread | Y | tunable |
| `MARGIN_HYPERLIQUID_PAPER_FEE_RATE` | settings.py:52 | float | 0.00045 | HL paper fee | Y | tunable |
| `MARGIN_HYPERLIQUID_PAPER_SPREAD_BPS` | settings.py:53 | float | 1.0 | HL paper spread | Y | tunable |
| `MARGIN_PAPER_FEE_RATE_OVERRIDE` | settings.py:58 | float? | None | explicit fee override | Y | tunable |
| `MARGIN_PAPER_SPREAD_BPS_OVERRIDE` | settings.py:59 | float? | None | explicit spread override | Y | tunable |
| `MARGIN_HYPERLIQUID_ASSET` | settings.py:63 | str | "BTC" | HL asset | Y | tunable |
| `MARGIN_HYPERLIQUID_POLL_INTERVAL_S` | settings.py:64 | float | 2.0 | HL poll cadence | Y | tunable |
| `MARGIN_HYPERLIQUID_PRICE_FRESHNESS_S` | settings.py:65 | float | 15.0 | HL staleness | Y | tunable |
| `MARGIN_ENGINE_USE_V4_ACTIONS` | settings.py:79 | bool | false | v4 gates master | Y | behavioural toggle |
| `MARGIN_V4_PRIMARY_TIMESCALE` | settings.py:80 | enum | "15m" | main timescale | Y | tunable dropdown |
| `MARGIN_V4_TIMESCALES` | settings.py:81 | csv | "5m,15m,1h,4h" | requested timescales | Y | tunable |
| `MARGIN_V4_STRATEGY` | settings.py:82 | str | "fee_aware_15m" | strategy key | Y | tunable |
| `MARGIN_V4_POLL_INTERVAL_S` | settings.py:83 | float | 2.0 | v4 poll cadence | Y | tunable |
| `MARGIN_V4_FRESHNESS_S` | settings.py:84 | float | 10.0 | v4 staleness | Y | tunable |
| `MARGIN_V4_ENTRY_EDGE` | settings.py:88 | float | 0.10 | entry conviction | Y | tunable |
| `MARGIN_V4_CONTINUATION_MIN_CONVICTION` | settings.py:89 | float | 0.10 | continuation conviction | Y | tunable |
| `MARGIN_V4_CONTINUATION_MAX` | settings.py:90 | int? | None | continuation cap | Y | tunable |
| `MARGIN_V4_MIN_EXPECTED_MOVE_BPS` | settings.py:91 | float | 15.0 | fee wall | Y | tunable |
| `MARGIN_V4_ALLOW_MEAN_REVERTING` | settings.py:92 | bool | false | allow mean-revert trades | Y | behavioural toggle |
| `MARGIN_V4_EVENT_EXIT_SECONDS` | settings.py:93 | int | 120 | force-exit window | Y | tunable |
| `MARGIN_V4_MACRO_MODE` | settings.py:114 | enum | "advisory" | veto/advisory | Y | tunable dropdown |
| `MARGIN_V4_MACRO_HARD_VETO_CONFIDENCE_FLOOR` | settings.py:115 | int | 80 | macro confidence floor | Y | tunable |
| `MARGIN_V4_MACRO_ADVISORY_SIZE_MULT_ON_CONFLICT` | settings.py:116 | float | 0.75 | advisory haircut | Y | tunable |
| `MARGIN_V4_ALLOW_NO_EDGE_IF_EXP_MOVE_BPS_GTE` | settings.py:125 | float? | None | NO_EDGE override | Y | tunable |
| `MARGIN_V4_MAX_MARK_DIVERGENCE_BPS` | settings.py:141 | float | 0.0 | DQ-07 divergence gate | Y | tunable (just shipped) |
| `MARGIN_REGIME_THRESHOLD` | settings.py:157 | float | 0.0 | v3 regime magnitude | Y | tunable |
| `MARGIN_REGIME_TIMESCALE` | settings.py:158 | enum | "1h" | regime scale | Y | tunable |
| `MARGIN_SIGNAL_THRESHOLD` | settings.py:161 | float | 0.50 | legacy signal gate | Y | tunable |
| `MARGIN_TIMESFM_WS_URL` | settings.py:151 | str | `ws://.../v3/signal` | v3 WS URL | N | infrastructure |
| `MARGIN_PROBABILITY_HTTP_URL` | settings.py:164 | str | `http://3.98.114.0:8080` | v2 HTTP URL | N | infrastructure |
| `MARGIN_PROBABILITY_ASSET` | settings.py:165 | str | "BTC" | v2 asset | Y | tunable |
| `MARGIN_PROBABILITY_TIMESCALE` | settings.py:166 | enum | "15m" | v2 timescale | Y | tunable |
| `MARGIN_PROBABILITY_SECONDS_TO_CLOSE` | settings.py:167 | int | 480 | v2 seconds offset | Y | tunable |
| `MARGIN_PROBABILITY_POLL_INTERVAL_S` | settings.py:168 | float | 30.0 | v2 poll cadence | Y | tunable |
| `MARGIN_PROBABILITY_FRESHNESS_S` | settings.py:169 | float | 120.0 | v2 staleness | Y | tunable |
| `MARGIN_PROBABILITY_MIN_CONVICTION` | settings.py:171 | float | 0.20 | v2 conviction | Y | tunable |
| `MARGIN_STARTING_CAPITAL` | settings.py:176 | float | 500.0 | paper start balance | Y | tunable |
| `MARGIN_LEVERAGE` | settings.py:177 | int | 3 | fixed leverage | Y | tunable |
| `MARGIN_BET_FRACTION` | settings.py:178 | float | 0.02 | per-trade fraction | Y | tunable |
| `MARGIN_MAX_OPEN_POSITIONS` | settings.py:181 | int | 1 | concurrent positions | Y | tunable |
| `MARGIN_MAX_EXPOSURE_PCT` | settings.py:182 | float | 0.20 | open exposure cap | Y | tunable |
| `MARGIN_DAILY_LOSS_LIMIT_PCT` | settings.py:183 | float | 0.10 | daily loss cap | Y | tunable |
| `MARGIN_CONSECUTIVE_LOSS_COOLDOWN` | settings.py:184 | int | 3 | consecutive losses | Y | tunable |
| `MARGIN_COOLDOWN_SECONDS` | settings.py:185 | int | 600 | cooldown duration | Y | tunable |
| `MARGIN_STOP_LOSS_PCT` | settings.py:186 | float | 0.006 | SL % | Y | tunable |
| `MARGIN_TAKE_PROFIT_PCT` | settings.py:187 | float | 0.005 | TP % | Y | tunable |
| `MARGIN_TRAILING_STOP_PCT` | settings.py:188 | float | 0.003 | trailing % | Y | tunable |
| `MARGIN_MAX_HOLD_SECONDS` | settings.py:189 | int | 900 | hold timeout | Y | tunable |
| `MARGIN_SIGNAL_REVERSAL_THRESHOLD` | settings.py:192 | float | -10.0 | legacy | Y | tunable |
| `MARGIN_TRADING_TIMESCALES` | settings.py:198 | csv | "15m" | trading scales | Y | tunable |
| `MARGIN_TELEGRAM_ENABLED` | settings.py:206 | bool | true | alerts on/off | Y | behavioural toggle |
| `MARGIN_TICK_INTERVAL_S` | settings.py:212 | float | 2.0 | position mgmt cadence | Y | tunable |

**margin_engine DB-managed count: ~41 keys.**
**margin_engine .env-only count: ~11 keys.**

### 4.3 hub — `hub/db/database.py::Settings` + inline

Hub itself has almost nothing to configure. The three pydantic fields are all infra:

| Key | Location | Type | Default | Rationale | DB? | Reason |
|---|---|---|---|---|---|---|
| `DATABASE_URL` | database.py:32 | str | `postgresql+asyncpg://...` | primary DB conn | N | infrastructure |
| `SECRET_KEY` | database.py:33 | str | "changeme..." | JWT signing | N | secret |
| `DEBUG` | database.py:34 | bool | false | verbose logging | N | deploy-env toggle |
| `MARGIN_ENGINE_URL` | api/margin.py:32 | str | `http://localhost:8090` | margin service URL | N | infrastructure |
| `TIMESFM_URL` | api/margin.py:33 | str | `http://localhost:8001` | timesfm service URL | N | infrastructure |
| `TRADING_APPROVAL_PASSWORD` | api/trading_config.py:374 | str | "" | live-config approve password | N | secret |

**hub DB-managed count: 0.**
**hub .env-only count: 6 keys.**

The hub is the *author* of DB-backed config — it does not consume it (except for the frontend read-through cache). CFG-09 is the cleanup task for hub: wire any future hub tunables through the shared loader, but right now there's nothing to wire.

### 4.4 data-collector — `data-collector/collector.py`, `data-collector/backfill.py`

Essentially zero env config. Reads only `DATABASE_URL`.

| Key | Location | Type | Default | Rationale | DB? | Reason |
|---|---|---|---|---|---|---|
| `DATABASE_URL` | collector.py:50 / backfill.py:34 | str | "" | asyncpg conn | N | infrastructure |

Everything else is hardcoded: `ASSETS=["BTC","ETH","SOL","XRP"]`, `TIMEFRAMES=["5m","15m"]`, `MIN_REQUEST_INTERVAL=0.25`, `POLL_INTERVAL=1`, `RESOLUTION_DELAY=30`, `GAMMA_API="https://gamma-api.polymarket.com"`, etc. (lines 37-48).

**Decision:** promote the 7 hardcoded tunables (`MIN_REQUEST_INTERVAL`, `BACKOFF_BASE`, `BACKOFF_MAX`, `POLL_INTERVAL`, `RESOLUTION_DELAY`, and the ASSETS/TIMEFRAMES lists) to DB-managed keys under service=`data-collector`. The UI gives the operator a "Data Collector" tab with 7 widgets.

**data-collector DB-managed count (after promotion): 7.**
**data-collector .env-only count: 1.**

### 4.5 macro-observer — `macro-observer/observer.py`

11 env reads, all inline at module top:

| Key | Location | Type | Default | Rationale | DB? | Reason |
|---|---|---|---|---|---|---|
| `DATABASE_URL` | observer.py:64 | str | "" | asyncpg | N | infrastructure |
| `POLL_INTERVAL` | observer.py:68 | int | 60 | loop cadence | Y | tunable |
| `QWEN_BASE_URL` | observer.py:71 | str | `http://194.228.55.129:39633/v1` | Qwen API base | N | infrastructure |
| `QWEN_API_KEY` | observer.py:72 | str | "" | Qwen bearer | N | secret |
| `QWEN_MODEL` | observer.py:73 | str | "qwen35-122b-abliterated" | model slug | Y | tunable dropdown |
| `QWEN_MAX_TOKENS` | observer.py:74 | int | 1536 | completion cap | Y | tunable |
| `QWEN_TIMEOUT_S` | observer.py:75 | float | 60 | HTTP timeout | Y | tunable |
| `ANTHROPIC_API_KEY` | observer.py:80 | str | "" | Claude fallback | N | secret |
| `ANTHROPIC_MODEL` | observer.py:81 | str | "claude-sonnet-4-6" | Claude model | Y | tunable dropdown |
| `TELEGRAM_BOT_TOKEN` | observer.py:804 | str | "" | alerts bot | N | secret |
| `TELEGRAM_CHAT_ID` | observer.py:805 | str | "" | alerts chat | N | secret-adjacent |
| `EVAL_INTERVAL` | observer.py:806 | int | 60 | eval cadence | Y | tunable |

**macro-observer DB-managed count: 6.**
**macro-observer .env-only count: 5.**

### 4.6 timesfm-service — `timesfm-service/app/*`

No env reads in Python at all — constructors hardcode `model_id="google/timesfm-2.5-200m-pytorch"`, `max_context=2048`, `max_horizon=300`, `normalize_inputs=True`, `use_continuous_quantile_head=True` (main.py:37-43). Anything passed in at runtime comes from Docker CMD args in the Dockerfile.

**Decision for v1:** read-only tab in `/config` surfacing the 5 hardcoded values via a `GET /api/v58/config/schema?service=timesfm` endpoint that the hub fills in from a small hardcoded list. No DB write path for v1. A later task (CFG-13) adds a proper loader once we have a reason to tune these at runtime.

**timesfm-service DB-managed count: 0.**
**timesfm-service .env-only count: 0.**
**timesfm-service read-only UI count: 5.**

### 4.7 Inventory totals

| Service         | .env-only | DB-managed (v1) | Read-only display only |
|-----------------|-----------|-----------------|-----------------------|
| engine          | 26        | 88              | 0                     |
| margin_engine   | 11        | 41              | 0                     |
| hub             | 6         | 0               | 0                     |
| data-collector  | 1         | 7               | 0                     |
| macro-observer  | 5         | 6               | 0                     |
| timesfm-service | 0         | 0               | 5                     |
| **TOTAL**       | **49**    | **142**         | **5**                 |

142 editable keys in the v1 `/config` page, spread across 5 service tabs. That's the target CFG-05 has to hit.

---

## 5. Proposed DB Schema

CFG-02 adds three new tables. We do **not** modify `trading_configs` on-merge. The new tables coexist with it for a transition period; CFG-10 decides whether to mothball `trading_configs` later (likely yes — it becomes a legacy "config templates" concept and the new tables become the runtime source of truth).

### 5.1 `config_keys` — schema registry

One row per (service, key) pair. Defines what the key is, its type, its default, and whether it's editable from the UI. This is the table the "schema discovery" endpoint (§7.2) reads.

```sql
CREATE TABLE IF NOT EXISTS config_keys (
  id              SERIAL PRIMARY KEY,
  service         VARCHAR(64)  NOT NULL,          -- 'engine' | 'margin_engine' | 'hub' | 'data-collector' | 'macro-observer' | 'timesfm'
  key             VARCHAR(128) NOT NULL,          -- env-var-style name, e.g. 'V10_6_ENABLED'
  category        VARCHAR(64)  NOT NULL,          -- 'risk' | 'vpin' | 'gates' | 'sizing' | 'thresholds' | 'execution' | 'infrastructure' | 'macro' | ...
  value_type      VARCHAR(16)  NOT NULL,          -- 'bool' | 'int' | 'float' | 'str' | 'enum' | 'csv'
  widget          VARCHAR(32)  NOT NULL DEFAULT 'auto',  -- 'toggle' | 'slider' | 'number' | 'dropdown' | 'text' | 'readonly' | 'auto'
  default_value   JSONB        NOT NULL,          -- the code-level default, for Reset-to-Default
  constraints     JSONB,                          -- {min, max, step, enum, pattern} — optional, widget-specific
  description     TEXT         NOT NULL,          -- long rationale (hover tooltip body)
  short_label     VARCHAR(128),                   -- human-friendly label for the row
  impact          TEXT,                           -- free-text "what this affects" (copy-paste from trading_config.py CONFIG_DEFAULTS)
  editable_via_ui BOOLEAN      NOT NULL DEFAULT TRUE,  -- false for read-only infra/secret surfacing
  restart_required BOOLEAN     NOT NULL DEFAULT FALSE, -- true for keys that are __init__-captured (gates.py refactor lags)
  env_var_name    VARCHAR(128),                   -- the .env-file equivalent, for fallback + docs
  created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  UNIQUE(service, key)
);

CREATE INDEX IF NOT EXISTS ix_config_keys_service_category
  ON config_keys (service, category);
```

Seeded at hub startup via a migration that iterates the 142 keys from §4 and INSERT-ON-CONFLICTs. The seed function lives in `hub/db/seed_config_keys.py` (CFG-02 deliverable). Whenever a new key lands in code, the developer adds a row to the seed list and redeploys the hub; the service-side loader then sees the new row and starts honouring it on next sync.

### 5.2 `config_values` — current values

One row per (service, key). This is the **current** value. Writes here are UPSERTs. Every write also appends to `config_history`.

```sql
CREATE TABLE IF NOT EXISTS config_values (
  id              SERIAL PRIMARY KEY,
  config_key_id   INTEGER      NOT NULL REFERENCES config_keys(id) ON DELETE CASCADE,
  value           JSONB        NOT NULL,          -- typed at read time using config_keys.value_type
  set_by          VARCHAR(64)  NOT NULL DEFAULT 'system',  -- JWT sub from hub/auth, or 'system' for seeds
  set_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  comment         TEXT,                           -- freeform "why I flipped this"
  UNIQUE(config_key_id)
);

CREATE INDEX IF NOT EXISTS ix_config_values_set_at
  ON config_values (set_at DESC);
```

Rationale for UNIQUE: there is exactly one "current" value per key. The history table captures everything else.

### 5.3 `config_history` — append-only audit log

Every write to `config_values` also INSERTs one row here. Never UPDATE. Never DELETE. This is the operator's rollback surface.

```sql
CREATE TABLE IF NOT EXISTS config_history (
  id              BIGSERIAL PRIMARY KEY,
  config_key_id   INTEGER      NOT NULL REFERENCES config_keys(id) ON DELETE CASCADE,
  old_value       JSONB,                          -- NULL on first-ever set
  new_value       JSONB        NOT NULL,
  set_by          VARCHAR(64)  NOT NULL,
  set_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  comment         TEXT,
  request_ip      VARCHAR(64),                    -- best-effort client IP from hub
  request_ua      VARCHAR(256),                   -- best-effort user-agent
  rolled_back_from_id BIGINT                      -- set if this row was created by a rollback from history.id
);

CREATE INDEX IF NOT EXISTS ix_config_history_key_time
  ON config_history (config_key_id, set_at DESC);
```

A rollback operation is implemented as: read `history.id`, copy its `old_value` (or `new_value` depending on direction), write a new row to `config_history` with `rolled_back_from_id=<id>` and the same `old→new` pair, and UPSERT `config_values`. Never delete history rows.

### 5.4 Relationship diagram

```
config_keys (schema)
     │ 1..1
     ▼
config_values (current)
     │ 1..N
     ▼
config_history (audit trail)
```

`config_keys` is the DDL-ish; `config_values` is the current state; `config_history` is the receipt log. The service-side loader (§6) only reads `config_keys` and `config_values`. The UI reads all three.

---

## 6. Service-Side Loader — Pseudocode (NOT implemented in this PR)

### 6.1 Shared pattern

Every service (engine, margin_engine, hub's own tunables later, macro-observer, data-collector) gets a small shim module, e.g. `engine/config/db_config_loader.py`, that owns a dict cache and exposes a `get(key, default)` function. The cache refreshes on a 10s TTL. It is thread-safe via a single-writer lock. Full pseudocode:

```python
# NOT IMPLEMENTED — this is the target shape for CFG-07..CFG-09.

class DBConfigLoader:
    """
    TTL-cached DB config reader with degrade-safe fallback.

    Lifecycle:
      - boot(service_name)           — initial load, blocking, sets cache.
      - tick()                        — called on every heartbeat by the service's main loop.
      - get(key, default)             — synchronous read from cache.

    Guarantees:
      - If DB is reachable: cache is refreshed every TTL seconds.
      - If DB is unreachable: get() returns last-known-good value OR .env fallback OR compile-time default.
      - Change events are logged (log.info) with before/after and set_by/set_at.
      - Never raises; DB errors are caught and logged at WARN.
    """

    def __init__(self, service: str, pool, env_fallback: dict, ttl_s: float = 10.0):
        self._service = service           # e.g. "engine"
        self._pool = pool                 # asyncpg pool (shared with other DB code)
        self._env_fallback = env_fallback # {"V10_6_ENABLED": "false", ...} — from settings.py/os.environ
        self._ttl = ttl_s
        self._cache: dict[str, Any] = {}
        self._last_sync: float = 0.0
        self._last_ok_cache: dict[str, Any] = {}   # used as fallback on DB error
        self._lock = asyncio.Lock()

    async def boot(self):
        """Initial blocking load at service start. Populates cache from DB, or
        .env fallback if DB unreachable. After this returns, get() is safe to
        call from sync code."""
        ok = await self._refresh()
        if not ok:
            log.warning("db_config.boot_fallback",
                        service=self._service,
                        reason="db_unreachable",
                        cache_size=len(self._cache))
            # seed cache from env fallback so first-tick get() returns something
            self._cache = dict(self._env_fallback)

    async def tick(self):
        """Called from the service heartbeat (engine: ~10s, margin_engine: 2s).
        Refreshes cache if TTL elapsed. Non-blocking on DB errors."""
        now = time.monotonic()
        if now - self._last_sync < self._ttl:
            return
        await self._refresh()

    async def _refresh(self) -> bool:
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT k.key, k.value_type, v.value, v.set_by, v.set_at
                    FROM config_keys k
                    LEFT JOIN config_values v ON v.config_key_id = k.id
                    WHERE k.service = $1 AND k.editable_via_ui = TRUE
                """, self._service)
            new_cache = {}
            for r in rows:
                if r["value"] is None:
                    # no current value set — use code default (from env fallback or key default in config_keys table)
                    new_cache[r["key"]] = self._env_fallback.get(r["key"])
                else:
                    new_cache[r["key"]] = _coerce(r["value"], r["value_type"])
            # log diffs
            for key, new_val in new_cache.items():
                old_val = self._cache.get(key)
                if old_val != new_val:
                    log.info("db_config.changed",
                             service=self._service,
                             key=key,
                             old=old_val,
                             new=new_val)
            self._cache = new_cache
            self._last_ok_cache = dict(new_cache)
            self._last_sync = time.monotonic()
            return True
        except Exception as exc:
            log.warning("db_config.refresh_failed",
                        service=self._service, error=str(exc))
            # KEEP the cache we had — degrade safe, never fail-closed
            return False

    def get(self, key: str, default=None):
        """Synchronous read. Reads from cache. Falls back to env_fallback,
        then to the explicit default, in that order. Never raises."""
        if key in self._cache:
            return self._cache[key]
        if key in self._env_fallback:
            return self._env_fallback[key]
        return default
```

### 6.2 Per-service integration

#### 6.2.1 engine

- `engine/config/runtime_config.py` is **already** a singleton with a `.sync()` method. Phase 1 (CFG-07) evolves it: keep the existing `_DB_KEY_MAP` path for backward compat, but add a parallel read-through path to `config_values` via the new loader. Phase 2 (CFG-07 cont.) swaps the singleton's primary source from `trading_configs.config` JSONB to `config_values`. The `runtime` singleton keeps the same public attribute surface so downstream code (`five_min_vpin.py`, `orchestrator.py`, etc.) doesn't change.
- `engine/signals/gates.py` — §6.3 below. This is the hard part.
- **Fallback path:** if DB unreachable, the singleton keeps its last-known-good values, which were themselves loaded from `.env` at module import time. This is exactly what the current `sync()` does today when it gets an exception (see `runtime_config.py:320-323`).

#### 6.2.2 margin_engine

- `margin_engine/infrastructure/config/settings.py` is pydantic. Phase 1 (CFG-08) adds a `DBConfigLoader` field on `MarginSettings` and exposes properties like `@property def v4_entry_edge(self) -> float: return self._db.get("V4_ENTRY_EDGE", default=self._v4_entry_edge_env)`. Downstream code that reads `settings.v4_entry_edge` gets the DB value if present, the env value otherwise.
- The main loop ticks every `tick_interval_s` (default 2s). DBConfigLoader.tick() is called first thing each iteration.

#### 6.2.3 hub

- Phase 1 (CFG-09) — **self-referential and tricky**. The hub both reads config (for its own tunables) and writes config (for other services). The read path must not depend on any DB-managed hub tunable, because a broken write would cascade into the hub's own loader. Concretely: `DATABASE_URL`, `SECRET_KEY`, `DEBUG`, `TRADING_APPROVAL_PASSWORD`, `MARGIN_ENGINE_URL`, `TIMESFM_URL` all stay in `.env` permanently. If the hub grows a DB-managed tunable later (e.g. CORS allowlist, rate limit), it reads it via the same loader but only AFTER init_db() has succeeded — see §7 open questions.

#### 6.2.4 macro-observer

- Phase 1 (CFG-10 — grouped into the cutover task since macro-observer touches are small). Replace the top-of-file `os.environ.get` reads with `loader.get("POLL_INTERVAL", default=60)` etc. Main loop is already a `while True: sleep(POLL_INTERVAL)`; add a `loader.tick()` call.

#### 6.2.5 data-collector

- Phase 1 (CFG-10). Same pattern as macro-observer. `POLL_INTERVAL=1s` is fast enough that tick-every-loop is fine.

#### 6.2.6 timesfm-service

- Out of scope for v1 beyond the read-only schema surface.

### 6.3 The gates.py problem

`engine/signals/gates.py` has 9 gate classes, and 8 of them read env vars in `__init__`. That means the RuntimeConfig singleton picking up a DB change does not propagate to the gates — they've already captured the old value into `self._max_spread_pct` etc. at pipeline construction time.

Three options for CFG-07:

1. **Rebuild the gate stack on each DB diff.** Cheap if the gate pipeline is a short-lived object. Currently it is NOT — gates are constructed once in the strategy's `__init__` and reused across windows. Refactoring to rebuild means threading the RuntimeConfig down into the strategy constructor, which is a ~200-line diff. Possible but risky.
2. **Make every gate read `runtime.get(...)` at `evaluate()` time instead of `__init__` time.** Cleaner, small per-gate diff, but ~9 files to touch and every gate test needs updating. 2-day job.
3. **Mark the gate keys `restart_required=TRUE` in `config_keys` and surface a warning badge in the UI.** This is the ship-it-now option. Phase 1 ships the keys as read-only-in-cache / restart-required; Phase 2 refactors one gate at a time to be hot-reloadable; Phase 3 flips `restart_required=FALSE` as each gate lands.

**Recommended:** option 3 for Phase 1, option 2 for Phase 2+. CFG-07 ships with `restart_required=TRUE` set on all gate keys (V10_*, V10_6_*, V11_*) and `restart_required=FALSE` on the runtime_config.py keys (which are already hot-reloadable). The UI shows a small "restart required after edit" badge next to restart-required keys. A follow-up task (CFG-07b) does the per-gate hot-reload refactor.

### 6.4 Startup order

```
┌─────────────────────────────────────────────────────────────────┐
│ service start                                                   │
│   1. read .env into os.environ (load_dotenv())                  │
│   2. parse pydantic Settings — needs DATABASE_URL               │
│   3. asyncpg pool connect                                       │
│   4. DBConfigLoader(service=SVC, pool=pool, env_fallback=...)   │
│   5. await loader.boot()                                        │
│      ├── success: cache = DB values, rest of boot uses cache    │
│      └── fail: cache = env_fallback, WARN log, boot continues   │
│   6. wire loader into RuntimeConfig / MarginSettings / etc      │
│   7. main loop starts; loader.tick() called every iteration     │
└─────────────────────────────────────────────────────────────────┘
```

The loader must boot AFTER the asyncpg pool but BEFORE the strategy/use-case wiring. This is straightforward in engine (slot in between `_db` init and `FiveMinVPIN(...)` construction) and margin_engine (slot in `main.py::run_live_loop` before `UseCases(...)` wiring).

---

## 7. Hub API Surface

CFG-03 ships read endpoints. CFG-04 ships write + history endpoints. All under the existing `hub/api/v58_monitor.py` router (or a new `hub/api/config_v2.py` router if we want to isolate — my recommendation is a new file to keep v58_monitor focused on trading monitoring). Prefix all routes under `/api/v58/config/*` for consistency with the existing pattern.

### 7.1 Read endpoints (CFG-03)

#### `GET /api/v58/config?service={service}`

Returns the full tree of current values for a service. Merges `config_keys` + `config_values`.

Response:

```json
{
  "service": "engine",
  "generated_at": "2026-04-11T14:23:45Z",
  "categories": [
    {
      "id": "risk",
      "label": "Risk Management",
      "keys": [
        {
          "key": "BET_FRACTION",
          "value": 0.025,
          "default_value": 0.025,
          "value_type": "float",
          "widget": "slider",
          "constraints": {"min": 0.01, "max": 0.20, "step": 0.005},
          "description": "Kelly fraction...",
          "short_label": "Bet Fraction",
          "set_by": "system",
          "set_at": "2026-04-10T09:12:00Z",
          "restart_required": false,
          "is_at_default": true
        },
        ...
      ]
    }
  ]
}
```

#### `GET /api/v58/config/schema?service={service}`

Returns just the schema (no values). Used for form initialisation before the user touches anything. Cacheable for 60s on the frontend.

#### `GET /api/v58/config/history?service={service}&key={key}&limit=50`

Returns the last N rows from `config_history` for a given key. Response is a list of `{old_value, new_value, set_by, set_at, comment, request_ip, rolled_back_from_id}`. Used by the per-key history drawer in the UI.

#### `GET /api/v58/config/services`

Returns the list of services that have any DB-managed keys, for sidebar population. Response: `[{"id": "engine", "label": "Engine (Polymarket 5m)", "key_count": 88, "last_changed": "..."} , ...]`.

### 7.2 Write endpoints (CFG-04)

#### `POST /api/v58/config/upsert`

Body:
```json
{
  "service": "engine",
  "key": "V10_6_ENABLED",
  "value": true,
  "comment": "enabling per Montreal audit checklist DS-01"
}
```

Behaviour:
1. Auth check via `auth/middleware.py::get_current_user` (CFG-06 will later restrict to an `admin` claim).
2. Look up `config_keys.id` by `(service, key)`. 404 if missing.
3. Coerce `value` against `config_keys.value_type` + `constraints`. 422 on violation.
4. In one transaction:
   - UPSERT `config_values` (`config_key_id` is unique).
   - INSERT `config_history` with `old_value=<prev>, new_value=<new>, set_by=<jwt.sub>, request_ip=<header>, request_ua=<header>, comment=<body.comment>`.
5. Return the updated row in the same shape as the `GET /api/v58/config` per-key element.

#### `POST /api/v58/config/rollback`

Body:
```json
{
  "history_id": 12345,
  "comment": "reverting DS-01 flip, too early"
}
```

Behaviour: read `config_history.id=12345`, take its `old_value`, UPSERT it back into `config_values`, INSERT a new history row with `rolled_back_from_id=12345`. Protects against re-rollback loops via the `rolled_back_from_id` link.

#### `POST /api/v58/config/reset`

Body: `{"service": "engine", "key": "V10_6_ENABLED", "comment": "..."}`

Behaviour: read `config_keys.default_value`, UPSERT `config_values` to the default, INSERT history row with `new_value=default`. Functionally equivalent to a user clicking the "Reset to default" button in the UI.

### 7.3 Error handling

- All write endpoints wrap the SQL in a transaction. If `config_history` INSERT fails, `config_values` UPSERT is rolled back — we MUST never have a silent write without an audit row.
- All endpoints log to structlog at `log.info("config.upsert", ...)` / `log.warning("config.coercion_failed", ...)`.
- Rate limiting via the existing hub middleware (whatever it currently uses for auth).

### 7.4 Deprecation path for legacy endpoints

- `/api/config` (hub/api/config.py `GET`/`PUT`) — mark DEPRECATED in PR body, remove in CFG-11.
- `/api/trading-config/*` (hub/api/trading_config.py) — keep during Phase 1/2 for backward compat with existing `TradingConfig.jsx`. CFG-06 replaces `TradingConfig.jsx` with a redirect to `/config`, at which point trading_config CRUD routes become legacy. CFG-11 removes them or reduces them to read-only aliases for the new API.

---

## 8. Frontend UX

### 8.1 Navigation

- Add a new top-level sidebar entry: **CONFIG** (under the SYSTEM group, next to "Deployments").
- Route: `/config` (the existing `/config → /trading-config` redirect in App.jsx:67 is removed in CFG-06).
- Old `/trading-config` route becomes a redirect back to `/config?service=engine` to preserve any bookmarks.

### 8.2 Page layout (ASCII mockup)

```
┌──────────────────────────────────────────────────────────────────────┐
│ Config · /config?service=engine                  [○ ACTIVE] [SAVED]  │
├──────────────────┬───────────────────────────────────────────────────┤
│                  │ § Risk Management                           [▼]   │
│ ▸ engine     88  │ ┌───────────────────────────────────────────────┐ │
│ ▸ margin_engine41│ │ Bet Fraction (Kelly)             [slider]     │ │
│   hub         0  │ │ ───●───────────── 0.025 · default 0.025       │ │
│   data-collector7│ │ ⓘ Kelly fraction per trade. Higher=aggressive │ │
│ ▸ macro-observer6│ │ Last changed: 2026-04-10 by billybrichards    │ │
│   timesfm (RO) 5 │ │ [Reset] [History] [Save]                      │ │
│                  │ └───────────────────────────────────────────────┘ │
│ filter: [____] │ │ Starting Bankroll                [slider]         │
│                │ │ ... 28 more risk keys                             │
│ ┌────────────┐ │ │                                                   │
│ │Admin only  │ │ § VPIN Thresholds                          [▼]     │
│ │□ edit mode │ │ § Gates (v10/v10.6)                        [▼]     │
│ │            │ │ § Execution & Sizing                       [▼]     │
│ │            │ │ § Strategy Toggles                         [▼]     │
│ │            │ │ § Fees & Venues                            [▼]     │
│ └────────────┘ │                                                     │
└──────────────────┴───────────────────────────────────────────────────┘
```

**Left sidebar** is a simple service list with key counts. The `▸` markers show which service the user is currently in. `timesfm (RO)` has a "read-only" badge.

**Main pane** is grouped by `category` (from `config_keys.category`) into collapsible sections. Each section has a header with the category label + key count + collapse arrow. Sections open by default on first visit; user's collapse state persists in localStorage.

### 8.3 Per-key row mockup

```
┌─────────────────────────────────────────────────────────────────┐
│ V10_6_ENABLED                          ⚠ restart required      │
│ Enable v10.6 decision surface (DS-01)                           │
│ ─────────────────────────────                                   │
│ [OFF]─────[●]  ON                        default: OFF           │
│                                                                 │
│ ⓘ V10.6 master flag. Enables EvalOffsetBoundsGate with          │
│   [V10_6_MIN_EVAL_OFFSET, V10_6_MAX_EVAL_OFFSET] bounds.        │
│   Evidence: 865-outcome study in                                │
│   docs/V10_6_DECISION_SURFACE_PROPOSAL.md §3.4                  │
│                                                                 │
│ Last changed: 2026-04-10T14:23 by billybrichards               │
│    old: false → new: true                                       │
│    comment: "enabling per DS-01 audit task"                     │
│                                                                 │
│ [ ↺ Reset to default ]  [ ⏱ History ]  [ ✓ Save change... ]    │
└─────────────────────────────────────────────────────────────────┘
```

Widgets per `value_type`:
- `bool` → toggle (current UI style, `ConfigWidget.jsx`)
- `int` (with `min`/`max`) → slider + number input
- `float` (with `min`/`max`) → slider + number input with step
- `str` (with `enum`) → dropdown
- `str` (without enum) → text field
- `csv` → text field with parsing preview ("current list: BTC, ETH, SOL")
- `readonly` → greyed-out value display with a small "this is hardcoded in source" tag

Reuse `frontend/src/components/ConfigWidget.jsx` which already handles 4 of the 6 widget types.

### 8.4 History drawer

Clicking the ⏱ History button on a row opens a right-side drawer:

```
┌─────────────────────────────────────────────┐
│ History: V10_6_ENABLED               [close]│
├─────────────────────────────────────────────┤
│ 2026-04-10 14:23  billybrichards           │
│   false → true                              │
│   "enabling per DS-01 audit task"           │
│   IP: 1.2.3.4 · UA: Claude Code            │
│   [↶ Rollback to this]                      │
│ ─────────────────────────────               │
│ 2026-04-08 09:11  system                    │
│   (initial set from seed)                   │
│   false                                     │
│ ─────────────────────────────               │
│ ...                                         │
└─────────────────────────────────────────────┘
```

Each history row has a Rollback button (CFG-06 gates it behind admin-only). Rollback calls `POST /api/v58/config/rollback` and reloads the drawer.

### 8.5 Phase 1 is read-only

Per CFG-05, the first shipping version of `/config` is **read-only**. All "Save" buttons are disabled with a tooltip: "read-only until CFG-06 lands". This is the zero-risk foot-in-the-door: the operator can inspect the full config surface from the UI before anyone touches a write path. CFG-06 flips the write path on after a period of read-only burn-in.

---

## 9. Migration Phasing

### Phase 0 — Build (CFG-02, CFG-03)

**Ships:** new DB tables + seed migration + hub read endpoints.

- CFG-02 (DDL + seed): add `config_keys`/`config_values`/`config_history` tables via `hub/db/migrations/` (or inline in `hub/main.py::lifespan` like the existing pattern). Seed `config_keys` with all 142 keys from §4. Set `config_values` to the current default for each. Do NOT touch any existing code paths.
- CFG-03 (read API): add `hub/api/config_v2.py` with the 4 GET endpoints from §7.1. Register in `hub/main.py` after existing routers.
- **No service-side loader yet. No frontend changes yet. No behavioural effect in production.**

Deploy: standard hub rollout. Verify by hitting `GET /api/v58/config?service=engine` and confirming the response mirrors §4.1.

### Phase 1 — Read-only observe (CFG-04, CFG-05)

**Ships:** write API + read-only frontend + read-through loaders in shadow mode.

- CFG-04 (write API): implement `POST /api/v58/config/upsert`, `/rollback`, `/reset`. Still no user-facing write path — these endpoints exist but the frontend calls only GET.
- CFG-05 (read-only frontend): new `/config` page, sidebar entry, all widgets disabled. Operator can browse full config surface and see history but cannot edit.
- Service-side loaders are NOT wired yet. Services continue to run from `.env` exactly as today.

Goal of Phase 1: operator spends 2-3 days clicking around the read-only UI, verifying that what the UI shows matches what engine/margin_engine is actually running (cross-reference against `system_state.config` snapshot from the heartbeat). Any mismatches surface DB-seed bugs BEFORE anyone tries to write.

### Phase 2 — Dual-read per service (CFG-07, CFG-08, CFG-10)

**Ships:** `DBConfigLoader` wiring per service + `/config` write enabled + cutover per service.

Cutover is service-by-service, not all-at-once:

1. CFG-07 — engine:
   - Wire `DBConfigLoader(service="engine", ...)` into `orchestrator.__init__`.
   - Replace `runtime_config.py::sync()` internals to read from `config_values` + `config_keys` instead of `trading_configs.config` JSONB. Keep the existing `SKIP_DB_CONFIG_SYNC` flag semantics intact — when set, both the new loader AND the legacy sync path are no-ops.
   - Deploy to prod with `SKIP_DB_CONFIG_SYNC=true` (current state). Observe that the new loader ticks without error. No behaviour change yet.
   - Flip `SKIP_DB_CONFIG_SYNC=false` ONCE on a low-traffic window, monitor, rollback by re-flipping if anything misbehaves.
   - Re-enable writes in the UI (CFG-06 step).
2. CFG-08 — margin_engine: same dance on the London host. Margin engine is still paper-only so risk is lower than engine.
3. CFG-10 — macro-observer + data-collector: tiny surface, low risk, can ship in one PR after CFG-07 lands.
4. CFG-09 — hub: only wire a loader when the hub grows its first DB-managed tunable. For now it's a no-op task that exists to track the self-referential risk.

At the end of Phase 2, DB is the authoritative source for all 142 keys. `.env` still contains them as fallback.

### Phase 3 — Cleanup (CFG-11)

**Ships:** `.env` gets trimmed, inline `os.environ.get` reads deleted, docs updated.

- Remove inline `os.environ.get` calls for any key that is now DB-managed. Each call becomes `runtime.get("KEY")` (or `settings.key` for margin_engine).
- `engine/config/runtime_config.py` `__init__` is no longer responsible for defaults — the `DBConfigLoader` owns them. The singleton keeps its public attribute surface but is backed by the loader.
- Update `docs/CONFIG.md` to document the new flow and retire the "Recommended: Edit .env on Montreal" section.
- Deprecate `hub/api/config.py` (the 13-key mini-API). Remove after two weeks of no frontend calls.
- `trading_configs` table stays in place but becomes a legacy "config presets" concept — CFG-12 (out of scope) decides whether to mothball it entirely.

---

## 10. Risk Matrix

### 10.1 Hot-reload risk for trading gates mid-window

**Scenario:** operator flips `V10_6_MIN_EVAL_OFFSET` from 90 to 120 at T-100 (mid-window). Current window is using 90; the gate re-reads 120 on its next `evaluate()` call and skips a trade it would have taken under the old value.

**Mitigation:**
- In Phase 1, all `V10_*` keys have `restart_required=TRUE` in `config_keys`, so the UI shows a warning and the service-side loader DOES NOT refresh those into live gate instances until the next Python process restart. The operator sees the change persist in the DB but it doesn't take effect until they manually bounce the engine. This is worse UX but zero correctness risk.
- In Phase 2b (CFG-07b), gate classes are refactored to read from `runtime.get(...)` at `evaluate()` time. Each gate becomes explicit about whether it's window-boundary safe (only applies at window start) or tick safe (applies every tick). The UI surfaces this per-key.
- For the window-boundary-only gates, the DBConfigLoader exposes `freeze_for_window(start_ts, end_ts)` which snapshots the cache at window start; gates read from the snapshot for the duration of the window; the live cache continues to refresh underneath. Next window picks up the new value cleanly.

### 10.2 DB outage risk (services must degrade gracefully — do NOT fail-closed)

**Scenario:** hub's Postgres is unreachable for 5 minutes. Engine heartbeat `loader.tick()` starts failing.

**Behaviour:**
- `DBConfigLoader._refresh()` catches `asyncpg.exceptions.*`, logs at WARN, returns `False`. The cache is NOT touched — engine continues running with last-known-good values.
- A `db_config.stale` gauge is exported to whatever metrics sink (hub /api/system or Telegram) shows "DB config hasn't refreshed in N seconds". Operator sees the alert but nothing is broken.
- Trading is NOT halted because of stale config. Fail-closed-on-DB-error was a past bug in this codebase (see `docs/CONFIG.md` history) and we are NOT repeating it.
- If the service restarts during the DB outage, `boot()` falls back to `.env` values via `env_fallback`. This is why the `.env` file stays around as a secondary source even after Phase 3.

### 10.3 Race conditions between parallel config writes

**Scenario:** two operators (or an operator + a scheduled task) POST `upsert` for the same key within 100ms.

**Behaviour:**
- `config_values` UPSERT uses `ON CONFLICT (config_key_id) DO UPDATE` — last write wins, both audit rows land in `config_history`. `set_at` timestamps reconstruct ordering.
- In the UI, the frontend fetches the latest value before enabling the Save button and includes an `If-Unchanged-Since: <set_at>` header (or body field) on POST. Hub checks the current `set_at` against the header; if they differ, returns `409 Conflict` with "this key was changed by <other_user> at <ts> — reload to see latest". This is optimistic concurrency.
- Phase 1 ships without the conflict check (read-only anyway). CFG-06 adds the check when enabling writes.

### 10.4 Secret rotation — NEVER DB-managed

**Restated explicitly because this is the #1 risk if someone takes a shortcut:**

Secrets are NEVER in `config_values` or `config_keys` or any other DB table in this plan. Every `config_keys` seed row for a service must have `editable_via_ui=TRUE` → that key MUST NOT be a secret. The seed migration has a hard gate: any seed entry whose `key` matches `.*_(API_KEY|SECRET|TOKEN|PASSWORD|PRIVATE_KEY|PASSPHRASE|FUNDER_ADDRESS|WALLET_KEY)$` causes the migration to abort.

Secret rotation workflow remains unchanged: operator SSH's to the host, `nano .env`, `systemctl restart` (or the python-kill-and-relaunch shell dance for engine). We are explicitly NOT solving secret rotation in this plan.

### 10.5 Bootstrap problem

**Scenario:** operator accidentally flips `SKIP_DB_CONFIG_SYNC=true` via the UI. This bricks the sync path on next boot because the loader never reads DB values.

**Behaviour:**
- `SKIP_DB_CONFIG_SYNC` is excluded from the `config_keys` seed — it lives ONLY in `.env` and NEVER in DB. §2.3 rule 3.
- The same applies to `DATABASE_URL` (can't connect to DB without this key, obviously), and to any `MARGIN_DATABASE_URL`, `ENGINE_DATABASE_URL` equivalents.
- The seed migration asserts this at hub startup: scanning the seed list for any `key` in the bootstrap list and aborting if one slipped in.

### 10.6 `V10_MIN_EVAL_OFFSET` name collision

The existing code already has a name-collision footgun. `engine/signals/gates.py:507` reads `V10_MIN_EVAL_OFFSET` and uses it as a **maximum** offset (in prod, set to 180 or 200). `engine/config/runtime_config.py:213-227` and `engine/signals/gates.py:202` read `V10_6_MIN_EVAL_OFFSET` with namespaced prefix for the same-concept minimum in the new v10.6 gate. A naive DB migration that deduplicates these at seed time would silently break production.

**Mitigation:** CFG-02 seed migration treats every env var name as a unique `(service, key)` tuple. The collision is documented in the `description` field of each key so the operator reading the UI sees the warning inline. A follow-up audit task (CFG-07c, out of scope) consolidates the two into one name once all v10.6 components land.

### 10.7 Inconsistent `trading_configs` vs `config_values` during Phase 1/2

**Scenario:** during Phase 1, `trading_configs.config` JSONB and `config_values` rows diverge because an operator flips a flag in one and forgets the other.

**Mitigation:**
- Phase 1 writes to `config_values` go through `POST /api/v58/config/upsert`, which ALSO mirrors the write into `trading_configs.config` JSONB for any key that already exists there (the 25 keys in §4.1.2). This is a backward-compat shim; the service-side loader continues reading `trading_configs` in Phase 1.
- Phase 2 (CFG-07 cutover) flips the primary read source. At this point we optionally stop mirroring into `trading_configs` — but the safer choice is to keep mirroring for one more phase in case we need to rollback to reading from `trading_configs`. CFG-10 makes the mirror optional after Phase 2 is stable for 7 days.

---

## 11. Frontend Audit — Existing Pages

Walk-through of every frontend page against "does DB-backed config change this?":

### 11.1 `/execution-hq` (ExecutionHQ.jsx)

- Already reads live engine state from `system_state.config` via `GET /api/system/status`. CFG-07 changes what's IN that blob but not HOW the page reads it. **No frontend change needed.**
- `GateHeartbeat.jsx` (the section just shipped in UI-01) reads per-gate status from the orchestrator heartbeat. As gates migrate to reading from DB config, GateHeartbeat's display values implicitly become DB-sourced. **No change needed.**
- Recommendation: add a small link in the ExecutionHQ header: "⚙ Configure engine" → `/config?service=engine`. Tracked as a sub-task of CFG-05.

### 11.2 `/deployments` (Deployments.jsx)

- Static service registry. Each service card lists `secretsNeeded` — this should NOT be displayed from DB since it's documentation. **No change needed.**
- Recommendation: add a "config keys managed: N" chip next to each service, linking to `/config?service=<id>`. Sub-task of CFG-05.

### 11.3 `/audit` (AuditChecklist.jsx)

- Fully static data. **No change needed** (other than adding the CFG-01..CFG-11 tasks, §12).

### 11.4 `/notes` (Notes.jsx)

- Persistent journal backed by `notes` DB table (NT-01). Unrelated to config. **No change needed.**
- Recommendation: a `config:<service>:<key>` tag convention on notes so the operator can mention "saw vpin_gate flipped from 0.45 → 0.50 → bad idea, reverted" in the notes and cross-ref. No code change, just a convention.

### 11.5 `/data/v1..v4` (V1Surface..V4Surface)

- Read-only data surfaces. They display values FROM config (gate thresholds, regime bounds, etc.) but do not edit them. CFG-07 changes the source of those values but the display components stay the same. **No change needed.**
- Recommendation: for each row that displays a config-sourced threshold, add a small ⚙ link back to `/config?service=engine&key=<KEY>` so operator can jump from surface → config in one click. Sub-task of CFG-05.

### 11.6 `/margin` (MarginEngine.jsx + V4Panel.jsx)

- Renders margin_engine state from `GET /api/margin/*`. Reads thresholds from the engine's own status endpoint. **No change needed** for the display. Add a link from the margin page header to `/config?service=margin_engine`. Sub-task of CFG-05.

### 11.7 Stale / legacy pages flagged for retirement

The following pages overlap with what `/config` will provide and should be considered for retirement in CFG-11:

- **`/config` old page (`frontend/src/pages/Config.jsx`, 155 lines):** currently routes to `/trading-config` via `App.jsx:67`. It was the minimal 13-key editor. It is superseded by CFG-05's `/config` page. **Retire after CFG-06.**
- **`/trading-config` (TradingConfig.jsx, 1373 lines):** the current full-featured config page. Superseded by CFG-05's `/config` page, which handles 142 keys instead of 25. **Retire after CFG-06** — leave a redirect from `/trading-config` → `/config?service=engine` for bookmarks.
- **`/setup` (Setup.jsx):** historically held the API-key setup flow. In the DB-config world, secrets remain host-side so setup.jsx is STILL the right tool for the host-side `.env` walkthrough, but it should be re-scoped and re-labelled "Secrets & Infrastructure" to make the split explicit. Tracked as CFG-11c, follow-up.
- **`/live` (LiveTrading.jsx):** partial overlap with ExecutionHQ. Not a config page per se, but has hardcoded threshold display values. Scan in CFG-11 to confirm none of its displayed thresholds are stale once CFG-07 lands.

The following pages should gain a "⚙ configure" link in their header pointing to the relevant `/config?service=...` tab (grouped sub-task of CFG-05):

- ExecutionHQ → engine
- MarginEngine → margin_engine
- V1Surface..V4Surface → engine (with `&key=...` deep-link)
- Deployments → matching service per card

---

## 12. Audit Checklist Task Updates

Add to `frontend/src/pages/AuditChecklist.jsx::TASKS` under a new category `'config-migration'` with color `T.cyan` and description "Full migration of runtime configuration from .env files to a DB-backed store with hot-reload, audit trail, and UI editor. Tracked in docs/CONFIG_MIGRATION_PLAN.md (this file)."

| ID      | Title                                                                       | Severity | Status      | Depends on |
|---------|-----------------------------------------------------------------------------|----------|-------------|------------|
| CFG-01  | Full DB-backed config migration plan (this doc)                             | MEDIUM   | DONE        | —          |
| CFG-02  | config_keys + config_values + config_history DB schema + seed migration     | HIGH     | OPEN        | CFG-01     |
| CFG-03  | hub /api/v58/config* read endpoints (GET schema/values/history/services)    | HIGH     | OPEN        | CFG-02     |
| CFG-04  | hub /api/v58/config POST upsert/rollback/reset + history append             | HIGH     | OPEN        | CFG-03     |
| CFG-05  | frontend /config page (read-only first) with widgets + history drawer       | HIGH     | OPEN        | CFG-03     |
| CFG-06  | frontend /config editable (admin only) + optimistic concurrency check       | HIGH     | OPEN        | CFG-04, CFG-05 |
| CFG-07  | engine service-side DBConfigLoader with TTL cache + safe degrade            | CRITICAL | OPEN        | CFG-04     |
| CFG-07b | engine gates.py hot-reload refactor (remove __init__-capture)               | HIGH     | OPEN        | CFG-07     |
| CFG-08  | margin_engine service-side DBConfigLoader wiring                            | HIGH     | OPEN        | CFG-04     |
| CFG-09  | hub service-side loader (self-referential — tricky, mostly no-op for v1)    | MEDIUM   | OPEN        | CFG-04     |
| CFG-10  | migration cutover per service (flip SKIP_DB_CONFIG_SYNC; include macro+data)| CRITICAL | OPEN        | CFG-07, CFG-08 |
| CFG-11  | frontend audit: retire legacy /config + /trading-config; add cross-links    | MEDIUM   | OPEN        | CFG-10     |
| CFG-11c | relabel /setup as "Secrets & Infrastructure"                                | LOW      | OPEN        | CFG-11     |

All tasks go into AuditChecklist.jsx TASKS array with `category: 'config-migration'`. File-path citations per task:
- CFG-02 → `hub/main.py`, `hub/db/seed_config_keys.py` (new)
- CFG-03 → `hub/api/config_v2.py` (new)
- CFG-04 → `hub/api/config_v2.py`
- CFG-05 → `frontend/src/pages/config/` (new dir)
- CFG-06 → same
- CFG-07 → `engine/config/runtime_config.py`, `engine/config/db_config_loader.py` (new)
- CFG-07b → `engine/signals/gates.py`
- CFG-08 → `margin_engine/infrastructure/config/db_config_loader.py` (new), `margin_engine/main.py`
- CFG-09 → `hub/services/db_config_loader.py` (new)
- CFG-10 → .env on Montreal + London hosts; github-actions/deploy-*.yml for env templating
- CFG-11 → `frontend/src/App.jsx`, `frontend/src/pages/Config.jsx`, `frontend/src/pages/TradingConfig.jsx`

---

## 13. Open Questions (to resolve before CFG-02 lands)

1. **Scope the hub Postgres instance choice.** Right now hub runs on Railway + its own PG (DEP-02 is migrating hub → AWS Montreal but that's not merged). `config_keys`/`config_values`/`config_history` live alongside `trading_configs` on whichever DB the hub points at. When hub migrates to AWS, these tables migrate with it. Confirm with operator that a cutover plan exists and the new DB is provisioned with `pgcrypto` / extensions needed. My assumption: none needed, pure asyncpg-native JSONB.

2. **Who gets edit permission in CFG-06?** The simplest cut is "any authenticated hub user". A stricter cut is "users with an admin JWT claim". CFG-06 can't ship edit mode until the operator decides. If the answer is "everyone", we ship CFG-06 tomorrow; if the answer is "admin claim", we need to wire the claim into `hub/auth/jwt.py` first.

3. **How aggressive is the TTL?** I proposed 10s because that matches the existing engine heartbeat cadence. margin_engine ticks at 2s so could go faster. Operator preference: 2s for everything, or per-service TTL?

4. **Do we keep `trading_configs` as a "preset bundle" concept?** The current paper/live-config + approval workflow is real product — it's how the operator bundles a set of values for "Paper Config v7.1" vs "Live Config v7.1" and approves them together. The new per-key model doesn't have a bundle concept. Options: (a) keep `trading_configs` as presets, build a "load preset into current values" action in the UI; (b) mothball `trading_configs` and move to per-key version tags; (c) keep both indefinitely. My recommendation: (a). Needs operator confirmation.

5. **Should `config_values.value` have a versioned-schema path?** If we change a key's `value_type` from `float` to `int`, historical rows in `config_values` with the old type break the loader. Simpler answer: disallow value_type changes — key renames instead. Confirm.

6. **timesfm-service read-only surface — where does the data come from?** I proposed "hardcoded list in hub". Alternative: timesfm-service grows a `GET /v4/config` endpoint that the hub proxies. Second option is cleaner long-term but requires a novakash-timesfm-repo PR. CFG-05 can ship with the first option and CFG-13 migrates.

7. **Comment is freeform text — do we want required comments on every write?** Safer: require a comment longer than 10 chars on writes to `restart_required=TRUE` keys. Frictionless: comments optional on all writes. My recommendation: optional in Phase 1, required on high-risk keys in Phase 2.

8. **Rollback UX: should rollback require a fresh comment?** Probably yes — "why are you rolling back". Confirm.

9. **Per-service deploy sequencing — which service first?** I recommend margin_engine first, not engine. Margin_engine is already paper-only, has cleaner pydantic settings, and a bad config change only affects Hyperliquid paper trades (no real money, no Polymarket exposure). Engine second. Operator confirm.

10. **Is the .env file on the host still edited by humans post-Phase-3, or is it CI-templated on every deploy?** DEP-02 and the existing deploy workflows use `set_env` helpers to template margin_engine's `.env`. Engine does not (runs as raw python under novakash user). The post-Phase-3 engine should EITHER template the minimal bootstrap set of env vars through CI-01 deploy-engine workflow (CI-01 is already drafted) OR continue to hand-edit. My recommendation: CI templates the secret keys, DB owns everything else. Confirm.

---

## Appendix A — Glossary

- **DBConfigLoader** — the proposed per-service TTL cache (§6). Not a real class yet; this is the name/shape we propose.
- **Hot reload** — the property that flipping a value in the DB affects the running service without a process restart, usually within one TTL window (~10s).
- **Restart required** — a key whose value is captured at `__init__` time and is not re-read until the service process restarts. The UI surfaces these with a warning badge. Mostly applies to `engine/signals/gates.py` classes in Phase 1; cleaned up in CFG-07b.
- **Seed migration** — the `hub/db/seed_config_keys.py` module that INSERT-ON-CONFLICTs the 142 rows from §4 into `config_keys` on hub startup. Idempotent. Owned by developers; adding a new config key means editing the seed.
- **Bootstrap key** — a `.env` key whose absence would prevent the DB-config system from functioning. Cannot be DB-managed. `DATABASE_URL`, `SECRET_KEY`, `SKIP_DB_CONFIG_SYNC`, and the chicken-and-egg set.
- **Fail-closed** — a failure mode where an error halts trading. We explicitly REJECT fail-closed-on-config-error semantics. Stale config is always safer than no trading.
- **Degrade-safe** — the complementary property. On DB error, the loader keeps serving from last-known-good cache, the service keeps trading, an alert fires. Nothing silently breaks.

## Appendix B — File paths touched across all CFG tasks

For the future agent executing this plan, here is the complete list of files that will be touched across CFG-02..CFG-11:

**Hub:**
- `hub/main.py` — register new router, DDL in lifespan
- `hub/api/config_v2.py` — NEW FILE — 4 GET + 3 POST endpoints
- `hub/db/seed_config_keys.py` — NEW FILE — 142-row seed
- `hub/db/migrations/` — if you prefer an out-of-band migration layer instead of lifespan DDL
- `hub/api/config.py` — DEPRECATED marker in Phase 1, removed in CFG-11
- `hub/api/trading_config.py` — unchanged through Phase 1/2, soft-deprecated in CFG-11

**Engine:**
- `engine/config/db_config_loader.py` — NEW FILE
- `engine/config/runtime_config.py` — internals swapped, public API preserved (CFG-07)
- `engine/strategies/orchestrator.py` — wire loader.boot() and loader.tick() into heartbeat (CFG-07)
- `engine/signals/gates.py` — each gate refactored to read at `evaluate()` time (CFG-07b, 9 classes)

**margin_engine:**
- `margin_engine/infrastructure/config/db_config_loader.py` — NEW FILE
- `margin_engine/infrastructure/config/settings.py` — pydantic fields become properties backed by loader (CFG-08)
- `margin_engine/main.py` — wire loader into startup (CFG-08)

**macro-observer, data-collector:**
- `macro-observer/observer.py` — replace inline env reads (CFG-10)
- `data-collector/collector.py` — replace hardcoded constants (CFG-10)

**Frontend:**
- `frontend/src/App.jsx` — new `/config` route, old `/config` redirect flipped (CFG-05/CFG-06)
- `frontend/src/pages/config/` — NEW DIR — ConfigPage.jsx, ServiceSidebar.jsx, KeyRow.jsx, HistoryDrawer.jsx, useConfigApi.js
- `frontend/src/pages/AuditChecklist.jsx` — add CFG-01..CFG-11 tasks
- `frontend/src/pages/Config.jsx` — RETIRED (CFG-11)
- `frontend/src/pages/TradingConfig.jsx` — RETIRED, redirect kept (CFG-11)

**Docs:**
- `docs/CONFIG_MIGRATION_PLAN.md` — this file (CFG-01, DONE)
- `docs/CONFIG.md` — rewritten to describe the new flow (CFG-11)

---

_End of plan. Next step: operator review, then CFG-02 kickoff._
