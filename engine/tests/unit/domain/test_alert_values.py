"""Unit tests for engine.domain.alert_values — Phase A.

Frozen dataclass validators + enum stability. No mocks.
"""
from __future__ import annotations

from decimal import Decimal

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
    MatchedTradeRow,
    OrphanDrift,
    OutcomeQuadrant,
    OutflowTx,
    RelayerCooldownPayload,
    ReconcilePayload,
    ResolvedAlertPayload,
    ShadowReportPayload,
    ShadowRow,
    StrategyEligibility,
    TradeAlertPayload,
    WalletDelta,
    WalletDeltaKind,
    WalletDeltaPayload,
    WindowOpenPayload,
    WindowSignalPayload,
)


# ---------------------------------------------------------------------------
# Enums are str-backed (matches OrderStatus pattern)
# ---------------------------------------------------------------------------


class TestEnums:
    def test_lifecycle_is_str(self):
        assert isinstance(LifecyclePhase.MARKET.value, str)
        assert LifecyclePhase.DECISION == "DECISION"

    def test_health_status(self):
        assert HealthStatus.OK != HealthStatus.DEGRADED
        assert HealthStatus("OK") is HealthStatus.OK

    def test_outcome_quadrant_four_values(self):
        members = set(OutcomeQuadrant)
        assert len(members) == 4

    def test_wallet_delta_kind_values(self):
        assert "MANUAL_WITHDRAWAL" in {m.value for m in WalletDeltaKind}

    def test_alert_tier_values(self):
        assert {m.value for m in AlertTier} == {
            "TACTICAL",
            "HEARTBEAT",
            "DIAGNOSTIC",
            "INFO",
        }


# ---------------------------------------------------------------------------
# Primitive blocks
# ---------------------------------------------------------------------------


class TestAlertHeader:
    def test_happy(self):
        h = AlertHeader(
            phase=LifecyclePhase.DECISION,
            title="TRADE",
            event_ts_unix=1_000,
            emit_ts_unix=2_000,
        )
        assert h.title == "TRADE"

    def test_empty_title(self):
        with pytest.raises(ValueError, match="title"):
            AlertHeader(
                phase=LifecyclePhase.OPS,
                title="",
                event_ts_unix=1,
                emit_ts_unix=1,
            )

    def test_bad_event_ts(self):
        with pytest.raises(ValueError, match="event_ts_unix"):
            AlertHeader(
                phase=LifecyclePhase.OPS,
                title="x",
                event_ts_unix=0,
                emit_ts_unix=1,
            )

    def test_frozen(self):
        h = AlertHeader(
            phase=LifecyclePhase.MARKET,
            title="x",
            event_ts_unix=1,
            emit_ts_unix=1,
        )
        with pytest.raises(AttributeError):
            h.title = "y"


class TestBtcPriceBlock:
    def test_happy(self):
        b = BtcPriceBlock(now_price_usd=75_000.0, window_open_usd=74_900.0)
        assert b.now_price_usd == 75_000.0

    def test_zero_price(self):
        with pytest.raises(ValueError):
            BtcPriceBlock(now_price_usd=0, window_open_usd=1.0)


class TestCumulativeTally:
    def test_win_rate(self):
        t = CumulativeTally(wins=3, losses=1, pnl_usdc=Decimal("5"))
        assert t.total == 4
        assert t.win_rate == 0.75

    def test_empty(self):
        t = CumulativeTally(wins=0, losses=0, pnl_usdc=Decimal("0"))
        assert t.win_rate is None

    def test_invalid_timeframe(self):
        with pytest.raises(ValueError):
            CumulativeTally(
                wins=0, losses=0, pnl_usdc=Decimal("0"), timeframe="1m"
            )

    def test_invalid_mode(self):
        with pytest.raises(ValueError):
            CumulativeTally(
                wins=0, losses=0, pnl_usdc=Decimal("0"), mode="AGGRESSIVE"
            )

    def test_negative_wins(self):
        with pytest.raises(ValueError):
            CumulativeTally(wins=-1, losses=0, pnl_usdc=Decimal("0"))


class TestHealthBadge:
    def test_happy(self):
        b = HealthBadge(status=HealthStatus.OK, reasons=())
        assert b.status is HealthStatus.OK

    def test_list_reasons_rejected(self):
        with pytest.raises(TypeError):
            HealthBadge(status=HealthStatus.OK, reasons=["foo"])  # must be tuple


