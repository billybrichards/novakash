"""Unit tests for CollectDenseSignalsUseCase."""
from __future__ import annotations

from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from domain.value_objects import Asset, Timeframe, WindowMarket
from use_cases.collect_dense_signals import CollectDenseSignalsUseCase
from use_cases.ports.market_discovery import MarketDiscoveryPort
from use_cases.ports.price_gateway import PriceGateway


class _FakeClock:
    def __init__(self, t: float):
        self._t = t

    def now(self) -> float:
        return self._t

    def set(self, t: float):
        self._t = t


class _FakePriceGw(PriceGateway):
    async def get_current_price(self, asset):
        return 50000.0

    async def get_window_candle(self, asset, window_ts, tf):
        return None


class _FakeDiscovery(MarketDiscoveryPort):
    async def find_window_market(self, asset, tf, window_ts):
        return WindowMarket(
            condition_id="0x1",
            up_token_id="1",
            down_token_id="2",
            market_slug=f"{asset.symbol.lower()}-updown-{tf.label}-{window_ts}",
        )


@pytest.fixture
def evaluate_uc_mock():
    mock = MagicMock()
    mock.execute = AsyncMock(return_value=None)
    return mock


@pytest.mark.asyncio
async def test_tick_calls_evaluate_window_with_skip_trade_true(evaluate_uc_mock):
    clock = _FakeClock(1776201350.0)  # 50s into a 5m window (window_ts=1776201300)
    uc = CollectDenseSignalsUseCase(
        assets=[Asset("BTC")],
        timeframes=[Timeframe(300)],
        price_gw=_FakePriceGw(),
        discovery=_FakeDiscovery(),
        evaluate_window_uc=evaluate_uc_mock,
        clock=clock,
    )
    await uc.tick()
    assert evaluate_uc_mock.execute.await_count == 1
    _, kwargs = evaluate_uc_mock.execute.call_args
    assert kwargs.get("skip_trade") is True


@pytest.mark.asyncio
async def test_tick_covers_all_asset_tf_pairs(evaluate_uc_mock):
    clock = _FakeClock(1776201350.0)
    uc = CollectDenseSignalsUseCase(
        assets=[Asset("BTC"), Asset("ETH"), Asset("SOL"), Asset("XRP")],
        timeframes=[Timeframe(300), Timeframe(900)],
        price_gw=_FakePriceGw(),
        discovery=_FakeDiscovery(),
        evaluate_window_uc=evaluate_uc_mock,
        clock=clock,
    )
    await uc.tick()
    assert evaluate_uc_mock.execute.await_count == 8  # 4 assets × 2 tfs


@pytest.mark.asyncio
async def test_tick_skips_out_of_range_offset(evaluate_uc_mock):
    # Exactly at window open: elapsed=0, eval_offset=300 → out of [2, 298]
    clock = _FakeClock(1776201300.0)
    uc = CollectDenseSignalsUseCase(
        assets=[Asset("BTC")],
        timeframes=[Timeframe(300)],
        price_gw=_FakePriceGw(),
        discovery=_FakeDiscovery(),
        evaluate_window_uc=evaluate_uc_mock,
        clock=clock,
    )
    await uc.tick()
    assert evaluate_uc_mock.execute.await_count == 0


@pytest.mark.asyncio
async def test_same_offset_not_written_twice_in_one_window(evaluate_uc_mock):
    clock = _FakeClock(1776201350.0)
    uc = CollectDenseSignalsUseCase(
        assets=[Asset("BTC")],
        timeframes=[Timeframe(300)],
        price_gw=_FakePriceGw(),
        discovery=_FakeDiscovery(),
        evaluate_window_uc=evaluate_uc_mock,
        clock=clock,
    )
    await uc.tick()
    await uc.tick()  # same clock → same offset → deduped
    assert evaluate_uc_mock.execute.await_count == 1
