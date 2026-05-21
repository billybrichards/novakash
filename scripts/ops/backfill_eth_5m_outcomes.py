"""Backfill window_snapshots.outcome + oracle_outcome for ETH 5m windows.

Why this exists
---------------
Until 2026-05-20 the bulk forward-writer
(`PgWindowRepository.populate_oracle_outcomes`) hardcoded
`asset='BTC' AND timeframe='5m'`, so every ETH 5m window has
`outcome IS NULL` even though ETH 5m signal_evaluations rows have been
flowing since 15:53 UTC (1,779+ rows by mid-afternoon, with the new
`probability_lgb_v9_2_eth` column populated).

Without `outcome` we cannot compute WR for the ETH 5m signal — so this
script does a one-shot backfill against the public Polymarket Gamma API
for every ETH 5m window in the requested lookback that still has
`oracle_outcome IS NULL`.

Companion code fix: the forward-writer in
`engine/adapters/persistence/pg_window_repo.py` now iterates
`_GAMMA_SLUG_PREFIXES` and covers ETH 5m going forward. This script
only needs to be run once to fill the gap that opened between 15:53 UTC
and the engine redeploy.

What it does
------------
  1. List ETH 5m windows in the lookback range with `oracle_outcome IS NULL`.
  2. Hit `https://gamma-api.polymarket.com/events?slug=eth-updown-5m-<ts>`.
  3. Parse `outcomePrices` to derive UP / DOWN.
  4. UPDATE `window_snapshots` (outcome + oracle_outcome + poly_resolved_outcome
     + poly_winner) — strictly NULL-only, never overwrite existing values.
  5. UPDATE `signal_evaluations.outcome` for every row tied to the window
     — also NULL-only.

Usage
-----
    # SAFE default: dry-run, only the most recent 50 windows
    python3 scripts/ops/backfill_eth_5m_outcomes.py --hours 24 --sample 2 --dry-run

    # Small smoke (writes 2 windows)
    python3 scripts/ops/backfill_eth_5m_outcomes.py --hours 24 --sample 2

    # Full 7d sweep (capped at 50/run for safety)
    python3 scripts/ops/backfill_eth_5m_outcomes.py --hours 168

Flags:
  --hours N          Lookback window (default 24)
  --dry-run          Fetch + derive but do not UPDATE
  --sample N         Only process N windows (smoke test)
  --max-per-run N    Hard cap on writes per invocation (default 50)
  --concurrency N    Parallel Gamma requests (default 6)

Read-only on Polymarket Gamma (no auth, free, Mac-OK per
`feedback_no_local_polymarket.md` — public market data endpoint).
Writes only to `window_snapshots` + `signal_evaluations` rows where
the target columns are still NULL.

Must be run from Montreal (RDS not publicly reachable from Mac).
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
SLUG_PREFIX = "eth-updown-5m-"
ASSET = "ETH"
TIMEFRAME = "5m"


def _parse_outcome(outcomes_raw, prices_raw) -> Optional[str]:
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
                up = str(name).strip().lower().startswith("u")
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


async def _list_null_outcome_windows(
    conn, hours: int, sample: Optional[int]
) -> list[int]:
    """Return ETH 5m windows in the last N hours where outcome OR oracle_outcome
    is still NULL. We backfill BOTH columns from Gamma so either being NULL
    is a candidate.
    """
    rows = await conn.fetch(
        """SELECT DISTINCT window_ts FROM window_snapshots
           WHERE asset = $1 AND timeframe = $2
             AND window_ts < EXTRACT(EPOCH FROM NOW())::bigint - 360
             AND window_ts > EXTRACT(EPOCH FROM NOW())::bigint - ($3 * 3600)
             AND (outcome IS NULL OR oracle_outcome IS NULL)
           ORDER BY window_ts DESC""",
        ASSET,
        TIMEFRAME,
        hours,
    )
    ts_list = [r["window_ts"] for r in rows]
    if sample is not None:
        ts_list = ts_list[:sample]
    return ts_list


async def _apply_outcome(
    conn, window_ts: int, outcome: str, dry_run: bool
) -> tuple[int, int]:
    """UPDATE window_snapshots + signal_evaluations for a single resolved window.

    Returns (snapshot_rows_updated, signal_eval_rows_updated). Both writes
    are strictly NULL-only — existing non-NULL values are preserved.
    """
    if dry_run:
        return (0, 0)

    snap_result = await conn.execute(
        """UPDATE window_snapshots
           SET oracle_outcome        = COALESCE(oracle_outcome, $3),
               poly_resolved_outcome = COALESCE(poly_resolved_outcome, $3),
               poly_winner           = COALESCE(poly_winner, $3),
               outcome               = COALESCE(outcome, $3)
           WHERE asset = $1 AND timeframe = $2 AND window_ts = $4
             AND (outcome IS NULL OR oracle_outcome IS NULL)""",
        ASSET,
        TIMEFRAME,
        outcome,
        window_ts,
    )
    snap_n = int(snap_result.split()[-1]) if snap_result else 0

    se_result = await conn.execute(
        """UPDATE signal_evaluations
           SET outcome = $1
           WHERE window_ts = $2
             AND asset     = $3
             AND timeframe = $4
             AND outcome IS NULL""",
        outcome,
        window_ts,
        ASSET,
        TIMEFRAME,
    )
    se_n = int(se_result.split()[-1]) if se_result else 0
    return (snap_n, se_n)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sample", type=int, default=None)
    ap.add_argument("--max-per-run", type=int, default=50)
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()

    db_url = os.environ.get("DATABASE_URL", "").replace("+asyncpg", "")
    if not db_url:
        print("DATABASE_URL missing", file=sys.stderr)
        sys.exit(2)
    conn = await asyncpg.connect(db_url)

    print("=" * 80)
    print(
        f"ETH 5m OUTCOME BACKFILL — hours={args.hours} dry_run={args.dry_run} "
        f"sample={args.sample} max_per_run={args.max_per_run}"
    )
    print("=" * 80)

    windows = await _list_null_outcome_windows(conn, args.hours, args.sample)
    print(f"\nCandidate ETH 5m windows (outcome OR oracle_outcome NULL): {len(windows)}")
    if args.max_per_run and len(windows) > args.max_per_run:
        print(
            f"Capping to --max-per-run={args.max_per_run} (use a larger value or "
            f"re-run for the rest)."
        )
        windows = windows[: args.max_per_run]
    print(f"Processing: {len(windows)}")

    if not windows:
        print("Nothing to do.")
        await conn.close()
        return

    stats = {
        "attempted": 0,
        "resolved": 0,
        "snapshot_updated": 0,
        "signal_eval_updated": 0,
        "unresolved": 0,
        "errors": 0,
    }

    sem = asyncio.Semaphore(args.concurrency)
    db_lock = asyncio.Lock()

    async with httpx.AsyncClient(
        http2=False,
        headers={"User-Agent": "novakash-eth5m-outcome-backfill/1.0"},
    ) as client:
        async def _process(ts: int):
            async with sem:
                outcome = await _fetch_gamma(client, ts)
            stats["attempted"] += 1
            if not outcome:
                stats["unresolved"] += 1
                return None
            stats["resolved"] += 1
            async with db_lock:
                snap_n, se_n = await _apply_outcome(conn, ts, outcome, args.dry_run)
            stats["snapshot_updated"] += snap_n
            stats["signal_eval_updated"] += se_n
            return outcome

        t0 = time.time()
        results = await asyncio.gather(
            *[_process(ts) for ts in windows], return_exceptions=True
        )
        elapsed = time.time() - t0
        stats["errors"] = sum(1 for r in results if isinstance(r, Exception))

    print(f"\nResult{' (DRY RUN)' if args.dry_run else ''} in {elapsed:.1f}s:")
    print(f"  attempted:                  {stats['attempted']}")
    print(f"  resolved by Gamma:          {stats['resolved']}")
    print(f"  unresolved (not closed yet): {stats['unresolved']}")
    print(f"  errors:                     {stats['errors']}")
    print(f"  window_snapshots rows UPDATEd:    {stats['snapshot_updated']}")
    print(f"  signal_evaluations rows UPDATEd:  {stats['signal_eval_updated']}")

    # Post-verification: how many ETH 5m windows in range still NULL?
    remaining = await conn.fetchrow(
        """SELECT COUNT(*) AS n
           FROM (SELECT DISTINCT window_ts FROM window_snapshots
                 WHERE asset=$1 AND timeframe=$2
                   AND window_ts < EXTRACT(EPOCH FROM NOW())::bigint - 360
                   AND window_ts > EXTRACT(EPOCH FROM NOW())::bigint - ($3 * 3600)
                   AND outcome IS NULL) t""",
        ASSET,
        TIMEFRAME,
        args.hours,
    )
    print(
        f"\nRemaining ETH 5m windows in {args.hours}h with outcome IS NULL: "
        f"{remaining['n']}"
    )

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
