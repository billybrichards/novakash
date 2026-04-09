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

---

## 3. Reconciler Cannot Match Polymarket Positions to DB Trades (Apr 8, 2026)

**Severity:** CRITICAL (trades resolve on Polymarket but outcomes never written to trades table)

**Symptom:** The reconciler logs `pos_token=?` for 10+ overnight losses, meaning
`data.get("tokenId", "")` returns an empty string from the Polymarket positions
API response. Trades resolve on Polymarket but outcomes are never written to the
trades table, trade_bible stays empty, and SITREP shows wrong W/L counts.

**Root cause (3 bugs):**

1. **Wrong field name from Polymarket data API.** The Polymarket data API
   (`https://data-api.polymarket.com/positions?user=<addr>`) returns the CLOB
   token ID in the `asset` field, NOT `tokenId`. The code was using
   `p.get("tokenId", "")` which returned empty. The engine stores the CLOB token
   ID (from `window.up_token_id` / `window.down_token_id`) as `token_id` in trade
   metadata. Since the reconciler extracted empty string from `tokenId`, it could
   never match.

2. **Unreliable scope check.** `_resolve_position` at line 683 used
   `'trade_pnl' in dir()` to determine whether per-trade PnL variables were in
   scope. `dir()` checks module-level names, NOT local variables -- this check
   was always unreliable and could silently use aggregate Polymarket cost instead
   of per-trade stake for notifications.

3. **No fallback matching.** When token_id matching failed (because the field was
   empty), there was no cost-based or condition_id-based fallback. Every
   unmatched resolution was silently logged and lost.

**Affected:** Every live trade placed since the data API change -- 10+ overnight
losses on Apr 8-9 logged `pos_token=?` and were never written to the trades table.

**Fix implemented:**

1. Changed `data.get("tokenId", "")` to `data.get("asset", "") or data.get("tokenId", "")`
   in 4 locations across `reconciler.py` and 1 location in `polymarket_client.py`
   (`get_position_outcomes` return dict). The `asset` field is the CLOB token ID
   that matches what the engine stores in trade metadata.

2. Replaced `'trade_pnl' in dir()` with `if matched_trade_id:` -- a reliable
   check that the trade was matched and per-trade variables are in scope.

3. Added cost-based fallback matching: if token_id exact and prefix matching both
   fail, match by approximate `stake_usd` (within $0.50) + recency.

4. Added raw data logging on match failure: logs `raw_asset`, `raw_tokenId`,
   `raw_keys`, `size`, and `avg_price` so future mismatches can be debugged
   without SSH access.

**Files changed:**
- `engine/reconciliation/reconciler.py` -- field name fix (4 locations), fallback match, raw logging, dir() fix
- `engine/execution/polymarket_client.py` -- `get_position_outcomes()` tokenId field extraction
