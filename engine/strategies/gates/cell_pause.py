"""CellPauseGate -- skip when a (strategy, direction, t_band, regime,
session) cell is currently auto-paused by the RollingWRMonitor.

Audits #379 + #385 (2026-05-06).

The gate consults a sync `lookup` callable injected at construction:
    lookup(strategy_id, direction, t_band, regime, session) -> Optional[str]
Returns a `reason` string when the cell is actively paused, or None when
it's not. The actual datastore (cell_pauses table) is read by an
in-memory snapshot service (refreshed every 30s by the engine main loop)
so the gate stays sync + zero-I/O at decision time.

If `lookup` is None the gate is a no-op (PASS) — useful before the
snapshot service has been wired into the engine boot path.
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING, Any, Callable, Optional

from services.cell_bucketing import session as _session
from services.cell_bucketing import t_band as _t_band
from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


# Function signature for the cell-pause lookup. Returns a string reason
# when the cell is actively paused, or None.
CellPauseLookup = Callable[
    [str, str, str, Optional[str], Optional[str]], Optional[str]
]


class CellPauseGate(Gate):
    """Skip when this strategy's (direction, t_band, regime, session) cell
    is actively auto-paused.

    Args:
        strategy_id: The strategy this gate belongs to. The gate is keyed
            by strategy so a pause for one strategy doesn't affect others.
        lookup: Sync callable that resolves a cell to a reason string when
            paused. None disables the gate (no-op PASS) — used during boot
            before the rolling-WR snapshot service is initialised.
        direction_field: Surface field to read for direction. Default
            "direction" (set by upstream signal blend); strategies that
            decide direction inside their hook can set this to None and
            pass `direction` via kwargs.
        regime_field: Surface field for the v4 regime. Default "v4_regime".
        hour_field: Surface field for hour-of-day UTC. Default "hour_utc".
    """

    def __init__(
        self,
        strategy_id: str,
        lookup: Optional[CellPauseLookup] = None,
        direction_field: str = "direction",
        regime_field: str = "v4_regime",
        hour_field: str = "hour_utc",
    ):
        self._strategy_id = strategy_id
        self._lookup = lookup
        self._direction_field = direction_field
        self._regime_field = regime_field
        self._hour_field = hour_field

    @property
    def name(self) -> str:
        return "cell_pause"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        if self._lookup is None:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason="no lookup wired (no-op)",
            )

        direction = self._resolve_direction(surface)
        if direction not in ("UP", "DOWN"):
            # Direction unknown at this point in the pipeline — let the
            # downstream signal-direction gate fire and skip with its own
            # reason. We don't want to block on partial info.
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason="direction unresolved (deferred to signal gate)",
            )

        eval_offset = getattr(surface, "eval_offset", None)
        regime = getattr(surface, self._regime_field, None)

        hour = getattr(surface, self._hour_field, None)
        if hour is None:
            window_ts = getattr(surface, "window_ts", None)
            if window_ts:
                try:
                    hour = _dt.datetime.fromtimestamp(
                        int(window_ts), _dt.timezone.utc
                    ).hour
                except (TypeError, ValueError, OSError):
                    hour = None

        cell_t_band = _t_band(eval_offset)
        cell_session = _session(hour)

        try:
            reason = self._lookup(
                self._strategy_id,
                direction,
                cell_t_band,
                regime,
                cell_session,
            )
        except Exception as exc:  # pragma: no cover — lookup must not raise
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason=f"lookup raised; failing open ({exc!r})",
            )

        if reason is None:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason=(
                    f"cell active: {direction}/{cell_t_band}/"
                    f"{regime or '*'}/{cell_session}"
                ),
            )

        return GateResult(
            passed=False,
            gate_name=self.name,
            reason=f"cell paused: {reason}",
            data={
                "strategy_id": self._strategy_id,
                "direction": direction,
                "t_band": cell_t_band,
                "regime": regime,
                "session": cell_session,
                "pause_reason": reason,
            },
        )

    def _resolve_direction(
        self, surface: "FullDataSurface"
    ) -> Optional[str]:
        d = getattr(surface, self._direction_field, None)
        if d in ("UP", "DOWN"):
            return d
        # Convenience fallback: derive direction from the LGB probability
        # when present (matches v9_ensemble's pl_dir computation).
        for fld in (
            "probability_lgb",
            "probability_lgb_v9_2",
            "probability_lgb_v9_1",
            "probability_lgb_v12",
        ):
            v: Any = getattr(surface, fld, None)
            if v is not None:
                try:
                    return "UP" if float(v) > 0.5 else "DOWN"
                except (TypeError, ValueError):
                    continue
        return None
