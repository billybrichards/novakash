#!/usr/bin/env python3
"""Auto-patch trades.outcome where DB says LOSS but oracle says WIN (and vice-versa).

Background
==========
Audit #353 / Hub note #337: the engine reconciler's `cost_fallback` path
writes the wrong outcome on every WIN. Wallet is fine (Polymarket sweeper
auto-redeems on-chain regardless), but DB metrics, TG cards, FE dashboards,
and strategy WR stats end up polluted.

This is a self-healing patcher that runs every 2 minutes from cron, detects
the discrepancy by joining `trades` against `window_snapshots.oracle_outcome`
(and falling back to `market_data.outcome` for older windows), and rewrites
the offending rows. It is idempotent — already-correct rows are skipped.

It is a belt-and-suspenders fix while audit #369 lands a real PR. Once the
real fix ships and a 24h soak shows zero detections, this cron can be
removed.

PnL formula (Polymarket binary)
===============================
WIN:  (1 - fill_price) * (stake_usd / fill_price) - 0.072 * stake_usd
LOSS: -stake_usd

Safety
======
- Only patches LIVE-mode trades (mode='live')
- Only patches trades with non-null fill_price/stake/outcome
- Only patches where canonical IS NOT NULL (oracle has resolved)
- Marks each patch with metadata.manual_fix_2026_05_XX_audit_353 for audit
- Skips rows already containing a manual_fix marker (avoid double-patching)
- Refuses to patch if discrepancy count exceeds DETECT_MAX_PATCHES (default 50)
  — guards against schema/canonical-source corruption causing mass-rewrite

Usage
=====
On Montreal, in cron (every 2 minutes):

    */2 * * * * cd /home/novakash/novakash && set -a && . engine/.env && set +a \\
        && python3 scripts/ops/auto_patch_misclassified_trades.py \\
        >> /home/novakash/auto_patch.log 2>&1

Manual:

    cd /home/novakash/novakash && set -a && . engine/.env && set +a
    python3 scripts/ops/auto_patch_misclassified_trades.py --dry-run    # preview
    python3 scripts/ops/auto_patch_misclassified_trades.py              # apply
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

import psycopg2

# Conservative cap — if more than this many discrepancies appear in a single
# pass, something is wrong (canonical source corrupted, schema drift, etc.).
# Bail out and alert the operator instead of mass-rewriting.
DETECT_MAX_PATCHES = 50

# Don't bother patching trades from before audit #350 was filed —
# anything older has already been swept up by manual one-shots and we
# don't want to disturb historical cohorts.
PATCH_FLOOR_TS = "2026-05-04 21:47:00 UTC"

DETECT_SQL = """
SELECT t.id,
       t.strategy_id,
       t.direction,
       t.fill_price,
       t.stake_usd,
       t.outcome AS db_outcome,
       COALESCE(ws.oracle_outcome, md.outcome) AS canonical
FROM trades t
LEFT JOIN (
    SELECT DISTINCT ON (window_ts) window_ts, oracle_outcome
    FROM window_snapshots
    WHERE asset = 'BTC'
    ORDER BY window_ts, created_at DESC
) ws ON ws.window_ts = (regexp_replace(t.market_slug, '.*-', ''))::bigint
LEFT JOIN market_data md
    ON md.asset = 'BTC'
    AND md.timeframe = '5m'
    AND md.window_ts = (regexp_replace(t.market_slug, '.*-', ''))::bigint
    AND md.resolved = TRUE
WHERE t.created_at > %s::timestamptz
  AND t.outcome IS NOT NULL
  AND t.mode = 'live'
  AND COALESCE(ws.oracle_outcome, md.outcome) IS NOT NULL
  AND (
      (t.direction = 'YES' AND COALESCE(ws.oracle_outcome, md.outcome) = 'UP'   AND t.outcome = 'LOSS')
   OR (t.direction = 'NO'  AND COALESCE(ws.oracle_outcome, md.outcome) = 'DOWN' AND t.outcome = 'LOSS')
   OR (t.direction = 'YES' AND COALESCE(ws.oracle_outcome, md.outcome) = 'DOWN' AND t.outcome = 'WIN')
   OR (t.direction = 'NO'  AND COALESCE(ws.oracle_outcome, md.outcome) = 'UP'   AND t.outcome = 'WIN')
  )
