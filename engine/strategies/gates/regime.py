"""RegimeGate -- filters by HMM regime."""

from __future__ import annotations

from typing import TYPE_CHECKING

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


class RegimeGate(Gate):
    """Pass if the current V4 regime is in the allowed set."""

    def __init__(self, allowed: list[str]):
        self._allowed = set(r.lower() for r in allowed)

    @property
    def name(self) -> str:
        return "regime"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        regime = surface.v4_regime
        if regime is None:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason="no regime data, pass by default",
            )

        if regime.lower() in self._allowed:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason=f"regime={regime} in allowed set",
                data={"regime": regime},
            )

        return GateResult(
            passed=False,
            gate_name=self.name,
            reason=f"regime={regime} not in {sorted(self._allowed)}",
            data={"regime": regime},
        )
