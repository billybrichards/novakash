# v11 Changelog + Agent Handover Doc

> **Status**: DEPLOYED to Montreal Apr 10, 11:16 UTC
> **Wallet**: ~$30 USDC (Apr 10 11:25 UTC, recovering from $20)
> **Engine**: running, v11 code fixes active, v10.5-ish config unchanged
> **Next agent**: start here, then read `/docs/APR9_FULL_DAY_ANALYSIS.md`
> and `/docs/OVERNIGHT_APR9-10_ANALYSIS.md` for historical context

---

## 1. TL;DR — What just happened

After two days of bad performance blamed on "adverse selection in choppy
markets", we discovered the actual root cause: **the FAK/FOK response
parser was reading the wrong field names from Polymarket's CLOB API**,
causing the engine to silently triple-fill every trade and lose money
on execution, not signal quality.

**The 32K-window signal backtest showing 85% WR was correct.** The
"overnight disaster" where SEQUOIA signals were 81% WR but filled
trades were 49% WR was NOT adverse selection — it was the engine paying
2-3x the intended stake on every trade while only recording one in
`trade_bible`.

**Quantified**: over 72 hours pre-fix, $1445 was actually spent on
Polymarket but `trade_bible` only recorded $883 in stakes. **$562 of
fills were orphaned** (51% of trades triple-filled, 24% double-filled,
only 25% single-fill as intended).

**v11 is three code fixes**, not a config tuning pass:
1. FAK/FOK response parsing → stop silent multi-fills
2. Telegram alert typo + silent exception handler → see trade alerts
3. `get_order_status` field name + case normalization → GTC polling works

**No config changes were deployed.** The existing Montreal config
(basically v10.5 with some v10.6 threshold tweaks) is unchanged.

---

## 2. The bugs, in order of impact

### 2.1 Polymarket response field-name mismatch (CRITICAL)

**Files:** `engine/execution/polymarket_client.py`
  - `place_fok_order()` (lines 492–640)
  - `place_market_order()` (lines 637–780)
  - `get_order_status()` (lines 1127–1180)

**What the code did (pre-v11):**
```python
size_matched_raw = response.get("size_matched", "0")  # field doesn't exist
filled = size_matched > 0 or status in ("MATCHED", "FILLED")  # case mismatch
```

**What Polymarket actually returns** (per their docs):
```json
{
  "success": true,
  "orderID": "0x...",
  "status": "matched",       // LOWERCASE
  "makingAmount": "5.00",    // USDC paid for BUY
  "takingAmount": "3.40",    // shares received for BUY
  "transactionsHashes": [...],
  "tradeIDs": [...]
}
```

**Result:** every FAK response was parsed as `filled=False size_matched=0`.
The price ladder in `fok_ladder.py` then:
1. Fired FAK attempt 2 at a worse price (+π cents)
2. If that "failed" too, fell back to a GTC limit order
3. By this point, 2 or 3 fills might have already happened silently
4. The engine only tracked the LAST order_id, so `trade_bible` showed
   one stake while the wallet actually paid for 2-3

**Fix (v11):** Read `takingAmount` as `size_matched`, normalize status
to lowercase, check `status == "matched" or size_matched > 0`. Return
`making_amount` and `taking_amount` alongside for downstream code.

### 2.2 Telegram alert path broken (HIGH)

**Files:** `engine/alerts/telegram.py`

**Three sub-bugs:**

a) **`send_entry_alert()` line 1785**: called `self._send_telegram(msg)`
   which **doesn't exist**. The real method is `self._send(text)`. This
   was an AttributeError every time a GTC fill was detected. The try/except
   swallowed it and logged `telegram.entry_alert_failed`, but because
   the GTC polling loop (see 2.3) never detected fills, this path was
   never reached and we never saw the warning.

b) **`send_system_alert()` line 1947**: `except Exception: pass` silently
   swallowed every error with zero logging. No way to diagnose failures.

c) **`send_entry_alert()` formatting**: would crash on `"?"` strings
   passed to `float()` if metadata fields were missing.

**Fix (v11):** Use `self._send()`. Add success log lines
(`telegram.entry_alert_sent`, `telegram.system_alert_sent`). Log failures
explicitly. Guard float conversions.

### 2.3 GTC polling status mismatch (MEDIUM)

