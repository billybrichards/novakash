"""
Tests: Fee-aware continuation decision logic.
"""

from __future__ import annotations

import pytest
from datetime import datetime

from margin_engine.domain.entities.position import Position
from margin_engine.domain.value_objects import (
    Money,
    Price,
    PositionState,
    StopLevel,
    TradeSide,
    V4Snapshot,
    TimescalePayload,
    Cascade,
)
from margin_engine.application.services.fee_aware_continuation import (
    calculate_fee_adjusted_pnl,
    get_tp_progress,
    should_take_partial_profit,
    calculate_signal_strength,
    calculate_hold_extension,
    check_continuation_alignment,
    fee_aware_continuation_decision,
    ContinuationDecision,
)
from margin_engine.application.services.continuation_alignment import (
    get_timescale_agreement,
    get_regime_quality,
)


class TestCalculateFeeAdjustedPnL:
    def test_long_position_profitable(self):
        pos = Position(
            side=TradeSide.LONG,
            state=PositionState.OPEN,
            entry_price=Price(50000.0),
            notional=Money(1000.0),
            opened_at=datetime.now().timestamp() - 300,
        )
        pnl = calculate_fee_adjusted_pnl(pos, 51000.0)
        assert 15 < pnl < 25

    def test_long_position_loss(self):
        pos = Position(
            side=TradeSide.LONG,
            state=PositionState.OPEN,
            entry_price=Price(50000.0),
            notional=Money(1000.0),
            opened_at=datetime.now().timestamp() - 300,
        )
        pnl = calculate_fee_adjusted_pnl(pos, 49000.0)
        assert pnl < -20

    def test_short_position_profitable(self):
        pos = Position(
            side=TradeSide.SHORT,
            state=PositionState.OPEN,
            entry_price=Price(50000.0),
            notional=Money(1000.0),
            opened_at=datetime.now().timestamp() - 300,
        )
        pnl = calculate_fee_adjusted_pnl(pos, 49000.0)
        assert 15 < pnl < 25

    def test_breakeven_position(self):
        pos = Position(
            side=TradeSide.LONG,
            state=PositionState.OPEN,
            entry_price=Price(50000.0),
            notional=Money(1000.0),
            opened_at=datetime.now().timestamp() - 300,
        )
        pnl = calculate_fee_adjusted_pnl(pos, 50000.0)
        assert -10 < pnl < 0

    def test_closed_position_returns_zero(self):
        pos = Position(
            side=TradeSide.LONG,
            state=PositionState.CLOSED,
            entry_price=Price(50000.0),
            notional=Money(1000.0),
        )
        pnl = calculate_fee_adjusted_pnl(pos, 60000.0)
        assert pnl == 0.0


class TestGetTpProgress:
    def test_progress_at_50_percent(self):
        pos = Position(
            side=TradeSide.LONG,
            state=PositionState.OPEN,
            entry_price=Price(50000.0),
            take_profit=StopLevel(51000.0),
        )
        progress = get_tp_progress(pos, 50500.0)
        assert progress == pytest.approx(0.5, rel=0.01)

    def test_progress_at_75_percent(self):
        pos = Position(
            side=TradeSide.LONG,
            state=PositionState.OPEN,
            entry_price=Price(50000.0),
            take_profit=StopLevel(51000.0),
        )
        progress = get_tp_progress(pos, 50750.0)
        assert progress == pytest.approx(0.75, rel=0.01)

    def test_no_take_profit(self):
        pos = Position(
            side=TradeSide.LONG,
            state=PositionState.OPEN,
            entry_price=Price(50000.0),
        )
        progress = get_tp_progress(pos, 51000.0)
        assert progress is None


