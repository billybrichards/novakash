"""Custom hooks for v4_down_only strategy.

CLOB-based sizing schedule from 897K-sample analysis (2026-04-12):
  clob_down_ask >= 0.55: 2.0x (market + model agree, 97%+ WR)
  clob_down_ask 0.35-0.55: 1.2x (mild agreement, 88-93% WR)
  clob_down_ask 0.25-0.35: 1.0x (contrarian, 87% WR)
  clob_down_ask < 0.25: skip (53%/31% WR, not tradeable)
  clob_down_ask is None: 1.5x (99% WR -- strong moves lack CLOB data)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface
    from strategies.registry import SizingResult


# CLOB sizing schedule -- data-driven from 620K sample audit (2026-04-12).
_CLOB_SIZING: list[tuple[float, float, str]] = [
    (0.55, 2.0, "strong_97pct"),
    (0.35, 1.2, "mild_88pct"),
    (0.25, 1.0, "contrarian_87pct"),
    (0.0, 0.0, "skip_sub25_53pct"),
]

_NULL_CLOB_SIZE_MOD = 1.5
_MAX_COLLATERAL_PCT = 0.10


def clob_sizing(surface: "FullDataSurface", sizing: "SizingResult") -> "SizingResult":
    """Apply CLOB-based sizing to the position.

    Reads clob_down_ask from the data surface and adjusts
    the size modifier accordingly.
    """
    from strategies.registry import SizingResult

    clob_ask = surface.clob_down_ask
    modifier = _NULL_CLOB_SIZE_MOD
    label = "no_clob_99pct"

    if clob_ask is not None:
        modifier = 0.0
        label = "skip_sub25_53pct"
        for threshold, mod, lbl in _CLOB_SIZING:
            if clob_ask >= threshold:
                modifier = mod
                label = lbl
                break

    return SizingResult(
        fraction=sizing.fraction,
        max_collateral_pct=min(sizing.max_collateral_pct, _MAX_COLLATERAL_PCT),
        entry_cap=sizing.entry_cap,
        size_modifier=modifier,
        label=f"down_{label}",
    )
