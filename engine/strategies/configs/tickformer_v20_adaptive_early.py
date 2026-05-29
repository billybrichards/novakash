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

RECAL 2026-05-28 (fix/tickformer-threshold-recal-2026-05-28):
  # v20 in SHADOW soak only — live WR @ 0.85 was 63% (below 80% threshold
  # for GHOST promotion). DO NOT promote to GHOST without further soak data.
  When env var ``TICKFORMER_RECAL_2026_05_28=true`` is set:
    up_threshold  : 0.90  →  0.85  (live max 0.927; 0.90 had zero crosses)
    down_threshold: 0.10  →  0.15  (symmetric)
    eval_offset band: [120, 280] → [60, 280]  (widen for broader soak coverage)
  Live evidence (hub notes #728/#729):
    prob 0.85-0.86 × eval_offset_remaining 60-119s → n=238, WR 63%
    (not 90% — SHADOW soak only, NOT promoting to GHOST)
  When false (default) the pre-recal defaults apply unchanged.

SHADOW kill switch via ``gate_params.shadow_only=1``.
(fix/tickformer-strategies-actually-fire — v20 scaffold)
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies.configs._tickformer_base import evaluate_tickformer_strategy

_STRATEGY_ID = "tickformer_v20_adaptive_early"
_VERSION = "1.0.0"

# ── Recalibration env-flag (fix/tickformer-threshold-recal-2026-05-28) ──
_PRE_RECAL_UP_THRESHOLD = 0.90
_PRE_RECAL_REM_MIN = 120
_PRE_RECAL_REM_MAX = 280
# Live-calibrated defaults (7d BTC 5m sweep, hub notes #728/#729).
# 0.85-0.86 × 60-119s → WR 63% (n=238). SHADOW soak only. NOT GHOST yet.
_RECAL_UP_THRESHOLD = 0.85
_RECAL_REM_MIN = 60
_RECAL_REM_MAX = 280


def _recal_active() -> bool:
    return os.environ.get(
        "TICKFORMER_RECAL_2026_05_28", "false"
    ).strip().lower() in ("1", "true", "yes", "on")


def evaluate_tickformer_v20_adaptive_early(
    surface: "FullDataSurface",
) -> StrategyDecision:
    """Evaluate the v20 adaptive early-entry SHADOW strategy.

    See :func:`strategies.configs._tickformer_base.evaluate_tickformer_strategy`
    for the full gating logic.

    When ``TICKFORMER_RECAL_2026_05_28=true`` the threshold lowers
    0.90→0.85 (was unreachable) and the band widens to 60-280s for
    broader soak coverage. Mode remains SHADOW — live 7d WR was 63%,
    below the 80% bar for GHOST promotion (hub note #729).
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
        prob_column="probability_tickformer_v20",
        strategy_id=_STRATEGY_ID,
        version=_VERSION,
        default_up_threshold=up_thr,
        default_rem_min=rem_min,
        default_rem_max=rem_max,
        entry_reason_label="tickformer_v20_adaptive_early_pass",
        not_available_reason="tickformer_v20_not_available",
    )
