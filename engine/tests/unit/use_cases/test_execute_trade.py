"""Tests for ExecuteTradeUseCase.

Covers the 10-step flow:
  1. Dedup check
  2. Stake calculation
  3. Risk approval
  4. Guardrails (rate limit, circuit breaker)
  5. Token ID resolution
  6. Order execution
  7. Trade recording
  8. Mark traded
  9. Telegram alert
  10. Return result

All ports are mocked -- zero I/O.
"""

from __future__ import annotations

import pytest
from dataclasses import dataclass, field, replace
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

from domain.value_objects import (
    ExecutionResult,
    RiskStatus,
    StakeCalculation,
    StrategyDecision,
    WindowKey,
    WindowMarket,
)

# ─── Fixtures ────────────────────────────────────────────────────────────


def _make_decision(**overrides) -> StrategyDecision:
    """Build a StrategyDecision with sensible defaults."""
    defaults = dict(
        action="TRADE",
        direction="DOWN",
        confidence="HIGH",
        confidence_score=0.75,
        entry_cap=0.55,
        collateral_pct=0.025,
        strategy_id="v4_down_only",
        strategy_version="2.0.0",
        entry_reason="v4_down_only_T120_DOWN_clob_sized",
        skip_reason=None,
        metadata={
            "gate_results": [
                {"gate": "timing", "passed": True, "reason": "T-120 in [90, 150]"},
                {"gate": "direction", "passed": True, "reason": "DOWN = DOWN"},
            ],
            "sizing": {"fraction": 0.025, "modifier": 2.0, "label": "strong_97pct"},
        },
    )
    defaults.update(overrides)
    return StrategyDecision(**defaults)


def _make_window_market(**overrides) -> WindowMarket:
    defaults = dict(
        condition_id="0xabc123",
        up_token_id="0xUP_TOKEN_ID_FULL",
        down_token_id="0xDOWN_TOKEN_ID_FULL",
        market_slug="btc-updown-5m-1713000000",
        active=True,
    )
    defaults.update(overrides)
    return WindowMarket(**defaults)


def _make_risk_status(**overrides) -> RiskStatus:
    defaults = dict(
        current_bankroll=500.0,
        peak_bankroll=520.0,
        drawdown_pct=0.04,
        daily_pnl=5.0,
        consecutive_losses=0,
        paper_mode=True,
        kill_switch_active=False,
    )
    defaults.update(overrides)
    return RiskStatus(**defaults)


def _make_fill_result(**overrides) -> ExecutionResult:
    defaults = dict(
        success=True,
        order_id="paper-abc123",
        fill_price=0.55,
        fill_size=18.18,
        stake_usd=10.0,
        fee_usd=0.18,
        execution_mode="paper",
        fak_attempts=0,
        fak_prices=[],
        token_id="0xDOWN_TOKEN_ID_FULL",
        execution_start=1000.0,
        execution_end=1000.5,
    )
    defaults.update(overrides)
    return ExecutionResult(**defaults)


class FakeClock:
    """Deterministic clock for testing."""

    def __init__(self, start: float = 1000.0):
        self._time = start

    def now(self) -> float:
        return self._time

    def advance(self, seconds: float) -> None:
        self._time += seconds


def _build_use_case(
    *,
    was_traded: bool = False,
    risk_status: Optional[RiskStatus] = None,
    fill_result: Optional[ExecutionResult] = None,
    clock: Optional[FakeClock] = None,
):
    """Build an ExecuteTradeUseCase with mock dependencies."""
    from use_cases.execute_trade import ExecuteTradeUseCase

    clock = clock or FakeClock()
    risk = risk_status or _make_risk_status()
    fill = fill_result or _make_fill_result()

    mock_poly = AsyncMock()
    mock_executor = AsyncMock()
    mock_executor.execute_order.return_value = fill
    mock_risk = MagicMock()
    mock_risk.get_status.return_value = risk
    mock_window_state = AsyncMock()
    mock_window_state.was_traded.return_value = was_traded
    mock_window_state.try_claim_trade.return_value = not was_traded
    mock_alerter = AsyncMock()
    mock_alerter.send_strategy_trade_alert = AsyncMock()
    mock_recorder = AsyncMock()

    uc = ExecuteTradeUseCase(
        polymarket=mock_poly,
        order_executor=mock_executor,
        risk_manager=mock_risk,
        window_state=mock_window_state,
        alerter=mock_alerter,
        trade_recorder=mock_recorder,
        clock=clock,
        paper_mode=True,
    )
    return uc, {
        "polymarket": mock_poly,
        "executor": mock_executor,
        "risk_manager": mock_risk,
        "window_state": mock_window_state,
        "alerter": mock_alerter,
        "recorder": mock_recorder,
        "clock": clock,
    }


