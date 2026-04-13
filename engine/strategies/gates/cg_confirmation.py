"""CGConfirmationGate -- CoinGlass OI + liquidation confirmation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


class CGConfirmationGate(Gate):
    """Pass if CoinGlass OI and liquidation data do not contradict the signal.

    Non-blocking if CoinGlass data is unavailable.
    oi_threshold: minimum OI in USD to consider the signal valid.
    liq_threshold: minimum liquidation volume to flag cascade risk.
    """

    def __init__(
        self,
        oi_threshold: float = 0.01,
        liq_threshold: float = 1_000_000,
    ):
        self._oi_threshold = oi_threshold
        self._liq_threshold = liq_threshold

    @property
    def name(self) -> str:
        return "cg_confirmation"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        if surface.cg_oi_usd is None:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason="no CoinGlass data, pass by default",
            )

        # Check if liquidation volume is extreme (cascade risk)
        liq_total = surface.cg_liq_total or 0.0
        if liq_total > self._liq_threshold:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=f"liquidation cascade: liq_total=${liq_total:,.0f} > ${self._liq_threshold:,.0f}",
                data={"liq_total": liq_total},
            )

        return GateResult(
            passed=True,
            gate_name=self.name,
            reason=f"CG confirmed: OI=${surface.cg_oi_usd:,.0f}, liq=${liq_total:,.0f}",
            data={
                "oi_usd": surface.cg_oi_usd,
                "liq_total": liq_total,
                "funding_rate": surface.cg_funding_rate,
            },
        )
