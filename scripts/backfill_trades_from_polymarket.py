#!/usr/bin/env python3
"""
Backfill poly_fills table from Polymarket data-api.

poly_fills is the authoritative source-of-truth for every CLOB fill
on our proxy wallet. The table is append-only; rows are tagged with
`source='data-api'` so we can distinguish them from engine-reported
trades and detect multi-fills.

This script:
1. Fetches trades from data-api.polymarket.com (paginated)
2. Groups by conditionId + 2min window to detect multi-fills
3. INSERT ... ON CONFLICT DO NOTHING by transaction_hash (append-only)
4. Attempts to link each fill to a trade_bible entry by market_slug
5. Reports discrepancies: gross spend vs tracked stake, multi-fill rate

NEVER overwrites existing rows — the on-chain record is immutable.

Usage:
    # Backfill last 48h (writes to DB)
    python3 scripts/backfill_trades_from_polymarket.py --hours 48

    # Dry run (report only)
    python3 scripts/backfill_trades_from_polymarket.py --hours 48 --dry-run

    # Link orphan fills to trade_bible where possible
    python3 scripts/backfill_trades_from_polymarket.py --hours 48 --link

Montreal rules: this script runs against Railway DB (hub DB URL).
Read-only against Polymarket data-api. Safe to run on any host.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    print("Install: pip3 install requests", file=sys.stderr)
    sys.exit(1)

try:
    import psycopg2
    from psycopg2.extras import Json, execute_values
except ImportError:
    print("Install: pip3 install psycopg2-binary", file=sys.stderr)
    sys.exit(1)


POLY_FUNDER_ADDRESS = "0x181D2ED714E0f7Fe9c6e4f13711376eDaab25E10"
DATA_API_URL = "https://data-api.polymarket.com/trades"
PAGE_LIMIT = 500
MULTI_FILL_WINDOW_SECONDS = 120  # Fills within 2min of same condition = multi-fill


def _get_db_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url.replace("postgresql+asyncpg://", "postgresql://", 1)

    repo_root = Path(__file__).resolve().parent.parent
    for candidate in (repo_root / "engine" / ".env", repo_root / "engine" / ".env.local"):
        if not candidate.exists():
            continue
        for line in candidate.read_text().splitlines():
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                value = line.split("=", 1)[1].strip()
                return value.replace("postgresql+asyncpg://", "postgresql://", 1)

    raise RuntimeError("DATABASE_URL not found in env or engine/.env[.local]")


def fetch_trades(user: str, since_ts: int) -> list[dict[str, Any]]:
    """Fetch trades from data-api, paginating backwards in time."""
    trades: list[dict[str, Any]] = []
    offset = 0
    seen_hashes: set[str] = set()
    while True:
        resp = requests.get(
            DATA_API_URL,
            params={"user": user, "limit": PAGE_LIMIT, "offset": offset},
            timeout=20,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break

        new_in_batch = 0
        oldest_in_batch = float("inf")
        for t in batch:
            tx = t.get("transactionHash")
            if tx and tx in seen_hashes:
                continue
            if tx:
                seen_hashes.add(tx)
            trades.append(t)
            new_in_batch += 1
            oldest_in_batch = min(oldest_in_batch, t.get("timestamp", 0))

        if oldest_in_batch < since_ts or len(batch) < PAGE_LIMIT or new_in_batch == 0:
            break
        offset += PAGE_LIMIT

    return [t for t in trades if t.get("timestamp", 0) >= since_ts]


def detect_multi_fills(trades: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group trades by conditionId and sort by time. Returns cond -> [fills]."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        if t.get("side") != "BUY":
            continue
        cond = t.get("conditionId") or ""
        groups[cond].append(t)

    for cond in groups:
        groups[cond].sort(key=lambda x: x.get("timestamp", 0))

    return groups


def upsert_poly_fills(
    conn,
    trades: list[dict[str, Any]],
    multi_fills: dict[str, list[dict[str, Any]]],
    dry_run: bool = False,
) -> tuple[int, int]:
    """Insert rows into poly_fills (append-only, skip duplicates by tx hash)."""
    rows = []
    for t in trades:
        tx = t.get("transactionHash")
        if not tx:
            # Skip trades with no tx hash — can't dedupe safely
            continue

        asset_token = t.get("asset", "")
        cond = t.get("conditionId", "")
        slug = t.get("slug", "")
        side = t.get("side", "")
        outcome = t.get("outcome", "")
        price = float(t.get("price", 0))
        size = float(t.get("size", 0))
        fee = float(t.get("fee", 0)) if t.get("fee") is not None else None

        ts = int(t.get("timestamp", 0))
        match_utc = datetime.fromtimestamp(ts, tz=timezone.utc)

        # Compute multi-fill metadata
        group = multi_fills.get(cond, [])
        total = len(group)
        try:
            index = group.index(t) + 1
        except ValueError:
            index = None
        is_multi = total > 1

        rows.append((
            tx, asset_token, cond, slug,
            side, outcome, price, size, fee,
            ts, match_utc,
            None,                  # trade_bible_id — filled by --link pass
            None,                  # clob_order_id — unknown from data-api
            "data-api",            # source tag
            datetime.now(timezone.utc),  # verified_at
            is_multi, index, total,
            Json(t),               # raw_payload
        ))

    if not rows:
        return 0, 0

    if dry_run:
        return len(rows), 0

    with conn.cursor() as cur:
        result = execute_values(
            cur,
            """
            INSERT INTO poly_fills (
                transaction_hash, asset_token_id, condition_id, market_slug,
                side, outcome, price, size, fee_usd,
                match_timestamp, match_time_utc,
                trade_bible_id, clob_order_id,
                source, verified_at,
                is_multi_fill, multi_fill_index, multi_fill_total,
                raw_payload
            ) VALUES %s
            ON CONFLICT (transaction_hash) DO NOTHING
            RETURNING transaction_hash
            """,
            rows,
            fetch=True,
        )
        inserted = len(result) if result else 0
        skipped = len(rows) - inserted
    conn.commit()
    return inserted, skipped


def link_orphans_to_trade_bible(conn, dry_run: bool = False) -> tuple[int, int]:
    """Link unlinked poly_fills to trade_bible via market_slug matching."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pf.id, pf.market_slug, pf.match_time_utc
            FROM poly_fills pf
            WHERE pf.trade_bible_id IS NULL
              AND pf.market_slug IS NOT NULL
              AND pf.side = 'BUY'
            """
        )
        unlinked = cur.fetchall()

    if not unlinked:
        return 0, 0

    linked = 0
    with conn.cursor() as cur:
        for fill_id, slug, match_time in unlinked:
            if not slug:
                continue
            # Match by market_slug + placed within 10 minutes of fill
            cur.execute(
                """
                SELECT id FROM trade_bible
                WHERE market_slug = %s
                  AND placed_at BETWEEN %s AND %s
                  AND is_live = true
                ORDER BY ABS(EXTRACT(EPOCH FROM (placed_at - %s))) ASC
                LIMIT 1
                """,
                (slug, match_time - timedelta(minutes=10), match_time + timedelta(minutes=5), match_time),
            )
            row = cur.fetchone()
            if row and not dry_run:
                cur.execute(
                    "UPDATE poly_fills SET trade_bible_id = %s WHERE id = %s",
                    (row[0], fill_id),
                )
                linked += 1

    if not dry_run:
        conn.commit()

    return linked, len(unlinked) - linked


def report_discrepancies(conn, trades: list[dict[str, Any]], multi_fills: dict[str, list[dict[str, Any]]]) -> None:
    """Print multi-fill breakdown and trade_bible gap."""
    total_windows = sum(1 for _ in multi_fills.values())
    single = sum(1 for g in multi_fills.values() if len(g) == 1)
    double = sum(1 for g in multi_fills.values() if len(g) == 2)
    triple = sum(1 for g in multi_fills.values() if len(g) >= 3)
    total_gross = sum(
        sum(f["price"] * f["size"] for f in g)
        for g in multi_fills.values()
    )

    print("\n=== Multi-Fill Audit ===")
    print(f"Total BUY windows:  {total_windows}")
    if total_windows:
        print(f"  Single-fill:      {single:3d} ({100*single/total_windows:.0f}%)")
        print(f"  Double-fill:      {double:3d} ({100*double/total_windows:.0f}%)")
        print(f"  Triple+ fill:     {triple:3d} ({100*triple/total_windows:.0f}%)")
    print(f"Total gross spent:  ${total_gross:.2f}")
    if total_windows:
        print(f"Avg cost/window:    ${total_gross/total_windows:.2f}")

    # Compare against trade_bible
    if not trades:
        return
    oldest = datetime.fromtimestamp(
        min(t.get("timestamp", 0) for t in trades),
        tz=timezone.utc,
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*), COALESCE(sum(stake_usd), 0)::numeric
            FROM trade_bible
            WHERE is_live AND placed_at >= %s
            """,
            (oldest,),
        )
        tb_count, tb_stake = cur.fetchone()

    print(f"\ntrade_bible entries: {tb_count}  recorded_stake=${float(tb_stake):.2f}")
    gap = total_gross - float(tb_stake)
    print(f"UNRECORDED SPEND:    ${gap:.2f}")
    if gap > 10:
        print(f"  ⚠️  ${gap:.2f} of actual CLOB fills are NOT tracked by the engine.")
        print("     Root cause: FAK/FOK response parsing bug (fixed v11).")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hours", type=int, default=48, help="Backfill last N hours")
    p.add_argument("--since", help="ISO timestamp override (e.g. 2026-04-08T00:00:00)")
    p.add_argument("--dry-run", action="store_true", help="Report only, no DB writes")
    p.add_argument("--link", action="store_true", help="Link orphan fills to trade_bible")
    args = p.parse_args()

    if args.since:
        since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
    else:
        since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    since_ts = int(since.timestamp())

    print(f"Backfill poly_fills from Polymarket data-api")
    print(f"Since: {since.isoformat()}")
    print(f"User:  {POLY_FUNDER_ADDRESS}")
    print(f"Mode:  {'DRY RUN' if args.dry_run else 'LIVE WRITE'}")
    print()

    trades = fetch_trades(POLY_FUNDER_ADDRESS, since_ts)
    print(f"Fetched {len(trades)} trades from data-api.polymarket.com")
    if not trades:
        return 0

    multi_fills = detect_multi_fills(trades)

    conn = psycopg2.connect(_get_db_url())
    try:
        inserted, skipped = upsert_poly_fills(conn, trades, multi_fills, dry_run=args.dry_run)
        print(f"poly_fills:  {inserted} inserted  {skipped} skipped (already present)")

        if args.link and not args.dry_run:
            linked, unlinkable = link_orphans_to_trade_bible(conn, dry_run=False)
            print(f"linked {linked} orphan fills to trade_bible ({unlinkable} unlinkable)")

        report_discrepancies(conn, trades, multi_fills)
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
