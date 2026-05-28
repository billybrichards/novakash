"""tickformer_v20_adaptive_early — TickFormer v20 adaptive-K-aware SHADOW.

Thin wrapper around the shared TickFormer evaluation core
(:mod:`strategies.configs._tickformer_base`). Reads
``probability_tickformer_v20`` (NUMERIC) emitted by the timesfm sister
PR (not yet opened — v20 is currently a research checkpoint). v20 is
the adaptive-K-aware retrain with a custom loss rewarding early high-
conviction firing; its defining property is fires at ~t-217s
(~22-23s into the 5min window) at 91% WR — earliest entry of any
tickformer variant.

Defaults: adaptive-early — ``up_threshold=0.90`` (recommended ceiling —
DO NOT raise above this; WR saturates per the backtest),
``eval_offset_remaining_min=60``, ``eval_offset_remaining_max=240``
(~91% WR / ~16 trades/day combined UP+DN at the recommended tier).

SHADOW kill switch via ``gate_params.shadow_only=1`` as in v16/v17/v18.
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
    """Evaluate the v20 adaptive-early SHADOW strategy.

    See :func:`strategies.configs._tickformer_base.evaluate_tickformer_strategy`
    for the full gating logic.
    """
    return evaluate_tickformer_strategy(
        surface,
        prob_column="probability_tickformer_v20",
        strategy_id=_STRATEGY_ID,
        version=_VERSION,
        default_up_threshold=0.90,
        default_rem_min=60,
        default_rem_max=240,
        entry_reason_label="tickformer_v20_adaptive_early_pass",
        not_available_reason="tickformer_v20_not_available",
    )
