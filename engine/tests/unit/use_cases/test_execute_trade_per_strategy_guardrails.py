"""Regression tests for per-strategy rate-limit scoping.

Hub note #541 (2026-05-19) post-mortem of v12_meta_gate's LIVE failure:
26 (window_ts, direction) candidates qualified, only 6 fired. Forensics
showed 25+ rows of ``execute_trade.guardrail_blocked failure_reason=
rate_limit: Ns < 30s`` for ``v12_meta_gate`` — the 30-second
``MIN_ORDER_INTERVAL_S`` was being ticked by SIBLING LIVE strategies
(v9_2_raw_lgb, v9_2_iso_expand, v9_1_lgb_only) firing first on the same
or adjacent windows. The 20 skipped v12_meta_gate candidates would have
been 17W/3L = 85% WR; the 6 that fired were 2W/4L = 33% (adversely
selected, the losing tail).

Root cause: ``_last_order_time`` and ``_order_timestamps`` were
process-wide attributes on a single ``ExecuteTradeUseCase`` instance
shared by all strategies. The 30s guardrail was designed to prevent
SAME-strategy spam within a window, not to gate parallel strategies.

Fix: scope ``_last_order_time`` per strategy_id behind the
``EXECUTE_PER_STRATEGY_GUARDRAILS`` env flag (default on). The global
hourly cap (``MAX_ORDERS_PER_HOUR``) and the circuit breaker stay
process-wide as safety backstops.

These tests cover:
  1. A strategy that JUST fired cannot fire again within 30s (legacy
     SAME-strategy spam protection is preserved).
  2. A DIFFERENT strategy CAN fire within 30s of the first strategy's
     fill (Hub #541 fix — the cross-strategy collision is gone).
  3. The legacy global behaviour is restored when the env flag is set
     to ``false`` (emergency rollback escape hatch).
  4. The hourly cap stays GLOBAL — once 20 orders/hour across all
     strategies, every strategy is blocked.
"""

from __future__ import annotations

import os
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from domain.value_objects import (
    ExecutionResult,
    RiskStatus,
    StrategyDecision,
    WindowMarket,
)


class _FakeClock:
    def __init__(self, start: float) -> None:
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += float(seconds)


def _decision(
    *,
    strategy_id: str = "v12_meta_gate",
    direction: str = "DOWN",
    metadata: Optional[dict] = None,
) -> StrategyDecision:
    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence="HIGH",
        confidence_score=0.75,
        entry_cap=0.65,
        collateral_pct=0.025,
        strategy_id=strategy_id,
        strategy_version="1.0.0",
        entry_reason="test",
        skip_reason=None,
        metadata=metadata or {"min_offset_sec": 30},
    )


def _market(window_ts: int, timeframe: str = "5m") -> WindowMarket:
    return WindowMarket(
        condition_id=f"0xcond-{window_ts}",
        up_token_id="0xUP",
        down_token_id="0xDOWN",
        market_slug=f"btc-updown-{timeframe}-{window_ts}",
        active=True,
    )


def _build_use_case(*, clock: _FakeClock):
    """ExecuteTradeUseCase with mocks. Always returns success on order
    placement so the rate-limit state accumulates."""
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
        token_id="0xDOWN",
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
    mock_window_state.try_claim_trade.return_value = (True, "test-claim-id")
    mock_window_state.was_traded.return_value = False
    mock_window_state.has_filled.return_value = False
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


# ─── Per-strategy guardrails (default ON) ────────────────────────────────


@pytest.mark.asyncio
async def test_same_strategy_still_rate_limited_within_30s(monkeypatch):
    """Bug-preserve regression: SAME-strategy spam protection must remain.

    The 30s interval was designed to stop a single strategy from firing
    repeatedly in the same window. Hub #541's fix scopes the timer
    per-strategy but MUST NOT remove protection for the same strategy.
    """
    monkeypatch.setenv("EXECUTE_PER_STRATEGY_GUARDRAILS", "true")
    window_ts = 1_779_136_200
    close_ts = window_ts + 300
    clock = _FakeClock(start=close_ts - 200)

    uc, _exec, ws = _build_use_case(clock=clock)
    decision = _decision(strategy_id="v12_meta_gate")
    market = _market(window_ts)

    # First fire — succeeds and ticks the per-strategy clock.
    result1 = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84000.0,
        open_price=84100.0,
    )
    assert result1.success is True

    # Same strategy retries 5s later — must be rate-limited.
    ws.try_claim_trade.return_value = (True, "test-claim-id-2")
    ws.has_filled.return_value = False  # allow past Step 0.5
    clock.advance(5.0)
    result2 = await uc.execute(
        decision=decision,
        window_market=market,
        current_btc_price=84000.0,
        open_price=84100.0,
    )
    assert result2.success is False
    assert "rate_limit" in result2.failure_reason


