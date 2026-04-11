#!/usr/bin/env python3
"""
POLY-SOT-c — One-shot historical backfill for SOT reconciliation.

The forward reconciler (POLY-SOT Phase 1 + POLY-SOT-b) only stamps rows
*written after* its respective merge. This script walks every historical
row that has `sot_reconciliation_state IS NULL` in either table, queries
Polymarket for the order's terminal status (when an order ID exists),
and stamps the row using the same comparison logic as the forward
reconciler.

Behaviour
---------
- Walks `manual_trades` and/or `trades` in ascending `id` order.
- For each row with NULL state:
    * If the row has a polymarket_order_id (or clob_order_id on the
      trades table), call `poly_client.get_order_status_sot(order_id)`
      and tag with the same `_compare_to_polymarket` decision matrix
      the forward reconciler uses.
    * If the row has NO order ID and is older than 24h, tag as
      `sot_reconciliation_state = 'no_order_id'` (a new terminal state
      added by POLY-SOT-c) with explanatory notes.
    * If the row has NO order ID and is YOUNGER than 24h, skip — the
      forward reconciler will catch it on its next pass once the
      orchestrator persists the order ID.
- Rate-limits Polymarket calls to 100ms between requests so we don't
  hammer the CLOB with a catch-up burst.
- Dry-run mode: prints what would be stamped but issues no UPDATEs.
- Idempotent: re-runs are no-ops because the WHERE clause filters on
  `sot_reconciliation_state IS NULL`.

Usage
-----
    # Always start with a dry run on the Montreal box (only Montreal can
    # call Polymarket):
    cd /home/novakash/novakash/engine
    python3 scripts/backfill_sot_reconciliation.py --table both --dry-run

    # Then for real:
    python3 scripts/backfill_sot_reconciliation.py --table both

Other flags:
    --table {manual_trades,trades,both}    target table(s) (default both)
    --older-than-hours HOURS               only process rows older than HOURS
    --limit N                              cap rows processed (0 = no cap)
    --batch-size N                         fetch this many rows per query (default 100)

Exit codes:
    0  — success
    1  — irrecoverable error (couldn't connect to DB / Polymarket / etc.)
    2  — partial success (some rows tagged, some skipped due to API errors)

Montreal rules
--------------
This script must run on the Montreal host because Polymarket geo-blocks
non-Montreal IPs. The DB connection points at the same Railway DB the
hub uses, so it's safe to run from any host that can reach the DB and
Polymarket.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Make engine modules importable when running this script directly with
# `python3 scripts/backfill_sot_reconciliation.py`. We add the engine
# root to sys.path so the bare imports below resolve.
_ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="POLY-SOT-c historical backfill for SOT reconciliation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--table",
        choices=("manual_trades", "trades", "both"),
        default="both",
        help="Target table(s) to backfill (default: both)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print decisions but issue no UPDATEs",
    )
    p.add_argument(
        "--older-than-hours",
        type=float,
        default=0.0,
        help="Only process rows older than this many hours (default 0 = all)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cap total rows processed across both tables (0 = no cap)",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Fetch this many rows per query (default 100)",
    )
    p.add_argument(
        "--rate-limit-ms",
        type=int,
        default=100,
        help="Sleep this many milliseconds between Polymarket calls (default 100)",
    )
    return p


def _no_order_id_age_threshold_hours() -> float:
    """Rows younger than this with no order ID are skipped (forward reconciler will pick them up)."""
    return 24.0


async def _backfill_manual_trades(
    pool,
    poly_client,
    *,
    dry_run: bool,
    older_than_hours: float,
    batch_size: int,
    limit: int,
    rate_limit_ms: int,
    reconciler,
) -> dict:
    """Walk manual_trades, tagging each NULL-state row.

    Returns a counts dict with keys: processed, agrees, unreconciled,
    engine_optimistic, polymarket_only, diverged, no_order_id, skipped, errors.
    """
    counts = {
        "processed": 0,
        "agrees": 0,
        "unreconciled": 0,
        "engine_optimistic": 0,
        "polymarket_only": 0,
        "diverged": 0,
        "no_order_id": 0,
        "skipped": 0,
        "errors": 0,
    }

    # Cursor-walk by ascending trade_id.
    last_id = ""
    total_estimate = 0
    try:
        async with pool.acquire() as conn:
            total_estimate = await conn.fetchval(
                "SELECT COUNT(*) FROM manual_trades WHERE sot_reconciliation_state IS NULL"
            )
    except Exception as exc:
        print(f"[BACKFILL][manual_trades] count_failed: {exc}", flush=True)
        return counts

    print(
        f"[BACKFILL] table=manual_trades NULL-state rows to inspect: {total_estimate}",
        flush=True,
    )

    while True:
        if limit and counts["processed"] >= limit:
            break

        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        trade_id,
                        polymarket_order_id,
                        status,
                        mode,
                        direction,
                        entry_price,
                        stake_usd,
                        created_at,
                        polymarket_confirmed_status,
                        polymarket_confirmed_fill_price,
                        polymarket_confirmed_size,
                        polymarket_confirmed_at,
                        polymarket_last_verified_at,
                        sot_reconciliation_state,
                        sot_reconciliation_notes
                    FROM manual_trades
                    WHERE sot_reconciliation_state IS NULL
                      AND ($1 = '' OR trade_id > $1)
                      AND ($2::numeric = 0 OR created_at < NOW() - make_interval(hours => $2))
                    ORDER BY trade_id ASC
                    LIMIT $3
                    """,
                    last_id,
                    older_than_hours,
                    batch_size,
                )
        except Exception as exc:
            print(f"[BACKFILL][manual_trades] fetch_failed: {exc}", flush=True)
            counts["errors"] += 1
            break

        if not rows:
            break

        for row in rows:
            if limit and counts["processed"] >= limit:
                break

            row_dict = dict(row)
            trade_id = row_dict["trade_id"]
            last_id = trade_id
            counts["processed"] += 1

            decision_state, decision = await _decide_for_row(
                row_dict, poly_client, reconciler, rate_limit_ms,
                table="manual_trades",
            )

            if decision_state == "skipped":
                counts["skipped"] += 1
                if not dry_run:
                    pass  # leave the row alone — forward reconciler will catch it
                continue
            if decision_state == "error":
                counts["errors"] += 1
                continue

            counts[decision_state] = counts.get(decision_state, 0) + 1

            print(
                f"[BACKFILL] table=manual_trades id={trade_id} state={decision_state} "
                f"(rows_processed={counts['processed']}/{total_estimate})",
                flush=True,
            )
            if dry_run:
                continue

            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE manual_trades
                        SET polymarket_confirmed_status = $1,
                            polymarket_confirmed_fill_price = $2,
                            polymarket_confirmed_size = $3,
                            polymarket_confirmed_at = $4,
                            polymarket_last_verified_at = NOW(),
                            sot_reconciliation_state = $5,
                            sot_reconciliation_notes = $6
                        WHERE trade_id = $7
                        """,
                        decision["confirmed_status"],
                        decision["confirmed_price"],
                        decision["confirmed_size"],
                        decision["confirmed_at"],
                        decision_state,
                        decision["notes"],
                        trade_id,
                    )
            except Exception as exc:
                print(
                    f"[BACKFILL][manual_trades] update_failed id={trade_id}: {exc}",
                    flush=True,
                )
                counts["errors"] += 1

        if len(rows) < batch_size:
            break

    return counts


async def _backfill_trades(
    pool,
    poly_client,
    *,
    dry_run: bool,
    older_than_hours: float,
    batch_size: int,
    limit: int,
    rate_limit_ms: int,
    reconciler,
) -> dict:
    """Walk the trades table, tagging each NULL-state row.

    Same shape as _backfill_manual_trades but reads from `trades` and
    keys by integer `id` instead of string `trade_id`.
    """
    counts = {
        "processed": 0,
        "agrees": 0,
        "unreconciled": 0,
        "engine_optimistic": 0,
        "polymarket_only": 0,
        "diverged": 0,
        "no_order_id": 0,
        "skipped": 0,
        "errors": 0,
    }

    last_id = 0
    total_estimate = 0
    try:
        async with pool.acquire() as conn:
            total_estimate = await conn.fetchval(
                "SELECT COUNT(*) FROM trades WHERE sot_reconciliation_state IS NULL"
            )
    except Exception as exc:
        print(f"[BACKFILL][trades] count_failed: {exc}", flush=True)
        return counts

    print(
        f"[BACKFILL] table=trades NULL-state rows to inspect: {total_estimate}",
        flush=True,
    )

    while True:
        if limit and counts["processed"] >= limit:
            break

        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        id AS trade_id,
                        order_id,
                        COALESCE(polymarket_order_id, clob_order_id) AS polymarket_order_id,
                        status,
                        mode,
                        direction,
                        entry_price,
                        stake_usd,
                        fill_price,
                        fill_size,
                        created_at,
                        is_live,
                        polymarket_confirmed_status,
                        polymarket_confirmed_fill_price,
                        polymarket_confirmed_size,
                        polymarket_confirmed_at,
                        polymarket_last_verified_at,
                        sot_reconciliation_state,
                        sot_reconciliation_notes
                    FROM trades
                    WHERE sot_reconciliation_state IS NULL
                      AND id > $1
                      AND ($2::numeric = 0 OR created_at < NOW() - make_interval(hours => $2))
                    ORDER BY id ASC
                    LIMIT $3
                    """,
                    last_id,
                    older_than_hours,
                    batch_size,
                )
        except Exception as exc:
            print(f"[BACKFILL][trades] fetch_failed: {exc}", flush=True)
            counts["errors"] += 1
            break

        if not rows:
            break

        for row in rows:
            if limit and counts["processed"] >= limit:
                break

            row_dict = dict(row)
            trade_id = row_dict["trade_id"]
            last_id = int(trade_id)
            counts["processed"] += 1

            decision_state, decision = await _decide_for_row(
                row_dict, poly_client, reconciler, rate_limit_ms,
                table="trades",
            )

            if decision_state == "skipped":
                counts["skipped"] += 1
                continue
            if decision_state == "error":
                counts["errors"] += 1
                continue

            counts[decision_state] = counts.get(decision_state, 0) + 1

            print(
                f"[BACKFILL] table=trades id={trade_id} state={decision_state} "
                f"(rows_processed={counts['processed']}/{total_estimate})",
                flush=True,
            )
            if dry_run:
                continue

            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE trades
                        SET polymarket_confirmed_status = $1,
                            polymarket_confirmed_fill_price = $2,
                            polymarket_confirmed_size = $3,
                            polymarket_confirmed_at = $4,
                            polymarket_last_verified_at = NOW(),
                            sot_reconciliation_state = $5,
                            sot_reconciliation_notes = $6
                        WHERE id = $7
                        """,
                        decision["confirmed_status"],
                        decision["confirmed_price"],
                        decision["confirmed_size"],
                        decision["confirmed_at"],
                        decision_state,
                        decision["notes"],
                        int(trade_id),
                    )
            except Exception as exc:
                print(
                    f"[BACKFILL][trades] update_failed id={trade_id}: {exc}",
                    flush=True,
                )
                counts["errors"] += 1

        if len(rows) < batch_size:
            break

    return counts


async def _decide_for_row(
    row: dict,
    poly_client,
    reconciler,
    rate_limit_ms: int,
    *,
    table: str,
) -> tuple[str, dict]:
    """Decide what state a single row should be tagged with.

    Returns ``(state, decision)`` where ``state`` is one of:
        agrees | unreconciled | engine_optimistic | polymarket_only |
        diverged | no_order_id | skipped | error
    and ``decision`` is the dict from ``_compare_to_polymarket`` (or a
    synthetic dict for the no_order_id / skipped paths).
    """
    poly_order_id = row.get("polymarket_order_id")
    created_at = row.get("created_at")
    age_hours: Optional[float] = None
    if isinstance(created_at, datetime):
        age_hours = (
            datetime.now(timezone.utc) - created_at
        ).total_seconds() / 3600.0

    # Path 1: no order ID at all.
    if not poly_order_id:
        if age_hours is not None and age_hours >= _no_order_id_age_threshold_hours():
            return (
                "no_order_id",
                {
                    "confirmed_status": None,
                    "confirmed_price": None,
                    "confirmed_size": None,
                    "confirmed_at": None,
                    "notes": (
                        "Backfilled — no order ID persisted at write time "
                        "(pre-POLY-SOT-b)"
                    ),
                    "should_alert": False,
                },
            )
        # Younger than 24h with no ID — let the forward reconciler catch it
        # on its next pass. Skip explicitly so we don't write a stale state.
        return (
            "skipped",
            {
                "confirmed_status": None,
                "confirmed_price": None,
                "confirmed_size": None,
                "confirmed_at": None,
                "notes": "skipped — no order ID, row younger than 24h",
                "should_alert": False,
            },
        )

    # Path 2: have an order ID — query Polymarket. Rate-limit so we
    # don't hammer the CLOB.
    if rate_limit_ms > 0:
        await asyncio.sleep(rate_limit_ms / 1000.0)
    try:
        order = await poly_client.get_order_status_sot(poly_order_id)
    except Exception as exc:
        return (
            "error",
            {
                "confirmed_status": None,
                "confirmed_price": None,
                "confirmed_size": None,
                "confirmed_at": None,
                "notes": f"polymarket fetch failed: {str(exc)[:120]}",
                "should_alert": False,
            },
        )

    decision = reconciler._compare_to_polymarket(row, order)
    return (decision["state"], decision)


async def _build_pool_and_poly_client():
    """Construct an asyncpg pool + a paper-mode polymarket client.

    The script needs both. Pulls DATABASE_URL from env (required) and
    constructs the polymarket client in paper mode by default — set
    LIVE_TRADING_ENABLED=true in the environment to verify against the
    real CLOB. The reconciler decision logic is identical in both
    modes; paper mode just uses synthetic order IDs.
    """
    import asyncpg

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError(
            "DATABASE_URL not set — backfill script needs the same DB the "
            "engine writes to. Run from the engine box or set DATABASE_URL "
            "explicitly."
        )

    # asyncpg doesn't accept the SQLAlchemy "+asyncpg" suffix.
    db_url = db_url.replace("postgresql+asyncpg://", "postgresql://", 1)

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=4)

    from execution.polymarket_client import PolymarketClient

    paper_mode = os.environ.get("LIVE_TRADING_ENABLED", "").strip().lower() != "true"
    poly_client = PolymarketClient(
        private_key=os.environ.get("POLY_PRIVATE_KEY", "0xdeadbeef"),
        api_key=os.environ.get("POLY_API_KEY", ""),
        api_secret=os.environ.get("POLY_API_SECRET", ""),
        api_passphrase=os.environ.get("POLY_API_PASSPHRASE", ""),
        funder_address=os.environ.get(
            "POLY_FUNDER_ADDRESS",
            "0x181D2ED714E0f7Fe9c6e4f13711376eDaab25E10",
        ),
        paper_mode=paper_mode,
    )
    if not paper_mode:
        # Live mode — connect the SDK so get_order_status_sot has a client.
        try:
            await poly_client.connect()
        except Exception as exc:
            print(
                f"[BACKFILL] WARNING — could not connect polymarket client: {exc}",
                flush=True,
            )
            print(
                "[BACKFILL] Continuing in best-effort mode; rows requiring "
                "Polymarket calls will be tagged unreconciled.",
                flush=True,
            )
    return pool, poly_client


def _build_reconciler_for_helper(poly_client):
    """Construct a CLOBReconciler with no DB pool, only used for the
    `_compare_to_polymarket` helper.

    The reconciler's full async machinery isn't needed — we just want
    its pure comparison helper to keep the decision matrix in lock-step
    with the forward reconciler.
    """
    from reconciliation.reconciler import CLOBReconciler

    class _NullAlerter:
        async def send_raw_message(self, _msg):
            return None

    return CLOBReconciler(
        poly_client=poly_client,
        db_pool=None,
        alerter=_NullAlerter(),
        shutdown_event=asyncio.Event(),
    )


async def _async_main(args) -> int:
    try:
        pool, poly_client = await _build_pool_and_poly_client()
    except Exception as exc:
        print(f"[BACKFILL] FATAL: {exc}", flush=True)
        return 1

    reconciler = _build_reconciler_for_helper(poly_client)

    print(
        f"[BACKFILL] starting table={args.table} dry_run={args.dry_run} "
        f"older_than_hours={args.older_than_hours} limit={args.limit} "
        f"batch_size={args.batch_size} rate_limit_ms={args.rate_limit_ms}",
        flush=True,
    )

    total_counts: dict = {}
    any_errors = False

    try:
        if args.table in ("manual_trades", "both"):
            mt_counts = await _backfill_manual_trades(
                pool,
                poly_client,
                dry_run=args.dry_run,
                older_than_hours=args.older_than_hours,
                batch_size=args.batch_size,
                limit=args.limit,
                rate_limit_ms=args.rate_limit_ms,
                reconciler=reconciler,
            )
            print(f"[BACKFILL] manual_trades counts: {mt_counts}", flush=True)
            total_counts["manual_trades"] = mt_counts
            if mt_counts.get("errors", 0) > 0:
                any_errors = True

        if args.table in ("trades", "both"):
            t_counts = await _backfill_trades(
                pool,
                poly_client,
                dry_run=args.dry_run,
                older_than_hours=args.older_than_hours,
                batch_size=args.batch_size,
                limit=args.limit,
                rate_limit_ms=args.rate_limit_ms,
                reconciler=reconciler,
            )
            print(f"[BACKFILL] trades counts: {t_counts}", flush=True)
            total_counts["trades"] = t_counts
            if t_counts.get("errors", 0) > 0:
                any_errors = True
    finally:
        try:
            await pool.close()
        except Exception:
            pass

    print(f"[BACKFILL] done — total: {total_counts}", flush=True)
    if any_errors:
        return 2
    return 0


def main() -> int:
    args = _build_arg_parser().parse_args()
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())
