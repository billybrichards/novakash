"""Scan trades table for (strategy_id, window_ts, direction) tuples with >1
non-cancelled fill.

Run modes:
  - Engine startup self-check (last 7d) — log WARN if anything found
  - Daily cron (default --hours 24) — TG alert if anything found

Exit code 0 if clean, 1 if multi-fire detected (so cron `||` can alert).

Usage:
    python3 scripts/ops/check_multi_fire.py [hours]   # default 24
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone


async def main(since_hours: int = 24) -> int:
    try:
        import asyncpg
    except ImportError:
        print("asyncpg not installed", file=sys.stderr)
        return 2

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    # Strip asyncpg+ scheme variant if present
    db_url = db_url.replace("postgresql+asyncpg://", "postgresql://", 1)

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT strategy_id,
                       direction,
                       COALESCE(metadata->>'window_ts','') AS window_ts,
                       COALESCE(metadata->>'asset','BTC')   AS asset,
                       COALESCE(metadata->>'timeframe','5m') AS timeframe,
                       COUNT(*) AS n_fires,
                       SUM(stake_usd)::numeric(12,4) AS total_stake,
                       ARRAY_AGG(id ORDER BY created_at) AS trade_ids,
                       ARRAY_AGG(created_at ORDER BY created_at) AS fired_at
                FROM trades
                WHERE created_at > $1
                  AND status NOT IN ('CANCELLED','SKIPPED','FAILED_EXECUTION')
                  AND strategy_id IS NOT NULL
                  AND COALESCE(metadata->>'window_ts','') != ''
                GROUP BY 1,2,3,4,5
                HAVING COUNT(*) > 1
                ORDER BY n_fires DESC, fired_at DESC
                """,
                cutoff,
            )
    finally:
        await pool.close()

    if not rows:
        print(f"OK: no multi-fire detected in last {since_hours}h")
        return 0

    print(
        f"MULTI-FIRE DETECTED: {len(rows)} "
        f"(strategy, window, direction) cells in last {since_hours}h"
    )
    for r in rows:
        payload = {
            "strategy_id": r["strategy_id"],
            "direction": r["direction"],
            "window_ts": r["window_ts"],
            "asset": r["asset"],
            "timeframe": r["timeframe"],
            "n_fires": r["n_fires"],
            "total_stake": str(r["total_stake"]),
            "trade_ids": [int(t) for t in r["trade_ids"]],
            "fired_at": [d.isoformat() for d in r["fired_at"]],
        }
        print(json.dumps(payload))
    return 1


if __name__ == "__main__":
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    sys.exit(asyncio.run(main(hours)))
