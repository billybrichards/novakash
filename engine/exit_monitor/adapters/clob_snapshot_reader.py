"""CLOBSnapshotReader — reads live CLOB best_bid/ask from the engine surface.

The engine's FullDataSurface already carries CLOB fields (clob_up_bid,
clob_up_ask, clob_down_bid, clob_down_ask) as part of its tick-level data.
We adapt those fields into the CLOBSnapshot DTO used by MonitorOpenTradesUseCase.

Design note: Polymarket 5-minute YES/NO CLOB bid side for held positions is a
$0.01 stub throughout window lifetime (documented in EXIT_MONITOR_FEASIBILITY.md
and PR #404). This reader captures that $0.01 stub faithfully — it is the honest
signal. The ev_delta column will reflect the real CLOB economics when backfilled.

TODO: clob_book_depth_usd is not currently available on the FullDataSurface.
The field exists in ticks_binance_book (Binance-side depth) but there is no
direct Polymarket held-side depth reading at the tick level. Add when the CLOB
sidecar table (add_ticks_binance_book.sql + related write path) exposes per-side
held depth. Until then, this field is always None in exit_monitor_shadow rows.
"""
from __future__ import annotations

from typing import Any, Optional

import structlog

from exit_monitor.use_cases.monitor_open_trades import CLOBSnapshot

log = structlog.get_logger(__name__)


def read_clob_snapshot(
    surface: Any,
    side: str,
) -> CLOBSnapshot:
    """Extract CLOBSnapshot from a FullDataSurface for a given trade side.

    Args:
        surface: engine FullDataSurface (or any object with clob_* attrs).
        side:    'UP' or 'DN' — the direction the trade is holding.

    Returns:
        CLOBSnapshot with best_bid_held, best_ask_held, best_bid_against,
        best_ask_against populated from surface CLOB fields. book_depth_usd
        is always None (see module TODO above).
    """
    if surface is None:
        return CLOBSnapshot()

    def _f(attr: str) -> Optional[float]:
        v = getattr(surface, attr, None)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    if side == "UP":
        # Holding UP token — UP bid is our exit price, DOWN ask is "against"
        best_bid_held = _f("clob_up_bid")
        best_ask_held = _f("clob_up_ask")
        best_bid_against = _f("clob_down_bid")
        best_ask_against = _f("clob_down_ask")
    else:
        # Holding DN token — DOWN bid is our exit price, UP ask is "against"
        best_bid_held = _f("clob_down_bid")
        best_ask_held = _f("clob_down_ask")
        best_bid_against = _f("clob_up_bid")
        best_ask_against = _f("clob_up_ask")

    return CLOBSnapshot(
        best_bid_held=best_bid_held,
        best_ask_held=best_ask_held,
        best_bid_against=best_bid_against,
        best_ask_against=best_ask_against,
        book_depth_usd=None,  # TODO: wire when per-side depth is available
    )


def read_clob_snapshots_by_asset(
    surface_by_asset: dict[str, Any],
    open_trades: list,
) -> dict[str, CLOBSnapshot]:
    """Build a {asset: CLOBSnapshot} dict for all assets in open_trades.

    When multiple open trades hold the same asset in different directions,
    we build a per-asset snapshot using the first trade's side. In practice
    each asset has at most one live trade at a time in novakash, so this
    is fine. If that changes, the caller should call read_clob_snapshot()
    per trade individually.
    """
    seen: dict[str, str] = {}
    for trade in open_trades:
        if trade.asset not in seen:
            seen[trade.asset] = trade.side

    result: dict[str, CLOBSnapshot] = {}
    for asset, side in seen.items():
        surface = surface_by_asset.get(asset)
        result[asset] = read_clob_snapshot(surface, side)
    return result
