"""Tests for adapters.alert.telegram_renderer — Phase D."""
from __future__ import annotations

from decimal import Decimal

import pytest

from adapters.alert.telegram_renderer import DIVIDER, TelegramRenderer
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
    ReconcilePayload,
    RelayerCooldownPayload,
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


def _hdr(phase: LifecyclePhase = LifecyclePhase.DECISION, order_id="0xabcdef012345") -> AlertHeader:
    return AlertHeader(
        phase=phase,
        title="BTC 5m",
        event_ts_unix=1_712_345_678,
        emit_ts_unix=1_712_345_700,
        window_id="BTC-1712345678",
        order_id=order_id,
        t_offset_secs=62,
    )


def _foot() -> AlertFooter:
    return AlertFooter(
        emit_ts_unix=1_712_345_700,
        wallet_usdc=Decimal("80.41"),
        paper_mode=False,
        window_id="BTC-1712345678",
        order_id="0xabcdef012345",
    )


def _btc() -> BtcPriceBlock:
    return BtcPriceBlock(
        now_price_usd=74_937.90,
        window_open_usd=75_034.96,
        chainlink_delta_pct=-0.13,
        tiingo_delta_pct=-0.12,
        sources_agree=True,
        t_offset_secs=62,
    )


# ---------------------------------------------------------------------------
# TradeAlertPayload
# ---------------------------------------------------------------------------


class TestRenderTrade:
    def _payload(self, confidence_label="OVERRIDE:risk_off", order_status="FILLED") -> TradeAlertPayload:
        return TradeAlertPayload(
            header=_hdr(),
            footer=_foot(),
            tier=AlertTier.TACTICAL,
            timeframe="5m",
            strategy_id="v4_fusion",
            strategy_version="4.3.0",
            mode="LIVE",
            direction="DOWN",
            confidence_label=confidence_label,
            confidence_score=0.65,
            gate_results=(
                {"name": "confidence", "passed": True},
                {"name": "regime_risk_off_override", "passed": True},
            ),
            stake_usdc=Decimal("3.94"),
            fill_price_cents=0.49,
            fill_size_shares=8.04,
            cost_usdc=Decimal("3.94"),
            order_submitted=True,
            order_status=order_status,
            btc=_btc(),
            health=HealthBadge(status=HealthStatus.OK, reasons=()),
            today_tally=CumulativeTally(wins=3, losses=1, pnl_usdc=Decimal("5.00")),
        )

    def test_contains_divider_and_header(self):
        out = TelegramRenderer().render(self._payload())
        assert DIVIDER in out
        assert "TRADE" not in out  # header title is just "BTC 5m"
        assert "BTC 5m" in out

    def test_conf_override_label_rendered(self):
        out = TelegramRenderer().render(self._payload("OVERRIDE:risk_off"))
        assert "conf=OVERRIDE:risk_off (0.65)" in out

    def test_tallies_in_output(self):
        out = TelegramRenderer().render(self._payload())
        assert "today: 3W/1L" in out
        assert "75%" in out

    def test_filled_shows_stake_and_cost(self):
        out = TelegramRenderer().render(self._payload(order_status="FILLED"))
        assert "stake=$3.94" in out
        assert "filled=$3.94" in out
        assert "8.04sh" in out

    def test_emit_ts_in_footer(self):
        out = TelegramRenderer().render(self._payload())
        assert "emit" in out
        assert "UTC" in out

    def test_live_mode_footer(self):
        out = TelegramRenderer().render(self._payload())
        assert "🔴 LIVE" in out

    def test_order_id_truncated(self):
        out = TelegramRenderer().render(self._payload())
        assert "ord=`0xabcdef01`" in out


# ---------------------------------------------------------------------------
# WindowSignalPayload — strategies grouped by mode
# ---------------------------------------------------------------------------


class TestRenderWindowSignal:
    def test_live_and_ghost_grouping(self):
        strats = [
            StrategyEligibility(
                strategy_id="v4_fusion",
                strategy_version="4.3.0",
                timeframe="5m",
                mode="LIVE",
                action="TRADE",
                direction="DOWN",
                confidence="HIGH",
                confidence_score=0.68,
            ),
            StrategyEligibility(
                strategy_id="v10_gate",
                strategy_version="1.0.0",
                timeframe="5m",
                mode="GHOST",
                action="SKIP",
                direction=None,
                confidence=None,
                confidence_score=None,
                skip_reason="min_dist_fail",
            ),
        ]
        p = WindowSignalPayload(
            header=_hdr(LifecyclePhase.STATE),
            footer=_foot(),
            tier=AlertTier.HEARTBEAT,
            timeframe="5m",
            btc=_btc(),
            vpin=0.62,
            p_up=0.14,
            p_up_distance=0.36,
            sources_agree=True,
            health=HealthBadge(status=HealthStatus.OK, reasons=()),
            strategies=tuple(strats),
        )
        out = TelegramRenderer().render(p)
        assert "*LIVE:*" in out
        assert "*GHOST (shadow):*" in out
        assert "v4_fusion" in out
        assert "v10_gate" in out
        assert "SKIP" in out
        assert "min_dist_fail" in out


