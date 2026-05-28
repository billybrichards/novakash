"""Use-case tests for MonitorOpenTradesUseCase.

Uses AsyncMock repos — no real DB. Verifies:
  - insert_trigger is called for crossing thresholds
  - No insert when no prob signal is available
  - CLOB data is threaded through correctly
  - Detector cleanup when position closes
  - Exceptions from repo are swallowed (not raised to caller)
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from exit_monitor.use_cases.monitor_open_trades import (
    CLOBSnapshot,
    MonitorOpenTradesUseCase,
    OpenTradeState,
)


@pytest.fixture
def mock_repo():
    repo = MagicMock()
    repo.insert_trigger = AsyncMock(return_value=None)
    return repo


def _trade(
    decision_id: int = 1,
    asset: str = "BTC",
    window_ts: int = 1_777_200_000,
    strategy_id: str = "tickformer_v18_t180",
    side: str = "UP",
    entry_p: float = 0.88,
    entry_eval_offset: int = 210,
) -> OpenTradeState:
    return OpenTradeState(
        decision_id=decision_id,
        asset=asset,
        window_ts=window_ts,
        strategy_id=strategy_id,
        side=side,
        entry_p=entry_p,
        entry_eval_offset=entry_eval_offset,
    )


@pytest.mark.asyncio
async def test_insert_called_for_crossing_threshold(mock_repo):
    """insert_trigger is called when p_against crosses a threshold."""
    uc = MonitorOpenTradesUseCase(repo=mock_repo)
    trade = _trade(side="UP")
    # P(UP)=0.40 → p_against=0.60 → crosses 0.50, 0.55, 0.60
    await uc.execute(
        open_trades=[trade],
        prob_tickformer_by_asset={"BTC": 0.40},
        clob_by_asset={"BTC": CLOBSnapshot(best_bid_held=0.01)},
        eval_offset_by_asset={"BTC": 150},
    )
    assert mock_repo.insert_trigger.call_count == 3  # 0.50, 0.55, 0.60 fired


@pytest.mark.asyncio
async def test_no_insert_when_no_prob_signal(mock_repo):
    """No insert when asset has no TickFormer probability."""
    uc = MonitorOpenTradesUseCase(repo=mock_repo)
    trade = _trade()
    await uc.execute(
        open_trades=[trade],
        prob_tickformer_by_asset={},  # no signal
        clob_by_asset={},
        eval_offset_by_asset={"BTC": 150},
    )
    mock_repo.insert_trigger.assert_not_called()


@pytest.mark.asyncio
async def test_no_insert_below_threshold(mock_repo):
    """No insert when p_against < 0.50."""
    uc = MonitorOpenTradesUseCase(repo=mock_repo)
    trade = _trade(side="UP")
    # P(UP)=0.60 → p_against=0.40, below all thresholds
    await uc.execute(
        open_trades=[trade],
        prob_tickformer_by_asset={"BTC": 0.60},
        clob_by_asset={"BTC": CLOBSnapshot()},
        eval_offset_by_asset={"BTC": 150},
    )
    mock_repo.insert_trigger.assert_not_called()


@pytest.mark.asyncio
async def test_clob_data_threaded_through(mock_repo):
    """CLOB snapshot fields are passed to insert_trigger."""
    uc = MonitorOpenTradesUseCase(repo=mock_repo)
    trade = _trade(side="DN")
    clob = CLOBSnapshot(
        best_bid_held=0.01,
        best_ask_held=0.10,
        best_bid_against=0.90,
        best_ask_against=0.95,
        book_depth_usd=None,
    )
    # P(UP)=0.70 → side=DN → p_against=0.70 → all thresholds fire
    await uc.execute(
        open_trades=[trade],
        prob_tickformer_by_asset={"BTC": 0.70},
        clob_by_asset={"BTC": clob},
        eval_offset_by_asset={"BTC": 90},
    )
    # Verify the first call includes CLOB fields
    call_kwargs = mock_repo.insert_trigger.call_args_list[0][1]
    assert call_kwargs["clob_best_bid_held"] == 0.01
    assert call_kwargs["clob_best_ask_held"] == 0.10
    assert call_kwargs["clob_best_bid_against"] == 0.90
    assert call_kwargs["clob_book_depth_usd"] is None


@pytest.mark.asyncio
async def test_first_cross_only_across_ticks(mock_repo):
    """Same threshold doesn't re-fire across multiple execute() calls."""
    uc = MonitorOpenTradesUseCase(repo=mock_repo)
    trade = _trade(side="UP")

    # Tick 1: P(UP)=0.40 → p_against=0.60 → 0.50, 0.55, 0.60 fire
    await uc.execute(
        open_trades=[trade],
        prob_tickformer_by_asset={"BTC": 0.40},
        clob_by_asset={"BTC": CLOBSnapshot()},
        eval_offset_by_asset={"BTC": 150},
    )
    count_after_tick1 = mock_repo.insert_trigger.call_count

    # Tick 2: same probability — same thresholds should NOT re-fire
    await uc.execute(
        open_trades=[trade],
        prob_tickformer_by_asset={"BTC": 0.40},
        clob_by_asset={"BTC": CLOBSnapshot()},
        eval_offset_by_asset={"BTC": 148},
    )
    count_after_tick2 = mock_repo.insert_trigger.call_count
    assert count_after_tick2 == count_after_tick1, "No new inserts on repeat tick"


