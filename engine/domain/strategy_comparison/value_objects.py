"""Value objects for strategy-comparison rollups.

Frozen, hashable, comparable. No behaviour beyond construction validation.
See docs/architecture/2026-05-01-strategy-comparison-system.md.
"""

from __future__ import annotations

from enum import Enum


class WindowPeriod(str, Enum):
    """Lookback window for a rollup row."""

    H1 = "1h"
    H15 = "15h"
    H24 = "24h"
    D7 = "7d"
    D30 = "30d"


class TBand(str, Enum):
    """T-minus bucket (seconds-to-window-close at fire time)."""

    ALL = "all"
    T_24_30 = "T-24-30"
    T_31_60 = "T-31-60"
    T_61_90 = "T-61-90"
    T_91_120 = "T-91-120"
    T_121_180 = "T-121-180"
    T_181_240 = "T-181-240"


class DirectionFilter(str, Enum):
    """UP/DOWN slice or aggregate."""

    ALL = "all"
    UP = "UP"
    DOWN = "DOWN"


class RegimeFilter(str, Enum):
    """Macro regime slice or aggregate."""

    ALL = "all"
    VOLATILE_TREND = "volatile_trend"
    CHOP = "chop"
    CALM_TREND = "calm_trend"
    RISK_OFF = "risk_off"


def t_band_from_offset(offset_seconds: int) -> TBand:
    """Bucket a window offset (seconds-from-window-open) into a TBand.

    NOT IMPLEMENTED — this is a design skeleton.
    """
    raise NotImplementedError("design skeleton — see docs/architecture/")
