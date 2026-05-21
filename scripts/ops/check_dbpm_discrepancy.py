"""Daily audit: compare DB trade rows vs Polymarket data-api positions per
recent 5m window. Alert (exit 1) on any gap > $1.

Drives Telegram alert via `||` chaining in cron, so silent writer-gap
regressions become loud within 1h instead of $69 surprise losses.

Run:
    python3 scripts/ops/check_dbpm_discrepancy.py [since_hours=4]

Exit code:
    0  — clean
    1  — discrepancy detected (cron pipes to TG)
    2  — config error
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone


def fetch_pm_positions(funder: str, condition_id: str) -> list[dict]:
    url = (
        f"https://data-api.polymarket.com/positions"
        f"?user={funder}&market={condition_id}&sizeThreshold=0.001"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "novakash-audit/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        print(f"WARN: pm fetch failed for {condition_id[:10]}: {e}", file=sys.stderr)
        return []


async def main(since_hours: int = 4) -> int:
    try:
        import asyncpg
    except ImportError:
        print("asyncpg not installed", file=sys.stderr)
        return 2

    db_url = os.environ.get("DATABASE_URL", "").replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    funder = os.environ.get(
        "POLY_FUNDER_ADDRESS", "0x181D2ED714E0f7Fe9c6e4f13711376eDaab25E10"
    )

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT market_slug,
                       COALESCE(metadata->>'condition_id',
                                metadata->>'conditionId',
                                '') AS condition_id,
                       SUM(stake_usd)::numeric(12,4) AS db_stake_total,
                       COUNT(*) AS db_rows
                FROM trades
                WHERE created_at > $1
                  AND status NOT IN ('CANCELLED','SKIPPED','FAILED_EXECUTION')
                  AND market_slug IS NOT NULL
                GROUP BY 1, 2
                """,
                cutoff,
            )
    finally:
        await pool.close()

    gaps = []
    for r in rows:
        cond = r["condition_id"]
        if not cond:
            continue
        positions = fetch_pm_positions(funder, cond)
        pm_cost = sum(float(p.get("initialValue") or 0) for p in positions)
        db_stake = float(r["db_stake_total"] or 0)
        gap = pm_cost - db_stake
        if abs(gap) > 1.0:
            gaps.append({
                "market_slug": r["market_slug"],
                "condition_id": cond,
                "db_rows": int(r["db_rows"]),
                "db_stake": round(db_stake, 2),
                "pm_cost": round(pm_cost, 2),
                "gap": round(gap, 2),
            })
        time.sleep(0.15)  # be nice to data-api

    if not gaps:
        print(f"OK: no DB↔PM discrepancies > $1 in last {since_hours}h ({len(rows)} markets checked)")
        return 0

    print(f"DISCREPANCY: {len(gaps)} markets where PM cost differs from DB by > $1")
    for g in gaps:
        print(json.dumps(g))
    return 1


if __name__ == "__main__":
    h = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    sys.exit(asyncio.run(main(h)))
