"""VPINGate -- VPIN floor + optional CASCADE regime block.

Reads ``surface.vpin`` and ``surface.regime`` (VPIN regime, one of
CALM | NORMAL | TRANSITION | CASCADE). Passes when:
  * vpin >= min, AND
  * if block_cascade=True, regime != "CASCADE".

Introduced for v4_down_only v2.3.0 — prevents firing during the
CASCADE-inversion pattern seen 2026-04-17 (project_overnight_collapse_apr17).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


class VPINGate(Gate):
    """VPIN floor gate with optional CASCADE block."""

    def __init__(self, min: float = 0.40, block_cascade: bool = True):
        self._min = float(min)
        self._block_cascade = bool(block_cascade)

    @property
    def name(self) -> str:
        return "vpin_gate"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        vpin = surface.vpin
        if vpin is None:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason="vpin is None",
            )
        if vpin < self._min:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=f"vpin={vpin:.3f} < {self._min:.3f}",
                data={"vpin": vpin, "min": self._min},
            )
        if self._block_cascade:
            regime = surface.regime
            if regime is not None and str(regime).upper() == "CASCADE":
                return GateResult(
                    passed=False,
                    gate_name=self.name,
                    reason=f"vpin_regime=CASCADE blocked (vpin={vpin:.3f})",
                    data={"vpin": vpin, "vpin_regime": regime},
                )
        return GateResult(
            passed=True,
            gate_name=self.name,
            reason=f"vpin={vpin:.3f} >= {self._min:.3f} (regime={surface.regime})",
            data={"vpin": vpin, "vpin_regime": surface.regime},
        )
