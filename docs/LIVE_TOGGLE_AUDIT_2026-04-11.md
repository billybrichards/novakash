# LIVE TOGGLE AUDIT -- 2026-04-11

> **Incident**: STOP-01 (2026-04-11 14:56 UTC)
> **Auditor**: Claude Code (automated trace)
> **Scope**: Full "flip to live" path from frontend toggle through DB to engine runtime
> **Classification**: READ-ONLY AUDIT -- no code changes

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Component Verdicts](#component-verdicts)
3. [Step-by-Step: What Happens When You Click "Go Live"](#step-by-step-what-happens-when-you-click-go-live)
4. [The Mode-Sync Heartbeat (The Critical Path)](#the-mode-sync-heartbeat-the-critical-path)
5. [STOP-01 Incident Reconstruction](#stop-01-incident-reconstruction)
6. [RESUME Procedure (Paper to Live)](#resume-procedure-paper-to-live)
7. [ROLLBACK Procedure (Live to Paper)](#rollback-procedure-live-to-paper)
8. [Source File Reference](#source-file-reference)
9. [Recommendations](#recommendations)

---

## Executive Summary

The novakash trading engine uses a **DB-authoritative mode system**. The `system_state` table
(single-row, `id=1`) holds two independent boolean columns: `paper_enabled` and `live_enabled`.
The engine's 10-second heartbeat loop reads these columns and switches mode at runtime without
requiring a restart. This means:

- **`.env` changes alone have NO lasting effect.** The engine reads `PAPER_MODE` from `.env`
  only at startup. Within 10 seconds, the heartbeat's mode-sync overwrites `settings.paper_mode`
  with whatever the DB says.
- **The UI toggle (or direct SQL UPDATE) is the only reliable way to change mode.**

STOP-01 proved this the hard way: an `.env` flip was overridden within one heartbeat cycle.

---

## Component Verdicts

| # | Component | File | Verdict | Detail |
|---|-----------|------|---------|--------|
| 1 | **Frontend LiveToggle** | `frontend/src/components/LiveToggle.jsx` | GREEN | Requires typed `CONFIRM` string + approved config + API keys checklist. Sends `confirmation: 'CONFIRM'` to backend. Disabling live is one-click (no confirmation). |
| 2 | **Hub `/trading-config/toggle-mode`** | `hub/api/trading_config.py:525` | GREEN | Server-side guard: live enable requires `confirmation='CONFIRM'` + active approved live config in DB. Updates `system_state.paper_enabled` or `system_state.live_enabled` column directly. |
| 3 | **Hub `/system/paper-mode` (legacy)** | `hub/api/system.py:106` | YELLOW | Writes `paper_mode` into `system_state.state` JSONB blob, but the engine mode-sync reads the **column-level** `paper_enabled`/`live_enabled`, not the JSONB field. This endpoint is effectively **dead code** for mode switching. The `System.jsx` page still calls it. |
| 4 | **DB schema (`system_state`)** | `hub/db/schema.sql:76` | GREEN | Clean design: `paper_enabled BOOLEAN DEFAULT TRUE`, `live_enabled BOOLEAN DEFAULT FALSE`. Safe defaults (paper on, live off). Single-row constraint (`CHECK id=1`). |
| 5 | **Engine startup (`.env` load)** | `engine/main.py` + `engine/config/settings.py:47` | GREEN | Reads `PAPER_MODE` from `.env` via pydantic-settings. Default is `True` (safe). But this value is **overridden within 10s** by the heartbeat mode-sync. |
| 6 | **Engine heartbeat mode-sync** | `engine/strategies/orchestrator.py:1787-1863` | GREEN | Core mechanism. Every 10s: reads `paper_enabled`/`live_enabled` from DB via `get_mode_toggles()`, computes `want_paper = not db_live or db_paper`, compares to current mode, switches if different. Sends Telegram alert on switch. Connects CLOB client when going live. |
| 7 | **Engine `get_mode_toggles()`** | `engine/persistence/db_client.py:458` | GREEN | Simple, defensive. Returns `None` if pool is missing (graceful degradation). Catches all exceptions silently (mode stays unchanged on DB failure). |
| 8 | **PolymarketClient `paper_mode` flag** | `engine/execution/polymarket_client.py` | GREEN | Every order-placing method gates on `self.paper_mode`. When `True`, orders are simulated locally. The flag is a plain Python boolean -- switching it is instant and atomic. |
| 9 | **Legacy System.jsx page** | `frontend/src/pages/System.jsx:35-38` | RED | Calls `/api/system/paper-mode` which writes to the JSONB blob, NOT the `paper_enabled`/`live_enabled` columns. Clicking "Switch to Live" on this page does **nothing** to the engine's actual mode. This is a stale UI that gives false confidence. |
| 10 | **Geoblock check** | `engine/strategies/orchestrator.py:447` | YELLOW | Only runs at startup, not on mode-sync. If engine starts in paper mode (safe) then switches to live via DB toggle, the geoblock check is never executed. Not a blocker since engine runs on Montreal EC2 (Canada, not blocked), but worth noting. |

---

## Step-by-Step: What Happens When You Click "Go Live"

```
  FRONTEND                          HUB (Railway)                    POSTGRES                     ENGINE (Montreal EC2)
  --------                          -------------                    --------                     --------------------

  1. User clicks LIVE toggle
     on LiveToggle component
           |
  2. Confirmation modal opens:
     - Checklist verified:
       [x] API keys configured
       [x] Live config approved
       [x] Live config active
     - User types "CONFIRM"
     - Clicks "ENABLE LIVE TRADING"
           |
  3. POST /trading-config/toggle-mode ------>
     { mode: "live",                    4. Server validates:
       enabled: true,                      - confirmation === "CONFIRM"?
       confirmation: "CONFIRM" }           - Active approved live config
                                             in trading_configs table?
                                           |
                                      5. UPDATE system_state ----------->
                                         SET live_enabled = true,        6. DB column updated:
                                             updated_at = NOW()             paper_enabled = true (unchanged)
                                         WHERE id = 1                       live_enabled  = true
                                           |                                     |
                                      <--- { ok: true } ------.                 |
           |                                                    |                |
  7. UI updates:                                                |                |
     LIVE pill glows red                                        |                |
     Header gets red border                                     |        8. Within 10 seconds:
                                                                |           _heartbeat_loop fires
                                                                |                |
                                                                |        9. get_mode_toggles()
                                                                |           SELECT paper_enabled,
                                                                |                  live_enabled
                                                                |           FROM system_state
                                                                |           WHERE id = 1
                                                                |                |
                                                                |       10. Compute want_paper:
                                                                |           db_paper=true, db_live=true
                                                                |           want_paper = not true or true
                                                                |                      = TRUE
                                                                |           ** STILL PAPER **
                                                                |
                                                                |   NOTE: To actually go LIVE, the user must
                                                                |   ALSO disable paper. The frontend should
                                                                |   handle this (see step 11 below).
                                                                |
                                                                |       11. If user also toggles paper OFF:
                                                                |           db_paper=false, db_live=true
                                                                |           want_paper = not true or false
                                                                |                      = FALSE
                                                                |           ** SWITCHES TO LIVE **
                                                                |                |
                                                                |       12. Mode switch cascade:
                                                                |           a. poly_client.paper_mode = False
                                                                |           b. settings.paper_mode = False
                                                                |           c. risk_manager._paper_mode = False
                                                                |           d. alerter._paper_mode = False
                                                                |           e. Telegram: "MODE SWITCH: PAPER -> LIVE"
                                                                |           f. poly_client.connect() (CLOB auth)
                                                                |           g. redeemer._paper_mode = False
                                                                |           h. redeemer.connect() + start loop
```

### Critical Formula

The mode decision at `orchestrator.py:1798`:

```python
want_paper = not db_live or db_paper
```

Truth table:

| `paper_enabled` | `live_enabled` | `want_paper` | Engine Mode |
|:---:|:---:|:---:|:---:|
| true  | false | **true**  | PAPER (default) |
| true  | true  | **true**  | PAPER (both on = paper wins) |
| false | true  | **false** | **LIVE** |
| false | false | **true**  | PAPER (both off = paper wins) |

**Key insight**: The ONLY way to reach LIVE mode is `paper_enabled=false AND live_enabled=true`.
Paper is the safe default in every other combination.

---

## The Mode-Sync Heartbeat (The Critical Path)

**Location**: `engine/strategies/orchestrator.py:1725-2190` (`_heartbeat_loop`)

The heartbeat runs in a `while not self._shutdown_event.is_set()` loop with a 10-second
sleep (`asyncio.wait_for(..., timeout=10.0)` at line 2184-2186).

### Heartbeat Sequence (every 10 seconds)

1. **Runtime config sync** -- pulls active trading config from DB (`runtime.sync()`)
2. **Risk status snapshot** -- reads current bankroll, drawdown, kill switch state
3. **Wallet balance check** -- every 60s (6th heartbeat), syncs real Polymarket wallet balance (live only)
4. **System state write** -- upserts engine status, balance, drawdown, VPIN, cascade state, config snapshot into `system_state.config` JSONB
5. **MODE SYNC** (lines 1787-1863):
   - `get_mode_toggles()` reads `paper_enabled`, `live_enabled` columns
   - Computes `want_paper = not db_live or db_paper`
   - If `want_paper != current_paper`, performs the full mode switch
   - On failure: logs `mode_sync.failed` at DEBUG level, mode stays unchanged (safe)
6. **Feed status update** -- writes venue connectivity booleans
7. **5-minute sitrep** -- every 30th heartbeat, sends Telegram status report

### What Gets Switched on Mode Change

When mode switches (line 1813-1861), these objects are updated **in-process** (no restart needed):

| Object | Attribute | Effect |
|--------|-----------|--------|
| `poly_client` | `.paper_mode` | Gates every `place_order()` / `cancel_order()` call |
| `settings` | `.paper_mode` | Read by strategies, risk checks, sitrep |
| `risk_manager` | `._paper_mode` | Controls bankroll tracking source |
| `alerter` | `._paper_mode` | Tags all Telegram messages with mode |
| `redeemer` | `._paper_mode` | Enables/disables on-chain redemption sweeps |
| `poly_client` | `.connect()` | Re-authenticates CLOB API (live only) |

---

## STOP-01 Incident Reconstruction

### Timeline (2026-04-11, all times UTC)

| Time | Event |
|------|-------|
| ~14:56 | User requests pause: "please pause live trading till we have finished our fixes" |
| ~14:58 | **First attempt**: `.env` edited on Montreal EC2 (`LIVE_TRADING_ENABLED=false`, `PAPER_MODE=true`). `scripts/restart_engine.sh` executed. Restart wrapper timed out. |
| ~15:06 | Engine restarts. Reads `PAPER_MODE=true` from `.env` on boot. Starts in paper mode. **Within 10 seconds**, heartbeat fires, reads `system_state` from DB: `paper_enabled=false, live_enabled=true`. Computes `want_paper = not true or false = FALSE`. **Auto-switches back to LIVE.** |
| ~14:08:43* | **Second attempt**: User flips UI toggle, which fires `POST /trading-config/toggle-mode` with `{ mode: "live", enabled: false }`. DB updated: `live_enabled=false`. |
| ~14:10 | Engine killed with `pkill`. Restarted. Reads `PAPER_MODE=true` from `.env`. Heartbeat fires, reads DB: `paper_enabled=true, live_enabled=false`. `want_paper = not false or true = TRUE`. **Stays in paper mode.** |
| ~14:10+ | **VERIFIED**: `system_state` shows `paper_enabled=t / live_enabled=f`. `place_order.requested` logs show `paper_mode=True`. `place_order.paper_filled` confirms simulation. |

*Note: timestamps from the AuditChecklist show 14:08:43 for the UI toggle and 14:10 for the restart,
suggesting the second attempt happened slightly before the first attempt's auto-recovery. The ordering
in the evidence reflects the sequence of actions, not wall-clock order.

### Root Cause

The `.env` file is read **once** at engine startup by pydantic-settings (`Settings` class).
The heartbeat's mode-sync reads the DB every 10 seconds and **overwrites** `settings.paper_mode`
in memory. The `.env` value is ephemeral; the DB is authoritative.

### Why `.env` Alone Fails

```
 Startup:  .env (PAPER_MODE=true)  -->  settings.paper_mode = True  -->  Engine starts in PAPER
                                                                              |
 +10 sec:  DB (paper_enabled=false, live_enabled=true)  ------------------>  Heartbeat overrides
                                                                              settings.paper_mode = False
                                                                              --> LIVE MODE RESTORED
```

---

## RESUME Procedure (Paper to Live)

### Prerequisites

- [ ] Approved live trading config exists in `trading_configs` table (`is_approved=true`, `is_active=true`, `mode='live'`)
- [ ] API keys configured on Montreal EC2 (POLY_API_KEY, POLY_API_SECRET, etc.)
- [ ] Engine process running (PID visible, heartbeat updating `system_state.last_heartbeat`)
- [ ] No active kill switch (`system_state.config->>'kill_switch_active'` is `false`)

### Steps

**Method A: Frontend (preferred)**

1. Navigate to the Trading Config page (not the legacy System page)
2. Verify the `LiveToggle` component shows `ENGINE: PAPER`
3. If paper toggle is ON, leave it for now
4. Click the LIVE toggle pill
5. Confirmation modal appears:
   - Verify all three checklist items are green
   - Type `CONFIRM` in the input field
   - Click "ENABLE LIVE TRADING"
6. Click the PAPER toggle pill to turn it OFF
7. Wait up to 10 seconds for the heartbeat to pick up the change
8. Verify the runtime indicator shows `ENGINE: LIVE`
9. Check Telegram for the mode switch alert: "MODE SWITCH: PAPER -> LIVE"

**Method B: Direct SQL (emergency/headless)**

```sql
-- Enable live, disable paper
UPDATE system_state
SET paper_enabled = false,
    live_enabled  = true,
    updated_at    = NOW()
WHERE id = 1;
```

Wait 10 seconds. Verify via:

```sql
SELECT paper_enabled, live_enabled,
       config->>'paper_mode' as engine_paper_mode,
       last_heartbeat
FROM system_state WHERE id = 1;
```

Expected: `paper_enabled=f, live_enabled=t, engine_paper_mode=false`, `last_heartbeat` within last 10s.

---

## ROLLBACK Procedure (Live to Paper)

### Steps

**Method A: Frontend (preferred, fastest)**

1. Click the LIVE toggle pill on the `LiveToggle` component -- it is a one-click disable (no confirmation needed)
2. Optionally ensure PAPER toggle is ON (it should already be, but verify)
3. Wait up to 10 seconds for the heartbeat
4. Verify `ENGINE: PAPER` indicator
5. Check Telegram for: "MODE SWITCH: LIVE -> PAPER"

**Method B: Direct SQL (emergency/headless)**

```sql
-- Disable live, enable paper
UPDATE system_state
SET paper_enabled = true,
    live_enabled  = false,
    updated_at    = NOW()
WHERE id = 1;
```

**Method C: Kill + SQL (nuclear option, if engine is misbehaving)**

```bash
# On Montreal EC2 (15.223.247.178)
ssh ubuntu@15.223.247.178

# 1. Kill the engine immediately
pkill -f "python.*engine/main.py"
# or: kill -9 <PID>

# 2. Update DB (via psql or UI)
psql "$DATABASE_URL" -c "
  UPDATE system_state
  SET paper_enabled = true,
      live_enabled  = false,
      updated_at    = NOW()
  WHERE id = 1;
"

# 3. Restart engine
cd /home/novakash/novakash/engine
nohup python main.py >> /var/log/novakash/engine.log 2>&1 &

# 4. Verify
sleep 12
psql "$DATABASE_URL" -c "
  SELECT paper_enabled, live_enabled,
         config->>'paper_mode' as mode,
         last_heartbeat
  FROM system_state WHERE id = 1;
"
```

### What NOT To Do

| Wrong Action | Why It Fails |
|---|---|
| Edit `.env` and restart engine | DB mode-sync overrides `.env` within 10 seconds |
| Edit `.env` without restart | pydantic-settings only reads `.env` at process startup |
| Use the legacy `/system` page toggle | Writes to JSONB blob, not the `paper_enabled`/`live_enabled` columns that the engine reads |
| Set `PAPER_MODE=true` in Railway env | Hub runs on Railway, but the engine runs on Montreal EC2. Railway env vars do not affect the engine. |

---

## Source File Reference

| File | Key Lines | Role |
|------|-----------|------|
| `engine/main.py` | 17, 29 | Loads `.env`, reads `settings.paper_mode`, creates Orchestrator |
| `engine/config/settings.py` | 47 | `paper_mode: bool = Field(default=True)` -- pydantic-settings field |
| `engine/strategies/orchestrator.py` | 120, 161-206 | Constructor: initializes all components with `settings.paper_mode` |
| `engine/strategies/orchestrator.py` | 447 | Geoblock check (startup, live only) |
| `engine/strategies/orchestrator.py` | 1725-2190 | `_heartbeat_loop` -- 10s cycle, mode-sync at 1787-1863 |
| `engine/strategies/orchestrator.py` | 1798 | **`want_paper = not db_live or db_paper`** -- the critical formula |
| `engine/persistence/db_client.py` | 370-421 | `update_system_state()` -- heartbeat writes config snapshot |
| `engine/persistence/db_client.py` | 458-471 | `get_mode_toggles()` -- reads `paper_enabled`, `live_enabled` |
| `engine/execution/polymarket_client.py` | 121, 164, 236 | `self.paper_mode` gates all real order placement |
| `hub/db/schema.sql` | 76-89 | `system_state` table DDL with safe defaults |
| `hub/api/trading_config.py` | 364-367 | `ToggleModeRequest` model (mode, enabled, confirmation) |
| `hub/api/trading_config.py` | 453-522 | `GET /trading-config/live-status` -- returns current toggle state + engine runtime state |
| `hub/api/trading_config.py` | 525-564 | `POST /trading-config/toggle-mode` -- the real mode toggle endpoint |
| `hub/api/system.py` | 106-130 | `POST /system/paper-mode` -- **LEGACY, writes to JSONB not columns** |
| `frontend/src/components/LiveToggle.jsx` | 57-104 | Paper toggle, live enable (with modal), live disable handlers |
| `frontend/src/pages/System.jsx` | 35-38 | **LEGACY toggle -- calls the wrong endpoint** |
| `frontend/src/pages/TradingConfig.jsx` | 820-821 | Renders `LiveToggle` with live-status data |

---

## Recommendations

### P0 -- Must Fix

1. **Remove or disable the legacy System.jsx paper/live toggle** (`System.jsx:91-96`).
   It calls `POST /api/system/paper-mode` which writes to the JSONB blob, not the columns
   the engine reads. This creates a false sense of control. Either remove the button entirely
   or rewire it to call `POST /trading-config/toggle-mode`.

### P1 -- Should Fix

2. **Add startup DB-sync.** On engine boot (in `main.py` or early in `orchestrator.run()`),
   read `system_state.paper_enabled/live_enabled` from DB and override `settings.paper_mode`
   **before** any component is initialized. Currently the engine starts with the `.env` value
   and only corrects after the first heartbeat (~10s). This 10-second window could execute
   trades in the wrong mode.

3. **Run geoblock check on mode-sync transitions**, not just startup. If the engine starts
   in paper mode and later switches to live via DB toggle, the geoblock check at line 447
   never fires.

### P2 -- Nice to Have

4. **Add a `mode_switch` audit log table.** Currently mode switches are only logged to
   structlog and Telegram. A DB table with `(timestamp, old_mode, new_mode, source, db_paper, db_live)`
   would provide a durable audit trail.

5. **Deprecate `POST /api/system/paper-mode`** endpoint entirely, or make it a thin proxy
   to the `toggle-mode` endpoint so there is exactly one code path for mode changes.

6. **Frontend: auto-disable paper when enabling live.** The current UI requires two clicks
   (enable live + disable paper). A single "Go Live" action that atomically sets
   `paper_enabled=false, live_enabled=true` would reduce operator error.

---

*Generated 2026-04-11 by automated source trace. No code was modified.*
