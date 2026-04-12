"""
Unit tests for regime-adaptive strategy selection (ME-STRAT-04).

Tests cover:
- Regime routing (TRENDING_UP, TRENDING_DOWN, MEAN_REVERTING, CHOPPY, NO_EDGE)
- Trend strategy (entry conditions, sizing, SL/TP)
- Mean-reversion strategy (fade extremes, sizing, SL/TP)
- No-trade strategy (CHOPPY/NO_EDGE)
- Integration with open_position use case
"""

import pytest
from typing import Optional

from margin_engine.domain.strategy import TradeDecision, Regime
from margin_engine.domain.value_objects import (
    V4Snapshot,
    TimescalePayload,
    Quantiles,
    Consensus,
    MacroBias,
    Cascade,
)
from margin_engine.services.regime_trend import TrendStrategy, TrendStrategyConfig
from margin_engine.services.regime_mean_reversion import (
    MeanReversionStrategy,
    MeanReversionConfig,
)
from margin_engine.services.regime_no_trade import NoTradeStrategy
from margin_engine.services.regime_adaptive import RegimeAdaptiveRouter


# ─────────────────────────────────────────────────────────────────────────
# Test fixtures
# ─────────────────────────────────────────────────────────────────────────


def create_v4_snapshot(
    regime: str = "TRENDING_UP",
    probability_up: Optional[float] = 0.65,
    expected_move_bps: Optional[float] = 50.0,
    status: str = "ok",
    timescale: str = "15m",
) -> V4Snapshot:
    """Create a mock V4 snapshot for testing."""
    return V4Snapshot(
        asset="BTC",
        ts=1713000000.0,
        last_price=65000.0,
        timescales={
            timescale: TimescalePayload(
                timescale=timescale,
                status=status,
                probability_up=probability_up,
                regime=regime,
                expected_move_bps=expected_move_bps,
                quantiles_at_close=Quantiles(p10=64500.0, p90=65500.0),
                cascade=Cascade(),
            )
        },
        consensus=Consensus(safe_to_trade=True),
        macro=MacroBias(status="ok"),
    )


# ─────────────────────────────────────────────────────────────────────────
# Trend Strategy Tests
# ─────────────────────────────────────────────────────────────────────────