# ---------------------------------------------------------------------------
# ReconcilePayload — dedupe + drift
# ---------------------------------------------------------------------------


class TestRenderReconcile:
    def test_drift_visible(self):
        p = ReconcilePayload(
            header=_hdr(LifecyclePhase.OPS),
            footer=_foot(),
            tier=AlertTier.DIAGNOSTIC,
            matched=(),
            orphan_drift=OrphanDrift(
                prior_count=11,
                current_count=26,
                new_condition_ids=("0xnew1", "0xnew2", "0xnew3"),
                worthless_tokens=26,
            ),
        )
        out = TelegramRenderer().render(p)
        assert "ORPHAN DRIFT" in out
        assert "11 → 26" in out
        assert "+15" in out

    def test_unchanged_orphans_compact(self):
        p = ReconcilePayload(
            header=_hdr(LifecyclePhase.OPS),
            footer=_foot(),
            tier=AlertTier.DIAGNOSTIC,
            matched=(),
            orphan_drift=OrphanDrift(prior_count=26, current_count=26),
        )
        out = TelegramRenderer().render(p)
        assert "orphans: 26 (unchanged)" in out

    def test_matched_grouped_by_strategy(self):
        rows = (
            MatchedTradeRow(
                timeframe="5m",
                strategy_id="v4_fusion",
                order_id="0xAAA1111111111",
                outcome="WIN",
                direction="DOWN",
                entry_price_cents=0.49,
                pnl_usdc=Decimal("4.28"),
                cost_usdc=Decimal("3.94"),
            ),
            MatchedTradeRow(
                timeframe="15m",
                strategy_id="v15m_fusion",
                order_id=None,
                outcome="LOSS",
                direction="UP",
                entry_price_cents=0.52,
                pnl_usdc=Decimal("-5.00"),
                cost_usdc=Decimal("5.00"),
            ),
        )
        p = ReconcilePayload(
            header=_hdr(LifecyclePhase.OPS),
            footer=_foot(),
            tier=AlertTier.DIAGNOSTIC,
            matched=rows,
        )
        out = TelegramRenderer().render(p)
        assert "5m v4_fusion" in out
        assert "15m v15m_fusion" in out
        assert "1W / 0L" in out
        assert "0W / 1L" in out


# ---------------------------------------------------------------------------
# ShadowReportPayload
# ---------------------------------------------------------------------------


class TestRenderShadowReport:
    def test_rows_and_actual_line(self):
        rows = (
            ShadowRow(
                timeframe="5m",
                strategy_id="v4_fusion",
                mode="LIVE",
                action="TRADE",
                direction="DOWN",
                outcome=OutcomeQuadrant.CORRECT_WIN,
                hypothetical_pnl_usdc=Decimal("4.17"),
                entry_price_cents=0.49,
            ),
            ShadowRow(
                timeframe="5m",
                strategy_id="v10_gate",
                mode="GHOST",
                action="TRADE",
                direction="UP",
                outcome=OutcomeQuadrant.WRONG_LOSS,
                hypothetical_pnl_usdc=Decimal("-5.00"),
                entry_price_cents=0.52,
            ),
            ShadowRow(
                timeframe="5m",
                strategy_id="v4_up_basic",
                mode="GHOST",
                action="SKIP",
                direction=None,
                outcome=None,
                hypothetical_pnl_usdc=None,
                entry_price_cents=None,
                skip_reason="direction_fail",
            ),
        )
        p = ShadowReportPayload(
            header=_hdr(LifecyclePhase.RESOLVE),
            footer=_foot(),
            tier=AlertTier.INFO,
            timeframe="5m",
            window_id="BTC-1712345678",
            actual_direction="DOWN",
            actual_open_usd=75_034.96,
            actual_close_usd=74_937.90,
            rows=rows,
            live_pnl_today_usdc=Decimal("6.20"),
            ghost_pnl_today_usdc=Decimal("12.40"),
        )
        out = TelegramRenderer().render(p)
        assert "actual: DOWN" in out
        assert "75,034.96" in out
        assert "CORRECT + WIN" in out
        assert "WRONG + LOSS" in out
        assert "skip" in out
        assert "direction_fail" in out
        assert "today LIVE:" in out
        assert "today GHOST:" in out
        assert "edge:" in out


# ---------------------------------------------------------------------------
# ResolvedAlertPayload — four-quadrant
# ---------------------------------------------------------------------------


