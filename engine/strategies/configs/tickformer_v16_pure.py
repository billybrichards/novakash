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

RECAL 2026-05-28 (fix/tickformer-threshold-recal-2026-05-28):
  When env var ``TICKFORMER_RECAL_2026_05_28=true`` is set, the
  default thresholds switch to the live-calibrated values:
    up_threshold  : 0.85  →  0.65  (live max 0.831; 88.9% WR pocket)
    down_threshold: 0.15  →  0.35  (symmetric)
    eval_offset band: unchanged [60, 240]
  When false (default) the pre-recal defaults are used unchanged —
  safe to merge before the engine agent picks up the env flag.
  Preferred pocket at promotion: eval_offset_remaining 180-220s
  (Tier D via tickformer_tiers.yaml).

SHADOW kill switch — when ``gate_params.shadow_only=1`` (default) the
strategy returns SKIP-with-decision-record even if YAML mode flips to
LIVE. See ``feedback_no_auto_promote.md`` (post-2026-04-17 rule).

Sibling timesfm PR: magic-model branch.
Engine precedent: v9_5_xrp_up_solo.py.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

from domain.value_objects import StrategyDecision
from strategies.configs._tickformer_base import evaluate_tickformer_strategy

_STRATEGY_ID = "tickformer_v16_pure"
_VERSION = "1.0.0"

# ── Recalibration env-flag (fix/tickformer-threshold-recal-2026-05-28) ──
# Pre-recal defaults (original offline-backtest operating point).
_PRE_RECAL_UP_THRESHOLD = 0.85
_PRE_RECAL_REM_MIN = 60
_PRE_RECAL_REM_MAX = 240
# Live-calibrated defaults (7d BTC 5m sweep, hub notes #728/#729).
# Best live pocket: prob 0.65-0.69 × eval_offset_remaining 180-220s → WR 88.9% (n=27).
_RECAL_UP_THRESHOLD = 0.65
_RECAL_REM_MIN = 60
_RECAL_REM_MAX = 240


def _recal_active() -> bool:
    return os.environ.get(
        "TICKFORMER_RECAL_2026_05_28", "false"
    ).strip().lower() in ("1", "true", "yes", "on")


def evaluate_tickformer_v16_pure(
    surface: "FullDataSurface",
) -> StrategyDecision:
    """Evaluate the v16 magic-model SHADOW strategy.

    See :func:`strategies.configs._tickformer_base.evaluate_tickformer_strategy`
    for the full gating logic.

    When ``TICKFORMER_RECAL_2026_05_28=true`` the default thresholds
    switch to the live-calibrated values (hub notes #728/#729). The YAML
    gate_params always win if explicitly set — the env flag only affects
    the Python-side *default* arguments passed to the base evaluator.
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
        prob_column="probability_tickformer_v16",
        strategy_id=_STRATEGY_ID,
        version=_VERSION,
        default_up_threshold=up_thr,
        default_rem_min=rem_min,
        default_rem_max=rem_max,
        entry_reason_label="tickformer_v16_pure_pass",
        not_available_reason="tickformer_v16_not_available",
    )
