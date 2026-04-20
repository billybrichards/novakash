"""SourceAgreementGate -- checks that multiple price sources agree on direction.

Extended 2026-04-20 (v4_down_only v2.3.0): optional ``require_chainlink_null_block``
and ``require_tiingo_null_block`` flags. When either is True, the gate fails if
the corresponding delta source is None (a null-block pattern — distinct from
direction agreement). Use these when a strategy wants to veto on feed-health
issues without requiring the legacy min_sources direction-agreement check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


class SourceAgreementGate(Gate):
    """Pass if at least min_sources price deltas agree on direction.

    When spot_only=True, only uses spot price sources (Tiingo, Chainlink, Binance).

    Null-block flags (v2.3.0):
      * require_chainlink_null_block: fail if surface.delta_chainlink is None.
      * require_tiingo_null_block: fail if surface.delta_tiingo is None.
        These run BEFORE the direction-agreement check. They block on feed
        health, not on direction opinion — the classic v4/v5 pattern for
        "at least confirm the oracles are speaking".
    """

    def __init__(
        self,
        min_sources: int = 2,
        spot_only: bool = False,
        require_chainlink_null_block: bool = False,
        require_tiingo_null_block: bool = False,
    ):
        self._min_sources = min_sources
        self._spot_only = spot_only
        self._require_chainlink_null_block = bool(require_chainlink_null_block)
        self._require_tiingo_null_block = bool(require_tiingo_null_block)

    @property
    def name(self) -> str:
        return "source_agreement"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        # v2.3.0: null-block feed-health checks run first.
        if (
            self._require_chainlink_null_block
            and surface.delta_chainlink is None
        ):
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason="chainlink delta is None (null-block)",
                data={"null_block": "chainlink"},
            )
        if (
            self._require_tiingo_null_block
            and surface.delta_tiingo is None
        ):
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason="tiingo delta is None (null-block)",
                data={"null_block": "tiingo"},
            )

        # v2.3.0: when min_sources=0, skip the direction-agreement check
        # entirely — useful when a strategy only wants the null-block
        # feed-health gate without forcing oracle direction alignment.
        if self._min_sources <= 0:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason="null-block checks passed; direction check disabled (min_sources=0)",
            )

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
