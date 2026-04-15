"""
TimesFM Forecast Client — Async HTTP client for the TimesFM microservice.

Connects to the TimesFM BTC forecast service running on a separate instance.
Provides direction (UP/DOWN), confidence, predicted close, quantile spreads.

Usage:
    client = TimesFMClient(base_url="http://3.98.114.0:8000")
    result = await client.get_forecast()
    # result.direction = "UP"
    # result.confidence = 0.82
    # result.predicted_close = 83521.50
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import aiohttp
import structlog

log = structlog.get_logger(__name__)

# Cache TTL — service refreshes every 1s, cache briefly to avoid duplicate calls
_CACHE_TTL_SECONDS = 0.8  # Service refreshes every 1s, cache for 0.8s


@dataclass
class TimesFMForecast:
    """Result from a TimesFM forecast call."""

    direction: str = ""           # "UP" or "DOWN"
    confidence: float = 0.0       # 0-1 (tighter quantile spread = higher)
    predicted_close: float = 0.0  # Final predicted price at horizon end
    spread: float = 0.0           # P90 - P10 at horizon end (uncertainty width)
    horizon: int = 0              # Forecast horizon (steps ahead)
    input_length: int = 0         # Number of input prices used

    # Quantile endpoints (horizon-end values)
    p10: float = 0.0
    p25: float = 0.0
    p50: float = 0.0
    p75: float = 0.0
    p90: float = 0.0

    # Derived: agreement with window open
    delta_vs_open_pct: float = 0.0  # (predicted_close - open) / open * 100
    open_price: float = 0.0         # Window open price (set by caller)

    # Meta
    timestamp: float = 0.0         # When forecast was generated
    fetch_latency_ms: float = 0.0  # How long the HTTP call took
    is_stale: bool = False         # True if using cached/old forecast
    error: str = ""                # Non-empty if forecast failed

    def summary(self) -> str:
        """One-line summary for logging."""
        if self.error:
            return f"TimesFM ERROR: {self.error}"
        return (
            f"TimesFM → {self.direction} (conf={self.confidence:.2f}) | "
            f"Predicted: ${self.predicted_close:,.2f} | "
            f"Spread: ${self.spread:.2f} | "
            f"δ vs open: {self.delta_vs_open_pct:+.4f}% | "
            f"Horizon: {self.horizon} | Latency: {self.fetch_latency_ms:.0f}ms"
        )


class TimesFMClient:
    """
    Async HTTP client for the TimesFM forecast microservice.

    Features:
    - Response caching (avoid hammering service faster than it refreshes)
    - Timeout handling (forecast service can be slow on first call)
    - Error resilience (never crashes the engine)
    """

    def __init__(
        self,
        base_url: str = "http://3.98.114.0:8000",
        timeout_seconds: float = 10.0,
        cache_ttl: float = _CACHE_TTL_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._cache_ttl = cache_ttl
        self._cached: Optional[TimesFMForecast] = None
        self._cached_at: float = 0.0
        self._consecutive_errors: int = 0
        self._log = log.bind(component="timesfm_client")

    async def get_forecast(self, open_price: float = 0.0, seconds_to_close: int = 0) -> TimesFMForecast:
        """
        Fetch a forecast from the TimesFM service.

        Args:
            open_price:       Window open price — used to compute delta_vs_open_pct.
            seconds_to_close: Seconds until the 5-min window closes.
                              If >0, requests a horizon-specific forecast (predicts
                              price at window close, not 60s from now).
                              If 0, returns the background-cached forecast.

        Returns:
            TimesFMForecast with direction, confidence, etc.
            On error, returns a forecast with error field set (never raises).
        """
        now = time.time()

        # Only use cache for default (non-window-specific) requests
        if seconds_to_close <= 0 and self._cached and (now - self._cached_at) < self._cache_ttl:
            result = self._cached
            result.is_stale = False
            if open_price > 0:
                result.open_price = open_price
                result.delta_vs_open_pct = (
                    (result.predicted_close - open_price) / open_price * 100
                )
            return result

        # Fetch forecast — with horizon if window-specific
        t0 = time.time()
        try:
            params = {}
            if seconds_to_close > 0:
                params["horizon"] = max(1, min(seconds_to_close, 600))

            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(f"{self._base_url}/forecast", params=params) as resp:
                    latency_ms = (time.time() - t0) * 1000

                    if resp.status != 200:
                        body = await resp.text()
                        self._consecutive_errors += 1
                        self._log.warning(
                            "timesfm.fetch_error",
                            status=resp.status,
                            body=body[:200],
                            consecutive_errors=self._consecutive_errors,
                        )
                        return self._error_result(
                            f"HTTP {resp.status}: {body[:100]}",
                            latency_ms,
                            open_price,
                        )

                    data = await resp.json()

            # Parse response
            quantiles = data.get("quantiles", {})
            p10_list = quantiles.get("p10", [])
            p25_list = quantiles.get("p25", [])
            p50_list = quantiles.get("p50", [])
            p75_list = quantiles.get("p75", [])
            p90_list = quantiles.get("p90", [])

            predicted_close = float(data.get("predicted_close", 0))
            spread = float(data.get("spread", 0))

            delta_vs_open = 0.0
            if open_price > 0 and predicted_close > 0:
                delta_vs_open = (predicted_close - open_price) / open_price * 100

            # Direction: use window open price if available (more accurate than
            # the service's direction which compares against buffer[0] ~34min ago)
            _svc_direction = data.get("direction", "")
            if open_price > 0 and predicted_close > 0:
                _direction = "UP" if predicted_close > open_price else "DOWN"
            else:
                _direction = _svc_direction

            result = TimesFMForecast(
                direction=_direction,
                confidence=float(data.get("confidence", 0)),
                predicted_close=predicted_close,
                spread=spread,
                horizon=int(data.get("horizon", 0)),
                input_length=int(data.get("input_length", 0)),
                p10=float(p10_list[-1]) if p10_list else 0.0,
                p25=float(p25_list[-1]) if p25_list else 0.0,
                p50=float(p50_list[-1]) if p50_list else 0.0,
                p75=float(p75_list[-1]) if p75_list else 0.0,
                p90=float(p90_list[-1]) if p90_list else 0.0,
                delta_vs_open_pct=delta_vs_open,
                open_price=open_price,
                timestamp=float(data.get("timestamp", time.time())),
                fetch_latency_ms=latency_ms,
                is_stale=False,
                error="",
            )

            # Cache
            self._cached = result
            self._cached_at = time.time()
            self._consecutive_errors = 0

            self._log.info(
                "timesfm.forecast_fetched",
                direction=result.direction,
                confidence=f"{result.confidence:.2f}",
                predicted_close=f"${result.predicted_close:,.2f}",
                spread=f"${result.spread:.2f}",
                latency=f"{latency_ms:.0f}ms",
            )

            return result

        except aiohttp.ClientError as exc:
            latency_ms = (time.time() - t0) * 1000
            self._consecutive_errors += 1
            self._log.warning(
                "timesfm.network_error",
                error=str(exc),
                latency=f"{latency_ms:.0f}ms",
                consecutive_errors=self._consecutive_errors,
            )
            # Return stale cache if available
            if self._cached:
                self._cached.is_stale = True
                if open_price > 0:
                    self._cached.open_price = open_price
                    self._cached.delta_vs_open_pct = (
                        (self._cached.predicted_close - open_price) / open_price * 100
                    )
                return self._cached
            return self._error_result(str(exc), latency_ms, open_price)

        except Exception as exc:
            latency_ms = (time.time() - t0) * 1000
            self._consecutive_errors += 1
            self._log.error(
                "timesfm.unexpected_error",
                error=str(exc),
                consecutive_errors=self._consecutive_errors,
            )
            return self._error_result(str(exc), latency_ms, open_price)

    async def health_check(self) -> dict:
        """Check if the TimesFM service is healthy."""
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(f"{self._base_url}/health") as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return {"status": "error", "http_status": resp.status}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    def _error_result(
        self, error: str, latency_ms: float, open_price: float
    ) -> TimesFMForecast:
        """Build an error result."""
        return TimesFMForecast(
            error=error,
            fetch_latency_ms=latency_ms,
            open_price=open_price,
            timestamp=time.time(),
        )

    @property
    def is_available(self) -> bool:
        """True if we have a recent successful forecast."""
        if self._cached is None:
            return False
        age = time.time() - self._cached_at
        return age < 60.0 and self._consecutive_errors < 5