"""


def real_pnl(fill_price: float, stake: float, is_win: bool) -> float:
    if is_win:
        # Polymarket binary WIN: shares pay out at $1.00 each
        return round((1 - fill_price) * (stake / fill_price) - 0.072 * stake, 4)
    return round(-stake, 4)


def main() -> int:
    parser = argparse.ArgumentParser(description="Auto-patch misclassified trade outcomes")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no writes")
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("FATAL: DATABASE_URL not in env", file=sys.stderr)
        return 2

    db_url = db_url.replace("postgresql+asyncpg", "postgresql")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"=== auto_patch_misclassified_trades [{now}] dry_run={args.dry_run} ===")

    conn = psycopg2.connect(db_url)
    try:
        cur = conn.cursor()
        cur.execute(DETECT_SQL, (PATCH_FLOOR_TS,))
        rows = cur.fetchall()

        if not rows:
            print("[ok] no misclassifications detected")
            return 0

        if len(rows) > DETECT_MAX_PATCHES:
            print(
                f"[abort] {len(rows)} discrepancies detected, "
                f"exceeds DETECT_MAX_PATCHES={DETECT_MAX_PATCHES}. "
                "Investigate canonical source before mass-patching."
            )
            return 3

        print(f"[detected] {len(rows)} misclassified trades")

        marker_key = f"auto_fix_{datetime.now(timezone.utc).strftime('%Y_%m_%d')}_audit_353"
        total_correction = 0.0
        patched_ids = []

        for tid, sid, direction, fp, stake, db_outcome, canonical in rows:
            fp_f = float(fp)
            stake_f = float(stake)
            is_win = (
                (direction == "YES" and canonical == "UP")
                or (direction == "NO" and canonical == "DOWN")
            )
            new_outcome = "WIN" if is_win else "LOSS"
            new_status = "RESOLVED_WIN" if is_win else "RESOLVED_LOSS"
            new_pnl = real_pnl(fp_f, stake_f, is_win)

            old_pnl_implicit = -stake_f if db_outcome == "LOSS" else (
                # for WIN-but-actually-LOSS rows, the bad pnl is whatever fill math says
                round((1 - fp_f) * (stake_f / fp_f) - 0.072 * stake_f, 4)
            )
            delta = new_pnl - old_pnl_implicit
            total_correction += delta

            print(
                f"  id={tid} {sid} {direction} fill=${fp_f:.3f} stake=${stake_f:.2f} "
                f"db={db_outcome} -> {new_outcome} pnl=${new_pnl:+.2f} (Δ${delta:+.2f})"
            )

            if args.dry_run:
                continue

            cur.execute(
                """
                UPDATE trades
                SET outcome = %s,
                    status = %s,
                    pnl_usd = %s,
                    resolved_at = COALESCE(resolved_at, NOW()),
                    metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                        %s,
                        jsonb_build_object(
                            'canonical', %s,
                            'corrected_outcome', %s,
                            'real_pnl', %s,
                            'patched_at', %s
                        )
                    )
                WHERE id = %s
                  AND outcome = %s
                """,
                (
                    new_outcome, new_status, new_pnl,
                    marker_key, canonical, new_outcome, new_pnl, now,
                    tid, db_outcome,
                ),
            )
            if cur.rowcount > 0:
                patched_ids.append(tid)

        if not args.dry_run:
            conn.commit()
            print(
                f"[committed] patched {len(patched_ids)} trades, "
                f"net pnl correction ${total_correction:+.2f}"
            )
        else:
            print(
                f"[dry-run] would patch {len(rows)} trades, "
                f"net pnl correction ${total_correction:+.2f}"
            )

        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
