"""Backfill window_snapshots.oracle_outcome + actual_direction from Polymarket Gamma.

Why this exists
---------------
Audit 2026-04-18 found 42.9% of window_snapshots.actual_direction labels in the
last 24h disagreed with delta_chainlink sign. Root cause:
``pg_window_repo.label_resolved_windows`` stamped labels from Binance
open_price/close_price columns, but Polymarket 5m markets resolve via the
Chainlink oracle. Binance tape vs Chainlink read can diverge on volatile
windows — exactly the windows that matter.

This script:
  1. Lists closed 5m windows in the requested lookback range.
  2. Queries Polymarket Gamma ``events?slug=btc-updown-5m-<ts>`` for each.
  3. Parses ``outcomePrices`` to derive the authoritative UP/DOWN winner.
  4. Writes ``oracle_outcome`` + ``poly_resolved_outcome`` on window_snapshots.
  5. Force-rewrites ``actual_direction`` using new priority chain (oracle >
     chainlink_close vs chainlink_open > delta_chainlink sign > NULL).

Usage
-----
    ssh novakash@15.223.247.178 \
      'python3 /home/novakash/novakash/scripts/ops/backfill_oracle_outcomes.py --hours 48'

Flags:
  --hours N          Lookback window (default 48)
  --dry-run          Fetch + derive but do not UPDATE
  --sample N         Only process N windows (smoke test)
  --no-gamma         Skip Gamma poll, just re-label from existing chainlink cols

Read-only on Polymarket Gamma (no auth, free). Writes only to Railway
window_snapshots rows that lack oracle_outcome OR whose actual_direction
disagrees with the oracle.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Optional

import httpx

try:
    import asyncpg  # type: ignore
except ImportError:
    print("asyncpg missing — install in engine venv", file=sys.stderr)
    sys.exit(1)

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(os.environ.get("ENGINE_ENV_PATH", "/home/novakash/novakash/engine/.env"))
except Exception:
    pass

GAMMA_BASE = "https://gamma-api.polymarket.com"
SLUG_PREFIX = "btc-updown-5m-"
ASSET = "BTC"
TIMEFRAME = "5m"


def _parse_outcome(outcomes_raw: str, prices_raw: str) -> Optional[str]:
    """Return 'UP' / 'DOWN' / None based on Gamma outcomePrices."""
    try:
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
    except Exception:
        return None
    if not isinstance(outcomes, list) or not isinstance(prices, list):
        return None
    if len(outcomes) != len(prices):
        return None
    for name, price in zip(outcomes, prices):
        try:
            if float(price) >= 0.999:
                up = name.strip().lower().startswith("u")
                return "UP" if up else "DOWN"
        except (TypeError, ValueError):
            continue
    return None


async def _fetch_gamma(
    client: httpx.AsyncClient, window_ts: int, retries: int = 2
) -> Optional[str]:
    slug = f"{SLUG_PREFIX}{window_ts}"
    for attempt in range(retries + 1):
        try:
            r = await client.get(
                f"{GAMMA_BASE}/events", params={"slug": slug}, timeout=12.0
            )
            if r.status_code != 200:
                continue
            data = r.json()
            if not isinstance(data, list) or not data:
                return None
            for event in data:
                for m in event.get("markets", []) or []:
                    if m.get("slug") != slug:
                        continue
                    if not m.get("closed"):
                        return None
                    if m.get("umaResolutionStatus") not in (None, "resolved"):
                        return None
                    return _parse_outcome(
                        m.get("outcomes") or "[]",
                        m.get("outcomePrices") or "[]",
                    )
            return None
        except Exception:
            if attempt == retries:
                return None
            await asyncio.sleep(0.5 * (attempt + 1))
    return None


async def _list_closed_windows(conn, hours: int, sample: Optional[int]) -> list[int]:
    rows = await conn.fetch(
        """SELECT DISTINCT window_ts FROM window_snapshots
           WHERE asset = $1 AND timeframe = $2
             AND window_ts < EXTRACT(EPOCH FROM NOW())::bigint - 360
             AND window_ts > EXTRACT(EPOCH FROM NOW())::bigint - ($3 * 3600)
           ORDER BY window_ts DESC""",
        ASSET,
        TIMEFRAME,
        hours,
    )
    ts_list = [r["window_ts"] for r in rows]
    if sample:
        ts_list = ts_list[:sample]
    return ts_list


async def _write_oracle(conn, window_ts: int, outcome: str, dry_run: bool) -> int:
    if dry_run:
        return 0
    result = await conn.execute(
        """UPDATE window_snapshots
           SET oracle_outcome = $3,
               poly_resolved_outcome = $3,
               poly_winner = $3
           WHERE asset = $1 AND timeframe = $2 AND window_ts = $4
             AND (oracle_outcome IS DISTINCT FROM $3
                  OR poly_resolved_outcome IS DISTINCT FROM $3
                  OR poly_winner IS DISTINCT FROM $3)""",
        ASSET,
        TIMEFRAME,
        outcome,
        window_ts,
    )
    return int(result.split()[-1]) if result else 0


async def _force_relabel(conn, hours: int, dry_run: bool) -> dict:
    """Rewrite actual_direction on all windows in range using new priority chain.

    Unlike pg_window_repo.label_resolved_windows, this overwrites non-NULL values
    because the prior labeler produced 42.9% wrong labels.
    """
    select_sql = """
        SELECT ws.window_ts, ws.actual_direction AS old_label,
               CASE
                   WHEN ws.oracle_outcome IN ('UP','DOWN') THEN ws.oracle_outcome
                   WHEN wp.chainlink_close IS NOT NULL
                        AND wp.chainlink_open IS NOT NULL
                        AND wp.chainlink_close > wp.chainlink_open THEN 'UP'
                   WHEN wp.chainlink_close IS NOT NULL
                        AND wp.chainlink_open IS NOT NULL
                        AND wp.chainlink_close < wp.chainlink_open THEN 'DOWN'
                   WHEN ws.delta_chainlink IS NOT NULL
                        AND ws.delta_chainlink > 0 THEN 'UP'
                   WHEN ws.delta_chainlink IS NOT NULL
                        AND ws.delta_chainlink < 0 THEN 'DOWN'
                   ELSE NULL
               END AS new_label,
               CASE
                   WHEN ws.oracle_outcome IN ('UP','DOWN') THEN 'oracle'
                   WHEN wp.chainlink_close IS NOT NULL AND wp.chainlink_open IS NOT NULL THEN 'chainlink_wp'
                   WHEN ws.delta_chainlink IS NOT NULL THEN 'delta_chainlink'
                   ELSE 'none'
               END AS source
        FROM window_snapshots ws
        LEFT JOIN window_predictions wp
          ON wp.window_ts = ws.window_ts AND wp.asset = ws.asset AND wp.timeframe = ws.timeframe
        WHERE ws.asset = $1 AND ws.timeframe = $2
          AND ws.window_ts < EXTRACT(EPOCH FROM NOW())::bigint - 360
          AND ws.window_ts > EXTRACT(EPOCH FROM NOW())::bigint - ($3 * 3600)
    """
    rows = await conn.fetch(select_sql, ASSET, TIMEFRAME, hours)
    unchanged = flipped = filled = skipped = 0
    by_source = {"oracle": 0, "chainlink_wp": 0, "delta_chainlink": 0, "none": 0}
    for r in rows:
        by_source[r["source"]] += 1
        if r["new_label"] is None:
            skipped += 1
            continue
        if r["old_label"] == r["new_label"]:
            unchanged += 1
        elif r["old_label"] is None:
            filled += 1
        else:
            flipped += 1
    if not dry_run:
        await conn.execute(
            """WITH labels AS (
                   SELECT ws.window_ts, ws.asset, ws.timeframe,
                          CASE
                              WHEN ws.oracle_outcome IN ('UP','DOWN') THEN ws.oracle_outcome
                              WHEN wp.chainlink_close IS NOT NULL
                                   AND wp.chainlink_open IS NOT NULL
                                   AND wp.chainlink_close > wp.chainlink_open THEN 'UP'
                              WHEN wp.chainlink_close IS NOT NULL
                                   AND wp.chainlink_open IS NOT NULL
                                   AND wp.chainlink_close < wp.chainlink_open THEN 'DOWN'
                              WHEN ws.delta_chainlink IS NOT NULL
                                   AND ws.delta_chainlink > 0 THEN 'UP'
                              WHEN ws.delta_chainlink IS NOT NULL
                                   AND ws.delta_chainlink < 0 THEN 'DOWN'
                              ELSE NULL
                          END AS label
                   FROM window_snapshots ws
                   LEFT JOIN window_predictions wp
                     ON wp.window_ts = ws.window_ts
                    AND wp.asset     = ws.asset
                    AND wp.timeframe = ws.timeframe
                   WHERE ws.asset = $1 AND ws.timeframe = $2
                     AND ws.window_ts < EXTRACT(EPOCH FROM NOW())::bigint - 360
                     AND ws.window_ts > EXTRACT(EPOCH FROM NOW())::bigint - ($3 * 3600)
               )
               UPDATE window_snapshots ws
               SET actual_direction = labels.label
               FROM labels
               WHERE ws.window_ts = labels.window_ts
                 AND ws.asset     = labels.asset
                 AND ws.timeframe = labels.timeframe
                 AND labels.label IS NOT NULL
                 AND ws.actual_direction IS DISTINCT FROM labels.label""",
            ASSET,
            TIMEFRAME,
            hours,
        )
    return {
        "total": len(rows),
        "filled_from_null": filled,
        "flipped_wrong": flipped,
        "unchanged": unchanged,
        "skipped_null": skipped,
        "by_source": by_source,
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=48)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sample", type=int, default=None)
    ap.add_argument("--no-gamma", action="store_true")
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()

    db_url = os.environ.get("DATABASE_URL", "").replace("+asyncpg", "")
    if not db_url:
        print("DATABASE_URL missing", file=sys.stderr)
        sys.exit(2)
    conn = await asyncpg.connect(db_url)

    print("=" * 80)
    print(f"ORACLE BACKFILL — asset={ASSET} tf={TIMEFRAME} hours={args.hours} "
          f"dry_run={args.dry_run} no_gamma={args.no_gamma}")
    print("=" * 80)

    windows = await _list_closed_windows(conn, args.hours, args.sample)
    print(f"\nClosed windows in range: {len(windows)}")

    gamma_stats = {"attempted": 0, "resolved": 0, "updated": 0, "errors": 0}
    if not args.no_gamma and windows:
        sem = asyncio.Semaphore(args.concurrency)
        db_lock = asyncio.Lock()
        async with httpx.AsyncClient(
            http2=False, headers={"User-Agent": "novakash-oracle-backfill/1.0"}
        ) as client:
            async def _process(ts):
                async with sem:
                    outcome = await _fetch_gamma(client, ts)
                gamma_stats["attempted"] += 1
                if outcome:
                    gamma_stats["resolved"] += 1
                    async with db_lock:
                        updated = await _write_oracle(conn, ts, outcome, args.dry_run)
                    gamma_stats["updated"] += updated
                return outcome

            t0 = time.time()
            results = await asyncio.gather(
                *[_process(ts) for ts in windows], return_exceptions=True
            )
            elapsed = time.time() - t0
            gamma_stats["errors"] = sum(1 for r in results if isinstance(r, Exception))
            print(f"\nGamma poll: {gamma_stats['resolved']}/{gamma_stats['attempted']} resolved "
                  f"({gamma_stats['updated']} rows updated) in {elapsed:.1f}s  "
                  f"errors={gamma_stats['errors']}")

    report = await _force_relabel(conn, args.hours, args.dry_run)
    print(f"\nRelabel report{' (DRY RUN)' if args.dry_run else ''}:")
    print(f"  total in range:    {report['total']}")
    print(f"  filled from NULL:  {report['filled_from_null']}")
    print(f"  flipped (was WRONG): {report['flipped_wrong']}")
    print(f"  unchanged:         {report['unchanged']}")
    print(f"  skipped (no data): {report['skipped_null']}")
    print(f"  by source: {report['by_source']}")

    # Post-verification: divergence between new actual_direction and delta_chainlink sign
    div = await conn.fetchrow(
        """SELECT COUNT(*) FILTER (WHERE actual_direction='UP' AND delta_chainlink<0) AS up_cl_down,
                  COUNT(*) FILTER (WHERE actual_direction='DOWN' AND delta_chainlink>0) AS down_cl_up,
                  COUNT(*) FILTER (WHERE actual_direction IS NOT NULL AND delta_chainlink IS NOT NULL) AS both
           FROM window_snapshots
           WHERE asset=$1 AND timeframe=$2
             AND window_ts > EXTRACT(EPOCH FROM NOW())::bigint - ($3 * 3600)""",
        ASSET, TIMEFRAME, args.hours,
    )
    mis = (div["up_cl_down"] or 0) + (div["down_cl_up"] or 0)
    tot = div["both"] or 0
    print(f"\nPost-backfill divergence (actual_direction vs delta_chainlink sign):")
    print(f"  total with both:   {tot}")
    print(f"  mismatch:          {mis}")
    if tot:
        print(f"  mismatch rate:     {100*mis/tot:.2f}%")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
