"""RegimeV4Gate -- v4 HMM regime filter using ``allow`` param name.

Thin alias over the existing RegimeGate semantics: reads
``surface.v4_regime`` and passes when the regime is in the ``allow``
set. Distinct from RegimeGate (which uses ``allowed``) so v4_down_only
v2.3.0's YAML matches the spec exactly.

Fails (skips the trade) when v4_regime is None rather than passing by
default — for DOWN-only strategies we want strict evidence of a
tradeable regime before firing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


class RegimeV4Gate(Gate):
    """Pass if ``surface.v4_regime`` is in the ``allow`` set."""

    def __init__(self, allow: list[str]):
        # Preserve case-insensitivity — HMM state strings are lowercase
        # but be defensive.
        self._allow = {str(r).lower() for r in allow}

    @property
    def name(self) -> str:
        return "regime_v4"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        regime = surface.v4_regime
        if regime is None:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=f"v4_regime is None; allow={sorted(self._allow)}",
            )
        if str(regime).lower() in self._allow:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason=f"regime={regime} in allow={sorted(self._allow)}",
                data={"v4_regime": regime},
            )
        return GateResult(
            passed=False,
            gate_name=self.name,
            reason=f"regime={regime} not in allow={sorted(self._allow)}",
            data={"v4_regime": regime},
        )
