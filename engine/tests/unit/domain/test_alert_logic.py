"""Unit tests for engine.domain.alert_logic — Phase A (pure, no mocks)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from domain.alert_logic import (
    CONFIDENCE_THRESHOLDS,
    classify_outcome,
    classify_wallet_delta,
    compute_shadow_outcome,
    is_window_stale,
    polymarket_share_payout,
    relabel_confidence_on_override,
    score_signal_health,
)
from domain.alert_values import (
    HealthStatus,
    OutcomeQuadrant,
    ShadowRow,
    WalletDeltaKind,
)


# ---------------------------------------------------------------------------
# score_signal_health
# ---------------------------------------------------------------------------


class TestScoreSignalHealth:
    def test_all_green(self):
        b = score_signal_health(
            vpin=0.60,
            p_up=0.85,
            p_up_distance=0.35,
            sources_agree=True,
            confidence_label="HIGH",
            confidence_override_active=False,
            eval_band_in_optimal=True,
            chainlink_feed_age_s=5.0,
        )
        assert b.status is HealthStatus.OK
        assert b.reasons == ()

    def test_one_amber_degraded(self):
        b = score_signal_health(
            vpin=0.60,
            p_up=0.85,
            p_up_distance=0.35,
            sources_agree=False,  # amber
            confidence_label="HIGH",
            confidence_override_active=False,
            eval_band_in_optimal=True,
            chainlink_feed_age_s=5.0,
        )
        assert b.status is HealthStatus.DEGRADED
        assert "sources:mixed" in b.reasons

    def test_two_amber_escalates_to_unsafe(self):
        b = score_signal_health(
            vpin=0.30,  # amber (low)
            p_up=0.85,
            p_up_distance=0.35,
            sources_agree=False,  # amber (mixed)
            confidence_label="HIGH",
            confidence_override_active=False,
            eval_band_in_optimal=True,
            chainlink_feed_age_s=5.0,
        )
        assert b.status is HealthStatus.UNSAFE

    def test_confidence_none_without_override_is_red(self):
        b = score_signal_health(
            vpin=0.60,
            p_up=0.50,
            p_up_distance=0.30,
            sources_agree=True,
            confidence_label="NONE",
            confidence_override_active=False,
            eval_band_in_optimal=True,
            chainlink_feed_age_s=5.0,
        )
        assert b.status is HealthStatus.UNSAFE
        assert any("confidence:none" in r for r in b.reasons)

    def test_confidence_none_with_override_not_red(self):
        b = score_signal_health(
            vpin=0.60,
            p_up=0.50,
            p_up_distance=0.30,
            sources_agree=True,
            confidence_label="NONE",
            confidence_override_active=True,
            eval_band_in_optimal=True,
            chainlink_feed_age_s=5.0,
        )
        assert b.status is HealthStatus.OK

    def test_stale_feed_is_red(self):
        b = score_signal_health(
            vpin=0.60,
            p_up=0.85,
            p_up_distance=0.35,
            sources_agree=True,
            confidence_label="HIGH",
            confidence_override_active=False,
            eval_band_in_optimal=True,
            chainlink_feed_age_s=45.0,  # > default 30s
        )
        assert b.status is HealthStatus.UNSAFE
        assert any("feed:stale" in r for r in b.reasons)

    def test_vpin_cascade_risk_amber(self):
        b = score_signal_health(
            vpin=0.90,  # cascade-risk
            p_up=0.85,
            p_up_distance=0.35,
            sources_agree=True,
            confidence_label="HIGH",
            confidence_override_active=False,
            eval_band_in_optimal=True,
            chainlink_feed_age_s=5.0,
        )
        assert b.status is HealthStatus.DEGRADED
        assert "vpin:cascade_risk" in b.reasons

    def test_flat_p_up_amber(self):
        b = score_signal_health(
            vpin=0.60,
            p_up=0.52,
            p_up_distance=0.02,
            sources_agree=True,
            confidence_label="LOW",
            confidence_override_active=False,
            eval_band_in_optimal=True,
            chainlink_feed_age_s=5.0,
        )
        assert b.status is HealthStatus.DEGRADED


# ---------------------------------------------------------------------------
# classify_outcome
# ---------------------------------------------------------------------------


class TestClassifyOutcome:
    def test_correct_win(self):
        assert (
            classify_outcome("UP", "UP", Decimal("2.00"))
            is OutcomeQuadrant.CORRECT_WIN
        )

    def test_correct_loss(self):
        assert (
            classify_outcome("UP", "UP", Decimal("-1.00"))
            is OutcomeQuadrant.CORRECT_LOSS
        )

    def test_wrong_win(self):
        assert (
            classify_outcome("UP", "DOWN", Decimal("1.50"))
            is OutcomeQuadrant.WRONG_WIN
        )

    def test_wrong_loss(self):
        assert (
            classify_outcome("DOWN", "UP", Decimal("-3.94"))
            is OutcomeQuadrant.WRONG_LOSS
        )

    def test_zero_pnl_is_loss(self):
        # pnl_usdc > 0 is the win predicate; 0 or negative counts as loss.
        assert (
            classify_outcome("UP", "UP", Decimal("0"))
            is OutcomeQuadrant.CORRECT_LOSS
        )

    def test_bad_direction_raises(self):
        with pytest.raises(ValueError):
            classify_outcome("SIDEWAYS", "UP", Decimal("1"))


# ---------------------------------------------------------------------------
# classify_wallet_delta
# ---------------------------------------------------------------------------


class TestClassifyWalletDelta:
    OWNER = frozenset({"0xOwnerA", "0xOwnerB"})
    POLY = frozenset({"0xCTF", "0xNegRisk"})
    REDEEMER = "0xRedeemer"

    def test_manual_withdrawal(self):
        kind = classify_wallet_delta(
            amount_usdc=Decimal("100"),
            dest_addr="0xOWNERA",  # case-insensitive
            owner_eoas=self.OWNER,
            poly_contracts=self.POLY,
            redeemer_addr=self.REDEEMER,
        )
        assert kind is WalletDeltaKind.MANUAL_WITHDRAWAL

    def test_trading_flow(self):
        kind = classify_wallet_delta(
            amount_usdc=Decimal("5"),
            dest_addr="0xctf",
            owner_eoas=self.OWNER,
            poly_contracts=self.POLY,
            redeemer_addr=self.REDEEMER,
        )
        assert kind is WalletDeltaKind.TRADING_FLOW

    def test_redemption(self):
        kind = classify_wallet_delta(
            amount_usdc=Decimal("20"),
            dest_addr="0xREDEEMER",
            owner_eoas=self.OWNER,
            poly_contracts=self.POLY,
            redeemer_addr=self.REDEEMER,
        )
        assert kind is WalletDeltaKind.REDEMPTION

    def test_unexpected(self):
        kind = classify_wallet_delta(
            amount_usdc=Decimal("1000"),
            dest_addr="0xDEADBEEF",
            owner_eoas=self.OWNER,
            poly_contracts=self.POLY,
            redeemer_addr=self.REDEEMER,
        )
        assert kind is WalletDeltaKind.UNEXPECTED

    def test_drift_when_no_dest(self):
        kind = classify_wallet_delta(
            amount_usdc=Decimal("5"),
            dest_addr=None,
            owner_eoas=self.OWNER,
            poly_contracts=self.POLY,
            redeemer_addr=self.REDEEMER,
        )
        assert kind is WalletDeltaKind.DRIFT


# ---------------------------------------------------------------------------
# polymarket_share_payout + compute_shadow_outcome
# ---------------------------------------------------------------------------


class TestPolymarketSharePayout:
    def test_win_payout_nets_fee(self):
        # 10 shares @ $0.50 = $5 cost. Win pays $10 gross, -7.2% fee = $9.28. Net = $4.28.
        pnl = polymarket_share_payout(shares=10.0, entry_price_cents=0.50, won=True)
        assert pnl == Decimal("4.2800")

    def test_loss_returns_negative_cost(self):
        pnl = polymarket_share_payout(shares=10.0, entry_price_cents=0.50, won=False)
        assert pnl == Decimal("-5.0000")


class TestComputeShadowOutcome:
    def test_trade_correct_win(self):
        row = compute_shadow_outcome(
            timeframe="5m",
            strategy_id="v4_fusion",
            mode="LIVE",
            action="TRADE",
            direction="DOWN",
            confidence="HIGH",
            confidence_score=0.88,
            entry_price_cents=0.49,
            stake_usdc=Decimal("3.94"),
            actual_direction="DOWN",
        )
        assert isinstance(row, ShadowRow)
        assert row.outcome is OutcomeQuadrant.CORRECT_WIN
        assert row.hypothetical_pnl_usdc is not None
        assert row.hypothetical_pnl_usdc > 0

    def test_trade_wrong_loss(self):
        row = compute_shadow_outcome(
            timeframe="5m",
            strategy_id="v10_gate",
            mode="GHOST",
            action="TRADE",
            direction="UP",
            confidence="MODERATE",
            confidence_score=0.62,
            entry_price_cents=0.52,
            stake_usdc=Decimal("5"),
            actual_direction="DOWN",
        )
        assert row.outcome is OutcomeQuadrant.WRONG_LOSS
        assert row.hypothetical_pnl_usdc == Decimal("-5.0000")

    def test_skip_row(self):
        row = compute_shadow_outcome(
            timeframe="15m",
            strategy_id="v15m_up_basic",
            mode="GHOST",
            action="SKIP",
            direction=None,
            confidence=None,
            confidence_score=None,
            entry_price_cents=None,
            stake_usdc=Decimal("0"),
            actual_direction="UP",
            skip_reason="direction_fail",
        )
        assert row.action == "SKIP"
        assert row.outcome is None
        assert row.hypothetical_pnl_usdc is None
        assert row.skip_reason == "direction_fail"

    def test_trade_missing_price_raises(self):
        with pytest.raises(ValueError):
            compute_shadow_outcome(
                timeframe="5m",
                strategy_id="x",
                mode="GHOST",
                action="TRADE",
                direction="UP",
                confidence="HIGH",
                confidence_score=0.9,
                entry_price_cents=None,
                stake_usdc=Decimal("5"),
                actual_direction="UP",
            )


# ---------------------------------------------------------------------------
# relabel_confidence_on_override  (fixes the conf=NONE bug)
# ---------------------------------------------------------------------------


class TestRelabelConfidenceOnOverride:
    def test_override_passed_none_label_becomes_override_label(self):
        gates = [{"name": "regime_risk_off_override", "passed": True}]
        out = relabel_confidence_on_override("NONE", 0.65, gates)
        assert out == "OVERRIDE:risk_off"

    def test_override_passed_null_label_also_becomes_override_label(self):
        gates = [{"name": "regime_risk_off_override", "passed": True}]
        out = relabel_confidence_on_override(None, 0.65, gates)
        assert out == "OVERRIDE:risk_off"

    def test_override_not_passed_falls_back_to_score_bucket(self):
        gates = [{"name": "regime_risk_off_override", "passed": False}]
        out = relabel_confidence_on_override("NONE", 0.65, gates)
        # 0.65 ≥ MODERATE threshold → MODERATE
        assert out == "MODERATE"

    def test_valid_label_passthrough(self):
        out = relabel_confidence_on_override("HIGH", 0.88, [])
        assert out == "HIGH"

    def test_no_gates_no_score_returns_unknown(self):
        assert relabel_confidence_on_override(None, None, []) == "UNKNOWN"

    def test_score_bucket_boundaries(self):
        assert relabel_confidence_on_override(None, 0.86, []) == "HIGH"
        assert relabel_confidence_on_override(None, 0.65, []) == "MODERATE"
        assert relabel_confidence_on_override(None, 0.45, []) == "LOW"
        assert relabel_confidence_on_override(None, 0.21, []) == "NONE"
        assert relabel_confidence_on_override(None, 0.10, []) == "NONE"

    def test_gate_object_with_attrs(self):
        class _G:
            def __init__(self, n, p):
                self.name = n
                self.passed = p

        gates = [_G("regime_risk_off_override", True)]
        out = relabel_confidence_on_override("NONE", 0.5, gates)
        assert out == "OVERRIDE:risk_off"

    def test_confidence_thresholds_canonical(self):
        # Matches pg_signal_repo.py:229 forward map
        assert CONFIDENCE_THRESHOLDS == {
            "HIGH": 0.85,
            "MODERATE": 0.65,
            "LOW": 0.45,
            "NONE": 0.20,
        }


# ---------------------------------------------------------------------------
# is_window_stale
# ---------------------------------------------------------------------------


class TestIsWindowStale:
    def test_before_close(self):
        now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)
        window_ts = int(now.timestamp()) - 100  # opened 100s ago
        assert is_window_stale(window_ts, 300, now) is False

    def test_after_close(self):
        now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)
        window_ts = int(now.timestamp()) - 400  # opened 400s ago, 5m window
        assert is_window_stale(window_ts, 300, now) is True

    def test_at_close_exact(self):
        now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)
        window_ts = int(now.timestamp()) - 300
        # now > close_ts is strict; exactly at close → not stale yet
        assert is_window_stale(window_ts, 300, now) is False

    def test_bad_duration(self):
        now = datetime.now(timezone.utc)
        with pytest.raises(ValueError):
            is_window_stale(int(now.timestamp()), 0, now)
