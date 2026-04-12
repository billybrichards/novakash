"""TimesFM v2.2 adapter -- wraps ``signals.timesfm_v2_client.TimesFMV2Client``.

Thin delegation shim around the existing TimesFM v2.2 calibrated-probability
client.  The adapter accepts the concrete client in its constructor and
delegates all calls -- zero business logic, just protocol conformance and
structured logging.

The v2 client supports two modes:
- **Pull mode** (GET): scorer assembles features from its own caches
- **Push mode** (POST): engine sends features, avoiding train/serve skew

The adapter exposes both through ``get_probability`` (pull) and
``score_with_features`` (push), mirroring the underlying client's API.

No formal ``PredictionPort`` is defined in ``engine/domain/ports.py``
yet.  This adapter will implement that port once it exists.

Phase 2 deliverable (CA-02).  Nothing imports this adapter yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import structlog

if TYPE_CHECKING:
    from signals.timesfm_v2_client import TimesFMV2Client

log = structlog.get_logger(__name__)


class TimesFMV2Adapter:
    """Wraps :class:`TimesFMV2Client` (v2.2 calibrated P(UP) model).

    Parameters
    ----------
    client : TimesFMV2Client
        The concrete v2 probability client, pre-configured with base_url
        and timeout by the composition root.
    """

    def __init__(self, client: "TimesFMV2Client") -> None:
        self._client = client
        self._log = log.bind(adapter="timesfm_v2")

    async def get_probability(
        self,
        asset: str,
        seconds_to_close: int,
        model: str = "oak",
    ) -> dict:
        """Pull-mode: fetch calibrated P(UP) from the scorer.

        Delegates to ``TimesFMV2Client.get_probability``.  The scorer
        assembles features from its own caches (PriceFeed, CoinGlass,
        Gamma).  Used by passive pollers (ELM prediction recorder) where
        the engine has no feature context.

        Returns a dict with ``probability_up``, ``probability_down``,
        ``probability_raw``, ``model_version``, ``delta_bucket``, etc.
        Returns an error dict on failure.
        """
        self._log.debug(
            "timesfm_v2.get_probability",
            asset=asset,
            seconds_to_close=seconds_to_close,
            model=model,
        )
        try:
            result = await self._client.get_probability(
                asset=asset,
                seconds_to_close=seconds_to_close,
                model=model,
            )
            self._log.debug(
                "timesfm_v2.probability_ok",
                p_up=result.get("probability_up"),
                model_version=result.get("model_version"),
            )
            return result
        except Exception as exc:
            self._log.warning(
                "timesfm_v2.get_probability_failed",
                error=str(exc)[:80],
            )
            return {"error": str(exc)[:120], "probability_up": None}

    async def score_with_features(
        self,
        features: object,
        model: str = "oak",
    ) -> dict:
        """Push-mode: send pre-assembled features to the scorer.

        Delegates to ``TimesFMV2Client.score_with_features``.  The engine
        sends the full v5 feature body, avoiding the v4 pull-mode feature
        assembly and its associated train/serve skew.

        The ``features`` parameter should be a ``V5FeatureBody`` instance
        (from ``signals.v2_feature_body``).

        Falls back to pull-mode GET transparently if the scorer does not
        support POST yet (HTTP 404/405/501).
        """
        self._log.debug(
            "timesfm_v2.score_with_features",
            model=model,
        )
        try:
            result = await self._client.score_with_features(
                features=features,
                model=model,
            )
            self._log.debug(
                "timesfm_v2.score_ok",
                p_up=result.get("probability_up"),
                mode=result.get("mode", "unknown"),
            )
            return result
        except Exception as exc:
            self._log.warning(
                "timesfm_v2.score_with_features_failed",
                error=str(exc)[:80],
            )
            return {"error": str(exc)[:120], "probability_up": None}

    async def health(self) -> dict:
        """Delegate to the underlying client's health endpoint.

        Returns the health-check dict from the v2 service, or a dict
        with ``{"status": "error", "error": ...}`` on failure.
        """
        try:
            return await self._client.health()
        except Exception as exc:
            self._log.warning("timesfm_v2.health_failed", error=str(exc)[:80])
            return {"status": "error", "error": str(exc)[:120]}

    async def close(self) -> None:
        """Close the underlying aiohttp session.  Safe to call multiple times."""
        try:
            await self._client.close()
        except Exception as exc:
            self._log.debug("timesfm_v2.close_error", error=str(exc)[:80])