# ─── Happy Path ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_down_trade():
    """DOWN signal -> fill -> record -> mark traded -> alert."""
    uc, mocks = _build_use_case()
    decision = _make_decision(direction="DOWN")
    market = _make_window_market()

    result = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84231.0,
        open_price=84331.0,
    )

    assert result.success
    assert result.execution_mode == "paper"
    assert result.strategy_id == "v4_down_only"
    assert result.direction == "DOWN"

    # Executor was called with the DOWN token
    mocks["executor"].execute_order.assert_called_once()
    call_args = mocks["executor"].execute_order.call_args
    assert call_args.kwargs["token_id"] == "0xDOWN_TOKEN_ID_FULL"
    assert call_args.kwargs["side"] == "NO"

    # Trade was recorded
    mocks["recorder"].record_trade.assert_called_once()

    # Window claimed and marked as traded
    mocks["window_state"].try_claim_trade.assert_called_once()
    mocks["window_state"].mark_traded.assert_called_once()

    # Alert sent
    mocks["alerter"].send_strategy_trade_alert.assert_called_once()


@pytest.mark.asyncio
async def test_happy_path_up_trade():
    """UP signal -> uses YES token."""
    uc, mocks = _build_use_case()
    decision = _make_decision(direction="UP")
    market = _make_window_market()

    result = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84231.0,
        open_price=84131.0,
    )

    assert result.success
    call_args = mocks["executor"].execute_order.call_args
    assert call_args.kwargs["token_id"] == "0xUP_TOKEN_ID_FULL"
    assert call_args.kwargs["side"] == "YES"


# ─── Dedup ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_already_traded_dedup():
    """Window already traded -> skip, no execution."""
    uc, mocks = _build_use_case(was_traded=True)
    decision = _make_decision()
    market = _make_window_market()

    result = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84231.0,
        open_price=84331.0,
    )

    assert not result.success
    assert result.failure_reason == "already_traded"
    mocks["window_state"].try_claim_trade.assert_called_once()
    mocks["executor"].execute_order.assert_not_called()
    mocks["recorder"].record_trade.assert_not_called()


# ─── Risk Check ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_risk_blocked_kill_switch():
    """Kill switch active -> blocked, alert sent."""
    risk = _make_risk_status(kill_switch_active=True)
    uc, mocks = _build_use_case(risk_status=risk)
    decision = _make_decision()
    market = _make_window_market()

    result = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84231.0,
        open_price=84331.0,
    )

    assert not result.success
    assert "kill_switch" in result.failure_reason
    mocks["executor"].execute_order.assert_not_called()
    # Alert should be sent for risk blocks
    mocks["alerter"].send_system_alert.assert_called_once()


@pytest.mark.asyncio
async def test_risk_blocked_drawdown():
    """Drawdown too high -> blocked."""
    risk = _make_risk_status(drawdown_pct=0.50)
    uc, mocks = _build_use_case(risk_status=risk)
    decision = _make_decision()
    market = _make_window_market()

    result = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84231.0,
        open_price=84331.0,
    )

    assert not result.success
    assert "drawdown" in result.failure_reason


@pytest.mark.asyncio
async def test_risk_blocked_stake_too_small():
    """Bankroll too small -> stake below minimum -> blocked."""
    risk = _make_risk_status(current_bankroll=10.0)
    uc, mocks = _build_use_case(risk_status=risk)
    # With bankroll=$10, bet_fraction=0.025 -> base=$0.25 -> below $2 min
    decision = _make_decision(collateral_pct=0.025)
    market = _make_window_market()

    result = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84231.0,
        open_price=84331.0,
    )

    assert not result.success
    assert "minimum" in result.failure_reason


