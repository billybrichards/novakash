# Configuration Guide — Novakash Engine

## ⚠️ IMPORTANT: How Config Works (Read This First)

### Config Priority (highest → lowest)

```
1. Shell environment variables  (export FOO=bar)
2. .env file                    (/home/novakash/novakash/engine/.env)
3. Hardcoded defaults           (config/runtime_config.py)
4. DB trading_configs table     (only when SKIP_DB_CONFIG_SYNC=false)
```

### The Env Var / DB Bug (fixed 2026-04-05, commit 6a64fd5)

**Problem:** `runtime_config.py` creates its singleton at module import time.
It reads `os.environ` directly — but `pydantic-settings` (used by `settings.py`)
loads `.env` into `settings.*` ONLY, not into `os.environ`.

Result: `.env` values were silently ignored. `runtime` used hardcoded defaults
(0.08% delta, 0.025 bet_frac) instead of `.env` values (0.02%, 0.10).

The DB had stale values (`vpin_gate=0.628` from old configs) and was overriding
the defaults via `runtime.sync()` every heartbeat.

**Fix:** `runtime_config.py` now calls `load_dotenv()` at the top of the file,
before the singleton is instantiated. This ensures `.env` → `os.environ` → `runtime`.

---

## Current Active Config (v7.1)

| Setting | Value | Env Var |
|---|---|---|
| VPIN gate | 0.45 | `FIVE_MIN_VPIN_GATE` |
| Min delta (normal/transition) | 0.02% | `FIVE_MIN_MIN_DELTA_PCT` |
| Min delta (cascade) | 0.01% | `FIVE_MIN_CASCADE_MIN_DELTA_PCT` |
| Bet fraction | 10% | `BET_FRACTION` |
| Starting bankroll | $100 | `STARTING_BANKROLL` |
| Max position | $40 | `MAX_POSITION_USD` |
| Max drawdown kill | 40% | `MAX_DRAWDOWN_KILL` |
| Daily loss limit | $20 / 20% | `DAILY_LOSS_LIMIT_USD` / `DAILY_LOSS_LIMIT_PCT` |
| Paper mode | true | `PAPER_MODE` |
| Skip DB config sync | true | `SKIP_DB_CONFIG_SYNC` |

---

## Changing Config

### Recommended: Edit .env on Montreal

```bash
ssh -i ~/.ssh/novakash-montreal.pem ubuntu@15.223.247.178
sudo -u novakash nano /home/novakash/novakash/engine/.env
# Edit values
sudo pkill -u novakash -f 'python3 main.py'
sudo -u novakash bash -c 'cd /home/novakash/novakash/engine && nohup python3 main.py > /home/novakash/engine.log 2>&1 &'
```

### Verify config was loaded correctly

```bash
sudo -u novakash bash -c 'cd /home/novakash/novakash/engine && python3 -c "
from config.runtime_config import runtime
print(\"vpin_gate:\", runtime.five_min_vpin_gate)
print(\"min_delta:\", runtime.five_min_min_delta_pct)
print(\"bet_fraction:\", runtime.bet_fraction)
print(\"paper_mode:\", __import__(\"os\").environ.get(\"PAPER_MODE\"))
"'
```

### Also update DB config (for reference / DB sync mode)

```bash
PGPASSWORD=... psql -h hopper.proxy.rlwy.net -p 35772 -U postgres -d railway -c "
UPDATE trading_configs SET config = config || '{\"bet_fraction\": 0.10}'::jsonb
WHERE is_active=true AND mode='paper';"
```

---

## DB Config Table

Two active configs, never more:

| ID | Name | Mode | Active |
|---|---|---|---|
| 24 | Live Config v7.1 | live | ✅ |
| 26 | Paper Config v7.1 | paper | ✅ |

All others are archived (`[ARCHIVED]` prefix).

### When SKIP_DB_CONFIG_SYNC=true (default)
DB config is ignored. Engine reads from `.env` only.

### When SKIP_DB_CONFIG_SYNC=false
DB config overlays `.env` values at each heartbeat (~10s).
Used for hot-reload without restart. Only set this if you want DB to be authoritative.

---

## VPIN Regimes and Thresholds

```
VPIN >= 0.65  → CASCADE    → momentum, min delta = 0.01%
VPIN 0.55-0.65 → TRANSITION → momentum, min delta = 0.02%
VPIN 0.45-0.55 → NORMAL     → contrarian, min delta = 0.02%
VPIN < 0.45   → CALM        → skip entirely (TIMESFM_ONLY disabled)
VPIN = 0.0    → TIMESFM_ONLY → skip (WebSocket down, no real signal)
```

The 0.12% transition threshold that was blocking TRANSITION trades was a **message-only**
artefact — the actual gate uses `five_min_min_delta_pct` (0.02%) for TRANSITION too.
