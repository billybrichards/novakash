"""
Backfill correct pnl_usd for trades inflated by the position-aggregate bug.

Bug
===
Two paths historically wrote `pnl_usd = -position_cost` from Polymarket's
`/positions` endpoint. When 2+ strategies bet the same condition_id with
the same direction, they share a token_id; Polymarket aggregates the
trades into ONE position. Writing the aggregate cost to a single matched
trade row inflated that row's pnl by 2x, 3x, even 5x.

Locations fixed (in PR alongside this script):
  - engine/reconciliation/reconciler.py:363 (backfill_on_startup)
  - engine/infrastructure/runtime.py:4871   (position_monitor loop)

Effect
======
DB-reported P&L over-states losses. TG cards' "Today: NW/ML (XX%, -$YY)"
line uses DB pnl_usd → looks worse than reality. On-chain wallet truth
is correct (USDC/pUSD balances). Just the accounting in trades.pnl_usd
that's wrong.

Detection rule
==============
A trade is "inflated" if `|pnl_usd| > stake_usd + 0.50` (with $0.50
slack for rounding/fees). Real losses always equal -stake_usd; real
wins always equal fill_size - stake_usd which can be larger than
stake_usd, so we only correct rows where status='RESOLVED_LOSS'
AND |pnl_usd| > stake_usd. Wins are left alone.

Usage
=====
On Montreal:
    cd /home/novakash/novakash
    set -a && source engine/.env && set +a
    python3 scripts/ops/backfill_pnl_inflation.py            # dry-run (default)
    python3 scripts/ops/backfill_pnl_inflation.py --execute  # apply

Safety
======
- Dry-run by default
- Only touches RESOLVED_LOSS rows where pnl > stake (impossible state)
- Only sets pnl_usd to -stake_usd (the mathematically correct value)
- Does NOT touch fill_size, status, resolved_at, anything else
- Idempotent: re-running on corrected rows is a no-op
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys


async def amain(execute: bool) -> int:
    import asyncpg  # type: ignore

    dsn_raw = os.environ.get("DATABASE_URL", "")
    if not dsn_raw:
        print("ERROR: DATABASE_URL not set — load engine/.env first")
        return 2
    dsn = dsn_raw.replace("postgresql+asyncpg://", "postgresql://").replace("+asyncpg", "")

    conn = await asyncpg.connect(dsn)
    try:
        # 1) Count + sample
        rows = await conn.fetch(
            """
            SELECT id, strategy_id, direction, fill_price, stake_usd, pnl_usd, fill_size,
                   ABS(pnl_usd) - stake_usd AS overstatement
            FROM trades
            WHERE status = 'RESOLVED_LOSS'
              AND is_live = true
              AND pnl_usd IS NOT NULL
              AND ABS(pnl_usd) > stake_usd + 0.50
            ORDER BY (ABS(pnl_usd) - stake_usd) DESC
            """
        )
        print(f"Found {len(rows)} loss rows with inflated pnl_usd (|pnl| > stake + 0.50):")
        if not rows:
            print("  Nothing to backfill. ✓")
            return 0

        total_overstated = 0.0
        for r in rows[:20]:  # show top 20
            ov = float(r["overstatement"])
            total_overstated += ov
            print(
                f"  tid={r['id']:6d}  {r['strategy_id'][:14]:14s}  {r['direction']:4s}  "
                f"stake=${float(r['stake_usd']):6.2f}  "
                f"pnl_db=${float(r['pnl_usd']):8.2f}  "
                f"overstatement=${ov:6.2f}"
            )
        if len(rows) > 20:
            print(f"  ... +{len(rows) - 20} more rows not shown")

        sum_over = await conn.fetchval(
            """
            SELECT COALESCE(SUM(ABS(pnl_usd) - stake_usd), 0)
            FROM trades
            WHERE status = 'RESOLVED_LOSS' AND is_live = true
              AND pnl_usd IS NOT NULL
              AND ABS(pnl_usd) > stake_usd + 0.50
            """
        )
        print(f"\nTotal cumulative overstatement: ${float(sum_over):.2f}")

        if not execute:
            print("\n[dry-run] Re-run with --execute to apply.")
            return 0

        # 2) Apply correction: pnl_usd = -stake_usd for all matching rows
        result = await conn.execute(
            """
            UPDATE trades
            SET pnl_usd = -CAST(stake_usd AS numeric)
            WHERE status = 'RESOLVED_LOSS'
              AND is_live = true
              AND pnl_usd IS NOT NULL
              AND ABS(pnl_usd) > stake_usd + 0.50
            """
        )
        affected = 0
        try:
            affected = int(str(result).split()[-1])
        except (ValueError, IndexError):
            pass
        print(f"\n[done] Updated {affected} row(s). pnl_usd now = -stake_usd for each.")
        return 0
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill pnl_usd for inflated loss rows.")
    parser.add_argument("--execute", action="store_true", help="Apply the correction (otherwise dry-run).")
    args = parser.parse_args()
    try:
        return asyncio.run(amain(execute=args.execute))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