# ─── Guardrails ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_blocked():
    """Too many orders too fast -> rate limited."""
    clock = FakeClock(1000.0)
    uc, mocks = _build_use_case(clock=clock)
    decision = _make_decision()
    market = _make_window_market()

    # First trade succeeds
    result1 = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84231.0,
        open_price=84331.0,
    )
    assert result1.success

    # Reset dedup for second call
    mocks["window_state"].was_traded.return_value = False

    # Second trade 5 seconds later -> rate limited (need 30s gap)
    clock.advance(5.0)
    result2 = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84231.0,
        open_price=84331.0,
    )
    assert not result2.success
    assert "rate_limit" in result2.failure_reason


@pytest.mark.asyncio
async def test_circuit_breaker_tripped():
    """3 consecutive real errors -> circuit breaker activated."""
    clock = FakeClock(1000.0)
    fail_fill = _make_fill_result(
        success=False, failure_reason="gtc_submit_error: clob rejected"
    )
    uc, mocks = _build_use_case(clock=clock, fill_result=fail_fill)
    decision = _make_decision()
    market = _make_window_market()

    # 3 failed orders to trip circuit breaker
    for i in range(3):
        clock.advance(31.0)  # Past rate limit interval
        mocks["window_state"].was_traded.return_value = False
        await uc.execute(
            decision=decision,
            window_market=market,
            current_btc_price=84231.0,
            open_price=84331.0,
        )

    # Next order should be circuit-breaker blocked
    clock.advance(31.0)
    mocks["window_state"].was_traded.return_value = False
    result = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84231.0,
        open_price=84331.0,
    )
    assert not result.success
    assert "circuit_breaker" in result.failure_reason


@pytest.mark.asyncio
async def test_circuit_breaker_sends_tg_alert():
    """Circuit breaker trip fires send_system_alert to TG."""
    clock = FakeClock(1000.0)
    fail_fill = _make_fill_result(
        success=False, failure_reason="gtc_submit_error: clob rejected"
    )
    uc, mocks = _build_use_case(clock=clock, fill_result=fail_fill)
    decision = _make_decision()
    market = _make_window_market()

    # 3 failed orders to trip circuit breaker
    for _ in range(3):
        clock.advance(31.0)
        mocks["window_state"].was_traded.return_value = False
        await uc.execute(
            decision=decision,
            window_market=market,
            current_btc_price=84231.0,
            open_price=84331.0,
        )

    # Verify send_system_alert was called with circuit breaker message
    alert_calls = mocks["alerter"].send_system_alert.call_args_list
    cb_alerts = [c for c in alert_calls if "Circuit breaker" in str(c)]
    assert len(cb_alerts) == 1
    assert "3 consecutive" in str(cb_alerts[0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_reason",
    [
        "fak_rfq_exhausted; gtc_fallback_disabled",
        "gtc_unfilled",
    ],
)
async def test_benign_no_fill_does_not_trip_breaker(failure_reason):
    """No-liquidity outcomes (FAK exhaustion, GTC unfilled) are market
    conditions, not infra errors. They MUST NOT count toward the
    consecutive-error counter that trips the 180s circuit breaker.
    """
    clock = FakeClock(1000.0)
    fail_fill = _make_fill_result(success=False, failure_reason=failure_reason)
    uc, mocks = _build_use_case(clock=clock, fill_result=fail_fill)
    decision = _make_decision()
    market = _make_window_market()

    # Ten benign no-fills in a row — well beyond CIRCUIT_BREAKER_ERRORS=3
    for _ in range(10):
        clock.advance(31.0)
        mocks["window_state"].was_traded.return_value = False
        result = await uc.execute(
            decision=decision,
            window_market=market,
            current_btc_price=84231.0,
            open_price=84331.0,
        )
        assert not result.success
        # Never circuit-breaker-blocked
        assert "circuit_breaker" not in (result.failure_reason or "")

    # No TG circuit-breaker alert fired
    alert_calls = mocks["alerter"].send_system_alert.call_args_list
    cb_alerts = [c for c in alert_calls if "Circuit breaker" in str(c)]
    assert cb_alerts == []


@pytest.mark.asyncio
async def test_benign_no_fills_do_not_poison_real_error_counter():
    """A real gtc_submit_error after a string of benign no-fills should
    start the counter from 1, not inherit from the benign stream.
    Two real errors alone must not trip (threshold=3).
    """
    clock = FakeClock(1000.0)
    benign = _make_fill_result(
        success=False, failure_reason="fak_rfq_exhausted; gtc_fallback_disabled"
    )
    real = _make_fill_result(
        success=False, failure_reason="gtc_submit_error: clob rejected"
    )
    uc, mocks = _build_use_case(clock=clock, fill_result=benign)
    decision = _make_decision()
    market = _make_window_market()

    # 5 benign no-fills
    for _ in range(5):
        clock.advance(31.0)
        mocks["window_state"].was_traded.return_value = False
        await uc.execute(
            decision=decision,
            window_market=market,
            current_btc_price=84231.0,
            open_price=84331.0,
        )

    # 2 real errors — below threshold, should NOT trip yet
    mocks["executor"].execute_order.return_value = real
    for _ in range(2):
        clock.advance(31.0)
        mocks["window_state"].was_traded.return_value = False
        await uc.execute(
            decision=decision,
            window_market=market,
            current_btc_price=84231.0,
            open_price=84331.0,
        )

    clock.advance(31.0)
    mocks["window_state"].was_traded.return_value = False
    result = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84231.0,
        open_price=84331.0,
    )
    # Third real error — this one trips
    assert "circuit_breaker" not in (result.failure_reason or "")
    # But the NEXT call should be blocked
    clock.advance(31.0)
    mocks["window_state"].was_traded.return_value = False
    blocked = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84231.0,
        open_price=84331.0,
    )
    assert "circuit_breaker" in (blocked.failure_reason or "")


