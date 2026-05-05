"""Audit #353: cost_fallback resolver path consults canonical_resolver.

Follow-up to PRs #483/#484/#485/#486. The cost_fallback match path in
``ReconcilePositionsUseCase.resolve_one`` is uniquely vulnerable to
NegRisk auto-redeem races:

  1. window_snapshots not yet populated (~30-60s lag at window close).
  2. on-chain CTF.payoutNumerators may take time to propagate.
  3. NegRisk auto-redeem clears YES tokens FAST when YES wins.
  4. ``LivePolymarketClient.get_position_outcomes()`` derives
     ``position.outcome`` from data-api ``curPrice``: tokens already
     redeemed → curPrice ≈ 0 → outcome="LOSS".
  5. The token_id no longer exists on the wallet (redeemed) so exact /
     prefix tiers miss → cost_fallback matches by stake.
  6. Without a fix, the engine writes ``outcome=LOSS`` even when the
     canonical UP/DOWN resolution actually means the trade WON.

Bug case — trade 7392 (v12_lgb_combo YES @ $0.7575 stake $30,
window 1777964100, 2026-05-05 07:00:26 UTC). Polymarket page confirmed
oracle_outcome=UP (trade SHOULD be WIN). Engine wrote LOSS via the
cost_fallback path before canonical_resolver was consulted.

Fix (this PR — audit #353):

* When ``match_method == "cost_fallback"`` the resolver is consulted
  the same way as exact / prefix matches but ``fallback_curprice_outcome``
  is NOT passed (Tier 4 must not echo the poisoned curPrice signal back).
* If Tiers 1-3 (HTML > window_snapshots > on-chain CTF) all miss the
  use case DEFERS — returns ``None`` and emits
  ``reconciler.cost_fallback_deferred`` at WARN — leaving the trade
  with ``outcome IS NULL`` so the next reconciler pass retries once
  canonical sources have populated.
* When canonical disagrees with curPrice the override fires under
  ``reconciler.cost_fallback_canonical_override`` (separate log line
  from the existing ``curprice_canonical_disagree`` so audits can
  count cost-fallback flips independently).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from domain.value_objects import PositionOutcome
from use_cases.reconcile_positions import ReconcilePositionsUseCase


# ─── Test fixtures ──────────────────────────────────────────────────────────


def _pos(
    *,
    condition_id: str = "0xcond353",
    token_id: str = "tok-redeemed-no-longer-on-wallet",
    outcome: str = "LOSS",  # NegRisk redeem race default: curPrice→0→LOSS
    size: float = 0.0,  # auto-redeemed
    avg_price: float = 0.7575,
    cost: float = 30.0,
    value: float = 0.0,
    pnl_raw: float = -30.0,
) -> PositionOutcome:
    return PositionOutcome(
        condition_id=condition_id,
        token_id=token_id,
        outcome=outcome,
        size=size,
        avg_price=avg_price,
        cost=cost,
        value=value,
        pnl_raw=pnl_raw,
    )


def _match_yes_trade(
    *,
    trade_id: str = "trade-7392",
    direction: str = "YES",
    stake_usd: float = 30.0,
    entry_price: float = 0.7575,
    window_ts: int = 1_777_964_100,
    asset: str = "BTC",
    timeframe: str = "5m",
    fill_size: float | None = 39.604,  # 30/0.7575
) -> dict:
    """A real YES trade — non-synthetic order_id + on-chain tx_hash so
    the cost_fallback phantom-trade guard in ``_find_matching_trade``
    accepts it.
    """
    return {
        "id": trade_id,
        "token_id": "trade-token-id-original",
        "stake_usd": stake_usd,
        "entry_price": entry_price,
        "fill_size": fill_size,
        "direction": direction,
        "asset": asset,
        "timeframe": timeframe,
        "window_ts": window_ts,
        "polymarket_order_id": "0x" + "ab" * 32,
        "polymarket_tx_hash": "0x" + "cd" * 32,
        "outcome": None,
        "resolved_at": None,
        "execution_mode": "live",
        "strategy": "v12_lgb_combo",
    }


def _make_canon_outcome(direction: str, source: str = "polymarket_html"):
    """Synthesise a CanonicalOutcome-shaped object via MagicMock so we
    don't have to import the dataclass (the resolver is mocked anyway).
    """
    co = MagicMock()
    co.direction = direction
    co.source = source
    co.sub_source = None
    co.price_to_beat = None
    co.close_price = None
    return co


def _make_uc(
    *,
    canon_return,
    matched_trade: dict,
):
    """Build a ReconcilePositionsUseCase wired with a mocked
    canonical_resolver and trade_repo whose cost_fallback tier returns
    ``matched_trade``.

    ``canon_return`` is what
    ``canonical_resolver.resolve_window_outcome_canonical`` returns.
    """
    trade_repo = AsyncMock()
    trade_repo.find_by_token_id = AsyncMock(return_value=None)
    trade_repo.find_by_token_prefix = AsyncMock(return_value=None)
    trade_repo.find_by_approximate_cost = AsyncMock(return_value=matched_trade)
    trade_repo.resolve_trade = AsyncMock(return_value=None)

    window_state = AsyncMock()
    window_state.mark_resolved = AsyncMock(return_value=None)

    alerts = AsyncMock()
    clock = MagicMock()
    clock.now.return_value = 1_777_964_400.0

    canonical_resolver = MagicMock()
    canonical_resolver.resolve_window_outcome_canonical = AsyncMock(
        return_value=canon_return
    )
    # Strip the html_fetcher hook used by per-trade v2 cards to keep
    # the resolution-card path silent in unit tests.
    canonical_resolver._html_fetcher = None

    uc = ReconcilePositionsUseCase(
        trade_repo=trade_repo,
        window_state=window_state,
        alerts=alerts,
        clock=clock,
        canonical_resolver=canonical_resolver,
    )
    return uc, trade_repo, canonical_resolver


# ─── Test 1: canonical=UP + YES trade → WIN (override curprice LOSS) ────────


@pytest.mark.asyncio
async def test_cost_fallback_yes_canonical_up_resolves_win():
    """NegRisk auto-redeem race: position.outcome=LOSS (curPrice=0 because
    YES tokens auto-redeemed), but canonical says UP → trade was YES → WIN.

    Without the fix the engine wrote LOSS. With the fix canonical wins.
    """
    canon = _make_canon_outcome("UP", source="polymarket_html")
    uc, trade_repo, resolver = _make_uc(
        canon_return=canon,
        matched_trade=_match_yes_trade(direction="YES"),
    )

    result = await uc.resolve_one(_pos(outcome="LOSS"))

    assert result is not None, "must resolve, not defer"
    assert result.outcome == "RESOLVED_WIN"
    assert result.match_method == "cost_fallback"
    assert result.pnl_usd > 0, "WIN must have positive PnL"

    # DB must be written with WIN, not LOSS.
    trade_repo.resolve_trade.assert_called_once()
    kwargs = trade_repo.resolve_trade.call_args.kwargs
    assert kwargs["outcome"] == "WIN"
    assert kwargs["status"] == "RESOLVED_WIN"

    # Canonical resolver must be called WITHOUT fallback_curprice_outcome
    # for cost_fallback (else Tier 4 could echo poisoned curPrice).
    resolver.resolve_window_outcome_canonical.assert_called_once()
    canon_kwargs = resolver.resolve_window_outcome_canonical.call_args.kwargs
    assert canon_kwargs["fallback_curprice_outcome"] is None, (
        "cost_fallback must NOT pass fallback_curprice_outcome — Tier 4 "
        "would echo the poisoned curPrice signal back and confirm LOSS"
    )


# ─── Test 2: canonical=DOWN + NO trade → WIN ────────────────────────────────


@pytest.mark.asyncio
async def test_cost_fallback_no_canonical_down_resolves_win():
    """Symmetric case: trade direction=NO (DOWN bet), canonical=DOWN → WIN."""
    canon = _make_canon_outcome("DOWN", source="window_snapshots")
    uc, trade_repo, _ = _make_uc(
        canon_return=canon,
        matched_trade=_match_yes_trade(direction="NO"),
    )

    result = await uc.resolve_one(_pos(outcome="LOSS"))

    assert result is not None
    assert result.outcome == "RESOLVED_WIN"
    assert result.match_method == "cost_fallback"

    kwargs = trade_repo.resolve_trade.call_args.kwargs
    assert kwargs["outcome"] == "WIN"


# ─── Test 3: canonical=None → DEFER (don't write outcome) ───────────────────


@pytest.mark.asyncio
async def test_cost_fallback_canonical_none_defers():
    """Canonical sources all miss (HTML + window_snapshots + on-chain
    haven't populated yet, ~30-60s lag at window close).

    The use case MUST defer — return None, write nothing, emit
    ``reconciler.cost_fallback_deferred`` WARN — so the next reconciler
    pass retries with canonical data populated. This is the safe
    behaviour: better to delay one cycle than misclassify.
    """
    uc, trade_repo, _ = _make_uc(
        canon_return=None,
        matched_trade=_match_yes_trade(direction="YES"),
    )

    result = await uc.resolve_one(_pos(outcome="LOSS"))

    assert result is None, (
        "cost_fallback + canonical None must defer (return None), not write LOSS"
    )
    # DB must NOT be touched on defer — the trade stays outcome=NULL
    # so the next reconciler pass picks it up.
    trade_repo.resolve_trade.assert_not_called()


@pytest.mark.asyncio
async def test_cost_fallback_canonical_none_emits_deferred_warn(capsys):
    """The defer path must emit a structured WARN so operators can
    monitor how often NegRisk auto-redeem races bite us. structlog
    renders the event to stdout — capture via capsys.
    """
    uc, _, _ = _make_uc(
        canon_return=None,
        matched_trade=_match_yes_trade(direction="YES"),
    )

    result = await uc.resolve_one(_pos(outcome="LOSS"))

    assert result is None
    captured = capsys.readouterr()
    blob = captured.out + captured.err
    assert "cost_fallback_deferred" in blob, (
        f"expected reconciler.cost_fallback_deferred WARN, got: {blob}"
    )


# ─── Test 4: trade 7392 production reproduction ─────────────────────────────


@pytest.mark.asyncio
async def test_trade_7392_reproduction_resolves_win():
    """Reproduce the exact production failure that triggered audit #353.

    Trade 7392 (v12_lgb_combo, YES @ $0.7575, stake $30,
    window_ts=1777964100). NegRisk auto-redeemed the YES tokens before
    the reconciler ran → position.outcome="LOSS", token_id no longer on
    wallet → exact + prefix tiers miss → cost_fallback matches by stake.
    Polymarket page confirms oracle_outcome=UP → trade SHOULD be WIN.

    This test fails on the pre-fix code (engine writes LOSS) and passes
    with the canonical override.
    """
    canon = _make_canon_outcome("UP", source="polymarket_html")
    matched = _match_yes_trade(
        trade_id="7392",
        direction="YES",
        stake_usd=30.0,
        entry_price=0.7575,
        window_ts=1_777_964_100,
        fill_size=30.0 / 0.7575,
    )
    uc, trade_repo, _ = _make_uc(canon_return=canon, matched_trade=matched)

    pos = _pos(
        condition_id="0xcond7392",
        outcome="LOSS",  # poisoned by NegRisk auto-redeem
        size=0.0,
        cost=30.0,
        avg_price=0.7575,
    )

    result = await uc.resolve_one(pos)

    assert result is not None
    assert result.outcome == "RESOLVED_WIN", (
        "trade 7392 (YES, oracle UP) MUST resolve WIN, not LOSS"
    )
    assert result.matched_trade_id == "7392"
    assert result.match_method == "cost_fallback"
    assert result.pnl_usd > 0

    kwargs = trade_repo.resolve_trade.call_args.kwargs
    assert kwargs["trade_id"] == "7392"
    assert kwargs["outcome"] == "WIN"
    assert kwargs["status"] == "RESOLVED_WIN"


# ─── Test 5: canonical agrees with curprice → no flip, write through ────────


@pytest.mark.asyncio
async def test_cost_fallback_canonical_agrees_writes_through():
    """When canonical confirms the curprice-derived outcome (e.g. real
    LOSS) the use case writes through normally — no override, no defer.
    """
    # YES trade, canonical=DOWN → genuine LOSS.
    canon = _make_canon_outcome("DOWN", source="polymarket_html")
    uc, trade_repo, _ = _make_uc(
        canon_return=canon,
        matched_trade=_match_yes_trade(direction="YES"),
    )

    result = await uc.resolve_one(_pos(outcome="LOSS"))

    assert result is not None
    assert result.outcome == "RESOLVED_LOSS"
    kwargs = trade_repo.resolve_trade.call_args.kwargs
    assert kwargs["outcome"] == "LOSS"


# ─── Test 6: canonical override fires distinct log event ────────────────────


@pytest.mark.asyncio
async def test_cost_fallback_canonical_override_log_event(capsys):
    """The cost_fallback override emits ``cost_fallback_canonical_override``
    (NOT the generic ``curprice_canonical_disagree``) so audit dashboards
    can count cost-fallback NegRisk-redeem flips independently.
    """
    canon = _make_canon_outcome("UP", source="polymarket_html")
    uc, _, _ = _make_uc(
        canon_return=canon,
        matched_trade=_match_yes_trade(direction="YES"),
    )

    await uc.resolve_one(_pos(outcome="LOSS"))

    captured = capsys.readouterr()
    blob = captured.out + captured.err
    assert "cost_fallback_canonical_override" in blob, (
        "expected reconciler.cost_fallback_canonical_override WARN distinct "
        f"from generic curprice_canonical_disagree, got logs: {blob}"
    )
    assert "curprice_canonical_disagree" not in blob, (
        "cost_fallback path must emit the cost_fallback-specific event, "
        "NOT the generic curprice_canonical_disagree"
    )
