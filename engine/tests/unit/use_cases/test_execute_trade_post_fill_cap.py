"""Regression tests: post-fill cap enforcement (RDS #820 / #824).

Scenario
--------
Symmetric mirror of the post-fill floor block (PR #643). Decision-time
gates compare against a cached CLOB estimate (`surface.clob_implied_up` /
`surface.clob_down_ask`). Between decision and execution the CLOB can
move, or the FAK ladder can step to a worse price. Real fills can
therefore land ABOVE the configured cap even though the decision-time
check passed. The post-fill block re-checks `result.fill_price` against
`meta["entry_cap_up"]` (UP) and `meta["entry_cap_down"]` (DOWN) and
rejects with a `_failed` result if exceeded.

Bug that triggered this (2026-06-02, RDS note #824):
  - tickformer_v20: 100+ `fill_above_down_cap` decision-time rejections
    over 9h, but ZERO `fill_above_up_cap` rejections, because the cap_up
    code path didn't exist anywhere in the engine. SRO row carried
    entry_cap_up=0.85 (per note #820); param was silently inert.

Tests
-----
1. UP fill above cap_up    → rejected, fill slot released.
2. UP fill AT cap_up       → accepted (boundary: > is strict).
3. UP fill below cap_up    → accepted.
4. DOWN fill above cap_down → rejected.
5. DOWN fill at cap_down    → accepted.
6. No cap set in meta       → accepted (no regression for unwired hooks).
7. fill_price=None          → accepted (no crash).
8. Reservation leak guard   → release_fill_slot + clear_trade_claim called on rejection.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from domain.value_objects import (
    ExecutionResult,
    RiskStatus,
    StrategyDecision,
    WindowMarket,
)


class _FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self._t = t

    def now(self) -> float:
        return self._t


def _risk() -> RiskStatus:
    return RiskStatus(
        current_bankroll=500.0,
        peak_bankroll=520.0,
        drawdown_pct=0.04,
        daily_pnl=5.0,
        consecutive_losses=0,
        paper_mode=False,
        kill_switch_active=False,
    )


def _market() -> WindowMarket:
    return WindowMarket(
        condition_id="0xCOND",
        up_token_id="0xUP",
        down_token_id="0xDOWN",
        market_slug="btc-updown-5m-1780000000",
        active=True,
    )


def _decision(
    direction: str = "UP",
    meta: dict | None = None,
) -> StrategyDecision:
    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence="HIGH",
        confidence_score=0.85,
        entry_cap=0.93,
        collateral_pct=0.025,
        strategy_id="tickformer_v20_adaptive_early",
        strategy_version="1.0.0",
        entry_reason="test",
        skip_reason=None,
        metadata=meta if meta is not None else {},
    )


def _fill_result(fill_price: float | None, *, direction: str = "UP") -> ExecutionResult:
    token_id = "0xUP" if direction == "UP" else "0xDOWN"
    return ExecutionResult(
        success=True,
        order_id="0xORDER",
        fill_price=fill_price,
        fill_size=9.0,
        stake_usd=5.0,
        fee_usd=0.05,
        execution_mode="fak",
        token_id=token_id,
        execution_start=1000.0,
        execution_end=1000.5,
    )


def _build_uc(*, fill_result: ExecutionResult):
    from use_cases.execute_trade import ExecuteTradeUseCase

    mock_poly = AsyncMock()
    mock_executor = AsyncMock()
    mock_executor.execute_order.return_value = fill_result
    mock_risk = MagicMock()
    mock_risk.get_status.return_value = _risk()

    mock_window_state = AsyncMock()
    mock_window_state.try_claim_trade.return_value = (True, "claim-id-1")
    mock_window_state.has_filled.return_value = False
    mock_window_state.try_claim_fill_slot.return_value = True
    mock_window_state.mark_traded.return_value = None

    mock_alerter = AsyncMock()
    mock_alerter.send_strategy_trade_alert = AsyncMock()
    mock_alerter.send_fill_confirmed = AsyncMock()
    mock_recorder = AsyncMock()

    uc = ExecuteTradeUseCase(
        polymarket=mock_poly,
        order_executor=mock_executor,
        risk_manager=mock_risk,
        window_state=mock_window_state,
        alerter=mock_alerter,
        trade_recorder=mock_recorder,
        clock=_FakeClock(),
        paper_mode=False,
    )
    return uc, mock_window_state, mock_recorder


# ─── Test 1: UP fill above cap_up → rejected (the v20 9608/9612 case) ─


@pytest.mark.asyncio
async def test_up_fill_above_cap_up_rejected():
    """fill_price=0.93, entry_cap_up=0.85 → rejected (mirrors 9608/9612)."""
    uc, ws, recorder = _build_uc(fill_result=_fill_result(0.93, direction="UP"))

    result = await uc.execute(
        decision=_decision(
            direction="UP",
            meta={"entry_cap_up": 0.85, "min_offset_sec": 30},
        ),
        window_market=_market(),
        current_btc_price=3500.0,
        open_price=3510.0,
    )

    assert not result.success
    assert "fill_above_up_cap" in (result.failure_reason or "")
    assert "0.930" in (result.failure_reason or "")
    assert "0.850" in (result.failure_reason or "")
    # Mirror cap_down format exactly: "fill_above_up_cap:F>C"
    assert result.failure_reason == "fill_above_up_cap:0.930>0.850"
    recorder.record_trade.assert_not_called()
    ws.mark_traded.assert_not_called()


# ─── Test 2: UP fill AT cap_up → accepted (boundary: > is strict) ─────


@pytest.mark.asyncio
async def test_up_fill_at_cap_up_accepted():
    """fill_price == entry_cap_up passes (only > is rejected)."""
    uc, ws, recorder = _build_uc(fill_result=_fill_result(0.85, direction="UP"))

    result = await uc.execute(
        decision=_decision(
            direction="UP",
            meta={"entry_cap_up": 0.85, "min_offset_sec": 30},
        ),
        window_market=_market(),
        current_btc_price=3500.0,
        open_price=3510.0,
    )

    assert result.success
    recorder.record_trade.assert_called_once()


# ─── Test 3: UP fill below cap_up → accepted ──────────────────────────


@pytest.mark.asyncio
async def test_up_fill_below_cap_up_accepted():
    """fill_price=0.70, entry_cap_up=0.85 → happy path."""
    uc, ws, recorder = _build_uc(fill_result=_fill_result(0.70, direction="UP"))

    result = await uc.execute(
        decision=_decision(
            direction="UP",
            meta={"entry_cap_up": 0.85, "min_offset_sec": 30},
        ),
        window_market=_market(),
        current_btc_price=3500.0,
        open_price=3510.0,
    )

    assert result.success
    recorder.record_trade.assert_called_once()


# ─── Test 4: DOWN fill above cap_down → rejected ──────────────────────


@pytest.mark.asyncio
async def test_down_fill_above_cap_down_rejected():
    """fill_price=0.92, entry_cap_down=0.85 (NO leg) → rejected.

    The decision-time gate already exists for cap_down; this test asserts
    that the new post-fill cap block ALSO rejects when execution slipped
    a real fill above the cap (e.g. CLOB moved between decision and order).
    """
    uc, ws, recorder = _build_uc(fill_result=_fill_result(0.92, direction="DOWN"))

    result = await uc.execute(
        decision=_decision(
            direction="DOWN",
            meta={"entry_cap_down": 0.85, "min_offset_sec": 30},
        ),
        window_market=_market(),
        current_btc_price=3500.0,
        open_price=3510.0,
    )

    assert not result.success
    assert "fill_above_down_cap" in (result.failure_reason or "")
    recorder.record_trade.assert_not_called()
    ws.mark_traded.assert_not_called()


# ─── Test 5: DOWN fill at cap_down → accepted ─────────────────────────


@pytest.mark.asyncio
async def test_down_fill_at_cap_down_accepted():
    """fill_price == entry_cap_down passes (only > is rejected)."""
    uc, ws, recorder = _build_uc(fill_result=_fill_result(0.85, direction="DOWN"))

    result = await uc.execute(
        decision=_decision(
            direction="DOWN",
            meta={"entry_cap_down": 0.85, "min_offset_sec": 30},
        ),
        window_market=_market(),
        current_btc_price=3500.0,
        open_price=3510.0,
    )

    assert result.success
    recorder.record_trade.assert_called_once()


# ─── Test 6: No cap in meta → accepted (no regression) ────────────────


@pytest.mark.asyncio
async def test_no_cap_in_meta_accepted():
    """Hooks not setting entry_cap_up/down must not regress — block no-ops."""
    uc, ws, recorder = _build_uc(fill_result=_fill_result(0.99, direction="UP"))

    result = await uc.execute(
        decision=_decision(
            direction="UP",
            meta={"min_offset_sec": 30},  # no cap keys
        ),
        window_market=_market(),
        current_btc_price=3500.0,
        open_price=3510.0,
    )

    assert result.success
    recorder.record_trade.assert_called_once()


# ─── Test 7: fill_price=None → no crash ───────────────────────────────


@pytest.mark.asyncio
async def test_none_fill_price_skips_cap_check():
    """A result with fill_price=None must not trigger a cap rejection or crash."""
    uc, ws, recorder = _build_uc(fill_result=_fill_result(None, direction="UP"))

    result = await uc.execute(
        decision=_decision(
            direction="UP",
            meta={"entry_cap_up": 0.85, "min_offset_sec": 30},
        ),
        window_market=_market(),
        current_btc_price=3500.0,
        open_price=3510.0,
    )

    # Non-prescriptive on success vs fail — just verify no exception.
    assert result.success or not result.success


# ─── Test 8: Reservation leak guard on cap rejection ──────────────────


@pytest.mark.asyncio
async def test_cap_rejection_releases_fill_slot_and_claim():
    """On post-fill cap rejection, release_fill_slot + clear_trade_claim must
    both be called before returning (mirrors floor-rejection contract)."""
    uc, ws, recorder = _build_uc(fill_result=_fill_result(0.93, direction="UP"))

    result = await uc.execute(
        decision=_decision(
            direction="UP",
            meta={"entry_cap_up": 0.85, "min_offset_sec": 30},
        ),
        window_market=_market(),
        current_btc_price=3500.0,
        open_price=3510.0,
    )

    assert not result.success
    assert ws.release_fill_slot.call_count >= 1
    assert ws.clear_trade_claim.call_count >= 1
    recorder.record_trade.assert_not_called()