@pytest.mark.asyncio
async def test_different_strategy_not_rate_limited_by_sibling_fire(monkeypatch):
    """Hub #541 fix: when v9_2_raw_lgb fires, v12_meta_gate trying to
    fire 5 seconds later on a different (window_ts, direction) MUST NOT
    hit ``rate_limit: 5.0s < 30s``.

    This is the smoking-gun regression. Before the fix, both strategies
    shared ``_last_order_time`` and the second was blocked for 30s.
    """
    monkeypatch.setenv("EXECUTE_PER_STRATEGY_GUARDRAILS", "true")
    window_ts_a = 1_779_136_200
    window_ts_b = 1_779_136_500  # next 5-min window
    close_ts = window_ts_a + 300
    clock = _FakeClock(start=close_ts - 200)

    uc, _exec, ws = _build_use_case(clock=clock)

    # v9_2_raw_lgb fires first.
    result_a = await uc.execute(
        decision=_decision(strategy_id="v9_2_raw_lgb", direction="DOWN"),
        window_market=_market(window_ts_a),
        current_btc_price=84000.0,
        open_price=84100.0,
    )
    assert result_a.success is True

    # Reset mocks for the second call.
    ws.try_claim_trade.return_value = (True, "test-claim-id-2")
    ws.has_filled.return_value = False

    # 5 seconds later, v12_meta_gate fires on a different window. Before
    # the fix this would have returned ``rate_limit: 5.0s < 30s``.
    clock.advance(5.0)
    result_b = await uc.execute(
        decision=_decision(strategy_id="v12_meta_gate", direction="UP"),
        window_market=_market(window_ts_b),
        current_btc_price=84000.0,
        open_price=84100.0,
    )
    assert result_b.success is True, (
        f"v12_meta_gate was blocked by v9_2_raw_lgb's clock: "
        f"{result_b.failure_reason!r} — Hub #541 regression"
    )


@pytest.mark.asyncio
async def test_third_strategy_also_unblocked(monkeypatch):
    """Three parallel LIVE strategies (v9_2_raw_lgb, v9_2_v12_combo,
    v12_meta_gate) all firing in quick succession should all succeed
    when each is firing on its own (window_ts, direction)."""
    monkeypatch.setenv("EXECUTE_PER_STRATEGY_GUARDRAILS", "true")
    base_window = 1_779_136_200
    close_ts = base_window + 300
    clock = _FakeClock(start=close_ts - 200)

    uc, _exec, ws = _build_use_case(clock=clock)

    for i, sid in enumerate([
        "v9_2_raw_lgb",
        "v9_2_v12_combo",
        "v12_meta_gate",
    ]):
        ws.try_claim_trade.return_value = (True, f"claim-{i}")
        ws.has_filled.return_value = False
        result = await uc.execute(
            decision=_decision(strategy_id=sid, direction="DOWN"),
            window_market=_market(base_window + 300 * i),
            current_btc_price=84000.0,
            open_price=84100.0,
        )
        assert result.success is True, (
            f"Strategy #{i} ({sid}) blocked: {result.failure_reason!r}"
        )
        clock.advance(3.0)  # 3s between fires — well under 30s


# ─── Legacy global behaviour (rollback escape hatch) ─────────────────────


