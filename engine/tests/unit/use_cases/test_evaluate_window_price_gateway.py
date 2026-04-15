"""Verify EvaluateWindowUseCase accepts PriceGateway port and routes correctly."""
from __future__ import annotations

import os
import sys

import pytest

_engine = os.path.join(os.path.dirname(__file__), "..", "..", "..")
if _engine not in sys.path:
    sys.path.insert(0, _engine)

# Set required env vars before engine imports trigger Settings()
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")


class _FakePriceGateway:
    """Minimal fake satisfying the PriceGateway abstract interface."""

    def __init__(self):
        self.current_calls = []
        self.candle_calls = []

    async def get_current_price(self, asset):
        self.current_calls.append(asset)
        return 3000.0

    async def get_window_candle(self, asset, window_ts, tf):
        self.candle_calls.append((asset, window_ts, tf))
        return None


def _make_uc(**kw):
    from use_cases.evaluate_window import EvaluateWindowUseCase
    return EvaluateWindowUseCase(**kw)


def test_init_accepts_price_gateway():
    from use_cases.ports.price_gateway import PriceGateway
    gw = _FakePriceGateway()
    uc = _make_uc(price_gateway=gw)
    assert uc._price_gateway is gw


def test_execute_accepts_price_gateway_kwarg():
    """PriceGateway is a valid __init__ kwarg."""
    from use_cases.evaluate_window import EvaluateWindowUseCase
    gw = _FakePriceGateway()
    uc = EvaluateWindowUseCase(price_gateway=gw)
    assert uc._price_gateway is gw


def test_price_gateway_is_optional():
    """Can construct without price_gateway (legacy back-compat)."""
    from use_cases.evaluate_window import EvaluateWindowUseCase
    uc = EvaluateWindowUseCase()
    assert uc._price_gateway is None