class TestShouldTakePartialProfit:
    def test_partial_at_75_percent_tp(self):
        pos = Position(
            side=TradeSide.LONG,
            state=PositionState.OPEN,
            entry_price=Price(50000.0),
            take_profit=StopLevel(51000.0),
        )
        v4 = V4Snapshot(
            asset="BTC",
            ts=datetime.now().timestamp(),
            timescales={
                "15m": TimescalePayload(
                    timescale="15m",
                    status="ok",
                    probability_up=0.55,
                    regime="TRENDING_UP",
                    cascade=Cascade(),
                )
            },
        )
        close_pct = should_take_partial_profit(pos, 50750.0, v4)
        assert close_pct == 0.25

    def test_partial_at_50_percent_weak_signal(self):
        pos = Position(
            side=TradeSide.LONG,
            state=PositionState.OPEN,
            entry_price=Price(50000.0),
            take_profit=StopLevel(51000.0),
        )
        v4 = V4Snapshot(
            asset="BTC",
            ts=datetime.now().timestamp(),
            timescales={
                "15m": TimescalePayload(
                    timescale="15m",
                    status="ok",
                    probability_up=0.52,
                    regime="CHOPPY",
                    cascade=Cascade(),
                )
            },
        )
        # At 50% with weak signal, may return None or 0.5
        close_pct = should_take_partial_profit(pos, 50500.0, v4)
        assert close_pct is None or close_pct == 0.5

    def test_no_partial_at_25_percent(self):
        pos = Position(
            side=TradeSide.LONG,
            state=PositionState.OPEN,
            entry_price=Price(50000.0),
            take_profit=StopLevel(51000.0),
        )
        v4 = V4Snapshot(
            asset="BTC",
            ts=datetime.now().timestamp(),
            timescales={
                "15m": TimescalePayload(
                    timescale="15m",
                    status="ok",
                    probability_up=0.55,
                    regime="TRENDING_UP",
                    cascade=Cascade(),
                )
            },
        )
        close_pct = should_take_partial_profit(pos, 50250.0, v4)
        assert close_pct is None


class TestCalculateSignalStrength:
    def test_strong_signal_high_conviction(self):
        ts = TimescalePayload(
            timescale="15m",
            status="ok",
            probability_up=0.80,
            regime="TRENDING_UP",
            cascade=Cascade(),
        )
        strength = calculate_signal_strength(ts)
        assert strength > 1.8

    def test_weak_signal_low_conviction(self):
        ts = TimescalePayload(
            timescale="15m",
            status="ok",
            probability_up=0.55,
            regime="CHOPPY",
            cascade=Cascade(),
        )
        strength = calculate_signal_strength(ts)
        assert strength < 1.0


class TestCheckContinuationAlignment:
    def test_full_alignment(self):
        pos = Position(side=TradeSide.LONG, entry_timescale="15m")
        v4 = V4Snapshot(
            asset="BTC",
            ts=datetime.now().timestamp(),
            timescales={
                "5m": TimescalePayload(
                    timescale="5m", status="ok", probability_up=0.60, cascade=Cascade()
                ),
                "15m": TimescalePayload(
                    timescale="15m", status="ok", probability_up=0.65, cascade=Cascade()
                ),
                "1h": TimescalePayload(
                    timescale="1h", status="ok", probability_up=0.55, cascade=Cascade()
                ),
                "4h": TimescalePayload(
                    timescale="4h", status="ok", probability_up=0.70, cascade=Cascade()
                ),
            },
        )
        result = check_continuation_alignment(v4, pos)
        assert result.aligned_count == 4
        assert result.should_continue is True
        assert result.hold_mult == 2.0

    def test_strong_alignment(self):
        pos = Position(side=TradeSide.LONG, entry_timescale="15m")
        v4 = V4Snapshot(
            asset="BTC",
            ts=datetime.now().timestamp(),
            timescales={
                "5m": TimescalePayload(
                    timescale="5m", status="ok", probability_up=0.40, cascade=Cascade()
                ),
                "15m": TimescalePayload(
                    timescale="15m", status="ok", probability_up=0.65, cascade=Cascade()
                ),
                "1h": TimescalePayload(
                    timescale="1h", status="ok", probability_up=0.55, cascade=Cascade()
                ),
                "4h": TimescalePayload(
                    timescale="4h", status="ok", probability_up=0.70, cascade=Cascade()
                ),
            },
        )
        result = check_continuation_alignment(v4, pos)
        assert result.aligned_count == 3
        assert result.should_continue is True
        assert result.hold_mult == 1.5

    def test_minimal_alignment(self):
        pos = Position(side=TradeSide.LONG, entry_timescale="15m")
        v4 = V4Snapshot(
            asset="BTC",
            ts=datetime.now().timestamp(),
            timescales={
                "5m": TimescalePayload(
                    timescale="5m", status="ok", probability_up=0.40, cascade=Cascade()
                ),
                "15m": TimescalePayload(
                    timescale="15m", status="ok", probability_up=0.65, cascade=Cascade()
                ),
                "1h": TimescalePayload(
                    timescale="1h", status="ok", probability_up=0.45, cascade=Cascade()
                ),
                "4h": TimescalePayload(
                    timescale="4h", status="ok", probability_up=0.70, cascade=Cascade()
                ),
            },
        )
        result = check_continuation_alignment(v4, pos)
        assert result.aligned_count == 2
        assert result.should_continue is True
        assert result.hold_mult == 1.0

    def test_misalignment_exit(self):
        pos = Position(side=TradeSide.LONG, entry_timescale="15m")
        v4 = V4Snapshot(
            asset="BTC",
            ts=datetime.now().timestamp(),
            timescales={
                "5m": TimescalePayload(
                    timescale="5m", status="ok", probability_up=0.40, cascade=Cascade()
                ),
                "15m": TimescalePayload(
                    timescale="15m", status="ok", probability_up=0.45, cascade=Cascade()
                ),
                "1h": TimescalePayload(
                    timescale="1h", status="ok", probability_up=0.48, cascade=Cascade()
                ),
                "4h": TimescalePayload(
                    timescale="4h", status="ok", probability_up=0.42, cascade=Cascade()
                ),
            },
        )
        result = check_continuation_alignment(v4, pos)
        assert result.aligned_count == 0
        assert result.should_continue is False
        assert result.hold_mult == 0.5