@pytest.mark.asyncio
async def test_legacy_global_rate_limit_when_flag_disabled(monkeypatch):
    """Setting ``EXECUTE_PER_STRATEGY_GUARDRAILS=false`` reverts to the
    pre-fix global behaviour: a sibling strategy IS blocked. Acts as the
    operator escape hatch if the per-strategy fix ever needs to be
    reverted in prod without redeploy.
    """
    monkeypatch.setenv("EXECUTE_PER_STRATEGY_GUARDRAILS", "false")
    window_ts_a = 1_779_136_200
    window_ts_b = 1_779_136_500
    close_ts = window_ts_a + 300
    clock = _FakeClock(start=close_ts - 200)

    uc, _exec, ws = _build_use_case(clock=clock)
    assert uc._per_strategy_guardrails is False

    # First strategy fires.
    result_a = await uc.execute(
        decision=_decision(strategy_id="v9_2_raw_lgb"),
        window_market=_market(window_ts_a),
        current_btc_price=84000.0,
        open_price=84100.0,
    )
    assert result_a.success is True

    # Sibling fires 5s later → legacy global timer blocks it.
    ws.try_claim_trade.return_value = (True, "claim-2")
    ws.has_filled.return_value = False
    clock.advance(5.0)
    result_b = await uc.execute(
        decision=_decision(strategy_id="v12_meta_gate"),
        window_market=_market(window_ts_b),
        current_btc_price=84000.0,
        open_price=84100.0,
    )
    assert result_b.success is False
    assert "rate_limit" in result_b.failure_reason


# ─── Global hourly cap is preserved ──────────────────────────────────────


def test_hourly_cap_stays_global(monkeypatch):
    """The 20 orders/hour cap MUST remain process-wide regardless of the
    per-strategy flag. A runaway strategy stack should never exceed the
    cap by spreading fire across strategy_ids.

    This is a synchronous unit test of ``_check_guardrails`` because the
    full execute() flow is heavy-weight for a state-only assertion.
    """
    monkeypatch.setenv("EXECUTE_PER_STRATEGY_GUARDRAILS", "true")
    from use_cases.execute_trade import (
        ExecuteTradeUseCase,
        MAX_ORDERS_PER_HOUR,
    )

    clock = _FakeClock(start=1_000_000.0)
    # Build with minimal mocks — we only call _check_guardrails / _record.
    uc = ExecuteTradeUseCase(
        polymarket=AsyncMock(),
        order_executor=AsyncMock(),
        risk_manager=MagicMock(),
        window_state=AsyncMock(),
        alerter=AsyncMock(),
        trade_recorder=AsyncMock(),
        clock=clock,
        paper_mode=True,
    )

    # Fire MAX_ORDERS_PER_HOUR orders across DIFFERENT strategies, each
    # spaced 60s apart so the per-strategy interval never triggers.
    for i in range(MAX_ORDERS_PER_HOUR):
        ok, reason = uc._check_guardrails(strategy_id=f"strat_{i}")
        assert ok, f"Unexpected block on order {i}: {reason!r}"
        uc._record_order_placed(strategy_id=f"strat_{i}")
        clock.advance(60.0)

    # One more order — different strategy, but the GLOBAL hourly cap
    # should bite.
    ok, reason = uc._check_guardrails(strategy_id="strat_overflow")
    assert ok is False
    assert "rate_limit" in reason
    assert f">= {MAX_ORDERS_PER_HOUR}/hr" in reason


# ─── Circuit breaker stays global ────────────────────────────────────────


def test_circuit_breaker_stays_global(monkeypatch):
    """The circuit breaker indexes on CLOB/infra errors that affect every
    strategy — when it trips for v9_2_raw_lgb, v12_meta_gate must also be
    blocked. This is unchanged by the per-strategy fix.
    """
    monkeypatch.setenv("EXECUTE_PER_STRATEGY_GUARDRAILS", "true")
    from use_cases.execute_trade import ExecuteTradeUseCase

    clock = _FakeClock(start=1_000_000.0)
    uc = ExecuteTradeUseCase(
        polymarket=AsyncMock(),
        order_executor=AsyncMock(),
        risk_manager=MagicMock(),
        window_state=AsyncMock(),
        alerter=AsyncMock(),
        trade_recorder=AsyncMock(),
        clock=clock,
        paper_mode=True,
    )

    # Trip the breaker manually (simulating 3 consecutive errors).
    uc._circuit_break_until = clock.now() + 120.0

    # Any strategy is blocked, not just the one that caused the trip.
    for sid in ["v9_2_raw_lgb", "v12_meta_gate", "v9_2_v12_combo"]:
        ok, reason = uc._check_guardrails(strategy_id=sid)
        assert ok is False
        assert "circuit_breaker" in reason