# ─── Token ID Missing ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_token_id():
    """Direction maps to empty token_id -> fail gracefully."""
    uc, mocks = _build_use_case()
    decision = _make_decision(direction="DOWN")
    market = _make_window_market(down_token_id="")

    result = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84231.0,
        open_price=84331.0,
    )

    assert not result.success
    assert result.failure_reason == "no_token_id"
    mocks["executor"].execute_order.assert_not_called()


# ─── Paper Mode ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_paper_mode_fill():
    """Paper executor simulates fill, full pipeline runs."""
    paper_fill = _make_fill_result(execution_mode="paper")
    uc, mocks = _build_use_case(fill_result=paper_fill)
    decision = _make_decision()
    market = _make_window_market()

    result = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84231.0,
        open_price=84331.0,
    )

    assert result.success
    assert result.execution_mode == "paper"
    mocks["recorder"].record_trade.assert_called_once()
    mocks["window_state"].mark_traded.assert_called_once()


# ─── Execution Error ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execution_exception_handled():
    """Executor raises exception -> caught, circuit breaker incremented."""
    clock = FakeClock(1000.0)
    uc, mocks = _build_use_case(clock=clock)
    mocks["executor"].execute_order.side_effect = RuntimeError("CLOB timeout")
    decision = _make_decision()
    market = _make_window_market()

    result = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84231.0,
        open_price=84331.0,
    )

    assert not result.success
    assert "execution_error" in result.failure_reason
    mocks["recorder"].record_trade.assert_not_called()


# ─── Stake Calculation ──────────────────────────────────────────────────


