"""
v59: Mark phantom trades and add is_phantom computed flag.

Phantom trades = execution_mode IN (gtc_resting, gtc) with no
polymarket_tx_hash and NULL fill_price. These were never executed
on-chain but got scored as real WIN/LOSS by the reconciler.

This migration:
  1. Marks existing phantoms with status='PHANTOM'
  2. Preserves original outcome/status in metadata for audit
  3. Is idempotent (safe to re-run)

Called from hub/main.py lifespan.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def mark_phantom_trades(session: AsyncSession) -> int:
    """Mark phantom trades and return count of rows updated.

    Criteria:
      - execution_mode IN ('gtc_resting', 'gtc')
      - polymarket_tx_hash IS NULL or empty
      - fill_price IS NULL
      - Not already marked as PHANTOM
    """
    result = await session.execute(text("""
        UPDATE trades
        SET status = 'PHANTOM',
            metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                'phantom_original_status', status,
                'phantom_original_outcome', outcome,
                'phantom_original_pnl', pnl_usd::text,
                'phantom_marked_at', NOW()::text,
                'phantom_reason', 'no_tx_hash_null_fill'
            )
        WHERE execution_mode IN ('gtc_resting', 'gtc')
          AND (polymarket_tx_hash IS NULL OR polymarket_tx_hash = '')
          AND fill_price IS NULL
          AND status != 'PHANTOM'
        RETURNING id
    """))
    rows = result.fetchall()
    return len(rows)
