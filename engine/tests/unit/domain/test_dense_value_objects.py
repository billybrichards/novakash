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
