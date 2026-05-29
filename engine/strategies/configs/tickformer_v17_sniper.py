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

RECAL 2026-05-28 (fix/tickformer-threshold-recal-2026-05-28):
  When env var ``TICKFORMER_RECAL_2026_05_28=true`` is set, the
  default thresholds switch to the live-calibrated values:
    up_threshold  : 0.85  →  0.65  (live max 0.752; zero crosses at 0.85)
    down_threshold: 0.15  →  0.35  (symmetric)
    eval_offset band: [60, 140] → [60, 179]  (100% WR pocket confirmed)
  Live evidence (hub notes #728/#729):
    prob 0.65-0.69 × eval_offset_remaining 120-179s → n=13, WR 100%
    prob 0.70-0.74 × eval_offset_remaining 60-119s  → n=15, WR 100%
  When false (default) the pre-recal defaults apply unchanged.

SHADOW kill switch via ``gate_params.shadow_only=1`` as in v16.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies.configs._tickformer_base import evaluate_tickformer_strategy

_STRATEGY_ID = "tickformer_v17_sniper"
_VERSION = "1.0.0"

# ── Recalibration env-flag (fix/tickformer-threshold-recal-2026-05-28) ──
_PRE_RECAL_UP_THRESHOLD = 0.85
_PRE_RECAL_REM_MIN = 60
_PRE_RECAL_REM_MAX = 140
# Live-calibrated defaults (7d BTC 5m sweep, hub notes #728/#729).
# Best pockets: 0.65-0.69×120-179s (WR 100% n=13) + 0.70-0.74×60-119s (WR 100% n=15).
_RECAL_UP_THRESHOLD = 0.65
_RECAL_REM_MIN = 60
_RECAL_REM_MAX = 179


def _recal_active() -> bool:
    return os.environ.get(
        "TICKFORMER_RECAL_2026_05_28", "false"
    ).strip().lower() in ("1", "true", "yes", "on")


def evaluate_tickformer_v17_sniper(
    surface: "FullDataSurface",
) -> StrategyDecision:
    """Evaluate the v17 sniper SHADOW strategy.

    See :func:`strategies.configs._tickformer_base.evaluate_tickformer_strategy`
    for the full gating logic.

    When ``TICKFORMER_RECAL_2026_05_28=true`` the default thresholds
    switch to the live-calibrated values (hub notes #728/#729).
    """
    if _recal_active():
        up_thr = _RECAL_UP_THRESHOLD
        rem_min = _RECAL_REM_MIN
        rem_max = _RECAL_REM_MAX
    else:
        up_thr = _PRE_RECAL_UP_THRESHOLD
        rem_min = _PRE_RECAL_REM_MIN
        rem_max = _PRE_RECAL_REM_MAX

    return evaluate_tickformer_strategy(
        surface,
        prob_column="probability_tickformer_v17",
        strategy_id=_STRATEGY_ID,
        version=_VERSION,
        default_up_threshold=up_thr,
        default_rem_min=rem_min,
        default_rem_max=rem_max,
        entry_reason_label="tickformer_v17_sniper_pass",
        not_available_reason="tickformer_v17_not_available",
    )
