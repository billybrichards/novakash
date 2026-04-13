"""TradeAdvisedGate -- checks V4 polymarket trade_advised flag."""

from __future__ import annotations

from typing import TYPE_CHECKING

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


class TradeAdvisedGate(Gate):
    """Pass if poly_trade_advised is True.

    If poly_trade_advised is None (no V4 data), fails the gate.
    """

    @property
    def name(self) -> str:
        return "trade_advised"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        advised = surface.poly_trade_advised
        if advised is None:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason="poly_trade_advised is None (no V4 polymarket data)",
            )

        if advised:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason="trade_advised=True",
            )

        reason = surface.poly_reason or "trade not advised"
        return GateResult(
            passed=False,
            gate_name=self.name,
            reason=f"trade_advised=False: {reason}",
            data={"poly_reason": reason},
        )
