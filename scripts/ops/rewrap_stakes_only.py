"""
Periodic rewrap: USDC -> pUSD for resolved trade stakes (wins + losses) +
50% of win profits.

Background
==========
Polymarket V2 settles in pUSD. Wins pay out in USDC. The engine bets out
of pUSD. We need to top pUSD back up after every cycle so the engine
never runs out of collateral.

Wrap formula (since last run):

    wrap = win_stake + loss_stake + (50% × win_profit)

  - win_stake: capital recycled from winning trades
  - loss_stake: capital reimbursed from losing trades (pUSD was burned;
    USDC wallet is unaffected because the engine bet from pUSD)
  - 50% of win profit: reinvested → bankroll compounds. Other 50%
    accumulates in USDC as cash reserve.

Without reimbursing loss stakes, pUSD slowly drains during losing
streaks even though the wallet's effective balance is fine. This was
observed live on 2026-04-30: pUSD dropped from $264 to $37 over a few
hours of bad sessions while USDC stayed at ~$200.

Unlike the full-balance cron (which would wrap ALL USDC above $3), this
script preserves the profit reserve while keeping pUSD topped up.

A state file tracks the last-run timestamp so the same trades are never
counted twice. Running twice in a row is safe (second run finds no new
trades -> skip).

Usage
=====
On Montreal (cron every 2h):

    cd /home/novakash/novakash && set -a && source engine/.env && set +a
    python3 scripts/ops/rewrap_stakes_only.py --execute

Flags:
    --dry-run   (default) Preview only, don't submit the on-chain wrap.
    --execute   Actually submit the wrap transaction.

State file: /home/novakash/rewrap_state.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_STATE_FILE = Path("/home/novakash/rewrap_state.json")
MIN_WRAP_USD = 5.0     # Don't wrap if total stakes < $5 (not worth the gas)
RESERVE_USD = 3.0      # Keep $3 USDC unwrapped as reserve
SCRIPT_DIR = Path(__file__).resolve().parent
WRAP_SCRIPT = SCRIPT_DIR / "wrap_usdc_pusd.py"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _load_state(state_file: Path) -> datetime:
    """Load last_rewrap_at from state file. Default to 24h ago if missing."""
    default = _now_utc() - timedelta(hours=24)
    if not state_file.exists():
        print(f"[state] No state file at {state_file}; defaulting to 24h ago: {default.isoformat()}")
        return default
    try:
        data = json.loads(state_file.read_text())
        ts = datetime.fromisoformat(data["last_rewrap_at"])
        # Ensure timezone-aware
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        print(f"[state] Last rewrap at: {ts.isoformat()}")
        return ts
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        print(f"[state] Corrupt state file ({exc}); defaulting to 24h ago")
        return default


def _save_state(state_file: Path, ts: datetime) -> None:
    """Write last_rewrap_at to state file."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({
        "last_rewrap_at": ts.isoformat(),
    }, indent=2) + "\n")
    print(f"[state] Saved last_rewrap_at = {ts.isoformat()}")


async def _query_wrap_amount(dsn: str, since: datetime, profit_reinvest_pct: float = 0.5) -> float:
    """Calculate wrap amount: full stakes (wins + losses) + fraction of profit.

    Wrap formula:
        wrap = win_stake + loss_stake + (profit_reinvest_pct × win_profit)

    Why losses too? Lost stakes are gone from pUSD (V2 collateral), but the
    USDC wallet is unchanged — winning trades elsewhere paid out into USDC.
    Without reimbursing loss stakes from USDC → pUSD, pUSD slowly drains
    every time we have a losing streak even though the WALLET is fine.

    Stake-source-of-truth: stake_usd (correct on losses).
    Profit-source-of-truth: pnl_usd on WINS only (loss pnl_usd may be
    inflated 2× by the legacy reconciler position-aggregate bug; we don't
    touch loss pnl_usd, only loss stake).

    Default 50% win profit reinvested → bankroll compounds while the other
    50% accumulates in USDC as cash reserve.
    """
    import asyncpg  # type: ignore

    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow(
            """
            SELECT
              COALESCE(SUM(stake_usd) FILTER (WHERE status='RESOLVED_WIN'),  0) AS win_stake,
              COALESCE(SUM(pnl_usd)   FILTER (WHERE status='RESOLVED_WIN'),  0) AS win_profit,
              COALESCE(SUM(stake_usd) FILTER (WHERE status='RESOLVED_LOSS'), 0) AS loss_stake,
              COUNT(*)               FILTER (WHERE status='RESOLVED_WIN')      AS win_n,
              COUNT(*)               FILTER (WHERE status='RESOLVED_LOSS')     AS loss_n
            FROM trades
            WHERE status IN ('RESOLVED_WIN','RESOLVED_LOSS')
              AND resolved_at > $1
            """,
            since,
        )
        win_stake  = float(row["win_stake"])  if row else 0.0
        win_profit = float(row["win_profit"]) if row else 0.0
        loss_stake = float(row["loss_stake"]) if row else 0.0
        win_n      = int(row["win_n"])         if row else 0
        loss_n     = int(row["loss_n"])        if row else 0
        reinvest = round(win_profit * profit_reinvest_pct, 2)
        total = round(win_stake + loss_stake + reinvest, 2)
        print(f"[db] Since {since.isoformat()}:")
        print(f"     Wins   : n={win_n} stake=${win_stake:.2f} profit=${win_profit:.2f}")
        print(f"     Losses : n={loss_n} stake=${loss_stake:.2f} (reimbursed in full)")
        print(f"     Reinvest {profit_reinvest_pct:.0%} of win profit: ${reinvest:.2f}")
        print(f"     Wrap total: ${total:.2f}  (= win_stake + loss_stake + reinvest)")
        return total
    finally:
        await conn.close()


