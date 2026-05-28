"""ExitSignalDetector — pure domain service for shadow exit detection.

No IO. No side effects. Returns a list of ShadowTrigger value objects.

Design notes:
  - Uses p_against thresholds from EXIT_MONITOR_FEASIBILITY.md.
  - First-cross-only per threshold per trade (first_cross_seen tracks which
    thresholds have already fired so they don't re-fire on the same trade).
  - All thresholds evaluated per tick so one row per threshold is written to
    exit_monitor_shadow — enabling post-hoc re-tuning without a re-run.
  - Binary symmetry: for side='UP', p_against = 1 - p_tickformer_v18.
    For side='DN', p_against = p_tickformer_v18 directly.
"""
from __future__ import annotations

from typing import Literal

from exit_monitor.domain.shadow_trigger import ShadowTrigger

# Default thresholds from EXIT_MONITOR_FEASIBILITY.md.
# Primary threshold is 0.55 (78% recall, 8.2% false-rate, 97s median lead).
# All four are evaluated per tick so the caller banks data for re-tuning.
DEFAULT_THRESHOLDS = (0.50, 0.55, 0.60, 0.65)


def _compute_p_against(prob_tickformer: float, side: Literal["UP", "DN"]) -> float:
    """Translate raw TickFormer P(UP) into p_against for the held direction.

    TickFormer always outputs P(UP).  For a trade holding 'DN', a high P(UP)
    IS the "against" signal — p_against = prob_tickformer directly.
    For a trade holding 'UP', p_against = 1 - prob_tickformer.
    """
    if side == "UP":
        return 1.0 - prob_tickformer
    return prob_tickformer  # side == "DN"


class ExitSignalDetector:
    """Stateless pure-function detector plus per-trade first-cross state.

    Instantiate once per monitored position; discard when the position closes.

    The first-cross-seen set prevents repeated firing on the same threshold
    within the same trade lifetime. This matches the design intent: we want
    one row per threshold at first crossing, not thousands of duplicate rows
    for every subsequent tick above the threshold.
    """

    def __init__(self) -> None:
        # Set of thresholds (float) that have already fired for this position.
        self._fired: set[float] = set()

    def should_shadow_exit(
        self,
        *,
        decision_id: int,
        asset: str,
        window_ts: int,
        strategy_id: str,
        side: str,              # 'UP' | 'DN'
        prob_tickformer: float,
        eval_offset: int,
        tickformer_model: str,
        entry_p: float | None = None,
        entry_eval_offset: int | None = None,
        thresholds: tuple[float, ...] = DEFAULT_THRESHOLDS,
    ) -> list[ShadowTrigger]:
        """Evaluate all thresholds and return triggers for those that fire.

        Returns an empty list when no threshold fires or all thresholds have
        already fired (first-cross-only guarantee).

        Args:
            decision_id:       strategy_decisions.id for this trade.
            asset:             e.g. 'BTC'.
            window_ts:         5-min bar unix timestamp.
            strategy_id:       e.g. 'tickformer_v18_t180'.
            side:              'UP' or 'DN' — the direction the trade holds.
            prob_tickformer:   raw P(UP) from the TickFormer model.
            eval_offset:       seconds-to-close at evaluation tick.
            tickformer_model:  'v18' or 'v20'.
            entry_p:           P(UP) at entry tick (for logging, not gating).
            entry_eval_offset: seconds-to-close at entry (for logging).
            thresholds:        ordered tuple of p_against thresholds to evaluate.

        Returns:
            List of ShadowTrigger — one per newly-crossed threshold.
        """
        p_against = _compute_p_against(prob_tickformer, side)
        p_for = 1.0 - p_against

        triggers: list[ShadowTrigger] = []
        for threshold in thresholds:
            if threshold in self._fired:
                continue
            if p_against >= threshold:
                self._fired.add(threshold)
                triggers.append(
                    ShadowTrigger(
                        decision_id=decision_id,
                        asset=asset,
                        window_ts=window_ts,
                        strategy_id=strategy_id,
                        side=side,
                        trigger_eval_offset=eval_offset,
                        threshold=threshold,
                        p_against=p_against,
                        p_for=p_for,
                        tickformer_model=tickformer_model,
                        entry_p=entry_p,
                        entry_eval_offset=entry_eval_offset,
                    )
                )
        return triggers

    def reset(self) -> None:
        """Clear fired-threshold state (call when a position is recycled)."""
        self._fired.clear()
