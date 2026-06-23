"""Unit tests for dense multi-asset signal collection value objects."""
from __future__ import annotations

import pytest

from engine.domain.value_objects import Asset


class TestAsset:
    def test_accepts_btc(self):
        a = Asset("BTC")
        assert a.symbol == "BTC"

    def test_accepts_eth_sol_xrp(self):
        for s in ("ETH", "SOL", "XRP"):
            assert Asset(s).symbol == s

    def test_normalizes_lowercase(self):
        assert Asset("btc").symbol == "BTC"

    def test_strips_whitespace(self):
        assert Asset(" eth ").symbol == "ETH"

    def test_rejects_unsupported(self):
        with pytest.raises(ValueError, match="unsupported asset"):
            Asset("FOO")

    def test_frozen(self):
        a = Asset("BTC")
        with pytest.raises((AttributeError, Exception)):
            a.symbol = "ETH"  # type: ignore[misc]

    def test_equality_by_value(self):
        assert Asset("BTC") == Asset("btc")


from engine.domain.value_objects import Timeframe


class TestTimeframe:
    def test_5m(self):
        tf = Timeframe(300)
        assert tf.duration_secs == 300
        assert tf.label == "5m"

    def test_15m(self):
        tf = Timeframe(900)
        assert tf.duration_secs == 900
        assert tf.label == "15m"

    def test_rejects_unsupported(self):
        with pytest.raises(ValueError, match="unsupported timeframe"):
            Timeframe(600)

    def test_frozen(self):
        tf = Timeframe(300)
        with pytest.raises((AttributeError, Exception)):
            tf.duration_secs = 900  # type: ignore[misc]


from engine.domain.value_objects import EvalOffset


class TestEvalOffset:
    def test_accepts_valid_range(self):
        for s in (2, 60, 240, 298, 600, 898):
            assert EvalOffset(s).seconds_before_close == s

    def test_rejects_below_2(self):
        with pytest.raises(ValueError, match="out of range"):
            EvalOffset(1)

    def test_rejects_above_898(self):
        with pytest.raises(ValueError, match="out of range"):
            EvalOffset(899)

    def test_rejects_zero_and_negative(self):
        with pytest.raises(ValueError):
            EvalOffset(0)
        with pytest.raises(ValueError):
            EvalOffset(-5)


from engine.domain.value_objects import PriceCandle


class TestPriceCandle:
    def test_holds_open_close_source(self):
        c = PriceCandle(open_price=50000.0, close_price=50100.0, source="tiingo_rest")
        assert c.open_price == 50000.0
        assert c.close_price == 50100.0
        assert c.source == "tiingo_rest"

    def test_frozen(self):
        c = PriceCandle(1.0, 2.0, "tiingo_rest")
        with pytest.raises((AttributeError, Exception)):
            c.open_price = 99.0  # type: ignore[misc]

    def test_delta_pct_helper(self):
        c = PriceCandle(open_price=100.0, close_price=101.0, source="tiingo_rest")
        assert c.delta_pct() == pytest.approx(1.0)

    def test_delta_pct_zero_open_returns_zero(self):
        c = PriceCandle(open_price=0.0, close_price=5.0, source="tiingo_rest")
        assert c.delta_pct() == 0.0
