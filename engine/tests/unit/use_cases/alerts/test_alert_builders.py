"""Phase C tests — alert builder use cases, fake-port mocks only."""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

import pytest

from domain.alert_values import (
    AlertTier,
    LifecyclePhase,
    MatchedTradeRow,
    OutcomeQuadrant,
    OutflowTx,
    ShadowReportPayload,
    StrategyEligibility,
    TradeAlertPayload,
    WalletDeltaKind,
    WalletDeltaPayload,
    WindowSignalPayload,
)
from domain.ports import ShadowDecisionRepository, TallyQueryPort
from domain.value_objects import StrategyDecision, WindowKey
from use_cases.alerts import (
    BuildReconcileAlertUseCase,
    BuildResolvedAlertUseCase,
    BuildShadowReportUseCase,
    BuildTradeAlertUseCase,
    BuildWalletDeltaAlertUseCase,
    BuildWindowSignalAlertUseCase,
    PublishAlertUseCase,
)
from use_cases.alerts.build_reconcile_alert import BuildReconcileAlertInput
from use_cases.alerts.build_resolved_alert import BuildResolvedAlertInput
from use_cases.alerts.build_shadow_report import BuildShadowReportInput
from use_cases.alerts.build_trade_alert import BuildTradeAlertInput
from use_cases.alerts.build_wallet_delta_alert import (
    BuildWalletDeltaAlertInput,
)
from use_cases.alerts.build_window_signal_alert import (
    BuildWindowSignalAlertInput,
)
from use_cases.ports import (
    AlerterPort,
    AlertRendererPort,
    Clock,
    OnChainTxQueryPort,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeClock(Clock):
    def __init__(self, now: float = 1_712_400_000.0):
        self._now = now

    def now(self) -> float:
        return self._now


class _FakeTallies(TallyQueryPort):
    def __init__(self, today_tally=None):
        self._today = today_tally

    async def today(self):
        return self._today

    async def last_hour(self):
        return None

    async def session(self, since_unix: int):
        return None

    async def today_by_strategy(self):
        return {}

    async def today_combined(self, timeframe=None):
        return None


class _FakeShadowRepo(ShadowDecisionRepository):
    def __init__(self, decisions: Optional[list[StrategyDecision]] = None):
        self._d = decisions or []

    async def save(self, window_key, decisions):
        self._d = list(decisions)

    async def find_by_window(self, window_key):
        return list(self._d)

    async def find_by_strategy(self, strategy_id, since_unix, limit=1000):
        return [x for x in self._d if x.strategy_id == strategy_id][:limit]


class _FakeOnChain(OnChainTxQueryPort):
    def __init__(self, txs: Optional[list[OutflowTx]] = None, latest_block: int = 100):
        self._t = txs or []
        self._latest = latest_block

    async def get_outflows_since(self, wallet, since_block):
        return [t for t in self._t if t.block_number >= since_block]

    async def get_latest_block(self):
        return self._latest


class _FakeRenderer(AlertRendererPort):
    def __init__(self, out: str = "RENDERED"):
        self._out = out
        self.rendered: list[object] = []

    def render(self, payload):
        self.rendered.append(payload)
        return self._out


class _FakeAlerter(AlerterPort):
    def __init__(self, raise_on_send: bool = False):
        self.system_alerts: list[str] = []
        self._raise = raise_on_send

    async def send_system_alert(self, message: str) -> None:
        if self._raise:
            raise RuntimeError("boom")
        self.system_alerts.append(message)

    async def send_trade_alert(self, window, decision):
        pass

    async def send_skip_summary(self, window, summary):
        pass

    async def send_heartbeat_sitrep(self, sitrep):
        pass


# ---------------------------------------------------------------------------
# BuildTradeAlertUseCase — conf=NONE override fix
# ---------------------------------------------------------------------------


def _trade_input(**overrides) -> BuildTradeAlertInput:
    base = dict(
        timeframe="5m",
        strategy_id="v4_fusion",
        strategy_version="4.3.0",
        mode="LIVE",
        direction="DOWN",
        confidence="NONE",
        confidence_score=0.65,
        gate_results=[{"name": "regime_risk_off_override", "passed": True}],
        stake_usdc=Decimal("3.94"),
        fill_price_cents=0.49,
        fill_size_shares=8.04,
        cost_usdc=Decimal("3.94"),
        order_submitted=True,
        order_status="FILLED",
        window_id="BTC-1712345678",
        order_id="0xab" * 16,
        btc_now_usd=75_000.0,
        btc_window_open_usd=74_900.0,
        btc_chainlink_delta_pct=-0.13,
        btc_tiingo_delta_pct=-0.12,
        vpin=0.62,
        p_up=0.14,
        p_up_distance=0.36,
        sources_agree=True,
        chainlink_feed_age_s=3.0,
        eval_band_in_optimal=True,
        event_ts_unix=1_712_345_678,
        t_offset_secs=62,
    )
    base.update(overrides)
    return BuildTradeAlertInput(**base)


class TestBuildTradeAlert:
    @pytest.mark.asyncio
    async def test_conf_none_override_relabeled(self):
        uc = BuildTradeAlertUseCase(_FakeTallies(), _FakeClock())
        payload = await uc.execute(_trade_input())
        assert isinstance(payload, TradeAlertPayload)
        assert payload.confidence_label == "OVERRIDE:risk_off"
        assert payload.confidence_score == 0.65
        # override active → health should not red-flag confidence
        assert "confidence:none_no_override" not in payload.health.reasons

    @pytest.mark.asyncio
    async def test_conf_high_passthrough(self):
        uc = BuildTradeAlertUseCase(_FakeTallies(), _FakeClock())
        payload = await uc.execute(
            _trade_input(
                confidence="HIGH",
                confidence_score=0.88,
                gate_results=[{"name": "confidence", "passed": True}],
            )
        )
        assert payload.confidence_label == "HIGH"

    @pytest.mark.asyncio
    async def test_paper_mode_info_tier(self):
        uc = BuildTradeAlertUseCase(_FakeTallies(), _FakeClock())
        payload = await uc.execute(_trade_input(paper_mode=True))
        assert payload.tier is AlertTier.INFO

    @pytest.mark.asyncio
    async def test_live_mode_tactical_tier(self):
        uc = BuildTradeAlertUseCase(_FakeTallies(), _FakeClock())
        payload = await uc.execute(_trade_input(paper_mode=False))
        assert payload.tier is AlertTier.TACTICAL

    @pytest.mark.asyncio
    async def test_header_phase_reflects_submission(self):
        uc = BuildTradeAlertUseCase(_FakeTallies(), _FakeClock())
        submitted = await uc.execute(_trade_input(order_submitted=True))
        not_submitted = await uc.execute(_trade_input(order_submitted=False))
        assert submitted.header.phase is LifecyclePhase.EXECUTION
        assert not_submitted.header.phase is LifecyclePhase.DECISION


# ---------------------------------------------------------------------------
# BuildWindowSignalAlert
# ---------------------------------------------------------------------------


class TestBuildWindowSignalAlert:
    @pytest.mark.asyncio
    async def test_happy(self):
        uc = BuildWindowSignalAlertUseCase(_FakeClock())
        strat = StrategyEligibility(
            strategy_id="v4_fusion",
            strategy_version="4.3.0",
            timeframe="5m",
            mode="LIVE",
            action="TRADE",
            direction="DOWN",
            confidence="HIGH",
            confidence_score=0.68,
        )
        payload = await uc.execute(
            BuildWindowSignalAlertInput(
                timeframe="5m",
                window_id="BTC-1",
                event_ts_unix=1_000,
                t_offset_secs=120,
                btc_now_usd=75_000.0,
                btc_window_open_usd=75_100.0,
                btc_chainlink_delta_pct=-0.13,
                btc_tiingo_delta_pct=-0.12,
                sources_agree=True,
                vpin=0.62,
                p_up=0.14,
                p_up_distance=0.36,
                strategies=[strat],
            )
        )
        assert isinstance(payload, WindowSignalPayload)
        assert payload.tier is AlertTier.HEARTBEAT
        assert payload.strategies == (strat,)
        assert payload.header.phase is LifecyclePhase.STATE


# ---------------------------------------------------------------------------
# BuildReconcileAlert — dedupe + drift
# ---------------------------------------------------------------------------


class TestBuildReconcileAlert:
    @pytest.mark.asyncio
    async def test_first_pass_emits_drift_for_new_orphans(self):
        uc = BuildReconcileAlertUseCase(_FakeClock())
        payload = await uc.execute(
            BuildReconcileAlertInput(
                matched=[],
                paper_matched=[],
                current_orphan_condition_ids=["a", "b", "c"],
                orphan_auto_redeemed_wins=0,
                orphan_worthless_tokens=3,
                event_ts_unix=1,
            )
        )
        assert payload is not None
        assert payload.orphan_drift is not None
        assert payload.orphan_drift.current_count == 3
        assert payload.orphan_drift.new_condition_ids == ("a", "b", "c")

    @pytest.mark.asyncio
    async def test_second_pass_same_orphans_silent(self):
        uc = BuildReconcileAlertUseCase(_FakeClock())
        # First pass registers baseline.
        await uc.execute(
            BuildReconcileAlertInput(
                matched=[],
                paper_matched=[],
                current_orphan_condition_ids=["a", "b"],
                orphan_auto_redeemed_wins=0,
                orphan_worthless_tokens=2,
                event_ts_unix=1,
            )
        )
        # Second pass, identical roster → silent.
        payload = await uc.execute(
            BuildReconcileAlertInput(
                matched=[],
                paper_matched=[],
                current_orphan_condition_ids=["a", "b"],
                orphan_auto_redeemed_wins=0,
                orphan_worthless_tokens=2,
                event_ts_unix=2,
            )
        )
        assert payload is None

    @pytest.mark.asyncio
    async def test_drift_detected_when_count_grows(self):
        uc = BuildReconcileAlertUseCase(_FakeClock())
        await uc.execute(
            BuildReconcileAlertInput(
                matched=[],
                paper_matched=[],
                current_orphan_condition_ids=["a"] * 1,
                orphan_auto_redeemed_wins=0,
                orphan_worthless_tokens=1,
                event_ts_unix=1,
            )
        )
        payload = await uc.execute(
            BuildReconcileAlertInput(
                matched=[],
                paper_matched=[],
                # mirrors screenshots: 11 → 26
                current_orphan_condition_ids=[str(i) for i in range(26)],
                orphan_auto_redeemed_wins=0,
                orphan_worthless_tokens=26,
                event_ts_unix=2,
            )
        )
        assert payload is not None
        drift = payload.orphan_drift
        assert drift is not None
        assert drift.prior_count == 1
        assert drift.current_count == 26
        assert drift.delta == 25

    @pytest.mark.asyncio
    async def test_matched_only_emits_without_drift(self):
        uc = BuildReconcileAlertUseCase(_FakeClock())
        row = MatchedTradeRow(
            timeframe="5m",
            strategy_id="v4_fusion",
            order_id="0xabc",
            outcome="WIN",
            direction="DOWN",
            entry_price_cents=0.49,
            pnl_usdc=Decimal("4.28"),
            cost_usdc=Decimal("3.94"),
        )
        payload = await uc.execute(
            BuildReconcileAlertInput(
                matched=[row],
                paper_matched=[],
                current_orphan_condition_ids=[],
                orphan_auto_redeemed_wins=0,
                orphan_worthless_tokens=0,
                event_ts_unix=1,
            )
        )
        assert payload is not None
        assert payload.matched == (row,)


# ---------------------------------------------------------------------------
# BuildShadowReport
# ---------------------------------------------------------------------------


def _decision(sid: str, action: str, direction: Optional[str], mode: str = "GHOST", entry_cap: Optional[float] = 0.50) -> StrategyDecision:
    return StrategyDecision(
        action=action,
        direction=direction,
        confidence="MODERATE" if action == "TRADE" else None,
        confidence_score=0.65 if action == "TRADE" else None,
        entry_cap=entry_cap,
        collateral_pct=0.025,
        strategy_id=sid,
        strategy_version="1.0.0",
        entry_reason="r",
        skip_reason=None if action == "TRADE" else "gate_failed",
        metadata={"mode": mode, "stake_usdc": "5.00"},
    )


class TestBuildShadowReport:
    @pytest.mark.asyncio
    async def test_empty_window_returns_none(self):
        repo = _FakeShadowRepo(decisions=[])
        uc = BuildShadowReportUseCase(repo, _FakeClock())
        out = await uc.execute(
            BuildShadowReportInput(
                window_key=WindowKey(asset="BTC", window_ts=1),
                timeframe="5m",
                window_id="BTC-1",
                actual_direction="DOWN",
                actual_open_usd=75_000.0,
                actual_close_usd=74_900.0,
                default_stake_usdc=Decimal("5.00"),
                event_ts_unix=1,
            )
        )
        assert out is None

    @pytest.mark.asyncio
    async def test_grouping_by_timeframe_and_outcomes(self):
        decisions = [
            _decision("v4_fusion", "TRADE", "DOWN", mode="LIVE", entry_cap=0.49),
            _decision("v4_down_only", "TRADE", "DOWN", mode="GHOST", entry_cap=0.51),
            _decision("v10_gate", "TRADE", "UP", mode="GHOST", entry_cap=0.52),
            _decision("v4_up_basic", "SKIP", None, mode="GHOST", entry_cap=None),
        ]
        repo = _FakeShadowRepo(decisions=decisions)
        uc = BuildShadowReportUseCase(repo, _FakeClock())
        out = await uc.execute(
            BuildShadowReportInput(
                window_key=WindowKey(asset="BTC", window_ts=1),
                timeframe="5m",
                window_id="BTC-1",
                actual_direction="DOWN",
                actual_open_usd=75_034.96,
                actual_close_usd=74_937.90,
                default_stake_usdc=Decimal("5.00"),
                event_ts_unix=1,
            )
        )
        assert isinstance(out, ShadowReportPayload)
        assert len(out.rows) == 4
        by_sid = {r.strategy_id: r for r in out.rows}
        assert by_sid["v4_fusion"].outcome is OutcomeQuadrant.CORRECT_WIN
        assert by_sid["v4_down_only"].outcome is OutcomeQuadrant.CORRECT_WIN
        assert by_sid["v10_gate"].outcome is OutcomeQuadrant.WRONG_LOSS
        assert by_sid["v4_up_basic"].action == "SKIP"
        assert by_sid["v4_up_basic"].outcome is None


# ---------------------------------------------------------------------------
# BuildResolvedAlert — four-quadrant
# ---------------------------------------------------------------------------


class TestBuildResolvedAlert:
    @pytest.mark.asyncio
    async def test_wrong_win_quadrant(self):
        uc = BuildResolvedAlertUseCase(_FakeTallies(), _FakeClock())
        out = await uc.execute(
            BuildResolvedAlertInput(
                timeframe="5m",
                strategy_id="v4_fusion",
                mode="LIVE",
                predicted_direction="UP",
                actual_direction="DOWN",
                pnl_usdc=Decimal("2.10"),   # won despite wrong prediction
                entry_price_cents=0.52,
                stake_usdc=Decimal("5.00"),
                window_id="BTC-1",
                order_id="0xabc",
                event_ts_unix=1,
                actual_open_usd=75_000.0,
                actual_close_usd=74_900.0,
            )
        )
        assert out.outcome_quadrant is OutcomeQuadrant.WRONG_WIN
        assert out.btc.close_price_usd == 74_900.0
        assert out.tier is AlertTier.TACTICAL


# ---------------------------------------------------------------------------
# BuildWalletDeltaAlert
# ---------------------------------------------------------------------------


class TestBuildWalletDeltaAlert:
    OWNERS = frozenset({"0xOwner"})
    POLYS = frozenset({"0xCTF"})
    REDEEMER = "0xRedeemer"

    @pytest.mark.asyncio
    async def test_manual_withdrawal_info_tier(self):
        tx = OutflowTx(
            tx_hash="0xabc",
            to_addr="0xOwner",
            amount_usdc=Decimal("186.62"),
            block_number=50,
            timestamp_unix=1,
        )
        uc = BuildWalletDeltaAlertUseCase(_FakeOnChain(txs=[tx]), _FakeClock())
        payload = await uc.execute(
            BuildWalletDeltaAlertInput(
                wallet_addr="0xWallet",
                prior_balance_usdc=Decimal("267.03"),
                new_balance_usdc=Decimal("80.41"),
                since_block=0,
                owner_eoas=self.OWNERS,
                poly_contracts=self.POLYS,
                redeemer_addr=self.REDEEMER,
                event_ts_unix=1,
            )
        )
        assert isinstance(payload, WalletDeltaPayload)
        assert payload.delta.kind is WalletDeltaKind.MANUAL_WITHDRAWAL
        assert payload.tier is AlertTier.INFO
        assert payload.owner_eoa_matched == "0xOwner"

    @pytest.mark.asyncio
    async def test_unknown_destination_tactical(self):
        tx = OutflowTx(
            tx_hash="0xabc",
            to_addr="0xDEADBEEF",
            amount_usdc=Decimal("1000"),
            block_number=50,
            timestamp_unix=1,
        )
        uc = BuildWalletDeltaAlertUseCase(_FakeOnChain(txs=[tx]), _FakeClock())
        payload = await uc.execute(
            BuildWalletDeltaAlertInput(
                wallet_addr="0xWallet",
                prior_balance_usdc=Decimal("1100"),
                new_balance_usdc=Decimal("100"),
                since_block=0,
                owner_eoas=self.OWNERS,
                poly_contracts=self.POLYS,
                redeemer_addr=self.REDEEMER,
                event_ts_unix=1,
            )
        )
        assert payload is not None
        assert payload.delta.kind is WalletDeltaKind.UNEXPECTED
        assert payload.tier is AlertTier.TACTICAL

    @pytest.mark.asyncio
    async def test_no_matching_tx_drift(self):
        uc = BuildWalletDeltaAlertUseCase(_FakeOnChain(txs=[]), _FakeClock())
        payload = await uc.execute(
            BuildWalletDeltaAlertInput(
                wallet_addr="0xWallet",
                prior_balance_usdc=Decimal("100"),
                new_balance_usdc=Decimal("50"),
                since_block=0,
                owner_eoas=self.OWNERS,
                poly_contracts=self.POLYS,
                redeemer_addr=self.REDEEMER,
                event_ts_unix=1,
            )
        )
        assert payload is not None
        assert payload.delta.kind is WalletDeltaKind.DRIFT
        assert payload.tier is AlertTier.TACTICAL

    @pytest.mark.asyncio
    async def test_trading_flow_silent(self):
        tx = OutflowTx(
            tx_hash="0x1",
            to_addr="0xCTF",
            amount_usdc=Decimal("5"),
            block_number=1,
            timestamp_unix=1,
        )
        uc = BuildWalletDeltaAlertUseCase(_FakeOnChain(txs=[tx]), _FakeClock())
        payload = await uc.execute(
            BuildWalletDeltaAlertInput(
                wallet_addr="0xWallet",
                prior_balance_usdc=Decimal("100"),
                new_balance_usdc=Decimal("95"),
                since_block=0,
                owner_eoas=self.OWNERS,
                poly_contracts=self.POLYS,
                redeemer_addr=self.REDEEMER,
                event_ts_unix=1,
            )
        )
        assert payload is None   # silent for routine trading flow

    @pytest.mark.asyncio
    async def test_inflow_skipped(self):
        uc = BuildWalletDeltaAlertUseCase(_FakeOnChain(), _FakeClock())
        payload = await uc.execute(
            BuildWalletDeltaAlertInput(
                wallet_addr="0xWallet",
                prior_balance_usdc=Decimal("100"),
                new_balance_usdc=Decimal("150"),
                since_block=0,
                owner_eoas=self.OWNERS,
                poly_contracts=self.POLYS,
                redeemer_addr=self.REDEEMER,
                event_ts_unix=1,
            )
        )
        assert payload is None


# ---------------------------------------------------------------------------
# PublishAlert
# ---------------------------------------------------------------------------


class TestPublishAlert:
    @pytest.mark.asyncio
    async def test_render_then_send(self):
        r = _FakeRenderer(out="HELLO")
        a = _FakeAlerter()
        p = PublishAlertUseCase(r, a)
        await p.execute(payload="whatever")
        assert r.rendered == ["whatever"]
        assert a.system_alerts == ["HELLO"]

    @pytest.mark.asyncio
    async def test_render_error_swallowed(self):
        class _Bad(AlertRendererPort):
            def render(self, payload):
                raise RuntimeError("render boom")

        p = PublishAlertUseCase(_Bad(), _FakeAlerter())
        # Must not raise — alerts never block main flow.
        result = await p.execute(payload=object())
        assert result is None

    @pytest.mark.asyncio
    async def test_send_error_swallowed(self):
        p = PublishAlertUseCase(_FakeRenderer(), _FakeAlerter(raise_on_send=True))
        result = await p.execute(payload=object())
        assert result is None
