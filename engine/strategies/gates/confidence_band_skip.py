"""ConfidenceBandSkipGate -- skip when model probability is in the no-trade band.

Data-backed by sweep 4 (`docs/data-backed-strategy-plan-2026-05-02/sweeps/04_conviction_*_btc_5m.tsv`):

For every LGB model on BTC 5m, there's a "noise zone" near 0.5 where
WR-if-bet-direction is at or below coin-flip. The clean tradeable signal
lives outside that band:

* v9   — trade outside [0.40, 0.65]
* v10  — trade outside [0.30, 0.70]
* v12  — trade outside [0.40, 0.70]
* classifier — trade outside [0.45, 0.55] (post-isotonic; pre-iso had
  a wider noise zone)

This gate is the "skip the middle" complement to ConfidenceGate's
``min_dist`` (which gates on distance from 0.5 in BOTH directions
symmetrically). ConfidenceBandSkip lets you specify an asymmetric or
explicit no-trade band tied to a specific model's empirical noise zone.

When the configured model's probability is None (not populated), the
gate FAILS with reason "model not populated" — strategies with this
gate require the model to be alive.

YAML usage
----------
::

    gates:
      - type: confidence_band_skip
        params:
          model: v12
          skip_band: [0.30, 0.70]    # skip if 0.30 <= p_v12 <= 0.70

Compared to ConfidenceGate(min_dist=0.20) which gates on |p-0.5|>=0.20
(symmetric: p<=0.30 or p>=0.70), confidence_band_skip lets you express
the same intent more directly when it's tied to a specific model and
asymmetric bands are wanted later.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from strategies.gates.base import Gate, GateResult
from strategies.gates.multi_model_consensus import _MODEL_FIELD_MAP

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


class ConfidenceBandSkipGate(Gate):
    """Pass when model probability is OUTSIDE skip_band."""

    def __init__(self, model: str, skip_band: list[float]):
        if model not in _MODEL_FIELD_MAP:
            raise ValueError(
                f"confidence_band_skip: unknown model={model!r}. "
                f"Known: {sorted(_MODEL_FIELD_MAP)}"
            )
        if not isinstance(skip_band, (list, tuple)) or len(skip_band) != 2:
            raise ValueError(
                f"confidence_band_skip: skip_band must be [lo, hi] — got {skip_band!r}"
            )
        lo, hi = float(skip_band[0]), float(skip_band[1])
        if not (0.0 <= lo < hi <= 1.0):
            raise ValueError(
                f"confidence_band_skip: skip_band [{lo}, {hi}] must satisfy 0 <= lo < hi <= 1"
            )
        self._model = model
        self._lo = lo
        self._hi = hi

    @property
    def name(self) -> str:
        return "confidence_band_skip"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        p = getattr(surface, _MODEL_FIELD_MAP[self._model], None)
        if p is None:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=f"{self._model} probability not populated",
                data={"model": self._model, "skip_band": [self._lo, self._hi]},
            )

        data = {
            "model": self._model,
            "p": p,
            "skip_band": [self._lo, self._hi],
        }

        if self._lo <= p <= self._hi:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=f"{self._model}={p:.3f} in skip_band [{self._lo}, {self._hi}]",
                data=data,
            )
        return GateResult(
            passed=True,
            gate_name=self.name,
            reason=f"{self._model}={p:.3f} outside skip_band [{self._lo}, {self._hi}]",
            data=data,
        )
