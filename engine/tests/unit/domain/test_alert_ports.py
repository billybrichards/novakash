"""Unit tests for engine.domain.ports — Phase B alert ports (4.18-4.21)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

import pytest

from domain.alert_values import (
    AlertFooter,
    AlertHeader,
    AlertTier,
    BtcPriceBlock,
    CumulativeTally,
    HealthBadge,
    HealthStatus,
    LifecyclePhase,
    OutflowTx,
    RelayerCooldownPayload,
)
from domain.ports import (
    ShadowDecisionRepository,
    TallyQueryPort,
)
from domain.value_objects import StrategyDecision, WindowKey
from use_cases.ports import AlertRendererPort, OnChainTxQueryPort


class TestAbstractPortsUninstantiable:
    def test_alert_renderer(self):
        with pytest.raises(TypeError):
            AlertRendererPort()  # type: ignore[abstract]

    def test_onchain_tx_query(self):
        with pytest.raises(TypeError):
            OnChainTxQueryPort()  # type: ignore[abstract]

    def test_shadow_decision_repo(self):
        with pytest.raises(TypeError):
            ShadowDecisionRepository()  # type: ignore[abstract]

    def test_tally_query(self):
        with pytest.raises(TypeError):
            TallyQueryPort()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# Fake implementations — prove each port contract is satisfiable
# ---------------------------------------------------------------------------


class _FakeRenderer(AlertRendererPort):
    def __init__(self):
        self.rendered: list[object] = []

    def render(self, payload: object) -> str:
        self.rendered.append(payload)
        return f"<{type(payload).__name__}>"


class _FakeOnChain(OnChainTxQueryPort):
    def __init__(self, txs: Optional[list[OutflowTx]] = None, latest_block: int = 100):
        self._txs = txs or []
        self._latest = latest_block

    async def get_outflows_since(
        self, wallet: str, since_block: int
    ) -> list[OutflowTx]:
        return [t for t in self._txs if t.block_number >= since_block]

    async def get_latest_block(self) -> int:
        return self._latest


class _FakeShadowRepo(ShadowDecisionRepository):
    def __init__(self):
        self.by_window: dict[str, list[StrategyDecision]] = {}

    async def save(
        self, window_key: WindowKey, decisions: list[StrategyDecision]
    ) -> None:
        self.by_window[window_key.key] = list(decisions)

    async def find_by_window(
        self, window_key: WindowKey
    ) -> list[StrategyDecision]:
        return list(self.by_window.get(window_key.key, []))

    async def find_by_strategy(
        self, strategy_id: str, since_unix: int, limit: int = 1000
    ) -> list[StrategyDecision]:
        hits = []
        for decisions in self.by_window.values():
            for d in decisions:
                if d.strategy_id == strategy_id:
                    hits.append(d)
        return hits[:limit]


class _FakeTally(TallyQueryPort):
    async def today(self) -> CumulativeTally:
        return CumulativeTally(wins=3, losses=1, pnl_usdc=Decimal("5.00"))

    async def last_hour(self) -> CumulativeTally:
        return CumulativeTally(wins=1, losses=0, pnl_usdc=Decimal("2.00"))

    async def session(self, since_unix: int) -> CumulativeTally:
        return CumulativeTally(wins=10, losses=4, pnl_usdc=Decimal("18.00"))

    async def today_by_strategy(
        self,
    ) -> dict[tuple[str, str, str], CumulativeTally]:
        return {
            ("5m", "v4_fusion", "LIVE"): CumulativeTally(
                wins=3, losses=1, pnl_usdc=Decimal("5"), timeframe="5m",
                strategy_id="v4_fusion", mode="LIVE",
            ),
        }

    async def today_combined(
        self, timeframe: Optional[str] = None
    ) -> CumulativeTally:
        return CumulativeTally(
            wins=3, losses=1, pnl_usdc=Decimal("5"), timeframe=timeframe
        )


class TestFakeRenderer:
    def test_renders(self):
        r = _FakeRenderer()
        p = RelayerCooldownPayload(
            header=AlertHeader(
                phase=LifecyclePhase.OPS,
                title="t",
                event_ts_unix=1,
                emit_ts_unix=1,
            ),
            footer=AlertFooter(emit_ts_unix=1),
            tier=AlertTier.DIAGNOSTIC,
            resumed=False,
            quota_left=62,
            quota_total=80,
        )
        out = r.render(p)
        assert out == "<RelayerCooldownPayload>"
        assert r.rendered == [p]


class TestFakeOnChain:
    @pytest.mark.asyncio
    async def test_filters_since_block(self):
        tx1 = OutflowTx(
            tx_hash="0x1",
            to_addr="0xa",
            amount_usdc=Decimal("5"),
            block_number=50,
            timestamp_unix=1,
        )
        tx2 = OutflowTx(
            tx_hash="0x2",
            to_addr="0xb",
            amount_usdc=Decimal("7"),
            block_number=150,
            timestamp_unix=2,
        )
        f = _FakeOnChain(txs=[tx1, tx2], latest_block=200)
        hits = await f.get_outflows_since("0xwallet", since_block=100)
        assert [t.tx_hash for t in hits] == ["0x2"]
        assert await f.get_latest_block() == 200


class TestFakeShadowRepo:
    @pytest.mark.asyncio
    async def test_save_and_find(self):
        repo = _FakeShadowRepo()
        wk = WindowKey(asset="BTC", window_ts=1_700_000_000)
        d = StrategyDecision(
            action="TRADE",
            direction="UP",
            confidence="HIGH",
            confidence_score=0.9,
            entry_cap=0.65,
            collateral_pct=0.025,
            strategy_id="v4_fusion",
            strategy_version="4.3.0",
            entry_reason="r",
            skip_reason=None,
        )
        await repo.save(wk, [d])
        rows = await repo.find_by_window(wk)
        assert rows == [d]
        per = await repo.find_by_strategy("v4_fusion", since_unix=0)
        assert per == [d]
        per_none = await repo.find_by_strategy("nope", since_unix=0)
        assert per_none == []

    @pytest.mark.asyncio
    async def test_empty_window_returns_empty_list(self):
        repo = _FakeShadowRepo()
        wk = WindowKey(asset="BTC", window_ts=1)
        assert await repo.find_by_window(wk) == []


class TestFakeTally:
    @pytest.mark.asyncio
    async def test_today_and_by_strategy(self):
        t = _FakeTally()
        today = await t.today()
        assert today.wins == 3
        by = await t.today_by_strategy()
        assert ("5m", "v4_fusion", "LIVE") in by
        assert by[("5m", "v4_fusion", "LIVE")].win_rate == 0.75

    @pytest.mark.asyncio
    async def test_today_combined_with_timeframe_filter(self):
        t = _FakeTally()
        combined = await t.today_combined(timeframe="15m")
        assert combined.timeframe == "15m"
