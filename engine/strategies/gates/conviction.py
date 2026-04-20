"""ConvictionGate -- filters by v4 conviction tier.

Reads ``surface.v4_conviction`` ("NONE" | "LOW" | "MEDIUM" | "HIGH") and
passes when the tier is in the ``allowed`` set. Case-insensitive match.

Introduced for v4_down_only v2.3.0 (Hub note #198 — MEDIUM conviction
post-ensemble is 46% WR / -$3.67 net, same failure pattern as today's
first LIVE v4_down_only trade).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


class ConvictionGate(Gate):
    """Pass if ``v4_conviction`` is in the allowed tier set."""

    def __init__(self, allowed: list[str]):
        # Normalise to upper-case for case-insensitive matching.
        self._allowed = {str(t).upper() for t in allowed}

    @property
    def name(self) -> str:
        return "conviction_gate"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        tier = surface.v4_conviction
        if tier is None:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=f"v4_conviction is None; allowed={sorted(self._allowed)}",
            )
        if str(tier).upper() in self._allowed:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason=f"conviction={tier} in allowed={sorted(self._allowed)}",
                data={"conviction": tier},
            )
        return GateResult(
            passed=False,
            gate_name=self.name,
            reason=(
                f"conviction={tier} not in allowed={sorted(self._allowed)}"
            ),
            data={"conviction": tier},
        )
