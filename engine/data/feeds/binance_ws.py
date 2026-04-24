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
import os
import time
from datetime import datetime
from decimal import Decimal
from typing import Callable, Awaitable, Optional
import aiohttp
import websockets
import structlog

from data.models import AggTrade, OrderBookSnapshot, ForcedLiquidation

log = structlog.get_logger(__name__)

BINANCE_SPOT_WSS = "wss://stream.binance.com:9443/stream"
BINANCE_FUTURES_WSS = "wss://fstream.binance.com/stream"
RECONNECT_DELAY_MAX = 60  # seconds

# REST fallback: poll aggTrades when WS is dead. Kicks in after
# REST_FALLBACK_AFTER_S seconds without WS data.
BINANCE_SPOT_REST = "https://api.binance.com/api/v3/aggTrades"
BINANCE_FUTURES_REST = "https://fapi.binance.com/fapi/v1/aggTrades"
REST_FALLBACK_AFTER_S = float(os.environ.get("BINANCE_REST_FALLBACK_AFTER", "10"))
REST_POLL_INTERVAL_S = float(os.environ.get("BINANCE_REST_POLL_INTERVAL", "2"))


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
        # 2026-04-23: The combined /stream?streams= endpoint silently
        # drops data with websockets 15.x (compression negotiation bug).
        # Single /ws/<stream> path works reliably with compression=None.
        stream_name = f"{self.symbol}@aggTrade"
        if self.venue == "spot":
            return f"wss://stream.binance.com:9443/ws/{stream_name}"
        else:
            return f"wss://fstream.binance.com/ws/{stream_name}"

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the WebSocket feed with automatic reconnection.

        Also starts a REST polling fallback that kicks in when WS is
        stale for >10 seconds. REST polls aggTrades every 2s — enough
        for VPIN bucket accumulation.
        """
        self._running = True
        # Start REST fallback alongside WS
        asyncio.create_task(self._rest_poll_loop(), name=f"binance_rest_{self.venue}")
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

    # ─── REST fallback ────────────────────────────────────────────────────────

    async def _rest_poll_loop(self) -> None:
        """Poll aggTrades via REST when WS is stale. Runs alongside WS loop."""
        rest_url = BINANCE_SPOT_REST if self.venue == "spot" else BINANCE_FUTURES_REST
        last_trade_id: int = 0
        _logged_fallback = False

        while self._running:
            await asyncio.sleep(REST_POLL_INTERVAL_S)
            # Only poll when WS hasn't delivered data recently
            if self._last_message_at is not None:
                age = (datetime.utcnow() - self._last_message_at).total_seconds()
                if age < REST_FALLBACK_AFTER_S:
                    if _logged_fallback:
                        log.info("binance_rest.ws_recovered", venue=self.venue)
                        _logged_fallback = False
                    continue

            if not _logged_fallback:
                log.warning(
                    "binance_rest.fallback_active",
                    venue=self.venue,
                    ws_age=round((datetime.utcnow() - self._last_message_at).total_seconds(), 1)
                    if self._last_message_at else "never",
                )
                _logged_fallback = True

            try:
                params = f"?symbol=BTCUSDT&limit=100"
                if last_trade_id > 0:
                    params += f"&fromId={last_trade_id + 1}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(rest_url + params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        trades = await resp.json()
                for t in trades:
                    tid = int(t["a"])
                    if tid <= last_trade_id:
                        continue
                    last_trade_id = tid
                    data = {
                        "s": t.get("s", "BTCUSDT"),
                        "p": t["p"],
                        "q": t["q"],
                        "m": t["m"],
                        "T": t["T"],
                    }
                    await self._dispatch(f"{self.symbol}@aggTrade", data)
                    self._last_message_at = datetime.utcnow()
            except Exception as exc:
                log.debug("binance_rest.poll_error", venue=self.venue, error=str(exc)[:100])

    # ─── Internal ─────────────────────────────────────────────────────────────

    async def _connect(self) -> None:
        """Open WebSocket connection and dispatch messages."""
        log.info("binance_ws.connecting", venue=self.venue, url=self._stream_url)
        # compression=None: websockets 15.x enables permessage-deflate by
        # default; Binance's server accepts the Upgrade but then silently
        # stops sending frames, causing an indefinite hang.  Disabling
        # compression restores data flow (verified 2026-04-23).
        async with websockets.connect(self._stream_url, compression=None) as ws:
            self._connected = True
            log.info("binance_ws.connected", venue=self.venue, symbol=self.symbol)
            async for raw in ws:
                if not self._running:
                    break
                try:
                    envelope = json.loads(raw)
                    # Combined stream: {"stream": "...", "data": {...}}
                    # Single stream:   {"e": "aggTrade", "p": "...", ...}
                    if "stream" in envelope:
                        stream = envelope["stream"]
                        data = envelope.get("data", {})
                    else:
                        # Single /ws/ stream — infer stream name from event type
                        etype = envelope.get("e", "")
                        stream = f"{self.symbol}@{etype}" if etype else ""
                        data = envelope
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