class TestTrendStrategy:
    """Tests for trend-following strategy."""

    def test_trend_up_strong_probability(self):
        """TRENDING_UP with strong probability → LONG trade."""
        v4 = create_v4_snapshot(regime="TRENDING_UP", probability_up=0.70)
        strategy = TrendStrategy()
        decision = strategy.decide(v4)

        assert decision.direction == "LONG"
        assert decision.is_trade is True
        assert decision.size_mult == 1.2
        assert decision.stop_loss_bps == 150
        assert decision.take_profit_bps == 200
        assert decision.hold_minutes == 60
        assert "TREND_LONG" in decision.reason

    def test_trend_down_strong_probability(self):
        """TRENDING_DOWN with strong probability → SHORT trade."""
        v4 = create_v4_snapshot(regime="TRENDING_DOWN", probability_up=0.30)
        strategy = TrendStrategy()
        decision = strategy.decide(v4)

        assert decision.direction == "SHORT"
        assert decision.is_trade is True
        assert decision.size_mult == 1.2
        assert decision.stop_loss_bps == 150
        assert decision.take_profit_bps == 200
        assert decision.hold_minutes == 60
        assert "TREND_SHORT" in decision.reason

    def test_trend_weak_probability(self):
        """Trending regime with weak probability → no trade."""
        v4 = create_v4_snapshot(
            regime="TRENDING_UP", probability_up=0.53
        )  # Only 3% edge
        strategy = TrendStrategy()
        decision = strategy.decide(v4)

        assert decision.direction is None
        assert decision.is_trade is False
        assert decision.reason == "TREND_TOO_WEAK"

    def test_trend_no_expected_move(self):
        """Trend with zero expected move → no trade."""
        v4 = create_v4_snapshot(
            regime="TRENDING_UP",
            probability_up=0.70,
            expected_move_bps=0.0,
        )
        strategy = TrendStrategy()
        decision = strategy.decide(v4)

        assert decision.direction is None
        assert decision.is_trade is False
        assert decision.reason == "EXPECTED_MOVE_TOO_SMALL"

    def test_trend_not_trending_regime(self):
        """Strong probability but wrong regime → no trade."""
        v4 = create_v4_snapshot(regime="CHOPPY", probability_up=0.75)
        strategy = TrendStrategy()
        decision = strategy.decide(v4)

        assert decision.direction is None
        assert decision.is_trade is False
        assert decision.reason == "NOT_TRENDING"

    def test_trend_custom_config(self):
        """Trend strategy with custom configuration."""
        config = TrendStrategyConfig(
            min_probability=0.60,
            size_mult=1.5,
            stop_loss_bps=200,
            take_profit_bps=300,
            hold_minutes=120,
        )
        v4 = create_v4_snapshot(regime="TRENDING_UP", probability_up=0.70)
        strategy = TrendStrategy(config=config)
        decision = strategy.decide(v4)

        assert decision.direction == "LONG"
        assert decision.size_mult == 1.5
        assert decision.stop_loss_bps == 200
        assert decision.take_profit_bps == 300
        assert decision.hold_minutes == 120

    def test_trend_missing_probability(self):
        """Trend strategy with missing probability → no trade."""
        v4 = create_v4_snapshot(probability_up=None)
        strategy = TrendStrategy()
        decision = strategy.decide(v4)

        assert decision.direction is None
        assert decision.is_trade is False
        assert decision.reason == "PROBABILITY_MISSING"

    def test_trend_missing_timescale(self):
        """Trend strategy with missing timescale → no trade."""
        v4 = V4Snapshot(
            asset="BTC",
            ts=1713000000.0,
            last_price=65000.0,
            timescales={
                "1h": TimescalePayload(
                    timescale="1h",
                    status="ok",
                    probability_up=0.70,
                    regime="TRENDING_UP",
                    expected_move_bps=50.0,
                    quantiles_at_close=Quantiles(p10=64500.0, p90=65500.0),
                    cascade=Cascade(),
                )
            },
            consensus=Consensus(safe_to_trade=True),
            macro=MacroBias(status="ok"),
        )
        strategy = TrendStrategy()
        decision = strategy.decide(v4)

        assert decision.direction is None
        assert decision.is_trade is False
        assert decision.reason == "PRIMARY_TIMESCALE_MISSING"


# ─────────────────────────────────────────────────────────────────────────
# Mean-Reversion Strategy Tests
# ─────────────────────────────────────────────────────────────────────────


