"""v7.1 regime-aware pre-gate hook. GHOST ONLY.

Replicates the v7.1 legacy decision logic that hub/api/v58_monitor.py
computes retroactively via _calc_v71_retroactive_decision:

    * VPIN gate: skip if vpin < 0.45 (TIMESFM_ONLY regime)
    * Delta gate: skip if |delta_pct| < 0.02%  (NORMAL/TRANSITION)
                  or         |delta_pct| < 0.01% (CASCADE: vpin >= 0.65)
    * Direction is taken from surface.signal_direction (engine's v5.7c
      baseline). If missing, we fall through to SKIP.

72h backtest (2026-04-16 → 2026-04-19): 520 eligible / 862 windows,
dir_wr=56.2%, poly_pnl_sim=+$212.01 on $10 stake. See hub note
"Extended config audit — 72h, all configs + YAML drafts".

NEVER promote to LIVE without ≥2 weeks of shadow data + Billy sign-off.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision


_STRATEGY_ID = "v7_1_regime"
_VERSION = "0.1.0-shadow"

# v7.1 thresholds — mirror hub/api/v58_monitor.py::_calc_v71_retroactive_decision
_VPIN_GATE = 0.45
_MIN_DELTA_NORMAL = 0.0002    # 0.02% (delta_pct stored as fraction)
_MIN_DELTA_CASCADE = 0.0001   # 0.01%
_CASCADE_THRESHOLD = 0.65
_INFORMED_THRESHOLD = 0.55

_BASE_CONFIDENCE_SCORE = 0.55
_DEFAULT_ENTRY_CAP = 0.65


def _skip(reason: str) -> StrategyDecision:
    return StrategyDecision(
        action="SKIP",
        direction=None,
        confidence=None,
        confidence_score=None,
        entry_cap=None,
        collateral_pct=None,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason="",
        skip_reason=reason,
        metadata={"hook": "v7_1_regime"},
    )


def evaluate_v71(surface: "FullDataSurface") -> Optional[StrategyDecision]:
    """Pre-gate hook. Returns TRADE/SKIP. Post-filter gates still run."""
    vpin = surface.vpin
    delta_pct = surface.delta_pct
    # Engine's v5.7c baseline direction. signal_direction is the canonical
    # field; fall back to surface attrs that exist in the current codebase.
    direction = (
        getattr(surface, "signal_direction", None)
        or getattr(surface, "direction", None)
    )

    if direction not in ("UP", "DOWN"):
        return _skip(f"v7_1: no baseline direction ({direction!r})")

    if vpin is None:
        return _skip("v7_1: vpin unavailable")
    if vpin < _VPIN_GATE:
        return _skip(f"v7_1: vpin {vpin:.3f} < {_VPIN_GATE}")

    if delta_pct is None:
        return _skip("v7_1: delta_pct unavailable")

    abs_delta = abs(delta_pct)
    if vpin >= _CASCADE_THRESHOLD:
        regime = "CASCADE"
        min_delta = _MIN_DELTA_CASCADE
    elif vpin >= _INFORMED_THRESHOLD:
        regime = "TRANSITION"
        min_delta = _MIN_DELTA_NORMAL
    else:
        regime = "NORMAL"
        min_delta = _MIN_DELTA_NORMAL

    if abs_delta < min_delta:
        return _skip(
            f"v7_1: |delta|={abs_delta:.4f} < {regime} min {min_delta:.4f}"
        )

    # Passed both gates — return TRADE. Registry then runs post-filter gates
    # (timing, trade_advised) before it finalises the decision.
    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence="MEDIUM",
        confidence_score=_BASE_CONFIDENCE_SCORE,
        entry_cap=_DEFAULT_ENTRY_CAP,
        collateral_pct=0.05,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=f"v7_1 {regime}: vpin={vpin:.3f}, |delta|={abs_delta:.4f}",
        skip_reason=None,
        metadata={
            "hook": "v7_1_regime",
            "vpin": vpin,
            "delta_pct": delta_pct,
            "regime": regime,
            "min_delta": min_delta,
        },
    )
