"""tickformer_v16_pure — TickFormer v16 "magic model" SHADOW strategy.

Thin wrapper around the shared TickFormer evaluation core
(:mod:`strategies.configs._tickformer_base`). Reads
``probability_tickformer_v16`` (NUMERIC) and the shared
``tickformer_trade_signal`` (UP/DOWN/HOLD) fields emitted by
timesfm-service (sister PR on the magic-model repo).

Defaults: Tier-C — ``up_threshold=0.85``,
``eval_offset_remaining_min=60``, ``eval_offset_remaining_max=240``
(admits the Tier-D dream zone by default). Operator narrows the band
or raises the threshold via YAML / runtime override.

SHADOW kill switch — when ``gate_params.shadow_only=1`` (default) the
strategy returns SKIP-with-decision-record even if YAML mode flips to
LIVE. See ``feedback_no_auto_promote.md`` (post-2026-04-17 rule).

Sibling timesfm PR: magic-model branch.
Engine precedent: v9_5_xrp_up_solo.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies.configs._tickformer_base import evaluate_tickformer_strategy

_STRATEGY_ID = "tickformer_v16_pure"
_VERSION = "1.0.0"


def evaluate_tickformer_v16_pure(
    surface: "FullDataSurface",
) -> StrategyDecision:
    """Evaluate the v16 magic-model SHADOW strategy.

    See :func:`strategies.configs._tickformer_base.evaluate_tickformer_strategy`
    for the full gating logic.
    """
    return evaluate_tickformer_strategy(
        surface,
        prob_column="probability_tickformer_v16",
        strategy_id=_STRATEGY_ID,
        version=_VERSION,
        default_up_threshold=0.85,
        default_rem_min=60,
        default_rem_max=240,
        entry_reason_label="tickformer_v16_pure_pass",
        not_available_reason="tickformer_v16_not_available",
    )
