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
            params = {"asset": asset, "timescale": timescale, "strategy": "polymarket_5m"}
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

            return self._parse(data, timescale=timescale)
        except Exception as exc:
            log.warning("v4_snapshot.fetch_error", error=str(exc)[:200])
            return None

    @staticmethod
    def _parse(data: dict, timescale: str = "5m") -> Optional[V4Snapshot]:
        """Parse JSON response into V4Snapshot VO.

        The V4 /v4/snapshot response nests per-timescale data under
        data["timescales"]["5m"] etc.  We extract the requested timescale
        and merge top-level fields (consensus, macro, ts).
        """
        try:
            # V4 response structure: {asset, ts, consensus, macro, timescales: {5m: {...}, 15m: {...}}}
            ts_data = data.get("timescales", {}).get(timescale)
            if ts_data is None:
                log.warning("v4_snapshot.timescale_missing", timescale=timescale, available=list(data.get("timescales", {}).keys()))
                return None

            # Extract recommended_action from the timescale block
            rec = ts_data.get("recommended_action") or {}

            # probability_up lives in the timescale block
            p_up = ts_data.get("probability_up")
            if p_up is None:
                log.warning("v4_snapshot.no_probability", timescale=timescale)
                return None

            return V4Snapshot(
                probability_up=float(p_up),
                conviction=ts_data.get("conviction", "NONE"),
                conviction_score=float(ts_data.get("conviction_score", 0.0)),
                regime=ts_data.get("regime", "chop"),
                regime_confidence=float(ts_data.get("regime_confidence", 0.0)),
                regime_persistence=float(ts_data.get("regime_persistence", 0.0)),
                regime_transition=ts_data.get("regime_transition"),
                recommended_side=rec.get("side"),
                recommended_collateral_pct=rec.get("collateral_pct"),
                recommended_sl_pct=rec.get("sl_pct"),
                recommended_tp_pct=rec.get("tp_pct"),
                recommended_reason=rec.get("reason"),
                recommended_conviction_score=rec.get("conviction_score"),
                sub_signals=ts_data.get("sub_signals", {}),
                consensus=data.get("consensus", {}),  # top-level
                macro=data.get("macro", {}),            # top-level
                quantiles=ts_data.get("quantiles_at_close") or ts_data.get("quantiles_full", {}),
                timescale=timescale,
                timestamp=float(data.get("ts", 0.0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("v4_snapshot.parse_error", error=str(exc)[:200])
            return None
