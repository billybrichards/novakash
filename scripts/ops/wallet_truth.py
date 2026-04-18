"""Canonical wallet-truth report — USE THIS instead of trusting TG alerts or the DB trades table.

Per memory note ``reference_clob_audit.md``: **"The DB lies — always check the CLOB."**

This script is the single source of truth for session P&L questions. It uses only
FREE data sources (no relayer quota consumed):

  1. ``data-api.polymarket.com/activity``   — trades + redeems
  2. ``data-api.polymarket.com/positions``  — current token holdings
  3. Polygon RPC ``eth_call`` via ``POLYGON_RPC_URL`` — on-chain USDC balance
  4. ``wallet_snapshots`` table (Montreal Postgres) — reconciler history

Produces:
  - Last 2h window: per-window resolution + P&L
  - Last 8h window: per-window resolution + P&L
  - Effective balance NOW: cash USDC + pending winning tokens

Usage
-----
On Montreal (has DATABASE_URL + POLYGON_RPC_URL + funder env):

    ssh novakash@15.223.247.178 'python3 /home/novakash/novakash/scripts/ops/wallet_truth.py'

Or locally (requires .env with the same vars reachable):

    python3 scripts/ops/wallet_truth.py

Or pass a custom window:

    python3 scripts/ops/wallet_truth.py --hours 4

Why this exists
---------------
Every session has repeatedly mis-reported P&L by trusting one of:
  - The trades DB table (has EXPIRED/orphan noise — stakes 10-50x actual)
  - Telegram reconcile-pass alerts (mix fresh losses with legacy orphans)
  - Raw cashflow math (ignores deposit events + pending-redeem lag)

This script grounds every number against on-chain + CLOB APIs, which
cannot lie. If a future Claude session is asked for "how are we doing?"
or "what's the P&L?", running this script is the FIRST action.

Session anti-patterns to avoid (actual examples from 2026-04-16):
  - "-$99.69 cashflow" (noisy, ignored deposit) — WRONG
  - "+$64.46 from trades table" (EXPIRED rows inflated) — WRONG
  - Reading "LOSS -$4.86" in TG reconcile pass as a fresh loss when it
    was actually a pre-#211 orphan settled days ago — WRONG

Always prefer the script output over any TG alert.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

# Load engine env for DATABASE_URL + POLYGON_RPC_URL + POLY_FUNDER_ADDRESS.
# Falls back to process env if the .env path doesn't exist.
for env_path in (
    "/home/novakash/novakash/engine/.env",
    os.path.expanduser("~/Code/novakash/engine/.env"),
    ".env",
):
    if os.path.exists(env_path):
        load_dotenv(env_path)
        break

USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # Polygon USDC


def _rpc():
    url = os.environ.get("POLYGON_RPC_URL")
    if not url:
        raise RuntimeError("POLYGON_RPC_URL not set — load engine .env")
    return url


def _funder():
    addr = os.environ.get("POLY_FUNDER_ADDRESS")
    if not addr:
        raise RuntimeError("POLY_FUNDER_ADDRESS not set — load engine .env")
    return addr


def onchain_usdc() -> float:
    """USDC balance of the funder/proxy wallet (on-chain source of truth)."""
    addr = _funder()
    data = "0x70a08231" + addr[2:].lower().rjust(64, "0")
    payload = json.dumps(
        {"jsonrpc": "2.0", "method": "eth_call",
         "params": [{"to": USDC_CONTRACT, "data": data}, "latest"], "id": 1}
    ).encode()
    req = urllib.request.Request(
        _rpc(), data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "curl/7.88"},
    )
    r = urllib.request.urlopen(req, timeout=10).read()
    return int(json.loads(r)["result"], 16) / 1e6


def poly_activity(since_ts: int) -> list[dict]:
    """All TRADE + REDEEM events from Polymarket data-api since ``since_ts``."""
    addr = _funder()
    all_rows: list[dict] = []
    offset = 0
    while offset < 3000:
        url = (
            "https://data-api.polymarket.com/activity?user="
            + addr + "&limit=500&offset=" + str(offset)
        )
        rows = json.loads(
            urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                timeout=15,
            ).read()
        )
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < 500:
            break
        offset += 500
    return [r for r in all_rows if r["timestamp"] >= since_ts]


def poly_positions() -> list[dict]:
    """Current (unredeemed) positions held by the proxy wallet."""
    addr = _funder()
    url = "https://data-api.polymarket.com/positions?user=" + addr + "&limit=500"
    return [
        p for p in json.loads(
            urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                timeout=15,
            ).read()
        )
        if float(p.get("size", 0)) > 0.01
    ]


async def _wallet_history(hours: int) -> list[tuple]:
    """wallet_snapshots rows for the last N hours (Montreal-side only)."""
    try:
        import asyncpg  # type: ignore
    except ImportError:
        return []
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        return []
    conn = await asyncpg.connect(db_url)
    try:
        rows = await conn.fetch(
            "SELECT recorded_at, balance_usdc FROM wallet_snapshots "
            "WHERE recorded_at > NOW() - INTERVAL '"
            + str(hours) + " hours' ORDER BY recorded_at ASC"
        )
    finally:
        await conn.close()
    return list(rows)


def wallet_history(hours: int) -> list[tuple]:
    try:
        return asyncio.run(_wallet_history(hours))
    except Exception:
        return []


def _title_short(t: dict) -> str:
    title = t.get("title", "")
    if "Bitcoin Up or Down" in title:
        return title.replace("Bitcoin Up or Down - ", "").split(" ET")[0] + " ET"
    return title[:35]


def _analyse(rows: list[dict], positions_by_cond: dict) -> dict:
    trades = [r for r in rows if r["type"] == "TRADE"]
    redeems = [
        r for r in rows
        if r["type"] == "REDEEM" and float(r.get("usdcSize", 0)) > 0.01
    ]

    by_cond: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_cond[t["conditionId"]].append(t)

    redeems_by_cond: dict[str, float] = defaultdict(float)
    for r in redeems:
        redeems_by_cond[r["conditionId"]] += float(r["usdcSize"])

    wins_resolved: list[tuple] = []
    wins_pending: list[tuple] = []
    losses: list[tuple] = []
    opens: list[tuple] = []
    total_spent = 0.0

    for cid, fills in by_cond.items():
        cost = sum(float(f["usdcSize"]) for f in fills)
        total_spent += cost
        first = fills[0]
        ts = datetime.fromtimestamp(
            first["timestamp"], tz=timezone.utc
        ).strftime("%H:%M")
        direction = first.get("outcome", "?")
        title = _title_short(first)
        payout = redeems_by_cond.get(cid, 0.0)
        pos = positions_by_cond.get(cid)
        if payout > 0:
            wins_resolved.append((ts, direction, cost, payout, title))
        elif pos:
            cv = float(pos["currentValue"])
            if cv > cost * 1.3:
                wins_pending.append((ts, direction, cost, cv, title))
            elif cv < 0.1:
                losses.append((ts, direction, cost, title))
            else:
                opens.append((ts, direction, cost, cv, title))
        else:
            # No redeem record + not in held positions = worthless loss
            losses.append((ts, direction, cost, title))

    return {
        "wins_resolved": wins_resolved,
        "wins_pending": wins_pending,
        "losses": losses,
        "opens": opens,
        "total_spent": total_spent,
        "n_fills": len(trades),
        "n_redeems": len(redeems),
    }


def _print_window(label: str, rows: list[dict], positions_by_cond: dict) -> None:
    r = _analyse(rows, positions_by_cond)
    n_windows = (
        len(r["wins_resolved"]) + len(r["wins_pending"])
        + len(r["losses"]) + len(r["opens"])
    )
    print("=" * 76)
    print(label)
    print("=" * 76)
    print(f"  Unique windows: {n_windows}  "
          f"({r['n_fills']} fills, {r['n_redeems']} redeems)")
    print()

    if r["wins_resolved"]:
        print("  WINS REDEEMED (USDC already in wallet):")
        for ts, d, c, p, t in r["wins_resolved"]:
            print(f"    {ts} {d:<4s}  cost ${c:5.2f} -> +${p:5.2f}  "
                  f"(net +${p - c:5.2f})  {t}")

    if r["wins_pending"]:
        print("  WINS PENDING (stuck as tokens — will land when NegRisk catches up):")
        for ts, d, c, cv, t in r["wins_pending"]:
            print(f"    {ts} {d:<4s}  cost ${c:5.2f} -> value ${cv:5.2f}  "
                  f"(+${cv - c:5.2f})  {t}")

    if r["losses"]:
        print("  LOSSES:")
        for ts, d, c, t in r["losses"]:
            print(f"    {ts} {d:<4s}  -${c:5.2f}  {t}")

    if r["opens"]:
        print("  OPEN markets (still trading, result unknown):")
        for ts, d, c, cv, t in r["opens"]:
            print(f"    {ts} {d:<4s}  cost ${c:5.2f} -> mark ${cv:5.2f}  {t}")

    got = sum(p for _, _, _, p, _ in r["wins_resolved"])
    pending_usd = sum(cv for _, _, _, cv, _ in r["wins_pending"])
    pending_cost = sum(c for _, _, c, _, _ in r["wins_pending"])
    losses_usd = sum(c for _, _, c, _ in r["losses"])

    print()
    print("  SUMMARY:")
    print(f"    Spent on trades:              ${r['total_spent']:8.2f}")
    print(f"    Redeemed to USDC already:     ${got:8.2f}")
    print(f"    Realized cashflow:            ${got - r['total_spent']:+8.2f}")
    print(f"    Realized losses:              -${losses_usd:.2f}")
    print(f"    Pending wins (will land):     ${pending_usd:8.2f} "
          f"(+${pending_usd - pending_cost:+.2f} unrealized)")
    realised_plus_unreal = (got - r["total_spent"]) + (pending_usd - pending_cost)
    print(f"    Net effective (realized + unrealized_on_pending):")
    print(f"      = {got - r['total_spent']:+.2f} + "
          f"{pending_usd - pending_cost:+.2f} = ${realised_plus_unreal:+.2f}")
    print()


def main(hours_window: int = 8) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hours", type=int, default=hours_window,
        help="Primary lookback in hours (default 8). Also always prints last 2h.",
    )
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    print("=" * 76)
    print(
        "WALLET TRUTH  —  "
        + now.strftime("%Y-%m-%d %H:%M:%S UTC")
        + "  —  free APIs, 0 relayer quota"
    )
    print("=" * 76)
    print()

    usdc = onchain_usdc()
    print(f"USDC on-chain NOW:              ${usdc:.4f}")

    # Optional context: early/recent wallet snapshots
    for h in (args.hours, 1):
        snaps = wallet_history(h)
        if snaps:
            label = f"Wallet snap {h}h ago"
            print(f"{label:32s}${float(snaps[0][1]):.2f}")
    print()

    positions = poly_positions()
    positions_by_cond = {p["conditionId"]: p for p in positions}

    rows_primary = poly_activity(
        int((now - timedelta(hours=args.hours)).timestamp())
    )
    rows_2h = poly_activity(int((now - timedelta(hours=2)).timestamp()))

    _print_window("LAST 2 HOURS", rows_2h, positions_by_cond)
    _print_window(f"LAST {args.hours} HOURS", rows_primary, positions_by_cond)

    # Effective balance right now
    pending_usd_all = sum(
        float(p.get("currentValue", 0))
        for p in positions
        if p.get("redeemable")
        and float(p.get("currentValue", 0)) > float(p.get("size", 0)) * 0.5
    )
    print("=" * 76)
    print("EFFECTIVE BALANCE RIGHT NOW")
    print("=" * 76)
    print(f"  Cash USDC:                    ${usdc:8.2f}")
    print(f"  + Pending wins (all ages):    ${pending_usd_all:8.2f}")
    print(f"  = Total effective:            ${usdc + pending_usd_all:8.2f}")
    print()


if __name__ == "__main__":
    main()
