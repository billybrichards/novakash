"""CollectDenseSignalsUseCase — shadow-only dense signal_evaluations writer.

Called every 2s. For each configured (asset, timeframe) pair:
    1. Compute current window_ts (floor of now / duration_secs).
    2. Compute elapsed = now - window_ts and eval_offset = duration - elapsed.
    3. If eval_offset in [2, duration-2] and (asset, window_ts, offset) not
       yet written in-process: invoke evaluate_window_uc.execute(..., skip_trade=True).

The use case NEVER calls order_manager or trade path. Shadow-only by
construction (enforced by ``skip_trade=True`` and test coverage).

Spec: docs/superpowers/specs/2026-04-15-dense-multi-asset-signals-design.md
"""
from __future__ import annotations

from typing import Any

import structlog

from domain.value_objects import Asset, Timeframe, WindowMarket
from use_cases.ports.clock import Clock
from use_cases.ports.market_discovery import MarketDiscoveryPort
from use_cases.ports.price_gateway import PriceGateway

log = structlog.get_logger(__name__)


class CollectDenseSignalsUseCase:
    """Shadow-only dense signal_evaluations writer."""

    def __init__(
        self,
        assets: list[Asset],
        timeframes: list[Timeframe],
        price_gw: PriceGateway,
        discovery: MarketDiscoveryPort,
        evaluate_window_uc: Any,  # EvaluateWindowUseCase — Any avoids circular import
        clock: Clock,
    ) -> None:
        self._assets = assets
        self._timeframes = timeframes
        self._price_gw = price_gw
        self._discovery = discovery
        self._eval_uc = evaluate_window_uc
        self._clock = clock
        self._written: set[tuple[str, int, str, int]] = set()  # (asset, ts, tf, offset)
        self._market_cache: dict[tuple[str, str, int], WindowMarket] = {}

    async def tick(self) -> None:
        now = self._clock.now()
        for asset in self._assets:
            for tf in self._timeframes:
                await self._maybe_write(asset, tf, now)

    async def _maybe_write(self, asset: Asset, tf: Timeframe, now: float) -> None:
        duration = tf.duration_secs
        window_ts = (int(now) // duration) * duration
        elapsed = int(now) - window_ts
        eval_offset = duration - elapsed
        if not (2 <= eval_offset <= duration - 2):
            return

        dedupe_key = (asset.symbol, window_ts, tf.label, eval_offset)
        if dedupe_key in self._written:
            return

        cache_key = (asset.symbol, tf.label, window_ts)
        market = self._market_cache.get(cache_key)
        if market is None:
            market = await self._discovery.find_window_market(asset, tf, window_ts)
            if market is None:
                log.debug(
                    "dense.no_market",
                    asset=asset.symbol, tf=tf.label, window_ts=window_ts,
                )
                return
            self._market_cache[cache_key] = market

        try:
            window = _DenseWindowAdapter(
                asset=asset.symbol,
                window_ts=window_ts,
                duration_secs=duration,
                up_token_id=market.up_token_id,
                down_token_id=market.down_token_id,
                eval_offset=eval_offset,
            )
            state = await self._build_market_state(asset)
            await self._eval_uc.execute(window, state, skip_trade=True)
            self._written.add(dedupe_key)
        except Exception as exc:
            log.warning(
                "dense.eval_failed",
                asset=asset.symbol, tf=tf.label, window_ts=window_ts,
                error=str(exc)[:200],
            )

    async def _build_market_state(self, asset: Asset) -> Any:
        """Minimal MarketState with current price for the asset."""
        from data.models import MarketState  # lazy import — avoids circular

        price = await self._price_gw.get_current_price(asset)
        if asset.symbol == "BTC":
            return MarketState(btc_price=price)
        return MarketState(btc_price=None)


class _DenseWindowAdapter:
    """Duck-typed stand-in for WindowInfo.

    Exposes only the attributes EvaluateWindowUseCase reads. Avoids importing
    WindowInfo from data.feeds (data layer) into the use case layer.
    """

    def __init__(
        self,
        asset: str,
        window_ts: int,
        duration_secs: int,
        up_token_id: str,
        down_token_id: str,
        eval_offset: int,
    ) -> None:
        self.asset = asset
        self.window_ts = window_ts
        self.duration_secs = duration_secs
        self.up_token_id = up_token_id
        self.down_token_id = down_token_id
        self.eval_offset = eval_offset
        self.open_price: float | None = None
        self.current_price: float | None = None
        self.up_price: float | None = None
        self.down_price: float | None = None
        self.price_source: str = "dense_collector"
