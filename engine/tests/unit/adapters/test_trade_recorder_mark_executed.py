"""Audit-task #255 F5 — tests for DBTradeRecorder's post-fill update of
strategy_decisions.executed / order_id / fill_price / fill_size.

The recorder receives a StrategyDecisionRepository and, after successfully
registering the order + flagging the window_snapshot, invokes
`mark_executed` with the fill details. Historical rows are untouched
(forward-only — no backfill).

Smoke-level coverage matching the user spec: an integration-style test
with a fake repo that records calls. We're not exercising real asyncpg
here — that path is covered by existing pg_strategy_decisions tests.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from adapters.execution.trade_recorder import DBTradeRecorder
from domain.value_objects import (
    ExecutionResult,
    StakeCalculation,
    StrategyDecision,
)


class _FakeRepo:
    """Minimal StrategyDecisionRepository stand-in that records calls."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def mark_executed(
        self,
        *,
        strategy_id: str,
        asset: str,
        window_ts: int,
        order_id,
        fill_price,
        fill_size,
    ) -> None:
        self.calls.append({
            "strategy_id": strategy_id,
            "asset": asset,
            "window_ts": window_ts,
            "order_id": order_id,
            "fill_price": fill_price,
            "fill_size": fill_size,
        })


def _decision() -> StrategyDecision:
    return StrategyDecision(
        action="TRADE",
        direction="UP",
        confidence="HIGH",
        confidence_score=0.82,
        entry_cap=0.73,
        collateral_pct=0.025,
        strategy_id="v6_sniper",
        strategy_version="6.0.4",
        entry_reason="pegged_path1",
        skip_reason=None,
        metadata={"window_ts": 1776399900, "regime": "TRENDING_UP"},
    )


def _result(market_slug: str = "BTC-5m-1776399900") -> ExecutionResult:
    return ExecutionResult(
        success=True,
        order_id="0xdeadbeef",
        fill_price=0.73,
        fill_size=5.0,
        stake_usd=4.0,
        fee_usd=0.0,
        execution_mode="fak",
        fak_attempts=1,
        fak_prices=[0.73],
        token_id="5555",
        market_slug=market_slug,
    )


def _stake() -> StakeCalculation:
    return StakeCalculation(
        base_stake=12.0,
        price_multiplier=1.0,
        adjusted_stake=12.5,
        bankroll=500.0,
        bet_fraction=0.025,
        hard_cap=50.0,
    )


@pytest.mark.asyncio
async def test_mark_executed_called_on_successful_fill():
    repo = _FakeRepo()
    db = MagicMock()
    db.update_window_trade_placed = AsyncMock()
    recorder = DBTradeRecorder(
        db_client=db,
        order_manager=None,
        strategy_decision_repo=repo,
    )

    await recorder.record_trade(_decision(), _result(), _stake())

    assert len(repo.calls) == 1
    c = repo.calls[0]
    assert c["strategy_id"] == "v6_sniper"
    assert c["asset"] == "BTC"
    assert c["window_ts"] == 1776399900
    assert c["order_id"] == "0xdeadbeef"
    assert c["fill_price"] == 0.73
    assert c["fill_size"] == 5.0


@pytest.mark.asyncio
async def test_mark_executed_skipped_when_no_repo():
    """The older wiring path (no strategy_decision_repo) is still supported.
    Recorder must not raise when repo is None."""
    db = MagicMock()
    db.update_window_trade_placed = AsyncMock()
    recorder = DBTradeRecorder(db_client=db, order_manager=None)
    # No crash — just a normal flow without the F5 write-back.
    await recorder.record_trade(_decision(), _result(), _stake())


@pytest.mark.asyncio
async def test_mark_executed_uses_metadata_window_ts_when_slug_fails():
    """If market_slug doesn't parse cleanly, fall back to metadata.window_ts."""
    repo = _FakeRepo()
    db = MagicMock()
    db.update_window_trade_placed = AsyncMock()
    recorder = DBTradeRecorder(
        db_client=db,
        order_manager=None,
        strategy_decision_repo=repo,
    )

    # market_slug without a trailing numeric segment — parsed last component
    # happens to be the numeric window anyway in this pattern. Use a
    # non-numeric suffix to force the fallback.
    result = _result(market_slug="weird-slug-no-ts")
    await recorder.record_trade(_decision(), result, _stake())
    assert len(repo.calls) == 1
    # metadata carries window_ts=1776399900 which must be picked up.
    assert repo.calls[0]["window_ts"] == 1776399900


@pytest.mark.asyncio
async def test_phantom_trade_does_not_mark_executed():
    """A phantom gtc_resting trade (success=True, fill_price=None) must
    NOT be recorded — we explicitly reject these upstream (incident
    2026-04-17). mark_executed must NOT fire."""
    repo = _FakeRepo()
    db = MagicMock()
    db.update_window_trade_placed = AsyncMock()
    recorder = DBTradeRecorder(
        db_client=db,
        order_manager=None,
        strategy_decision_repo=repo,
    )
    result = ExecutionResult(
        success=True,
        order_id="0xphantom",
        fill_price=None,         # phantom
        fill_size=None,
        stake_usd=4.0,
        fee_usd=0.0,
        execution_mode="gtc_resting",
        fak_attempts=0,
        fak_prices=[],
        token_id="",
        market_slug="BTC-5m-1776399900",
    )

    await recorder.record_trade(_decision(), result, _stake())

    # Recorder returned early — no strategy_decisions update either.
    assert repo.calls == []


@pytest.mark.asyncio
async def test_unsuccessful_result_does_not_mark_executed():
    repo = _FakeRepo()
    recorder = DBTradeRecorder(
        db_client=None,
        order_manager=None,
        strategy_decision_repo=repo,
    )
    result = ExecutionResult(
        success=False,
        order_id=None,
        fill_price=None,
        fill_size=None,
        stake_usd=0.0,
        fee_usd=0.0,
        execution_mode="",
        fak_attempts=0,
        fak_prices=[],
    )
    await recorder.record_trade(_decision(), result, _stake())
    assert repo.calls == []


@pytest.mark.asyncio
async def test_repo_error_does_not_propagate():
    """A DB hiccup on mark_executed must never block the trade flow."""

    class _RaisingRepo:
        async def mark_executed(self, **kwargs):
            raise RuntimeError("db down")

    db = MagicMock()
    db.update_window_trade_placed = AsyncMock()
    recorder = DBTradeRecorder(
        db_client=db,
        order_manager=None,
        strategy_decision_repo=_RaisingRepo(),
    )
    # Does not raise.
    await recorder.record_trade(_decision(), _result(), _stake())
