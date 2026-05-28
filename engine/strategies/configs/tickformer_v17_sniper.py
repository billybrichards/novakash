"""tickformer_v17_sniper — TickFormer v17 precision-sniper SHADOW strategy.

Thin wrapper around the shared TickFormer evaluation core
(:mod:`strategies.configs._tickformer_base`). Reads
``probability_tickformer_v17`` (NUMERIC) emitted by timesfm-service
(sister PR on the magic-model repo). v17 is the precision-tuned
successor to v16 — same hybrid transformer + multi-step autoregressive
head but with the loss reweighted toward late-window precision.

Defaults: sniper tier — ``up_threshold=0.85``,
``eval_offset_remaining_min=60``, ``eval_offset_remaining_max=140``.
v17 sustains 94-98% WR across this band in the val sweep; widening to
220 admits the Tier-D dream zone at the cost of ~6pp WR.

SHADOW kill switch via ``gate_params.shadow_only=1`` as in v16.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies.configs._tickformer_base import evaluate_tickformer_strategy

_STRATEGY_ID = "tickformer_v17_sniper"
_VERSION = "1.0.0"


def evaluate_tickformer_v17_sniper(
    surface: "FullDataSurface",
) -> StrategyDecision:
    """Evaluate the v17 sniper SHADOW strategy.

    See :func:`strategies.configs._tickformer_base.evaluate_tickformer_strategy`
    for the full gating logic.
    """
    return evaluate_tickformer_strategy(
        surface,
        prob_column="probability_tickformer_v17",
        strategy_id=_STRATEGY_ID,
        version=_VERSION,
        default_up_threshold=0.85,
        default_rem_min=60,
        default_rem_max=140,
        entry_reason_label="tickformer_v17_sniper_pass",
        not_available_reason="tickformer_v17_not_available",
    )
