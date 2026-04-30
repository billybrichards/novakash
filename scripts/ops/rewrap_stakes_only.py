"""
Balance-driven pUSD top-up cron — replaces profit-sinking trade-driven rewrap.

Policy
======
1. **pUSD floor at $100.** If pUSD balance < $100, wrap (100 - pUSD) USDC
   into pUSD so pUSD returns to $100. This keeps the engine collateralized
   for trading without excess pUSD inventory.
2. **Let USDC accumulate.** Profits land in USDC; do NOT auto-wrap them
   back to pUSD. USDC is real cash reserve.
3. **Overflow at USDC > $200.** When USDC accumulates above $200 (a
   healthy reserve buffer), wrap the excess (`USDC - 200`) into pUSD.
   This grows the trading bankroll only when we're winning hard.

Decision tree:
    if pUSD < 100:    → wrap (100 - pUSD), capped at available USDC
    elif USDC > 200:  → wrap (USDC - 200) overflow into pUSD
    else:             → no-op (let USDC accumulate)

Why this replaces the trade-driven formula
==========================================
The previous script wrapped `win_stake + loss_stake + 50% × win_profit`
every 30 min. That sank profits back into pUSD continuously, defeating
the goal of accumulating USDC as cash reserve. On 2026-04-30 it kept
pUSD topped up but USDC never grew despite winning sessions.

The new balance-driven model is simpler:
- No DB query (no trade-table dependency, no inflated-pnl risk)
- No state file (idempotent — re-running is a no-op)
- Self-correcting (deficit/overflow naturally regulates flow)

Usage
=====
On Montreal (cron every 30 min at :03/:33):

    cd /home/novakash/novakash && set -a && source engine/.env && set +a
    python3 scripts/ops/rewrap_stakes_only.py --execute

Flags:
    --dry-run        (default) Preview only.
    --execute        Submit the wrap transaction.
    --pusd-floor     Override the pUSD floor (default $100).
    --usdc-overflow  Override the USDC overflow threshold (default $200).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PUSD_FLOOR_USD = 100.0
DEFAULT_USDC_OVERFLOW_USD = 200.0
MIN_WRAP_USD = 5.0     # Below this, gas dominates — skip
RESERVE_USD = 3.0      # Always keep a tiny USDC reserve for gas / buffer
SCRIPT_DIR = Path(__file__).resolve().parent
WRAP_SCRIPT = SCRIPT_DIR / "wrap_usdc_pusd.py"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _read_balances() -> tuple[float, float]:
    """Return (pUSD, USDC) balances in human USD via wrap-script helpers.

    Reuses the proven cross-RPC USDC balance check + a single-call pUSD
    balance read. Both bypass any DB caching layer and read directly
    from Polygon RPC.
    """
    funder = os.environ.get("POLY_FUNDER_ADDRESS")
    if not funder:
        raise RuntimeError("POLY_FUNDER_ADDRESS not set in env")

    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        from wrap_usdc_pusd import (  # type: ignore
            PUSD,
            cross_check_usdc_balance,
            erc20_balance_of,
        )
        from web3 import Web3

        proxy = Web3.to_checksum_address(funder)

        # USDC: cross-RPC consensus (avoids cache lag — see memory)
        usdc_raw = cross_check_usdc_balance(proxy)
        usdc_usd = usdc_raw / 1e6

        # pUSD: single RPC read (less prone to cache inconsistency)
        rpc_url = os.environ.get("POLYGON_RPC_URL")
        if not rpc_url:
            raise RuntimeError("POLYGON_RPC_URL not set in env")
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        pusd_raw = erc20_balance_of(w3, PUSD, proxy)
        pusd_usd = pusd_raw / 1e6

        print(f"[rpc] proxy USDC: ${usdc_usd:.4f}   pUSD: ${pusd_usd:.4f}")
        return pusd_usd, usdc_usd
    finally:
        sys.path.pop(0)


def _decide_wrap(
    pusd_usd: float,
    usdc_usd: float,
    pusd_floor: float,
    usdc_overflow: float,
) -> tuple[float, str]:
    """Decide how much to wrap. Returns (amount_usd, reason).

    Decision tree:
      1. pUSD < floor    → top up to floor (capped at available USDC)
      2. USDC > overflow → wrap excess USDC above the overflow threshold
      3. otherwise       → no-op (let USDC accumulate as cash reserve)
    """
    available = max(0.0, usdc_usd - RESERVE_USD)

    # Case 1: pUSD below floor — top up
    if pusd_usd < pusd_floor:
        deficit = pusd_floor - pusd_usd
        wrap = min(deficit, available)
        reason = (
            f"pUSD ${pusd_usd:.2f} < floor ${pusd_floor:.2f} "
            f"(deficit ${deficit:.2f}, available ${available:.2f})"
        )
        return round(wrap, 2), reason

    # Case 2: USDC has overflowed — wrap excess
    if usdc_usd > usdc_overflow:
        excess = usdc_usd - usdc_overflow
        wrap = min(excess, available)  # always honor reserve
        reason = (
            f"USDC ${usdc_usd:.2f} > overflow ${usdc_overflow:.2f} "
            f"(excess ${excess:.2f}, wrapping into pUSD)"
        )
        return round(wrap, 2), reason

    # Case 3: nothing to do
    return 0.0, (
        f"pUSD ${pusd_usd:.2f} >= floor ${pusd_floor:.2f} AND "
        f"USDC ${usdc_usd:.2f} <= overflow ${usdc_overflow:.2f} (no-op)"
    )


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

    print(f"\n[wrap] Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, timeout=180)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Balance-driven pUSD top-up: keep pUSD at floor, let USDC "
            "accumulate, overflow excess USDC into pUSD."
        )
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Submit the on-chain wrap. Without this, runs in dry-run mode.",
    )
    parser.add_argument(
        "--pusd-floor",
        type=float,
        default=DEFAULT_PUSD_FLOOR_USD,
        help=f"pUSD floor target in USD (default: ${DEFAULT_PUSD_FLOOR_USD})",
    )
    parser.add_argument(
        "--usdc-overflow",
        type=float,
        default=DEFAULT_USDC_OVERFLOW_USD,
        help=(
            "USDC overflow threshold in USD; excess wrapped to pUSD "
            f"(default: ${DEFAULT_USDC_OVERFLOW_USD})"
        ),
    )
    args = parser.parse_args()

    mode_label = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"=== rewrap_stakes_only [{mode_label}] — {_now_utc().isoformat()} ===")
    print(
        f"[policy] pUSD floor=${args.pusd_floor:.2f}  "
        f"USDC overflow=${args.usdc_overflow:.2f}  "
        f"reserve=${RESERVE_USD:.2f}\n"
    )

    pk = os.environ.get("POLY_PRIVATE_KEY")
    funder = os.environ.get("POLY_FUNDER_ADDRESS")
    if not pk or not funder:
        print("ERROR: POLY_PRIVATE_KEY and POLY_FUNDER_ADDRESS must be set in env")
        return 2

    try:
        pusd_usd, usdc_usd = _read_balances()
    except Exception as exc:
        print(f"ERROR: balance read failed: {exc}")
        return 1

    wrap_amount, reason = _decide_wrap(
        pusd_usd, usdc_usd, args.pusd_floor, args.usdc_overflow
    )
    print(f"[decision] {reason}")
    print(f"[decision] wrap_amount=${wrap_amount:.2f}")

    if wrap_amount < MIN_WRAP_USD:
        if wrap_amount > 0:
            print(
                f"\n[skip] wrap ${wrap_amount:.2f} < min ${MIN_WRAP_USD:.2f} "
                f"(gas would dominate). Nothing to do."
            )
        else:
            print("\n[skip] no wrap needed.")
        return 0

    kept = usdc_usd - wrap_amount
    print(f"\n[plan] Wrapping ${wrap_amount:.2f} USDC -> pUSD")
    print(f"       Keeping ${kept:.2f} USDC")

    rc = _do_wrap(wrap_amount, args.execute)
    if rc != 0:
        print(f"\n[error] wrap_usdc_pusd.py exited with code {rc}")
        return 1

    if args.execute:
        print("\n[done] Wrap submitted.")
    else:
        print(
            f"\n[dry-run] Would wrap ${wrap_amount:.2f}. "
            f"Re-run with --execute to submit."
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        sys.exit(1)
