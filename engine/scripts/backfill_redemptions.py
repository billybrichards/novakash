#!/usr/bin/env python3
"""One-shot historical backfill for auto-redeem reconciliation (Hub #586).

Polymarket enabled on-chain auto-redeem in May 2026. Wins now settle to
USDC and disappear from data-api positions. The legacy reconciler can
only see LOSSES, so the trades table has a backlog of unstamped wins.

This script walks ``data-api activity?type=REDEEM`` for the funder,
matches each redemption to unresolved trade rows by ``market_slug``
filtered by the winning direction from ``window_snapshots.oracle_outcome``,
and stamps the trades as WIN with proper payout_usd / pnl_usd / redeemed
columns.

Behaviour
---------
- Walks the funder's REDEEM events from ``--since-days`` ago to now.
- For each event with an oracle_outcome AND unresolved trades, stamps
  the matching rows via the same ``ReconcileRedemptionsUseCase`` the
  live reconciler uses (zero behaviour drift).
- Idempotent: the use-case's WHERE guard
  (``outcome IS NULL AND redeemed = FALSE``) means re-running is a
  no-op.
- Dry-run mode: prints what would be stamped, issues no UPDATEs.

Usage
-----
    # ALWAYS dry-run first on the Montreal box:
    cd /home/novakash/novakash/engine
    python3 -m scripts.backfill_redemptions --since-days 3 --dry-run

    # Then for real:
    python3 -m scripts.backfill_redemptions --since-days 3
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

import asyncpg
import structlog


log = structlog.get_logger(__name__)


async def _connect_db() -> asyncpg.Pool:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("DB_URL")
    if not dsn:
        # Build from individual env vars
        host = os.environ.get("DB_HOST", "localhost")
        port = os.environ.get("DB_PORT", "5432")
        user = os.environ.get("DB_USER", "postgres")
        password = os.environ.get("DB_PASSWORD", "")
        name = os.environ.get("DB_NAME", "novakash")
        dsn = f"postgresql://{user}:{password}@{host}:{port}/{name}"
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    return pool


async def _list_recent_redemptions(
    funder: str,
    *,
    since_days: int,
) -> list[dict]:
    """Fetch all REDEEM events since N days ago — pure dict form so we
    can show a pre-flight summary before invoking the use case."""
    from adapters.polymarket.redeem_activity_feed import (
        PolymarketRedemptionFeed,
    )

    feed = PolymarketRedemptionFeed(funder)
    since_ts = int(
        datetime.now(timezone.utc).timestamp() - since_days * 86400
    )
    events = await feed.fetch_since(
        since_timestamp=since_ts, max_pages=10, page_size=500
    )
    return [
        {
            "tx": e.transaction_hash,
            "slug": e.market_slug,
            "usdc": e.usdc_size,
            "size": e.size,
            "ts": e.timestamp,
            "event": e,
        }
        for e in events
    ]


async def _summarise(
    pool: asyncpg.Pool, events: list[dict]
) -> dict[str, int]:
    """Pre-flight: count trades that would be stamped per event."""
    from adapters.persistence.pg_trade_repo import PgTradeRepository
    from adapters.persistence.pg_window_repo import PgWindowRepository

    trades_repo = PgTradeRepository(pool)
    window_repo = PgWindowRepository(pool)

    winning_map = {"UP": "YES", "DOWN": "NO"}
    summary = {
        "events_seen": len(events),
        "events_with_oracle": 0,
        "events_with_unresolved": 0,
        "total_unresolved_trades": 0,
    }
    for row in events:
        oracle = await window_repo.get_oracle_outcome_by_slug(row["slug"])
        if oracle not in ("UP", "DOWN"):
            continue
        summary["events_with_oracle"] += 1
        winning_dir = winning_map[oracle]
        trades = await trades_repo.find_unresolved_live_trades_for_slug(
            row["slug"], winning_direction=winning_dir
        )
        if trades:
            summary["events_with_unresolved"] += 1
            summary["total_unresolved_trades"] += len(trades)
            log.info(
                "backfill_redemptions.would_stamp",
                slug=row["slug"],
                winning_direction=winning_dir,
                n_trades=len(trades),
                trade_ids=[t["id"] for t in trades[:10]],
                tx=row["tx"][:20],
            )
    return summary


async def _execute_backfill(
    pool: asyncpg.Pool, events: list[dict]
) -> None:
    from adapters.persistence.pg_trade_repo import PgTradeRepository
    from adapters.persistence.pg_window_repo import PgWindowRepository
    from use_cases.reconcile_redemptions import ReconcileRedemptionsUseCase

    trades_repo = PgTradeRepository(pool)
    window_repo = PgWindowRepository(pool)
    uc = ReconcileRedemptionsUseCase(
        trade_repo=trades_repo, window_state=window_repo
    )

    event_objs = [row["event"] for row in events]
    result = await uc.execute(event_objs)
    print()
    print("=" * 60)
    print("BACKFILL RESULT")
    print("=" * 60)
    print(f"  events_seen:        {result.events_seen}")
    print(f"  trades_stamped:     {result.trades_stamped}")
    print(f"  skipped_no_oracle:  {result.events_skipped_no_oracle}")
    print(f"  skipped_no_match:   {result.events_skipped_no_match}")
    print(f"  errors:             {result.errors}")
    print(f"  total_payout_usd:   ${result.total_payout_usd:.2f}")
    print("=" * 60)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since-days",
        type=int,
        default=3,
        help="How many days of REDEEM history to scan (default 3).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be stamped, but do not write to DB.",
    )
    parser.add_argument(
        "--funder",
        type=str,
        default=os.environ.get(
            "POLY_FUNDER_ADDRESS",
            "0x181D2ED714E0f7Fe9c6e4f13711376eDaab25E10",
        ),
        help="Polymarket funder proxy address.",
    )
    args = parser.parse_args()

    print(f"Funder: {args.funder}")
    print(f"Since:  {args.since_days} days ago")
    print(f"Mode:   {'DRY RUN' if args.dry_run else 'LIVE (will UPDATE)'}")
    print()

    print("Fetching REDEEM events from data-api...")
    events = await _list_recent_redemptions(
        args.funder, since_days=args.since_days
    )
    print(f"  Found {len(events)} REDEEM events.")

    pool = await _connect_db()
    try:
        print()
        print("Pre-flight summary (no writes yet):")
        summary = await _summarise(pool, events)
        print()
        for k, v in summary.items():
            print(f"  {k}: {v}")

        if args.dry_run:
            print()
            print("Dry-run only. No DB updates performed.")
            return 0

        if summary["total_unresolved_trades"] == 0:
            print()
            print("Nothing to stamp. Exiting.")
            return 0

        print()
        print(
            f"Proceeding to stamp {summary['total_unresolved_trades']} "
            f"trade rows..."
        )
        await _execute_backfill(pool, events)
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
