"""
TimesFM v2.2 Calibrated Probability Client

HTTP client for the v2.2 LightGBM direction model running on Montreal EC2.
Returns isotonic-calibrated P(UP) at any seconds_to_close delta bucket.

Endpoints:
  GET  /v2/probability?asset=BTC&seconds_to_close=N
    — legacy pull-mode path. The scorer pulls features from its own
      PriceFeed / CoinGlass / Gamma caches. Used by passive pollers
      (ELM prediction recorder) where the engine has no feature context.

  POST /v2/probability
    — push-mode path for Sequoia v5. The engine already has every v5
      feature in memory at decision time, so it sends them in the
      request body and the scorer uses them directly. Avoids the v4
      pull-mode feature assembly entirely, which is what ships the
      train/serve skew.

Service: http://3.98.114.0:8080 (Montreal, ca-central-1)

Until the scorer side ships POST support, `score_with_features()` will
get HTTP 404 / 405 / 501 on POST and transparently fall back to GET.
The fallback is sticky for the session — once we see "POST unsupported"
we stop trying until the client is re-created (next process restart).

Feature-flagged via V2_EARLY_ENTRY_ENABLED env var.
"""

import asyncio
import json
import os
import time
from typing import Optional

import aiohttp
import structlog

from signals.v2_feature_body import V5FeatureBody

# Use structlog to match the rest of engine/signals/. All warning/info
# calls in this module pass structured kwargs (status=..., error=...,
# body=...) which structlog binds into the log record. The old stdlib
# `logging.getLogger(__name__)` tolerated the format string but
# crashed when kwargs were supplied — a latent bug that never fired in
# production because real 5xx from the scorer is rare. The dual-mode
# fallback tests exercise the 5xx path directly, which surfaced it.
logger = structlog.get_logger(__name__)

_DEFAULT_URL = os.environ.get("TIMESFM_V2_URL", "http://3.98.114.0:8080")
_DEFAULT_TIMEOUT = float(os.environ.get("TIMESFM_V2_TIMEOUT", "5.0"))