class TestShadowRow:
    def test_trade_row(self):
        r = ShadowRow(
            timeframe="5m",
            strategy_id="v4_fusion",
            mode="LIVE",
            action="TRADE",
            direction="DOWN",
            outcome=OutcomeQuadrant.CORRECT_WIN,
            hypothetical_pnl_usdc=Decimal("4.28"),
            entry_price_cents=0.49,
        )
        assert r.strategy_id == "v4_fusion"

    def test_bad_timeframe(self):
        with pytest.raises(ValueError):
            ShadowRow(
                timeframe="10m",
                strategy_id="x",
                mode="GHOST",
                action="SKIP",
                direction=None,
                outcome=None,
                hypothetical_pnl_usdc=None,
                entry_price_cents=None,
            )

    def test_bad_mode(self):
        with pytest.raises(ValueError):
            ShadowRow(
                timeframe="5m",
                strategy_id="x",
                mode="PAUSED",
                action="SKIP",
                direction=None,
                outcome=None,
                hypothetical_pnl_usdc=None,
                entry_price_cents=None,
            )


class TestMatchedTradeRow:
    def test_happy(self):
        m = MatchedTradeRow(
            timeframe="5m",
            strategy_id="v4_fusion",
            order_id="0xabc",
            outcome="WIN",
            direction="DOWN",
            entry_price_cents=0.49,
            pnl_usdc=Decimal("4.28"),
            cost_usdc=Decimal("3.94"),
        )
        assert m.outcome == "WIN"

    def test_bad_outcome(self):
        with pytest.raises(ValueError):
            MatchedTradeRow(
                timeframe="5m",
                strategy_id="x",
                order_id=None,
                outcome="PUSH",
                direction="UP",
                entry_price_cents=0.5,
                pnl_usdc=Decimal("0"),
                cost_usdc=Decimal("0"),
            )


class TestOrphanDrift:
    def test_delta_positive(self):
        o = OrphanDrift(prior_count=11, current_count=26, new_condition_ids=("a",))
        assert o.delta == 15
        assert o.changed is True

    def test_unchanged(self):
        o = OrphanDrift(prior_count=26, current_count=26)
        assert o.changed is False

    def test_negative_count(self):
        with pytest.raises(ValueError):
            OrphanDrift(prior_count=-1, current_count=0)


class TestOutflowTx:
    def test_happy(self):
        tx = OutflowTx(
            tx_hash="0xabc",
            to_addr="0xdead",
            amount_usdc=Decimal("10"),
            block_number=123,
            timestamp_unix=1_700_000_000,
        )
        assert tx.to_addr == "0xdead"

    def test_zero_amount_invalid(self):
        with pytest.raises(ValueError):
            OutflowTx(
                tx_hash="0xabc",
                to_addr="0xdead",
                amount_usdc=Decimal("0"),
                block_number=123,
                timestamp_unix=1,
            )


class TestWalletDelta:
    def test_happy(self):
        d = WalletDelta(
            kind=WalletDeltaKind.MANUAL_WITHDRAWAL,
            amount_usdc=Decimal("-186.62"),
            prior_balance_usdc=Decimal("267.03"),
            new_balance_usdc=Decimal("80.41"),
            dest_addr="0xOwner",
        )
        assert d.kind is WalletDeltaKind.MANUAL_WITHDRAWAL

    def test_negative_balance_invalid(self):
        with pytest.raises(ValueError):
            WalletDelta(
                kind=WalletDeltaKind.DRIFT,
                amount_usdc=Decimal("-1"),
                prior_balance_usdc=Decimal("-5"),
                new_balance_usdc=Decimal("0"),
            )


class TestStrategyEligibility:
    def test_happy(self):
        s = StrategyEligibility(
            strategy_id="v4_fusion",
            strategy_version="4.3.0",
            timeframe="5m",
            mode="LIVE",
            action="TRADE",
            direction="DOWN",
            confidence="HIGH",
            confidence_score=0.68,
        )
        assert s.mode == "LIVE"

    def test_bad_action(self):
        with pytest.raises(ValueError):
            StrategyEligibility(
                strategy_id="x",
                strategy_version="1",
                timeframe="5m",
                mode="GHOST",
                action="MAYBE",
                direction=None,
                confidence=None,
                confidence_score=None,
            )


# ---------------------------------------------------------------------------
# Payload shape smoke tests (construct + frozen check)
# ---------------------------------------------------------------------------


def _mk_header(phase: LifecyclePhase = LifecyclePhase.DECISION) -> AlertHeader:
    return AlertHeader(
        phase=phase, title="t", event_ts_unix=1, emit_ts_unix=2
    )


def _mk_footer() -> AlertFooter:
    return AlertFooter(emit_ts_unix=2)


def _mk_btc() -> BtcPriceBlock:
    return BtcPriceBlock(now_price_usd=75_000.0, window_open_usd=74_900.0)


def _mk_health() -> HealthBadge:
    return HealthBadge(status=HealthStatus.OK, reasons=())


