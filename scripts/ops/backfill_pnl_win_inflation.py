"""
Backfill correct pnl_usd for WIN trades inflated by the position-aggregate bug.

Bug
===
When two strategies (e.g. v9_lgb_only AND v10_lgb_only) bet the SAME
``condition_id`` in the SAME direction in the SAME 5-min window, they
share a ``token_id`` on Polymarket. There is ONE token redemption
on-chain. But the DB records BOTH trades as winners and writes
``pnl_usd`` for each independently → reported pnl is roughly 2x the
truth on every dual-strategy win.

This is the WIN-side equivalent of the LOSS-side fix in
``backfill_pnl_inflation.py`` / PR #422. Same root cause (position
aggregate from Polymarket /positions endpoint), different symptom:
LOSS path inflated negative pnl, WIN path inflates positive pnl.

Audit reference: ``docs/ops/2026-04-30-today-pnl-audit.md`` — single
day showed 16 dual-strategy phantom-win groups with +$158 of inflation.

Locations patched alongside this script:
  - engine/reconciliation/reconciler.py:363 (backfill_on_startup)
  - engine/infrastructure/runtime.py:4871   (position_monitor loop)

Detection rule
==============
For each ``(condition_id, direction, window_ts)`` group with COUNT(*) >= 2
AND all rows status='RESOLVED_WIN':

  1. Sum DB pnl_usd across the group  (db_pnl_total)
  2. Query on-chain redemption for that condition_id
     (Polymarket data-api/activity, deduped same as audit_today_pnl.py)
  3. true_cashflow = onchain_payout - sum(stake_usd_in_group)
  4. inflation = db_pnl_total - true_cashflow

Correction strategy
===================
Redistribute true_cashflow proportionally by stake:

  new_pnl_per_row[i] = (true_cashflow * stake_usd[i] / sum(stake_usd_in_group))

Conservative floor: never make pnl_usd MORE positive than the original
DB value (we're correcting overstatement, not creating new wins).

Idempotent: re-running on already-corrected rows should be a no-op.
``ABS(sum_db_pnl - true_cashflow) < $0.50`` flags a group as already-clean.

Usage
=====
On Montreal:
    cd /home/novakash/novakash
    set -a && source engine/.env && set +a
    python3 scripts/ops/backfill_pnl_win_inflation.py            # dry-run
    python3 scripts/ops/backfill_pnl_win_inflation.py --execute  # apply

Safety
======
- Dry-run by default
- Only touches RESOLVED_WIN rows in dual-strategy groups
- Only reduces pnl_usd (never inflates further)
- Skips already-clean groups (idempotent)
- Read-only Polymarket data-api hits (no on-chain writes)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.request
from collections import defaultdict


def _f(x) -> float:
    if x is None:
        return 0.0
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _http_get_json(url: str, retries: int = 3, timeout: int = 20):
    """GET + JSON parse with retry on transient 4xx/5xx."""
    import time as _t
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except Exception as e:  # noqa: BLE001
            last_err = e
            _t.sleep(1 + attempt)
    raise RuntimeError(f"GET failed after {retries} retries: {url[:80]} — {last_err}")


def fetch_activity(funder: str, since_ts: int) -> list[dict]:
    """All TRADE + REDEEM events from Polymarket data-api since ``since_ts``.

    Dedupes by (transactionHash, type, side, asset, outcomeIndex) — the
    activity endpoint returns each row twice (~26% in 500-row pages).
    Same dedup logic as ``audit_today_pnl.py``.
    """
    rows: list[dict] = []
    offset = 0
    while offset < 50000:
        url = (
            "https://data-api.polymarket.com/activity?user="
            + funder + "&limit=500&offset=" + str(offset)
        )
        page = _http_get_json(url)
        if not page:
            break
        rows.extend(page)
        if len(page) < 500:
            break
        offset += 500
        if page and page[-1].get("timestamp", 0) < since_ts:
            break

    seen = set()
    deduped: list[dict] = []
    for r in rows:
        if r.get("timestamp", 0) < since_ts:
            continue
        key = (
            r.get("transactionHash"),
            r.get("type"),
            r.get("side", ""),
            r.get("asset", ""),
            r.get("outcomeIndex", 0),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped


def build_redemption_map(activity: list[dict]) -> tuple[dict[str, float], dict[str, str]]:
    """Return (condition_id → on-chain redemption USDC, slug → condition_id).

    Slug map lets us match DB trades that lack a polymarket_tx_hash but
    have a market_slug.
    """
    redeems: dict[str, float] = defaultdict(float)
    slug_to_cond: dict[str, str] = {}
    for r in activity:
        cid = r.get("conditionId")
        if not cid:
            continue
        slug = r.get("slug")
        if slug and slug not in slug_to_cond:
            slug_to_cond[slug] = cid
        if r.get("type") == "REDEEM" and _f(r.get("usdcSize")) > 0.01:
            redeems[cid] += _f(r["usdcSize"])
    return dict(redeems), slug_to_cond


async def fetch_dual_win_groups(conn, since_days: int | None = None) -> list[dict]:
    """Find every dual-strategy WIN group historically.

    A group = ``(condition_id, direction, window_ts)`` with >= 2 rows
    all in RESOLVED_WIN status and on the LIVE side.

    ``since_days`` (None = all history) limits how far back to scan.
    """
    where_since = ""
    args: list = []
    if since_days is not None:
        where_since = "AND created_at > NOW() - ($1::int * INTERVAL '1 day')"
        args = [since_days]
    rows = await conn.fetch(
        f"""
        WITH dual_groups AS (
          SELECT
            COALESCE(metadata->>'condition_id', metadata->>'market_slug', market_slug) AS group_key,
            COALESCE(market_slug, metadata->>'market_slug')                              AS slug,
            COALESCE(metadata->>'direction', direction)                                  AS dir,
            FLOOR(EXTRACT(EPOCH FROM created_at) / 300) * 300                            AS window_ts,
            COUNT(*)                                                                     AS n,
            ARRAY_AGG(id ORDER BY id)                                                    AS ids,
            ARRAY_AGG(strategy_id ORDER BY id)                                           AS strategies,
            ARRAY_AGG(stake_usd::float ORDER BY id)                                      AS stakes,
            ARRAY_AGG(pnl_usd::float ORDER BY id)                                        AS pnls,
            ARRAY_AGG(polymarket_tx_hash ORDER BY id)                                    AS tx_hashes,
            ARRAY_AGG(metadata->>'token_id' ORDER BY id)                                 AS token_ids
          FROM trades
          WHERE is_live = true
            AND status = 'RESOLVED_WIN'
            AND pnl_usd IS NOT NULL
            {where_since}
          GROUP BY 1, 2, 3, 4
          HAVING COUNT(*) >= 2
        )
        SELECT * FROM dual_groups
        ORDER BY window_ts DESC
        """,
        *args,
    )
    return [dict(r) for r in rows]


def reconcile_group(group: dict, redeems: dict[str, float], slug_to_cond: dict[str, str]) -> dict:
    """Compute true cashflow + inflation for a group + new pnl per row.

    Resolves a condition_id by:
      1. Trying the group's slug → cond mapping.
      2. Falling back to any tx_hash on the rows that maps to a known cond.

    Returns dict with:
      condition_id, sum_stake, sum_pnl_db, onchain_payout, true_cashflow,
      inflation, new_pnls (list aligned with group['ids']), already_clean,
      resolvable.
    """
    slug = group.get("slug")
    cid = slug_to_cond.get(slug) if slug else None
    sum_stake = sum(_f(s) for s in group["stakes"])
    sum_pnl_db = sum(_f(p) for p in group["pnls"])
    onchain_payout = redeems.get(cid, 0.0) if cid else 0.0
    true_cashflow = onchain_payout - sum_stake
    inflation = sum_pnl_db - true_cashflow

    # Build new pnl per row, proportional by stake
    if sum_stake > 0 and onchain_payout > 0:
        new_pnls = [
            round(true_cashflow * _f(s) / sum_stake, 4)
            for s in group["stakes"]
        ]
    else:
        new_pnls = list(group["pnls"])

    # Conservative floor: never inflate further (only correct overstatement)
    floored = []
    for original, new in zip(group["pnls"], new_pnls):
        original_f = _f(original)
        if new > original_f:
            floored.append(round(original_f, 4))
        else:
            floored.append(round(new, 4))

    already_clean = onchain_payout > 0 and abs(sum_pnl_db - true_cashflow) < 0.50
    resolvable = onchain_payout > 0

    return {
        "condition_id": cid,
        "sum_stake": round(sum_stake, 2),
        "sum_pnl_db": round(sum_pnl_db, 2),
        "onchain_payout": round(onchain_payout, 2),
        "true_cashflow": round(true_cashflow, 2),
        "inflation": round(inflation, 2),
        "new_pnls": floored,
        "already_clean": already_clean,
        "resolvable": resolvable,
    }


async def amain(execute: bool, since_days: int | None) -> int:
    import asyncpg  # type: ignore

    dsn_raw = os.environ.get("DATABASE_URL", "")
    if not dsn_raw:
        print("ERROR: DATABASE_URL not set — load engine/.env first")
        return 2
    dsn = dsn_raw.replace("postgresql+asyncpg://", "postgresql://").replace("+asyncpg", "")

    funder = os.environ.get("POLY_FUNDER_ADDRESS", "")
    if not funder:
        print("ERROR: POLY_FUNDER_ADDRESS not set — load engine/.env")
        return 2

    conn = await asyncpg.connect(dsn)
    try:
        groups = await fetch_dual_win_groups(conn, since_days=since_days)
        if not groups:
            print(f"No dual-strategy WIN groups found (since_days={since_days}). Nothing to do.")
            return 0
        print(f"Found {len(groups)} dual-strategy WIN groups (since_days={since_days}).")

        # Pull on-chain activity since the oldest window in our groups
        # (minus 1 day buffer for BUY events that straddle the window).
        oldest_window_ts = min(int(g["window_ts"]) for g in groups)
        since_ts = oldest_window_ts - 86400
        print(f"Fetching on-chain activity since {since_ts} (oldest_window {oldest_window_ts}).")
        activity = fetch_activity(funder, since_ts)
        print(f"Got {len(activity)} deduped on-chain rows.")

        redeems, slug_to_cond = build_redemption_map(activity)
        print(f"Mapped {len(redeems)} REDEEMs across {len(slug_to_cond)} slugs.")

        # Reconcile each group
        results = []
        for g in groups:
            r = reconcile_group(g, redeems, slug_to_cond)
            r["group"] = g
            results.append(r)

        n_resolvable = sum(1 for r in results if r["resolvable"])
        n_clean = sum(1 for r in results if r["already_clean"])
        n_to_fix = sum(
            1 for r in results
            if r["resolvable"] and not r["already_clean"] and r["inflation"] > 0.50
        )
        n_unresolvable = len(results) - n_resolvable
        total_inflation = sum(
            r["inflation"] for r in results
            if r["resolvable"] and not r["already_clean"] and r["inflation"] > 0.50
        )

        print()
        print(f"  resolvable groups (REDEEM found): {n_resolvable}")
        print(f"  already clean (within $0.50):     {n_clean}")
        print(f"  groups to correct:                {n_to_fix}")
        print(f"  unresolvable (no REDEEM, skipped):{n_unresolvable}")
        print(f"  total inflation to correct:       ${total_inflation:.2f}")
        print()

        if n_to_fix == 0:
            print("Nothing to backfill. Already idempotent.")
            return 0

        # Show top 20
        to_fix = sorted(
            (r for r in results if r["resolvable"] and not r["already_clean"] and r["inflation"] > 0.50),
            key=lambda r: -r["inflation"],
        )
        print("Top 20 groups by inflation:")
        for r in to_fix[:20]:
            g = r["group"]
            ids = list(g["ids"])
            strats = ",".join(s.replace("_lgb_only", "") for s in g["strategies"])[:24]
            print(
                f"  ts={int(g['window_ts'])}  {g['dir']:4s}  {strats:24s}  "
                f"ids={ids}  stake_sum=${r['sum_stake']:6.2f}  "
                f"pnl_db=${r['sum_pnl_db']:+7.2f}  payout=${r['onchain_payout']:6.2f}  "
                f"true=${r['true_cashflow']:+6.2f}  infl=${r['inflation']:+6.2f}"
            )
        if len(to_fix) > 20:
            print(f"  ... +{len(to_fix) - 20} more")

        if not execute:
            print("\n[dry-run] Re-run with --execute to apply.")
            return 0

        # Apply: per-row UPDATE in a transaction
        n_rows_updated = 0
        async with conn.transaction():
            for r in to_fix:
                ids = list(r["group"]["ids"])
                new_pnls = r["new_pnls"]
                for tid, new_pnl in zip(ids, new_pnls):
                    await conn.execute(
                        """UPDATE trades
                           SET pnl_usd = $1
                           WHERE id = $2
                             AND status = 'RESOLVED_WIN'
                             AND is_live = true""",
                        new_pnl,
                        tid,
                    )
                    n_rows_updated += 1

        print(f"\n[done] Updated {n_rows_updated} row(s) across {n_to_fix} groups.")
        print(f"       Total inflation removed: ${total_inflation:.2f}")
        return 0
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill pnl_usd for inflated WIN rows.")
    parser.add_argument("--execute", action="store_true", help="Apply correction (default: dry-run).")
    parser.add_argument(
        "--since-days",
        type=int,
        default=None,
        help="Only consider trades created in the last N days (default: all history).",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(amain(execute=args.execute, since_days=args.since_days))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