def _get_usdc_balance_usd() -> float:
    """Cross-RPC USDC balance check via wrap_usdc_pusd.py helper.

    Imports the proven cross_check_usdc_balance function from the wrap
    script. Falls back to a subprocess call if the import fails.
    """
    # Use the same env vars as wrap_usdc_pusd.py
    funder = os.environ.get("POLY_FUNDER_ADDRESS")
    if not funder:
        raise RuntimeError("POLY_FUNDER_ADDRESS not set in env")

    # Try direct import from the wrap script
    try:
        # Add script dir to path temporarily for import
        sys.path.insert(0, str(SCRIPT_DIR))
        try:
            from wrap_usdc_pusd import cross_check_usdc_balance
            from web3 import Web3
            proxy = Web3.to_checksum_address(funder)
            raw_balance = cross_check_usdc_balance(proxy)
            balance_usd = raw_balance / 1e6
            print(f"[rpc] Proxy USDC balance (cross-RPC consensus): ${balance_usd:.4f}")
            return balance_usd
        finally:
            sys.path.pop(0)
    except Exception as exc:
        print(f"[warn] Direct import failed ({exc}); falling back to subprocess")

    # Fallback: run a quick Python one-liner that does the same thing
    code = (
        "import sys; sys.path.insert(0, %r); "
        "from wrap_usdc_pusd import cross_check_usdc_balance; "
        "from web3 import Web3; "
        "proxy = Web3.to_checksum_address(%r); "
        "print(cross_check_usdc_balance(proxy) / 1e6)"
    ) % (str(SCRIPT_DIR), funder)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Balance check failed: {result.stderr.strip()}")
    balance_usd = float(result.stdout.strip())
    print(f"[rpc] Proxy USDC balance (subprocess): ${balance_usd:.4f}")
    return balance_usd


def _do_wrap(amount_usd: float, execute: bool) -> int:
    """Shell out to wrap_usdc_pusd.py to perform the on-chain wrap."""
    cmd = [
        sys.executable,
        str(WRAP_SCRIPT),
        "--amount-usdc", f"{amount_usd:.4f}",
        "--skip-allowances",
    ]
    if execute:
        cmd.append("--execute")
    # else: dry-run is the default

    print(f"\n[wrap] Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, timeout=180)
    return result.returncode


async def async_main() -> int:
    parser = argparse.ArgumentParser(
        description="Rewrap only the stake amounts from resolved wins (keep profits in USDC)."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually submit the on-chain wrap. Without this, runs in dry-run mode.",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help=f"Path to the state file (default: {DEFAULT_STATE_FILE})",
    )
    args = parser.parse_args()

    state_file = args.state_file

    mode_label = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"=== rewrap_stakes_only [{mode_label}] — {_now_utc().isoformat()} ===\n")

    # ---- Validate env ----
    db_url_raw = os.environ.get("DATABASE_URL", "")
    if not db_url_raw:
        print("ERROR: DATABASE_URL not set — load engine .env")
        return 2
    # Normalize for asyncpg (strip SQLAlchemy dialect prefix)
    dsn = (
        db_url_raw
        .replace("postgresql+asyncpg://", "postgresql://")
        .replace("+asyncpg", "")
    )

    pk = os.environ.get("POLY_PRIVATE_KEY")
    funder = os.environ.get("POLY_FUNDER_ADDRESS")
    if not pk or not funder:
        print("ERROR: POLY_PRIVATE_KEY and POLY_FUNDER_ADDRESS must be set in env")
        return 2

    # ---- Load state ----
    last_rewrap = _load_state(state_file)

    # ---- Query winning stakes + profit ----
    try:
        wrap_amount = await _query_wrap_amount(dsn, last_rewrap, profit_reinvest_pct=0.5)
    except Exception as exc:
        print(f"ERROR: DB query failed: {exc}")
        return 1

    # ---- Check threshold ----
    if wrap_amount < MIN_WRAP_USD:
        print(f"\n[skip] Wrap amount ${wrap_amount:.2f} < minimum ${MIN_WRAP_USD:.2f}. Nothing to wrap.")
        return 0

    # ---- Check USDC balance ----
    try:
        usdc_balance = _get_usdc_balance_usd()
    except Exception as exc:
        print(f"ERROR: USDC balance check failed: {exc}")
        return 1

    required = wrap_amount + RESERVE_USD
    if usdc_balance < required:
        print(
            f"\n[skip] USDC balance ${usdc_balance:.4f} < required "
            f"${wrap_amount:.2f} (stakes+reinvest) + ${RESERVE_USD:.2f} (reserve) = ${required:.2f}. "
            f"Cannot wrap without dipping into reserve."
        )
        return 0

    # ---- Wrap ----
    kept = usdc_balance - wrap_amount
    print(f"\n[plan] Wrapping ${wrap_amount:.2f} USDC -> pUSD (stakes + 50% profit)")
    print(f"       Keeping ${kept:.2f} USDC (50% profit accumulating)")

    rc = _do_wrap(wrap_amount, args.execute)

    if rc != 0:
        print(f"\n[error] wrap_usdc_pusd.py exited with code {rc}")
        return 1

    # ---- Update state (only on successful execute) ----
    if args.execute:
        _save_state(state_file, _now_utc())
        print("\n[done] Wrap submitted and state updated.")
    else:
        print(f"\n[dry-run] Would wrap ${wrap_amount:.2f}. Re-run with --execute to submit.")

    return 0


def main() -> int:
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
