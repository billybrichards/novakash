"""Orphan-trade reconciler — detect on-chain Polymarket fills missing from the trades DB.

Some 5-min Up/Down fills happen on-chain but never persist to the ``trades`` table.
Suspected root cause: ``asyncio.CancelledError`` (a ``BaseException``, not caught by
``except Exception``) propagating through ``engine/use_cases/execute_trade.py`` after
``_executor.execute_order`` succeeds but before the trade-persist call. PR #401 fixed
the ``committed=True`` flag for fill_slot release, but the trade-persist path may
still be racy. Parallel v10 tasks where the second task hits
``already_filled_this_window`` after the first's fill returned can also strand a
trade record.

This script is **READ-ONLY**. No DB writes. No on-chain writes. It:

  1. Pulls ALL positions for the proxy wallet from data-api.polymarket.com.
  2. For each Up/Down 5-min position with ``totalBought > 0.5``, checks the
     ``trades`` table for any row whose ``metadata->>'window_ts'`` matches the
     window_ts decoded from the position's slug (``btc-updown-5m-<ts>``).
  3. Flags as ORPHAN if no trade row exists for that window_ts.
     Flags as SUSPICIOUS if a trade exists but ``fill_size`` drifts >20% from
     ``totalBought``.
  4. Separately flags partial-sell residuals — positions where exit-monitor sells
     left behind a winning (or losing) tail (realizedPnl < -1.0 AND
     currentValue > 0.1 AND size > 0).
  5. Prints a console table + JSON summary at the end.

Usage on Montreal (has DATABASE_URL + POLY_FUNDER_ADDRESS in engine/.env):

    set -a && source engine/.env && set +a
    python3 scripts/ops/reconcile_orphan_trades.py

Or pass a custom funder / lookback:

    python3 scripts/ops/reconcile_orphan_trades.py --proxy 0x... --hours 24

Reference test cases (orphan windows from 2026-04-27 night):
  - 1777263600 (04:23 UTC) v10 fill 0x79ec...d556df, $7.47, won @ $0.75
  - 1777257600 (02:40 UTC) DOWN, totalBought=$11.86, realizedPnl=-$3.74,
    residual won @ $1.00 with cv=$0.54
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

try:
    from dotenv import load_dotenv  # type: ignore
except ImportError:  # pragma: no cover — dotenv optional
    load_dotenv = None  # type: ignore

# Load engine env if available; fall back to process env otherwise.
if load_dotenv is not None:
    for env_path in (
        "/home/novakash/novakash/engine/.env",
        os.path.expanduser("~/Code/novakash/engine/.env"),
        "engine/.env",
        ".env",
    ):
        if os.path.exists(env_path):
            load_dotenv(env_path)
            break


DEFAULT_PROXY = "0x181D2ED714E0f7Fe9c6e4f13711376eDaab25E10"
SLUG_WINDOW_RE = re.compile(r"btc-updown-5m-(\d+)$")
DATAAPI_POSITIONS = "https://data-api.polymarket.com/positions"


@dataclass
class Position:
    """Subset of data-api position fields we care about."""

    condition_id: str
    slug: str
    outcome: str  # "Up" / "Down" (data-api capitalisation)
    total_bought: float
    realized_pnl: float
    current_value: float
    size: float
    redeemable: bool
    title: str
    window_ts: Optional[int] = None

    @classmethod
    def from_api(cls, raw: dict) -> "Position":
        slug = raw.get("slug") or ""
        m = SLUG_WINDOW_RE.search(slug)
        return cls(
            condition_id=raw.get("conditionId", ""),
            slug=slug,
            outcome=raw.get("outcome", "?"),
            total_bought=float(raw.get("totalBought") or 0.0),
            realized_pnl=float(raw.get("realizedPnl") or 0.0),
            current_value=float(raw.get("currentValue") or 0.0),
            size=float(raw.get("size") or 0.0),
            redeemable=bool(raw.get("redeemable") or False),
            title=(raw.get("title") or "")[:60],
            window_ts=int(m.group(1)) if m else None,
        )


@dataclass
class TradeRow:
    """Subset of trades table fields used for cross-reference."""

    id: int
    strategy_id: Optional[str]
    direction: Optional[str]  # "UP" / "DOWN"
    fill_price: Optional[float]
    fill_size: Optional[float]
    status: Optional[str]
    outcome: Optional[str]


@dataclass
class Findings:
    orphans: list[Position] = field(default_factory=list)
    suspicious: list[tuple[Position, TradeRow]] = field(default_factory=list)
    residuals: list[Position] = field(default_factory=list)
    matched: int = 0


def fetch_positions(proxy: str, *, size_threshold: float = 0.001, limit: int = 500) -> list[Position]:
    """Pull all positions for ``proxy`` from data-api (read-only HTTP)."""
    url = (
        f"{DATAAPI_POSITIONS}?user={proxy}"
        f"&sizeThreshold={size_threshold}&limit={limit}"
    )
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (orphan-reconciler)"}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = json.loads(r.read())
    return [Position.from_api(p) for p in raw]


def filter_5min_btc(positions: list[Position]) -> list[Position]:
    """Only Up/Down 5-min BTC windows (slug decodes to a window_ts)."""
    return [p for p in positions if p.window_ts is not None]


async def query_trades_for_windows(
    db_url: str, window_ts_list: list[int]
) -> dict[int, list[TradeRow]]:
    """Return mapping of window_ts -> list[TradeRow] (empty list if none)."""
    if not window_ts_list:
        return {}
    try:
        import asyncpg  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "asyncpg is required to query Postgres; install it (`pip install asyncpg`)"
        ) from exc

    # Build a single IN-clause query keyed by (metadata->>'window_ts').
    # window_ts is stored as text inside metadata jsonb.
    placeholders = ",".join(f"${i + 1}" for i in range(len(window_ts_list)))
    query = (
        "SELECT id, strategy_id, direction, fill_price, fill_size, status, outcome, "
        "metadata->>'window_ts' AS wts "
        "FROM trades "
        f"WHERE metadata->>'window_ts' IN ({placeholders}) "
        "ORDER BY created_at"
    )
    args = [str(ts) for ts in window_ts_list]

    conn = await asyncpg.connect(db_url)
    try:
        rows = await conn.fetch(query, *args)
    finally:
        await conn.close()

    result: dict[int, list[TradeRow]] = {ts: [] for ts in window_ts_list}
    for r in rows:
        try:
            wts = int(r["wts"])
        except (TypeError, ValueError):
            continue
        if wts in result:
            result[wts].append(
                TradeRow(
                    id=r["id"],
                    strategy_id=r["strategy_id"],
                    direction=r["direction"],
                    fill_price=(
                        float(r["fill_price"]) if r["fill_price"] is not None else None
                    ),
                    fill_size=(
                        float(r["fill_size"]) if r["fill_size"] is not None else None
                    ),
                    status=r["status"],
                    outcome=r["outcome"],
                )
            )
    return result


def _direction_match(position_outcome: str, trade_direction: Optional[str]) -> bool:
    """data-api uses 'Up'/'Down'; trades.direction is 'UP'/'DOWN'."""
    if not trade_direction:
        return False
    return position_outcome.upper() == trade_direction.upper()


def reconcile(
    positions: list[Position],
    trades_by_window: dict[int, list[TradeRow]],
    *,
    min_total_bought: float = 0.5,
    fill_drift_pct: float = 0.20,
) -> Findings:
    """Cross-reference every 5-min position against the trades table."""
    f = Findings()

    for p in positions:
        if p.window_ts is None:
            continue

        # --- Partial-sell residual detector (independent of orphan check) ---
        if p.realized_pnl < -1.0 and p.current_value > 0.1 and p.size > 0:
            f.residuals.append(p)

        # Only chase orphans for materially-sized fills.
        if p.total_bought < min_total_bought:
            continue

        rows = trades_by_window.get(p.window_ts, [])
        # Prefer a row matching this position's direction.
        dir_match = [r for r in rows if _direction_match(p.outcome, r.direction)]

        if not rows:
            f.orphans.append(p)
            continue
        if not dir_match:
            # Window has trades but none match this Up/Down side.
            f.orphans.append(p)
            continue

        # Direction-matching row exists — check fill size drift.
        # Aggregate fill_size across matching rows (multiple fills for one window).
        total_fill = sum(r.fill_size or 0.0 for r in dir_match)
        # On Polymarket, totalBought is in USDC notional — fill_size is shares.
        # Without fill_price we cannot back out notional cleanly; skip drift check
        # if any matching row lacks fill_price.
        if total_fill > 0 and all(r.fill_price for r in dir_match):
            notional = sum((r.fill_size or 0.0) * (r.fill_price or 0.0) for r in dir_match)
            if notional > 0:
                drift = abs(notional - p.total_bought) / max(p.total_bought, 1e-9)
                if drift > fill_drift_pct:
                    f.suspicious.append((p, dir_match[0]))
                    continue

        f.matched += 1

    return f


def _fmt_pos_row(label: str, p: Position) -> str:
    return (
        f"  {label:11}  ts={p.window_ts}  {p.outcome:<4}  "
        f"bought=${p.total_bought:6.2f}  realized=${p.realized_pnl:+6.2f}  "
        f"cv=${p.current_value:6.2f}  size={p.size:7.2f}  "
        f"cid={p.condition_id[:14]}..  {p.title}"
    )


def render_report(f: Findings, *, total_positions: int) -> dict:
    """Print human report and return a JSON-serialisable summary."""
    print("=" * 92)
    print("ORPHAN-TRADE RECONCILER")
    print("=" * 92)
    print(f"  Total 5-min positions checked:   {total_positions}")
    print(f"  Matched (DB row found):          {f.matched}")
    print(f"  ORPHANS (on-chain, no DB row):   {len(f.orphans)}")
    print(f"  SUSPICIOUS (fill_size drift):    {len(f.suspicious)}")
    print(f"  PARTIAL-SELL RESIDUALS:          {len(f.residuals)}")
    print()

    if f.orphans:
        print("-- ORPHANS --------------------------------------------------------------------")
        for p in sorted(f.orphans, key=lambda x: x.window_ts or 0):
            print(_fmt_pos_row("ORPHAN", p))
        print()

    if f.suspicious:
        print("-- SUSPICIOUS (fill_size drift > 20%) -----------------------------------------")
        for p, t in sorted(f.suspicious, key=lambda x: x[0].window_ts or 0):
            print(_fmt_pos_row("SUSPICIOUS", p))
            print(
                f"      DB row id={t.id} dir={t.direction} "
                f"fill_size={t.fill_size} fill_price={t.fill_price} "
                f"status={t.status} outcome={t.outcome}"
            )
        print()

    if f.residuals:
        print("-- PARTIAL-SELL RESIDUALS (exit-monitor left a tail) --------------------------")
        for p in sorted(f.residuals, key=lambda x: x.window_ts or 0):
            print(_fmt_pos_row("RESIDUAL", p))
        print()

    summary = {
        "total_positions_checked": total_positions,
        "matched": f.matched,
        "orphan_count": len(f.orphans),
        "suspicious_count": len(f.suspicious),
        "residual_count": len(f.residuals),
        "orphan_total_bought_usd": round(sum(p.total_bought for p in f.orphans), 2),
        "orphan_realized_loss_usd": round(
            sum(p.realized_pnl for p in f.orphans if p.realized_pnl < 0), 2
        ),
        "orphan_unredeemed_value_usd": round(
            sum(p.current_value for p in f.orphans), 2
        ),
        "residual_realized_loss_usd": round(
            sum(p.realized_pnl for p in f.residuals if p.realized_pnl < 0), 2
        ),
        "residual_unredeemed_value_usd": round(
            sum(p.current_value for p in f.residuals), 2
        ),
        "orphans": [
            {
                "window_ts": p.window_ts,
                "direction": p.outcome,
                "total_bought": round(p.total_bought, 2),
                "realized_pnl": round(p.realized_pnl, 2),
                "current_value": round(p.current_value, 2),
                "condition_id": p.condition_id,
                "slug": p.slug,
            }
            for p in f.orphans
        ],
    }

    print("=" * 92)
    print("JSON SUMMARY")
    print("=" * 92)
    print(json.dumps(summary, indent=2))
    return summary


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--proxy",
        default=os.environ.get("POLY_FUNDER_ADDRESS", DEFAULT_PROXY),
        help="Polymarket proxy wallet (defaults to POLY_FUNDER_ADDRESS env)",
    )
    p.add_argument(
        "--min-total-bought",
        type=float,
        default=0.5,
        help="Ignore positions with totalBought below this USD threshold (default 0.5)",
    )
    p.add_argument(
        "--fill-drift-pct",
        type=float,
        default=0.20,
        help="Flag SUSPICIOUS when fill notional drift > this fraction (default 0.20)",
    )
    return p.parse_args(argv)


async def _async_main(args: argparse.Namespace) -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set — load engine/.env first", file=sys.stderr)
        return 2

    print(f"Querying data-api positions for proxy {args.proxy} ...", file=sys.stderr)
    raw_positions = fetch_positions(args.proxy)
    five_min = filter_5min_btc(raw_positions)
    print(
        f"  {len(raw_positions)} total positions, {len(five_min)} 5-min BTC windows",
        file=sys.stderr,
    )

    window_ts_list = sorted({p.window_ts for p in five_min if p.window_ts is not None})
    print(f"  Cross-referencing {len(window_ts_list)} unique window_ts in trades table ...", file=sys.stderr)
    trades_by_window = await query_trades_for_windows(db_url, window_ts_list)

    findings = reconcile(
        five_min,
        trades_by_window,
        min_total_bought=args.min_total_bought,
        fill_drift_pct=args.fill_drift_pct,
    )
    render_report(findings, total_positions=len(five_min))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())
