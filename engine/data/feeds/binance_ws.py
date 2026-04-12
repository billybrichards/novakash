"""
Binance WebSocket Feed (Spot + Futures)

Supports two venues:
  - "futures" (default) — wss://fstream.binance.com, subscribes to aggTrade,
    depth20, and forceOrder for VPIN volume accumulation and cascade detection.
  - "spot" — wss://stream.binance.com:9443, subscribes to aggTrade only,
    providing spot BTC price for oracle-aligned delta calculation.

Reconnects automatically with exponential back-off.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from decimal import Decimal
from typing import Callable, Awaitable, Optional
import websockets
import structlog

from data.models import AggTrade, OrderBookSnapshot, ForcedLiquidation

log = structlog.get_logger(__name__)

BINANCE_SPOT_WSS = "wss://stream.binance.com:9443/stream"
BINANCE_FUTURES_WSS = "wss://fstream.binance.com/stream"
RECONNECT_DELAY_MAX = 60  # seconds


class BinanceWebSocketFeed:
    """
    Connects to Binance combined stream and dispatches
    typed events to registered handlers.

    Parameters:
        venue: "futures" (default) for fstream.binance.com (VPIN + liquidations),
               "spot" for stream.binance.com (oracle-aligned BTC price).

    Attributes:
        connected: True while the WebSocket is open and receiving messages.
        last_message_at: Timestamp of the most recently processed message.
        venue: "spot" or "futures".
    """

    def __init__(
        self,
        symbol: str = "btcusdt",
        venue: str = "futures",
        on_trade: Callable[[AggTrade], Awaitable[None]] | None = None,
        on_book: Callable[[OrderBookSnapshot], Awaitable[None]] | None = None,
        on_liquidation: Callable[[ForcedLiquidation], Awaitable[None]] | None = None,
    ) -> None:
        if venue not in ("spot", "futures"):
            raise ValueError(f"venue must be 'spot' or 'futures', got {venue!r}")
        self.symbol = symbol.lower()
        self.venue = venue
        self._on_trade = on_trade
        self._on_book = on_book
        self._on_liquidation = on_liquidation
        self._running = False
        self._reconnect_delay = 1.0
        self._connected = False
        self._last_message_at: Optional[datetime] = None

    # ─── Public Status Properties ──────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        """True if the WebSocket connection is currently open."""
        return self._connected

    @property
    def last_message_at(self) -> Optional[datetime]:
        """Timestamp of the last successfully processed message."""
        return self._last_message_at

    @property
    def _stream_url(self) -> str:
        if self.venue == "spot":
            # Spot: aggTrade only (for BTC spot price)
            streams = [f"{self.symbol}@aggTrade"]
            base = BINANCE_SPOT_WSS
        else:
            # Futures: aggTrade + depth + forceOrder (for VPIN + cascades)
            streams = [
                f"{self.symbol}@aggTrade",
                f"{self.symbol}@depth20@100ms",
                f"{self.symbol}@forceOrder",
            ]
            base = BINANCE_FUTURES_WSS
        return f"{base}?streams={'/'.join(streams)}"

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the WebSocket feed with automatic reconnection."""
        self._running = True
        while self._running:
            try:
                await self._connect()
                self._reconnect_delay = 1.0  # reset on successful connection
            except Exception as exc:
                self._connected = False
                log.warning(
                    "binance_ws.disconnected",
                    venue=self.venue,
                    error=str(exc),
                    retry_in=self._reconnect_delay,
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, RECONNECT_DELAY_MAX)

    async def stop(self) -> None:
        """Signal the feed to stop reconnecting."""
        self._running = False
        self._connected = False
        log.info("binance_ws.stopped", venue=self.venue)

    # ─── Internal ─────────────────────────────────────────────────────────────

    async def _connect(self) -> None:
        """Open WebSocket connection and dispatch messages."""
        log.info("binance_ws.connecting", venue=self.venue, url=self._stream_url)
        async with websockets.connect(self._stream_url) as ws:
            self._connected = True
            log.info("binance_ws.connected", venue=self.venue, symbol=self.symbol)
            async for raw in ws:
                if not self._running:
                    break
                try:
                    envelope = json.loads(raw)
                    stream: str = envelope.get("stream", "")
                    data: dict = envelope.get("data", {})
                    await self._dispatch(stream, data)
                    self._last_message_at = datetime.utcnow()
                except Exception as exc:
                    log.error("binance_ws.parse_error", error=str(exc))
        self._connected = False
        log.info("binance_ws.connection_closed", venue=self.venue, symbol=self.symbol)

    async def _dispatch(self, stream: str, data: dict) -> None:
        """Route raw message to the correct typed handler."""
        if "aggTrade" in stream and self._on_trade:
            trade = AggTrade(
                symbol=data["s"],
                price=Decimal(data["p"]),
                quantity=Decimal(data["q"]),
                is_buyer_maker=data["m"],
                trade_time=datetime.utcfromtimestamp(data["T"] / 1000),
            )
            await self._on_trade(trade)

        elif "depth" in stream and self._on_book:
            book = OrderBookSnapshot(
                symbol=data.get("s", self.symbol.upper()),
                bids=[(Decimal(b[0]), Decimal(b[1])) for b in data.get("bids", [])],
                asks=[(Decimal(a[0]), Decimal(a[1])) for a in data.get("asks", [])],
                last_update_id=data.get("lastUpdateId", 0),
                timestamp=datetime.utcnow(),
            )
            await self._on_book(book)

        elif "forceOrder" in stream and self._on_liquidation:
            o = data.get("o", {})
            liq = ForcedLiquidation(
                symbol=o.get("s", ""),
                side=o.get("S", ""),
                price=Decimal(o.get("p", "0")),
                quantity=Decimal(o.get("q", "0")),
                timestamp=datetime.utcfromtimestamp(o.get("T", 0) / 1000),
            )
            await self._on_liquidation(liq)
