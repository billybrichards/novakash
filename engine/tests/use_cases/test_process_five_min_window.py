"""Tests for ProcessFiveMinWindowUseCase."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from use_cases.process_five_min_window import ProcessFiveMinWindowUseCase


class FakeWindow:
    def __init__(self):
        self.asset = "BTC"
        self.window_ts = 1713225600
        self.open_price = 65000.0
        self.up_price = 0.60
        self.down_price = 0.40
        self.state = MagicMock(value="ACTIVE")


@pytest.mark.asyncio
async def test_process_window_no_strategy_is_noop():
    """With no strategy injected, use case runs without error and returns None."""
    uc = ProcessFiveMinWindowUseCase(strategy=None, shadow_strategies=[])
    result = await uc.execute(FakeWindow())
    assert result is None


@pytest.mark.asyncio
async def test_process_window_calls_strategy_append():
    """Strategy.append_pending_window and append_recent_window are called."""
    strategy = MagicMock()
    strategy.append_pending_window = MagicMock()
    strategy.append_recent_window = MagicMock()
    strategy.on_window = AsyncMock(return_value=None)
    uc = ProcessFiveMinWindowUseCase(strategy=strategy, shadow_strategies=[])
    await uc.execute(FakeWindow())
    strategy.append_pending_window.assert_called_once()
    strategy.append_recent_window.assert_called_once()


@pytest.mark.asyncio
async def test_process_window_calls_on_window():
    """strategy.on_window is called with the window."""
    strategy = MagicMock()
    strategy.append_pending_window = MagicMock()
    strategy.append_recent_window = MagicMock()
    strategy.on_window = AsyncMock(return_value=None)
    window = FakeWindow()
    uc = ProcessFiveMinWindowUseCase(strategy=strategy, shadow_strategies=[])
    await uc.execute(window)
    strategy.on_window.assert_called_once_with(window)


@pytest.mark.asyncio
async def test_process_window_broadcasts_to_shadows():
    """Shadow strategies receive on_window call."""
    strategy = MagicMock()
    strategy.append_pending_window = MagicMock()
    strategy.append_recent_window = MagicMock()
    strategy.on_window = AsyncMock(return_value=None)

    shadow = MagicMock()
    shadow.on_window = AsyncMock(return_value=None)

    window = FakeWindow()
    uc = ProcessFiveMinWindowUseCase(strategy=strategy, shadow_strategies=[shadow])
    await uc.execute(window)
    shadow.on_window.assert_called_once_with(window)


@pytest.mark.asyncio
async def test_process_window_shadow_error_does_not_propagate():
    """If a shadow strategy raises, the use case continues without error."""
    strategy = MagicMock()
    strategy.append_pending_window = MagicMock()
    strategy.append_recent_window = MagicMock()
    strategy.on_window = AsyncMock(return_value=None)

    shadow = MagicMock()
    shadow.strategy_id = "ghost_v1"
    shadow.on_window = AsyncMock(side_effect=RuntimeError("shadow boom"))

    uc = ProcessFiveMinWindowUseCase(strategy=strategy, shadow_strategies=[shadow])
    # Should not raise
    await uc.execute(FakeWindow())