**File:** `engine/execution/polymarket_client.py` `get_order_status()`

Same bug pattern as 2.1 — reads `size_matched` (doesn't exist) and
returns whatever case the CLOB sent. The caller in
`engine/strategies/five_min_vpin.py` around line 2715 does:

```python
if filled or clob_status not in ("LIVE", "UNKNOWN"):
    break
```

If Polymarket returns lowercase `"live"`, the check `clob_status not in
("LIVE", "UNKNOWN")` evaluates **True** immediately, so the loop exits
on the first poll claiming the order is "neither LIVE nor UNKNOWN"
(when actually it IS live, just lowercase).

**Fix (v11):** Normalize status to UPPERCASE in `get_order_status` for
back-compat. Try `size_matched`, `sizeMatched`, `takingAmount` in order.

---

## 3. New table: `poly_fills` (source of truth)

**Migration:** `hub/db/migrations/versions/20260410_01_poly_fills.sql`

Purpose: **authoritative, append-only record of every Polymarket CLOB
fill** for our proxy wallet, sourced from `data-api.polymarket.com`.
This is the **only table** analysis queries should read when computing
real P&L, real stakes, and real fill counts. `trade_bible` and `trades`
are engine-side records that can drift from reality (as the last 72h
proved).

### Schema highlights

```sql
transaction_hash    TEXT UNIQUE       -- on-chain global identifier
source              VARCHAR(32)       -- 'data-api' | 'clob-api' | 'on-chain' | 'engine-reported'
is_multi_fill       BOOLEAN           -- True if same condition_id had 2+ fills within 2min
multi_fill_index    INTEGER           -- 1, 2, 3 — which attempt this was
multi_fill_total    INTEGER           -- total fills for the condition_id
trade_bible_id      INTEGER           -- nullable FK; NULL = orphan fill
verified_at         TIMESTAMPTZ       -- last time we confirmed against the chain
raw_payload         JSONB             -- full data-api response for debugging/reprocessing
```

### Design invariants

1. **Append-only**: rows are `INSERT ... ON CONFLICT DO NOTHING`.
   Never UPDATE a `poly_fills` row. If the chain data ever changes
   (impossible but defensively-coded), insert a new row with a new
   `verified_at`.

2. **Source-tagged**: every row carries a `source` so we can distinguish
   data-api rows (authoritative) from engine-reported rows (may lie).

3. **Orphan-preserving**: fills with no matching `trade_bible` entry
   remain in the table with `trade_bible_id = NULL`. These are the
   multi-fill casualties — critical evidence of the bug.

4. **Immutable chain data**: `price`, `size`, `match_timestamp` come
   from on-chain and are never touched post-insert.

---

## 4. New module: `PolyFillsReconciler`

**File:** `engine/reconciliation/poly_fills_reconciler.py`

A periodic reconciler that runs from the orchestrator every 5 minutes
and keeps `poly_fills` in sync with Polymarket's data-api. It:

1. **Fetches** trades from `data-api.polymarket.com/trades?user=<funder>`
   (paginated, last ~2h per run by default)
2. **Detects** multi-fills by grouping on `condition_id` within a 2min
   window and tagging `is_multi_fill`, `multi_fill_index`, `multi_fill_total`
3. **Inserts** new rows into `poly_fills` (ON CONFLICT DO NOTHING)
4. **Links** orphan fills to `trade_bible` via `condition_id` (primary)
   or `market_slug` (fallback), temporal-proximity-matched within 10 min
5. **Enriches** `trade_bible.condition_id` and `trade_bible.market_slug`
   from linked `poly_fills` rows (only if the trade_bible field is NULL
   — never overwrites)

### Wiring in orchestrator

`orchestrator.py` `_start_services` section 5e (around line 690):
```python
self._poly_fills_reconciler = PolyFillsReconciler(
    pool=self._db._pool,
    funder_address=self._settings.poly_funder_address,
)
self._tasks.append(
    asyncio.create_task(self._poly_fills_loop(), name="poly_fills_reconciler")
)
```

`_poly_fills_loop()` method (around line 1653):
- 30s initial delay (avoid startup hammer)
- Runs `sync(hours=POLY_FILLS_LOOKBACK_HOURS)` every
  `POLY_FILLS_SYNC_INTERVAL_S` seconds (defaults: 2h lookback, 300s interval)
- Logs `poly_fills_loop.sync_result` only when something changed
- Catches all exceptions and continues

### CLI usage

```bash
# From engine/ dir (so module imports work)
cd /home/novakash/novakash/engine
python3 -m reconciliation.poly_fills_reconciler --hours 48

# From repo root (via the helper script)
python3 scripts/backfill_trades_from_polymarket.py --hours 72 --link
```

Both are idempotent — running multiple times is a no-op after the first.

---

## 5. Current config on Montreal (as of v11 deploy)

**File:** `/home/novakash/novakash/engine/.env` (gitignored, only on Montreal)

### Sizing
```
BET_FRACTION=0.050                 # 5% of bankroll
STARTING_BANKROLL=63               # will drift from wallet — NEEDS MANUAL UPDATE on top-ups
ABSOLUTE_MAX_BET=6.0
```

### Gate thresholds (hybrid v10.5 / v10.6)
```
V10_DUNE_ENABLED=true
V10_DUNE_MODEL=oak                 # cosmetic — actual model is SEQUOIA on timesfm server
V10_DUNE_MIN_P=0.60                # v10.5
V10_MIN_EVAL_OFFSET=180            # v10.5 — NOT T-200

# Regime-specific (mix of v10.5 and v10.6)
V10_TRANSITION_MIN_P=0.70
V10_CASCADE_MIN_P=0.67
V10_NORMAL_MIN_P=0.60
V10_LOW_VOL_MIN_P=0.60
V10_TRENDING_MIN_P=0.67
V10_CALM_MIN_P=0.72

# Penalties
V10_OFFSET_PENALTY_MAX=0.04
V10_DOWN_PENALTY=0.03

# Delta gate (v10.5 — NOT tightened)
V10_MIN_DELTA_PCT=0.005
V10_TRANSITION_MIN_DELTA=0.010
```

### Entry cap (flat $0.68 — NOT confidence-scaled)
```
V10_DUNE_CAP_MARGIN=0.05
V10_DUNE_CAP_FLOOR=0.35
V10_DUNE_CAP_CEILING=0.68
```

### Risk
```
MAX_DRAWDOWN_KILL=0.80             # loose — NO auto-resume configured
DAILY_LOSS_LIMIT_PCT=0.60          # loose
CONSECUTIVE_LOSS_COOLDOWN=10       # loose
COOLDOWN_SECONDS=300
```

### CoinGlass gates (v10.4)
```
V10_CG_TAKER_GATE=true
V10_CG_TAKER_OPPOSING_PCT=55
V10_CG_SMART_OPPOSING_PCT=52
V10_CG_TAKER_OPPOSING_PENALTY=0.05
V10_CG_TAKER_ALIGNED_BONUS=0.01
V10_CG_CONFIRM_BONUS=0.01
V10_CG_CONFIRM_MIN=2
```

### v11 reconciler
```
# None configured yet — defaults are:
#   POLY_FILLS_SYNC_INTERVAL_S=300 (5 min)
#   POLY_FILLS_LOOKBACK_HOURS=2
```

---

## 6. Proposed winning config (AFTER 24h of clean-fill data)

**Do NOT deploy these yet.** The logic: we now have correct single-fill
execution. Previous backtests were polluted by the multi-fill bug. We
need at least 24h of clean data (100+ trades) before we know what the
REAL signal performance looks like. Only then should we decide whether
to tune.

**Once we have that data**, the proposal is:

### Tighter signal gates (if the data supports it)
```
V10_MIN_DELTA_PCT=0.03             # was 0.005 — block <0.03% delta (54-59% WR)
V10_TRANSITION_MIN_DELTA=0.05      # was 0.010
V10_DUNE_MIN_P=0.70                # was 0.60
V10_NORMAL_MIN_P=0.70
V10_TRANSITION_MIN_P=0.72
V10_CASCADE_MIN_P=0.70
V10_LOW_VOL_MIN_P=0.99             # block — too small a sample
V10_TRENDING_MIN_P=0.99            # block
V10_CALM_MIN_P=0.99                # block
```

### Confidence-scaled cap (v10.6)
```
V10_CAP_SCALE_BASE=0.62            # was flat 0.68
V10_CAP_SCALE_CEILING=0.68
V10_CAP_SCALE_MIN_CONF=0.70
V10_CAP_SCALE_MAX_CONF=0.85
V10_DUNE_CAP_FLOOR=0.55
# And disable the old flat cap vars or leave them as fallback
```

### Risk tightening
```
MAX_DRAWDOWN_KILL=0.60             # was 0.80
KILL_AUTO_RESUME_MINUTES=30        # NEW — enables the code already deployed in v11
CONSECUTIVE_LOSS_COOLDOWN=3        # was 10
COOLDOWN_SECONDS=600               # 10 min
DAILY_LOSS_LIMIT_PCT=0.25
```

### Monitoring the reconciler
```
POLY_FILLS_SYNC_INTERVAL_S=300     # default
POLY_FILLS_LOOKBACK_HOURS=2        # default
```

---

## 7. Agent handover — start here

### Pre-checks (run these first on every session start)

```bash
# 1. Confirm engine is alive
ssh ubuntu@15.223.247.178 'ps aux | grep "python3 main.py" | grep -v grep'

# 2. Check most recent trade
PGPASSWORD=wKbsHjsWoWaUKkzSqgCUIijtnOKHIcQj psql -h hopper.proxy.rlwy.net -p 35772 -U postgres -d railway -c "
SELECT trade_id, trade_outcome, pnl_usd, direction, stake_usd, placed_at
FROM trade_bible WHERE is_live AND placed_at >= NOW() - interval '1 hour'
ORDER BY placed_at DESC LIMIT 5;"

# 3. Check multi-fill status (post-v11 should be ~0%)
PGPASSWORD=wKbsHjsWoWaUKkzSqgCUIijtnOKHIcQj psql -h hopper.proxy.rlwy.net -p 35772 -U postgres -d railway -c "
SELECT 
  SUM(CASE WHEN multi_fill_total = 1 THEN 1 ELSE 0 END) as single,
  SUM(CASE WHEN multi_fill_total = 2 THEN 1 ELSE 0 END) as double,
  SUM(CASE WHEN multi_fill_total >= 3 THEN 1 ELSE 0 END) as triple,
  COUNT(DISTINCT condition_id) as total
FROM poly_fills
WHERE side='BUY' AND match_time_utc >= NOW() - interval '2 hours';"

# 4. Check wallet
PGPASSWORD=wKbsHjsWoWaUKkzSqgCUIijtnOKHIcQj psql -h hopper.proxy.rlwy.net -p 35772 -U postgres -d railway -c "
SELECT balance_usdc, recorded_at FROM wallet_snapshots ORDER BY recorded_at DESC LIMIT 1;"

# 5. Check for engine errors
ssh ubuntu@15.223.247.178 'sudo tail -50 /home/novakash/engine.log | grep -iE "error|failed|exception"'
```

### Montreal deployment rules (REPEAT FROM `docs/DEPLOYMENT.md`)

1. **NEVER push from Montreal** — push from local/OpenClaw, Montreal pulls
2. **Engine reads `engine/.env` not `.env.local`** — `.env.local` is a reference
3. **SSH requires fresh EC2 Instance Connect key every session**
4. **After crashes**: `sudo chown -R novakash:novakash /home/novakash/novakash/`
5. **Restart pattern**:
   ```bash
   sudo -u novakash bash -c 'cd /home/novakash/novakash && git pull origin develop'
   sudo pkill -9 -f 'python3 main.py' ; sleep 5
   sudo -u novakash bash -c 'cd /home/novakash/novakash/engine && nohup python3 main.py > /home/novakash/engine.log 2>&1 & disown ; echo started'
   sleep 8
   ps aux | grep 'python3 main.py' | grep -v grep  # Must show 1 process
   ```

### How to run the poly_fills backfill manually

```bash
# Backfill the last 48h from anywhere with DB access
DATABASE_URL="postgresql://postgres:...@hopper.proxy.rlwy.net:35772/railway" \
  python3 scripts/backfill_trades_from_polymarket.py --hours 48 --link

# Dry-run first to see what it would do
python3 scripts/backfill_trades_from_polymarket.py --hours 48 --dry-run
```

### Analysis queries that use `poly_fills` as ground truth

**Real cost vs recorded cost (last 24h):**
```sql
SELECT 
  COUNT(DISTINCT pf.condition_id) as windows,
  ROUND(SUM(pf.cost_usd)::numeric, 2) as actual_spent,
  ROUND(COALESCE(SUM(tb.stake_usd), 0)::numeric, 2) as recorded_stake,
  ROUND(SUM(pf.cost_usd)::numeric - COALESCE(SUM(DISTINCT tb.stake_usd), 0)::numeric, 2) as unrecorded
FROM poly_fills pf
LEFT JOIN trade_bible tb ON tb.id = pf.trade_bible_id
WHERE pf.side='BUY' AND pf.match_time_utc >= NOW() - interval '24 hours';
```

**Multi-fill breakdown (post-v11 should show 100% single):**
```sql
SELECT 
  multi_fill_total,
  COUNT(DISTINCT condition_id) as windows,
  ROUND(AVG(cost_usd)::numeric, 2) as avg_cost
FROM poly_fills
WHERE side='BUY' AND match_time_utc >= NOW() - interval '24 hours'
GROUP BY multi_fill_total ORDER BY multi_fill_total;
```

**Verify v11 is actually active** (the log should show these messages):
```bash
sudo grep -E 'place_market_order.result.*making_amount|telegram.entry_alert_sent|telegram.system_alert_sent|poly_fills_loop.sync_result' /home/novakash/engine.log | tail -20
```

### What to monitor over the next 24 hours

1. **Multi-fill rate** via the query above — should drop from 75% to ~0%
2. **`trade_bible` stake vs `poly_fills` cost_usd gap** — should shrink to <5%
3. **Telegram alerts** — user should receive trade entry alerts live
4. **Wallet drift** — wallet change should now match `trade_bible` PnL
5. **WR per session** — now that fills are clean, the real WR should be
   close to the signal eval WR (previously ~70-80% for TRADE decisions)

---

## 8. Honest uncertainty / things NOT done

1. **We haven't verified a single-fill trade end-to-end yet**. The engine
   restart happened at 11:16 UTC on Apr 10. The first post-fix trade at
   11:17:32 went to GTC fallback (FAK `no_match` on both attempts). The
   second trade at 11:22:33 did the same. We need to see a trade where
   FAK attempt 1 ACTUALLY returns `status: matched` and the ladder
   correctly stops — then we'll know the fix is proven on the happy path.

2. **The `PolyFillsReconciler` has been wired but the engine is running
   an older checkout from before the wiring commit**. It needs a git pull
   + restart to activate. (Next step after this doc is committed.)

3. **No config changes deployed.** If the current hybrid v10.5/v10.6 config
   produces bad WR on clean fills, we'll need to tune. But that's a
   tomorrow problem.

4. **Kill switch auto-resume code is deployed but disabled**
   (`KILL_AUTO_RESUME_MINUTES` not set, defaults to 0). To activate, set
   the env var on Montreal and restart.

5. **`trade_bible.condition_id` is populated for 0 rows.** The reconciler
   will backfill this over time but the old data will remain NULL unless
   we run a one-shot historical linking pass.

6. **The FAK "no_match" exception path** may itself be hiding real fills
   if py-clob-client raises when the chain response has `status: matched`
   but some other field it expects is missing. Needs log inspection the
   next time we see FAK fire.

---

## 9. File manifest (what changed in v11)

```
NEW:
  engine/reconciliation/poly_fills_reconciler.py       # periodic reconciler class + CLI
  hub/db/migrations/versions/20260410_01_poly_fills.sql # poly_fills schema
  scripts/backfill_trades_from_polymarket.py           # manual backfill helper
  docs/V11_CHANGELOG_AND_HANDOVER.md                   # THIS FILE

MODIFIED:
  engine/execution/polymarket_client.py
    - place_fok_order() response parsing fixed
    - place_market_order() response parsing fixed
    - get_order_status() field names + case normalized
  engine/alerts/telegram.py
    - send_entry_alert() _send_telegram→_send typo, safer formatting, success log
    - send_system_alert() except-pass→logged warning, success log
  engine/strategies/orchestrator.py
    - _start_services() section 5e: wire PolyFillsReconciler
    - _poly_fills_loop() method: periodic sync loop
  engine/execution/risk_manager.py            (committed earlier in session)
    - _kill_switch_triggered_at timestamp field
    - is_killed auto-resume after KILL_AUTO_RESUME_MINUTES
```
