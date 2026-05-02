"""ModelDisagreementVetoGate -- block fire when two models strongly disagree.

Data-backed by sweep 6 (`docs/data-backed-strategy-plan-2026-05-02/sweeps/06_disagree_v12_v9_btc_5m.tsv`):

When ``|p_v12 - p_v9|`` is small (<0.10) the two models agree strongly; WR is
~62-78% in their consensus direction. As disagreement grows past 0.10 the
WR diverges:

* |diff| in [0.10, 0.15]: follow v9 → 90% WR; follow v12 → 83%
* |diff| in [0.15, 0.20]: follow v9 → 71%; follow v12 → 64%
* |diff| ≥ 0.20: follow v9 → 74%; follow v12 → 82% (v12 wins at extremes)

The veto pattern: when a strategy is anchored on v9 (or v9.1), any moderate
v12 disagreement (|diff| ≥ 0.20 by default) signals regime drift that v12
sees first. The data says we should NOT follow v9 in that zone — better to
SKIP than fire on v9's stale view.

Conversely if a strategy were anchored on v12, the same logic would say
"skip when |v9 - v12| ≥ 0.20" because at extreme disagreement v12 still
wins on average — the veto's role is to filter the moderate disagreement
zone where the anchor is wrong.

Default threshold (max_abs_diff = 0.20) is the data-backed cutoff.

Behaviour when models unavailable
----------------------------------
If either probability is None (model not loaded, asset not scored), the
gate PASSES with reason "models not both populated". This avoids
spurious skips during box warmup or when scoring an asset that doesn't
have all models. The strategy's other gates are responsible for ensuring
the primary model is actually populated.

YAML usage
----------
::

    gates:
      - type: model_disagreement_veto
        params:
          primary: v9                  # the model the strategy trusts
          secondary: v12               # the model whose disagreement vetos
          max_abs_diff: 0.20

Both ``primary`` and ``secondary`` accept the same names as
``multi_model_consensus``: v9, v9_1, v10, v12, classifier.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from strategies.gates.base import Gate, GateResult
from strategies.gates.multi_model_consensus import _MODEL_FIELD_MAP

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


class ModelDisagreementVetoGate(Gate):
    """Pass unless ``|p_secondary - p_primary| >= max_abs_diff``."""

    def __init__(self, primary: str, secondary: str, max_abs_diff: float = 0.20):
        if primary not in _MODEL_FIELD_MAP:
            raise ValueError(
                f"model_disagreement_veto: unknown primary={primary!r}. "
                f"Known: {sorted(_MODEL_FIELD_MAP)}"
            )
        if secondary not in _MODEL_FIELD_MAP:
            raise ValueError(
                f"model_disagreement_veto: unknown secondary={secondary!r}. "
                f"Known: {sorted(_MODEL_FIELD_MAP)}"
            )
        if primary == secondary:
            raise ValueError(
                f"model_disagreement_veto: primary and secondary must differ ({primary!r})"
            )
        if not (0.0 < max_abs_diff <= 1.0):
            raise ValueError(
                f"model_disagreement_veto: max_abs_diff={max_abs_diff} must be in (0, 1]"
            )
        self._primary = primary
        self._secondary = secondary
        self._max_diff = float(max_abs_diff)

    @property
    def name(self) -> str:
        return "model_disagreement_veto"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        p_primary = getattr(surface, _MODEL_FIELD_MAP[self._primary], None)
        p_secondary = getattr(surface, _MODEL_FIELD_MAP[self._secondary], None)

        if p_primary is None or p_secondary is None:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason=(
                    f"models not both populated "
                    f"({self._primary}={p_primary}, {self._secondary}={p_secondary})"
                ),
                data={
                    "primary": self._primary,
                    "secondary": self._secondary,
                    "p_primary": p_primary,
                    "p_secondary": p_secondary,
                },
            )

        diff = abs(p_secondary - p_primary)
        data = {
            "primary": self._primary,
            "secondary": self._secondary,
            "p_primary": p_primary,
            "p_secondary": p_secondary,
            "abs_diff": diff,
            "max_abs_diff": self._max_diff,
        }

        # Tolerance for float-equality at the threshold boundary.  Without
        # this, ``0.70 - 0.50`` evaluates to ``0.19999999999999996`` and
        # would slip past a strict ``diff >= 0.20`` veto check.
        EPS = 1e-9
        if diff + EPS >= self._max_diff:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=(
                    f"|{self._secondary}-{self._primary}|="
                    f"{diff:.3f} >= max_abs_diff={self._max_diff}"
                ),
                data=data,
            )
        return GateResult(
            passed=True,
            gate_name=self.name,
            reason=(
                f"|{self._secondary}-{self._primary}|="
                f"{diff:.3f} < {self._max_diff}"
            ),
            data=data,
        )