class TestStakeCalculation:
    """Unit tests for _calculate_stake (extracted from five_min_vpin)."""

    def _calc(self, bankroll=500.0, entry_cap=0.50, collateral_pct=0.025):
        from use_cases.execute_trade import ExecuteTradeUseCase

        risk = _make_risk_status(current_bankroll=bankroll)
        clock = FakeClock()
        uc, _ = _build_use_case(risk_status=risk, clock=clock)
        decision = _make_decision(entry_cap=entry_cap, collateral_pct=collateral_pct)
        return uc._calculate_stake(decision)

    def test_50_cent_token_1x_multiplier(self):
        """50c token -> 1.0x multiplier."""
        s = self._calc(entry_cap=0.50)
        assert abs(s.price_multiplier - 1.0) < 0.01

    def test_40_cent_token_higher_multiplier(self):
        """40c token -> 1.2x multiplier (better R/R, bigger bet)."""
        s = self._calc(entry_cap=0.40)
        assert abs(s.price_multiplier - 1.2) < 0.01

    def test_65_cent_token_lower_multiplier(self):
        """65c token -> 0.7x multiplier (worse R/R, smaller bet)."""
        s = self._calc(entry_cap=0.65)
        assert abs(s.price_multiplier - 0.7) < 0.01

    def test_stake_hard_cap(self):
        """Stake never exceeds $50 hard cap."""
        s = self._calc(bankroll=10000.0, collateral_pct=0.10)
        assert s.adjusted_stake <= 50.0

    def test_stake_minimum_rejected_by_risk(self):
        """Very small bankroll -> tiny stake -> risk check rejects."""
        s = self._calc(bankroll=10.0, collateral_pct=0.01)
        assert s.adjusted_stake < 2.0  # Below minimum

    def test_fee_calculation(self):
        """Fee = 0.072 * p * (1-p) * stake."""
        from use_cases.execute_trade import ExecuteTradeUseCase

        fee = ExecuteTradeUseCase.calculate_fee(0.55, 10.0)
        expected = 0.072 * 0.55 * 0.45 * 10.0
        assert abs(fee - expected) < 0.001


# ─── Window Key Extraction ──────────────────────────────────────────────


class TestWindowKeyExtraction:
    """Test _make_window_key from market slug."""

    def test_standard_slug(self):
        from use_cases.execute_trade import ExecuteTradeUseCase

        market = _make_window_market(market_slug="btc-updown-5m-1713000000")
        key = ExecuteTradeUseCase._make_window_key(market)
        assert key.asset == "BTC"
        assert key.window_ts == 1713000000
        assert key.timeframe == "5m"

    def test_15m_slug(self):
        from use_cases.execute_trade import ExecuteTradeUseCase

        market = _make_window_market(market_slug="btc-updown-15m-1713000000")
        key = ExecuteTradeUseCase._make_window_key(market)
        assert key.timeframe == "15m"


# ─── Alert Format ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_alert_includes_strategy_name():
    """Telegram alert includes strategy name in subject line."""
    uc, mocks = _build_use_case()
    decision = _make_decision(strategy_id="v4_down_only", strategy_version="2.0.0")
    market = _make_window_market()

    await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84231.0,
        open_price=84331.0,
    )

    alert_kwargs = mocks["alerter"].send_strategy_trade_alert.call_args.kwargs
    assert alert_kwargs["strategy_id"] == "v4_down_only"
    assert alert_kwargs["strategy_version"] == "2.0.0"


@pytest.mark.asyncio
async def test_alert_paper_mode_label():
    """Paper mode trades are labeled in the alert."""
    uc, mocks = _build_use_case()
    decision = _make_decision()
    market = _make_window_market()

    await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84231.0,
        open_price=84331.0,
    )

    alert_kwargs = mocks["alerter"].send_strategy_trade_alert.call_args.kwargs
    assert alert_kwargs["paper_mode"] is True


# ─── Port-contract + regression tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_port_contract_approved_trade_reaches_executor():
    """Port contract: approved decision -> executor.execute_order called once
    + recorder.record_trade called once. Guards against execution-path dropout."""
    uc, mocks = _build_use_case()
    result = await uc.execute(
        decision=_make_decision(direction="DOWN"),
        window_market=_make_window_market(),
        current_btc_price=84231.0,
        open_price=84331.0,
    )
    assert result.success
    mocks["executor"].execute_order.assert_called_once()
    mocks["recorder"].record_trade.assert_called_once()


@pytest.mark.asyncio
async def test_port_contract_risk_rejected_skips_executor():
    """Port contract: risk-rejected trade must NOT reach the executor.
    Guards against #207-class silent execution path dropout."""
    risk = _make_risk_status(kill_switch_active=True)
    uc, mocks = _build_use_case(risk_status=risk)
    result = await uc.execute(
        decision=_make_decision(),
        window_market=_make_window_market(),
        current_btc_price=84231.0,
        open_price=84331.0,
    )
    assert not result.success
    mocks["executor"].execute_order.assert_not_called()
    mocks["recorder"].record_trade.assert_not_called()


