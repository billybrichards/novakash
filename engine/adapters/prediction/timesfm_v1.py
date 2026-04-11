"""TimesFM v1 adapter -- wraps ``signals.timesfm_client.TimesFMClient``.

Thin delegation shim around the existing TimesFM v1 forecast client.
The adapter accepts the concrete client in its constructor and delegates
all calls -- zero business logic, just protocol conformance and
structured logging.

No formal ``PredictionPort`` is defined in ``engine/domain/ports.py``
yet.  This adapter will implement that port once it exists.  Until then,
it serves as a structural boundary that the composition root can wire.

Phase 2 deliverable (CA-02).  Nothing imports this adapter yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import structlog

if TYPE_CHECKING:
    from engine.signals.timesfm_client import TimesFMClient, TimesFMForecast

log = structlog.get_logger(__name__)


class TimesFMV1Adapter:
    """Wraps :class:`TimesFMClient` (v1 direction/confidence model).

    Parameters
    ----------
    client : TimesFMClient
        The concrete v1 forecast client, pre-configured with base_url
        and timeout by the composition root.
    """

    def __init__(self, client: "TimesFMClient") -> None:
        self._client = client
        self._log = log.bind(adapter="timesfm_v1")

    async def get_forecast(
        self,
        open_price: float = 0.0,
        seconds_to_close: int = 0,
    ) -> "TimesFMForecast":
        """Fetch a v1 direction forecast.

        Delegates to ``TimesFMClient.get_forecast``.  The returned
        ``TimesFMForecast`` dataclass includes direction, confidence,
        predicted_close, spread, and quantile endpoints.

        This method never raises -- the underlying client swallows
        errors and returns a ``TimesFMForecast`` with ``error`` set.
        """
        self._log.debug(
            "timesfm_v1.get_forecast",
            open_price=f"${open_price:,.2f}" if open_price else "N/A",
            seconds_to_close=seconds_to_close,
        )
        result = await self._client.get_forecast(
            open_price=open_price,
            seconds_to_close=seconds_to_close,
        )
        if result.error:
            self._log.warning(
                "timesfm_v1.forecast_error",
                error=result.error[:80],
            )
        else:
            self._log.debug(
                "timesfm_v1.forecast_ok",
                direction=result.direction,
                confidence=f"{result.confidence:.2f}",
                latency_ms=f"{result.fetch_latency_ms:.0f}",
            )
        return result

    async def health_check(self) -> dict:
        """Delegate to the underlying client's health check.

        Returns the health-check dict from the v1 service, or a dict
        with ``{"status": "error", "error": ...}`` on failure.
        """
        try:
            return await self._client.health_check()
        except Exception as exc:
            self._log.warning("timesfm_v1.health_check_failed", error=str(exc)[:80])
            return {"status": "error", "error": str(exc)[:120]}
