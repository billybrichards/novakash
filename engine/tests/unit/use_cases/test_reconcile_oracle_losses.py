"""Regression tests for ReconcileOracleLossesUseCase — RDS note #614.

Driver incident: 3 LIVE FAK trades (9124, 9130, 9131) sat outcome=NULL
for >10h despite their windows resolving DOWN hours earlier. Worthless
YES tokens never trigger on-chain redemption, so the auto-redeem path
(PR #575) cannot stamp them; and the legacy CLOB reconciler only sees
trades visible in data-api positions (which dropped these trades when
the WIN side of the condition auto-redeemed).

These tests pin the stamp/skip semantics so the gap stays closed.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from domain.value_objects import ReconcileOracleLossesResult
from use_cases.reconcile_oracle_losses import ReconcileOracleLossesUseCase


def _trade(
    *,
    trade_id: int = 9124,
    strategy: str = "v9_2_iso_expand",
    direction: str = "YES",
    stake: float = 4.98,
    slug: str = "btc-updown-5m-1779489000",
    status: str = "OPEN",
    execution_mode: str = "fak",
    polymarket_tx_hash=None,
    created_at_iso: str = "2026-05-22T22:32:25Z",
) -> dict:
    from datetime import datetime
    return {
        "id": trade_id,
        "strategy": strategy,
        "strategy_id": strategy,
        "direction": direction,
        "stake_usd": stake,
        "entry_price": 0.82,
        "fill_price": 0.82,
        "fill_size": stake / 0.82,
        "market_slug": slug,
        "metadata": {},
        "polymarket_tx_hash": polymarket_tx_hash,
        "execution_mode": execution_mode,
        "status": status,
        "created_at": datetime.fromisoformat(created_at_iso.replace("Z", "+00:00")),
    }


@pytest.mark.asyncio
async def test_stamps_loss_when_oracle_disagrees_with_yes():
    """YES trade on a DOWN-resolved window → outcome=LOSS, pnl=-stake."""
    trade_repo = AsyncMock()
    window_state = AsyncMock()

    trade_repo.find_unresolved_live_trades_older_than.return_value = [
        _trade(trade_id=9124, direction="YES", stake=4.98)
    ]
    window_state.get_oracle_outcome_by_slug.return_value = "DOWN"
    trade_repo.stamp_oracle_loss.return_value = True

    uc = ReconcileOracleLossesUseCase(
        trade_repo=trade_repo,
        window_state=window_state,
        min_age_seconds=1800,
    )
    result = await uc.execute()

    assert isinstance(result, ReconcileOracleLossesResult)
    assert result.trades_scanned == 1
    assert result.trades_stamped_loss == 1
    assert result.skipped_no_oracle == 0
    assert result.skipped_oracle_win == 0
    assert result.errors == 0
    assert result.total_loss_usd == pytest.approx(-4.98, abs=1e-4)

    trade_repo.stamp_oracle_loss.assert_awaited_once()
    kwargs = trade_repo.stamp_oracle_loss.await_args.kwargs
    assert kwargs["trade_id"] == 9124
    assert kwargs["pnl_usd"] == pytest.approx(-4.98, abs=1e-4)


@pytest.mark.asyncio
async def test_stamps_loss_for_no_trade_on_up_window():
    """NO trade on an UP-resolved window → LOSS."""
    trade_repo = AsyncMock()
    window_state = AsyncMock()

    trade_repo.find_unresolved_live_trades_older_than.return_value = [
        _trade(trade_id=9145, direction="NO", stake=5.05, slug="btc-updown-5m-1779562800")
    ]
    window_state.get_oracle_outcome_by_slug.return_value = "UP"
    trade_repo.stamp_oracle_loss.return_value = True

    uc = ReconcileOracleLossesUseCase(
        trade_repo=trade_repo,
        window_state=window_state,
    )
    result = await uc.execute()

    assert result.trades_stamped_loss == 1
    assert result.total_loss_usd == pytest.approx(-5.05, abs=1e-4)


@pytest.mark.asyncio
async def test_defers_when_oracle_says_trade_won():
    """When oracle direction matches the trade, do NOT stamp LOSS — leave
    NULL so the redemption reconciler can stamp WIN with proper payout.
    """
    trade_repo = AsyncMock()
    window_state = AsyncMock()

    trade_repo.find_unresolved_live_trades_older_than.return_value = [
        _trade(trade_id=9200, direction="YES", stake=10.0)
    ]
    window_state.get_oracle_outcome_by_slug.return_value = "UP"

    uc = ReconcileOracleLossesUseCase(
        trade_repo=trade_repo,
        window_state=window_state,
    )
    result = await uc.execute()

    assert result.trades_stamped_loss == 0
    assert result.skipped_oracle_win == 1
    assert result.total_loss_usd == 0.0
    trade_repo.stamp_oracle_loss.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_when_oracle_not_yet_populated():
    """Oracle NULL → defer, don't stamp."""
    trade_repo = AsyncMock()
    window_state = AsyncMock()

    trade_repo.find_unresolved_live_trades_older_than.return_value = [
        _trade(trade_id=9300, direction="YES", stake=3.0)
    ]
    window_state.get_oracle_outcome_by_slug.return_value = None

    uc = ReconcileOracleLossesUseCase(
        trade_repo=trade_repo,
        window_state=window_state,
    )
    result = await uc.execute()

    assert result.trades_stamped_loss == 0
    assert result.skipped_no_oracle == 1
    trade_repo.stamp_oracle_loss.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_unparseable_trade_missing_direction():
    trade_repo = AsyncMock()
    window_state = AsyncMock()

    bad = _trade(trade_id=9400, direction="", stake=1.0)
    trade_repo.find_unresolved_live_trades_older_than.return_value = [bad]

    uc = ReconcileOracleLossesUseCase(
        trade_repo=trade_repo,
        window_state=window_state,
    )
    result = await uc.execute()

    assert result.skipped_unparseable == 1
    assert result.trades_stamped_loss == 0
    window_state.get_oracle_outcome_by_slug.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_unparseable_trade_missing_slug():
    trade_repo = AsyncMock()
    window_state = AsyncMock()

    bad = _trade(trade_id=9401, direction="YES", stake=1.0, slug="")
    trade_repo.find_unresolved_live_trades_older_than.return_value = [bad]

    uc = ReconcileOracleLossesUseCase(
        trade_repo=trade_repo,
        window_state=window_state,
    )
    result = await uc.execute()

    assert result.skipped_unparseable == 1
    window_state.get_oracle_outcome_by_slug.assert_not_awaited()


