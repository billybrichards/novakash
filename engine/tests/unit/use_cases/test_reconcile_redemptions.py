"""Regression tests for ReconcileRedemptionsUseCase — Hub #586.

Driver incident: 2026-05-22 LIVE session. 124 verified wins via Gamma,
0 wins recorded in trades table. Polymarket auto-redeem removed winners
from data-api positions so the legacy reconciler couldn't see them. New
reconciler polls activity?type=REDEEM and stamps the matching trades.

These tests pin the matching, idempotency, and oracle-deferral logic so
the regression never returns.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from domain.value_objects import RedemptionEvent
from use_cases.reconcile_redemptions import ReconcileRedemptionsUseCase


def _redemption(
    *,
    tx: str = "0xabc1230000000000000000000000000000000000000000000000000000000000",
    slug: str = "btc-updown-5m-1779543300",
    cid: str = "0x0771fde4fbe150953cc76605f371722fbe8cebd5e14a0b23419f16485fcb4a7a",
    usdc: float = 15.0,
    size: float = 15.0,
    ts: int = 1779543638,
) -> RedemptionEvent:
    return RedemptionEvent(
        transaction_hash=tx,
        condition_id=cid,
        market_slug=slug,
        usdc_size=usdc,
        size=size,
        timestamp=ts,
        funder_address="0x181d2ed714e0f7fe9c6e4f13711376edaab25e10",
    )


def _trade(
    *,
    trade_id: int = 9136,
    strategy: str = "v9_2_raw_lgb",
    direction: str = "NO",
    stake: float = 3.55,
    fill_price: float = 0.91,
    fill_size: float = 3.9,
) -> dict:
    return {
        "id": trade_id,
        "strategy": strategy,
        "strategy_id": strategy,
        "direction": direction,
        "stake_usd": stake,
        "entry_price": fill_price,
        "fill_price": fill_price,
        "fill_size": fill_size,
        "market_slug": "btc-updown-5m-1779543300",
        "metadata": {},
        "polymarket_tx_hash": None,
        "execution_mode": "fak_ladder",
        "created_at": None,
    }


@pytest.mark.asyncio
async def test_stamps_winning_no_side_on_down_resolution():
    """Window 1779543300 BTC resolved DOWN. NO-side trades win."""
    repo = AsyncMock()
    window = AsyncMock()
    window.get_oracle_outcome_by_slug.return_value = "DOWN"
    repo.find_unresolved_live_trades_for_slug.return_value = [
        _trade(trade_id=9135, direction="NO"),
        _trade(trade_id=9136, direction="NO"),
    ]
    repo.stamp_redemption_win.return_value = True

    uc = ReconcileRedemptionsUseCase(trade_repo=repo, window_state=window)
    result = await uc.execute([_redemption()])

    assert result.trades_stamped == 2
    assert result.events_seen == 1
    assert result.events_skipped_no_match == 0
    assert result.events_skipped_no_oracle == 0
    assert result.errors == 0
    # find_unresolved_live_trades_for_slug was called with winning_direction=NO
    call = repo.find_unresolved_live_trades_for_slug.await_args
    assert call.args[0] == "btc-updown-5m-1779543300"
    assert call.kwargs["winning_direction"] == "NO"


@pytest.mark.asyncio
async def test_stamps_winning_yes_side_on_up_resolution():
    """Window resolved UP — YES-side trades win, NO-side ignored."""
    repo = AsyncMock()
    window = AsyncMock()
    window.get_oracle_outcome_by_slug.return_value = "UP"
    repo.find_unresolved_live_trades_for_slug.return_value = [
        _trade(trade_id=9134, direction="YES", stake=3.32, fill_price=0.91),
    ]
    repo.stamp_redemption_win.return_value = True

    uc = ReconcileRedemptionsUseCase(trade_repo=repo, window_state=window)
    result = await uc.execute(
        [_redemption(slug="btc-updown-5m-1779542400", usdc=15.0)]
    )

    assert result.trades_stamped == 1
    call = repo.find_unresolved_live_trades_for_slug.await_args
    assert call.kwargs["winning_direction"] == "YES"


@pytest.mark.asyncio
async def test_defers_when_oracle_outcome_missing():
    """No oracle_outcome yet → skip the event; next pass will retry.
    The trade is NOT stamped — leaving outcome=NULL is the safe default
    so a backlog of new redemptions doesn't get mis-stamped."""
    repo = AsyncMock()
    window = AsyncMock()
    window.get_oracle_outcome_by_slug.return_value = None
    repo.stamp_redemption_win.return_value = True

    uc = ReconcileRedemptionsUseCase(trade_repo=repo, window_state=window)
    result = await uc.execute([_redemption()])

    assert result.trades_stamped == 0
    assert result.events_skipped_no_oracle == 1
    repo.find_unresolved_live_trades_for_slug.assert_not_called()
    repo.stamp_redemption_win.assert_not_called()


@pytest.mark.asyncio
async def test_idempotent_when_no_unresolved_trades_match():
    """Re-running over already-stamped redemptions is safe — the query
    returns no rows because outcome IS NOT NULL filters them out."""
    repo = AsyncMock()
    window = AsyncMock()
    window.get_oracle_outcome_by_slug.return_value = "DOWN"
    repo.find_unresolved_live_trades_for_slug.return_value = []

    uc = ReconcileRedemptionsUseCase(trade_repo=repo, window_state=window)
    result = await uc.execute([_redemption()])

    assert result.trades_stamped == 0
    assert result.events_skipped_no_match == 1
    assert result.events_skipped_no_oracle == 0
    repo.stamp_redemption_win.assert_not_called()


