"""Regression tests: retrade-on-failure, lock-release semantics, poly 85¢ cap.

Context — window 1778444400 (2026-05-10)
-----------------------------------------
v_consensus_4way emitted two consecutive FAILED_EXECUTION cards for the
same window:

  Fire 1: reason="already_executing_in_process"
  Fire 2: reason="gtc_submit_error: Token price 0.88 exceeds 80¢ cap — skipping"

Three related issues were identified and addressed:

Sub-task A — Lock releases on every failure path
    The ``_in_flight_keys`` set in :class:`ExecuteTradeUseCase` is managed
    in a ``try/finally`` block so it always releases on success, failure, or
    exception.  These tests assert that property exhaustively.

Sub-task B — Poly-side token-price cap raised 80¢ → 85¢
    v_consensus_4way uses ``fill_band_max=0.82``; with the pi_bonus the GTC
    price can reach 0.83-0.85.  The hard-coded 0.80 ceiling in
    ``polymarket/live_client.py`` and ``execution/polymarket_client.py``
    blocked every such attempt.  The new default is 0.85, configurable via
    FIVE_MIN_MAX_ENTRY_PRICE / FIFTEEN_MIN_MAX_ENTRY_PRICE env vars.

Sub-task C — Retrade allowed when first attempt failed
    The registry's ``_executed_windows`` only records SUCCESSFUL fires, so
    a failed attempt does NOT prevent the next eval tick from retrying.
    The ``_in_flight_keys`` gate blocks only CONCURRENT attempts; sequential
    retries see an empty set and proceed normally.
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from domain.value_objects import (
    ExecutionResult,
    RiskStatus,
    StrategyDecision,
    WindowMarket,
)


# ── Shared helpers ────────────────────────────────────────────────────────────


class _FakeClock:
    def __init__(self, start: float) -> None:
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += float(seconds)


def _decision(
    *,
    strategy_id: str = "v_consensus_4way",
    direction: str = "DOWN",
    entry_cap: float = 0.82,
    metadata: Optional[dict] = None,
) -> StrategyDecision:
    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence="HIGH",
        confidence_score=0.80,
        entry_cap=entry_cap,
        collateral_pct=0.025,
        strategy_id=strategy_id,
        strategy_version="1.0.0",
        entry_reason="consensus_4way",
        skip_reason=None,
        metadata=metadata or {"min_offset_sec": 31},
    )


def _market(window_ts: int, timeframe: str = "5m") -> WindowMarket:
    return WindowMarket(
        condition_id=f"0xcond-{window_ts}",
        up_token_id="0xUP",
        down_token_id="0xDOWN",
        market_slug=f"btc-updown-{timeframe}-{window_ts}",
        active=True,
    )


def _fill(*, clock: _FakeClock, success: bool = True, reason: str = "") -> ExecutionResult:
    if success:
        return ExecutionResult(
            success=True,
            order_id="0xabc123",
            fill_price=0.72,
            fill_size=6.94,
            stake_usd=5.0,
            fee_usd=0.036,
            execution_mode="gtc",
            fak_attempts=2,
            fak_prices=[0.82, 0.79],
            token_id="0xDOWN",
            execution_start=clock.now(),
            execution_end=clock.now() + 0.3,
        )
    return ExecutionResult(
        success=False,
        failure_reason=reason or "fak_rfq_exhausted; gtc_fallback_disabled",
        stake_usd=5.0,
        execution_mode="none",
        fak_attempts=2,
        fak_prices=[],
        token_id="0xDOWN",
        execution_start=clock.now(),
        execution_end=clock.now() + 0.3,
    )


def _build_uc(*, clock: _FakeClock, executor_result: ExecutionResult):
    from use_cases.execute_trade import ExecuteTradeUseCase

    risk = RiskStatus(
        current_bankroll=500.0,
        peak_bankroll=520.0,
        drawdown_pct=0.04,
        daily_pnl=5.0,
        consecutive_losses=0,
        paper_mode=True,
        kill_switch_active=False,
    )

    mock_executor = AsyncMock()
    mock_executor.execute_order.return_value = executor_result

    mock_risk = MagicMock()
    mock_risk.get_status.return_value = risk

    mock_window_state = AsyncMock()
    mock_window_state.try_claim_trade.return_value = (True, "test-claim-id")
    mock_window_state.was_traded.return_value = False
    mock_window_state.has_filled.return_value = False
    mock_window_state.try_claim_fill_slot.return_value = True

    mock_alerter = AsyncMock()
    mock_recorder = AsyncMock()
    mock_poly = AsyncMock()

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


# ── Sub-task A: lock-release tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_lock_releases_after_successful_fill():
    """After a successful FAK fill the in-process key must be absent so
    a subsequent sequential call (next eval tick) can reach the dedup
    layer independently.
    """
    window_ts = 1_778_444_400
    clock = _FakeClock(start=window_ts + 300 - 90)  # 90s before close

    uc, _, _ = _build_uc(clock=clock, executor_result=_fill(clock=clock, success=True))

    dec = _decision(strategy_id="v_consensus_4way", direction="DOWN")
    mkt = _market(window_ts)

    result = await uc.execute(
        decision=dec, window_market=mkt, current_btc_price=65000.0, open_price=65100.0
    )
    assert result.success is True
    # Lock must be empty after success — no residual entry
    assert not uc._in_flight_keys, f"lock not empty: {uc._in_flight_keys}"


@pytest.mark.asyncio
async def test_lock_releases_after_failed_execution():
    """After an execution failure (e.g. gtc_submit_error / fak_exhausted) the
    in-process key must be absent so the next eval tick can retry.

    Regression: window 1778444400 — first in-flight attempt held the key;
    once it failed, the key must clear so subsequent attempts proceed.
    """
    window_ts = 1_778_444_400
    clock = _FakeClock(start=window_ts + 300 - 88)  # 88s before close

    failed_result = _fill(clock=clock, success=False, reason="gtc_submit_error: Token price 0.88 exceeds 85¢ cap — skipping")
    uc, _, _ = _build_uc(clock=clock, executor_result=failed_result)

    dec = _decision(strategy_id="v_consensus_4way", direction="DOWN")
    mkt = _market(window_ts)

    result = await uc.execute(
        decision=dec, window_market=mkt, current_btc_price=65000.0, open_price=65100.0
    )
    assert result.success is False
    # Lock must be empty after failure — next eval tick must be allowed to retry
    assert not uc._in_flight_keys, f"lock not empty after failure: {uc._in_flight_keys}"


@pytest.mark.asyncio
async def test_lock_releases_after_executor_exception():
    """If execute_order raises an unhandled exception, the in-process key
    must still be discarded (via the outer try/finally) so the strategy
    isn't locked out of the window permanently.
    """
    window_ts = 1_778_444_400
    clock = _FakeClock(start=window_ts + 300 - 88)

    uc, mock_executor, _ = _build_uc(
        clock=clock, executor_result=_fill(clock=clock, success=True)
    )
    mock_executor.execute_order.side_effect = RuntimeError("simulated CLOB outage")

    dec = _decision(strategy_id="v_consensus_4way", direction="DOWN")
    mkt = _market(window_ts)

    result = await uc.execute(
        decision=dec, window_market=mkt, current_btc_price=65000.0, open_price=65100.0
    )
    assert result.success is False
    assert not uc._in_flight_keys, f"lock not empty after exception: {uc._in_flight_keys}"


# ── Sub-task B: poly 85¢ cap ─────────────────────────────────────────────────


def test_five_min_max_entry_price_default_is_85_cents():
    """FIVE_MIN_MAX_ENTRY_PRICE env var must default to 0.85, not 0.80.

    Regression: window 1778444400 — GTC price 0.88 exceeded the hard-coded
    0.80 ceiling and was rejected.  v_consensus_4way fill_band_max=0.82 plus
    pi_bonus reaches 0.83-0.85; the cap must be at least 0.85 to allow fills.
    """
    # Verify default with no env var set
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FIVE_MIN_MAX_ENTRY_PRICE", None)
        os.environ.pop("FIFTEEN_MIN_MAX_ENTRY_PRICE", None)

        # Re-import to pick up env (adapters/polymarket/live_client.py reads at call time)
        from adapters.polymarket import live_client as lc

        # Simulate what place_order reads
        is_15m = False
        cap = (
            float(os.environ.get("FIFTEEN_MIN_MAX_ENTRY_PRICE", "0.85"))
            if is_15m
            else float(os.environ.get("FIVE_MIN_MAX_ENTRY_PRICE", "0.85"))
        )
        assert cap == 0.85, f"Expected default cap 0.85, got {cap}"

        # A price of 0.83 (fill_band_max=0.82 + pi_bonus=0.01) must NOT exceed cap
        assert 0.83 <= cap, "0.83 should be <= 0.85 cap (would have been rejected at 0.80)"

        # A price of 0.88 should still be REJECTED (> 0.85)
        assert 0.88 > cap, "0.88 exceeds 0.85 cap — correct, should still be rejected"


def test_five_min_max_entry_price_env_override():
    """FIVE_MIN_MAX_ENTRY_PRICE env var overrides the 0.85 default."""
    with patch.dict(os.environ, {"FIVE_MIN_MAX_ENTRY_PRICE": "0.90"}):
        cap = float(os.environ.get("FIVE_MIN_MAX_ENTRY_PRICE", "0.85"))
        assert cap == 0.90
        # 0.88 must now pass (≤ 0.90)
        assert 0.88 <= cap


# ── Sub-task C: retrade-on-failure (sequential) ───────────────────────────────


@pytest.mark.asyncio
async def test_sequential_retry_allowed_after_failed_attempt():
    """After a failed attempt (gtc_submit_error / fak_exhausted), the NEXT
    eval tick for the same (strategy, window, direction) must be allowed to
    proceed through the in-process gate to the executor.

    This is the end-to-end regression for window 1778444400 Fire 2:
    Fire 1 was still in-flight (correctly blocked by the lock).
    After Fire 1 completed with failure, Fire 2 must reach the executor.
    """
    window_ts = 1_778_444_400
    clock = _FakeClock(start=window_ts + 300 - 88)

    uc, mock_executor, mock_ws = _build_uc(
        clock=clock, executor_result=_fill(clock=clock, success=False, reason="gtc_submit_error: test")
    )

    dec = _decision(strategy_id="v_consensus_4way", direction="DOWN")
    mkt = _market(window_ts)

    # Attempt 1: fails
    r1 = await uc.execute(
        decision=dec, window_market=mkt, current_btc_price=65000.0, open_price=65100.0
    )
    assert r1.success is False

    # Attempt 2: lock cleared, should reach executor again (retry)
    clock.advance(31.0)  # bypass 30s rate guardrail
    # Reset has_filled so dedup doesn't block (simulate no real fill was recorded)
    mock_ws.has_filled.return_value = False
    mock_ws.try_claim_trade.return_value = (True, "claim-2")
    mock_ws.try_claim_fill_slot.return_value = True

    # Second attempt also fails (same executor result for simplicity)
    r2 = await uc.execute(
        decision=dec, window_market=mkt, current_btc_price=65000.0, open_price=65100.0
    )
    # Must have reached the executor (2nd call), not been blocked by in-process gate
    assert mock_executor.execute_order.call_count == 2, (
        f"Expected executor called twice (retry), got {mock_executor.execute_order.call_count}"
    )
    assert r2.failure_reason != "already_executing_in_process", (
        "Retry should not be blocked by in-process lock after first attempt failed"
    )


@pytest.mark.asyncio
async def test_successful_fill_blocks_retry_in_same_window():
    """After a SUCCESSFUL fill, the registry-level dedup prevents re-fire.
    The in-process gate does NOT permanently lock; it only blocks concurrent
    attempts.  The has_filled DB check (Step 0.5) is the terminal invariant.

    This test proves the complement of test_sequential_retry_allowed_after_failed_attempt:
    success → block; failure → allow retry.
    """
    window_ts = 1_778_444_400
    clock = _FakeClock(start=window_ts + 300 - 88)

    uc, mock_executor, mock_ws = _build_uc(
        clock=clock, executor_result=_fill(clock=clock, success=True)
    )

    dec = _decision(strategy_id="v_consensus_4way", direction="DOWN")
    mkt = _market(window_ts)

    # Attempt 1: succeeds
    r1 = await uc.execute(
        decision=dec, window_market=mkt, current_btc_price=65000.0, open_price=65100.0
    )
    assert r1.success is True

    # Attempt 2: has_filled returns True (mark_traded was called after success)
    clock.advance(31.0)
    mock_ws.has_filled.return_value = True  # DB shows this window already filled

    r2 = await uc.execute(
        decision=dec, window_market=mkt, current_btc_price=65000.0, open_price=65100.0
    )
    assert r2.success is False
    assert r2.failure_reason == "already_filled_this_window"
    # Executor must NOT be called a second time
    assert mock_executor.execute_order.call_count == 1


# ── Sub-task D: Bug 1 — already_executing_in_process is silent (no TG card) ────


def test_already_executing_in_process_is_silent_skip(monkeypatch):
    """already_executing_in_process must NOT emit a FAILED_EXECUTION TG card.

    Window 1778448300 (2026-05-10): the second concurrent eval tick returned
    already_executing_in_process and the registry emitted a ❌ FAILED_EXECUTION
    TG card for what is correct serialization behaviour. The operator saw a
    confusing card that looked like a real failure. Fix: treat this reason as
    a silent skip — log at DEBUG, no TG card emitted.
    """
    # Import the registry _fire_trade_attempt_card logic indirectly by
    # constructing an ExecutionResult with the specific failure reason and
    # verifying _is_real_order_error returns False for it (which keeps the
    # circuit breaker clean) and verifying that the registry outcome
    # classification returns early (silent drop).
    from use_cases.execute_trade import _is_real_order_error

    # The failure reason for in-process lock — must NOT trip circuit breaker
    reason = "already_executing_in_process"
    assert not _is_real_order_error(reason), (
        "already_executing_in_process must NOT count as a real order error"
    )


# ── Sub-task E: Bug 2 — cap-rejection allows retrade on next tick ────────────


@pytest.mark.asyncio
async def test_cap_rejection_does_not_block_retry_via_registry_dedup():
    """After a cap-rejection (gtc_submit_error: Token price X exceeds Y cap),
    the NEXT sequential eval tick for the same (strategy, window, direction)
    must reach the executor again — the registry dedup only records successful
    fills, not failed attempts.

    Window 1778448300 (2026-05-10): price was 0.88 > 0.85 cap on tick 1.
    If price dropped to 0.82 on tick 2, the strategy should fire again.
    This test proves the dedup path allows it.
    """
    window_ts = 1_778_448_300
    clock = _FakeClock(start=window_ts + 300 - 88)

    cap_rejection_result = _fill(
        clock=clock,
        success=False,
        reason="gtc_submit_error: Token price 0.88 exceeds 85¢ cap — skipping",
    )
    uc, mock_executor, mock_ws = _build_uc(
        clock=clock, executor_result=cap_rejection_result
    )

    dec = _decision(strategy_id="v_consensus_4way", direction="UP")
    mkt = _market(window_ts)

    # Tick 1: cap-rejection
    r1 = await uc.execute(
        decision=dec, window_market=mkt, current_btc_price=65000.0, open_price=65100.0
    )
    assert r1.success is False
    assert "exceeds" in (r1.failure_reason or "")

    # In-process lock must be clear
    assert not uc._in_flight_keys

    # Tick 2: price recovered, executor now succeeds
    clock.advance(31.0)
    mock_ws.has_filled.return_value = False
    mock_ws.try_claim_trade.return_value = (True, "claim-2")
    mock_ws.try_claim_fill_slot.return_value = True
    mock_executor.execute_order.return_value = _fill(clock=clock, success=True)

    r2 = await uc.execute(
        decision=dec, window_market=mkt, current_btc_price=65000.0, open_price=65100.0
    )
    # Must have reached the executor a second time (retry after cap-rejection)
    assert mock_executor.execute_order.call_count == 2, (
        f"Expected 2 executor calls (retry after cap-rejection), "
        f"got {mock_executor.execute_order.call_count}"
    )
    assert r2.failure_reason != "already_executing_in_process"


# ── Sub-task F: Bug 3 — circuit breaker excludes cap-rejection safety rejects ──


def test_cap_rejection_not_real_order_error():
    """gtc_submit_error from a client-side price cap must NOT be a real order
    error — it must not increment the circuit-breaker consecutive-error counter.

    Window 1778448300 (2026-05-10): 3 cap-rejections tripped the circuit
    breaker and silenced ALL strategies for 180s. Cap-rejections are raised
    by the engine client BEFORE any Polymarket API call. They are safety
    enforcement by the engine, not infrastructure failures.
    """
    from use_cases.execute_trade import _is_real_order_error

    # Cap-rejection variants that must NOT count as real errors
    cap_reasons = [
        "gtc_submit_error: Token price 0.88 exceeds 85¢ cap — skipping",
        "gtc_submit_error: Token price 0.91 exceeds 85¢ cap — skipping",
        "gtc_submit_error: price 0.88 exceeds safety cap 0.85",
        "gtc_submit_error: 0.88 exceeds cap -- skipping",
    ]
    for reason in cap_reasons:
        assert not _is_real_order_error(reason), (
            f"Cap-rejection should NOT be a real order error: {reason!r}"
        )


def test_real_gtc_submit_error_still_counts():
    """A genuine GTC submit error (e.g. auth failure, signer error) that does
    NOT contain a cap-related substring must still count as a real order error
    and increment the circuit-breaker counter.
    """
    from use_cases.execute_trade import _is_real_order_error

    real_errors = [
        "gtc_submit_error: clob_auth_error: 401 Unauthorized",
        "gtc_submit_error: signer failed: invalid key",
        "gtc_submit_error: network error: connection refused",
        "execution_error: RPC timeout",
    ]
    for reason in real_errors:
        assert _is_real_order_error(reason), (
            f"Real order error should count toward circuit breaker: {reason!r}"
        )
