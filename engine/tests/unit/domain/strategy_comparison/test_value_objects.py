"""Tests for domain.strategy_comparison.value_objects."""
from __future__ import annotations

import pytest

from domain.strategy_comparison.value_objects import (
    DirectionFilter,
    RegimeFilter,
    TBand,
    WindowPeriod,
    t_band_from_offset,
)


class TestWindowPeriod:
    def test_values(self):
        assert WindowPeriod.H1.value == "1h"
        assert WindowPeriod.H15.value == "15h"
        assert WindowPeriod.H24.value == "24h"
        assert WindowPeriod.D7.value == "7d"
        assert WindowPeriod.D30.value == "30d"

    def test_str_enum_coercion(self):
        assert WindowPeriod("24h") is WindowPeriod.H24


class TestTBand:
    def test_all_values(self):
        assert TBand.ALL.value == "all"
        assert TBand.T_24_30.value == "T-24-30"
        assert TBand.T_181_240.value == "T-181-240"


class TestDirectionFilter:
    def test_values(self):
        assert DirectionFilter.ALL.value == "all"
        assert DirectionFilter.UP.value == "UP"
        assert DirectionFilter.DOWN.value == "DOWN"


class TestRegimeFilter:
    def test_values(self):
        assert RegimeFilter.ALL.value == "all"
        assert RegimeFilter.VOLATILE_TREND.value == "volatile_trend"
        assert RegimeFilter.RISK_OFF.value == "risk_off"


class TestTBandFromOffset:
    def test_t24_30_midpoint(self):
        assert t_band_from_offset(27) is TBand.T_24_30

    def test_t24_30_lower_edge(self):
        assert t_band_from_offset(24) is TBand.T_24_30

    def test_t24_30_upper_edge(self):
        assert t_band_from_offset(30) is TBand.T_24_30

    def test_t31_60(self):
        assert t_band_from_offset(45) is TBand.T_31_60

    def test_t31_60_edge(self):
        assert t_band_from_offset(31) is TBand.T_31_60
        assert t_band_from_offset(60) is TBand.T_31_60

    def test_t61_90(self):
        assert t_band_from_offset(75) is TBand.T_61_90

    def test_t91_120(self):
        assert t_band_from_offset(100) is TBand.T_91_120

    def test_t121_180(self):
        assert t_band_from_offset(150) is TBand.T_121_180

    def test_t181_240(self):
        assert t_band_from_offset(200) is TBand.T_181_240

    def test_t181_240_edges(self):
        assert t_band_from_offset(181) is TBand.T_181_240
        assert t_band_from_offset(240) is TBand.T_181_240

    def test_below_range_returns_all(self):
        assert t_band_from_offset(10) is TBand.ALL
        assert t_band_from_offset(0) is TBand.ALL

    def test_above_range_returns_all(self):
        assert t_band_from_offset(300) is TBand.ALL
        assert t_band_from_offset(241) is TBand.ALL

    def test_between_bands_returns_all(self):
        # 23 is below the lowest named band
        assert t_band_from_offset(23) is TBand.ALL
