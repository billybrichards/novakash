"""V4SnapshotHttpAdapter -- HTTP client for /v4/snapshot.

Implements V4SnapshotPort by fetching from the timesfm service.
Timeout: 5s.  Returns None on any error.

Audit: SP-03.
"""
from __future__ import annotations

import os
from typing import Optional

import structlog

from domain.value_objects import V4Snapshot

log = structlog.get_logger(__name__)

_TIMESFM_URL = os.environ.get("TIMESFM_URL", "http://localhost:8001")
_TIMEOUT_S = 5


class V4SnapshotHttpAdapter:
    """V4SnapshotPort implementation -- HTTP client for /v4/snapshot."""

    @property
    def base_url(self) -> str:
        return _TIMESFM_URL

    async def get_snapshot(
        self,
        asset: str,
        timescale: str,
    ) -> Optional[V4Snapshot]:
        """Fetch the latest V4 snapshot for (asset, timescale).

        Returns None on timeout, HTTP error, or missing data.
        MUST NOT raise.
        """
        try:
            import aiohttp

            url = f"{self.base_url}/v4/snapshot"
            params = {"asset": asset, "timescale": timescale}
            timeout = aiohttp.ClientTimeout(total=_TIMEOUT_S)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params=params) as resp:
                    if resp.status != 200:
                        log.warning(
                            "v4_snapshot.http_error",
                            status=resp.status,
                            asset=asset,
                            timescale=timescale,
                        )
                        return None
                    data = await resp.json()

            return self._parse(data)
        except Exception as exc:
            log.warning("v4_snapshot.fetch_error", error=str(exc)[:200])
            return None

    @staticmethod
    def _parse(data: dict) -> Optional[V4Snapshot]:
        """Parse JSON response into V4Snapshot VO."""
        try:
            # Extract recommended_action sub-dict
            rec = data.get("recommended_action") or {}
            return V4Snapshot(
                probability_up=float(data["probability_up"]),
                conviction=data.get("conviction", "NONE"),
                conviction_score=float(data.get("conviction_score", 0.0)),
                regime=data.get("regime", "chop"),
                regime_confidence=float(data.get("regime_confidence", 0.0)),
                regime_persistence=float(data.get("regime_persistence", 0.0)),
                regime_transition=data.get("regime_transition"),
                recommended_side=rec.get("side"),
                recommended_collateral_pct=rec.get("collateral_pct"),
                recommended_sl_pct=rec.get("sl_pct"),
                recommended_tp_pct=rec.get("tp_pct"),
                recommended_reason=rec.get("reason"),
                recommended_conviction_score=rec.get("conviction_score"),
                sub_signals=data.get("sub_signals", {}),
                consensus=data.get("consensus", {}),
                macro=data.get("macro", {}),
                quantiles=data.get("quantiles", {}),
                timescale=data.get("timescale", "5m"),
                timestamp=float(data.get("timestamp", 0.0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("v4_snapshot.parse_error", error=str(exc)[:200])
            return None