class TestMeanReversionStrategy:
    """Tests for mean-reversion (fade) strategy."""

    def test_fade_bullish_extreme(self):
        """Very bullish (p=0.80) in MEAN_REVERTING → SHORT fade."""
        v4 = create_v4_snapshot(regime="MEAN_REVERTING", probability_up=0.80)
        strategy = MeanReversionStrategy()
        decision = strategy.decide(v4)

        assert decision.direction == "SHORT"
        assert decision.is_trade is True
        assert decision.size_mult == 0.8
        assert decision.stop_loss_bps == 80
        assert decision.take_profit_bps == 50
        assert decision.hold_minutes == 15
        assert "FADE_SHORT" in decision.reason

    def test_fade_bearish_extreme(self):
        """Very bearish (p=0.20) in MEAN_REVERTING → LONG fade."""
        v4 = create_v4_snapshot(regime="MEAN_REVERTING", probability_up=0.20)
        strategy = MeanReversionStrategy()
        decision = strategy.decide(v4)

        assert decision.direction == "LONG"
        assert decision.is_trade is True
        assert decision.size_mult == 0.8
        assert decision.stop_loss_bps == 80
        assert decision.take_profit_bps == 50
        assert decision.hold_minutes == 15
        assert "FADE_LONG" in decision.reason

    def test_fade_not_extreme_enough(self):
        """Moderate probability (p=0.60) → no trade (not extreme enough)."""
        v4 = create_v4_snapshot(regime="MEAN_REVERTING", probability_up=0.60)
        strategy = MeanReversionStrategy()
        decision = strategy.decide(v4)

        assert decision.direction is None
        assert decision.is_trade is False
        assert decision.reason == "NOT_EXTREME_ENOUGH"

    def test_fade_weak_probability(self):
        """Extreme but weak fade probability → no trade."""
        # p=0.90 is extreme (90% > 70% threshold)
        # but fade probability = 1 - 0.90 = 0.10 < 0.15 min_fade_conviction
        v4 = create_v4_snapshot(regime="MEAN_REVERTING", probability_up=0.90)
        strategy = MeanReversionStrategy()
        decision = strategy.decide(v4)

        assert decision.direction is None
        assert decision.is_trade is False
        assert decision.reason == "FADE_WEAK"

    def test_fade_not_mean_reverting_regime(self):
        """Extreme probability but wrong regime → no trade."""
        v4 = create_v4_snapshot(regime="TRENDING_UP", probability_up=0.85)
        strategy = MeanReversionStrategy()
        decision = strategy.decide(v4)

        assert decision.direction is None
        assert decision.is_trade is False
        assert decision.reason == "NOT_MEAN_REVERTING"

    def test_fade_custom_config(self):
        """Mean-reversion with custom configuration."""
        config = MeanReversionConfig(
            entry_threshold=0.75,
            size_mult=0.6,
            stop_loss_bps=100,
            take_profit_bps=60,
            hold_minutes=20,
        )
        v4 = create_v4_snapshot(regime="MEAN_REVERTING", probability_up=0.80)
        strategy = MeanReversionStrategy(config=config)
        decision = strategy.decide(v4)

        assert decision.direction == "SHORT"
        assert decision.size_mult == 0.6
        assert decision.stop_loss_bps == 100
        assert decision.take_profit_bps == 60
        assert decision.hold_minutes == 20

    def test_fade_boundary_threshold(self):
        """Exactly at threshold (p=0.70) → trade."""
        v4 = create_v4_snapshot(regime="MEAN_REVERTING", probability_up=0.70)
        strategy = MeanReversionStrategy()
        decision = strategy.decide(v4)

        assert decision.direction == "SHORT"
        assert decision.is_trade is True

    def test_fade_below_lower_threshold(self):
        """Exactly at lower threshold (p=0.30) → trade."""
        v4 = create_v4_snapshot(regime="MEAN_REVERTING", probability_up=0.30)
        strategy = MeanReversionStrategy()
        decision = strategy.decide(v4)

        assert decision.direction == "LONG"
        assert decision.is_trade is True


# ─────────────────────────────────────────────────────────────────────────
# No-Trade Strategy Tests
# ─────────────────────────────────────────────────────────────────────────


class TestNoTradeStrategy:
    """Tests for no-trade strategy."""

    def test_choppy_no_trade(self):
        """CHOPPY regime → no trade by default."""
        v4 = create_v4_snapshot(regime="CHOPPY", probability_up=0.80)
        strategy = NoTradeStrategy()
        decision = strategy.decide(v4)

        assert decision.direction is None
        assert decision.is_trade is False
        assert decision.reason == "REGIME_NO_TRADE"

    def test_no_edge_no_trade(self):
        """NO_EDGE regime → no trade by default."""
        v4 = create_v4_snapshot(regime="NO_EDGE", probability_up=0.80)
        strategy = NoTradeStrategy()
        decision = strategy.decide(v4)

        assert decision.direction is None
        assert decision.is_trade is False
        assert decision.reason == "REGIME_NO_TRADE"

    def test_choppy_allow_trade(self):
        """CHOPPY with allow_trade=True → speculative trade."""
        v4 = create_v4_snapshot(regime="CHOPPY", probability_up=0.80)
        strategy = NoTradeStrategy(allow_trade=True, size_mult=0.1)
        decision = strategy.decide(v4)

        # Still no trade direction, but with size_mult for logging
        assert decision.direction is None
        assert decision.is_trade is False
        assert decision.size_mult == 0.1
        assert "CHOPPY_SPECULATIVE" in decision.reason


# ─────────────────────────────────────────────────────────────────────────
# Regime Router Tests
# ─────────────────────────────────────────────────────────────────────────


