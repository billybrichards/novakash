# TODO: Fix Reconciler Orphaned Position Bug

**Priority:** CRITICAL
**Date:** 2026-04-08
**Impact:** 12+ WINs worth +$18.62 were never recorded. Trades marked EXPIRED despite confirmed CLOB fills.

## The Bug

Trades with `clob_status=MATCHED` and `shares_filled > 0` get marked `status=EXPIRED` with `outcome=NULL`. The position exists on Polymarket (shares held) but the engine thinks the order expired unfilled.

**Root cause chain:**
1. Engine places GTC limit order with GTD expiry (420s)
2. Strategy polls for fills for 60s, then stops
3. Market maker fills the GTC between 60s-420s
4. Engine marks trade EXPIRED (it stopped checking)
5. Reconciler sees position resolution but can't match back (timing/state mismatch)
6. Trade stuck in EXPIRED + outcome=NULL forever

**On 2026-04-08:** 16 trades had confirmed fills but no resolution. 12 were WINs (+$18.62 hidden profit).

## Fix Required

### 1. Implement CLOB Trade History API in Reconciler

Add a new method to `engine/execution/polymarket_client.py`:

```python
async def get_trade_history(self, limit: int = 100) -> list[dict]:
    """Fetch filled trade history from CLOB API.

    Returns list of fills with: asset_id, outcome, side, price, size, match_time.
    Use to reconcile orphaned positions that the 60s fill polling missed.
    """
    # Uses py_clob_client.get_trades()
    # Each fill has an 'outcome' field (Up/Down) from the oracle
```

### 2. Add Orphan Detection to Reconciler Poll Loop

In `engine/reconciliation/reconciler.py`, after the position poll:

```python
# After existing position resolution logic...
# Check for EXPIRED trades with confirmed fills but no outcome
orphans = await conn.fetch("""
    SELECT id, direction, stake_usd, metadata->>'token_id' as token_id,
           CAST(metadata->>'shares_filled' AS float) as shares
    FROM trades
    WHERE is_live = true AND outcome IS NULL
      AND metadata->>'clob_status' = 'MATCHED'
      AND status = 'EXPIRED'
""")
if orphans:
    # Query CLOB trade history
    fills = await self._poly.get_trade_history()
    # Match by token_id, determine WIN/LOSS from fill outcome field
    # Update trades table → trigger auto-populates trade_bible
```

### 3. Fix the EXPIRED Status Assignment

In `engine/strategies/five_min_vpin.py`, the trade should NOT be marked EXPIRED if `clob_status=MATCHED`:

```python
# Before marking EXPIRED, check if it actually filled
if metadata.get("clob_status") == "MATCHED" and float(metadata.get("shares_filled", 0)) > 0:
    # Don't mark EXPIRED — it filled, just resolution unknown
    status = "FILLED"
```

### 4. Fix trade_bible Unique Constraint

`idx_bible_resolved` on `resolved_at` prevents bulk resolution updates. Either:
- Change to `UNIQUE(trade_id)` only (already exists)
- Drop the `resolved_at` unique index
- Use `ON CONFLICT (trade_id)` which already works

## Verification

After fix:
```sql
-- Should return 0
SELECT count(*) FROM trades
WHERE is_live = true AND outcome IS NULL
  AND metadata->>'clob_status' = 'MATCHED';
```

## Manual Resolution Applied (2026-04-08)

12 orphaned WINs resolved via SQL update. 2 unmatched (#2643, #2782 — tokens fell off CLOB history). Corrected stats: 39W/24L (61.9% WR), -$29.64 PnL (was showing -$48.26 before fix).