@pytest.mark.asyncio
async def test_dedup_via_was_traded_fallback_path():
    """When window_state has no try_claim_trade, fall back to was_traded().
    Covers the elif branch (lines 169-174) for older window_state impls."""
    from use_cases.execute_trade import ExecuteTradeUseCase

    class LegacyWindowState:
        """Window state without try_claim_trade (older impl)."""
        async def was_traded(self, key):
            return True  # Already traded

        async def mark_traded(self, key, order_id):
            pass

    mock_risk = MagicMock()
    mock_risk.get_status.return_value = _make_risk_status()

    uc = ExecuteTradeUseCase(
        polymarket=AsyncMock(),
        order_executor=AsyncMock(),
        risk_manager=mock_risk,
        window_state=LegacyWindowState(),
        alerter=AsyncMock(),
        trade_recorder=AsyncMock(),
        clock=FakeClock(),
        paper_mode=True,
    )
    result = await uc.execute(
        decision=_make_decision(),
        window_market=_make_window_market(),
        current_btc_price=84231.0,
        open_price=84331.0,
    )
    assert not result.success
    assert result.failure_reason == "already_traded"


@pytest.mark.asyncio
async def test_execution_error_clears_trade_claim():
    """Executor exception -> clear_trade_claim called so window can be retried."""
    clock = FakeClock(1000.0)
    uc, mocks = _build_use_case(clock=clock)
    mocks["executor"].execute_order.side_effect = RuntimeError("CLOB timeout")
    mocks["window_state"].clear_trade_claim = AsyncMock()

    result = await uc.execute(
        decision=_make_decision(),
        window_market=_make_window_market(),
        current_btc_price=84231.0,
        open_price=84331.0,
    )
    assert not result.success
    assert "execution_error" in result.failure_reason
    mocks["window_state"].clear_trade_claim.assert_called_once()


@pytest.mark.asyncio
async def test_failed_fill_clears_trade_claim():
    """Failed fill (success=False) -> clear_trade_claim so dedup doesn't block retry."""
    fail_fill = _make_fill_result(success=False, failure_reason="no_liquidity")
    uc, mocks = _build_use_case(fill_result=fail_fill)
    mocks["window_state"].clear_trade_claim = AsyncMock()

    result = await uc.execute(
        decision=_make_decision(),
        window_market=_make_window_market(),
        current_btc_price=84231.0,
        open_price=84331.0,
    )
    assert not result.success
    assert result.failure_reason == "no_liquidity"
    mocks["window_state"].clear_trade_claim.assert_called_once()


@pytest.mark.asyncio
async def test_fallback_to_send_system_alert_when_no_rich_alert():
    """When alerter has no send_strategy_trade_alert, falls back to
    _format_trade_alert + send_system_alert. Covers lines 384-396."""
    uc, mocks = _build_use_case()
    # Strip the rich-alert method so the fallback branch runs
    del mocks["alerter"].send_strategy_trade_alert

    result = await uc.execute(
        decision=_make_decision(direction="DOWN"),
        window_market=_make_window_market(),
        current_btc_price=84231.0,
        open_price=84331.0,
    )
    assert result.success
    mocks["alerter"].send_system_alert.assert_called_once()
    alert_text = mocks["alerter"].send_system_alert.call_args.args[0]
    assert "TRADE" in alert_text
    assert "v4_down_only" in alert_text


@pytest.mark.asyncio
async def test_format_trade_alert_paper_prefix():
    """_format_trade_alert includes PAPER MODE prefix when paper_mode=True."""
    uc, _ = _build_use_case()
    decision = _make_decision(direction="DOWN", strategy_id="v4_test")
    fill = _make_fill_result(
        fill_price=0.55, fill_size=18.0, stake_usd=10.0,
        execution_start=1000.0, execution_end=1001.5,
    )
    msg = uc._format_trade_alert(decision, fill, stake=None, btc_price=84000.0, open_price=84100.0)
    assert "PAPER MODE" in msg
    assert "v4_test" in msg


@pytest.mark.asyncio
async def test_format_trade_alert_failure_reason():
    """_format_trade_alert includes failure reason when fill fails."""
    uc, _ = _build_use_case()
    decision = _make_decision(direction="DOWN")
    fill = _make_fill_result(success=False, failure_reason="no_liquidity")
    msg = uc._format_trade_alert(decision, fill, stake=None, btc_price=84000.0, open_price=84100.0)
    assert "no_liquidity" in msg
