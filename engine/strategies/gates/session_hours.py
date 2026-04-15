"""SessionHoursGate -- filters by UTC hour of day.

Supports two mutually exclusive modes:

* **Allowlist** (``hours_utc``): gate passes only when the current UTC hour is
  in the provided set.  All other hours are blocked.

* **Blocklist** (``block_hours_utc``): gate passes for every UTC hour *except*
  those in the provided set.  Useful for excluding a specific session window
  (e.g. US open 14-19 UTC) without enumerating all allowed hours.

Exactly one of the two parameters must be supplied.  Passing both raises a
``ValueError``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


class SessionHoursGate(Gate):
    """Pass or fail based on the current UTC hour.

    Allowlist mode (``hours_utc``):
        Gate passes when ``hour_utc`` is in the allowed set.

    Blocklist mode (``block_hours_utc``):
        Gate fails when ``hour_utc`` is in the blocked set.
    """

    def __init__(
        self,
        hours_utc: list[int] | None = None,
        block_hours_utc: list[int] | None = None,
    ):
        if hours_utc and block_hours_utc:
            raise ValueError(
                "SessionHoursGate: use hours_utc OR block_hours_utc, not both"
            )
        if not hours_utc and not block_hours_utc:
            raise ValueError(
                "SessionHoursGate: must provide hours_utc or block_hours_utc"
            )
        self._allow = frozenset(hours_utc or [])
        self._block = frozenset(block_hours_utc or [])

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
        if self._allow and hour not in self._allow:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=f"hour={hour} not in allowed {sorted(self._allow)}",
                data={"hour_utc": hour},
            )
        if self._block and hour in self._block:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=f"hour={hour} in blocked {sorted(self._block)}",
                data={"hour_utc": hour},
            )
        return GateResult(
            passed=True,
            gate_name=self.name,
            reason=(
                f"hour={hour} in allowed {sorted(self._allow)}"
                if self._allow
                else f"hour={hour} not in blocked {sorted(self._block)}"
            ),
            data={"hour_utc": hour},
        )
