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

Gamma fallback (2026-05-19, audit #XXX follow-up)
-------------------------------------------------
``populate_oracle_outcomes`` runs every 2 min in the reconciler and only
polls windows aged 6-15 min (``min_age_seconds=360``, ``lookback_seconds=900``).
``canonical_resolver`` (HTML scrape path) can resolve a trade *before* the
oracle writer fires, leaving a 2-6 min window where:

  * trade.outcome is set (sometimes via cost_fallback → wrong)
  * ws.oracle_outcome IS NULL
  * md.outcome IS NULL

The original detector predicate (``COALESCE(ws.oracle_outcome, md.outcome)
IS NOT NULL``) blinds itself in that gap. Today (2026-05-19) trades 8584
and 8605 were stranded LOSS in DB and needed a manual SQL UPDATE.

The Gamma fallback closes this gap: a second pass over trades with NULL
canonical asks Polymarket Gamma directly (same query the engine writer
uses) and patches when Gamma confirms a winner. Tagged with marker
``gamma_fallback_<date>_audit_369`` for audit-trail separation from the
main path.

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
import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

import psycopg2

try:
    import httpx  # type: ignore
except ImportError:
    httpx = None  # type: ignore  # Gamma fallback disabled if missing

# Conservative cap — if more than this many discrepancies appear in a single
# pass, something is wrong (canonical source corrupted, schema drift, etc.).
# Bail out and alert the operator instead of mass-rewriting.
DETECT_MAX_PATCHES = 50

# Don't bother patching trades from before audit #350 was filed —
# anything older has already been swept up by manual one-shots and we
# don't want to disturb historical cohorts.
PATCH_FLOOR_TS = "2026-05-04 21:47:00 UTC"

# Gamma fallback config. Only look at trades whose window has actually
# closed (window_ts + 300s in the past) — too-fresh queries return "not
# closed" and waste the request. Cap concurrency so we don't hammer Gamma
# from cron.
GAMMA_BASE = "https://gamma-api.polymarket.com"
SLUG_PREFIX = "btc-updown-5m-"
GAMMA_MAX_LOOKUPS_PER_RUN = 20
GAMMA_TIMEOUT_SECS = 10.0
# Only run Gamma fallback on trades >= this old (cost_fallback already had
# a chance to run, but oracle writer may have missed). 2 min is enough that
# the canonical_resolver path has definitely tried.
GAMMA_MIN_TRADE_AGE_SECS = 120

# NOTE on the LATERAL sub-query (audit #369 follow-up, 2026-05-19):
# window_snapshots has ~150-300 rows per window (one per evaluation tick)
# and ``oracle_outcome`` is denormalised across them. The bulk writer
# (engine/adapters/persistence/pg_window_repo.py:1942) only UPDATEs rows
# that already exist when the poll fires; late-arriving ticks keep the
# NULL until the next poll cycle.
#
# The ORIGINAL sub-query used ``DISTINCT ON (window_ts) ORDER BY
# created_at DESC`` which picked the latest tick — OFTEN NULL even
# though oracle had been written for the window, blinding auto-patch
# on fresh trades. Verified today (2026-05-19) on trade 8606:
# 109/312 rows for window 1779201900 had oracle_outcome=DOWN, 203 had
# NULL; the latest-by-created_at row was NULL → primary DETECT skipped
# the trade despite a clear misclassification.
#
# Fix: LATERAL join with ``LIMIT 1`` on ``oracle_outcome IS NOT NULL``.
# Uses ``idx_ws_ts`` (window_ts btree) for a bounded index scan per
# trade — sub-second on RDS prod. Picks ANY non-NULL value for the
# window (UP/DOWN are mutually exclusive per market — only one will
# ever be present), or NULL if no row has been written yet.
DETECT_SQL = """
SELECT t.id,
       t.strategy_id,
       t.direction,
       t.fill_price,
       t.stake_usd,
       t.outcome AS db_outcome,
       COALESCE(ws.oracle_outcome, md.outcome) AS canonical
FROM trades t
LEFT JOIN LATERAL (
    -- Per-trade lookup: ANY row for the trade's window with non-NULL
    -- oracle_outcome wins. idx_ws_ts covers ``window_ts``, so this is a
    -- bounded index scan per trade (typically 1-5 rows scanned before
    -- a non-NULL is found). Avoids the full-table aggregate that
    -- choked on RDS prod.
    SELECT oracle_outcome FROM window_snapshots
    WHERE asset = 'BTC'
      AND window_ts = (regexp_replace(t.market_slug, '.*-', ''))::bigint
      AND oracle_outcome IS NOT NULL
    LIMIT 1
) ws ON TRUE
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


# Gamma-fallback candidate detector — finds LIVE trades whose window has
# closed but neither window_snapshots.oracle_outcome nor market_data.outcome
# is populated. These are exactly the rows the primary DETECT_SQL is
# blind to (audit #369). We bound by GAMMA_MAX_LOOKUPS_PER_RUN to avoid
# hammering Gamma.
# Pre-filter trades to the last 60 min where the writer might not have
# caught up yet, then use indexed NOT EXISTS probes on window_snapshots
# / market_data. Bounded scan; runs in single-digit seconds on RDS prod.
DETECT_GAMMA_CANDIDATES_SQL = """
WITH recent_trades AS (
    SELECT id, strategy_id, direction, fill_price, stake_usd, outcome,
           (regexp_replace(market_slug, '.*-', ''))::bigint AS window_ts
    FROM trades
    WHERE created_at > %s::timestamptz
      AND created_at > NOW() - INTERVAL '60 minutes'
      AND outcome IS NOT NULL
      AND mode = 'live'
      AND fill_price IS NOT NULL
      AND stake_usd IS NOT NULL
      AND market_slug LIKE 'btc-updown-5m-%%'
)
SELECT rt.id,
       rt.strategy_id,
       rt.direction,
       rt.fill_price,
       rt.stake_usd,
       rt.outcome AS db_outcome,
       rt.window_ts
FROM recent_trades rt
WHERE rt.window_ts < EXTRACT(EPOCH FROM NOW())::bigint - %s
  AND NOT EXISTS (
      SELECT 1 FROM window_snapshots ws
      WHERE ws.asset = 'BTC'
        AND ws.window_ts = rt.window_ts
        AND ws.oracle_outcome IS NOT NULL
  )
  AND NOT EXISTS (
      SELECT 1 FROM market_data md
      WHERE md.asset = 'BTC'
        AND md.timeframe = '5m'
        AND md.window_ts = rt.window_ts
        AND md.resolved = TRUE
        AND md.outcome IS NOT NULL
  )
ORDER BY rt.window_ts DESC
LIMIT %s
"""


def gamma_outcome_for_window(client, window_ts: int) -> Optional[str]:
    """Return 'UP' / 'DOWN' / None for a 5m BTC window, via Polymarket Gamma.

    Same parsing logic as ``engine.adapters.persistence.pg_window_repo.populate_oracle_outcomes``
    — keep these aligned. Read-only (no auth required).
    """
    slug = f"{SLUG_PREFIX}{window_ts}"
    try:
        r = client.get(
            f"{GAMMA_BASE}/events",
            params={"slug": slug},
            timeout=GAMMA_TIMEOUT_SECS,
        )
        if r.status_code != 200:
            return None
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
                outcomes_raw = m.get("outcomes") or "[]"
                prices_raw = m.get("outcomePrices") or "[]"
                try:
                    outcomes = (
                        json.loads(outcomes_raw)
                        if isinstance(outcomes_raw, str)
                        else outcomes_raw
                    )
                    prices = (
                        json.loads(prices_raw)
                        if isinstance(prices_raw, str)
                        else prices_raw
                    )
                except Exception:
                    return None
                if not (
                    isinstance(outcomes, list)
                    and isinstance(prices, list)
                    and len(outcomes) == len(prices)
                ):
                    return None
                for name, price in zip(outcomes, prices):
                    try:
                        if float(price) >= 0.999:
                            return (
                                "UP"
                                if str(name).strip().lower().startswith("u")
                                else "DOWN"
                            )
                    except (TypeError, ValueError):
                        continue
        return None
    except Exception:
        return None


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

        if len(rows) > DETECT_MAX_PATCHES:
            print(
                f"[abort] {len(rows)} discrepancies detected, "
                f"exceeds DETECT_MAX_PATCHES={DETECT_MAX_PATCHES}. "
                "Investigate canonical source before mass-patching."
            )
            return 3

        if rows:
            print(f"[detected] {len(rows)} misclassified trades")
        else:
            print("[ok] no misclassifications detected via DB canonical")

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

        if rows:
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

        # ── Gamma fallback (audit #369) ──
        # The primary detector only matches trades where DB has canonical
        # truth (ws.oracle_outcome OR md.outcome). For trades resolved
        # before the bulk oracle writer ran (typically the 2-6 min window
        # post-window-close), both columns are NULL and the primary pass
        # is blind. Ask Polymarket Gamma directly here.
        if httpx is None:
            print("[gamma-fallback] httpx not installed — skipping")
            return 0

        cur.execute(
            DETECT_GAMMA_CANDIDATES_SQL,
            (PATCH_FLOOR_TS, GAMMA_MIN_TRADE_AGE_SECS, GAMMA_MAX_LOOKUPS_PER_RUN),
        )
        gamma_candidates = cur.fetchall()
        if not gamma_candidates:
            return 0

        print(
            f"[gamma-fallback] {len(gamma_candidates)} trade(s) with NULL "
            "ws.oracle_outcome AND md.outcome — querying Gamma"
        )

        gamma_marker_key = (
            f"gamma_fallback_{datetime.now(timezone.utc).strftime('%Y_%m_%d')}"
            "_audit_369"
        )
        gamma_total_correction = 0.0
        gamma_patched_ids = []
        gamma_resolved = 0
        gamma_unresolved = 0
        gamma_already_correct = 0

        with httpx.Client(
            headers={"User-Agent": "novakash-auto-patch/gamma-fallback"},
        ) as client:
            for tid, sid, direction, fp, stake, db_outcome, window_ts in gamma_candidates:
                gamma_out = gamma_outcome_for_window(client, int(window_ts))
                if gamma_out not in ("UP", "DOWN"):
                    gamma_unresolved += 1
                    continue
                gamma_resolved += 1

                fp_f = float(fp)
                stake_f = float(stake)
                is_win = (
                    (direction == "YES" and gamma_out == "UP")
                    or (direction == "NO" and gamma_out == "DOWN")
                )
                new_outcome = "WIN" if is_win else "LOSS"
                new_status = "RESOLVED_WIN" if is_win else "RESOLVED_LOSS"
                new_pnl = real_pnl(fp_f, stake_f, is_win)

                if db_outcome == new_outcome:
                    gamma_already_correct += 1
                    continue

                old_pnl_implicit = -stake_f if db_outcome == "LOSS" else (
                    round((1 - fp_f) * (stake_f / fp_f) - 0.072 * stake_f, 4)
                )
                delta = new_pnl - old_pnl_implicit
                gamma_total_correction += delta

                print(
                    f"  [gamma] id={tid} {sid} {direction} fill=${fp_f:.3f} "
                    f"stake=${stake_f:.2f} db={db_outcome} -> {new_outcome} "
                    f"pnl=${new_pnl:+.2f} (Δ${delta:+.2f}) "
                    f"window_ts={window_ts} gamma={gamma_out}"
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
                                'source', 'gamma_direct',
                                'canonical', %s,
                                'corrected_outcome', %s,
                                'real_pnl', %s,
                                'window_ts', %s,
                                'patched_at', %s
                            )
                        )
                    WHERE id = %s
                      AND outcome = %s
                    """,
                    (
                        new_outcome, new_status, new_pnl,
                        gamma_marker_key, gamma_out, new_outcome, new_pnl,
                        int(window_ts), now,
                        tid, db_outcome,
                    ),
                )
                if cur.rowcount > 0:
                    gamma_patched_ids.append(tid)

        if gamma_patched_ids and not args.dry_run:
            conn.commit()

        print(
            f"[gamma-fallback] resolved={gamma_resolved} "
            f"unresolved={gamma_unresolved} "
            f"already_correct={gamma_already_correct} "
            f"patched={len(gamma_patched_ids)} "
            f"net pnl correction ${gamma_total_correction:+.2f}"
            f"{' (dry-run)' if args.dry_run else ''}"
        )

        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
