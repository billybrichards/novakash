"""
Polymarket CLOB WebSocket Feed

Subscribes to the Polymarket CLOB WebSocket for real-time order book
updates on BTC-related markets.

Used by:
  - ArbScanner: to detect sub-$1 YES+NO price inefficiencies
  - VPINScanner: to compute prediction market flow toxicity
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from decimal import Decimal
from typing import Callable, Awaitable, Optional
import websockets
import structlog

from data.models import PolymarketOrderBook

log = structlog.get_logger(__name__)

POLYMARKET_WSS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RECONNECT_DELAY_MAX = 60


class PolymarketWebSocketFeed:
    """
    Connects to Polymarket CLOB WebSocket and emits order book snapshots.

    Subscribes to a list of market token IDs (YES tokens; NO inferred
    from the market complement).

    Attributes:
        connected: True while the WebSocket connection is open.
        last_message_at: Timestamp of the most recently processed message.
    """

    def __init__(
        self,
        token_ids: list[str],
        on_book: Callable[[PolymarketOrderBook], Awaitable[None]] | None = None,
    ) -> None:
        self.token_ids = token_ids
        self._on_book = on_book
        self._running = False
        self._connected = False
        self._last_message_at: Optional[datetime] = None
        self._reconnect_delay = 1.0
        # Map token_id -> market_slug for context
        self._token_to_slug: dict[str, str] = {}

    # ─── Public Status Properties ──────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        """True if the WebSocket connection is currently open."""
        return self._connected

    @property
    def last_message_at(self) -> Optional[datetime]:
        """Timestamp of the last successfully processed message."""
        return self._last_message_at

    def set_market_map(self, token_to_slug: dict[str, str]) -> None:
        """Provide token → market slug mapping."""
        self._token_to_slug = token_to_slug

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start WebSocket feed with automatic reconnect."""
        self._running = True
        while self._running:
            try:
                await self._connect()
                self._reconnect_delay = 1.0
            except Exception as exc:
                self._connected = False
                log.warning(
                    "polymarket_ws.disconnected",
                    error=str(exc),
                    retry_in=self._reconnect_delay,
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, RECONNECT_DELAY_MAX)

    async def stop(self) -> None:
        self._running = False
        self._connected = False
        log.info("polymarket_ws.stopped")

    # ─── Internal ─────────────────────────────────────────────────────────────

    async def _connect(self) -> None:
        """Open connection and handle subscription + message loop."""
        log.info("polymarket_ws.connecting")
        async with websockets.connect(POLYMARKET_WSS) as ws:
            # Subscribe to order books for all tracked tokens
            sub_msg = {
                "type": "subscribe",
                "channel": "market",
                "markets": self.token_ids,
            }
            await ws.send(json.dumps(sub_msg))
            self._connected = True
            log.info("polymarket_ws.subscribed", markets=len(self.token_ids))

            async for raw in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw)
                    await self._handle(msg)
                    self._last_message_at = datetime.utcnow()
                except Exception as exc:
                    log.error("polymarket_ws.parse_error", error=str(exc))

        self._connected = False
        log.info("polymarket_ws.connection_closed")

    async def _handle(self, msg: dict) -> None:
        """Parse order book message and emit PolymarketOrderBook."""
        event_type = msg.get("event_type", "")
        if event_type not in ("book", "price_change"):
            return

        token_id = msg.get("asset_id", "")
        market_slug = self._token_to_slug.get(token_id, token_id)

        def _parse_levels(levels: list[list]) -> list[tuple[Decimal, Decimal]]:
            return [(Decimal(p), Decimal(s)) for p, s in levels]

        book = PolymarketOrderBook(
            market_slug=market_slug,
            token_id=token_id,
            yes_bids=_parse_levels(msg.get("bids", [])),
            yes_asks=_parse_levels(msg.get("asks", [])),
            no_bids=[],   # Separate subscription for NO token
            no_asks=[],
            timestamp=datetime.utcnow(),
        )

        if self._on_book:
            await self._on_book(book)
