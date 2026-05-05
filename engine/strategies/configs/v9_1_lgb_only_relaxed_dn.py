"""Hook for v9_1_lgb_only_relaxed_dn — DOWN-floor relaxation soak variant.

Same Python evaluation as ``v9_1_lgb_only`` (delegates to
``evaluate_v9_1_lgb_only`` so the v9.1 booster + v9 ensemble gate stack are
identical). The ONLY behavioural difference is the YAML ``gate_params``:

  - ``lgb_dist_min_down`` lowered from production runtime override (0.25)
    to 0.15 — opens the 0.15-0.25 DOWN dist sub-buckets that historical
    data (3d, n=57) shows ~85-86% WR with Wilson 95% lower ≈ 67-74% (vs.
    breakeven ~32-34% at avg fill 0.32) — see Hub note #342.
  - ``lgb_dist_min_up`` kept at production strict floor (0.20).

Mode: GHOST. Soaks 24-48h to verify the historical Wilson edge holds under
current regime and current production gates. Promote to LIVE at half-stake
(fraction=0.125 = half of v9_1_lgb_only LIVE 0.25) only after manual review
per CLAUDE.md "yes do it" rule.

Why a wrapper hook rather than reusing ``evaluate_v9_1_lgb_only`` directly:

The v9_1_lgb_only hook hard-codes ``strategy_id="v9_1_lgb_only"`` into the
returned StrategyDecision (engine/strategies/configs/v9_1_lgb_only.py:33).
The registry already stamps the *correct* strategy_id on the persisted
decision row (registry.py uses the YAML name as the dict key), so DB rows
are fine. But anywhere downstream that reads ``decision.strategy_id``
directly (TG cards, log lines, alerts) would see the wrong id.

This wrapper relabels the value-object identity to match the YAML name,
matching the v9_1_lgb_only → v9_ensemble pattern.

Hub note #342 — full conviction analysis + Wilson math + half-stake design.
docs/v9_1_PROVENANCE.md — v9.1 lineage.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies.configs.v9_1_lgb_only import evaluate_v9_1_lgb_only as _evaluate_v9_1

_STRATEGY_ID = "v9_1_lgb_only_relaxed_dn"
_VERSION = "9.1.0-relaxed-dn"


def evaluate_v9_1_lgb_only_relaxed_dn(
    surface: "FullDataSurface",
) -> StrategyDecision:
    """Relabel-only wrapper around evaluate_v9_1_lgb_only.

    Gate stack, model source, ensemble logic are all identical. The only
    difference is the YAML's ``lgb_dist_min_down`` (0.25 → 0.15), which is
    applied via the contextvar gate_params lookup inside the underlying
    v9_ensemble → v8_champion_lgb_only chain (see gate_params.py).
    """
    decision = _evaluate_v9_1(surface)

    # Relabel identity. Keep all other fields (action, direction, gates,
    # metadata, etc.) exactly as v9_1_lgb_only computed them so downstream
    # observability is identical to v9_1_lgb_only with one strategy_id swap.
    return StrategyDecision(
        action=decision.action,
        direction=decision.direction,
        confidence=decision.confidence,
        confidence_score=decision.confidence_score,
        entry_cap=decision.entry_cap,
        collateral_pct=decision.collateral_pct,
        strategy_id=_STRATEGY_ID,
        strategy_version=_VERSION,
        entry_reason=(
            decision.entry_reason.replace("v9_1_lgb_only", _STRATEGY_ID)
            if decision.entry_reason
            else ""
        ),
        skip_reason=decision.skip_reason,
        metadata=dict(decision.metadata or {}),
    )
