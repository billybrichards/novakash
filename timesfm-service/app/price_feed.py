"""
Binance WebSocket BTC price tick collector.

Connects to wss://stream.binance.com:9443/ws/btcusdt@trade,
aggregates prices per second, and maintains a thread-safe rolling buffer.
"""

import asyncio
import json
import logging
import time
from collections import deque
from threading import Lock
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"
BUFFER_SIZE = 2048
RECONNECT_DELAY = 5  # seconds between reconnect attempts


class PriceFeed:
    """
    Thread-safe BTC price feed from Binance WebSocket.

    Aggregates raw trades into 1-second OHLCV buckets (using avg price),
    then stores in a rolling deque of `BUFFER_SIZE` entries.
    """

    def __init__(self, buffer_size: int = BUFFER_SIZE):
        self._buffer: deque[float] = deque(maxlen=buffer_size)
        self._lock = Lock()
        self._connected = False
        self._last_price: Optional[float] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False

        # Accumulate trades within current second
        self._current_second: Optional[int] = None
        self._current_prices: list[float] = []

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def last_price(self) -> Optional[float]:
        return self._last_price

    def get_prices(self, n: Optional[int] = None) -> list[float]:
        """Return a copy of the last n prices (thread-safe)."""
        with self._lock:
            if n is None:
                return list(self._buffer)
            return list(self._buffer)[-n:]

    @property
    def buffer_size(self) -> int:
        with self._lock:
            return len(self._buffer)

    def _flush_current_second(self) -> None:
        """Average prices in current second bucket and push to buffer."""
        if self._current_prices:
            avg_price = sum(self._current_prices) / len(self._current_prices)
            with self._lock:
                self._buffer.append(avg_price)
                self._last_price = avg_price
            self._current_prices = []

    def _handle_trade(self, trade: dict) -> None:
        """Process a single trade message from Binance."""
        try:
            price = float(trade["p"])
            trade_time_ms = int(trade["T"])
            trade_second = trade_time_ms // 1000
        except (KeyError, ValueError) as e:
            logger.debug(f"Malformed trade message: {e}")
            return

        if self._current_second is None:
            self._current_second = trade_second

        if trade_second != self._current_second:
            # New second — flush previous bucket
            self._flush_current_second()
            self._current_second = trade_second

        self._current_prices.append(price)

    async def _connect_and_stream(self) -> None:
        """Main WebSocket loop — connects and processes messages."""
        connector = aiohttp.TCPConnector(ssl=True)
        timeout = aiohttp.ClientTimeout(total=None, sock_read=30)

        try:
            async with aiohttp.ClientSession(
                connector=connector, timeout=timeout
            ) as session:
                logger.info(f"Connecting to Binance WebSocket: {BINANCE_WS_URL}")
                async with session.ws_connect(
                    BINANCE_WS_URL,
                    heartbeat=20,
                    max_msg_size=0,
                ) as ws:
                    self._connected = True
                    logger.info("Binance WebSocket connected.")

                    # Periodic second-flush task
                    flush_task = asyncio.create_task(self._periodic_flush())

                    try:
                        async for msg in ws:
                            if not self._running:
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    data = json.loads(msg.data)
                                    self._handle_trade(data)
                                except json.JSONDecodeError:
                                    logger.debug("Non-JSON message received")
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                logger.error(f"WebSocket error: {ws.exception()}")
                                break
                            elif msg.type == aiohttp.WSMsgType.CLOSED:
                                logger.warning("WebSocket closed by server.")
                                break
                    finally:
                        flush_task.cancel()
                        try:
                            await flush_task
                        except asyncio.CancelledError:
                            pass

        except aiohttp.ClientError as e:
            logger.error(f"WebSocket connection error: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Unexpected error in price feed: {e}", exc_info=True)
        finally:
            self._connected = False

    async def _periodic_flush(self) -> None:
        """Flush the current-second bucket every second to handle quiet markets."""
        while self._running:
            await asyncio.sleep(1)
            now_second = int(time.time())
            if (
                self._current_second is not None
                and self._current_second < now_second
            ):
                self._flush_current_second()
                self._current_second = now_second

    async def start(self) -> None:
        """Start the price feed (reconnects automatically on failure)."""
        self._running = True
        logger.info("Price feed starting...")

        while self._running:
            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                logger.info("Price feed cancelled.")
                break
            except Exception as e:
                logger.error(f"Price feed error: {e}", exc_info=True)

            if self._running:
                logger.info(f"Reconnecting in {RECONNECT_DELAY}s...")
                await asyncio.sleep(RECONNECT_DELAY)

        logger.info("Price feed stopped.")

    async def stop(self) -> None:
        """Stop the price feed gracefully."""
        logger.info("Stopping price feed...")
        self._running = False
        self._connected = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
