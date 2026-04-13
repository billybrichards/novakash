"""SessionHoursGate -- filters by UTC hour of day."""

from __future__ import annotations

from typing import TYPE_CHECKING

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


class SessionHoursGate(Gate):
    """Pass if the current UTC hour is in the allowed set."""

    def __init__(self, hours_utc: list[int]):
        self._hours = frozenset(hours_utc)

    @property
    def name(self) -> str:
        return "session_hours"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        hour = surface.hour_utc
        if hour is None:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason="hour_utc is None",
            )
        if hour in self._hours:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason=f"hour={hour} in allowed set",
                data={"hour_utc": hour},
            )
        return GateResult(
            passed=False,
            gate_name=self.name,
            reason=f"hour={hour} not in {sorted(self._hours)}",
            data={"hour_utc": hour},
        )
