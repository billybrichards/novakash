"""Backfill ``trades.redeemed``/``redeemed_at``/``redemption_tx`` from on-chain truth.

Background — writer regression #7 (2026-05-01)
----------------------------------------------
The on-chain redemption path (primary on Montreal since 2026-04-16) silently
skipped the trades writeback. Result: ``trades.redeemed`` stayed false on
every WIN row despite real on-chain redemptions confirming. The forward
fix lives in ``engine/execution/redeemer.py::redeem_position_onchain``.

This script cleans up the historic gap. It pulls every REDEEM activity
event from ``data-api.polymarket.com/activity`` for the funder wallet,
matches each redeemed condition_id to its WIN trade rows on Montreal RDS,
and runs the same UPDATE the live writer now runs:

    UPDATE trades
       SET redeemed      = true,
           redeemed_at   = <activity.timestamp>,
           redemption_tx = <activity.transactionHash>
     WHERE metadata->>'condition_id' = <activity.conditionId>
       AND outcome = 'WIN'
       AND redeemed = false
       AND is_live = true
       AND fill_size IS NOT NULL
       AND fill_size > 0

Idempotent — only touches rows where ``redeemed = false``. Safe to re-run.

Usage
-----
DRY-RUN (default — prints intended UPDATEs, touches nothing):

    ssh novakash@... 'cd /home/novakash/novakash && \\
        set -a && source engine/.env && set +a && \\
        python3 scripts/ops/backfill_redeemed_flag.py'

EXECUTE (writes to RDS):

    python3 scripts/ops/backfill_redeemed_flag.py --execute

Optional flags:

    --hours N         Look back N hours (default 168 = 7d).
    --condition-id X  Backfill a single condition_id only (debug aid).
    --verbose         Print every match, including no-op rows.

Constraints (per memory note ``feedback_no_local_polymarket.md``):
  - data-api.polymarket.com calls must run from Montreal — never from
    a Mac. Polygon RPC calls would be fine from Mac, but this script
    uses the activity API which is *.polymarket.com.
  - Read-only on RDS by default. ``--execute`` is the only write path.

Constraints (per memory note ``reference_wallet_truth.md``):
  - On-chain activity feed is the canonical truth for "what's
    redeemed". Don't try to reconstruct from the trades table.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Optional

# Load engine env (mirrors wallet_truth.py)
try:
    from dotenv import load_dotenv  # type: ignore
    for env_path in (
        "/home/novakash/novakash/engine/.env",
        os.path.expanduser("~/Code/novakash/engine/.env"),
        ".env",
    ):
        if os.path.exists(env_path):
            load_dotenv(env_path)
            break
except ImportError:
    pass


def _funder() -> str:
    addr = os.environ.get("POLY_FUNDER_ADDRESS")
    if not addr:
        sys.exit("POLY_FUNDER_ADDRESS not set — load engine/.env first")
    return addr


def _db_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL not set — load engine/.env first")
    # asyncpg wants postgresql:// not postgresql+asyncpg://
    return url.replace("postgresql+asyncpg://", "postgresql://")


def _fetch_redeems(since_ts: int) -> list[dict]:
    """Pull REDEEM events from the Polymarket activity API.

    Returns dicts with: conditionId, timestamp, usdcSize, transactionHash.
    Paginates 500 at a time, stops at since_ts or 3000-row hard limit.
    """
    addr = _funder()
    rows: list[dict] = []
    offset = 0
    while offset < 3000:
        url = (
            "https://data-api.polymarket.com/activity?user="
            + addr + "&limit=500&offset=" + str(offset)
        )
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0"}
        )
        page = json.loads(urllib.request.urlopen(req, timeout=15).read())
        if not page:
            break
        rows.extend(page)
        if len(page) < 500:
            break
        offset += 500
        # Stop early if oldest row is older than window — page is reverse-chrono
        if page[-1]["timestamp"] < since_ts:
            break
    return [
        r for r in rows
        if r["type"] == "REDEEM"
        and r["timestamp"] >= since_ts
        and float(r.get("usdcSize", 0)) > 0.0
        # Only WIN-side redeems matter for the trades.redeemed=true flag.
        # Loss-side redeems pay $0 and we filter is_live + outcome=WIN
        # in the UPDATE anyway, but excluding here keeps the log readable.
    ]


def _consolidate(redeems: list[dict]) -> dict[str, dict]:
    """Collapse multiple redeem events on the same condition_id to one row.

    Keeps the LATEST timestamp (most recent redemption) and the tx_hash
    of that latest event. The DB UPDATE is idempotent per-condition,
    so re-running with multiple events for the same cond is harmless,
    but we want stable output.
    """
    by_cond: dict[str, dict] = {}
    for r in redeems:
        cid = r.get("conditionId")
        if not cid:
            continue
        existing = by_cond.get(cid)
        if existing is None or r["timestamp"] > existing["timestamp"]:
            by_cond[cid] = r
    return by_cond


async def _backfill(
    *,
    redeems_by_cond: dict[str, dict],
    execute: bool,
    verbose: bool,
) -> dict[str, int]:
    """Apply (or simulate) the UPDATE for each condition_id.

    Returns a counters dict: scanned, matched, updated, skipped_already_set.
    """
    try:
        import asyncpg  # type: ignore
    except ImportError:
        sys.exit("asyncpg not installed — pip install asyncpg")

    counters = {
        "scanned": len(redeems_by_cond),
        "matched": 0,
        "updated": 0,
        "skipped_already_set": 0,
        "no_matching_trade": 0,
    }

    conn = await asyncpg.connect(_db_url())
    try:
        for cid, ev in redeems_by_cond.items():
            redeemed_at = datetime.fromtimestamp(
                ev["timestamp"], tz=timezone.utc
            )
            tx_hash = ev.get("transactionHash") or ev.get("transaction_hash")

            # Probe matching candidate rows (read-only).
            candidates = await conn.fetch(
                """
                SELECT id, redeemed, redeemed_at, redemption_tx, fill_size
                  FROM trades
                 WHERE metadata->>'condition_id' = $1
                   AND outcome = 'WIN'
                   AND is_live = true
                   AND fill_size IS NOT NULL
                   AND fill_size > 0
                """,
                cid,
            )
            if not candidates:
                counters["no_matching_trade"] += 1
                if verbose:
                    print(
                        f"  [no-match] {cid[:14]}.. (redeemed on-chain at "
                        f"{redeemed_at.isoformat()}, $"
                        f"{float(ev.get('usdcSize', 0)):.2f})"
                    )
                continue

            counters["matched"] += 1
            already = sum(1 for c in candidates if c["redeemed"])
            todo = [c for c in candidates if not c["redeemed"]]

            if not todo:
                counters["skipped_already_set"] += already
                if verbose:
                    print(
                        f"  [already] {cid[:14]}.. {already} row(s) already "
                        f"redeemed=true — no-op"
                    )
                continue

            if execute:
                result = await conn.execute(
                    """
                    UPDATE trades
                       SET redeemed      = true,
                           redeemed_at   = $1,
                           redemption_tx = $2
                     WHERE metadata->>'condition_id' = $3
                       AND outcome = 'WIN'
                       AND redeemed = false
                       AND is_live = true
                       AND fill_size IS NOT NULL
                       AND fill_size > 0
                    """,
                    redeemed_at,
                    tx_hash,
                    cid,
                )
                try:
                    n = int(result.split()[-1])
                except (ValueError, IndexError):
                    n = 0
                counters["updated"] += n
                print(
                    f"  [UPDATED] {cid[:14]}.. "
                    f"rows={n} tx={tx_hash[:14] if tx_hash else 'null'}.. "
                    f"at={redeemed_at.isoformat()}"
                )
            else:
                # Dry-run — show what we would do.
                ids = ",".join(str(c["id"]) for c in todo)
                print(
                    f"  [DRY] {cid[:14]}.. would UPDATE {len(todo)} row(s) "
                    f"id={ids} tx={tx_hash[:14] if tx_hash else 'null'}.. "
                    f"at={redeemed_at.isoformat()}"
                )
                counters["updated"] += len(todo)
    finally:
        await conn.close()

    return counters


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hours", type=int, default=168,
        help="Lookback window in hours (default 168 = 7 days).",
    )
    parser.add_argument(
        "--condition-id", default=None,
        help="Backfill a single condition_id only (debug aid).",
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually run the UPDATEs. Default is DRY-RUN.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print no-op rows too.",
    )
    args = parser.parse_args()

    since_ts = int(datetime.now(timezone.utc).timestamp()) - args.hours * 3600
    print(f"== backfill_redeemed_flag ==")
    print(f"  mode:   {'EXECUTE' if args.execute else 'DRY-RUN'}")
    print(f"  window: {args.hours}h (since ts={since_ts})")
    print(f"  funder: {_funder()}")

    print("\nFetching REDEEM events from data-api.polymarket.com ...")
    redeems = _fetch_redeems(since_ts)
    if args.condition_id:
        redeems = [r for r in redeems if r.get("conditionId") == args.condition_id]
    redeems_by_cond = _consolidate(redeems)
    print(
        f"  {len(redeems)} REDEEM events  -> "
        f"{len(redeems_by_cond)} unique condition_ids"
    )

    if not redeems_by_cond:
        print("\nNothing to backfill.")
        return

    print("\nRunning UPDATEs (idempotent, only touches redeemed=false rows)...")
    counters = asyncio.run(
        _backfill(
            redeems_by_cond=redeems_by_cond,
            execute=args.execute,
            verbose=args.verbose,
        )
    )

    print("\n== summary ==")
    for k, v in counters.items():
        print(f"  {k:>22s}: {v}")
    if not args.execute:
        print("\n(dry-run — re-run with --execute to apply.)")


if __name__ == "__main__":
    main()
