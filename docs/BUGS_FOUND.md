# Bugs Found

## 1. Duplicate Trades on Engine Restart (Apr 8, 2026)

**Severity:** CRITICAL (direct money loss)

**Symptom:** After an engine restart, the same 5-minute window received two
trades instead of one.  The in-memory dedup variable
`self._last_executed_window` was cleared on restart, so the engine did not know
a trade had already been placed for the active window.

**Affected windows:**

| Window TS    | Trade IDs      | Outcome |
|-------------|----------------|---------|
| 1775683200  | #2898, #2900   | Both filled -- duplicate stake lost |
| 1775683800  | #2907, #2909   | Both filled -- duplicate stake lost |

**Financial impact:** Each duplicate trade risked the full per-window stake
(~$2-5 depending on Kelly sizing).  At minimum, the extra exposure doubled the
loss on losing windows and wasted fees on winning windows.  Estimated extra
cost: two redundant stakes plus fees.

**Root cause:** `FiveMinVPINStrategy.__init__` initialised
`self._last_executed_window = None`, a single-string comparison that resets to
`None` every time the process restarts.  There was no DB persistence of which
windows had already been traded.

**Fix implemented (same day):**

1. Replaced the single-string `_last_executed_window` with a `set[str]`
   called `_traded_windows`.
2. On `start()`, the strategy loads recently traded window keys from the
   `trades` table via `DBClient.load_recent_traded_windows(hours=2)`.
3. All dedup checks now use `window_key in self._traded_windows` instead of
   `self._last_executed_window == window_key`.
4. After each trade, the key is added to the set.
5. A periodic cleanup removes keys older than 2 hours to prevent unbounded
   memory growth.

**Files changed:**
- `engine/strategies/five_min_vpin.py` -- dedup logic
- `engine/persistence/db_client.py` -- new `load_recent_traded_windows()` query

---

## 2. Redeemer "Failed: 7" on Every Startup (Apr 8, 2026)

**Severity:** LOW (no money lost, accounting nuisance)

**Symptom:** The Builder Relayer redeemer reports 7 failed redemptions on every
engine startup / sweep cycle.  The same 7 positions fail repeatedly.

**Analysis:**

The redeemer (`engine/execution/redeemer.py`) works as follows:
1. Fetches all positions from `data-api.polymarket.com/positions?user=<funder>`
2. Filters for resolved positions where `curPrice <= 0.01` or `>= 0.99`
3. Calls `redeemPositions()` on the CTF contract via the Builder Relayer API
4. Polls for CONFIRMED status

The 7 failures are most likely **already-redeemed positions** that the
Polymarket data API still reports.  Once a position is redeemed on-chain, the
CTF contract's `redeemPositions()` call either reverts or returns a zero-value
transaction.  The Builder Relayer then times out waiting for confirmation,
returning a FAILED status.

Alternatively, these could be positions from very old markets where the
condition has been settled but the relay transaction format has changed.

**Why it's not harmful:** The redeemer only processes positions that show
`curPrice <= 0.01` or `>= 0.99`.  Failed redemptions just mean those
positions stay in the "unredeemed" list.  No USDC is lost -- the relay
transaction either reverts or does nothing.

**Recommended fix (future):**

1. Track redeemed condition IDs in a local DB table to skip known-failed
   positions on subsequent sweeps.
2. After N consecutive failures for the same `conditionId`, mark it as
   "permanently failed" and stop retrying.
3. Log the specific condition IDs that fail so they can be investigated
   manually on PolygonScan.

**Investigation note:** SSH access to the Montreal engine was not available
during this session.  To get the actual condition IDs that are failing, run:
```bash
ssh novakash@15.223.247.178 "grep -a 'redeem.*FAIL\|redeem.*error' /home/novakash/engine.log | tail -30"
```
