"""SpreadGate -- checks CLOB spread is reasonable."""

from __future__ import annotations

from typing import TYPE_CHECKING

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


class SpreadGate(Gate):
    """Pass if the CLOB spread is below max_spread_bps.

    Calculates spread from clob_up_bid/ask or clob_down_bid/ask
    depending on the predicted direction.
    If no CLOB data, passes (non-blocking).
    """

    def __init__(self, max_spread_bps: float = 100):
        self._max_bps = max_spread_bps

    @property
    def name(self) -> str:
        return "spread"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        # Determine direction for spread check
        direction = surface.poly_direction
        if direction is None and surface.v2_probability_up is not None:
            direction = "UP" if surface.v2_probability_up > 0.5 else "DOWN"

        bid = None
        ask = None
        if direction == "UP":
            bid = surface.clob_up_bid
            ask = surface.clob_up_ask
        elif direction == "DOWN":
            bid = surface.clob_down_bid
            ask = surface.clob_down_ask

        if bid is None or ask is None:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason="no CLOB bid/ask, pass by default",
            )

        if bid == 0:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason="bid=0, no liquidity",
            )

        spread_bps = ((ask - bid) / bid) * 10000
        if spread_bps <= self._max_bps:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason=f"spread={spread_bps:.0f}bps <= {self._max_bps}bps",
                data={"spread_bps": spread_bps, "bid": bid, "ask": ask},
            )

        return GateResult(
            passed=False,
            gate_name=self.name,
            reason=f"spread={spread_bps:.0f}bps > {self._max_bps}bps",
            data={"spread_bps": spread_bps, "bid": bid, "ask": ask},
        )
