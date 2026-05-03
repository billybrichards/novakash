"""
Polymarket RTDS Chainlink Streams Feed — WebSocket

Connects to Polymarket's real-time data service (RTDS) WebSocket and subscribes
to ``crypto_prices_chainlink`` updates. This is the EXACT off-chain Chainlink
Data Streams source Polymarket uses to derive ``priceToBeat`` / ``finalPrice``
on its 5-minute and 15-minute crypto Up/Down markets — sub-second cadence,
canonical resolution oracle.

Why this exists
---------------
The legacy ``ChainlinkFeed`` polls the on-chain Chainlink Aggregator V3 contract
on Polygon every ~5s. That contract is updated only every 10-30s and lags the
off-chain stream by tens of dollars per BTC window. As a result the engine's
``WindowInfo.open_price`` was systematically $7-$33 off from Polymarket's UI
priceToBeat across every BTC 5m window observed today, producing train/serve
skew for any model (e.g. v9.1) trained on priceToBeat-derived deltas.

This feed reads the same off-chain stream Polymarket reads, samples it at each
window boundary, and exposes ``get_window_open_price(asset, window_ts)`` so the
5m / 15m feed can use the canonical value.

Endpoint
--------
``wss://ws-live-data.polymarket.com``  — anonymous (no auth) for crypto topics.

Subscription payload
--------------------
::

    {
      "action": "subscribe",
      "subscriptions": [{
        "topic": "crypto_prices_chainlink",
        "type": "*",
        "filters": "{\"symbol\":\"btc/usd\"}"
      }]
    }

Update message
--------------
::

    {
      "topic":"crypto_prices_chainlink",
      "type":"update",
      "timestamp": <server_recv_ms>,
      "payload": {
        "symbol":"btc/usd",
        "timestamp": <chainlink_streams_ms>,
        "value": 78657.412985,
        "full_accuracy_value": "78657412985000000000000"
      }
    }

The feed sends ``PING`` every 5s as a text frame to keep the connection alive
(per Polymarket RTDS docs).

Defensive design
----------------
If the WebSocket fails to connect or drops:

* ``get_latest_price`` and ``get_window_open_price`` return ``None``.
* Reconnect runs in the background with exponential backoff (capped at 60s).
* No exceptions propagate to the engine's main loop. Callers fall through to
  the existing chainlink_polygon / Binance fallback chain.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

import structlog
import websockets
from websockets.exceptions import ConnectionClosed

log = structlog.get_logger(__name__)

WS_URL = "wss://ws-live-data.polymarket.com"

# Polymarket RTDS symbol convention is lowercase "btc/usd".
# Map engine asset codes → RTDS symbols.
ASSET_TO_SYMBOL: dict[str, str] = {
    "BTC": "btc/usd",
    "ETH": "eth/usd",
    "SOL": "sol/usd",
    "XRP": "xrp/usd",
}
SYMBOL_TO_ASSET: dict[str, str] = {v: k for k, v in ASSET_TO_SYMBOL.items()}

# How close (in seconds) a tick must be to a window boundary to qualify as the
# window-open sample. Polymarket samples right AT the boundary; we accept the
# first tick whose chainlink-streams timestamp is within this many seconds
# AFTER the boundary. 5s is generous given ~1Hz update cadence.
BOUNDARY_TOLERANCE_SECS = 5.0

PING_INTERVAL_SECS = 5.0
RECEIVE_TIMEOUT_SECS = 30.0  # If we receive nothing for this long, reconnect.

# Window durations the engine cares about (seconds).
WINDOW_DURATIONS = (300, 900)

# Cap how many (asset, window_ts) entries we retain. We rarely need more than
# a few minutes of history; this protects against unbounded growth.
MAX_WINDOW_OPENS_PER_ASSET = 64

DEBUG_LOG_TICKS = os.environ.get("RTDS_DEBUG_TICKS", "").lower() in ("1", "true", "yes")


class PolymarketRTDSFeed:
    """Subscribes to Polymarket's Chainlink Streams via RTDS WebSocket.

    Public surface
    --------------
    * ``await start()`` — runs forever; reconnects on disconnect.
    * ``await stop()`` — graceful shutdown.
    * ``get_latest_price(asset)`` — most recent price seen for asset.
    * ``get_window_open_price(asset, window_ts)`` — sampled price at window
      boundary, or ``None`` if not yet captured.
    * ``connected`` / ``last_message_at`` — health introspection.
    """

    def __init__(self, assets: Optional[list[str]] = None) -> None:
        """
        Args:
            assets: list of engine asset codes to subscribe to. Defaults to
                all four supported assets (BTC, ETH, SOL, XRP).
        """
        self._assets = assets or ["BTC", "ETH", "SOL", "XRP"]
        # symbol -> (price, chainlink_streams_timestamp_ms)
        self.latest_prices: dict[str, tuple[float, int]] = {}
        # (asset, window_ts) -> open price sampled at boundary
        self.window_open_prices: dict[tuple[str, int], float] = {}
        # Per-asset bounded ordered list of window_ts we've captured (for trim).
        self._captured_window_ts: dict[str, list[int]] = {a: [] for a in self._assets}

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._connected = False
        self._last_message_at: Optional[datetime] = None
        self._ping_task: Optional[asyncio.Task] = None

        self._log = log.bind(component="PolymarketRTDSFeed", assets=self._assets)

    # ─── Health / status ─────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_message_at(self) -> Optional[datetime]:
        return self._last_message_at

    # ─── Public price accessors ──────────────────────────────────────────────

    def get_latest_price(self, asset: str) -> Optional[float]:
        """Return the most recently observed price for ``asset`` or ``None``."""
        symbol = ASSET_TO_SYMBOL.get(asset.upper())
        if not symbol:
            return None
        entry = self.latest_prices.get(symbol)
        return entry[0] if entry else None

    def get_window_open_price(self, asset: str, window_ts: int) -> Optional[float]:
        """Return the boundary-sampled price for (asset, window_ts) or ``None``.

        Returns ``None`` if no tick within ``BOUNDARY_TOLERANCE_SECS`` of the
        boundary has yet been observed. Caller MUST fall through to the next
        source in the priority chain in that case.
        """
        return self.window_open_prices.get((asset.upper(), int(window_ts)))

    # ─── Lifecycle ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Run forever — connect, subscribe, receive, reconnect on failure."""
        self._running = True
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    WS_URL,
                    open_timeout=10.0,
                    close_timeout=5.0,
                    max_size=2**22,  # 4 MB — initial snapshot can be large
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    self._log.info("rtds.connected", url=WS_URL)
                    backoff = 1.0  # reset backoff on successful connect

                    await self._subscribe(ws)
                    self._ping_task = asyncio.create_task(
                        self._ping_loop(ws), name="rtds:ping"
                    )

                    try:
                        await self._receive_loop(ws)
                    finally:
                        if self._ping_task and not self._ping_task.done():
                            self._ping_task.cancel()
                            try:
                                await self._ping_task
                            except (asyncio.CancelledError, Exception):
                                pass
                        self._ping_task = None

            except asyncio.CancelledError:
                self._log.info("rtds.cancelled")
                self._running = False
                break
            except Exception as exc:  # noqa: BLE001 — defensive top-level catch
                self._log.warning(
                    "rtds.disconnected",
                    error=str(exc)[:200],
                    backoff_secs=backoff,
                )
            finally:
                self._ws = None
                self._connected = False

            if not self._running:
                break

            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                break
            backoff = min(backoff * 2.0, 60.0)

        self._log.info("rtds.stopped")

    async def stop(self) -> None:
        """Stop the feed gracefully."""
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    # ─── WebSocket internals ─────────────────────────────────────────────────

    async def _subscribe(self, ws) -> None:
        """Send the subscription payload for every configured asset."""
        subs = []
        for asset in self._assets:
            symbol = ASSET_TO_SYMBOL.get(asset.upper())
            if not symbol:
                self._log.warning("rtds.unknown_asset", asset=asset)
                continue
            subs.append(
                {
                    "topic": "crypto_prices_chainlink",
                    "type": "*",
                    "filters": json.dumps({"symbol": symbol}),
                }
            )
        if not subs:
            self._log.warning("rtds.no_valid_subscriptions")
            return
        payload = {"action": "subscribe", "subscriptions": subs}
        await ws.send(json.dumps(payload))
        self._log.info("rtds.subscribed", count=len(subs), symbols=[s["filters"] for s in subs])

    async def _ping_loop(self, ws) -> None:
        """Send a literal ``PING`` text frame every 5s (Polymarket convention)."""
        try:
            while self._running:
                await asyncio.sleep(PING_INTERVAL_SECS)
                try:
                    await ws.send("PING")
                except Exception as exc:
                    self._log.debug("rtds.ping_send_failed", error=str(exc)[:120])
                    return
        except asyncio.CancelledError:
            return

    async def _receive_loop(self, ws) -> None:
        """Drain messages, dispatch to ``_handle_message``."""
        while self._running:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=RECEIVE_TIMEOUT_SECS)
            except asyncio.TimeoutError:
                self._log.warning(
                    "rtds.recv_timeout",
                    seconds=RECEIVE_TIMEOUT_SECS,
                )
                return  # triggers reconnect
            except ConnectionClosed as exc:
                self._log.info(
                    "rtds.connection_closed",
                    code=getattr(exc, "code", None),
                    reason=str(getattr(exc, "reason", ""))[:120],
                )
                return
            self._last_message_at = datetime.now(timezone.utc)
            try:
                self._handle_message(raw)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("rtds.handle_error", error=str(exc)[:200])

    # ─── Message handling ────────────────────────────────────────────────────

    def _handle_message(self, raw: str) -> None:
        """Parse a single WS frame and update caches."""
        # Polymarket sometimes responds with bare "PONG" (string) — ignore.
        if not raw or raw == "PONG":
            return
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return

        if not isinstance(msg, dict):
            return

        topic = msg.get("topic")
        if topic != "crypto_prices_chainlink":
            return

        # Initial snapshot: payload.data = [ {timestamp, value}, ... ]
        # Live update:      payload = {symbol, timestamp, value, ...}
        payload = msg.get("payload") or {}
        if not isinstance(payload, dict):
            return

        # Snapshot path — array of historical ticks under "data".
        if isinstance(payload.get("data"), list):
            symbol = payload.get("symbol")
            if not symbol:
                # Snapshot may not include symbol at top level — best effort.
                return
            for entry in payload["data"]:
                if not isinstance(entry, dict):
                    continue
                self._record_tick(
                    symbol=symbol,
                    timestamp_ms=entry.get("timestamp"),
                    value=entry.get("value"),
                )
            return

        # Live update path.
        symbol = payload.get("symbol")
        timestamp_ms = payload.get("timestamp")
        value = payload.get("value")
        self._record_tick(symbol=symbol, timestamp_ms=timestamp_ms, value=value)

    def _record_tick(
        self,
        *,
        symbol: Optional[str],
        timestamp_ms: Optional[int],
        value: Optional[float],
    ) -> None:
        """Update ``latest_prices`` and try to capture window-boundary opens."""
        if symbol is None or timestamp_ms is None or value is None:
            return
        try:
            ts_ms = int(timestamp_ms)
            price = float(value)
        except (TypeError, ValueError):
            return
        if price <= 0:
            return

        asset = SYMBOL_TO_ASSET.get(symbol)
        if asset is None:
            return

        # Always update latest (only if this tick is newer than what we have).
        prev = self.latest_prices.get(symbol)
        if prev is None or ts_ms >= prev[1]:
            self.latest_prices[symbol] = (price, ts_ms)

        if DEBUG_LOG_TICKS:
            self._log.debug(
                "rtds.tick",
                asset=asset,
                price=price,
                ts_ms=ts_ms,
            )

        # Window-boundary capture for every supported window duration.
        ts_secs = ts_ms / 1000.0
        for duration in WINDOW_DURATIONS:
            window_ts = (int(ts_secs) // duration) * duration
            key = (asset, window_ts)
            if key in self.window_open_prices:
                continue  # already captured first sample at this boundary
            offset_from_boundary = ts_secs - window_ts
            if offset_from_boundary < 0:
                # Tick predates the boundary — shouldn't really happen but skip.
                continue
            if offset_from_boundary > BOUNDARY_TOLERANCE_SECS:
                # Past the tolerance window. We've missed the open for this
                # window — leave the entry empty so callers fall through to
                # their next source rather than using a stale mid-window price.
                continue
            self.window_open_prices[key] = price
            self._track_capture(asset, window_ts)
            self._log.info(
                "polymarket_rtds.window_open_captured",
                asset=asset,
                window_ts=window_ts,
                duration=duration,
                price=price,
                offset_secs=round(offset_from_boundary, 3),
            )

    def _track_capture(self, asset: str, window_ts: int) -> None:
        """Bound the in-memory cache; evict oldest if over capacity."""
        captured = self._captured_window_ts.setdefault(asset, [])
        captured.append(window_ts)
        if len(captured) > MAX_WINDOW_OPENS_PER_ASSET:
            # Drop the oldest unique window_ts to bound memory.
            old_ts = captured.pop(0)
            # We may have entries for both 300s and 900s alignment that share
            # the same window_ts — only purge the one that matches; harmless
            # if missing.
            self.window_open_prices.pop((asset, old_ts), None)
