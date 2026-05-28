"""tickformer_v20_adaptive_early — TickFormer v20 adaptive early-entry SHADOW strategy.

Thin wrapper around the shared TickFormer evaluation core
(:mod:`strategies.configs._tickformer_base`). Reads
``probability_tickformer_v20`` (NUMERIC) emitted by timesfm-service.

v20 is the adaptive early-entry successor — trained with a multi-step
autoregressive head biased toward early-window precision (t-180+) while
maintaining v18's broad-band stability property. Also used by the exit
monitor (``tickformer_prob_reader`` v20 path) as the exit signal source.

Defaults: early-entry band — ``up_threshold=0.90``,
``eval_offset_remaining_min=120``, ``eval_offset_remaining_max=280``
(admit the full early-entry zone).

SHADOW kill switch via ``gate_params.shadow_only=1``.
(fix/tickformer-strategies-actually-fire — v20 scaffold)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies.configs._tickformer_base import evaluate_tickformer_strategy

_STRATEGY_ID = "tickformer_v20_adaptive_early"
_VERSION = "1.0.0"


def evaluate_tickformer_v20_adaptive_early(
    surface: "FullDataSurface",
) -> StrategyDecision:
    """Evaluate the v20 adaptive early-entry SHADOW strategy.

    See :func:`strategies.configs._tickformer_base.evaluate_tickformer_strategy`
    for the full gating logic.
    """
    return evaluate_tickformer_strategy(
        surface,
        prob_column="probability_tickformer_v20",
        strategy_id=_STRATEGY_ID,
        version=_VERSION,
        default_up_threshold=0.90,
        default_rem_min=120,
        default_rem_max=280,
        entry_reason_label="tickformer_v20_adaptive_early_pass",
        not_available_reason="tickformer_v20_not_available",
    )
