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

RECAL 2026-05-28 (fix/tickformer-threshold-recal-2026-05-28):
  When env var ``TICKFORMER_RECAL_2026_05_28=true`` is set, the
  default thresholds switch to the live-calibrated values:
    up_threshold  : 0.90  →  0.86  (sniper-grade; live max 0.923)
    down_threshold: 0.10  →  0.14  (symmetric)
    eval_offset band: [60, 220] → [60, 119]  (100% WR n=6; AVOID 180-220s)
  Live evidence (hub notes #728/#729):
    prob 0.86+ × eval_offset_remaining 60-119s  → n=6,  WR 100% (TARGET)
    prob 0.86+ × eval_offset_remaining 180-220s → n=5,  WR  40% (TRAP — excluded)
  WARNING: 180-220s at high conviction is counterintuitively WORSE for v18.
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

_STRATEGY_ID = "tickformer_v18_t180"
_VERSION = "1.0.0"

# ── Recalibration env-flag (fix/tickformer-threshold-recal-2026-05-28) ──
_PRE_RECAL_UP_THRESHOLD = 0.90
_PRE_RECAL_REM_MIN = 60
_PRE_RECAL_REM_MAX = 220
# Live-calibrated defaults (7d BTC 5m sweep, hub notes #728/#729).
# Target: 0.86+ × 60-119s → WR 100% (n=6). Exclude 180-220s trap (40% WR).
_RECAL_UP_THRESHOLD = 0.86
_RECAL_REM_MIN = 60
_RECAL_REM_MAX = 119


def _recal_active() -> bool:
    return os.environ.get(
        "TICKFORMER_RECAL_2026_05_28", "false"
    ).strip().lower() in ("1", "true", "yes", "on")


def evaluate_tickformer_v18_t180(
    surface: "FullDataSurface",
) -> StrategyDecision:
    """Evaluate the v18 balanced t-180 SHADOW strategy.

    See :func:`strategies.configs._tickformer_base.evaluate_tickformer_strategy`
    for the full gating logic.

    When ``TICKFORMER_RECAL_2026_05_28=true`` the band tightens to
    60-119s (100% WR pocket) and actively excludes the 180-220s trap
    (40% WR at high conviction per hub notes #728/#729).
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
        prob_column="probability_tickformer_v18",
        strategy_id=_STRATEGY_ID,
        version=_VERSION,
        default_up_threshold=up_thr,
        default_rem_min=rem_min,
        default_rem_max=rem_max,
        entry_reason_label="tickformer_v18_t180_pass",
        not_available_reason="tickformer_v18_not_available",
    )
