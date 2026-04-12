"""Three-source consensus adapter -- composes three MarketFeedPort instances.

Implements :class:`engine.domain.ports.ConsensusPricePort` by fetching
deltas from Chainlink, Tiingo, and Binance in parallel and returning a
:class:`DeltaSet` value object.

This adapter is a direct structural extraction of the inline
consensus-building logic in ``five_min_vpin.py`` lines ~340-480.  The
actual delta computation is delegated to each feed's ``get_window_delta``
method -- this adapter only composes the three calls and packages the
results.

Phase 2 deliverable (CA-02).  Nothing imports this adapter yet.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import structlog

from domain.ports import ConsensusPricePort, MarketFeedPort
from domain.value_objects import DeltaSet

log = structlog.get_logger(__name__)


class ThreeSourceConsensusAdapter(ConsensusPricePort):
    """Composes three :class:`MarketFeedPort` feeds into a :class:`DeltaSet`.

    Parameters
    ----------
    chainlink_feed : MarketFeedPort
        Chainlink on-chain price source (typically ``ChainlinkDbAdapter``).
    tiingo_feed : MarketFeedPort
        Tiingo price source (typically ``TiingoDbAdapter``).
    binance_feed : MarketFeedPort
        Binance live price source (typically ``BinanceWebSocketAdapter``).
    """

    def __init__(
        self,
        chainlink_feed: MarketFeedPort,
        tiingo_feed: MarketFeedPort,
        binance_feed: MarketFeedPort,
    ) -> None:
        self._chainlink = chainlink_feed
        self._tiingo = tiingo_feed
        self._binance = binance_feed
        self._log = log.bind(adapter="three_source_consensus")

    async def get_deltas(
        self,
        asset: str,
        window_ts: int,
        open_price: float,
    ) -> DeltaSet:
        """Fetch deltas from all three sources in parallel.

        Returns a :class:`DeltaSet` with per-source ``Optional[float]``
        entries.  Missing sources are ``None``, not errors.  Each feed's
        ``get_window_delta`` is contractually obligated to swallow its own
        errors and return ``None`` on failure.

        The caller (EvaluateWindowUseCase in Phase 3, or the inline
        strategy logic today) decides the consensus policy -- typically
        requiring at least 2/3 sources with matching sign for the
        SourceAgreementGate to pass.
        """
        results: list[Optional[float]] = await asyncio.gather(
            self._chainlink.get_window_delta(asset, window_ts, open_price),
            self._tiingo.get_window_delta(asset, window_ts, open_price),
            self._binance.get_window_delta(asset, window_ts, open_price),
            return_exceptions=False,
        )

        delta_chainlink = results[0]
        delta_tiingo = results[1]
        delta_binance = results[2]

        sources_present = sum(
            1 for d in (delta_chainlink, delta_tiingo, delta_binance) if d is not None
        )

        self._log.info(
            "consensus.deltas_fetched",
            asset=asset,
            window_ts=window_ts,
            delta_chainlink=f"{delta_chainlink:+.4f}%" if delta_chainlink is not None else "N/A",
            delta_tiingo=f"{delta_tiingo:+.4f}%" if delta_tiingo is not None else "N/A",
            delta_binance=f"{delta_binance:+.4f}%" if delta_binance is not None else "N/A",
            sources_present=sources_present,
        )

        # TODO: TECH_DEBT - construct DeltaSet with actual field values
        # once the VO is fleshed out in Phase 1 value-object work.
        # For now we return a stub DeltaSet; the three delta values are
        # logged above for observability.
        return DeltaSet()