class TestCalculateHoldExtension:
    def test_extended_hold_strong_aligned(self):
        pos = Position(side=TradeSide.LONG, entry_timescale="15m")
        v4 = V4Snapshot(
            asset="BTC",
            ts=datetime.now().timestamp(),
            timescales={
                "5m": TimescalePayload(
                    timescale="5m", status="ok", probability_up=0.70, cascade=Cascade()
                ),
                "15m": TimescalePayload(
                    timescale="15m",
                    status="ok",
                    probability_up=0.75,
                    regime="TRENDING_UP",
                    cascade=Cascade(),
                ),
                "1h": TimescalePayload(
                    timescale="1h", status="ok", probability_up=0.68, cascade=Cascade()
                ),
                "4h": TimescalePayload(
                    timescale="4h", status="ok", probability_up=0.72, cascade=Cascade()
                ),
            },
        )
        extension = calculate_hold_extension(v4, pos, max_extension=2.0)
        assert extension == 2.0

    def test_reduced_hold_weak_signal(self):
        pos = Position(side=TradeSide.LONG, entry_timescale="15m")
        v4 = V4Snapshot(
            asset="BTC",
            ts=datetime.now().timestamp(),
            timescales={
                "15m": TimescalePayload(
                    timescale="15m",
                    status="ok",
                    probability_up=0.52,
                    regime="CHOPPY",
                    cascade=Cascade(),
                )
            },
        )
        extension = calculate_hold_extension(v4, pos, min_conviction=0.10)
        assert extension == 0.5

    def test_normal_hold_mixed_signals(self):
        pos = Position(side=TradeSide.LONG, entry_timescale="15m")
        v4 = V4Snapshot(
            asset="BTC",
            ts=datetime.now().timestamp(),
            timescales={
                "5m": TimescalePayload(
                    timescale="5m", status="ok", probability_up=0.45, cascade=Cascade()
                ),
                "15m": TimescalePayload(
                    timescale="15m",
                    status="ok",
                    probability_up=0.60,
                    regime="MEAN_REVERTING",
                    cascade=Cascade(),
                ),
                "1h": TimescalePayload(
                    timescale="1h", status="ok", probability_up=0.55, cascade=Cascade()
                ),
                "4h": TimescalePayload(
                    timescale="4h", status="ok", probability_up=0.54, cascade=Cascade()
                ),
            },
        )
        extension = calculate_hold_extension(v4, pos)
        assert 0.5 <= extension <= 2.0


