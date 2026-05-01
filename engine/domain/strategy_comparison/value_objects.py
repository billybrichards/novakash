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
    """Bucket seconds-to-window-close into a TBand.

    offset_seconds is the T-minus value: seconds remaining before window close.
    """
    s = offset_seconds
    if 24 <= s <= 30:
        return TBand.T_24_30
    if 31 <= s <= 60:
        return TBand.T_31_60
    if 61 <= s <= 90:
        return TBand.T_61_90
    if 91 <= s <= 120:
        return TBand.T_91_120
    if 121 <= s <= 180:
        return TBand.T_121_180
    if 181 <= s <= 240:
        return TBand.T_181_240
    return TBand.ALL
