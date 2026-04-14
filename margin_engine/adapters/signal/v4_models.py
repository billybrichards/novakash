"""
v4 snapshot models adapter layer - re-exports from domain for backward compatibility.

These types are now defined in domain/value_objects/v4_data.py to maintain
clean architecture - the domain layer should not depend on adapter layer.

This module re-exports them for backward compatibility with existing imports.
New code should import directly from domain/value_objects/v4_data.py.
"""

from margin_engine.domain.value_objects import (
    Cascade,
    Consensus,
    MacroBias,
    Quantiles,
    TimescalePayload,
    V4Snapshot,
)

__all__ = [
    "Cascade",
    "Consensus",
    "MacroBias",
    "Quantiles",
    "TimescalePayload",
    "V4Snapshot",
]
