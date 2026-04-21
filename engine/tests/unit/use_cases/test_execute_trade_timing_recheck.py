"""Tests for ``_recheck_timing_before_execute`` and its wiring in
``ExecuteTradeUseCase.execute``.

Context
-------
2026-04-21: strategy timing gate relied on ``surface.eval_offset`` which
was set once per CLOSING milestone and not refreshed across retries /
RFQ fallback / GTC polls. This caused a fill at T-16 despite a
``min_offset_sec=30`` gate. These tests lock in the wall-clock recheck
that runs in ``ExecuteTradeUseCase.execute`` right after dedup passes.
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from domain.value_objects import (
    ExecutionResult,
    RiskStatus,
    StrategyDecision,
    WindowKey,
    WindowMarket,
)


# ─── Fake clock ───────────────────────────────────────────────────────────


class _FakeClock:
    def __init__(self, start: float) -> None:
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += float(seconds)


# ─── Builders ─────────────────────────────────────────────────────────────


def _decision(metadata: Optional[dict] = None, direction: str = "UP") -> StrategyDecision:
    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence="HIGH",
        confidence_score=0.75,
        entry_cap=0.65,
        collateral_pct=0.03,
        strategy_id="v6_sniper",
        strategy_version="6.1.2",
        entry_reason="test",
        skip_reason=None,
        metadata=metadata or {},
    )


def _window_key(window_ts: int, timeframe: str = "5m") -> WindowKey:
    return WindowKey(asset="BTC", window_ts=window_ts, timeframe=timeframe)


def _market(window_ts: int, timeframe: str = "5m") -> WindowMarket:
    return WindowMarket(
        condition_id=f"0xcond-{window_ts}",
        up_token_id="0xUP",
        down_token_id="0xDOWN",
        market_slug=f"btc-updown-{timeframe}-{window_ts}",
        active=True,
    )


# ─── Pure-function tests for _recheck_timing_before_execute ───────────────


def test_execute_passes_within_range():
    """current_offset=75s > min=30 → returns None (proceed)."""
    from use_cases.execute_trade import _recheck_timing_before_execute

    window_ts = 1_776_800_000
    close_ts = window_ts + 300
    now = close_ts - 75  # T-75

    reason = _recheck_timing_before_execute(
        _window_key(window_ts),
        _decision(metadata={"min_offset_sec": 30}),
        now_fn=lambda: float(now),
    )
    assert reason is None


def test_execute_aborts_below_min_offset():
    """current_offset=15s < min=30 → returns eval_offset_drift."""
    from use_cases.execute_trade import _recheck_timing_before_execute

    window_ts = 1_776_800_000
    close_ts = window_ts + 300
    now = close_ts - 15  # T-15

    reason = _recheck_timing_before_execute(
        _window_key(window_ts),
        _decision(metadata={"min_offset_sec": 30}),
        now_fn=lambda: float(now),
    )
    assert reason is not None
    assert reason.startswith("eval_offset_drift")
    assert "current=15s" in reason
    assert "min=30s" in reason


def test_execute_aborts_past_close():
    """Wall-clock at/past window close → returns eval_offset_past_close,
    regardless of min_offset."""
    from use_cases.execute_trade import _recheck_timing_before_execute

    window_ts = 1_776_800_000
    close_ts = window_ts + 300
    now = close_ts + 10  # 10s past close

    reason = _recheck_timing_before_execute(
        _window_key(window_ts),
        _decision(metadata={"min_offset_sec": 30}),
        now_fn=lambda: float(now),
    )
    assert reason is not None
    assert reason.startswith("eval_offset_past_close")


def test_execute_aborts_exactly_at_close():
    """offset == 0 must be treated as past-close (window has ticked over)."""
    from use_cases.execute_trade import _recheck_timing_before_execute

    window_ts = 1_776_800_000
    close_ts = window_ts + 300

    reason = _recheck_timing_before_execute(
        _window_key(window_ts),
        _decision(metadata={"min_offset_sec": 30}),
        now_fn=lambda: float(close_ts),
    )
    assert reason is not None
    assert reason.startswith("eval_offset_past_close")


def test_execute_uses_strategy_gate_params_min():
    """min_offset_sec from nested gate_params metadata overrides default."""
    from use_cases.execute_trade import _recheck_timing_before_execute

    window_ts = 1_776_800_000
    close_ts = window_ts + 300
    now = close_ts - 45  # T-45

    # Strategy declares min=60 in metadata.gate_params — T-45 must block
    reason = _recheck_timing_before_execute(
        _window_key(window_ts),
        _decision(metadata={"gate_params": {"min_offset_sec": 60}}),
        now_fn=lambda: float(now),
    )
    assert reason is not None
    assert reason.startswith("eval_offset_drift")
    assert "min=60s" in reason


def test_cross_window_guard_blocks_15m_timeframe():
    """close_ts is derived from timeframe — 15m window past its own close
    must still be caught even if the wall clock would still be inside a
    hypothetical 5m window."""
    from use_cases.execute_trade import _recheck_timing_before_execute

    window_ts = 1_776_800_000
    # 15m → close_ts = window_ts + 900; advance the clock past that
    now = window_ts + 905

    reason = _recheck_timing_before_execute(
        _window_key(window_ts, timeframe="15m"),
        _decision(),
        now_fn=lambda: float(now),
    )
    assert reason is not None
    assert reason.startswith("eval_offset_past_close")


def test_missing_window_ts_falls_open():
    """Degenerate case — if we cannot compute timing (no window_ts), the
    recheck is a no-op and defers to strategy gates, so we don't block
    every trade by accident."""
    from use_cases.execute_trade import _recheck_timing_before_execute

    # WindowKey validates window_ts > 0 at construction, so fake a
    # duck-typed object to exercise the fall-open path.
    class _FakeKey:
        window_ts = 0
        timeframe = "5m"
        duration_secs = 0

    reason = _recheck_timing_before_execute(
        _FakeKey(),  # type: ignore[arg-type]
        _decision(),
        now_fn=lambda: 1_776_800_000.0,
    )
    assert reason is None


def test_default_min_offset_used_when_metadata_missing():
    """When metadata doesn't declare min_offset_sec, the helper uses the
    module default (30s)."""
    from use_cases.execute_trade import (
        _recheck_timing_before_execute,
        _EVAL_OFFSET_MIN_DEFAULT,
    )

    assert _EVAL_OFFSET_MIN_DEFAULT == 30

    window_ts = 1_776_800_000
    close_ts = window_ts + 300
    now = close_ts - 20  # below 30 default

    reason = _recheck_timing_before_execute(
        _window_key(window_ts),
        _decision(metadata={}),
        now_fn=lambda: float(now),
    )
    assert reason is not None
    assert reason.startswith("eval_offset_drift")


# ─── Integration-ish wiring tests against ExecuteTradeUseCase.execute ────


def _build_use_case(*, clock: _FakeClock):
    """Build ExecuteTradeUseCase with mocks. Mirrors the helper in
    test_execute_trade but injects a FakeClock."""
    from use_cases.execute_trade import ExecuteTradeUseCase

    fill = ExecutionResult(
        success=True,
        order_id="paper-xyz",
        fill_price=0.55,
        fill_size=18.18,
        stake_usd=10.0,
        fee_usd=0.18,
        execution_mode="paper",
        fak_attempts=0,
        fak_prices=[],
        token_id="0xUP",
        execution_start=clock.now(),
        execution_end=clock.now() + 0.5,
    )

    risk = RiskStatus(
        current_bankroll=500.0,
        peak_bankroll=520.0,
        drawdown_pct=0.04,
        daily_pnl=5.0,
        consecutive_losses=0,
        paper_mode=True,
        kill_switch_active=False,
    )

    mock_poly = AsyncMock()
    mock_executor = AsyncMock()
    mock_executor.execute_order.return_value = fill
    mock_risk = MagicMock()
    mock_risk.get_status.return_value = risk
    mock_window_state = AsyncMock()
    mock_window_state.try_claim_trade.return_value = True
    mock_window_state.was_traded.return_value = False
    mock_alerter = AsyncMock()
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
    return uc, mock_executor, mock_window_state


@pytest.mark.asyncio
async def test_execute_blocks_when_wall_clock_past_close():
    """Integration: if ExecuteTradeUseCase is called when the wall clock
    is past the window's close, it must NOT hit the executor; it must
    return failure_reason='eval_offset_past_close...' and release the
    dedup claim."""
    window_ts = 1_776_800_000
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts + 5)  # 5s past close

    uc, mock_executor, mock_window_state = _build_use_case(clock=clock)
    decision = _decision(metadata={"min_offset_sec": 30})
    market = _market(window_ts)

    result = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84000.0,
        open_price=84100.0,
    )

    assert result.success is False
    assert (result.failure_reason or "").startswith("eval_offset_past_close")
    mock_executor.execute_order.assert_not_called()
    # Claim was released so future strategies are free to attempt again
    mock_window_state.clear_trade_claim.assert_called_once()


@pytest.mark.asyncio
async def test_execute_blocks_when_below_min_offset():
    """Integration: T-15 against min_offset=30 must short-circuit at
    the recheck and not hit the executor."""
    window_ts = 1_776_800_000
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 15)  # T-15

    uc, mock_executor, mock_window_state = _build_use_case(clock=clock)
    decision = _decision(metadata={"min_offset_sec": 30})
    market = _market(window_ts)

    result = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84000.0,
        open_price=84100.0,
    )

    assert result.success is False
    assert (result.failure_reason or "").startswith("eval_offset_drift")
    mock_executor.execute_order.assert_not_called()


@pytest.mark.asyncio
async def test_execute_proceeds_when_timing_ok():
    """Integration: at T-90 with min_offset=30 the trade must pass the
    recheck and reach the executor."""
    window_ts = 1_776_800_000
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 90)  # T-90

    uc, mock_executor, _ = _build_use_case(clock=clock)
    decision = _decision(metadata={"min_offset_sec": 30})
    market = _market(window_ts)

    result = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84000.0,
        open_price=84100.0,
    )

    assert result.success is True
    mock_executor.execute_order.assert_called_once()
