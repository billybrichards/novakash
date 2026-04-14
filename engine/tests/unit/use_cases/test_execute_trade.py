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
    """3 consecutive errors -> circuit breaker activated."""
    clock = FakeClock(1000.0)
    fail_fill = _make_fill_result(success=False, failure_reason="clob_error")
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
