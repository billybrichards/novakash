"""Tests for RunHeartbeatTickUseCase."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from use_cases.run_heartbeat_tick import RunHeartbeatTickUseCase


def _make_uc(
    publish_uc=None,
    aggregator=None,
    risk_manager=None,
    wallet_rpc_reader=None,
    poly_client=None,
    paper_mode=True,
):
    if publish_uc is None:
        publish_uc = MagicMock()
        publish_uc.tick = AsyncMock(return_value=None)
        publish_uc.set_wallet_balance = MagicMock()
    if aggregator is None:
        aggregator = MagicMock()
        aggregator.get_state = AsyncMock(return_value=MagicMock(
            vpin=None, btc_price=None, cascade=None,
            binance_connected=True, coinglass_connected=False,
            chainlink_connected=False, polymarket_connected=True, opinion_connected=False,
        ))
    if risk_manager is None:
        risk_manager = MagicMock()
        risk_manager.sync_bankroll = AsyncMock()
        risk_manager.get_status = MagicMock(return_value={"daily_pnl": 0.0, "current_bankroll": 500.0})

    engine_state_reader = MagicMock()
    engine_state_reader.update = MagicMock()

    return RunHeartbeatTickUseCase(
        publish_heartbeat_uc=publish_uc,
        engine_state_reader=engine_state_reader,
        aggregator=aggregator,
        risk_manager=risk_manager,
        order_manager=None,
        poly_client=poly_client,
        settings=MagicMock(paper_mode=paper_mode),
        wallet_rpc_reader=wallet_rpc_reader,
    )


@pytest.mark.asyncio
async def test_tick_calls_publish_heartbeat():
    publish_uc = MagicMock()
    publish_uc.tick = AsyncMock(return_value=None)
    publish_uc.set_wallet_balance = MagicMock()
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


# ── Bankroll sync with combined USDC + pUSD ─────────────────────────────────


def _make_wallet_rpc(usdc=202.0, pusd=284.0):
    """Build a mock WalletRPCReader returning given balances."""
    reader = MagicMock()
    reader.get_balance = AsyncMock(return_value=usdc)
    reader.get_pusd_balance = AsyncMock(return_value=pusd)
    return reader


async def _run_wallet_tick(uc):
    """Run enough ticks to trigger the wallet refresh (every 6th tick)."""
    for _ in range(6):
        await uc.execute()


@pytest.mark.asyncio
async def test_sync_bankroll_uses_combined_usdc_and_pusd():
    """The real-world scenario: USDC=$202, pUSD=$284 → effective=$486."""
    reader = _make_wallet_rpc(usdc=202.0, pusd=284.0)
    risk = MagicMock()
    risk.sync_bankroll = AsyncMock()
    risk.get_status = MagicMock(return_value={"current_bankroll": 500.0})

    uc = _make_uc(
        wallet_rpc_reader=reader,
        risk_manager=risk,
        paper_mode=False,
    )
    await _run_wallet_tick(uc)

    risk.sync_bankroll.assert_called_once_with(486.0, usdc=202.0, pusd=284.0)


@pytest.mark.asyncio
async def test_sync_bankroll_pusd_failure_falls_back_to_usdc_only():
    """If pUSD read fails, we still sync with USDC-only (graceful degradation)."""
    reader = MagicMock()
    reader.get_balance = AsyncMock(return_value=350.0)
    reader.get_pusd_balance = AsyncMock(side_effect=RuntimeError("RPC down"))
    risk = MagicMock()
    risk.sync_bankroll = AsyncMock()
    risk.get_status = MagicMock(return_value={"current_bankroll": 500.0})

    uc = _make_uc(
        wallet_rpc_reader=reader,
        risk_manager=risk,
        paper_mode=False,
    )
    await _run_wallet_tick(uc)

    # Should sync with USDC only (pusd=0.0 due to exception)
    risk.sync_bankroll.assert_called_once_with(350.0, usdc=350.0, pusd=0.0)


@pytest.mark.asyncio
async def test_sync_bankroll_pusd_returns_none_treated_as_zero():
    """If pUSD RPC returns None (no consensus), treat as 0."""
    reader = MagicMock()
    reader.get_balance = AsyncMock(return_value=200.0)
    reader.get_pusd_balance = AsyncMock(return_value=None)
    risk = MagicMock()
    risk.sync_bankroll = AsyncMock()
    risk.get_status = MagicMock(return_value={"current_bankroll": 500.0})

    uc = _make_uc(
        wallet_rpc_reader=reader,
        risk_manager=risk,
        paper_mode=False,
    )
    await _run_wallet_tick(uc)

    risk.sync_bankroll.assert_called_once_with(200.0, usdc=200.0, pusd=0.0)


@pytest.mark.asyncio
async def test_sync_bankroll_rpc_fails_falls_back_to_clob():
    """If WalletRPCReader USDC read fails, fallback to poly_client."""
    reader = MagicMock()
    reader.get_balance = AsyncMock(return_value=None)
    reader.get_pusd_balance = AsyncMock(return_value=100.0)

    poly = MagicMock()
    poly.get_balance = AsyncMock(return_value=250.0)

    risk = MagicMock()
    risk.sync_bankroll = AsyncMock()
    risk.get_status = MagicMock(return_value={"current_bankroll": 500.0})

    uc = _make_uc(
        wallet_rpc_reader=reader,
        risk_manager=risk,
        poly_client=poly,
        paper_mode=False,
    )
    await _run_wallet_tick(uc)

    # USDC from CLOB fallback + pUSD from RPC
    risk.sync_bankroll.assert_called_once_with(350.0, usdc=250.0, pusd=100.0)


@pytest.mark.asyncio
async def test_sync_bankroll_all_reads_fail_skips_sync():
    """If both RPC and CLOB fail, sync_bankroll is not called."""
    reader = MagicMock()
    reader.get_balance = AsyncMock(return_value=None)
    reader.get_pusd_balance = AsyncMock(return_value=None)

    poly = MagicMock()
    poly.get_balance = AsyncMock(return_value=None)

    risk = MagicMock()
    risk.sync_bankroll = AsyncMock()
    risk.get_status = MagicMock(return_value={"current_bankroll": 500.0})

    uc = _make_uc(
        wallet_rpc_reader=reader,
        risk_manager=risk,
        poly_client=poly,
        paper_mode=False,
    )
    await _run_wallet_tick(uc)

    risk.sync_bankroll.assert_not_called()


@pytest.mark.asyncio
async def test_sync_bankroll_no_reader_uses_clob_only():
    """Without WalletRPCReader, falls back to poly_client USDC-only."""
    poly = MagicMock()
    poly.get_balance = AsyncMock(return_value=400.0)

    risk = MagicMock()
    risk.sync_bankroll = AsyncMock()
    risk.get_status = MagicMock(return_value={"current_bankroll": 500.0})

    uc = _make_uc(
        wallet_rpc_reader=None,
        risk_manager=risk,
        poly_client=poly,
        paper_mode=False,
    )
    await _run_wallet_tick(uc)

    risk.sync_bankroll.assert_called_once_with(400.0, usdc=400.0, pusd=0.0)


@pytest.mark.asyncio
async def test_paper_mode_skips_wallet_sync():
    """In paper mode, sync_bankroll should not be called with wallet data."""
    reader = _make_wallet_rpc(usdc=202.0, pusd=284.0)
    risk = MagicMock()
    risk.sync_bankroll = AsyncMock()
    risk.get_status = MagicMock(return_value={"current_bankroll": 500.0})

    uc = _make_uc(
        wallet_rpc_reader=reader,
        risk_manager=risk,
        paper_mode=True,
    )
    await _run_wallet_tick(uc)

    risk.sync_bankroll.assert_not_called()
