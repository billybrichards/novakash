"""Writer-bypass root-cause regression (2026-05-21).

Disaster window 1779336900 (2026-05-21 04:18 UTC) leaked ~$42.65 to
on-chain GTC fills that produced ZERO trades rows. Polymarket
data-api ``/activity`` showed 3 distinct buy transactions for the
proxy on that conditionId; ``trades`` table held only 2 rows ($26.70
of stake). PR #561's sub-fill writer only handles the
``mark_traded → SECONDARY_FILL`` case; the ACTUAL drop was earlier:
``DBTradeRecorder.record_trade`` short-circuited with
``trade_recorder.phantom_rejected`` when ``execution_mode='gtc_resting'``
and ``fill_price=None``. That branch ate the row entirely and the
reconciler had nothing to match a later on-chain fill against.

The fix: ALWAYS record a row for a successful ``execute_order``. A
provisional ``gtc_resting`` row carries ``fill_price=None,
fill_size=None`` until either:

  * the reconciler matches an on-chain fill by ``clob_order_id`` and
    stamps ``fill_price`` / ``fill_size`` / ``polymarket_tx_hash``; or
  * the ``v59_mark_phantom_trades`` migration sweeps the row to
    ``status='PHANTOM'`` after the window closes unfilled.

Exposure queries already filter ``COALESCE(fill_size, 0) > 0`` so a
provisional row never inflates the cap, and PnL queries exclude
PHANTOM rows so WR stays clean.

These tests assert the writer-bypass is closed at the recorder level.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from adapters.execution.trade_recorder import DBTradeRecorder
from domain.value_objects import (
    ExecutionResult,
    StakeCalculation,
    StrategyDecision,
)


# ── Fixtures ─────────────────────────────────────────────────────────────


def _decision(strategy_id: str = "v9_2_raw_lgb") -> StrategyDecision:
    return StrategyDecision(
        action="TRADE",
        direction="UP",
        confidence="HIGH",
        confidence_score=0.78,
        entry_cap=0.91,
        collateral_pct=0.025,
        strategy_id=strategy_id,
        strategy_version="1.0.0",
        entry_reason=f"{strategy_id}_writer_bypass_regression",
        skip_reason=None,
        metadata={"window_ts": 1779336900, "regime": "CASCADE"},
    )


def _stake() -> StakeCalculation:
    return StakeCalculation(
        base_stake=13.35,
        price_multiplier=1.0,
        adjusted_stake=13.35,
        bankroll=500.0,
        bet_fraction=0.025,
        hard_cap=25.0,
    )


def _gtc_resting_result(order_id: str = "0xGTC_RESTING_ORDER_ID") -> ExecutionResult:
    """An ExecutionResult mimicking what FAKLadderExecutor returns when a
    GTC was placed and is sitting on the book unfilled at poll end."""
    return ExecutionResult(
        success=True,                       # placement succeeded
        order_id=order_id,                  # this IS the clob_order_id the reconciler matches on
        fill_price=None,                    # not yet filled
        fill_size=None,                     # not yet filled
        stake_usd=13.35,                    # what we asked to spend
        fee_usd=0.0,
        execution_mode="gtc_resting",
        fak_attempts=4,
        fak_prices=[0.91, 0.93, 0.95, 0.98],
        token_id="0xUP",
        market_slug="btc-updown-5m-1779336900",
    )


def _filled_fak_result() -> ExecutionResult:
    """An ExecutionResult mimicking a successful FAK fill."""
    return ExecutionResult(
        success=True,
        order_id="0xFAK_FILLED",
        fill_price=0.90,
        fill_size=14.85,
        stake_usd=13.35,
        fee_usd=0.087,
        execution_mode="fak",
        fak_attempts=1,
        fak_prices=[0.91],
        token_id="0xUP",
        market_slug="btc-updown-5m-1779336900",
    )


class _FakeStrategyDecisionRepo:
    def __init__(self) -> None:
        self.mark_executed_calls: list[dict] = []

    async def mark_executed(self, **kwargs) -> None:
        self.mark_executed_calls.append(kwargs)


class _FakeOrderManager:
    def __init__(self) -> None:
        self.register_calls: list = []

    async def register_order(self, order) -> None:
        self.register_calls.append(order)


# ── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gtc_resting_writes_provisional_row():
    """ROOT-CAUSE FIX: every successful execute_order MUST produce a
    trades row. Pre-fix the recorder returned early for ``gtc_resting``
    with ``fill_price=None`` (the ``phantom_rejected`` branch) and the
    on-chain fill that landed 30-60s later had no DB row to be matched
    to. This is THE writer bypass we're closing.
    """
    sd_repo = _FakeStrategyDecisionRepo()
    om = _FakeOrderManager()
    db = MagicMock()
    db.update_window_trade_placed = AsyncMock()

    recorder = DBTradeRecorder(
        db_client=db,
        order_manager=om,
        strategy_decision_repo=sd_repo,
    )

    await recorder.record_trade(_decision(), _gtc_resting_result(), _stake())

    # 1. OrderManager.register_order MUST be called — that's the path
    #    that lands the row in the `trades` table via DBClient.
    assert len(om.register_calls) == 1, (
        "Pre-fix behaviour leaked the order at the recorder. Every "
        "successful execute_order must reach OrderManager so a row is "
        "written that the reconciler can later match against an on-chain fill."
    )
    order = om.register_calls[0]
    assert order.order_id == "0xGTC_RESTING_ORDER_ID"
    # Provisional fields: stake_usd is set (planned spend), fill_price stays None.
    assert order.fill_price is None
    assert order.stake_usd == pytest.approx(13.35)
    # execution_mode flows into metadata so downstream filters
    # (`fill_size > 0`, ``v59_mark_phantom_trades``) can identify
    # the row as provisional / phantom-candidate.
    assert order.metadata["execution_mode"] == "gtc_resting"

    # 2. window_snapshot.trade_placed gets set — same as a real fill.
    db.update_window_trade_placed.assert_awaited_once()

    # 3. strategy_decisions.executed gets flipped with the GTC's
    #    clob_order_id, so the per-(strategy, window) audit row carries
    #    the same identifier the reconciler will later match on.
    #    fill_price / fill_size stay NULL until that match lands.
    assert len(sd_repo.mark_executed_calls) == 1
    sd = sd_repo.mark_executed_calls[0]
    assert sd["strategy_id"] == "v9_2_raw_lgb"
    assert sd["order_id"] == "0xGTC_RESTING_ORDER_ID"
    assert sd["fill_price"] is None
    assert sd["fill_size"] is None


@pytest.mark.asyncio
async def test_fak_filled_path_unchanged():
    """Sanity: the FAK happy-path is byte-identical to pre-fix behaviour.

    The BTC 5m hot path must stay byte-identical — only the
    ``gtc_resting`` / ``gtc`` branch changes. A normal FAK fill with
    ``fill_price=0.90, fill_size=14.85`` still writes a fully-populated
    row.
    """
    sd_repo = _FakeStrategyDecisionRepo()
    om = _FakeOrderManager()
    db = MagicMock()
    db.update_window_trade_placed = AsyncMock()

    recorder = DBTradeRecorder(
        db_client=db,
        order_manager=om,
        strategy_decision_repo=sd_repo,
    )

    await recorder.record_trade(_decision(), _filled_fak_result(), _stake())

    assert len(om.register_calls) == 1
    order = om.register_calls[0]
    assert order.fill_price == pytest.approx(0.90)
    assert order.metadata["fill_size"] == pytest.approx(14.85)
    assert order.metadata["execution_mode"] == "fak"

    assert len(sd_repo.mark_executed_calls) == 1
    sd = sd_repo.mark_executed_calls[0]
    assert sd["fill_price"] == pytest.approx(0.90)
    assert sd["fill_size"] == pytest.approx(14.85)


@pytest.mark.asyncio
async def test_unsuccessful_result_still_skipped():
    """A failed execute_order (success=False) is still skipped — we only
    record on the success branch."""
    sd_repo = _FakeStrategyDecisionRepo()
    om = _FakeOrderManager()
    db = MagicMock()
    db.update_window_trade_placed = AsyncMock()

    recorder = DBTradeRecorder(
        db_client=db,
        order_manager=om,
        strategy_decision_repo=sd_repo,
    )

    failed = ExecutionResult(
        success=False,
        order_id=None,
        fill_price=None,
        fill_size=None,
        stake_usd=0.0,
        fee_usd=0.0,
        execution_mode="none",
        fak_attempts=0,
        fak_prices=[],
        failure_reason="fak_exhausted",
        token_id="0xUP",
        market_slug="btc-updown-5m-1779336900",
    )

    await recorder.record_trade(_decision(), failed, _stake())

    assert om.register_calls == []
    assert sd_repo.mark_executed_calls == []
    db.update_window_trade_placed.assert_not_called()


@pytest.mark.asyncio
async def test_plain_gtc_mode_also_records():
    """The legacy ``gtc`` execution_mode (pre-resting branch) must also
    record. Pre-fix it shared the phantom-rejected guard with
    ``gtc_resting``."""
    sd_repo = _FakeStrategyDecisionRepo()
    om = _FakeOrderManager()
    db = MagicMock()
    db.update_window_trade_placed = AsyncMock()

    recorder = DBTradeRecorder(
        db_client=db,
        order_manager=om,
        strategy_decision_repo=sd_repo,
    )

    plain_gtc = ExecutionResult(
        success=True,
        order_id="0xPLAIN_GTC",
        fill_price=None,
        fill_size=None,
        stake_usd=4.0,
        fee_usd=0.0,
        execution_mode="gtc",
        fak_attempts=0,
        fak_prices=[],
        token_id="0xUP",
        market_slug="btc-updown-5m-1779336900",
    )

    await recorder.record_trade(_decision(), plain_gtc, _stake())

    assert len(om.register_calls) == 1
    assert len(sd_repo.mark_executed_calls) == 1


@pytest.mark.asyncio
async def test_provisional_row_carries_clob_order_id():
    """The reconciler's match key is ``clob_order_id`` — the engine's
    ``order_id`` from the GTC placement IS that key. Verify it survives
    into both the OrderManager metadata and the strategy_decisions
    update so reconciliation can wire the on-chain fill back to the
    provisional row.
    """
    sd_repo = _FakeStrategyDecisionRepo()
    om = _FakeOrderManager()
    db = MagicMock()
    db.update_window_trade_placed = AsyncMock()

    recorder = DBTradeRecorder(
        db_client=db,
        order_manager=om,
        strategy_decision_repo=sd_repo,
    )

    await recorder.record_trade(
        _decision(),
        _gtc_resting_result(order_id="0xDISASTER_WINDOW_MYSTERY_TX"),
        _stake(),
    )

    order = om.register_calls[0]
    assert order.order_id == "0xDISASTER_WINDOW_MYSTERY_TX"

    sd = sd_repo.mark_executed_calls[0]
    assert sd["order_id"] == "0xDISASTER_WINDOW_MYSTERY_TX"
