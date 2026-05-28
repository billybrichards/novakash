"""Shadow trigger value objects for the exit monitor domain layer.

Pure data — no IO, no imports outside stdlib. Fully testable in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ShadowTrigger:
    """Immutable value object representing one threshold crossing on one tick.

    One ShadowTrigger is produced per threshold that fires on a given tick.
    The caller receives a list of ShadowTriggers (up to 4 per tick — one per
    threshold) and writes one ``exit_monitor_shadow`` row per trigger so all
    thresholds are persisted for post-hoc re-tuning.

    Attributes:
        decision_id:          strategy_decisions.id for the trade being watched.
        asset:                e.g. 'BTC'.
        window_ts:            5-min window unix timestamp.
        strategy_id:          e.g. 'tickformer_v18_t180'.
        side:                 'UP' or 'DN' — the direction the trade is holding.
        entry_p:              TickFormer prob at trade entry (None when missing).
        entry_eval_offset:    seconds-to-close at entry (None when missing).
        trigger_eval_offset:  seconds-to-close when shadow exit fired.
        threshold:            the p_against threshold that fired (0.50/0.55/0.60/0.65).
        p_against:            actual p_against at trigger.
        p_for:                actual p_for at trigger (= 1 - p_against for binary).
        tickformer_model:     'v18' or 'v20'.
    """

    decision_id: int
    asset: str
    window_ts: int
    strategy_id: str
    side: str                     # 'UP' | 'DN'
    trigger_eval_offset: int
    threshold: float              # 0.50 | 0.55 | 0.60 | 0.65
    p_against: float
    p_for: float
    tickformer_model: str         # 'v18' | 'v20'
    entry_p: float | None = None
    entry_eval_offset: int | None = None