@pytest.mark.asyncio
async def test_idempotent_when_stamp_guard_rejects():
    """If another reconciler already stamped this trade, stamp_oracle_loss
    returns False — we tally that as already-stamped, NOT a stamped LOSS."""
    trade_repo = AsyncMock()
    window_state = AsyncMock()

    trade_repo.find_unresolved_live_trades_older_than.return_value = [
        _trade(trade_id=9131, direction="YES", stake=6.13)
    ]
    window_state.get_oracle_outcome_by_slug.return_value = "DOWN"
    trade_repo.stamp_oracle_loss.return_value = False  # WHERE guard rejected

    uc = ReconcileOracleLossesUseCase(
        trade_repo=trade_repo,
        window_state=window_state,
    )
    result = await uc.execute()

    assert result.trades_stamped_loss == 0
    assert result.total_loss_usd == 0.0
    assert result.errors == 0


@pytest.mark.asyncio
async def test_continues_after_per_trade_exception():
    """One trade raising must not abort the whole pass."""
    trade_repo = AsyncMock()
    window_state = AsyncMock()

    trade_repo.find_unresolved_live_trades_older_than.return_value = [
        _trade(trade_id=9500, direction="YES", stake=2.0,
               slug="btc-updown-5m-1779489000"),
        _trade(trade_id=9501, direction="NO", stake=3.0,
               slug="btc-updown-5m-1779562800"),
    ]

    async def _oracle(slug):
        if "1779489000" in slug:
            raise RuntimeError("simulated Gamma flake")
        return "UP"  # second trade is NO + UP → LOSS

    window_state.get_oracle_outcome_by_slug.side_effect = _oracle
    trade_repo.stamp_oracle_loss.return_value = True

    uc = ReconcileOracleLossesUseCase(
        trade_repo=trade_repo,
        window_state=window_state,
    )
    result = await uc.execute()

    assert result.errors == 1
    assert result.trades_stamped_loss == 1
    assert result.total_loss_usd == pytest.approx(-3.0, abs=1e-4)


@pytest.mark.asyncio
async def test_returns_empty_result_on_repo_fetch_failure():
    trade_repo = AsyncMock()
    window_state = AsyncMock()
    trade_repo.find_unresolved_live_trades_older_than.side_effect = (
        RuntimeError("db down")
    )

    uc = ReconcileOracleLossesUseCase(
        trade_repo=trade_repo,
        window_state=window_state,
    )
    result = await uc.execute()

    assert result.trades_scanned == 0
    assert result.errors == 1
    window_state.get_oracle_outcome_by_slug.assert_not_awaited()


@pytest.mark.asyncio
async def test_passes_min_age_seconds_to_repo():
    trade_repo = AsyncMock()
    window_state = AsyncMock()
    trade_repo.find_unresolved_live_trades_older_than.return_value = []

    uc = ReconcileOracleLossesUseCase(
        trade_repo=trade_repo,
        window_state=window_state,
        min_age_seconds=3600,
    )
    await uc.execute()

    trade_repo.find_unresolved_live_trades_older_than.assert_awaited_once_with(
        3600
    )
