"""CLOBSizingGate -- sets position size modifier from CLOB data.

This is a "pass-through" gate -- it always passes but attaches
size_modifier data that the registry uses for position sizing.
If CLOB data indicates untradeable conditions (e.g. clob_down_ask < 0.25),
it fails the gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


class CLOBSizingGate(Gate):
    """Evaluate CLOB pricing bands and set size modifier.

    schedule: list of (threshold, modifier, label) tuples, descending by threshold.
    null_modifier: modifier when CLOB data is None (strong moves often lack CLOB data).
    """

    def __init__(
        self,
        schedule: list[dict],
        null_modifier: float = 1.0,
    ):
        # Convert from YAML dict format to tuples
        self._schedule: list[tuple[float, float, str]] = [
            (entry["threshold"], entry["modifier"], entry["label"])
            for entry in schedule
        ]
        self._null_modifier = null_modifier

    @property
    def name(self) -> str:
        return "clob_sizing"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        clob_ask = surface.clob_down_ask

        if clob_ask is None:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason=f"no CLOB data, using null_modifier={self._null_modifier}",
                data={"size_modifier": self._null_modifier, "label": "no_clob"},
            )

        for threshold, modifier, label in self._schedule:
            if clob_ask >= threshold:
                if modifier == 0.0:
                    return GateResult(
                        passed=False,
                        gate_name=self.name,
                        reason=f"clob_down_ask={clob_ask:.3f} => {label} (skip)",
                        data={"clob_down_ask": clob_ask, "label": label},
                    )
                return GateResult(
                    passed=True,
                    gate_name=self.name,
                    reason=f"clob_down_ask={clob_ask:.3f} => {label} ({modifier}x)",
                    data={
                        "size_modifier": modifier,
                        "label": label,
                        "clob_down_ask": clob_ask,
                    },
                )

        # Below all thresholds
        return GateResult(
            passed=False,
            gate_name=self.name,
            reason=f"clob_down_ask={clob_ask:.3f} below all thresholds",
            data={"clob_down_ask": clob_ask},
        )
