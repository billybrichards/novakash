"""Unit tests for ExecuteManualTradeUseCase.

All ports are mocked.  No DB, no network, no Polymarket.

Covers:
  - Existing happy path / error paths (pre-Track B)
  - B2: Risk-gate rejection → status='failed_risk_gate' + Telegram alert
  - B3: Manual-trade alerter routes to MANUAL_TRADE_TELEGRAM_CHAT_ID when set
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from domain.value_objects import (
    FillResult,
    ManualTradeOutcome,
    PendingTrade,
    WindowMarket,
)
from use_cases.execute_manual_trade import ExecuteManualTradeUseCase


def _pending(
    trade_id="tid-001", direction="UP", entry_price=0.45,
    stake_usd=4.0, window_ts=1700000000, asset="BTC",
):
    return PendingTrade(
        trade_id=trade_id, direction=direction, entry_price=entry_price,
        stake_usd=stake_usd, window_ts=window_ts, asset=asset,
    )


def _market(up="tok-up-abc", down="tok-down-xyz"):
    return WindowMarket(
        condition_id="cond-123", up_token_id=up, down_token_id=down,
        market_slug="btc-updown-5m-1700000000",
    )


def _approving_risk_manager():
    """Returns a mock RiskManager that approves all trades."""
    rm = AsyncMock()
    rm.approve.return_value = (True, "ok")
    return rm


def _rejecting_risk_manager(reason: str = "kill_switch: active"):
    """Returns a mock RiskManager that rejects all trades."""
    rm = AsyncMock()
    rm.approve.return_value = (False, reason)
    return rm


class Ports:
    def __init__(self):
        self.polymarket = AsyncMock()
        self.manual_trade_repo = AsyncMock()
        self.window_state = AsyncMock()
        self.alerts = AsyncMock()
        self.clock = MagicMock()
        self.clock.now.return_value = 1700000100.0

    def uc(self, paper_mode=True, risk_manager=None):
        return ExecuteManualTradeUseCase(
            polymarket=self.polymarket,
            manual_trade_repo=self.manual_trade_repo,
            window_state=self.window_state,
            alerts=self.alerts,
            clock=self.clock,
            risk_manager=risk_manager,
            paper_mode=paper_mode,
        )


@pytest.mark.asyncio
async def test_drain_once_no_pending_returns_empty():
    p = Ports()
    p.polymarket.poll_pending_trades.return_value = []
    assert await p.uc().drain_once() == []
    p.manual_trade_repo.update_status.assert_not_called()


@pytest.mark.asyncio
async def test_paper_mode_happy_path():
    p = Ports()
    p.polymarket.poll_pending_trades.return_value = [_pending()]
    p.polymarket.get_window_market.return_value = _market()

    results = await p.uc(paper_mode=True).drain_once()

    assert len(results) == 1
    o = results[0]
    assert o.trade_id == "tid-001"
    assert o.status == "open"
    assert o.paper is True
    assert o.clob_order_id.startswith("manual-paper-")
    assert o.token_source == "window_market"

    calls = p.manual_trade_repo.update_status.call_args_list
    assert len(calls) == 2
    assert calls[0].args == ("tid-001", "executing")
    assert calls[1].args[0] == "tid-001"
    assert calls[1].args[1] == "open"


@pytest.mark.asyncio
async def test_live_mode_places_real_order():
    p = Ports()
    p.polymarket.poll_pending_trades.return_value = [_pending(entry_price=0.50, stake_usd=10.0)]
    p.polymarket.get_window_market.return_value = _market()
    p.polymarket.place_order.return_value = FillResult(
        order_id="clob-real-123", filled_size=10.0, filled_price=0.52,
    )

    results = await p.uc(paper_mode=False).drain_once()

    assert results[0].status == "open"
    assert results[0].clob_order_id == "clob-real-123"
    assert results[0].paper is False
    p.polymarket.place_order.assert_called_once_with(
        token_id="tok-up-abc", side="YES", size=10.0, price=0.52,
    )


@pytest.mark.asyncio
async def test_fallback_to_db_when_market_returns_none():
    p = Ports()
    p.polymarket.poll_pending_trades.return_value = [_pending(direction="DOWN")]
    p.polymarket.get_window_market.return_value = None
    p.manual_trade_repo.get_token_ids.return_value = {
        "up_token_id": "db-up", "down_token_id": "db-down",
    }

    results = await p.uc(paper_mode=True).drain_once()

    assert results[0].status == "open"
    assert results[0].token_source == "market_data_db"


@pytest.mark.asyncio
async def test_failed_no_token_when_both_sources_miss():
    p = Ports()
    p.polymarket.poll_pending_trades.return_value = [_pending()]
    p.polymarket.get_window_market.return_value = None
    p.manual_trade_repo.get_token_ids.return_value = None

    results = await p.uc().drain_once()

    assert results[0].status == "failed_no_token"
    assert results[0].clob_order_id is None
    p.alerts.send_system_alert.assert_called_once()
    assert "FAILED" in p.alerts.send_system_alert.call_args.args[0]


@pytest.mark.asyncio
async def test_down_direction_maps_to_no_and_down_token():
    p = Ports()
    p.polymarket.poll_pending_trades.return_value = [_pending(direction="DOWN")]
    p.polymarket.get_window_market.return_value = _market()

    results = await p.uc(paper_mode=True).drain_once()

    assert results[0].status == "open"
    assert results[0].token_source == "window_market"


@pytest.mark.asyncio
async def test_place_order_exception_marks_failed():
    p = Ports()
    p.polymarket.poll_pending_trades.return_value = [_pending()]
    p.polymarket.get_window_market.return_value = _market()
    p.polymarket.place_order.side_effect = RuntimeError("CLOB down")

    results = await p.uc(paper_mode=False).drain_once()

    assert results[0].status.startswith("failed:")
    assert "CLOB down" in results[0].status


@pytest.mark.asyncio
async def test_multiple_trades_processed_independently():
    p = Ports()
    p.polymarket.poll_pending_trades.return_value = [_pending(trade_id="t1"), _pending(trade_id="t2", direction="DOWN")]
    p.polymarket.get_window_market.return_value = _market()

    results = await p.uc(paper_mode=True).drain_once()

    assert len(results) == 2
    assert all(r.status == "open" for r in results)


@pytest.mark.asyncio
async def test_price_capped_at_max():
    p = Ports()
    p.polymarket.poll_pending_trades.return_value = [_pending(entry_price=0.64)]
    p.polymarket.get_window_market.return_value = _market()
    p.polymarket.place_order.return_value = FillResult(
        order_id="capped", filled_size=4.0, filled_price=0.65,
    )

    await p.uc(paper_mode=False).drain_once()

    assert p.polymarket.place_order.call_args.kwargs["price"] == 0.65


@pytest.mark.asyncio
async def test_alert_failure_does_not_break_use_case():
    p = Ports()
    p.polymarket.poll_pending_trades.return_value = [_pending()]
    p.polymarket.get_window_market.return_value = None
    p.manual_trade_repo.get_token_ids.return_value = None
    p.alerts.send_system_alert.side_effect = RuntimeError("Telegram down")

    results = await p.uc().drain_once()


# ─────────────────────────────────────────────────────────────────────────────
# B2: Risk-gate tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_risk_gate_approval_allows_trade():
    """When RiskManager approves, trade proceeds to 'open'."""
    p = Ports()
    p.polymarket.poll_pending_trades.return_value = [_pending()]
    p.polymarket.get_window_market.return_value = _market()
    rm = _approving_risk_manager()

    results = await p.uc(paper_mode=True, risk_manager=rm).drain_once()

    assert results[0].status == "open"
    rm.approve.assert_called_once_with(4.0, strategy="manual_trade")
    # place_order NOT called in paper mode, but status must be open
    p.manual_trade_repo.update_status.assert_any_call("tid-001", "open", clob_order_id=results[0].clob_order_id)


@pytest.mark.asyncio
async def test_risk_gate_rejection_marks_failed_risk_gate():
    """When RiskManager rejects, trade status becomes 'failed_risk_gate'."""
    p = Ports()
    p.polymarket.poll_pending_trades.return_value = [_pending()]
    p.polymarket.get_window_market.return_value = _market()
    rm = _rejecting_risk_manager("kill_switch: active (drawdown 50.0%)")

    results = await p.uc(paper_mode=True, risk_manager=rm).drain_once()

    assert results[0].status == "failed_risk_gate"
    assert results[0].clob_order_id is None
    p.manual_trade_repo.update_status.assert_any_call("tid-001", "failed_risk_gate")


@pytest.mark.asyncio
async def test_risk_gate_rejection_sends_telegram_alert():
    """Risk-gate rejection triggers a Telegram alert containing the reason."""
    p = Ports()
    p.polymarket.poll_pending_trades.return_value = [_pending(stake_usd=10.0)]
    p.polymarket.get_window_market.return_value = _market()
    rejection_reason = "daily_loss_limit: down $50.00 today"
    rm = _rejecting_risk_manager(rejection_reason)

    await p.uc(paper_mode=False, risk_manager=rm).drain_once()

    p.alerts.send_system_alert.assert_called_once()
    alert_text = p.alerts.send_system_alert.call_args.args[0]
    assert "Rejected" in alert_text or "Risk Gate" in alert_text
    assert rejection_reason in alert_text


@pytest.mark.asyncio
async def test_risk_gate_rejection_does_not_call_place_order():
    """After risk gate rejection, place_order must never be called."""
    p = Ports()
    p.polymarket.poll_pending_trades.return_value = [_pending()]
    p.polymarket.get_window_market.return_value = _market()
    rm = _rejecting_risk_manager("exposure_limit: $60.00 > $50.00")

    await p.uc(paper_mode=False, risk_manager=rm).drain_once()

    p.polymarket.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_no_risk_manager_skips_gate():
    """When risk_manager is None (default), the gate is bypassed and trade proceeds."""
    p = Ports()
    p.polymarket.poll_pending_trades.return_value = [_pending()]
    p.polymarket.get_window_market.return_value = _market()

    results = await p.uc(paper_mode=True, risk_manager=None).drain_once()

    assert results[0].status == "open"


@pytest.mark.asyncio
async def test_risk_gate_checked_after_token_resolution():
    """Risk gate should only fire after the token is resolved (not on failed_no_token)."""
    p = Ports()
    p.polymarket.poll_pending_trades.return_value = [_pending()]
    p.polymarket.get_window_market.return_value = None
    p.manual_trade_repo.get_token_ids.return_value = None
    # Even with a rejecting risk manager, the status should be failed_no_token
    # because token resolution failed first.
    rm = _rejecting_risk_manager()

    results = await p.uc(paper_mode=True, risk_manager=rm).drain_once()

    assert results[0].status == "failed_no_token"
    # Risk manager was NOT consulted (token resolution failed first)
    rm.approve.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# B3: ManualTradeAlerter routing tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_manual_trade_alerter_routes_to_override_chat():
    """When override_chat_id is set, send_raw_message_to is called (not send_raw_message)."""
    from adapters.alert.manual_trade_alerter import ManualTradeAlerter

    base_alerter = AsyncMock()
    base_alerter.send_raw_message_to = AsyncMock()
    base_alerter.send_raw_message = AsyncMock()

    manual_alerter = ManualTradeAlerter(
        alerter=base_alerter,
        override_chat_id="-100123456789",
    )

    await manual_alerter.send_system_alert("Test manual trade alert")

    base_alerter.send_raw_message_to.assert_called_once_with(
        text="Test manual trade alert",
        chat_id="-100123456789",
    )
    base_alerter.send_raw_message.assert_not_called()


@pytest.mark.asyncio
async def test_manual_trade_alerter_falls_back_when_no_override():
    """When override_chat_id is unset, send_raw_message is called on the primary alerter."""
    from adapters.alert.manual_trade_alerter import ManualTradeAlerter

    base_alerter = AsyncMock()
    base_alerter.send_raw_message = AsyncMock()
    base_alerter.send_raw_message_to = AsyncMock()

    manual_alerter = ManualTradeAlerter(alerter=base_alerter, override_chat_id=None)

    await manual_alerter.send_system_alert("Fallback alert")

    base_alerter.send_raw_message.assert_called_once_with("Fallback alert")
    base_alerter.send_raw_message_to.assert_not_called()


@pytest.mark.asyncio
async def test_manual_trade_alerter_empty_string_treated_as_no_override():
    """Empty-string override_chat_id falls back to primary path (not override)."""
    from adapters.alert.manual_trade_alerter import ManualTradeAlerter

    base_alerter = AsyncMock()
    base_alerter.send_raw_message = AsyncMock()
    base_alerter.send_raw_message_to = AsyncMock()

    manual_alerter = ManualTradeAlerter(alerter=base_alerter, override_chat_id="")

    await manual_alerter.send_system_alert("Empty override test")

    base_alerter.send_raw_message.assert_called_once_with("Empty override test")
    base_alerter.send_raw_message_to.assert_not_called()
