"""TimingGate -- checks eval_offset is within the strategy's trading window."""

from __future__ import annotations

from typing import TYPE_CHECKING

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


class TimingGate(Gate):
    """Pass if eval_offset is between min_offset and max_offset (inclusive)."""

    def __init__(self, min_offset: int, max_offset: int):
        self._min = min_offset
        self._max = max_offset

    @property
    def name(self) -> str:
        return "timing"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        offset = surface.eval_offset
        if offset is None:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason="eval_offset is None",
            )
        if self._min <= offset <= self._max:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason=f"T-{offset} in [{self._min}, {self._max}]",
                data={"eval_offset": offset},
            )
        return GateResult(
            passed=False,
            gate_name=self.name,
            reason=f"T-{offset} outside [{self._min}, {self._max}]",
            data={"eval_offset": offset},
        )
