"""Backfill corrected pnl_usd on resolved trades.

The engine's PnL formula was computing ``shares = stake_usd / entry_price``
instead of using the actual ``fill_size`` (CLOB shares received). This
overcounts shares whenever ``stake_usd / entry_price > fill_size`` (price
improvement, partial fills, rounding). On-chain truth (Polymarket data-api)
showed -$575 net while the DB showed +$2,476 — the inflated WIN PnL is the
root cause.

Correct formula
---------------
  WIN:  pnl_usd = fill_size - stake_usd   (each share pays $1)
  LOSS: pnl_usd = -stake_usd              (you lose your stake)

LOSS rows are already correct and are NOT touched.

This script:
  1. Queries all RESOLVED_WIN trades that have fill_size populated.
  2. Computes the correct pnl_usd = fill_size - stake_usd.
  3. In dry-run (default): prints discrepancies.
  4. With --execute: UPDATEs the rows.

Idempotent — re-running produces no changes once values are correct.

Usage
-----
    # Dry-run (default): shows what would change
    python3 scripts/ops/backfill_pnl.py

    # Apply corrections
    python3 scripts/ops/backfill_pnl.py --execute

    # Also check LOSS rows for sanity (no corrections applied)
    python3 scripts/ops/backfill_pnl.py --include-losses

Safety
------
- Reads DATABASE_URL from engine/.env (same env as wallet_truth.py);
  falls back to process env.
- UPDATE clause uses ``WHERE id = $N`` with per-row IDs — surgical.
- Only RESOLVED_WIN rows with non-NULL fill_size are candidates.
- LOSS rows are read-only audited (printed if wrong) but never written.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

try:
    import asyncpg  # type: ignore
except ImportError:
    print("ERROR: asyncpg not installed. `pip install asyncpg`", file=sys.stderr)
    sys.exit(1)


# ── Config ──────────────────────────────────────────────────────────────
ENV_PATH = Path("/home/novakash/novakash/engine/.env")


def _load_env() -> dict[str, str]:
    """Pull DATABASE_URL from engine/.env or process env."""
    env: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    # Process env wins over .env file
    if os.environ.get("DATABASE_URL"):
        env["DATABASE_URL"] = os.environ["DATABASE_URL"]
    if not env.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL not found in env or engine/.env", file=sys.stderr)
        sys.exit(2)
    # asyncpg wants plain postgresql://
    env["DATABASE_URL"] = env["DATABASE_URL"].replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    return env


async def main(execute: bool, include_losses: bool) -> None:
    env = _load_env()
    pool = await asyncpg.create_pool(env["DATABASE_URL"], min_size=1, max_size=3)

    try:
        await _backfill_wins(pool, execute=execute)
        if include_losses:
            await _audit_losses(pool)
    finally:
        await pool.close()


async def _backfill_wins(pool: asyncpg.Pool, *, execute: bool) -> None:
    """Fix RESOLVED_WIN rows where pnl_usd != fill_size - stake_usd."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, order_id, strategy, direction,
                   stake_usd, entry_price, fill_size, pnl_usd,
                   outcome, status, created_at, resolved_at
            FROM trades
            WHERE status = 'RESOLVED_WIN'
              AND outcome = 'WIN'
              AND fill_size IS NOT NULL
              AND fill_size > 0
            ORDER BY resolved_at DESC NULLS LAST
            """
        )

    if not rows:
        print("No RESOLVED_WIN rows with fill_size found.")
        return

    print(f"\nFound {len(rows)} RESOLVED_WIN trades with fill_size.\n")
    print(f"{'ID':>6}  {'Strategy':<20} {'Stake':>8}  {'FillSz':>8}  "
          f"{'Entry':>7}  {'Old PnL':>10}  {'Correct':>10}  {'Delta':>10}  Action")
    print("-" * 110)

    corrections = 0
    total_old_pnl = 0.0
    total_new_pnl = 0.0
    updates: list[tuple[float, int]] = []

    for row in rows:
        trade_id = row["id"]
        stake = float(row["stake_usd"] or 0)
        fill_size = float(row["fill_size"])
        old_pnl = float(row["pnl_usd"] or 0)
        entry_price = float(row["entry_price"] or 0)
        strategy = row["strategy"] or "?"

        correct_pnl = round(fill_size - stake, 4)
        delta = round(correct_pnl - old_pnl, 4)

        total_old_pnl += old_pnl
        total_new_pnl += correct_pnl

        if abs(delta) < 0.0001:
            # Already correct
            action = "OK"
        else:
            corrections += 1
            action = "FIX" if execute else "WOULD_FIX"
            updates.append((correct_pnl, trade_id))

        if abs(delta) >= 0.0001:
            print(
                f"{trade_id:>6}  {strategy:<20} ${stake:>7.2f}  {fill_size:>8.2f}  "
                f"${entry_price:>6.4f}  ${old_pnl:>9.4f}  ${correct_pnl:>9.4f}  "
                f"${delta:>+9.4f}  {action}"
            )

    print("-" * 110)
    print(f"\nSummary:")
    print(f"  Total RESOLVED_WIN rows:  {len(rows)}")
    print(f"  Already correct:          {len(rows) - corrections}")
    print(f"  Need correction:          {corrections}")
    print(f"  Old total WIN PnL:        ${total_old_pnl:>+.2f}")
    print(f"  Correct total WIN PnL:    ${total_new_pnl:>+.2f}")
    print(f"  Total PnL adjustment:     ${total_new_pnl - total_old_pnl:>+.2f}")

    if corrections == 0:
        print("\nAll WIN PnL values are already correct. Nothing to do.")
        return

    if not execute:
        print(f"\n  DRY RUN — {corrections} rows would be updated.")
        print("  Re-run with --execute to apply corrections.")
        return

    # Apply corrections
    print(f"\nApplying {len(updates)} corrections...")
    async with pool.acquire() as conn:
        updated = 0
        for correct_pnl, trade_id in updates:
            result = await conn.execute(
                """UPDATE trades
                   SET pnl_usd = $1
                   WHERE id = $2
                     AND status = 'RESOLVED_WIN'
                     AND outcome = 'WIN'""",
                correct_pnl,
                trade_id,
            )
            count = int(result.split()[-1])
            updated += count

    print(f"  Updated {updated}/{len(updates)} rows.")
    print("  Done.")


async def _audit_losses(pool: asyncpg.Pool) -> None:
    """Read-only audit of RESOLVED_LOSS rows."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, stake_usd, pnl_usd, strategy
            FROM trades
            WHERE status = 'RESOLVED_LOSS'
              AND outcome = 'LOSS'
              AND pnl_usd IS NOT NULL
            ORDER BY resolved_at DESC NULLS LAST
            """
        )

    if not rows:
        print("\nNo RESOLVED_LOSS rows found.")
        return

    wrong = 0
    for row in rows:
        stake = float(row["stake_usd"] or 0)
        old_pnl = float(row["pnl_usd"])
        expected = round(-stake, 4)
        if abs(old_pnl - expected) >= 0.01:
            wrong += 1
            if wrong <= 20:
                print(
                    f"  LOSS anomaly: id={row['id']} stake=${stake:.2f} "
                    f"pnl=${old_pnl:.4f} expected=${expected:.4f} "
                    f"strategy={row['strategy']}"
                )

    print(f"\nLOSS audit: {len(rows)} rows, {wrong} anomalies"
          f"{' (showing first 20)' if wrong > 20 else ''}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill corrected pnl_usd on resolved trades."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Actually apply the corrections (default: dry-run).",
    )
    parser.add_argument(
        "--include-losses",
        action="store_true",
        default=False,
        help="Also audit RESOLVED_LOSS rows (read-only, no writes).",
    )
    args = parser.parse_args()

    asyncio.run(main(execute=args.execute, include_losses=args.include_losses))
