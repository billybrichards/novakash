"""Domain-layer test builders.

One factory per entity/value object. Kwargs override defaults. Builders
must NOT import adapters or infrastructure — they only produce domain
objects.
"""
from __future__ import annotations

import time
from typing import Any


def make_window_state(**overrides: Any) -> dict:
    """Build a minimal WindowState payload for tests.

    Returns a dict (not the dataclass) to stay framework-agnostic — callers
    wrap in the actual domain dataclass if needed. This mirrors the
    clean-arch guide's Result/DTO pattern.
    """
    defaults = {
        "asset": "BTC",
        "window_ts": int(time.time()),
        "eval_offset": 120,
        "direction": "UP",
        "confidence": 0.75,
    }
    defaults.update(overrides)
    return defaults


def make_trade(**overrides: Any) -> dict:
    """Build a minimal Trade payload for tests."""
    defaults = {
        "trade_id": "test-trade-1",
        "strategy_id": "v4_fusion",
        "asset": "BTC",
        "direction": "UP",
        "size_usd": 10.0,
        "entry_price": 0.50,
        "mode": "PAPER",
        "status": "OPEN",
        "opened_ts": int(time.time()),
    }
    defaults.update(overrides)
    return defaults


def make_strategy_decision(**overrides: Any) -> dict:
    """Build a minimal StrategyDecision payload."""
    defaults = {
        "strategy_id": "test",
        "version": "1.0",
        "action": "TRADE",
        "direction": "UP",
        "mode": "LIVE",
        "size_usd": 10.0,
        "reason": "test",
    }
    defaults.update(overrides)
    return defaults