class TestFeeAwareContinuationDecision:
    def test_decision_close_partial_at_75_percent(self):
        """At 75% TP, partial take-profit triggers first."""
        pos = Position(
            side=TradeSide.LONG,
            state=PositionState.OPEN,
            entry_price=Price(50000.0),
            take_profit=StopLevel(51000.0),
            notional=Money(1000.0),
            opened_at=datetime.now().timestamp() - 300,
            entry_timescale="15m",
        )
        v4 = V4Snapshot(
            asset="BTC",
            ts=datetime.now().timestamp(),
            timescales={
                "5m": TimescalePayload(
                    timescale="5m", status="ok", probability_up=0.70, cascade=Cascade()
                ),
                "15m": TimescalePayload(
                    timescale="15m",
                    status="ok",
                    probability_up=0.75,
                    regime="TRENDING_UP",
                    cascade=Cascade(),
                ),
                "1h": TimescalePayload(
                    timescale="1h", status="ok", probability_up=0.72, cascade=Cascade()
                ),
                "4h": TimescalePayload(
                    timescale="4h", status="ok", probability_up=0.68, cascade=Cascade()
                ),
            },
        )
        result = fee_aware_continuation_decision(
            position=pos,
            mark_price=50750.0,
            v4=v4,
            fee_aware_enabled=True,
            alignment_enabled=True,
        )
        assert result.decision == ContinuationDecision.CLOSE_PARTIAL
        assert result.partial_close_pct == 0.25

    def test_decision_close_all_misaligned(self):
        """CLOSE_ALL when timescales misaligned."""
        pos = Position(
            side=TradeSide.LONG,
            state=PositionState.OPEN,
            entry_price=Price(50000.0),
            notional=Money(1000.0),
            opened_at=datetime.now().timestamp() - 300,
            entry_timescale="15m",
        )
        v4 = V4Snapshot(
            asset="BTC",
            ts=datetime.now().timestamp(),
            timescales={
                "5m": TimescalePayload(
                    timescale="5m", status="ok", probability_up=0.40, cascade=Cascade()
                ),
                "15m": TimescalePayload(
                    timescale="15m", status="ok", probability_up=0.45, cascade=Cascade()
                ),
                "1h": TimescalePayload(
                    timescale="1h", status="ok", probability_up=0.48, cascade=Cascade()
                ),
                "4h": TimescalePayload(
                    timescale="4h", status="ok", probability_up=0.42, cascade=Cascade()
                ),
            },
        )
        result = fee_aware_continuation_decision(
            position=pos,
            mark_price=50100.0,
            v4=v4,
            fee_aware_enabled=True,
            alignment_enabled=True,
        )
        assert result.decision == ContinuationDecision.CLOSE_ALL
        assert result.timescale_aligned == 0

    def test_decision_continue_breakeven(self):
        """CONTINUE at breakeven with aligned signals."""
        pos = Position(
            side=TradeSide.LONG,
            state=PositionState.OPEN,
            entry_price=Price(50000.0),
            notional=Money(1000.0),
            opened_at=datetime.now().timestamp() - 300,
            entry_timescale="15m",
        )
        v4 = V4Snapshot(
            asset="BTC",
            ts=datetime.now().timestamp(),
            timescales={
                "5m": TimescalePayload(
                    timescale="5m", status="ok", probability_up=0.55, cascade=Cascade()
                ),
                "15m": TimescalePayload(
                    timescale="15m",
                    status="ok",
                    probability_up=0.58,
                    regime="MEAN_REVERTING",
                    cascade=Cascade(),
                ),
                "1h": TimescalePayload(
                    timescale="1h", status="ok", probability_up=0.56, cascade=Cascade()
                ),
                "4h": TimescalePayload(
                    timescale="4h", status="ok", probability_up=0.54, cascade=Cascade()
                ),
            },
        )
        result = fee_aware_continuation_decision(
            position=pos,
            mark_price=50000.0,
            v4=v4,
            fee_aware_enabled=True,
            alignment_enabled=True,
        )
        assert result.decision == ContinuationDecision.CONTINUE


class TestRegimeQuality:
    def test_trending_regime_bonus(self):
        ts = TimescalePayload(
            timescale="15m",
            status="ok",
            probability_up=0.60,
            regime="TRENDING_UP",
            cascade=Cascade(),
        )
        assert get_regime_quality(ts) == 1.2

    def test_choppy_regime_penalty(self):
        ts = TimescalePayload(
            timescale="15m",
            status="ok",
            probability_up=0.60,
            regime="CHOPPY",
            cascade=Cascade(),
        )
        assert get_regime_quality(ts) == 0.7

    def test_no_edge_regime(self):
        ts = TimescalePayload(
            timescale="15m",
            status="ok",
            probability_up=0.60,
            regime="NO_EDGE",
            cascade=Cascade(),
        )
        assert get_regime_quality(ts) == 0.5

    def test_unknown_regime_default(self):
        ts = TimescalePayload(
            timescale="15m",
            status="ok",
            probability_up=0.60,
            regime="UNKNOWN",
            cascade=Cascade(),
        )
        assert get_regime_quality(ts) == 1.0


class TestAlignmentScore:
    def test_get_timescale_agreement(self):
        pos = Position(side=TradeSide.LONG, entry_timescale="15m")
        v4 = V4Snapshot(
            asset="BTC",
            ts=datetime.now().timestamp(),
            timescales={
                "5m": TimescalePayload(
                    timescale="5m", status="ok", probability_up=0.60, cascade=Cascade()
                ),
                "15m": TimescalePayload(
                    timescale="15m", status="ok", probability_up=0.65, cascade=Cascade()
                ),
                "1h": TimescalePayload(
                    timescale="1h", status="ok", probability_up=0.45, cascade=Cascade()
                ),
                "4h": TimescalePayload(
                    timescale="4h", status="ok", probability_up=0.70, cascade=Cascade()
                ),
            },
        )
        breakdown = get_timescale_agreement(v4, pos)
        assert len(breakdown) == 4
        assert breakdown[0].timescale == "5m"
        assert breakdown[0].aligned is True
        assert breakdown[2].timescale == "1h"
        assert breakdown[2].aligned is False