class TestRegimeAdaptiveRouter:
    """Tests for regime adaptive router."""

    def test_route_trending_up(self):
        """TRENDING_UP → TrendStrategy → LONG."""
        v4 = create_v4_snapshot(regime="TRENDING_UP", probability_up=0.70)
        router = RegimeAdaptiveRouter()
        decision = router.decide(v4)

        assert decision.direction == "LONG"
        assert decision.is_trade is True
        assert "TRENDING_UP" in decision.reason

    def test_route_trending_down(self):
        """TRENDING_DOWN → TrendStrategy → SHORT."""
        v4 = create_v4_snapshot(regime="TRENDING_DOWN", probability_up=0.25)
        router = RegimeAdaptiveRouter()
        decision = router.decide(v4)

        assert decision.direction == "SHORT"
        assert decision.is_trade is True
        assert "TRENDING_DOWN" in decision.reason

    def test_route_mean_reverting(self):
        """MEAN_REVERTING → MeanReversionStrategy → fade."""
        v4 = create_v4_snapshot(regime="MEAN_REVERTING", probability_up=0.80)
        router = RegimeAdaptiveRouter()
        decision = router.decide(v4)

        assert decision.direction == "SHORT"
        assert decision.is_trade is True
        assert "MEAN_REVERTING" in decision.reason

    def test_route_choppy(self):
        """CHOPPY → NoTradeStrategy → no trade."""
        v4 = create_v4_snapshot(regime="CHOPPY", probability_up=0.75)
        router = RegimeAdaptiveRouter()
        decision = router.decide(v4)

        assert decision.direction is None
        assert decision.is_trade is False
        assert "CHOPPY" in decision.reason or "NO_TRADE" in decision.reason

    def test_route_no_edge(self):
        """NO_EDGE → NoTradeStrategy → no trade."""
        v4 = create_v4_snapshot(regime="NO_EDGE", probability_up=0.75)
        router = RegimeAdaptiveRouter()
        decision = router.decide(v4)

        assert decision.direction is None
        assert decision.is_trade is False
        assert "NO_EDGE" in decision.reason or "NO_TRADE" in decision.reason

    def test_route_unknown_regime(self):
        """Unknown regime → NoTradeStrategy → no trade."""
        v4 = create_v4_snapshot(regime="UNKNOWN_REGIME", probability_up=0.75)
        router = RegimeAdaptiveRouter()
        decision = router.decide(v4)

        assert decision.direction is None
        assert decision.is_trade is False
        assert "REGIME_UNKNOWN" in decision.reason

    def test_router_get_regime(self):
        """Router can extract regime from snapshot."""
        v4 = create_v4_snapshot(regime="TRENDING_UP")
        router = RegimeAdaptiveRouter()
        regime = router.get_regime(v4)

        assert regime == "TRENDING_UP"

    def test_router_get_regime_missing(self):
        """Router returns None when timescale missing."""
        v4 = V4Snapshot(
            asset="BTC",
            ts=1713000000.0,
            last_price=65000.0,
            timescales={
                "1h": TimescalePayload(
                    timescale="1h",
                    status="ok",
                    probability_up=0.70,
                    regime="TRENDING_UP",
                    expected_move_bps=50.0,
                    quantiles_at_close=Quantiles(p10=64500.0, p90=65500.0),
                    cascade=Cascade(),
                )
            },
            consensus=Consensus(safe_to_trade=True),
            macro=MacroBias(status="ok"),
        )
        router = RegimeAdaptiveRouter()
        regime = router.get_regime(v4)

        assert regime is None

    def test_router_strategy_sizing(self):
        """Router applies correct size_mult per regime."""
        # Trend: 1.2x
        v4_trend = create_v4_snapshot(regime="TRENDING_UP", probability_up=0.70)
        router = RegimeAdaptiveRouter()
        decision_trend = router.decide(v4_trend)
        assert decision_trend.size_mult == 1.2

        # Mean-reversion: 0.8x
        v4_mr = create_v4_snapshot(regime="MEAN_REVERTING", probability_up=0.80)
        decision_mr = router.decide(v4_mr)
        assert decision_mr.size_mult == 0.8

    def test_router_strategy_sl_tp(self):
        """Router applies correct SL/TP per regime."""
        # Trend: 150/200 bps
        v4_trend = create_v4_snapshot(regime="TRENDING_UP", probability_up=0.70)
        router = RegimeAdaptiveRouter()
        decision_trend = router.decide(v4_trend)
        assert decision_trend.stop_loss_bps == 150
        assert decision_trend.take_profit_bps == 200

        # Mean-reversion: 80/50 bps
        v4_mr = create_v4_snapshot(regime="MEAN_REVERTING", probability_up=0.80)
        decision_mr = router.decide(v4_mr)
        assert decision_mr.stop_loss_bps == 80
        assert decision_mr.take_profit_bps == 50