class TestRenderResolved:
    def test_wrong_win_label(self):
        p = ResolvedAlertPayload(
            header=_hdr(LifecyclePhase.RESOLVE),
            footer=_foot(),
            tier=AlertTier.TACTICAL,
            timeframe="5m",
            strategy_id="v4_fusion",
            mode="LIVE",
            predicted_direction="UP",
            actual_direction="DOWN",
            outcome_quadrant=OutcomeQuadrant.WRONG_WIN,
            pnl_usdc=Decimal("2.10"),
            entry_price_cents=0.52,
            stake_usdc=Decimal("5.00"),
            btc=_btc(),
        )
        out = TelegramRenderer().render(p)
        assert "predicted: UP" in out
        assert "actual:    DOWN" in out
        assert "WRONG + WIN" in out
        assert "+$2.10" in out


# ---------------------------------------------------------------------------
# WalletDeltaPayload
# ---------------------------------------------------------------------------


class TestRenderWalletDelta:
    def test_manual_withdrawal_info(self):
        p = WalletDeltaPayload(
            header=_hdr(LifecyclePhase.OPS),
            footer=_foot(),
            tier=AlertTier.INFO,
            delta=WalletDelta(
                kind=WalletDeltaKind.MANUAL_WITHDRAWAL,
                amount_usdc=Decimal("-186.62"),
                prior_balance_usdc=Decimal("267.03"),
                new_balance_usdc=Decimal("80.41"),
                dest_addr="0xOwnerMeta123",
                tx_hash="0xabcdef0000000001",
            ),
            owner_eoa_matched="0xOwnerMeta123",
        )
        out = TelegramRenderer().render(p)
        assert "🏦" in out
        assert "MANUAL WITHDRAWAL" in out
        assert "-$186.62" in out
        assert "$267.03" in out
        assert "$80.41" in out
        assert "tx:" in out

    def test_unexpected_action_required(self):
        p = WalletDeltaPayload(
            header=_hdr(LifecyclePhase.OPS),
            footer=_foot(),
            tier=AlertTier.TACTICAL,
            delta=WalletDelta(
                kind=WalletDeltaKind.UNEXPECTED,
                amount_usdc=Decimal("-1000"),
                prior_balance_usdc=Decimal("1100"),
                new_balance_usdc=Decimal("100"),
                dest_addr="0xDEADBEEF",
            ),
        )
        out = TelegramRenderer().render(p)
        assert "🚨" in out
        assert "UNEXPECTED" in out
        assert "ACTION REQUIRED" in out

    def test_drift_action_required(self):
        p = WalletDeltaPayload(
            header=_hdr(LifecyclePhase.OPS),
            footer=_foot(),
            tier=AlertTier.TACTICAL,
            delta=WalletDelta(
                kind=WalletDeltaKind.DRIFT,
                amount_usdc=Decimal("-50"),
                prior_balance_usdc=Decimal("100"),
                new_balance_usdc=Decimal("50"),
            ),
        )
        out = TelegramRenderer().render(p)
        assert "DRIFT" in out
        assert "ACTION REQUIRED" in out


# ---------------------------------------------------------------------------
# RelayerCooldownPayload
# ---------------------------------------------------------------------------


class TestRenderRelayerCooldown:
    def test_cooldown(self):
        p = RelayerCooldownPayload(
            header=_hdr(LifecyclePhase.OPS),
            footer=_foot(),
            tier=AlertTier.DIAGNOSTIC,
            resumed=False,
            quota_left=62,
            quota_total=80,
            cooldown_reset_unix=1_712_349_000,
            reason="429 RelayerApiException",
        )
        out = TelegramRenderer().render(p)
        assert "RELAYER COOLDOWN" in out
        assert "62/80" in out

    def test_resumed(self):
        p = RelayerCooldownPayload(
            header=_hdr(LifecyclePhase.OPS),
            footer=_foot(),
            tier=AlertTier.DIAGNOSTIC,
            resumed=True,
            quota_left=80,
            quota_total=80,
        )
        out = TelegramRenderer().render(p)
        assert "RELAYER RESUMED" in out


# ---------------------------------------------------------------------------
# WindowOpenPayload
# ---------------------------------------------------------------------------


class TestRenderWindowOpen:
    def test_gamma_and_btc(self):
        p = WindowOpenPayload(
            header=_hdr(LifecyclePhase.MARKET),
            footer=_foot(),
            tier=AlertTier.HEARTBEAT,
            timeframe="5m",
            btc=_btc(),
            gamma_up_cents=0.51,
            gamma_down_cents=0.49,
            gamma_tilt="BALANCED",
        )
        out = TelegramRenderer().render(p)
        assert "Gamma:" in out
        assert "BALANCED" in out


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_unknown_payload_type_raises(self):
        with pytest.raises(TypeError, match="no render case"):
            TelegramRenderer().render("not a payload")
