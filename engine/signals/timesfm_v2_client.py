"""
TimesFM v2.2 Calibrated Probability Client

HTTP client for the v2.2 LightGBM direction model running on Montreal EC2.
Returns isotonic-calibrated P(UP) at any seconds_to_close delta bucket.

Endpoint: GET /v2/probability?asset=BTC&seconds_to_close=N
Service:  http://3.98.114.0:8080 (Montreal, ca-central-1)

Feature-flagged via V2_EARLY_ENTRY_ENABLED env var.
"""

import asyncio
import logging
import os
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_DEFAULT_URL = os.environ.get("TIMESFM_V2_URL", "http://3.98.114.0:8080")
_DEFAULT_TIMEOUT = float(os.environ.get("TIMESFM_V2_TIMEOUT", "5.0"))


class TimesFMV2Client:
    """Client for TimesFM v2.2 calibrated probability API."""

    def __init__(
        self,
        base_url: str = _DEFAULT_URL,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_health: Optional[dict] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def get_probability(self, asset: str, seconds_to_close: int) -> dict:
        """
        Get calibrated P(UP) for an asset at a given seconds_to_close.

        Returns dict with:
            probability_up: float (0-1, calibrated)
            probability_raw: float (0-1, uncalibrated)
            model_version: str
            delta_bucket: int
            feature_freshness_ms: dict
            timesfm: dict (v1 forecast embedded)
            timestamp: float
        """
        session = await self._get_session()
        url = f"{self._base_url}/v2/probability"
        params = {"asset": asset.upper(), "seconds_to_close": seconds_to_close}

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("v2.probability.error", status=resp.status, body=body[:100])
                    raise RuntimeError(f"v2 API returned {resp.status}: {body[:100]}")
                return await resp.json()
        except asyncio.TimeoutError:
            logger.warning("v2.probability.timeout", asset=asset, stc=seconds_to_close)
            raise
        except aiohttp.ClientError as exc:
            logger.warning("v2.probability.connection_error", error=str(exc)[:80])
            raise

    async def health(self) -> dict:
        """GET /v2/health — check model status."""
        session = await self._get_session()
        try:
            async with session.get(f"{self._base_url}/v2/health") as resp:
                self._last_health = await resp.json()
                return self._last_health
        except Exception as exc:
            logger.warning("v2.health.error", error=str(exc)[:80])
            return {"status": "down", "error": str(exc)[:80]}

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
