"""tickformer_v20_adaptive_early_xrp — V4 ETH/XRP TickFormer SHADOW (GHOST soak) strategy.

Thin wrapper that points the shared TickFormer evaluation core at
``probability_tickformer_v20_xrp`` (NUMERIC) emitted by the
classifier-gpu sister PR. Asset gate (XRP) is enforced via YAML
``asset: XRP`` upstream; this wrapper is asset-agnostic.

Defaults from V4 sweep top pocket (RDS hub note #823):
  up_threshold=0.9
  eval_offset_remaining 120-280s

Per feedback_no_auto_promote: shadow_only=1 default. Billy flips by hand.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies.configs._tickformer_base import evaluate_tickformer_strategy

_STRATEGY_ID = "tickformer_v20_adaptive_early_xrp"
_VERSION = "1.0.0"


def evaluate_tickformer_v20_adaptive_early_xrp(
    surface: "FullDataSurface",
) -> StrategyDecision:
    """Evaluate the v20 adaptive early-entry SHADOW strategy.

    See :func:`strategies.configs._tickformer_base.evaluate_tickformer_strategy`
    for the full gating logic.
    """
    return evaluate_tickformer_strategy(
        surface,
        prob_column="probability_tickformer_v20_xrp",
        strategy_id=_STRATEGY_ID,
        version=_VERSION,
        default_up_threshold=0.9,
        default_rem_min=120,
        default_rem_max=280,
        entry_reason_label="tickformer_v20_adaptive_early_xrp_pass",
        not_available_reason="tickformer_v20_xrp_not_available",
    )
