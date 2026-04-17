"""
HTTP probability adapter — polls TimesFM /v2/probability/15m.

This is the ML-directed signal source for the v2 margin strategy. It replaces
the v3 composite as the direction signal. The composite is retained by the
main loop as a regime/volatility filter, not a direction signal.

Why poll instead of WebSocket:
  The /v2/probability endpoint is a stateless REST endpoint — you ask for
  a specific (asset, timescale, seconds_to_close) tuple and get back the
  model's prediction at that exact horizon. There is no push channel
  because predictions are computed on demand. Polling every 30s gives us
  30 predictions per 15-minute window, which is more than enough to catch
  any actionable signal as it crosses our conviction threshold.

Freshness guarantee:
  get_latest() returns None if the cached value is older than
  freshness_seconds. Better to skip a tick than to trade on a prediction
  made 5 minutes ago — BTC can move 10+ bps in that time, large enough
  to have changed the correct action.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import aiohttp

from margin_engine.domain.ports import ProbabilityPort
from margin_engine.domain.value_objects import ProbabilitySignal

logger = logging.getLogger(__name__)


class ProbabilityHttpAdapter(ProbabilityPort):
    """
    Polls /v2/probability/15m on a fixed cadence and caches the latest
    response per (asset, timescale) key.

    Cadence: every poll_interval_s (default 30s), request a fresh prediction
    from the TimesFM service. Store it with a wall-clock timestamp.

    Staleness: get_latest() returns None if the cached value is older than
    freshness_seconds. The strategy should treat a None return as "no
    actionable data" and skip trading this tick.

    Failure mode: on HTTP error or timeout we log and keep the previous
    cached value (which will expire on its own freshness clock). We do NOT
    retry aggressively — the next scheduled poll will try again. Trading
    on stale predictions during an upstream outage is exactly the kind of
    silent-failure trap the new strategy is designed to avoid.
    """

    def __init__(
        self,
        base_url: str = "http://16.52.14.182:8080",
        asset: str = "BTC",
        timescale: str = "15m",
        seconds_to_close: int = 480,
        poll_interval_s: float = 30.0,
        freshness_seconds: float = 120.0,
        request_timeout_s: float = 8.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._asset = asset
        self._timescale = timescale
        self._seconds_to_close = seconds_to_close
        self._poll_interval = poll_interval_s
        self._freshness = freshness_seconds
        self._request_timeout = request_timeout_s

        self._latest: Optional[ProbabilitySignal] = None
        self._latest_received_at: float = 0.0
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def connect(self) -> None:
        """Start the background polling task."""
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._request_timeout),
        )
        self._task = asyncio.create_task(self._poll_loop(), name="prob-http-poll")
        logger.info(
            "Probability HTTP adapter started: %s %s seconds_to_close=%d, "
            "poll=%.0fs, freshness=%.0fs",
            self._base_url, self._timescale, self._seconds_to_close,
            self._poll_interval, self._freshness,
        )

    async def disconnect(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("Probability HTTP adapter stopped")

    async def get_latest(
        self,
        asset: str = "BTC",
        timescale: str = "15m",
    ) -> Optional[ProbabilitySignal]:
        """
        Return the most recent prediction if it's still fresh.

        We compare against the receive time rather than the model's own
        timestamp because clock skew between the engine box and the
        TimesFM box could otherwise mask a real staleness problem.
        """
        if self._latest is None:
            return None
        if (time.time() - self._latest_received_at) > self._freshness:
            return None
        if asset.upper() != self._latest.asset.upper():
            return None
        if timescale != self._latest.timescale:
            return None
        return self._latest

    async def force_refresh(
        self,
        asset: str = "BTC",
        timescale: str = "15m",
    ) -> Optional[ProbabilitySignal]:
        """
        Bypass the poll cadence and fire an immediate HTTP call.

        Used by the v2-fallback continuation path in ManagePositionsUseCase
        when the v4 snapshot is unavailable and we need a fresh prediction
        for the NEW 15m window rather than the cached value from the
        previous window. The standard 30s polling cadence would leave us
        reading data that describes a window that already closed.

        Fails soft — returns None on any error so the caller can treat
        that as "exit the position safely".
        """
        try:
            await self._poll_once()
        except Exception as e:
            logger.warning(
                "force_refresh: immediate poll failed: %s", e,
            )
            return None
        return await self.get_latest(asset=asset, timescale=timescale)

    async def _poll_loop(self) -> None:
        """Fetch a prediction on a fixed cadence until cancelled."""
        while not self._stop.is_set():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Probability poll failed: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                continue
            if self._stop.is_set():
                break

    async def _poll_once(self) -> None:
        """Single HTTP round-trip to /v2/probability/{timescale}."""
        if self._session is None:
            return

        path = f"/v2/probability/{self._timescale}" if self._timescale == "15m" else "/v2/probability"
        url = f"{self._base_url}{path}"
        params = {
            "asset": self._asset,
            "seconds_to_close": self._seconds_to_close,
        }

        async with self._session.get(url, params=params) as resp:
            if resp.status != 200:
                logger.warning(
                    "Probability poll %s returned HTTP %d", url, resp.status,
                )
                return
            payload = await resp.json()

        # Expected fields per app/v2_routes.py V2ProbabilityResponse:
        # asset, seconds_to_close, delta_bucket, probability_up,
        # probability_down, probability_raw, model_version,
        # feature_freshness_ms, timesfm, timestamp
        try:
            signal = ProbabilitySignal(
                probability_up=float(payload["probability_up"]),
                asset=payload.get("asset", self._asset).upper(),
                timescale=self._timescale,
                seconds_to_close=int(payload["seconds_to_close"]),
                # Window boundaries: derived from the API response's timestamp
                # and seconds_to_close, since v2/probability doesn't return
                # window_close_ts explicitly on the 15m variant. The model's
                # `timestamp` field is the wall-clock time of the prediction;
                # seconds_to_close is the time from then until window close.
                window_open_ts=int(payload.get("timestamp", time.time()))
                - (900 - int(payload["seconds_to_close"])),
                window_close_ts=int(payload.get("timestamp", time.time()))
                + int(payload["seconds_to_close"]),
                model_version=str(payload.get("model_version", "unknown")),
                timestamp=float(payload.get("timestamp", time.time())),
            )
        except (KeyError, ValueError, TypeError) as e:
            logger.warning("Probability payload parse failed: %s: %s", e, payload)
            return

        self._latest = signal
        self._latest_received_at = time.time()
        logger.info(
            "Probability: %s %s p_up=%.3f conviction=%.3f "
            "seconds_to_close=%d model=%s",
            signal.asset, signal.timescale, signal.probability_up,
            signal.conviction, signal.seconds_to_close,
            signal.model_version[:40],
        )
