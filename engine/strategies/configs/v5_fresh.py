"""Custom hooks for v5_fresh — relaxed-gate sibling of v5_ensemble.

Zero new logic. v5_fresh reuses ``v5_ensemble.evaluate_polymarket_ensemble``
under a different strategy_id / version, with different ``gate_params``
supplied by ``v5_fresh.yaml``. The gate_params contextvar layer
(``strategies.gate_params``, PR #267) routes each strategy's YAML bag
into the shared hook at evaluation time, so the same function sees
strict values under v5_ensemble and relaxed values under v5_fresh
without any module-global conflict.

Why a separate strategy exists:
- v5_ensemble runs the hardened Tier-0 gates (audit #223/#228/#234).
- v5_fresh runs Tier A+B relaxations: health_gate=unsafe (allows
  DEGRADED through), risk_off_override_dist_min=0.15 (more override
  trades), ensemble_disagreement_threshold=0.20 (adds a model-sanity
  guard the strict config didn't need).
- Both run LIVE. Execute-trade dedup is window-level
  (WindowKey(asset, window_ts, timeframe)) so only one fills per
  window if they agree — v5_fresh's extra trades come from windows
  v5_ensemble declines, not double-stakes. Double-stake is a separate
  opt-in (hub audit #246).

Rewrite pattern: the inner hook returns a StrategyDecision stamped
with v5_ensemble's id/version — we replace those two fields, preserving
everything else (metadata, skip reason, entry_cap, sizing, gate_results).
"""

from __future__ import annotations

import importlib.util
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision


_STRATEGY_ID = "v5_fresh"
_VERSION = "5.3.0"


def _load_v5_ensemble_module():
    """Load v5_ensemble.py via importlib so the shared hook is reachable
    without a package-level circular import.

    Mirrors the pattern in ``v4_fusion_v5_9.py`` — the hooks directory
    is imported file-by-file by StrategyRegistry, so v5_fresh cannot
    simply ``from v5_ensemble import ...``. spec_from_file_location
    gets us the module object directly.
    """
    base_path = Path(__file__).with_name("v5_ensemble.py")
    spec = importlib.util.spec_from_file_location(
        "strategy_hooks.v5_ensemble_base", base_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_base = _load_v5_ensemble_module()


def _rewrite(decision: StrategyDecision) -> StrategyDecision:
    return replace(
        decision,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
    )


def evaluate_polymarket_ensemble(
    surface: "FullDataSurface",
) -> Optional[StrategyDecision]:
    decision = _base.evaluate_polymarket_ensemble(surface)
    if decision is None:
        return None
    return _rewrite(decision)
