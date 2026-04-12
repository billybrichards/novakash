"""
Unit tests for V4 data flow — construction, gate dispatch, database writes.

Coverage target: 90%+ on V4-related code paths.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from margin_engine.adapters.persistence.pg_repository import (
    ADDITIVE_MIGRATIONS_SQL,
    PgPositionRepository,
)
from margin_engine.domain.entities.portfolio import Portfolio
from margin_engine.domain.entities.position import Position
from margin_engine.domain.ports import ExchangePort, V4SnapshotPort
from margin_engine.domain.value_objects import (
    Cascade,
    Consensus,
    MacroBias,
    Money,
    Price,
    PositionState,
    Quantiles,
    TradeSide,
    V4Snapshot,
    TimescalePayload,
)
from margin_engine.use_cases.open_position import OpenPositionUseCase


# ─── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def mock_v4_snapshot() -> V4Snapshot:
    """Create a complete V4 snapshot with all fields populated."""
    return V4Snapshot.from_dict(
        {
            "asset": "BTC",
            "ts": 1712940000.0,
            "last_price": 65432.5,
            "server_version": "v4.0.0",
            "strategy": "fee_aware_15m",
            "consensus": {
                "safe_to_trade": True,
                "safe_to_trade_reason": "all_sources_agree",
                "reference_price": 65430.0,
                "max_divergence_bps": 2.5,
                "source_agreement_score": 0.98,
            },
            "macro": {
                "bias": "BULL",
                "confidence": 75,
                "direction_gate": "ALLOW_ALL",
                "size_modifier": 1.1,
                "threshold_modifier": 1.0,
                "override_active": False,
                "reasoning": "Strong upward momentum across all timescales",
                "age_s": 30.0,
                "status": "ok",
                "timescale_map": {
                    "5m": {
                        "bias": "BULL",
                        "confidence": 70,
                        "direction_gate": "ALLOW_ALL",
                        "size_modifier": 1.05,
                    },
                    "15m": {
                        "bias": "BULL",
                        "confidence": 75,
                        "direction_gate": "ALLOW_ALL",
                        "size_modifier": 1.1,
                    },
                    "1h": {
                        "bias": "NEUTRAL",
                        "confidence": 45,
                        "direction_gate": "ALLOW_ALL",
                        "size_modifier": 1.0,
                    },
                    "4h": {
                        "bias": "BULL",
                        "confidence": 65,
                        "direction_gate": "ALLOW_ALL",
                        "size_modifier": 1.05,
                    },
                },
            },
            "max_impact_in_window": "LOW",
            "minutes_to_next_high_impact": 120.0,
            "timescales": {
                "5m": {
                    "timescale": "5m",
                    "status": "ok",
                    "window_ts": 1712940000,
                    "window_close_ts": 1712940300,
                    "seconds_to_close": 300,
                    "probability_up": 0.62,
                    "probability_raw": 0.61,
                    "model_version": "v5.2.0",
                    "quantiles_at_close": {
                        "p10": 65200.0,
                        "p25": 65300.0,
                        "p50": 65450.0,
                        "p75": 65600.0,
                        "p90": 65800.0,
                    },
                    "expected_move_bps": 35.0,
                    "vol_forecast_bps": 40.0,
                    "downside_var_bps_p10": -50.0,
                    "upside_var_bps_p90": 60.0,
                    "regime": "TRENDING_UP",
                    "composite_v3": 0.25,
                    "cascade": {
                        "strength": 0.65,
                        "tau1": 120.0,
                        "tau2": 300.0,
                        "exhaustion_t": 450.0,
                        "signal": 0.35,
                    },
                    "direction_agreement": 0.82,
                },
                "15m": {
                    "timescale": "15m",
                    "status": "ok",
                    "window_ts": 1712939400,
                    "window_close_ts": 1712940300,
                    "seconds_to_close": 900,
                    "probability_up": 0.72,
                    "probability_raw": 0.70,
                    "model_version": "v5.2.0",
                    "quantiles_at_close": {
                        "p10": 65100.0,
                        "p25": 65250.0,
                        "p50": 65450.0,
                        "p75": 65650.0,
                        "p90": 65900.0,
                    },
                    "expected_move_bps": 55.0,
                    "vol_forecast_bps": 50.0,
                    "downside_var_bps_p10": -70.0,
                    "upside_var_bps_p90": 80.0,
                    "regime": "TRENDING_UP",
                    "composite_v3": 0.35,
                    "cascade": {
                        "strength": 0.70,
                        "tau1": 180.0,
                        "tau2": 400.0,
                        "exhaustion_t": 600.0,
                        "signal": 0.45,
                    },
                    "alignment": {
                        "direction_agreement": 0.88,
                    },
                },
                "1h": {
                    "timescale": "1h",
                    "status": "ok",
                    "window_ts": 1712937600,
                    "window_close_ts": 1712941200,
                    "seconds_to_close": 3600,
                    "probability_up": 0.58,
                    "probability_raw": 0.57,
                    "model_version": "v5.2.0",
                    "quantiles_at_close": {
                        "p10": 64800.0,
                        "p25": 65100.0,
                        "p50": 65450.0,
                        "p75": 65800.0,
                        "p90": 66200.0,
                    },
                    "expected_move_bps": 85.0,
                    "vol_forecast_bps": 90.0,
                    "downside_var_bps_p10": -100.0,
                    "upside_var_bps_p90": 120.0,
                    "regime": "TRENDING_UP",
                    "composite_v3": 0.20,
                    "cascade": {
                        "strength": 0.55,
                        "tau1": 300.0,
                        "tau2": 600.0,
                        "exhaustion_t": 900.0,
                        "signal": 0.25,
                    },
                    "direction_agreement": 0.75,
                },
                "4h": {
                    "timescale": "4h",
                    "status": "ok",
                    "window_ts": 1712926800,
                    "window_close_ts": 1712941200,
                    "seconds_to_close": 14400,
                    "probability_up": 0.55,
                    "probability_raw": 0.54,
                    "model_version": "v5.2.0",
                    "quantiles_at_close": {
                        "p10": 64000.0,
                        "p25": 64500.0,
                        "p50": 65450.0,
                        "p75": 66400.0,
                        "p90": 67200.0,
                    },
                    "expected_move_bps": 150.0,
                    "vol_forecast_bps": 140.0,
                    "downside_var_bps_p10": -200.0,
                    "upside_var_bps_p90": 250.0,
                    "regime": "TRENDING_UP",
                    "composite_v3": 0.15,
                    "cascade": {
                        "strength": 0.45,
                        "tau1": 600.0,
                        "tau2": 1200.0,
                        "exhaustion_t": 1800.0,
                        "signal": 0.15,
                    },
                    "direction_agreement": 0.68,
                },
            },
        }
    )


@pytest.fixture
def mock_v4_snapshot_cold_start() -> V4Snapshot:
    """Create a V4 snapshot with cold_start status (missing probability)."""
    return V4Snapshot.from_dict(
        {
            "asset": "BTC",
            "ts": 1712940000.0,
            "last_price": 65432.5,
            "timescales": {
                "15m": {
                    "timescale": "15m",
                    "status": "cold_start",
                    "window_ts": 1712939400,
                    "window_close_ts": 1712940300,
                    "seconds_to_close": 900,
                    "probability_up": None,  # Cold start has no probability
                    "regime": None,
                },
            },
        }
    )


@pytest.fixture
def mock_v4_snapshot_missing_fields() -> V4Snapshot:
    """Create a V4 snapshot with minimal fields (defensive parsing test)."""
    return V4Snapshot.from_dict(
        {
            "asset": "BTC",
            "ts": 1712940000.0,
            # Intentionally missing most fields
        }
    )


@pytest.fixture
def mock_exchange() -> AsyncMock:
    """Create a mock exchange port."""
    exchange = AsyncMock(spec=ExchangePort)
    exchange.get_balance = AsyncMock(return_value=MagicMock(amount=500.0))
    exchange.place_market_order = AsyncMock(
        return_value=MagicMock(
            order_id="test_order_123",
            fill_price=Price(value=65435.0),
            filled_notional=10.0,
            commission=0.0045,
            commission_is_actual=True,
        )
    )
    exchange.get_mark = AsyncMock(return_value=Price(value=65435.0))
    return exchange


@pytest.fixture
def mock_portfolio() -> Portfolio:
    """Create a test portfolio."""
    return Portfolio(
        starting_capital=Money.usd(500.0),
        leverage=3,
        max_open_positions=1,
        max_exposure_pct=0.20,
    )


@pytest.fixture
def mock_v4_port(mock_v4_snapshot: V4Snapshot) -> V4SnapshotPort:
    """Create a mock V4 snapshot port."""
    port = AsyncMock(spec=V4SnapshotPort)
    port.get_latest = AsyncMock(return_value=mock_v4_snapshot)
    return port


# ─── Tests: V4 Snapshot Construction ──────────────────────────────────────


class TestV4SnapshotConstruction:
    """Test V4 snapshot construction with all fields."""

    def test_full_snapshot_construction(self, mock_v4_snapshot: V4Snapshot):
        """Test that a full snapshot is constructed correctly."""
        assert mock_v4_snapshot.asset == "BTC"
        assert mock_v4_snapshot.ts == 1712940000.0
        assert mock_v4_snapshot.last_price == 65432.5
        assert mock_v4_snapshot.server_version == "v4.0.0"
        assert mock_v4_snapshot.strategy == "fee_aware_15m"

    def test_consensus_fields(self, mock_v4_snapshot: V4Snapshot):
        """Test consensus object construction."""
        assert mock_v4_snapshot.consensus.safe_to_trade is True
        assert mock_v4_snapshot.consensus.safe_to_trade_reason == "all_sources_agree"
        assert mock_v4_snapshot.consensus.reference_price == 65430.0
        assert mock_v4_snapshot.consensus.max_divergence_bps == 2.5
        assert mock_v4_snapshot.consensus.source_agreement_score == 0.98

    def test_macro_fields(self, mock_v4_snapshot: V4Snapshot):
        """Test macro object construction."""
        assert mock_v4_snapshot.macro.bias == "BULL"
        assert mock_v4_snapshot.macro.confidence == 75
        assert mock_v4_snapshot.macro.direction_gate == "ALLOW_ALL"
        assert mock_v4_snapshot.macro.size_modifier == 1.1
        assert mock_v4_snapshot.macro.status == "ok"
        assert (
            mock_v4_snapshot.macro.reasoning
            == "Strong upward momentum across all timescales"
        )
        assert mock_v4_snapshot.macro.age_s == 30.0

    def test_macro_timescale_map(self, mock_v4_snapshot: V4Snapshot):
        """Test macro timescale map construction."""
        assert "5m" in mock_v4_snapshot.macro.timescale_map
        assert "15m" in mock_v4_snapshot.macro.timescale_map
        assert "1h" in mock_v4_snapshot.macro.timescale_map
        assert "4h" in mock_v4_snapshot.macro.timescale_map

        # Check 15m timescale macro
        m15m = mock_v4_snapshot.macro.for_timescale("15m")
        assert m15m is not None
        assert m15m["bias"] == "BULL"
        assert m15m["confidence"] == 75
        assert m15m["size_modifier"] == 1.1

    def test_all_timescales_present(self, mock_v4_snapshot: V4Snapshot):
        """Test that all 4 timescales are present."""
        assert "5m" in mock_v4_snapshot.timescales
        assert "15m" in mock_v4_snapshot.timescales
        assert "1h" in mock_v4_snapshot.timescales
        assert "4h" in mock_v4_snapshot.timescales

    def test_timescale_payload_fields(self, mock_v4_snapshot: V4Snapshot):
        """Test TimescalePayload construction with all fields."""
        ts15m = mock_v4_snapshot.timescales["15m"]
        assert ts15m.timescale == "15m"
        assert ts15m.status == "ok"
        assert ts15m.window_ts == 1712939400
        assert ts15m.window_close_ts == 1712940300
        assert ts15m.seconds_to_close == 900
        assert ts15m.probability_up == 0.72
        assert ts15m.probability_raw == 0.70
        assert ts15m.model_version == "v5.2.0"
        assert ts15m.expected_move_bps == 55.0
        assert ts15m.vol_forecast_bps == 50.0
        assert ts15m.regime == "TRENDING_UP"
        assert ts15m.composite_v3 == 0.35
        assert ts15m.direction_agreement == 0.88

    def test_quantiles_construction(self, mock_v4_snapshot: V4Snapshot):
        """Test Quantiles object construction."""
        q = mock_v4_snapshot.timescales["15m"].quantiles_at_close
        assert q.p10 == 65100.0
        assert q.p25 == 65250.0
        assert q.p50 == 65450.0
        assert q.p75 == 65650.0
        assert q.p90 == 65900.0

    def test_cascade_construction(self, mock_v4_snapshot: V4Snapshot):
        """Test Cascade object construction."""
        c = mock_v4_snapshot.timescales["15m"].cascade
        assert c.strength == 0.70
        assert c.tau1 == 180.0
        assert c.tau2 == 400.0
        assert c.exhaustion_t == 600.0
        assert c.signal == 0.45

    def test_tradeable_property(self, mock_v4_snapshot: V4Snapshot):
        """Test is_tradeable property."""
        # 15m is tradeable (status=ok, probability exists, regime is TRENDING_UP)
        assert mock_v4_snapshot.timescales["15m"].is_tradeable is True

        # 5m is also tradeable
        assert mock_v4_snapshot.timescales["5m"].is_tradeable is True

    def test_suggested_side(self, mock_v4_snapshot: V4Snapshot):
        """Test suggested_side property."""
        # 15m has p_up=0.72 > 0.5, so LONG
        assert mock_v4_snapshot.timescales["15m"].suggested_side == TradeSide.LONG

        # 5m has p_up=0.62 > 0.5, so LONG
        assert mock_v4_snapshot.timescales["5m"].suggested_side == TradeSide.LONG

    def test_meets_threshold(self, mock_v4_snapshot: V4Snapshot):
        """Test meets_threshold method."""
        # 15m: |0.72 - 0.5| = 0.22 >= 0.10
        assert mock_v4_snapshot.timescales["15m"].meets_threshold(0.10) is True
        assert mock_v4_snapshot.timescales["15m"].meets_threshold(0.20) is True
        assert mock_v4_snapshot.timescales["15m"].meets_threshold(0.25) is False

        # 5m: |0.62 - 0.5| = 0.12 >= 0.10
        assert mock_v4_snapshot.timescales["5m"].meets_threshold(0.10) is True
        assert mock_v4_snapshot.timescales["5m"].meets_threshold(0.15) is False


class TestV4SnapshotDefensiveParsing:
    """Test V4 snapshot defensive parsing with missing fields."""

    def test_cold_start_snapshot(self, mock_v4_snapshot_cold_start: V4Snapshot):
        """Test cold_start status handling."""
        ts15m = mock_v4_snapshot_cold_start.timescales["15m"]
        assert ts15m.status == "cold_start"
        assert ts15m.probability_up is None
        assert ts15m.is_tradeable is False  # Not tradeable when cold_start

    def test_minimal_snapshot(self, mock_v4_snapshot_missing_fields: V4Snapshot):
        """Test minimal snapshot with missing fields."""
        assert mock_v4_snapshot_missing_fields.asset == "BTC"
        assert mock_v4_snapshot_missing_fields.ts == 1712940000.0
        assert mock_v4_snapshot_missing_fields.last_price is None
        assert mock_v4_snapshot_missing_fields.server_version == "unknown"
        assert mock_v4_snapshot_missing_fields.strategy == "unknown"

        # Consensus defaults
        assert mock_v4_snapshot_missing_fields.consensus.safe_to_trade is False

        # Macro defaults
        assert mock_v4_snapshot_missing_fields.macro.bias == "NEUTRAL"
        assert mock_v4_snapshot_missing_fields.macro.confidence == 0
        assert mock_v4_snapshot_missing_fields.macro.direction_gate == "ALLOW_ALL"

    def test_missing_timescales(self, mock_v4_snapshot_missing_fields: V4Snapshot):
        """Test snapshot with no timescales."""
        assert len(mock_v4_snapshot_missing_fields.timescales) == 0
        assert mock_v4_snapshot_missing_fields.get_tradeable("15m") is None

    def test_get_tradeable(self, mock_v4_snapshot: V4Snapshot):
        """Test get_tradeable method."""
        # 15m is tradeable
        assert mock_v4_snapshot.get_tradeable("15m") is not None

        # Non-existent timescale
        assert mock_v4_snapshot.get_tradeable("99m") is None

        # Cold start timescale
        cold = V4Snapshot.from_dict(
            {
                "asset": "BTC",
                "ts": 1712940000.0,
                "timescales": {
                    "15m": {
                        "timescale": "15m",
                        "status": "cold_start",
                        "window_ts": 1712939400,
                        "window_close_ts": 1712940300,
                        "seconds_to_close": 900,
                    },
                },
            }
        )
        assert cold.get_tradeable("15m") is None


# ─── Tests: Gate Dispatcher ───────────────────────────────────────────────


class TestV4GateDispatcher:
    """Test V4 gate dispatcher in OpenPositionUseCase."""

    @pytest.mark.asyncio
    async def test_all_gates_pass(
        self, mock_exchange, mock_portfolio, mock_v4_port, mock_v4_snapshot
    ):
        """Test that V4 snapshot data is correctly structured and accessible."""
        # This test verifies V4 data structure rather than full execution path
        # (execution path tested in other test files like test_open_position_macro_advisory.py)

        # Verify the v4 snapshot is valid
        payload = mock_v4_snapshot.timescales.get("15m")
        assert payload is not None
        assert payload.is_tradeable is True
        assert payload.meets_threshold(0.10) is True
        assert payload.suggested_side == TradeSide.LONG
        assert payload.probability_up == 0.72
        assert payload.regime == "TRENDING_UP"
        assert payload.expected_move_bps == 55.0
        assert payload.composite_v3 == 0.35
        assert payload.quantiles_at_close.p10 == 65100.0
        assert payload.quantiles_at_close.p90 == 65900.0
        assert payload.cascade.strength == 0.70
        assert payload.cascade.exhaustion_t == 600.0

        # Verify top-level V4 fields
        assert mock_v4_snapshot.consensus.safe_to_trade is True
        assert mock_v4_snapshot.macro.bias == "BULL"
        assert mock_v4_snapshot.macro.confidence == 75
        assert mock_v4_snapshot.macro.direction_gate == "ALLOW_ALL"
        assert mock_v4_snapshot.macro.size_modifier == 1.1

        # Verify per-timescale macro map
        m15m = mock_v4_snapshot.macro.for_timescale("15m")
        assert m15m is not None
        assert m15m["bias"] == "BULL"
        assert m15m["confidence"] == 75

        # Verify all timescales are present
        assert "5m" in mock_v4_snapshot.timescales
        assert "15m" in mock_v4_snapshot.timescales
        assert "1h" in mock_v4_snapshot.timescales
        assert "4h" in mock_v4_snapshot.timescales

        # Verify 5m, 1h, 4h data
        assert mock_v4_snapshot.timescales["5m"].probability_up == 0.62
        assert mock_v4_snapshot.timescales["1h"].probability_up == 0.58
        assert mock_v4_snapshot.timescales["4h"].probability_up == 0.55

    @pytest.mark.asyncio
    async def test_gate_not_tradeable(
        self, mock_exchange, mock_portfolio, mock_v4_snapshot_cold_start
    ):
        """Test gate ①: not tradeable (cold_start)."""
        mock_v4_port = AsyncMock(spec=V4SnapshotPort)
        mock_v4_port.get_latest = AsyncMock(return_value=mock_v4_snapshot_cold_start)

        use_case = OpenPositionUseCase(
            exchange=mock_exchange,
            portfolio=mock_portfolio,
            repository=AsyncMock(),
            alerts=AsyncMock(),
            probability_port=AsyncMock(),
            signal_port=AsyncMock(),
            v4_snapshot_port=mock_v4_port,
            engine_use_v4_actions=True,
            v4_primary_timescale="15m",
        )

        result = await use_case.execute()

        # Should skip due to not_tradeable
        assert result is None

    @pytest.mark.asyncio
    async def test_gate_consensus_fail(self, mock_exchange, mock_portfolio):
        """Test gate ②: consensus.safe_to_trade=False."""
        v4_snapshot = V4Snapshot.from_dict(
            {
                "asset": "BTC",
                "ts": 1712940000.0,
                "last_price": 65432.5,
                "consensus": {
                    "safe_to_trade": False,
                    "safe_to_trade_reason": "source_divergence_high",
                },
                "timescales": {
                    "15m": {
                        "timescale": "15m",
                        "status": "ok",
                        "window_ts": 1712939400,
                        "window_close_ts": 1712940300,
                        "seconds_to_close": 900,
                        "probability_up": 0.72,
                        "regime": "TRENDING_UP",
                    },
                },
            }
        )

        mock_v4_port = AsyncMock(spec=V4SnapshotPort)
        mock_v4_port.get_latest = AsyncMock(return_value=v4_snapshot)

        use_case = OpenPositionUseCase(
            exchange=mock_exchange,
            portfolio=mock_portfolio,
            repository=AsyncMock(),
            alerts=AsyncMock(),
            probability_port=AsyncMock(),
            signal_port=AsyncMock(),
            v4_snapshot_port=mock_v4_port,
            engine_use_v4_actions=True,
            v4_primary_timescale="15m",
        )

        result = await use_case.execute()

        # Should skip due to consensus_fail
        assert result is None

    @pytest.mark.asyncio
    async def test_gate_conviction_below_threshold(self, mock_exchange, mock_portfolio):
        """Test gate ⑥: conviction below entry edge."""
        v4_snapshot = V4Snapshot.from_dict(
            {
                "asset": "BTC",
                "ts": 1712940000.0,
                "last_price": 65432.5,
                "consensus": {"safe_to_trade": True},
                "macro": {
                    "bias": "NEUTRAL",
                    "confidence": 50,
                    "direction_gate": "ALLOW_ALL",
                    "status": "ok",
                },
                "timescales": {
                    "15m": {
                        "timescale": "15m",
                        "status": "ok",
                        "window_ts": 1712939400,
                        "window_close_ts": 1712940300,
                        "seconds_to_close": 900,
                        "probability_up": 0.55,  # |0.55 - 0.5| = 0.05 < 0.10
                        "regime": "TRENDING_UP",
                        "expected_move_bps": 55.0,
                    },
                },
            }
        )

        mock_v4_port = AsyncMock(spec=V4SnapshotPort)
        mock_v4_port.get_latest = AsyncMock(return_value=v4_snapshot)

        use_case = OpenPositionUseCase(
            exchange=mock_exchange,
            portfolio=mock_portfolio,
            repository=AsyncMock(),
            alerts=AsyncMock(),
            probability_port=AsyncMock(),
            signal_port=AsyncMock(),
            v4_snapshot_port=mock_v4_port,
            engine_use_v4_actions=True,
            v4_primary_timescale="15m",
            v4_entry_edge=0.10,
        )

        result = await use_case.execute()

        # Should skip due to conviction_below_threshold
        assert result is None

    @pytest.mark.asyncio
    async def test_gate_expected_move_below_fee_wall(
        self, mock_exchange, mock_portfolio
    ):
        """Test gate ⑦: expected move below fee wall."""
        v4_snapshot = V4Snapshot.from_dict(
            {
                "asset": "BTC",
                "ts": 1712940000.0,
                "last_price": 65432.5,
                "consensus": {"safe_to_trade": True},
                "macro": {
                    "bias": "NEUTRAL",
                    "confidence": 50,
                    "direction_gate": "ALLOW_ALL",
                    "status": "ok",
                },
                "timescales": {
                    "15m": {
                        "timescale": "15m",
                        "status": "ok",
                        "window_ts": 1712939400,
                        "window_close_ts": 1712940300,
                        "seconds_to_close": 900,
                        "probability_up": 0.72,
                        "regime": "TRENDING_UP",
                        "expected_move_bps": 10.0,  # 10 bps < 15 bps fee wall
                    },
                },
            }
        )

        mock_v4_port = AsyncMock(spec=V4SnapshotPort)
        mock_v4_port.get_latest = AsyncMock(return_value=v4_snapshot)

        use_case = OpenPositionUseCase(
            exchange=mock_exchange,
            portfolio=mock_portfolio,
            repository=AsyncMock(),
            alerts=AsyncMock(),
            probability_port=AsyncMock(),
            signal_port=AsyncMock(),
            v4_snapshot_port=mock_v4_port,
            engine_use_v4_actions=True,
            v4_primary_timescale="15m",
            v4_entry_edge=0.10,
            v4_min_expected_move_bps=15.0,
        )

        result = await use_case.execute()

        # Should skip due to expected_move_below_fee_wall
        assert result is None

    @pytest.mark.asyncio
    async def test_v4_path_disabled_falls_back_to_v2(
        self, mock_exchange, mock_portfolio, mock_v4_port
    ):
        """Test that v4=False falls back to v2 path."""
        # Mock probability port to return None (no v2 signal available)
        prob_port = AsyncMock()
        prob_port.get_latest = AsyncMock(return_value=None)

        # Mock signal port
        sig_port = AsyncMock()
        sig_port.get_latest_signal = AsyncMock(return_value=None)

        use_case = OpenPositionUseCase(
            exchange=mock_exchange,
            portfolio=mock_portfolio,
            repository=AsyncMock(),
            alerts=AsyncMock(),
            probability_port=prob_port,
            signal_port=sig_port,
            v4_snapshot_port=mock_v4_port,
            engine_use_v4_actions=False,  # V4 disabled
            v4_primary_timescale="15m",
        )

        result = await use_case.execute()

        # Should use v2 path (returns None because v2 probability is None)
        assert result is None


# ─── Tests: Database Writes ───────────────────────────────────────────────


class TestV4DatabaseWrites:
    """Test that V4 fields are correctly written to database."""

    def test_additive_migrations_include_v4_columns(self):
        """Test that additive migrations include all V4 columns."""
        migrations = " ".join(ADDITIVE_MIGRATIONS_SQL)

        assert "v4_entry_regime" in migrations
        assert "v4_entry_macro_bias" in migrations
        assert "v4_entry_macro_confidence" in migrations
        assert "v4_entry_expected_move_bps" in migrations
        assert "v4_entry_composite_v3" in migrations
        assert "v4_entry_consensus_safe" in migrations
        assert "v4_entry_window_close_ts" in migrations
        assert "v4_snapshot_ts_at_entry" in migrations

    def test_position_v4_fields(self, mock_v4_snapshot: V4Snapshot):
        """Test that Position entity has all V4 fields."""
        position = Position(
            asset="BTC",
            side=TradeSide.LONG,
            leverage=3,
            entry_signal_score=0.72,
            entry_timescale="15m",
            venue="hyperliquid",
            strategy_version="v4-fusion",
            v4_entry_regime="TRENDING_UP",
            v4_entry_macro_bias="BULL",
            v4_entry_macro_confidence=75,
            v4_entry_expected_move_bps=55.0,
            v4_entry_composite_v3=0.35,
            v4_entry_consensus_safe=True,
            v4_entry_window_close_ts=1712940300,
            v4_snapshot_ts_at_entry=1712940000.0,
        )

        assert position.v4_entry_regime == "TRENDING_UP"
        assert position.v4_entry_macro_bias == "BULL"
        assert position.v4_entry_macro_confidence == 75
        assert position.v4_entry_expected_move_bps == 55.0
        assert position.v4_entry_composite_v3 == 0.35
        assert position.v4_entry_consensus_safe is True
        assert position.v4_entry_window_close_ts == 1712940300
        assert position.v4_snapshot_ts_at_entry == 1712940000.0


# ─── Tests: Default Values ────────────────────────────────────────────────


class TestV4DefaultValues:
    """Test default values when V4 data is missing."""

    def test_missing_probability_returns_not_tradeable(self):
        """Test that missing probability_up makes timescale not tradeable."""
        v4 = V4Snapshot.from_dict(
            {
                "asset": "BTC",
                "ts": 1712940000.0,
                "timescales": {
                    "15m": {
                        "timescale": "15m",
                        "status": "ok",
                        "window_ts": 1712939400,
                        "window_close_ts": 1712940300,
                        "seconds_to_close": 900,
                        # probability_up missing
                        "regime": "TRENDING_UP",
                    },
                },
            }
        )

        assert v4.timescales["15m"].probability_up is None
        assert v4.timescales["15m"].is_tradeable is False

    def test_missing_quantiles_defaults_to_empty(self):
        """Test that missing quantiles defaults to empty Quantiles."""
        v4 = V4Snapshot.from_dict(
            {
                "asset": "BTC",
                "ts": 1712940000.0,
                "timescales": {
                    "15m": {
                        "timescale": "15m",
                        "status": "ok",
                        "window_ts": 1712939400,
                        "window_close_ts": 1712940300,
                        "seconds_to_close": 900,
                        "probability_up": 0.72,
                        "regime": "TRENDING_UP",
                        # quantiles_at_close missing
                    },
                },
            }
        )

        q = v4.timescales["15m"].quantiles_at_close
        assert q.p10 is None
        assert q.p25 is None
        assert q.p50 is None
        assert q.p75 is None
        assert q.p90 is None

    def test_missing_cascade_defaults_to_empty(self):
        """Test that missing cascade defaults to empty Cascade."""
        v4 = V4Snapshot.from_dict(
            {
                "asset": "BTC",
                "ts": 1712940000.0,
                "timescales": {
                    "15m": {
                        "timescale": "15m",
                        "status": "ok",
                        "window_ts": 1712939400,
                        "window_close_ts": 1712940300,
                        "seconds_to_close": 900,
                        "probability_up": 0.72,
                        "regime": "TRENDING_UP",
                        # cascade missing
                    },
                },
            }
        )

        c = v4.timescales["15m"].cascade
        assert c.strength is None
        assert c.tau1 is None
        assert c.tau2 is None
        assert c.exhaustion_t is None
        assert c.signal is None

    def test_missing_macro_defaults_to_neutral(self):
        """Test that missing macro defaults to NEUTRAL/0."""
        v4 = V4Snapshot.from_dict(
            {
                "asset": "BTC",
                "ts": 1712940000.0,
                # macro missing
            }
        )

        assert v4.macro.bias == "NEUTRAL"
        assert v4.macro.confidence == 0
        assert v4.macro.direction_gate == "ALLOW_ALL"
        assert v4.macro.status == "ok"


# ─── Test Execution ───────────────────────────────────────────────────────


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
