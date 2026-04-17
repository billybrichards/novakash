"""Phase D tests for in-memory adapter fallbacks."""
from __future__ import annotations

from decimal import Decimal

import pytest

from adapters.onchain.in_memory_onchain import InMemoryOnChainQuery
from adapters.persistence.in_memory_shadow_decision_repo import (
    InMemoryShadowDecisionRepository,
)
from adapters.persistence.in_memory_tally_repo import InMemoryTallyRepo
from domain.alert_values import CumulativeTally, OutflowTx
from domain.value_objects import StrategyDecision, WindowKey


def _mk_decision(sid: str = "v4_fusion") -> StrategyDecision:
    return StrategyDecision(
        action="TRADE",
        direction="UP",
        confidence="HIGH",
        confidence_score=0.9,
        entry_cap=0.65,
        collateral_pct=0.025,
        strategy_id=sid,
        strategy_version="1.0.0",
        entry_reason="r",
        skip_reason=None,
    )


class TestInMemoryShadowRepo:
    @pytest.mark.asyncio
    async def test_save_and_find_by_window(self):
        repo = InMemoryShadowDecisionRepository()
        wk = WindowKey(asset="BTC", window_ts=1)
        await repo.save(wk, [_mk_decision("a"), _mk_decision("b")])
        rows = await repo.find_by_window(wk)
        assert len(rows) == 2
        assert {r.strategy_id for r in rows} == {"a", "b"}

    @pytest.mark.asyncio
    async def test_find_by_strategy_respects_limit(self):
        repo = InMemoryShadowDecisionRepository()
        for i in range(5):
            wk = WindowKey(asset="BTC", window_ts=1 + i)
            await repo.save(wk, [_mk_decision("v4")])
        hits = await repo.find_by_strategy("v4", since_unix=0, limit=3)
        assert len(hits) == 3

    @pytest.mark.asyncio
    async def test_eviction_bounded(self):
        repo = InMemoryShadowDecisionRepository(max_windows=2)
        for i in range(4):
            wk = WindowKey(asset="BTC", window_ts=1 + i)
            await repo.save(wk, [_mk_decision("v4")])
        # Only the last 2 windows should remain.
        assert len(repo._by_window) == 2


class TestInMemoryTallyRepo:
    @pytest.mark.asyncio
    async def test_zero_defaults(self):
        r = InMemoryTallyRepo()
        t = await r.today()
        assert t.wins == 0 and t.losses == 0

    @pytest.mark.asyncio
    async def test_preload(self):
        r = InMemoryTallyRepo()
        r.preload(today=CumulativeTally(wins=5, losses=2, pnl_usdc=Decimal("10")))
        t = await r.today()
        assert t.wins == 5
        assert t.win_rate == 5 / 7

    @pytest.mark.asyncio
    async def test_by_strategy_and_timeframe_sum(self):
        r = InMemoryTallyRepo()
        r.preload(
            by_strategy={
                ("5m", "v4_fusion", "LIVE"): CumulativeTally(
                    wins=2, losses=1, pnl_usdc=Decimal("3"),
                    timeframe="5m", strategy_id="v4_fusion", mode="LIVE",
                ),
                ("5m", "v4_down_only", "GHOST"): CumulativeTally(
                    wins=1, losses=0, pnl_usdc=Decimal("2"),
                    timeframe="5m", strategy_id="v4_down_only", mode="GHOST",
                ),
                ("15m", "v15m_fusion", "LIVE"): CumulativeTally(
                    wins=0, losses=1, pnl_usdc=Decimal("-5"),
                    timeframe="15m", strategy_id="v15m_fusion", mode="LIVE",
                ),
            }
        )
        by = await r.today_by_strategy()
        assert len(by) == 3
        combined_5m = await r.today_combined("5m")
        assert combined_5m.wins == 3
        assert combined_5m.losses == 1
        assert combined_5m.pnl_usdc == Decimal("5")


class TestInMemoryOnChain:
    @pytest.mark.asyncio
    async def test_filters_since_block_and_latest(self):
        q = InMemoryOnChainQuery()
        tx1 = OutflowTx(
            tx_hash="0x1",
            to_addr="0xa",
            amount_usdc=Decimal("5"),
            block_number=10,
            timestamp_unix=1,
        )
        tx2 = OutflowTx(
            tx_hash="0x2",
            to_addr="0xb",
            amount_usdc=Decimal("7"),
            block_number=50,
            timestamp_unix=2,
        )
        q.preload([tx1, tx2], latest_block=100)
        assert [t.tx_hash for t in await q.get_outflows_since("0x", 20)] == ["0x2"]
        assert await q.get_latest_block() == 100

    @pytest.mark.asyncio
    async def test_default_empty(self):
        q = InMemoryOnChainQuery()
        assert await q.get_outflows_since("0x", 0) == []
        assert await q.get_latest_block() == 0
