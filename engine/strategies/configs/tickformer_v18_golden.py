"""tickformer_v18_golden — TickFormer v18 mid-conviction early-window SHADOW strategy.

Thin wrapper around the shared TickFormer evaluation core
(:mod:`strategies.configs._tickformer_base`). Reads
``probability_tickformer_v18`` (NUMERIC) emitted by timesfm-service.

NEW strategy added by fix/tickformer-threshold-recal-2026-05-28 (2026-05-28).
Targets the v18 mid-conviction early-window live pocket discovered in the
7-day BTC 5m sweep (189 resolved windows, hub notes #728/#729):

  prob 0.80-0.85 × eval_offset_remaining 120-179s → n=13, WR 100%

This is a DISTINCT pocket from tickformer_v18_t180 (which now targets the
sniper pocket: 0.86+ × 60-119s). v18_golden covers the lower-conviction
early-window band that v18_t180 does not reach after recalibration.

Operating point:
  up_threshold           : 0.80
  eval_offset_remaining  : [120, 179] seconds (early window before open)
  Reads: probability_tickformer_v18 (NOT v20 — this is the v18 mid-pocket)

Mode starts SHADOW. Operator promotes to GHOST when soak data confirms
the live pocket holds. See feedback_no_auto_promote.md.

FEATURE FLAG:
  Like the other recalibrated strategies, this strategy's *Python*-side
  defaults are guarded by ``TICKFORMER_RECAL_2026_05_28=true``.
  When the env flag is false (default), the strategy uses its
  native defaults and fires on any cross of up_threshold=0.80 in the
  configured band — the flag only matters for the env-flag zero-change
  guarantee on the other four strategies. v18_golden is a net-new
  strategy so there is no pre-recal state to preserve; it simply does
  not run at all until the engine loads the new YAML (which happens
  when this PR is deployed). The env flag check is included for
  consistency and operator clarity.

SHADOW kill switch via ``gate_params.shadow_only=1`` (default).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies.configs._tickformer_base import evaluate_tickformer_strategy

_STRATEGY_ID = "tickformer_v18_golden"
_VERSION = "1.0.0"

# ── Operating-point constants (live-calibrated, hub notes #728/#729) ────
# Live pocket: prob 0.80-0.85 × eval_offset_remaining 120-179s → WR 100% (n=13).
_UP_THRESHOLD = 0.80
_REM_MIN = 120
_REM_MAX = 179


def _recal_active() -> bool:
    return os.environ.get(
        "TICKFORMER_RECAL_2026_05_28", "false"
    ).strip().lower() in ("1", "true", "yes", "on")


def evaluate_tickformer_v18_golden(
    surface: "FullDataSurface",
) -> StrategyDecision:
    """Evaluate the v18 golden mid-conviction early-window SHADOW strategy.

    Targets the live pocket: prob 0.80-0.85 × eval_offset_remaining
    120-179s (100% WR, n=13, hub note #728). Complements v18_t180 which
    now covers the sniper pocket (0.86+ × 60-119s) after RECAL 2026-05-28.

    See :func:`strategies.configs._tickformer_base.evaluate_tickformer_strategy`
    for the full gating logic.
    """
    return evaluate_tickformer_strategy(
        surface,
        prob_column="probability_tickformer_v18",
        strategy_id=_STRATEGY_ID,
        version=_VERSION,
        default_up_threshold=_UP_THRESHOLD,
        default_rem_min=_REM_MIN,
        default_rem_max=_REM_MAX,
        entry_reason_label="tickformer_v18_golden_pass",
        not_available_reason="tickformer_v18_not_available",
    )
