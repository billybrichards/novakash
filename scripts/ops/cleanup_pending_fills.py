"""Defensive cleanup of stale 'pending' rows in strategy_window_fills.

Background — audit #401 (2026-04-28):

The pessimistic fill-slot guard in PgWindowRepository.try_claim_fill_slot
writes a placeholder row (order_id='pending') BEFORE firing the FAK
ladder. On normal completion execute_trade.execute removes it via
release_fill_slot OR mark_traded UPSERTs the placeholder to a real
order_id. Multiple defence-in-depth backstops cover failure modes:

  1. has_filled() ignores 'pending' rows older than
     STALE_PLACEHOLDER_TTL_SECONDS (60s) — so a leak self-recovers
     on the next eval tick.
  2. try_claim_fill_slot's ON CONFLICT DO UPDATE STEALS stale
     placeholders.
  3. execute_trade's try/finally backstop releases on
     CancelledError / BaseException paths.

This script is the LAST line of defence: a periodic sweep that
removes 'pending' rows older than 5 minutes regardless. Run from
cron (or systemd timer) on the engine host every 5–15 minutes.

Why both defences? Because today (2026-04-28) we observed 52 stale
'pending' rows blocking v9_lgb_only and v10_lgb_only entries that
required a manual DELETE to recover. The in-band defences above
should make this script a no-op in normal operation; if it
consistently deletes rows, that signals a regression in the in-band
defences worth investigating.

Usage on Montreal:
    python3 scripts/ops/cleanup_pending_fills.py            # dry-run
    python3 scripts/ops/cleanup_pending_fills.py --apply    # commit

Schedule (systemd timer or crontab):
    */10 * * * *  cd /opt/btc-trader && python3 \\
        scripts/ops/cleanup_pending_fills.py --apply --quiet
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Optional

# Stale threshold. Generous vs the 60s in-band TTL so this script ONLY
# removes truly abandoned rows — never races an active FAK ladder.
STALE_THRESHOLD_MINUTES = 5


def _build_dsn() -> str:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_DSN")
    if dsn:
        # asyncpg requires the bare scheme; strip SQLAlchemy's
        # `+asyncpg` if a hub-style URL was reused.
        return dsn.replace("postgresql+asyncpg://", "postgresql://")
    user = os.environ.get("DB_USER", "btctrader")
    pwd = os.environ.get("DB_PASSWORD", "")
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "5432")
    name = os.environ.get("DB_NAME", "btc_trader")
    return f"postgresql://{user}:{pwd}@{host}:{port}/{name}"


async def _run(apply: bool, quiet: bool, threshold_minutes: int) -> int:
    try:
        import asyncpg  # type: ignore
    except ImportError:
        print(
            "asyncpg not installed — pip install asyncpg",
            file=sys.stderr,
        )
        return 2

    dsn = _build_dsn()
    conn: Optional[asyncpg.Connection] = None
    try:
        conn = await asyncpg.connect(dsn)
        # Count first — keeps the dry-run cheap and surfaces a metric
        # whether or not we apply.
        count = await conn.fetchval(
            """SELECT COUNT(*) FROM strategy_window_fills
                WHERE order_id = 'pending'
                  AND filled_at < NOW() - ($1 || ' minutes')::interval""",
            str(int(threshold_minutes)),
        )
        if not quiet:
            print(
                f"stale_pending_rows={count} "
                f"threshold_minutes={threshold_minutes} apply={apply}"
            )
        if not apply or not count:
            return 0
        tag = await conn.execute(
            """DELETE FROM strategy_window_fills
                WHERE order_id = 'pending'
                  AND filled_at < NOW() - ($1 || ' minutes')::interval""",
            str(int(threshold_minutes)),
        )
        if not quiet:
            print(f"deleted_tag={tag}")
        return 0
    finally:
        if conn is not None:
            await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete (default: dry-run, count only)",
    )
    ap.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress stdout; useful from cron",
    )
    ap.add_argument(
        "--threshold-minutes",
        type=int,
        default=STALE_THRESHOLD_MINUTES,
        help=(
            f"Rows older than this are deleted (default: "
            f"{STALE_THRESHOLD_MINUTES})"
        ),
    )
    args = ap.parse_args()
    return asyncio.run(
        _run(
            apply=args.apply,
            quiet=args.quiet,
            threshold_minutes=args.threshold_minutes,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
