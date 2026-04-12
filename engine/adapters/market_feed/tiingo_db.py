"""Tiingo DB adapter -- reads latest Tiingo price from PostgreSQL.

Implements :class:`engine.domain.ports.MarketFeedPort` by delegating to
``DBClient.get_latest_tiingo_price``.  The price originates from the
``TiingoFeed`` poller that writes to ``ticks_tiingo`` every 2s; this
adapter just reads the most recent row.

This is the DB-backed fallback for Tiingo -- when the REST candle fetch
fails, the consensus builder can try this adapter as a second attempt
using the cached top-of-book price.

Phase 2 deliverable (CA-02).  Nothing imports this adapter yet.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Optional

import structlog

from domain.ports import MarketFeedPort
from domain.value_objects import Tick, WindowClose

if TYPE_CHECKING:
    from persistence.db_client import DBClient

log = structlog.get_logger(__name__)


class TiingoDbAdapter(MarketFeedPort):
    """Reads Tiingo price from the ``ticks_tiingo`` table.

    Parameters
    ----------
    db_client : DBClient
        The shared database client with an active asyncpg pool.
    """

    def __init__(self, db_client: "DBClient") -> None:
        self._db = db_client
        self._log = log.bind(adapter="tiingo_db")

    # -- MarketFeedPort: get_latest_tick ------------------------------------

    async def get_latest_tick(self, asset: str) -> Optional[Tick]:
        """Return the latest Tiingo top-of-book price as a Tick.

        Delegates to ``DBClient.get_latest_tiingo_price``.  Returns
        ``None`` if the DB has no rows or the query fails.
        """
        try:
            price = await self._db.get_latest_tiingo_price(asset)
            if price is None:
                return None

            # TODO: TECH_DEBT - populate Tick fields (price, ts, source)
            # once the VO is fleshed out in Phase 1 value-object work.
            return Tick()

        except Exception as exc:
            self._log.debug(
                "tiingo_db.get_latest_tick_failed",
                error=str(exc)[:80],
            )
            return None

    # -- MarketFeedPort: get_window_delta -----------------------------------

    async def get_window_delta(
        self,
        asset: str,
        window_ts: int,
        open_price: float,
    ) -> Optional[float]:
        """Compute pct delta using the latest Tiingo price vs ``open_price``.

        Simple point-in-time delta: ``(tiingo_price - open_price) / open_price * 100``.
        Returns ``None`` if no Tiingo price is available.
        """
        try:
            price = await self._db.get_latest_tiingo_price(asset)
            if price is None or open_price <= 0:
                return None

            delta = (price - open_price) / open_price * 100
            self._log.debug(
                "tiingo_db.delta",
                asset=asset,
                tiingo_price=f"${price:,.2f}",
                open_price=f"${open_price:,.2f}",
                delta=f"{delta:+.4f}%",
            )
            return delta

        except Exception as exc:
            self._log.debug(
                "tiingo_db.get_window_delta_failed",
                error=str(exc)[:80],
            )
            return None

    # -- MarketFeedPort: subscribe_window_close -----------------------------

    def subscribe_window_close(
        self,
        asset: str,
        timeframe: str,
    ) -> AsyncIterator[WindowClose]:
        """Not implemented -- Tiingo DB is poll-based, not streaming."""
        raise NotImplementedError(
            "TiingoDbAdapter is poll-based; use a WebSocket feed for "
            "window-close subscriptions"
        )