class TestPayloadShapes:
    def test_trade_alert_payload(self):
        p = TradeAlertPayload(
            header=_mk_header(),
            footer=_mk_footer(),
            tier=AlertTier.TACTICAL,
            timeframe="5m",
            strategy_id="v4_fusion",
            strategy_version="4.3.0",
            mode="LIVE",
            direction="DOWN",
            confidence_label="HIGH",
            confidence_score=0.68,
            gate_results=(),
            stake_usdc=Decimal("3.94"),
            fill_price_cents=0.49,
            fill_size_shares=8.04,
            cost_usdc=Decimal("3.94"),
            order_submitted=True,
            order_status="FILLED",
            btc=_mk_btc(),
            health=_mk_health(),
        )
        assert p.order_status == "FILLED"

    def test_trade_alert_bad_status(self):
        with pytest.raises(ValueError):
            TradeAlertPayload(
                header=_mk_header(),
                footer=_mk_footer(),
                tier=AlertTier.TACTICAL,
                timeframe="5m",
                strategy_id="x",
                strategy_version="1",
                mode="LIVE",
                direction="UP",
                confidence_label="HIGH",
                confidence_score=0.9,
                gate_results=(),
                stake_usdc=Decimal("5"),
                fill_price_cents=None,
                fill_size_shares=None,
                cost_usdc=None,
                order_submitted=False,
                order_status="PENDING",  # invalid
                btc=_mk_btc(),
                health=_mk_health(),
            )

    def test_window_signal_payload(self):
        p = WindowSignalPayload(
            header=_mk_header(LifecyclePhase.STATE),
            footer=_mk_footer(),
            tier=AlertTier.HEARTBEAT,
            timeframe="5m",
            btc=_mk_btc(),
            vpin=0.62,
            p_up=0.85,
            p_up_distance=0.35,
            sources_agree=True,
            health=_mk_health(),
            strategies=(),
        )
        assert p.vpin == 0.62

    def test_window_open_payload(self):
        p = WindowOpenPayload(
            header=_mk_header(LifecyclePhase.MARKET),
            footer=_mk_footer(),
            tier=AlertTier.HEARTBEAT,
            timeframe="15m",
            btc=_mk_btc(),
            gamma_up_cents=0.51,
            gamma_down_cents=0.49,
            gamma_tilt="BALANCED",
        )
        assert p.timeframe == "15m"

    def test_reconcile_payload(self):
        p = ReconcilePayload(
            header=_mk_header(LifecyclePhase.OPS),
            footer=_mk_footer(),
            tier=AlertTier.DIAGNOSTIC,
            matched=(),
        )
        assert p.orphan_drift is None

    def test_shadow_report_payload(self):
        p = ShadowReportPayload(
            header=_mk_header(LifecyclePhase.RESOLVE),
            footer=_mk_footer(),
            tier=AlertTier.INFO,
            timeframe="5m",
            window_id="BTC-1000",
            actual_direction="DOWN",
            actual_open_usd=75_034.96,
            actual_close_usd=74_937.90,
            rows=(),
        )
        assert p.actual_direction == "DOWN"

    def test_resolved_alert_payload(self):
        p = ResolvedAlertPayload(
            header=_mk_header(LifecyclePhase.RESOLVE),
            footer=_mk_footer(),
            tier=AlertTier.TACTICAL,
            timeframe="5m",
            strategy_id="v4_fusion",
            mode="LIVE",
            predicted_direction="DOWN",
            actual_direction="DOWN",
            outcome_quadrant=OutcomeQuadrant.CORRECT_WIN,
            pnl_usdc=Decimal("4.28"),
            entry_price_cents=0.49,
            stake_usdc=Decimal("3.94"),
            btc=_mk_btc(),
        )
        assert p.outcome_quadrant is OutcomeQuadrant.CORRECT_WIN

    def test_wallet_delta_payload(self):
        p = WalletDeltaPayload(
            header=_mk_header(LifecyclePhase.OPS),
            footer=_mk_footer(),
            tier=AlertTier.INFO,
            delta=WalletDelta(
                kind=WalletDeltaKind.MANUAL_WITHDRAWAL,
                amount_usdc=Decimal("-186.62"),
                prior_balance_usdc=Decimal("267.03"),
                new_balance_usdc=Decimal("80.41"),
                dest_addr="0xOwner",
            ),
            owner_eoa_matched="0xOwner",
        )
        assert p.delta.kind is WalletDeltaKind.MANUAL_WITHDRAWAL

    def test_relayer_cooldown_payload(self):
        p = RelayerCooldownPayload(
            header=_mk_header(LifecyclePhase.OPS),
            footer=_mk_footer(),
            tier=AlertTier.DIAGNOSTIC,
            resumed=False,
            quota_left=62,
            quota_total=80,
            cooldown_reset_unix=3_000,
            reason="429 RelayerApiException",
        )
        assert p.quota_left == 62
