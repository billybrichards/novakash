#!/usr/bin/env python3
"""v15m_up_basic LIVE kill-criteria checker.

Designed to run from cron every 15 minutes. Connects to RDS, evaluates the
4 kill criteria, and if ANY trip, automatically deletes the runtime override
row (reverting the strategy to GHOST) and posts an alert.

Kill criteria (set 2026-05-01 by Billy + agent, audit task #343):
1. Rolling 50-trade WR < 65%
2. Cumulative PnL since LIVE promotion < -$25
3. 3 consecutive losses (most recent)
4. Any single-trade loss > $5

Usage:
    RDS_PWD=...  python3 scripts/v15m_up_basic_kill_check.py [--dry-run]

Cron suggestion (Montreal box):
    */15 * * * * /usr/bin/python3 /home/novakash/novakash/scripts/v15m_up_basic_kill_check.py >> /home/novakash/v15m_kill_check.log 2>&1

Exit codes:
    0 = OK (or dry-run reported what it would do)
    1 = kill criterion tripped, override deleted
    2 = error (DB connection, query failure, etc.)
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

# stdlib psycopg2 fallback (sync) — keeps dependencies minimal for cron use.
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("ERROR: psycopg2 not installed. `pip install psycopg2-binary`")
    sys.exit(2)


STRATEGY_ID = "v15m_up_basic"
LIVE_PROMOTE_TS = "2026-05-01 18:41:32+00"

# Kill thresholds
ROLLING_WR_WINDOW = 50
ROLLING_WR_FLOOR_PCT = 65.0
CUMULATIVE_PNL_FLOOR = -25.0
CONSECUTIVE_LOSS_LIMIT = 3
SINGLE_TRADE_LOSS_FLOOR = -5.0


def _conn():
    """Open RDS connection from env vars."""
    return psycopg2.connect(
        host=os.environ.get(
            "RDS_HOST",
            "novakash-pg-prod.cpmisy2asv71.ca-central-1.rds.amazonaws.com",
        ),
        port=int(os.environ.get("RDS_PORT", 5432)),
        user=os.environ.get("RDS_USER", "postgres"),
        password=os.environ["RDS_PWD"],
        dbname=os.environ.get("RDS_DB", "novakash"),
        connect_timeout=10,
    )


def evaluate_kill_criteria(conn) -> tuple[str, dict]:
    """Returns ('OK' or 'KILL: <reason>', stats_dict)."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Pull all resolved trades since LIVE promotion, ordered by recency.
    cur.execute(
        """
        SELECT id, created_at, outcome, pnl_usd
        FROM trades
        WHERE strategy_id = %s
          AND created_at > %s
          AND outcome IN ('WIN', 'LOSS')
        ORDER BY created_at DESC
        """,
        (STRATEGY_ID, LIVE_PROMOTE_TS),
    )
    rows = cur.fetchall()
    cur.close()

    n_total = len(rows)
    cumulative_pnl = sum(float(r["pnl_usd"] or 0) for r in rows)
    worst = min((float(r["pnl_usd"] or 0) for r in rows), default=0.0)

    last_50 = rows[: ROLLING_WR_WINDOW]
    last50_wins = sum(1 for r in last_50 if r["outcome"] == "WIN")
    last50_losses = sum(1 for r in last_50 if r["outcome"] == "LOSS")
    last50_n = last50_wins + last50_losses
    last50_wr = (100.0 * last50_wins / last50_n) if last50_n else None

    # Most-recent consecutive losses
    consec_losses = 0
    for r in rows:
        if r["outcome"] == "LOSS":
            consec_losses += 1
        else:
            break

    stats = {
        "total_resolved": n_total,
        "last50_wins": last50_wins,
        "last50_losses": last50_losses,
        "last50_wr_pct": last50_wr,
        "cumulative_pnl_usd": round(cumulative_pnl, 2),
        "worst_trade_pnl_usd": round(worst, 2),
        "current_consec_losses": consec_losses,
    }

    # Evaluate (order matters — most severe first)
    if worst < SINGLE_TRADE_LOSS_FLOOR:
        return f"KILL: single-trade loss ${worst:.2f} < ${SINGLE_TRADE_LOSS_FLOOR}", stats
    if consec_losses >= CONSECUTIVE_LOSS_LIMIT:
        return f"KILL: {consec_losses} consecutive losses", stats
    if cumulative_pnl < CUMULATIVE_PNL_FLOOR:
        return f"KILL: cumulative PnL ${cumulative_pnl:.2f} < ${CUMULATIVE_PNL_FLOOR}", stats
    if last50_n >= ROLLING_WR_WINDOW and last50_wr < ROLLING_WR_FLOOR_PCT:
        return f"KILL: rolling 50-trade WR {last50_wr:.1f}% < {ROLLING_WR_FLOOR_PCT}%", stats

    return "OK", stats


def revert_override(conn, reason: str) -> bool:
    """DELETE the override row to flip strategy back to GHOST."""
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM strategy_runtime_overrides WHERE strategy_id = %s RETURNING strategy_id",
        (STRATEGY_ID,),
    )
    deleted = cur.fetchone()
    conn.commit()
    cur.close()

    if deleted:
        print(
            f"[{datetime.now(timezone.utc).isoformat()}] REVERTED: {STRATEGY_ID} override row deleted. "
            f"Reason: {reason}"
        )
        return True
    print(
        f"[{datetime.now(timezone.utc).isoformat()}] WARN: tried to revert {STRATEGY_ID} but no override row found."
    )
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Evaluate but do not revert")
    args = ap.parse_args()

    if not os.environ.get("RDS_PWD"):
        print("ERROR: RDS_PWD env var not set", file=sys.stderr)
        sys.exit(2)

    try:
        conn = _conn()
    except Exception as exc:
        print(f"ERROR: RDS connect failed: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        verdict, stats = evaluate_kill_criteria(conn)
    except Exception as exc:
        print(f"ERROR: kill-criteria query failed: {exc}", file=sys.stderr)
        conn.close()
        sys.exit(2)

    timestamp = datetime.now(timezone.utc).isoformat()
    print(
        f"[{timestamp}] {STRATEGY_ID} verdict={verdict} stats={stats}"
    )

    if verdict.startswith("KILL"):
        if args.dry_run:
            print(f"[{timestamp}] DRY-RUN: would revert override")
            conn.close()
            sys.exit(1)
        revert_override(conn, verdict)
        conn.close()
        sys.exit(1)

    conn.close()
    sys.exit(0)


if __name__ == "__main__":
    main()
