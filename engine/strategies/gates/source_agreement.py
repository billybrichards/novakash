"""SourceAgreementGate -- checks that multiple price sources agree on direction."""

from __future__ import annotations

from typing import TYPE_CHECKING

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


class SourceAgreementGate(Gate):
    """Pass if at least min_sources price deltas agree on direction.

    When spot_only=True, only uses spot price sources (Tiingo, Chainlink, Binance).
    """

    def __init__(self, min_sources: int = 2, spot_only: bool = False):
        self._min_sources = min_sources
        self._spot_only = spot_only

    @property
    def name(self) -> str:
        return "source_agreement"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        sources = {}
        if surface.delta_tiingo is not None:
            sources["tiingo"] = surface.delta_tiingo
        if surface.delta_chainlink is not None:
            sources["chainlink"] = surface.delta_chainlink
        if surface.delta_binance is not None:
            sources["binance"] = surface.delta_binance

        if len(sources) < self._min_sources:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=f"only {len(sources)} sources available, need {self._min_sources}",
                data={"available_sources": list(sources.keys())},
            )

        # Count direction agreement
        up_count = sum(1 for v in sources.values() if v > 0)
        down_count = sum(1 for v in sources.values() if v < 0)
        max_agreement = max(up_count, down_count)
        direction = "UP" if up_count >= down_count else "DOWN"

        if max_agreement >= self._min_sources:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason=f"{max_agreement}/{len(sources)} sources agree on {direction}",
                data={
                    "agreement": max_agreement,
                    "direction": direction,
                    "sources": sources,
                },
            )

        return GateResult(
            passed=False,
            gate_name=self.name,
            reason=f"only {max_agreement}/{len(sources)} agree, need {self._min_sources}",
            data={"sources": sources},
        )
