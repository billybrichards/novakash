"""tickformer_v18_t180 — TickFormer v18 balanced t-180 SHADOW strategy.

Thin wrapper around the shared TickFormer evaluation core
(:mod:`strategies.configs._tickformer_base`). Reads
``probability_tickformer_v18`` (NUMERIC) emitted by timesfm-service
(sister PR on the magic-model repo). v18 is v17's broad-band successor
— same architecture, trained across the full t-60..t-220 window with a
uniform precision loss; WR holds 85%+ across the entire band at
thr 0.90.

Defaults: balanced broad-band — ``up_threshold=0.90``,
``eval_offset_remaining_min=60``, ``eval_offset_remaining_max=220``
(~92% WR / ~16 trades/day combined UP+DN).

SHADOW kill switch via ``gate_params.shadow_only=1`` as in v16.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies.configs._tickformer_base import evaluate_tickformer_strategy

_STRATEGY_ID = "tickformer_v18_t180"
_VERSION = "1.0.0"


def evaluate_tickformer_v18_t180(
    surface: "FullDataSurface",
) -> StrategyDecision:
    """Evaluate the v18 balanced t-180 SHADOW strategy.

    See :func:`strategies.configs._tickformer_base.evaluate_tickformer_strategy`
    for the full gating logic.
    """
    return evaluate_tickformer_strategy(
        surface,
        prob_column="probability_tickformer_v18",
        strategy_id=_STRATEGY_ID,
        version=_VERSION,
        default_up_threshold=0.90,
        default_rem_min=60,
        default_rem_max=220,
        entry_reason_label="tickformer_v18_t180_pass",
        not_available_reason="tickformer_v18_not_available",
    )