# ─────────────────────────────────────────────────────────────────────────
# TradeDecision Tests
# ─────────────────────────────────────────────────────────────────────────


class TestTradeDecision:
    """Tests for TradeDecision dataclass."""

    def test_decision_is_trade(self):
        """TradeDecision correctly identifies trades vs no-trade."""
        trade = TradeDecision(
            direction="LONG",
            size_mult=1.0,
            stop_loss_bps=100,
            take_profit_bps=150,
            hold_minutes=30,
            reason="TEST",
        )
        no_trade = TradeDecision(
            direction=None,
            size_mult=0.0,
            stop_loss_bps=0,
            take_profit_bps=0,
            hold_minutes=0,
            reason="NO_TRADE",
        )

        assert trade.is_trade is True
        assert no_trade.is_trade is False

    def test_decision_pct_conversions(self):
        """TradeDecision correctly converts bps to pct."""
        decision = TradeDecision(
            direction="LONG",
            size_mult=1.0,
            stop_loss_bps=150,
            take_profit_bps=200,
            hold_minutes=30,
            reason="TEST",
        )

        assert decision.stop_loss_pct == 0.015
        assert decision.take_profit_pct == 0.020

    def test_decision_reward_risk_ratio(self):
        """TradeDecision correctly calculates R:R ratio."""
        decision = TradeDecision(
            direction="LONG",
            size_mult=1.0,
            stop_loss_bps=100,
            take_profit_bps=200,
            hold_minutes=30,
            reason="TEST",
        )

        assert decision.reward_risk_ratio == 2.0

    def test_decision_zero_sl(self):
        """TradeDecision handles zero stop_loss_bps."""
        decision = TradeDecision(
            direction=None,
            size_mult=0.0,
            stop_loss_bps=0,
            take_profit_bps=0,
            hold_minutes=0,
            reason="NO_TRADE",
        )

        assert decision.reward_risk_ratio == 0.0


# ─────────────────────────────────────────────────────────────────────────
# Integration Tests
# ─────────────────────────────────────────────────────────────────────────


class TestRegimeAdaptiveIntegration:
    """Integration tests for regime adaptive system."""

    def test_full_workflow_trending(self):
        """Full workflow: TRENDING_UP → router → trend strategy → LONG."""
        v4 = create_v4_snapshot(
            regime="TRENDING_UP",
            probability_up=0.70,
            expected_move_bps=50.0,
        )
        router = RegimeAdaptiveRouter()

        # Get regime
        regime = router.get_regime(v4)
        assert regime == "TRENDING_UP"

        # Get strategy
        strategy = router.get_strategy(regime)
        assert isinstance(strategy, TrendStrategy)

        # Make decision
        decision = router.decide(v4)

        assert decision.direction == "LONG"
        assert decision.is_trade is True
        assert "TRENDING_UP" in decision.reason or "TREND_LONG" in decision.reason

    def test_full_workflow_mean_reverting(self):
        """Full workflow: MEAN_REVERTING → router → MR strategy → fade."""
        v4 = create_v4_snapshot(
            regime="MEAN_REVERTING",
            probability_up=0.85,
        )
        router = RegimeAdaptiveRouter()

        # Get regime
        regime = router.get_regime(v4)
        assert regime == "MEAN_REVERTING"

        # Get strategy
        strategy = router.get_strategy(regime)
        assert isinstance(strategy, MeanReversionStrategy)

        # Make decision
        decision = router.decide(v4)
        assert decision.direction == "SHORT"
        assert decision.is_trade is True
        assert "MEAN_REVERTING" in decision.reason or "FADE_SHORT" in decision.reason

    def test_full_workflow_choppy(self):
        """Full workflow: CHOPPY → router → no-trade strategy → no trade."""
        v4 = create_v4_snapshot(
            regime="CHOPPY",
            probability_up=0.75,
        )
        router = RegimeAdaptiveRouter()

        # Get regime
        regime = router.get_regime(v4)
        assert regime == "CHOPPY"

        # Get strategy
        strategy = router.get_strategy(regime)
        assert isinstance(strategy, NoTradeStrategy)

        # Make decision
        decision = router.decide(v4)
        assert decision.direction is None
        assert decision.is_trade is False
        assert "CHOPPY" in decision.reason or "NO_TRADE" in decision.reason


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
