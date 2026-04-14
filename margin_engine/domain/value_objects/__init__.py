"""
Domain value objects package.

Exports all value objects including v4 data types.
Re-exports from value_objects_legacy.py (original file) and v4_data.py (new v4 types).
"""

# Re-export original value objects from value_objects_legacy.py using relative import
from .value_objects_legacy import (
    CompositeSignal,
    Money,
    Price,
    ProbabilitySignal,
    StopLevel,
    TradeSide,
    ExitReason,
    PositionState,
)

# Re-export v4 data types from v4_data.py
from .v4_data import (
    Cascade,
    Consensus,
    MacroBias,
    Quantiles,
    TimescalePayload,
    V4Snapshot,
    _parse_macro,
)

__all__ = [
    # Original value objects
    "TradeSide",
    "ExitReason",
    "PositionState",
    "Money",
    "Price",
    "CompositeSignal",
    "StopLevel",
    "ProbabilitySignal",
    # v4 data types
    "Cascade",
    "Consensus",
    "MacroBias",
    "Quantiles",
    "TimescalePayload",
    "V4Snapshot",
    "_parse_macro",
]
