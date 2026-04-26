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


def test_execute_does_not_abort_below_min_offset():
    """T-15 against min=30 used to return eval_offset_drift, but the
    drift check was deliberately disabled (PR 7e31330) because it caused
    false blocks for strategies whose own gate fires near min_offset
    (3-tick entry confirmation can burn ~6s after the strategy gate
    passes at T-24). Only ``eval_offset_past_close`` remains enforced.

    See audit #319 — live evidence (2026-04-26) shows enforcing drift
    here breaks v9_lgb_only / v10_lgb_only entirely. The strategy's own
    timing gate is the authority on min_offset; this layer is the
    past-close safety net only.
    """
    from use_cases.execute_trade import _recheck_timing_before_execute

    window_ts = 1_776_800_000
    close_ts = window_ts + 300
    now = close_ts - 15  # T-15

    reason = _recheck_timing_before_execute(
        _window_key(window_ts),
        _decision(metadata={"min_offset_sec": 30}),
        now_fn=lambda: float(now),
    )
    # Drift check disabled — recheck is past-close only, T-15 is inside
    # the window so it must fall open.
    assert reason is None


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


def test_execute_does_not_block_on_gate_params_min():
    """Same as test_execute_does_not_abort_below_min_offset, but for the
    nested ``gate_params`` metadata path. The recheck must remain a
    past-close-only gate; min-offset enforcement is the strategy's
    responsibility (see audit #319)."""
    from use_cases.execute_trade import _recheck_timing_before_execute

    window_ts = 1_776_800_000
    close_ts = window_ts + 300
    now = close_ts - 45  # T-45 (inside window)

    reason = _recheck_timing_before_execute(
        _window_key(window_ts),
        _decision(metadata={"gate_params": {"min_offset_sec": 60}}),
        now_fn=lambda: float(now),
    )
    assert reason is None


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


def test_default_min_offset_constant_still_30s():
    """The default min_offset constant is parsed from env on import; we
    pin it to 30s for log/diagnostic stability. The recheck no longer
    uses this value to block (drift check disabled), but the constant
    remains read so future re-enables don't need a code change."""
    from use_cases.execute_trade import _EVAL_OFFSET_MIN_DEFAULT

    assert _EVAL_OFFSET_MIN_DEFAULT == 30


def test_inside_window_with_no_metadata_passes():
    """No metadata → no min_offset declared → recheck must still fall
    open inside the window (only past-close blocks)."""
    from use_cases.execute_trade import _recheck_timing_before_execute

    window_ts = 1_776_800_000
    close_ts = window_ts + 300
    now = close_ts - 20  # T-20 (still inside)

    reason = _recheck_timing_before_execute(
        _window_key(window_ts),
        _decision(metadata={}),
        now_fn=lambda: float(now),
    )
    assert reason is None


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
    # Audit #320: try_claim_trade returns (bool, claim_id) tuple.
    mock_window_state.try_claim_trade.return_value = (True, "test-claim-id")
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
    is past the window's close, it must NOT hit the executor.

    Audit #317 reopen (2026-04-26): the timing recheck runs BEFORE
    try_claim_trade, so a stale-surface eval short-circuits with zero
    lease activity. The pre-fix expectation that clear_trade_claim be
    called is no longer correct — we never acquired in the first place,
    so there is nothing to clear. Verifying that try_claim_trade is
    NOT called is the stronger invariant: the dedup state is never
    poisoned by stale-surface attempts.
    """
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
    # CRITICAL: the lease must NEVER be acquired on past-close attempts.
    # Pre-fix this branch acquired then tried to release, and any release
    # bug poisoned dedup state for the next 15s. See audit #317 second
    # occurrence forensics.
    mock_window_state.try_claim_trade.assert_not_called()
    mock_window_state.clear_trade_claim.assert_not_called()


@pytest.mark.asyncio
async def test_execute_does_not_block_when_below_min_offset():
    """Integration: T-15 against min_offset=30 used to block at the
    recheck. After PR 7e31330 / audit #319 the drift check is disabled,
    so the executor IS reached and a successful (paper) fill happens.

    The strategy's own timing gate is the authority on min_offset; the
    recheck only catches past-close drift.
    """
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

    # Drift check disabled — executor IS hit, paper fill succeeds.
    assert result.success is True
    assert result.failure_reason is None
    mock_executor.execute_order.assert_called_once()


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
