"""Conviction Fade Detector — exit monitor for fading LGB confidence.

The hedge-exit system looks for model *flips* to opposite direction,
but real losses show a different pattern: conviction fades — the model
stays in the same direction but loses confidence (dist shrinks from
entry level).

This detector:
  1. Records entry conviction (LGB dist) at fill registration time
  2. On each eval tick, compares current LGB dist to entry dist
  3. Triggers when dist drops by ``fade_threshold_pct`` (percentage) or
     below ``absolute_floor`` (absolute) for ``min_consecutive_ticks``
     consecutive ticks within the active offset window

Shadow mode (default) logs but does NOT execute exits, collecting data
for threshold tuning.

See Hub notes — conviction fade analysis 2026-04-29.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

import structlog

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

log = structlog.get_logger(__name__)


@dataclass
class FadeInstruction:
    """Returned when conviction fade detector fires."""

    strategy_id: str
    window_ts: int
    entry_dist: float
    current_dist: float
    fade_pct: float  # how much dist has dropped, as fraction (0.0–1.0)
    recommended_action: str  # "shadow_exit" or "exit"
    trigger_reason: str  # "threshold_pct" or "absolute_floor"
    consecutive_ticks: int


@dataclass
class _FadeState:
    """Per-position fade tracking state."""

    entry_dist: float
    consecutive_fade_ticks: int = 0
    registered_at: float = field(default_factory=time.time)


class ConvictionFadeDetector:
    """Detects conviction fading for open monitored positions.

    Singleton per engine instance, like PositionMonitor.
    State keyed by ``{strategy_id}:{window_ts}``.
    """

    def __init__(self) -> None:
        self._states: dict[str, _FadeState] = {}
        self._log = log.bind(component="conviction_fade")

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        strategy_id: str,
        window_ts: int,
        entry_dist: float,
    ) -> None:
        """Record entry conviction at fill time.

        ``entry_dist`` = abs(probability_lgb - 0.5) at time of fill.
        """
        key = f"{strategy_id}:{window_ts}"
        self._states[key] = _FadeState(entry_dist=entry_dist)
        self._log.info(
            "conviction_fade.registered",
            key=key,
            entry_dist=f"{entry_dist:.4f}",
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def remove(self, strategy_id: str, window_ts: int) -> None:
        """Remove tracking state for a closed/expired position."""
        key = f"{strategy_id}:{window_ts}"
        self._states.pop(key, None)

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate(
        self,
        strategy_id: str,
        window_ts: int,
        surface: Any,
        *,
        conviction_fade_enabled: bool = True,
        conviction_fade_threshold_pct: float = 0.40,
        conviction_fade_absolute_floor: float = 0.08,
        conviction_fade_min_ticks: int = 3,
        conviction_fade_active_offset_min: int = 60,
        conviction_fade_active_offset_max: int = 200,
        stale_mark_max_age_seconds: float = 5.0,
    ) -> Optional[FadeInstruction]:
        """Evaluate conviction fade for one position on one tick.

        Returns a ``FadeInstruction`` when the fade gate fires,
        ``None`` otherwise.

        Always logs the tick for shadow data collection regardless of
        whether the gate fires.
        """
        if not conviction_fade_enabled:
            return None

        key = f"{strategy_id}:{window_ts}"
        state = self._states.get(key)
        if state is None:
            return None

        # ── Window match guard ────────────────────────────────────────
        surface_window_ts = getattr(surface, "window_ts", None)
        if surface_window_ts is not None and surface_window_ts != window_ts:
            return None

        # ── Active offset window ──────────────────────────────────────
        eval_offset = getattr(surface, "eval_offset", None)
        if eval_offset is None:
            return None
        try:
            offset_f = float(eval_offset)
        except (TypeError, ValueError):
            return None
        if not (conviction_fade_active_offset_min <= offset_f <= conviction_fade_active_offset_max):
            # Outside active window — don't reset count (same pattern as
            # hedge exit: a flicker outside shouldn't reset a nearly-fired gate)
            return None

        # ── Stale CLOB guard ──────────────────────────────────────────
        now = time.time()
        last_clob = getattr(surface, "last_clob_update_ts", None)
        if last_clob is not None:
            try:
                if (now - float(last_clob)) > stale_mark_max_age_seconds:
                    return None
            except (TypeError, ValueError):
                pass

        # ── Read current LGB dist ─────────────────────────────────────
        # Try v10-suffixed first for v10 strategies, then fall back.
        probability_lgb = None
        if "v10" in (strategy_id or "").lower():
            probability_lgb = getattr(surface, "probability_lgb_v10", None)
        if probability_lgb is None:
            probability_lgb = getattr(surface, "probability_lgb", None)
        if probability_lgb is None:
            # Surface incomplete — fail closed, reset count.
            state.consecutive_fade_ticks = 0
            self._log.debug(
                "conviction_fade.skip_no_lgb",
                key=key,
            )
            return None
        try:
            p_lgb = float(probability_lgb)
        except (TypeError, ValueError):
            state.consecutive_fade_ticks = 0
            return None

        current_dist = abs(p_lgb - 0.5)
        entry_dist = state.entry_dist

        # ── Guard: entry_dist is 0 or near-zero ──────────────────────
        if entry_dist <= 0.001:
            self._log.debug(
                "conviction_fade.skip_zero_entry",
                key=key,
                entry_dist=f"{entry_dist:.4f}",
            )
            state.consecutive_fade_ticks = 0
            return None

        # ── Compute fade percentage ───────────────────────────────────
        fade_pct = (entry_dist - current_dist) / entry_dist if entry_dist > 0 else 0.0

        # ── Check triggers ────────────────────────────────────────────
        threshold_triggered = fade_pct >= conviction_fade_threshold_pct
        floor_triggered = current_dist < conviction_fade_absolute_floor
        is_faded = threshold_triggered or floor_triggered

        if is_faded:
            state.consecutive_fade_ticks += 1
        else:
            state.consecutive_fade_ticks = 0

        trigger_reason = ""
        if threshold_triggered and floor_triggered:
            trigger_reason = "threshold_pct+absolute_floor"
        elif threshold_triggered:
            trigger_reason = "threshold_pct"
        elif floor_triggered:
            trigger_reason = "absolute_floor"

        # ── Log every tick ────────────────────────────────────────────
        self._log.info(
            "conviction_fade.tick",
            strategy=strategy_id,
            window_ts=window_ts,
            entry_dist=f"{entry_dist:.4f}",
            current_dist=f"{current_dist:.4f}",
            fade_pct=f"{fade_pct:.4f}",
            threshold=f"{conviction_fade_threshold_pct:.2f}",
            floor=f"{conviction_fade_absolute_floor:.3f}",
            ticks_below=f"{state.consecutive_fade_ticks}/{conviction_fade_min_ticks}",
            is_faded=is_faded,
        )

        # ── Check consecutive tick threshold ──────────────────────────
        if state.consecutive_fade_ticks >= conviction_fade_min_ticks:
            self._log.info(
                "conviction_fade.triggered",
                strategy=strategy_id,
                window_ts=window_ts,
                entry_dist=f"{entry_dist:.4f}",
                current_dist=f"{current_dist:.4f}",
                fade_pct=f"{fade_pct:.4f}",
                trigger_reason=trigger_reason,
                consecutive_ticks=state.consecutive_fade_ticks,
            )
            return FadeInstruction(
                strategy_id=strategy_id,
                window_ts=window_ts,
                entry_dist=entry_dist,
                current_dist=current_dist,
                fade_pct=fade_pct,
                recommended_action="exit",
                trigger_reason=trigger_reason,
                consecutive_ticks=state.consecutive_fade_ticks,
            )

        # ── Not triggered — log reason for false-negative analysis ────
        if not is_faded:
            self._log.info(
                "conviction_fade.no_trigger",
                strategy=strategy_id,
                window_ts=window_ts,
                entry_dist=f"{entry_dist:.4f}",
                current_dist=f"{current_dist:.4f}",
                fade_pct=f"{fade_pct:.4f}",
                reason="below_threshold",
            )
        else:
            self._log.info(
                "conviction_fade.no_trigger",
                strategy=strategy_id,
                window_ts=window_ts,
                entry_dist=f"{entry_dist:.4f}",
                current_dist=f"{current_dist:.4f}",
                fade_pct=f"{fade_pct:.4f}",
                reason=f"consecutive_ticks_{state.consecutive_fade_ticks}/{conviction_fade_min_ticks}",
            )

        return None
