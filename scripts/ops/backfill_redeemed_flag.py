"""Backfill ``trades.redeemed``/``redeemed_at``/``redemption_tx`` from on-chain truth.

Background — writer regression #7 (2026-05-01)
----------------------------------------------
The on-chain redemption path (primary on Montreal since 2026-04-16) silently
skipped the trades writeback. Result: ``trades.redeemed`` stayed false on
every WIN row despite real on-chain redemptions confirming. The forward
fix lives in ``engine/execution/redeemer.py::redeem_position_onchain``.

This script cleans up the historic gap. It pulls every REDEEM activity
event from ``data-api.polymarket.com/activity`` for the funder wallet
and matches each redeemed market to its WIN trade rows on Montreal RDS.

Match key — ``market_slug``
---------------------------
The natural join is ``market_slug``: every BTC up/down 5-min market has a
unique slug (``btc-updown-5m-<unix_ts>``), the activity API returns it as
``slug``, and ``trades.market_slug`` already stores it for every row. We
use ``market_slug`` instead of ``conditionId`` because ``trades.metadata``
does NOT carry ``condition_id`` (verified via ``jsonb_object_keys``); the
prior implementation joined on a key that never existed and matched zero
rows. ``conditionId`` is still captured in the log output for audit.

Why slug is safe (no over-match risk):
  * Each market resolves with exactly ONE winning side. ``outcome='WIN'``
    selects only that side, so even though a slug nominally has UP+DOWN
    tokens, only the winning side has ``outcome='WIN'`` rows.
  * Verified empirically (30d): 0 slugs have WIN rows on both directions.
  * Multi-row matches per slug are real (split fills) and all should
    flip together — the real REDEEM tx covers all of them at once.

The UPDATE this script runs:

    UPDATE trades
       SET redeemed      = true,
           redeemed_at   = <activity.timestamp>,
           redemption_tx = <activity.transactionHash>
     WHERE market_slug = <activity.slug>
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
    # type=REDEEM cuts the page payload by ~10x vs the full activity feed.
    while offset < 5000:
        url = (
            "https://data-api.polymarket.com/activity?user="
            + addr + "&type=REDEEM&limit=500&offset=" + str(offset)
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
    """Collapse multiple redeem events on the same market slug to one row.

    Keeps the LATEST timestamp (most recent redemption) and the tx_hash
    of that latest event. The DB UPDATE is idempotent per-slug, so
    re-running with multiple events for the same slug is harmless, but
    we want stable output. Rows missing a slug are dropped (can't match).
    """
    by_slug: dict[str, dict] = {}
    for r in redeems:
        slug = r.get("slug") or r.get("eventSlug")
        if not slug:
            continue
        existing = by_slug.get(slug)
        if existing is None or r["timestamp"] > existing["timestamp"]:
            by_slug[slug] = r
    return by_slug


async def _backfill(
    *,
    redeems_by_slug: dict[str, dict],
    execute: bool,
    verbose: bool,
) -> dict[str, int]:
    """Apply (or simulate) the UPDATE for each market slug.

    Returns a counters dict: scanned, matched, updated, skipped_already_set.
    """
    try:
        import asyncpg  # type: ignore
    except ImportError:
        sys.exit("asyncpg not installed — pip install asyncpg")

    counters = {
        "scanned": len(redeems_by_slug),
        "matched": 0,
        "updated": 0,
        "skipped_already_set": 0,
        "no_matching_trade": 0,
    }

    conn = await asyncpg.connect(_db_url())
    try:
        for slug, ev in redeems_by_slug.items():
            redeemed_at = datetime.fromtimestamp(
                ev["timestamp"], tz=timezone.utc
            )
            tx_hash = ev.get("transactionHash") or ev.get("transaction_hash")
            cid = ev.get("conditionId") or ""
            cid_tag = cid[:10] + ".." if cid else "<no-cid>"

            # Probe matching candidate rows (read-only).
            candidates = await conn.fetch(
                """
                SELECT id, redeemed, redeemed_at, redemption_tx, fill_size,
                       direction, metadata->>'token_id' AS token_id
                  FROM trades
                 WHERE market_slug = $1
                   AND outcome = 'WIN'
                   AND is_live = true
                   AND fill_size IS NOT NULL
                   AND fill_size > 0
                """,
                slug,
            )
            if not candidates:
                counters["no_matching_trade"] += 1
                if verbose:
                    print(
                        f"  [no-match] {slug} cid={cid_tag} "
                        f"(redeemed on-chain at {redeemed_at.isoformat()}, "
                        f"${float(ev.get('usdcSize', 0)):.2f})"
                    )
                continue

            counters["matched"] += 1
            already = sum(1 for c in candidates if c["redeemed"])
            todo = [c for c in candidates if not c["redeemed"]]

            if not todo:
                counters["skipped_already_set"] += already
                if verbose:
                    print(
                        f"  [already] {slug} {already} row(s) already "
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
                     WHERE market_slug = $3
                       AND outcome = 'WIN'
                       AND redeemed = false
                       AND is_live = true
                       AND fill_size IS NOT NULL
                       AND fill_size > 0
                    """,
                    redeemed_at,
                    tx_hash,
                    slug,
                )
                try:
                    n = int(result.split()[-1])
                except (ValueError, IndexError):
                    n = 0
                counters["updated"] += n
                print(
                    f"  [UPDATED] {slug} cid={cid_tag} rows={n} "
                    f"tx={tx_hash[:14] if tx_hash else 'null'}.. "
                    f"at={redeemed_at.isoformat()}"
                )
            else:
                # Dry-run — show what we would do.
                ids = ",".join(str(c["id"]) for c in todo)
                print(
                    f"  [DRY] {slug} cid={cid_tag} would UPDATE "
                    f"{len(todo)} row(s) id={ids} "
                    f"tx={tx_hash[:14] if tx_hash else 'null'}.. "
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
        "--slug", default=None,
        help="Backfill a single market_slug only (debug aid).",
    )
    parser.add_argument(
        "--condition-id", default=None,
        help="Backfill a single conditionId only (debug aid; "
             "filters the activity feed before slug match).",
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
    if args.slug:
        redeems = [
            r for r in redeems
            if (r.get("slug") == args.slug or r.get("eventSlug") == args.slug)
        ]
    redeems_by_slug = _consolidate(redeems)
    print(
        f"  {len(redeems)} REDEEM events  -> "
        f"{len(redeems_by_slug)} unique market_slugs"
    )

    if not redeems_by_slug:
        print("\nNothing to backfill.")
        return

    print("\nRunning UPDATEs (idempotent, only touches redeemed=false rows)...")
    counters = asyncio.run(
        _backfill(
            redeems_by_slug=redeems_by_slug,
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
