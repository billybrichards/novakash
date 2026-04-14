"""
Entry strategies package.

Provides strategy pattern implementations for different entry approaches:
- V4Strategy: 10-gate decision stack with ML-derived signals
- V2Strategy: Legacy probability-based entry
"""

from .base import EntryStrategy
from .v4_strategy import V4Strategy
from .v2_strategy import V2Strategy

__all__ = ["EntryStrategy", "V4Strategy", "V2Strategy"]
