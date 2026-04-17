"""Phase G.2-G.7 dual-fire tests for TelegramAlerter helpers."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Optional

import pytest

from adapters.alert.telegram_renderer import TelegramRenderer
from adapters.onchain.in_memory_onchain import InMemoryOnChainQuery
from adapters.persistence.in_memory_shadow_decision_repo import (
    InMemoryShadowDecisionRepository,
)
from adapters.persistence.in_memory_tally_repo import InMemoryTallyRepo
from alerts.telegram import TelegramAlerter
from domain.alert_values import OutflowTx
from domain.value_objects import StrategyDecision, WindowKey
from use_cases.alerts import PublishAlertUseCase
from use_cases.ports import AlerterPort, Clock


class _Clock(Clock):
    def now(self) -> float:
        return 1_712_400_000.0


class _CapturingAlerter(AlerterPort):
    def __init__(self):
        self.sent: list[str] = []

    async def send_system_alert(self, message: str) -> None:
        self.sent.append(message)

    async def send_trade_alert(self, window, decision):
        pass

    async def send_skip_summary(self, window, summary):
        pass

    async def send_heartbeat_sitrep(self, sitrep):
        pass


def _wire_alerter(
    *,
    enabled: bool = True,
    onchain=None,
    shadow_repo=None,
    owner_eoas: Optional[frozenset[str]] = None,
) -> tuple[TelegramAlerter, _CapturingAlerter]:
    alerter = TelegramAlerter(
        bot_token="", chat_id="", alerts_paper=True, alerts_live=True, paper_mode=False
    )
    capturing = _CapturingAlerter()
    publish = PublishAlertUseCase(TelegramRenderer(), capturing)
    alerter.set_narrative_v2(
        enabled=enabled,
        publish_uc=publish,
        clock=_Clock(),
        tallies=InMemoryTallyRepo(),
        shadow_repo=shadow_repo,
        onchain=onchain,
        owner_eoas=owner_eoas,
    )
    return alerter, capturing


# ---------------------------------------------------------------------------
# G.2: send_trade_resolved → ResolvedAlertPayload
# ---------------------------------------------------------------------------


class TestG2Resolved:
    @pytest.mark.asyncio
    async def test_wrong_win_quadrant_via_dual_fire(self):
        alerter, cap = _wire_alerter()
        # Fake "Order" — we only need the attributes the helper reads.
        order = SimpleNamespace(
            direction="YES",       # predicted UP
            price=0.52,
            pnl_usd=2.10,
            stake_usd=5.00,
            outcome="WIN",
            order_id="0xabcdef012345",
            metadata={"strategy_id": "v4_fusion"},
        )
        await alerter._emit_narrative_v2_resolved(
            order=order,
            window_ts=1_712_345_678,
            asset="BTC",
            timeframe="5m",
            open_price=75_034.96,
            close_price=74_937.90,   # actual DOWN
        )
        assert len(cap.sent) == 1
        msg = cap.sent[0]
        assert "predicted: UP" in msg
        assert "actual:    DOWN" in msg
        assert "WRONG + WIN" in msg
        assert "+$2.10" in msg

    @pytest.mark.asyncio
    async def test_correct_win_quadrant(self):
        alerter, cap = _wire_alerter()
        order = SimpleNamespace(
            direction="NO",          # predicted DOWN
            price=0.49,
            pnl_usd=4.17,
            stake_usd=3.94,
            outcome="WIN",
            order_id="0x1",
            metadata={"strategy_id": "v4_fusion"},
        )
        await alerter._emit_narrative_v2_resolved(
            order=order,
            window_ts=1_712_345_678,
            asset="BTC",
            timeframe="5m",
            open_price=75_034.96,
            close_price=74_937.90,
        )
        assert "CORRECT + WIN" in cap.sent[0]


# ---------------------------------------------------------------------------
# Per-trade resolved card from reconciler
# ---------------------------------------------------------------------------


class TestPerTradeResolvedV2:
    @pytest.mark.asyncio
    async def test_win_up_emits_correct_win(self):
        alerter, cap = _wire_alerter()
        await alerter.emit_per_trade_resolved_v2(
            direction="YES",  # predicted UP
            outcome="WIN",
            pnl=2.10,
            entry_price=0.52,
            cost=5.00,
            window_ts=1_712_345_678,
            strategy="v4_fusion",
        )
        assert len(cap.sent) == 1
        msg = cap.sent[0]
        assert "CORRECT + WIN" in msg
        assert "+$2.10" in msg

    @pytest.mark.asyncio
    async def test_loss_down_emits_wrong_loss(self):
        alerter, cap = _wire_alerter()
        await alerter.emit_per_trade_resolved_v2(
            direction="NO",    # predicted DOWN
            outcome="LOSS",
            pnl=-4.29,
            entry_price=0.72,
            cost=4.29,
            window_ts=1_712_345_678,
            strategy="v4_fusion",
        )
        assert len(cap.sent) == 1
        msg = cap.sent[0]
        assert "WRONG + LOSS" in msg
        assert "-$4.29" in msg

    @pytest.mark.asyncio
    async def test_silent_when_v2_disabled(self):
        alerter, cap = _wire_alerter(enabled=False)
        await alerter.emit_per_trade_resolved_v2(
            direction="UP",
            outcome="WIN",
            pnl=1.00,
            entry_price=0.50,
            cost=2.00,
            window_ts=1_712_345_678,
            strategy="v4_fusion",
        )
        assert len(cap.sent) == 0


# ---------------------------------------------------------------------------
# G.3: reconcile pass → ReconcilePayload (dedupe across passes)
# ---------------------------------------------------------------------------


class TestG3Reconcile:
    @pytest.mark.asyncio
    async def test_first_pass_emits_drift_for_orphans(self):
        alerter, cap = _wire_alerter()
        live = [
            {
                "strategy": "v4_fusion",
                "direction": "DOWN",
                "outcome": "WIN",
                "pnl": 4.28,
                "stake": 3.94,
                "cost": 3.94,
                "entry_price": 0.49,
                "matched": True,
                "condition_id": "0xmatched",
            },
            {
                "strategy": "orphan",
                "direction": "UP",
                "outcome": "LOSS",
                "pnl": -5.0,
                "stake": 5.0,
                "cost": 5.0,
                "entry_price": None,
                "matched": False,
                "condition_id": "0xorphan1",
            },
            {
                "strategy": "orphan",
                "direction": "DOWN",
                "outcome": "WIN",
                "pnl": 5.0,
                "stake": 5.0,
                "cost": 5.0,
                "entry_price": None,
                "matched": False,
                "condition_id": "0xorphan2",
            },
        ]
        await alerter.emit_reconcile_v2(live_alerts=live, paper_alerts=[])
        assert len(cap.sent) == 1
        msg = cap.sent[0]
        assert "ORPHAN DRIFT" in msg
        assert "0 → 2" in msg
        assert "v4_fusion" in msg

    @pytest.mark.asyncio
    async def test_normalizes_yes_no_to_up_down(self):
        """Polymarket YES/NO side naming must be accepted by emit_reconcile_v2
        and normalized to the UP/DOWN domain direction. 2026-04-17 bug: v2
        reconcile threw ValueError on every resolved live trade because the
        MatchedTradeRow validator rejected 'YES'/'NO'."""
        alerter, cap = _wire_alerter()
        live = [
            {
                "strategy": "v4_fusion",
                "direction": "YES",  # ← legacy Polymarket naming
                "outcome": "WIN",
                "pnl": 1.57,
                "stake": 2.21,
                "cost": 2.21,
                "entry_price": 0.356,
                "matched": True,
                "condition_id": "0xabc",
                "order_id": "0xorder",
            },
            {
                "strategy": "v4_fusion",
                "direction": "NO",
                "outcome": "LOSS",
                "pnl": -2.21,
                "stake": 2.21,
                "cost": 2.21,
                "entry_price": 0.356,
                "matched": True,
                "condition_id": "0xdef",
                "order_id": "0xorder2",
            },
        ]
        # Must not raise. Must emit a reconcile payload with both rows
        # rendered (direction mapped YES→UP, NO→DOWN).
        await alerter.emit_reconcile_v2(live_alerts=live, paper_alerts=[])
        assert len(cap.sent) == 1
        msg = cap.sent[0]
        assert "WIN  UP" in msg
        assert "LOSS  DOWN" in msg

    @pytest.mark.asyncio
    async def test_second_pass_same_orphans_silent(self):
        alerter, cap = _wire_alerter()
        live = [
            {
                "matched": False,
                "condition_id": "0xA",
                "outcome": "LOSS",
                "strategy": "x",
                "direction": "UP",
                "pnl": 0.0,
                "stake": 5.0,
                "cost": 5.0,
                "entry_price": 0.5,
            },
        ]
        await alerter.emit_reconcile_v2(live_alerts=live, paper_alerts=[])
        await alerter.emit_reconcile_v2(live_alerts=live, paper_alerts=[])
        # First emits drift; second emits nothing (same roster, no matched).
        assert len(cap.sent) == 1


# ---------------------------------------------------------------------------
# G.4: window_report → WindowSignalPayload
# ---------------------------------------------------------------------------


class TestG4WindowReport:
    @pytest.mark.asyncio
    async def test_emits_with_btc_block_and_health(self):
        alerter, cap = _wire_alerter()
        await alerter._emit_narrative_v2_window_report(
            asset="BTC",
            timeframe="5m",
            window_ts=1_712_345_678,
            delta_pct=-0.13,
            vpin=0.62,
            open_price=75_034.96,
            now_price=74_937.90,
        )
        assert len(cap.sent) == 1
        msg = cap.sent[0]
        assert "BTC $74,937" in msg
        assert "VPIN" in msg
        assert "health:" in msg


# ---------------------------------------------------------------------------
# G.5: window_open → WindowOpenPayload
# ---------------------------------------------------------------------------


class TestG5WindowOpen:
    @pytest.mark.asyncio
    async def test_gamma_tilt_balanced(self):
        alerter, cap = _wire_alerter()
        await alerter._emit_narrative_v2_window_open(
            asset="BTC",
            timeframe="5m",
            window_ts=1_712_345_678,
            open_price=75_000.0,
            gamma_up_price=0.50,
            gamma_down_price=0.50,
        )
        assert len(cap.sent) == 1
        assert "BALANCED" in cap.sent[0]

    @pytest.mark.asyncio
    async def test_gamma_tilt_up(self):
        alerter, cap = _wire_alerter()
        await alerter._emit_narrative_v2_window_open(
            asset="BTC",
            timeframe="5m",
            window_ts=1_712_345_678,
            open_price=75_000.0,
            gamma_up_price=0.55,
            gamma_down_price=0.45,
        )
        assert "UP" in cap.sent[0]


# ---------------------------------------------------------------------------
# G.6: wallet delta classifier
# ---------------------------------------------------------------------------


class TestG6WalletDelta:
    @pytest.mark.asyncio
    async def test_manual_withdrawal_info(self):
        tx = OutflowTx(
            tx_hash="0xwithdraw",
            to_addr="0xOwner",
            amount_usdc=Decimal("186.62"),
            block_number=50,
            timestamp_unix=1,
        )
        onchain = InMemoryOnChainQuery()
        onchain.preload([tx], latest_block=100)
        alerter, cap = _wire_alerter(
            onchain=onchain, owner_eoas=frozenset({"0xOwner"})
        )
        # First call sets baseline.
        await alerter.emit_wallet_delta_if_any(
            new_wallet_usdc=Decimal("267.03"), wallet_addr="0xWallet"
        )
        assert cap.sent == []
        # Second call: big outflow → MANUAL_WITHDRAWAL INFO tier.
        await alerter.emit_wallet_delta_if_any(
            new_wallet_usdc=Decimal("80.41"), wallet_addr="0xWallet"
        )
        assert len(cap.sent) == 1
        assert "MANUAL WITHDRAWAL" in cap.sent[0]
        assert "🏦" in cap.sent[0]

    @pytest.mark.asyncio
    async def test_unknown_dest_tactical(self):
        tx = OutflowTx(
            tx_hash="0xbad",
            to_addr="0xDEADBEEF",
            amount_usdc=Decimal("100"),
            block_number=50,
            timestamp_unix=1,
        )
        onchain = InMemoryOnChainQuery()
        onchain.preload([tx], latest_block=100)
        alerter, cap = _wire_alerter(
            onchain=onchain, owner_eoas=frozenset({"0xOwner"})
        )
        await alerter.emit_wallet_delta_if_any(
            new_wallet_usdc=Decimal("200"), wallet_addr="0xWallet"
        )
        await alerter.emit_wallet_delta_if_any(
            new_wallet_usdc=Decimal("100"), wallet_addr="0xWallet"
        )
        assert len(cap.sent) == 1
        assert "UNEXPECTED" in cap.sent[0]
        assert "ACTION REQUIRED" in cap.sent[0]

    @pytest.mark.asyncio
    async def test_no_change_silent(self):
        alerter, cap = _wire_alerter(
            onchain=InMemoryOnChainQuery(), owner_eoas=frozenset()
        )
        await alerter.emit_wallet_delta_if_any(
            new_wallet_usdc=Decimal("100")
        )
        await alerter.emit_wallet_delta_if_any(
            new_wallet_usdc=Decimal("100")
        )
        assert cap.sent == []


# ---------------------------------------------------------------------------
# G.7: shadow save + post-resolve shadow report
# ---------------------------------------------------------------------------


class TestG7Shadow:
    @pytest.mark.asyncio
    async def test_save_then_emit_roundtrip(self):
        repo = InMemoryShadowDecisionRepository()
        alerter, cap = _wire_alerter(shadow_repo=repo)
        decisions = [
            StrategyDecision(
                action="TRADE",
                direction="DOWN",
                confidence="HIGH",
                confidence_score=0.88,
                entry_cap=0.49,
                collateral_pct=0.025,
                strategy_id="v4_fusion",
                strategy_version="4.3.0",
                entry_reason="r",
                skip_reason=None,
                metadata={"mode": "LIVE", "stake_usdc": "5.00"},
            ),
            StrategyDecision(
                action="TRADE",
                direction="UP",
                confidence="MODERATE",
                confidence_score=0.62,
                entry_cap=0.52,
                collateral_pct=0.025,
                strategy_id="v10_gate",
                strategy_version="1.0.0",
                entry_reason="r",
                skip_reason=None,
                metadata={"mode": "GHOST", "stake_usdc": "5.00"},
            ),
        ]
        await alerter.shadow_save_decisions(
            asset="BTC",
            window_ts=1_712_345_678,
            timeframe="5m",
            decisions=decisions,
        )
        await alerter.emit_shadow_report_v2(
            asset="BTC",
            timeframe="5m",
            window_ts=1_712_345_678,
            actual_direction="DOWN",
            actual_open_usd=75_034.96,
            actual_close_usd=74_937.90,
        )
        assert len(cap.sent) == 1
        msg = cap.sent[0]
        assert "SHADOW REPORT" in msg
        assert "v4_fusion" in msg
        assert "v10_gate" in msg
        assert "CORRECT + WIN" in msg
        assert "WRONG + LOSS" in msg

    @pytest.mark.asyncio
    async def test_emit_without_persisted_decisions_silent(self):
        repo = InMemoryShadowDecisionRepository()
        alerter, cap = _wire_alerter(shadow_repo=repo)
        await alerter.emit_shadow_report_v2(
            asset="BTC",
            timeframe="5m",
            window_ts=1_712_345_678,
            actual_direction="UP",
            actual_open_usd=75_000.0,
            actual_close_usd=75_100.0,
        )
        assert cap.sent == []

    @pytest.mark.asyncio
    async def test_shadow_save_silent_when_no_repo(self):
        alerter = TelegramAlerter(
            bot_token="",
            chat_id="",
            alerts_paper=True,
            alerts_live=True,
            paper_mode=False,
        )
        # No set_narrative_v2 call → shadow_repo is None
        await alerter.shadow_save_decisions(
            asset="BTC",
            window_ts=1,
            timeframe="5m",
            decisions=[],
        )   # Must not raise.
