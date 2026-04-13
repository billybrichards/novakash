"""TakerFlowGate -- checks CoinGlass taker buy/sell flow alignment."""

from __future__ import annotations

from typing import TYPE_CHECKING

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


class TakerFlowGate(Gate):
    """Pass if taker flow aligns with the predicted direction.

    For UP predictions: taker_buy_vol > taker_sell_vol.
    For DOWN predictions: taker_sell_vol > taker_buy_vol.
    If no CoinGlass data, passes (non-blocking).
    """

    @property
    def name(self) -> str:
        return "taker_flow"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        buy_vol = surface.cg_taker_buy_vol
        sell_vol = surface.cg_taker_sell_vol

        if buy_vol is None or sell_vol is None:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason="no CoinGlass taker data, pass by default",
            )

        # Determine predicted direction
        direction = surface.poly_direction
        if direction is None and surface.v2_probability_up is not None:
            direction = "UP" if surface.v2_probability_up > 0.5 else "DOWN"

        if direction is None:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason="no direction to check against, pass by default",
            )

        total = buy_vol + sell_vol
        if total == 0:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason="zero taker volume, pass by default",
            )

        buy_ratio = buy_vol / total

        if direction == "UP" and buy_ratio > 0.5:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason=f"UP: buy_ratio={buy_ratio:.3f} > 0.5",
                data={"buy_ratio": buy_ratio, "direction": direction},
            )
        if direction == "DOWN" and buy_ratio < 0.5:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason=f"DOWN: buy_ratio={buy_ratio:.3f} < 0.5 (sell dominant)",
                data={"buy_ratio": buy_ratio, "direction": direction},
            )

        return GateResult(
            passed=False,
            gate_name=self.name,
            reason=f"taker flow misaligned: {direction} but buy_ratio={buy_ratio:.3f}",
            data={"buy_ratio": buy_ratio, "direction": direction},
        )
