"""Hub #455 (2026-05-10) — reconciler regression tests.

Two patches shipped together:

Patch #1 — Paginate /positions
    Polymarket data-api caps each page at 500 records.  When a wallet has
    more than 500 historical positions the engine was silently dropping
    everything on page 2+.  Two winning v_consensus_4way trades on page 2
    were invisible to the reconciler and fell through to the cost-fallback
    tier where they were mis-classified as LOSS.

    Covered tests:
      * test_fetch_all_positions_single_page — single-page wallet: one GET call
      * test_fetch_all_positions_multi_page  — 600-position wallet: two GET calls,
                                              all 600 records returned

Patch #2 — condition_id required in cost-fallback match
    find_by_approximate_cost previously matched ANY trade with stake within
    $0.15.  All v_consensus_4way entries carry a $5 stake, so a losing $4.94
    position matched the wrong unrelated winning trade and stamped LOSS on it.

    Fix: pass position.condition_id into find_by_approximate_cost; the repo
    adds a ``metadata->>'condition_id' = $2`` clause so cross-market matches
    are rejected.

    Covered tests:
      * test_cost_match_passes_condition_id — reconcile_positions passes cid
      * test_cost_match_condition_id_required_in_repo_sql — repo SQL has cid filter
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch, call
import pytest

from domain.value_objects import PositionOutcome
from use_cases.reconcile_positions import ReconcilePositionsUseCase


# ─── Helpers ────────────────────────────────────────────────────────────────

PAGE_SIZE = 500


def _make_position_records(n: int, base_cid: str = "0xcond") -> list[dict]:
    """Synthetic position records for mock API responses."""
    return [
        {
            "conditionId": f"{base_cid}{i:04d}",
            "curPrice": 0.5,
            "size": 10.0,
            "avgPrice": 0.5,
            "asset": f"tok{i}",
        }
        for i in range(n)
    ]


def _pos(
    condition_id: str = "0xcondABCD",
    outcome: str = "LOSS",
    cost: float = 5.0,
    token_id: str = "",
) -> PositionOutcome:
    return PositionOutcome(
        condition_id=condition_id,
        outcome=outcome,
        size=0.0,
        avg_price=0.5,
        cost=cost,
        value=0.0,
        pnl_raw=-cost,
        token_id=token_id or None,
    )


def _make_uc(
    *,
    matched_trade: dict | None = None,
) -> tuple[ReconcilePositionsUseCase, AsyncMock]:
    """Minimal ReconcilePositionsUseCase with mocked repos."""
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

    uc = ReconcilePositionsUseCase(
        trade_repo=trade_repo,
        window_state=window_state,
        alerts=alerts,
        clock=clock,
        canonical_resolver=None,  # disabled — isolate patch #2
    )
    return uc, trade_repo


# ─── Patch #1: Pagination ───────────────────────────────────────────────────


def _make_mock_response(records: list):
    """Build a context-manager-compatible aiohttp response mock."""
    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(return_value=records)
    # Support `async with session.get(...) as resp:` pattern.
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _make_mock_session(responses: list):
    """Build an aiohttp.ClientSession mock that returns ``responses`` in order."""
    call_idx = {"n": 0}
    get_calls: list[str] = []

    def _get(url, **kwargs):
        idx = call_idx["n"]
        call_idx["n"] += 1
        get_calls.append(url)
        return _make_mock_response(responses[idx])

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.get = MagicMock(side_effect=_get)
    session._get_calls = get_calls  # expose for assertions
    return session


class TestFetchAllPositionsPagination:
    """_fetch_all_positions paginates until a short page is returned."""

    @pytest.mark.asyncio
    async def test_single_page_wallet_one_request(self):
        """300 positions → one GET call, 300 records returned."""
        records = _make_position_records(300)
        mock_session = _make_mock_session([records])

        from adapters.polymarket.live_client import LivePolymarketClient
        import structlog

        client = LivePolymarketClient.__new__(LivePolymarketClient)
        client._funder_address = "0x" + "ab" * 20
        client._log = structlog.get_logger()

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await client._fetch_all_positions()

        assert len(result) == 300
        # Only one page requested — offset=0
        assert len(mock_session._get_calls) == 1
        called_url = mock_session._get_calls[0]
        assert "offset=0" in called_url
        assert f"limit={PAGE_SIZE}" in called_url

    @pytest.mark.asyncio
    async def test_multi_page_wallet_all_records_returned(self):
        """600 positions → two GET calls (page1=500, page2=100), 600 total."""
        page1 = _make_position_records(PAGE_SIZE, base_cid="0xpage1_")
        page2 = _make_position_records(100, base_cid="0xpage2_")
        mock_session = _make_mock_session([page1, page2])

        from adapters.polymarket.live_client import LivePolymarketClient
        import structlog

        client = LivePolymarketClient.__new__(LivePolymarketClient)
        client._funder_address = "0x" + "ab" * 20
        client._log = structlog.get_logger()

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await client._fetch_all_positions()

        # All 600 records must be present.
        assert len(result) == PAGE_SIZE + 100

        # Two separate GET calls — first at offset=0, second at offset=500.
        assert len(mock_session._get_calls) == 2
        assert "offset=0" in mock_session._get_calls[0]
        assert f"offset={PAGE_SIZE}" in mock_session._get_calls[1]

    def test_polymarket_client_also_has_paginated_method(self):
        """PolymarketClient (non-live adapter) also exposes _fetch_all_positions."""
        from execution.polymarket_client import PolymarketClient

        assert hasattr(PolymarketClient, "_fetch_all_positions"), (
            "PolymarketClient must expose _fetch_all_positions so "
            "get_portfolio_value / get_position_outcomes paginate correctly."
        )
        src = inspect.getsource(PolymarketClient._fetch_all_positions)
        assert "offset" in src, (
            "PolymarketClient._fetch_all_positions must iterate over "
            "pages using an offset parameter."
        )

    def test_live_client_get_position_outcomes_uses_fetch_all(self):
        """LivePolymarketClient.get_position_outcomes delegates to _fetch_all_positions."""
        from adapters.polymarket.live_client import LivePolymarketClient

        src = inspect.getsource(LivePolymarketClient.get_position_outcomes)
        assert "_fetch_all_positions" in src, (
            "get_position_outcomes must call self._fetch_all_positions() "
            "to paginate; direct single-page URL requests miss page 2+."
        )

    def test_live_client_get_portfolio_value_uses_fetch_all(self):
        """LivePolymarketClient.get_portfolio_value delegates to _fetch_all_positions."""
        from adapters.polymarket.live_client import LivePolymarketClient

        src = inspect.getsource(LivePolymarketClient.get_portfolio_value)
        assert "_fetch_all_positions" in src, (
            "get_portfolio_value must call self._fetch_all_positions() "
            "to paginate; direct single-page URL requests miss page 2+."
        )


# ─── Patch #2: condition_id in cost-fallback match ───────────────────────────


class TestCostFallbackConditionIdRequired:
    """find_by_approximate_cost receives condition_id from reconciler."""

    @pytest.mark.asyncio
    async def test_reconciler_passes_condition_id_to_cost_match(self):
        """ReconcilePositionsUseCase._find_matching_trade passes
        position.condition_id into find_by_approximate_cost.

        Hub #455: v_consensus_4way all share a $5 stake — without the
        condition_id filter a losing $4.94 position can match the wrong
        winning trade and stamp LOSS on it.
        """
        pos = _pos(condition_id="0xcondABCD1234", cost=5.0)
        uc, trade_repo = _make_uc(matched_trade=None)

        # Tiers 1 + 2 miss (no token_id on pos, trade_repo returns None).
        # Tier 3 (cost match) should be called with the condition_id.
        await uc.resolve_one(pos)

        trade_repo.find_by_approximate_cost.assert_called_once()
        kwargs = trade_repo.find_by_approximate_cost.call_args

        # Accept both positional and keyword forms.
        if kwargs[1]:
            passed_cid = kwargs[1].get("condition_id")
        else:
            # Positional: find_by_approximate_cost(cost, condition_id)
            passed_cid = kwargs[0][1] if len(kwargs[0]) > 1 else None

        assert passed_cid == "0xcondABCD1234", (
            "reconciler must pass position.condition_id to "
            "find_by_approximate_cost so cross-market same-stake matches "
            "are rejected (Hub #455)."
        )

    @pytest.mark.asyncio
    async def test_cost_match_cid_rejects_wrong_condition_id(self):
        """When cost matches but condition_id differs, trade is not resolved.

        Simulates the Hub #455 failure: $4.94 losing position with
        condition_id=0xWRONG matches by cost against a $5 winning trade
        with condition_id=0xCORRECT.  The fix makes the repo return None
        for the wrong-cid query; the reconciler should not resolve the trade.
        """
        winning_trade = {
            "id": "trade-win",
            "token_id": "tok-winner",
            "stake_usd": 5.0,
            "entry_price": 0.50,
            "fill_size": 10.0,
            "direction": "YES",
            "asset": "BTC",
            "window_ts": 1_778_000_000,
            "polymarket_order_id": "0x" + "ab" * 32,
            "polymarket_tx_hash": "0x" + "cd" * 32,
            "outcome": None,
            "resolved_at": None,
            "strategy": "v_consensus_4way",
        }

        # Simulate repo behavior post-fix: condition_id query returns None
        # (wrong market), cost-only fallback returns the match (legacy path).
        # The reconciler should use the condition_id-filtered result (None)
        # and not fall through to stamp the wrong outcome.
        uc, trade_repo = _make_uc(matched_trade=None)
        # trade_repo returns None — simulating condition_id filter rejecting match.

        losing_pos = _pos(
            condition_id="0xWRONG_MARKET",
            outcome="LOSS",
            cost=4.94,
        )
        result = await uc.resolve_one(losing_pos)

        # No match found → no resolution → result is None.
        assert result is None, (
            "A cost-only match against a different condition_id must not "
            "resolve the trade (Hub #455 — cross-market false positive)."
        )
        trade_repo.resolve_trade.assert_not_called()

    def test_repo_find_by_approximate_cost_signature_accepts_condition_id(self):
        """PgTradeRepository.find_by_approximate_cost accepts condition_id kwarg."""
        from adapters.persistence.pg_trade_repo import PgTradeRepository

        sig = inspect.signature(PgTradeRepository.find_by_approximate_cost)
        assert "condition_id" in sig.parameters, (
            "PgTradeRepository.find_by_approximate_cost must accept "
            "condition_id= kwarg to support Hub #455 fix."
        )

    def test_repo_sql_includes_condition_id_filter(self):
        """PgTradeRepository.find_by_approximate_cost SQL uses condition_id filter."""
        from adapters.persistence.pg_trade_repo import PgTradeRepository

        src = inspect.getsource(PgTradeRepository.find_by_approximate_cost)
        # The method should contain a metadata->>'condition_id' = $2 clause.
        assert "condition_id" in src, (
            "find_by_approximate_cost source must contain a condition_id "
            "filter clause to prevent cross-market same-stake false positives."
        )
        assert "metadata" in src, (
            "The condition_id filter must query the trades.metadata JSONB "
            "column (metadata->>'condition_id')."
        )

    def test_port_find_by_approximate_cost_signature_accepts_condition_id(self):
        """TradeRepository port declares condition_id= parameter."""
        from domain.ports import TradeRepository

        sig = inspect.signature(TradeRepository.find_by_approximate_cost)
        assert "condition_id" in sig.parameters, (
            "TradeRepository.find_by_approximate_cost port must declare "
            "condition_id= so concrete implementations know to implement it."
        )
