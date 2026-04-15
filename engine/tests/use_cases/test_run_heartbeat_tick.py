"""Tests for RunHeartbeatTickUseCase."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from use_cases.run_heartbeat_tick import RunHeartbeatTickUseCase


def _make_uc(publish_uc=None, aggregator=None, risk_manager=None):
    if publish_uc is None:
        publish_uc = MagicMock()
        publish_uc.tick = AsyncMock(return_value=None)
    if aggregator is None:
        aggregator = MagicMock()
        aggregator.get_state = AsyncMock(return_value=MagicMock(
            vpin=None, btc_price=None, cascade=None,
            binance_connected=True, coinglass_connected=False,
            chainlink_connected=False, polymarket_connected=True, opinion_connected=False,
        ))
    if risk_manager is None:
        risk_manager = MagicMock()
        risk_manager.get_status = MagicMock(return_value={"daily_pnl": 0.0, "current_bankroll": 500.0})

    engine_state_reader = MagicMock()
    engine_state_reader.update = MagicMock()

    return RunHeartbeatTickUseCase(
        publish_heartbeat_uc=publish_uc,
        engine_state_reader=engine_state_reader,
        aggregator=aggregator,
        risk_manager=risk_manager,
        order_manager=None,
        poly_client=None,
        settings=MagicMock(paper_mode=True),
    )


@pytest.mark.asyncio
async def test_tick_calls_publish_heartbeat():
    publish_uc = MagicMock()
    publish_uc.tick = AsyncMock(return_value=None)
    uc = _make_uc(publish_uc=publish_uc)
    await uc.execute()
    publish_uc.tick.assert_called_once()


@pytest.mark.asyncio
async def test_tick_calls_engine_state_reader_update():
    uc = _make_uc()
    await uc.execute()
    uc._engine_state_reader.update.assert_called_once()


@pytest.mark.asyncio
async def test_tick_survives_aggregator_error():
    aggregator = MagicMock()
    aggregator.get_state = AsyncMock(side_effect=RuntimeError("feed down"))
    uc = _make_uc(aggregator=aggregator)
    await uc.execute()  # Should not raise