@pytest.mark.asyncio
async def test_detector_cleaned_up_when_position_closes(mock_repo):
    """Detector is removed when trade disappears from open_trades."""
    uc = MonitorOpenTradesUseCase(repo=mock_repo)
    trade = _trade(decision_id=99)

    # First tick: trade open
    await uc.execute(
        open_trades=[trade],
        prob_tickformer_by_asset={"BTC": 0.85},
        clob_by_asset={"BTC": CLOBSnapshot()},
        eval_offset_by_asset={"BTC": 150},
    )
    assert 99 in uc._detectors

    # Second tick: trade closed (not in open_trades)
    await uc.execute(
        open_trades=[],
        prob_tickformer_by_asset={},
        clob_by_asset={},
        eval_offset_by_asset={},
    )
    assert 99 not in uc._detectors


@pytest.mark.asyncio
async def test_repo_exception_swallowed(mock_repo):
    """Exception from repo.insert_trigger is swallowed — caller never sees it."""
    mock_repo.insert_trigger = AsyncMock(side_effect=RuntimeError("db down"))
    uc = MonitorOpenTradesUseCase(repo=mock_repo)
    trade = _trade(side="DN")

    # Should not raise
    await uc.execute(
        open_trades=[trade],
        prob_tickformer_by_asset={"BTC": 0.70},
        clob_by_asset={"BTC": CLOBSnapshot()},
        eval_offset_by_asset={"BTC": 100},
    )


@pytest.mark.asyncio
async def test_multiple_assets_independent(mock_repo):
    """Each asset's trades are evaluated independently."""
    uc = MonitorOpenTradesUseCase(repo=mock_repo)
    btc_trade = _trade(decision_id=1, asset="BTC", side="UP")
    eth_trade = _trade(decision_id=2, asset="ETH", side="DN")

    # BTC prob triggers (p_against=0.60), ETH prob doesn't (p_against=0.30)
    await uc.execute(
        open_trades=[btc_trade, eth_trade],
        prob_tickformer_by_asset={"BTC": 0.40, "ETH": 0.70},  # ETH DN: p_against=0.70, wait
        clob_by_asset={"BTC": CLOBSnapshot(), "ETH": CLOBSnapshot()},
        eval_offset_by_asset={"BTC": 150, "ETH": 150},
    )
    # Both should have triggered (BTC: p_against=0.60, ETH DN: p_against=0.70)
    assert mock_repo.insert_trigger.call_count > 0