# HTTP statuses that mean "the scorer doesn't support POST push-mode
# yet" — these are the only codes that trigger the silent GET fallback.
# Everything else (5xx, timeouts, auth errors) propagates so we don't
# mask real outages.
_POST_UNSUPPORTED_STATUSES: frozenset[int] = frozenset({404, 405, 501})


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
        # Sticky fallback flag: flipped to True the first time a POST
        # push-mode request returns a "POST unsupported" status. While
        # True, `score_with_features()` immediately GETs without even
        # attempting POST. Reset only by creating a new client instance.
        self._push_mode_supported: Optional[bool] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _probability_url(self, model: str) -> str:
        # DUNE uses /v2/probability/cedar endpoint, OAK uses /v2/probability.
        if model in ("cedar", "dune"):
            return f"{self._base_url}/v2/probability/cedar"
        return f"{self._base_url}/v2/probability"

    async def get_probability(
        self, asset: str, seconds_to_close: int, model: str = "oak",
    ) -> dict:
        """
        Get calibrated P(UP) for an asset at a given seconds_to_close.

        PULL-MODE. Prefer `score_with_features()` on decision-path calls;
        this method is kept for passive pollers that have no feature
        context (e.g. the ELM prediction recorder sweep).

        Returns dict with:
            probability_up: float (0-1, calibrated)
            probability_down: float (0-1)
            probability_raw: float (0-1, uncalibrated)
            model_version: str
            delta_bucket: int
            feature_freshness_ms: dict
            timesfm: dict (v1 forecast embedded — v1 forecaster's own metric,
                NOT confidence in the P(UP). Read via
                `v2_feature_body.confidence_from_result()` not directly.)
            confidence: float (0-1, OPTIONAL — top-level confidence in the
                P(UP) call. Present only when the scorer ships the fix.
                Prefer `confidence_from_result()` which handles fallback.)
            timestamp: float
        """
        session = await self._get_session()
        url = self._probability_url(model)
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

    async def score_with_features(
        self,
        asset: str,
        seconds_to_close: int,
        features: V5FeatureBody,
        model: str = "oak",
    ) -> dict:
        """
        PUSH-MODE scoring for Sequoia v5+.

        The engine hands the scorer a fully-populated `V5FeatureBody`;
        the scorer uses it directly in place of its own pull-mode
        feature assembly. This is the only code path that avoids the
        train/serve skew that shipped with v5.

        Fallback contract:
          - First call in a session attempts POST /v2/probability with
            JSON body `{asset, seconds_to_close, features: {...}}`.
          - If the server returns 404/405/501, the client interprets
            that as "scorer hasn't shipped push-mode yet", logs ONCE,
            flips `_push_mode_supported = False`, and immediately
            retries as a GET to the same URL. All subsequent calls in
            the session go straight to GET without trying POST again.
          - Any other error (5xx, timeout, network failure) propagates.
            We do NOT silently fall back from a real outage — that
            would mask production problems.

        Even in fallback mode, the engine-side feature body is still
        useful: call sites can log `features.coverage()` to detect
        broken feature collection independently of whether the scorer
        is using the body yet.

        Args:
            asset: "BTC", "ETH", etc.
            seconds_to_close: seconds until window close.
            features: populated V5FeatureBody. Fields the engine cannot
                supply MUST be left as None (do not default to 0.0).
            model: "oak" (production) or "cedar"/"dune" (staging).

        Returns: same dict shape as `get_probability()`.
        """
        session = await self._get_session()
        url = self._probability_url(model)

        # Fast path: we already learned this session that the scorer
        # doesn't do push-mode, so skip straight to GET.
        if self._push_mode_supported is False:
            return await self.get_probability(asset, seconds_to_close, model)

        body = {
            "asset": asset.upper(),
            "seconds_to_close": int(seconds_to_close),
            "features": features.to_json_dict(),
        }

        try:
            # Perform the POST and fully consume its response inside
            # the `async with` block, so the connection is released
            # back to the pool BEFORE we decide whether to fall back
            # to a GET. Holding the POST response open across a second
            # HTTP call on the same session risks connection starvation
            # under load.
            should_fallback = False
            async with session.post(url, json=body) as resp:
                status = resp.status
                if status == 200:
                    payload = await resp.json()
                    # Success — lock in push-mode for the session.
                    if self._push_mode_supported is None:
                        self._push_mode_supported = True
                        logger.info(
                            "v2.probability.push_mode_active",
                            url=url,
                            feature_coverage=round(features.coverage(), 3),
                        )
                    return payload

                if status in _POST_UNSUPPORTED_STATUSES:
                    # Drain the body (small, no cost) so the connection
                    # can be released cleanly, then downgrade outside
                    # the `async with`.
                    await resp.read()
                    if self._push_mode_supported is None:
                        logger.info(
                            "v2.probability.push_mode_unsupported",
                            status=status,
                            url=url,
                            note="falling back to GET for remainder of session",
                        )
                        self._push_mode_supported = False
                    should_fallback = True
                else:
                    # Any other non-200: propagate. Do NOT downgrade on
                    # 5xx — that would mask real outages.
                    text_body = await resp.text()
                    logger.warning(
                        "v2.probability.push_error",
                        status=status,
                        body=text_body[:120],
                    )
                    raise RuntimeError(
                        f"v2 push API returned {status}: {text_body[:120]}"
                    )

            # `async with` is closed — connection released. Safe to
            # issue the fallback GET now.
            if should_fallback:
                return await self.get_probability(asset, seconds_to_close, model)
        except asyncio.TimeoutError:
            logger.warning(
                "v2.probability.push_timeout",
                asset=asset,
                stc=seconds_to_close,
            )
            raise
        except aiohttp.ClientError as exc:
            logger.warning(
                "v2.probability.push_connection_error",
                error=str(exc)[:80],
            )
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
