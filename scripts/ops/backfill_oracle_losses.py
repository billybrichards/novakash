"""Backfill outcome='LOSS' on unresolved live trades whose window resolved
against them — companion to scripts/ops/backfill_oracle_outcomes.py.

Why this exists
---------------
Audit RDS #614 (2026-05-23) found unresolved LIVE trades sitting
``outcome=NULL, status=OPEN`` for many hours despite their windows being
long-resolved on Polymarket. Root cause:

  * Worthless tokens (curPrice=0) never produce on-chain redemption
    activity → the auto-redeem reconciler (PR #575) cannot stamp them.
  * The legacy CLOB reconciler only stamps trades visible in the
    data-api ``positions`` endpoint → trades whose CLOB POST silently
    dropped the order_id (FAK timeouts, parsing failures) are invisible.

The engine fix is ``use_cases/reconcile_oracle_losses.py`` — a new pass
that scans unresolved live trades and stamps LOSS when oracle disagrees.
This script runs the same logic once against the existing backlog,
useful immediately post-deploy to clear stale rows.

Usage
-----
    ssh novakash@<montreal> \\
      'python3 /home/novakash/novakash/scripts/ops/backfill_oracle_losses.py \\
            --min-age-min 30'

Flags:
  --min-age-min N   Only stamp trades older than N minutes (default 30).
  --dry-run         Show what would be stamped but do not UPDATE.
  --limit N         Process at most N trades (smoke test).

Idempotent: WHERE guard ensures already-stamped trades are no-ops.
Disjoint from the WIN paths so a WIN/LOSS race cannot happen.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

try:
    import asyncpg  # type: ignore
except ImportError:
    print("asyncpg missing — install in engine venv", file=sys.stderr)
    sys.exit(1)

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(os.environ.get("ENGINE_ENV_PATH", "/home/novakash/novakash/engine/.env"))
except Exception:
    pass


_SLUG_RE = re.compile(r"^([a-z]+)-updown-([0-9a-z]+)-(\d+)$")

_BET_DIRECTION_FOR_TRADE = {"YES": "UP", "NO": "DOWN"}


def _parse_slug(slug: str):
    """Return (asset_upper, timeframe, window_ts) or None."""
    if not slug:
        return None
    m = _SLUG_RE.match(slug.strip().lower())
    if not m:
        return None
    asset, tf, ts = m.groups()
    try:
        return asset.upper(), tf, int(ts)
    except (TypeError, ValueError):
        return None


async def _list_candidates(conn, min_age_seconds: int, limit: int):
    rows = await conn.fetch(
        """
        SELECT id, strategy, direction, stake_usd, market_slug, status,
               execution_mode, polymarket_tx_hash, created_at
          FROM trades
         WHERE outcome IS NULL
           AND is_live = TRUE
           AND order_id NOT LIKE 'paper-%'
           AND status NOT IN ('CANCELLED', 'SKIPPED', 'FAILED_EXECUTION')
           AND market_slug IS NOT NULL AND market_slug != ''
           AND created_at < (NOW() - ($1 || ' seconds')::interval)
         ORDER BY created_at ASC
         LIMIT $2
        """,
        str(int(min_age_seconds)),
        int(limit),
    )
    return [dict(r) for r in rows]


async def _get_oracle(conn, asset: str, timeframe: str, window_ts: int):
    row = await conn.fetchrow(
        """SELECT MAX(oracle_outcome) AS oracle_outcome
             FROM window_snapshots
            WHERE asset = $1 AND timeframe = $2 AND window_ts = $3""",
        asset,
        timeframe,
        window_ts,
    )
    if not row:
        return None
    return row["oracle_outcome"]


async def _stamp_loss(conn, trade_id: int, pnl: float, dry_run: bool) -> bool:
    if dry_run:
        return False
    result = await conn.execute(
        """
        UPDATE trades
           SET outcome = 'LOSS',
               status = 'RESOLVED_LOSS',
               pnl_usd = $1,
               resolved_at = NOW()
         WHERE id = $2
           AND outcome IS NULL
           AND is_live = TRUE
        """,
        float(pnl),
        int(trade_id),
    )
    try:
        return int(result.split()[-1]) > 0
    except (ValueError, IndexError):
        return False


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-age-min", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=2000)
    args = ap.parse_args()

    db_url = os.environ.get("DATABASE_URL", "").replace("+asyncpg", "")
    if not db_url:
        print("DATABASE_URL missing", file=sys.stderr)
        sys.exit(2)

    conn = await asyncpg.connect(db_url)

    print("=" * 80)
    print(
        f"ORACLE-LOSS BACKFILL — min_age={args.min_age_min}min "
        f"dry_run={args.dry_run} limit={args.limit}"
    )
    print("=" * 80)

    candidates = await _list_candidates(conn, args.min_age_min * 60, args.limit)
    print(f"\nUnresolved live candidates: {len(candidates)}")

    stamped = 0
    skipped_no_oracle = 0
    skipped_oracle_win = 0
    skipped_unparseable = 0
    total_loss = 0.0

    for c in candidates:
        slug = c.get("market_slug") or ""
        direction = (c.get("direction") or "").upper()
        stake = c.get("stake_usd")
        trade_id = c.get("id")

        parsed = _parse_slug(slug)
        if not parsed or direction not in _BET_DIRECTION_FOR_TRADE or stake is None:
            skipped_unparseable += 1
            continue
        asset, timeframe, window_ts = parsed

        oracle = await _get_oracle(conn, asset, timeframe, window_ts)
        if oracle not in ("UP", "DOWN"):
            skipped_no_oracle += 1
            continue

        bet = _BET_DIRECTION_FOR_TRADE[direction]
        if oracle == bet:
            skipped_oracle_win += 1
            continue

        pnl = -float(stake)
        did = await _stamp_loss(conn, int(trade_id), round(pnl, 4), args.dry_run)
        if did:
            stamped += 1
            total_loss += pnl
            print(
                f"  stamped LOSS trade_id={trade_id} strategy={c.get('strategy')!s:<24s} "
                f"slug={slug:<32s} dir={direction:<3s} oracle={oracle:<4s} "
                f"pnl=${pnl:+.2f}"
            )
        elif args.dry_run:
            stamped += 1  # count as "would stamp"
            total_loss += pnl
            print(
                f"  WOULD stamp LOSS trade_id={trade_id} strategy={c.get('strategy')!s:<24s} "
                f"slug={slug:<32s} dir={direction:<3s} oracle={oracle:<4s} "
                f"pnl=${pnl:+.2f}"
            )

    print(
        f"\n{'DRY ' if args.dry_run else ''}SUMMARY: "
        f"stamped={stamped} skipped_oracle_win={skipped_oracle_win} "
        f"skipped_no_oracle={skipped_no_oracle} skipped_unparseable={skipped_unparseable}"
    )
    print(f"Total stamped LOSS pnl: ${total_loss:+.2f}")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
