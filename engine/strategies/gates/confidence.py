"""ConfidenceGate -- checks probability distance from 0.5."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


class ConfidenceGate(Gate):
    """Pass if |probability - 0.5| >= min_dist (and <= max_dist if set).

    Conviction source selection (YAML ``params.source``):
      * ``"poly"`` (default, legacy) — read ``poly_confidence_distance`` and
        fall back to ``v2_probability_up`` when poly is unavailable. Matches
        the pre-2026-05-19 behaviour exactly.
      * ``"classifier"`` — read ``probability_classifier`` (TimesFM Path1
        head). Required by the 15m classifier strategies (Hub #546). The
        15m payload on /v4/snapshot does not ship the polymarket
        recommended_outcome block, so the legacy poly-first path always
        returned ``no confidence distance available`` — silently blocking
        the strategy from EVER firing despite 5,632 ticks of data emitted
        to strategy_decisions.metadata_json.
    """

    def __init__(
        self,
        min_dist: float,
        max_dist: Optional[float] = None,
        source: str = "poly",
    ):
        self._min = min_dist
        self._max = max_dist
        if source not in ("poly", "classifier"):
            raise ValueError(
                f"ConfidenceGate source must be 'poly' or 'classifier', got {source!r}"
            )
        self._source = source

    @property
    def name(self) -> str:
        return "confidence"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        if self._source == "classifier":
            # Hub #546 — 15m classifier strategies read the Path1 classifier
            # head directly. Poly block is not present on 15m snapshots.
            prob = getattr(surface, "probability_classifier", None)
            dist = abs(prob - 0.5) if prob is not None else None
            unavailable_reason = "probability_classifier not available"
        else:
            # Legacy poly-first behaviour (pre-#546, unchanged).
            dist = surface.poly_confidence_distance
            if dist is None and surface.v2_probability_up is not None:
                dist = abs(surface.v2_probability_up - 0.5)
            unavailable_reason = "no confidence distance available"

        if dist is None:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=unavailable_reason,
            )

        if dist < self._min:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=f"dist={dist:.3f} < min={self._min:.2f}",
                data={"distance": dist, "source": self._source},
            )

        if self._max is not None and dist > self._max:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=f"dist={dist:.3f} > max={self._max:.2f}",
                data={"distance": dist, "source": self._source},
            )

        return GateResult(
            passed=True,
            gate_name=self.name,
            reason=f"dist={dist:.3f} in [{self._min:.2f}, {f'{self._max:.2f}' if self._max is not None else 'inf'}]",
            data={"distance": dist, "source": self._source},
        )
