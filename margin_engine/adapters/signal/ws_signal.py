"""
WebSocket signal adapter — connects to TimesFM v3 composite endpoint.

Receives composite scores at each timescale's cadence and stores the
latest value per timescale. The margin engine reads from this adapter
to make trading decisions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import aiohttp

from margin_engine.domain.ports import SignalPort
from margin_engine.domain.value_objects import CompositeSignal

logger = logging.getLogger(__name__)


class WsSignalAdapter(SignalPort):
    """
    Connects to ws://<timesfm-host>:8080/v3/signal.
    Receives composite scores and stores latest per timescale.
    """

    def __init__(self, url: str = "ws://3.98.114.0:8080/v3/signal") -> None:
        self._url = url
        self._latest: dict[str, CompositeSignal] = {}
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._connected = False

    async def connect(self) -> None:
        """Start the WS receiver loop."""
        self._task = asyncio.create_task(self._receive_loop(), name="signal-ws")
        logger.info("Signal WS adapter connecting to %s", self._url)

    async def disconnect(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        self._connected = False
        logger.info("Signal WS adapter disconnected")

    async def get_latest_signal(self, timescale: str) -> Optional[CompositeSignal]:
        sig = self._latest.get(timescale)
        if sig and (time.time() - sig.timestamp) > 300:
            return None  # stale
        return sig

    async def get_all_signals(self) -> dict[str, CompositeSignal]:
        now = time.time()
        return {
            k: v for k, v in self._latest.items()
            if (now - v.timestamp) < 300
        }

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def _receive_loop(self) -> None:
        """Reconnecting WS receiver loop."""
        backoff = 1.0
        while True:
            try:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=None, sock_read=60),
                )
                self._ws = await self._session.ws_connect(
                    self._url, heartbeat=30,
                )
                self._connected = True
                backoff = 1.0
                logger.info("Signal WS connected to %s", self._url)

                async for msg in self._ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._handle_message(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                        break

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Signal WS error: %s, reconnecting in %.0fs", e, backoff)
            finally:
                self._connected = False
                if self._ws and not self._ws.closed:
                    await self._ws.close()
                if self._session and not self._session.closed:
                    await self._session.close()

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type")
        if msg_type == "composite_score":
            timescale = msg.get("timescale")
            score = msg.get("composite")
            asset = msg.get("asset", "BTC")
            ts = msg.get("ts", time.time())

            if timescale and score is not None:
                try:
                    self._latest[timescale] = CompositeSignal(
                        score=float(score),
                        timescale=timescale,
                        asset=asset,
                        timestamp=float(ts),
                    )
                except ValueError as e:
                    logger.debug("Invalid signal: %s", e)

        elif msg_type == "heartbeat":
            pass  # connection alive