@pytest.mark.asyncio
async def test_idempotent_when_stamp_guard_rejects():
    """Race with the legacy reconciler: the WHERE guard returns 0 rows
    updated. We don't count it as 'stamped' but the event still completes
    cleanly."""
    repo = AsyncMock()
    window = AsyncMock()
    window.get_oracle_outcome_by_slug.return_value = "DOWN"
    repo.find_unresolved_live_trades_for_slug.return_value = [
        _trade(trade_id=9135, direction="NO"),
    ]
    repo.stamp_redemption_win.return_value = False  # guard rejected

    uc = ReconcileRedemptionsUseCase(trade_repo=repo, window_state=window)
    result = await uc.execute([_redemption()])

    assert result.trades_stamped == 0
    assert result.errors == 0


@pytest.mark.asyncio
async def test_payout_uses_fill_size_when_available():
    """Authoritative payout = fill_size (each winning share = $1)."""
    repo = AsyncMock()
    window = AsyncMock()
    window.get_oracle_outcome_by_slug.return_value = "DOWN"
    repo.find_unresolved_live_trades_for_slug.return_value = [
        _trade(trade_id=9135, direction="NO", stake=3.55, fill_size=3.9),
    ]
    repo.stamp_redemption_win.return_value = True

    uc = ReconcileRedemptionsUseCase(trade_repo=repo, window_state=window)
    await uc.execute([_redemption()])

    call = repo.stamp_redemption_win.await_args
    assert call.kwargs["payout_usd"] == pytest.approx(3.9)
    assert call.kwargs["pnl_usd"] == pytest.approx(3.9 - 3.55)


@pytest.mark.asyncio
async def test_payout_derives_from_stake_and_entry_when_no_fill_size():
    """Fallback: payout = stake / entry_price = shares."""
    repo = AsyncMock()
    window = AsyncMock()
    window.get_oracle_outcome_by_slug.return_value = "DOWN"
    trade = _trade(trade_id=9135, direction="NO", stake=10.0, fill_price=0.5)
    trade["fill_size"] = None  # force the fallback
    repo.find_unresolved_live_trades_for_slug.return_value = [trade]
    repo.stamp_redemption_win.return_value = True

    uc = ReconcileRedemptionsUseCase(trade_repo=repo, window_state=window)
    await uc.execute([_redemption()])

    call = repo.stamp_redemption_win.await_args
    assert call.kwargs["payout_usd"] == pytest.approx(20.0)
    assert call.kwargs["pnl_usd"] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_carries_redemption_tx_and_timestamp_to_repo():
    """The on-chain transactionHash + timestamp from the REDEEM activity
    must be persisted with the trade — they are the audit trail."""
    repo = AsyncMock()
    window = AsyncMock()
    window.get_oracle_outcome_by_slug.return_value = "DOWN"
    repo.find_unresolved_live_trades_for_slug.return_value = [
        _trade(trade_id=9135, direction="NO"),
    ]
    repo.stamp_redemption_win.return_value = True

    uc = ReconcileRedemptionsUseCase(trade_repo=repo, window_state=window)
    event = _redemption(
        tx="0xcb1ed7b5e18637e29fb92c460107031d69c9fe6b459df187e8206f7d9cd7878d",
        ts=1779543638,
    )
    await uc.execute([event])

    call = repo.stamp_redemption_win.await_args
    assert (
        call.kwargs["redemption_tx"]
        == "0xcb1ed7b5e18637e29fb92c460107031d69c9fe6b459df187e8206f7d9cd7878d"
    )
    assert call.kwargs["redeemed_at_epoch"] == 1779543638


@pytest.mark.asyncio
async def test_per_event_error_does_not_block_other_events():
    """One bad redemption row must not poison the whole batch."""
    repo = AsyncMock()
    window = AsyncMock()
    # Window 1 raises, window 2 succeeds.
    async def oracle_lookup(slug):
        if "bad" in slug:
            raise RuntimeError("boom")
        return "DOWN"
    window.get_oracle_outcome_by_slug.side_effect = oracle_lookup
    repo.find_unresolved_live_trades_for_slug.return_value = [
        _trade(trade_id=9135, direction="NO"),
    ]
    repo.stamp_redemption_win.return_value = True

    bad_event = _redemption(
        tx="0xdeadbeef", slug="btc-updown-5m-bad", usdc=10.0
    )
    good_event = _redemption()

    uc = ReconcileRedemptionsUseCase(trade_repo=repo, window_state=window)
    result = await uc.execute([bad_event, good_event])

    assert result.errors == 1
    assert result.trades_stamped == 1


@pytest.mark.asyncio
async def test_total_payout_aggregates_across_events():
    """Result.total_payout_usd is the sum of all stamped payouts."""
    repo = AsyncMock()
    window = AsyncMock()
    window.get_oracle_outcome_by_slug.return_value = "DOWN"

    call_n = {"count": 0}

    async def trades_lookup(slug, *, winning_direction=None):
        call_n["count"] += 1
        if call_n["count"] == 1:
            return [_trade(trade_id=9135, direction="NO", stake=5.0, fill_size=10.0)]
        return [_trade(trade_id=9136, direction="NO", stake=7.0, fill_size=15.0)]

    repo.find_unresolved_live_trades_for_slug.side_effect = trades_lookup
    repo.stamp_redemption_win.return_value = True

    uc = ReconcileRedemptionsUseCase(trade_repo=repo, window_state=window)
    result = await uc.execute(
        [
            _redemption(tx="0xaaa"),
            _redemption(tx="0xbbb", slug="btc-updown-5m-1779542400"),
        ]
    )

    assert result.trades_stamped == 2
    # 10.0 (payout for trade 9135) + 15.0 (trade 9136) = 25.0
    assert result.total_payout_usd == pytest.approx(25.0)
