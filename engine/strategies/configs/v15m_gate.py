"""Custom hooks for v10_gate strategy.

Ports the V10 confidence classification and DUNE-based evaluation
from the existing V10GateStrategy adapter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface
    from strategies.registry import SizingResult


def classify_confidence(
    surface: "FullDataSurface",
    sizing: "SizingResult",
) -> "SizingResult":
    """Classify confidence from V2 probability (DUNE).

    HIGH if max(p, 1-p) > 0.75, else MODERATE.
    Adjusts the sizing label accordingly.
    """
    from strategies.registry import SizingResult

    p = surface.v2_probability_up
    if p is not None and max(p, 1 - p) > 0.75:
        label = "HIGH"
    else:
        label = "MODERATE"

    return SizingResult(
        fraction=sizing.fraction,
        max_collateral_pct=sizing.max_collateral_pct,
        entry_cap=sizing.entry_cap,
        size_modifier=sizing.size_modifier,
        label=f"v15m_DUNE_{label}",
    )
