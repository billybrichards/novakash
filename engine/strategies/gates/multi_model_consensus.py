"""MultiModelConsensusGate -- N-of-M LGB+classifier agreement on direction.

Data-backed by sweep 11 (`docs/data-backed-strategy-plan-2026-05-02/sweeps/11_consensus_simulation_5m.tsv`):

* 3-of-5 UP-vote on BTC 5m windows where each `p_<model> >= 0.55` resolved
  UP at **80.7%** WR (n=135 in 14d).
* 4-of-5 DOWN-vote (each `p_<model> <= 0.45`) resolved DOWN at **91.7%** WR
  (n=24 in 14d, rare but strong).
* No-majority windows (≤2 votes either side) resolved 45.9% UP — coin flip.

Vote rule
---------
For each model in the configured list, the vote is computed against
``surface.probability_lgb`` (v9), ``probability_lgb_v9_1`` (v9.1),
``probability_lgb_v10`` (v10), ``probability_lgb_v12`` (v12), and
``probability_classifier`` (classifier head). The mapping from ``models``
list to surface fields is hardcoded below — explicit and auditable.

* UP vote: ``p >= vote_threshold_up`` (default 0.55)
* DOWN vote: ``p <= vote_threshold_dn`` (default 0.45)
* No vote: between the two thresholds (the no-trade middle band)

Gate semantics
--------------
* If ``direction == "UP"``: pass when up_votes >= min_votes.
* If ``direction == "DOWN"``: pass when down_votes >= min_votes.
* If ``direction is None``: pass when EITHER side reaches min_votes; the
  passing direction is captured in ``data["direction"]`` so the strategy
  pre-gate hook can route fill-side selection.

Models with ``None`` probability are SKIPPED in the count (no vote either
way). The ``available`` count tracks how many models contributed at all —
used in skip-reason text and in the failure path to distinguish "models
not loaded yet" from "models all disagreed".

YAML usage
----------
::

    gates:
      - type: multi_model_consensus
        params:
          models: [v9, v9_1, v10, v12, classifier]
          direction: UP                       # optional; None = either side
          min_votes: 3
          vote_threshold_up: 0.55             # default
          vote_threshold_dn: 0.45             # default; or set vote_threshold (symmetric)

Choosing thresholds
-------------------
The default 0.55 / 0.45 band is the data-backed cutoff from sweep 4
(per-model conviction sweeps): below 0.55 for UP and above 0.45 for DOWN
the per-model edge is < 5pp above coinflip. Tighten only if you want
fewer, higher-conviction fires.

Choosing min_votes
------------------
* min_votes=3 with 5 models: ~10/day on BTC 5m, ~80% WR
* min_votes=4 with 5 models: ~3/day, ~75-92% WR (DOWN side stronger)
* min_votes=5 with 5 models: ~0.5/day, very small sample
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


# Mapping from YAML model name to FullDataSurface attribute. Hardcoded to
# match the sister-repo's /v4/snapshot output naming. Adding a new model
# requires one line here AND the corresponding field in FullDataSurface.
_MODEL_FIELD_MAP: dict[str, str] = {
    "v9":         "probability_lgb",
    "v9_1":       "probability_lgb_v9_1",
    "v10":        "probability_lgb_v10",
    "v12":        "probability_lgb_v12",
    "classifier": "probability_classifier",
}


class MultiModelConsensusGate(Gate):
    """N-of-M model vote on direction with conviction band."""

    def __init__(
        self,
        models: list[str],
        min_votes: int = 3,
        direction: Optional[str] = None,
        vote_threshold: Optional[float] = None,
        vote_threshold_up: Optional[float] = None,
        vote_threshold_dn: Optional[float] = None,
    ):
        unknown = [m for m in models if m not in _MODEL_FIELD_MAP]
        if unknown:
            raise ValueError(
                f"multi_model_consensus: unknown model(s) {unknown}. "
                f"Known: {sorted(_MODEL_FIELD_MAP)}"
            )
        if min_votes < 1 or min_votes > len(models):
            raise ValueError(
                f"multi_model_consensus: min_votes={min_votes} must be in [1, {len(models)}]"
            )
        if direction is not None and direction not in ("UP", "DOWN"):
            raise ValueError(
                f"multi_model_consensus: direction must be 'UP', 'DOWN', or None — got {direction!r}"
            )

        # Symmetric threshold shorthand: vote_threshold=0.55 → up=0.55, dn=0.45.
        # Explicit asymmetric values win when both forms are provided.
        if vote_threshold_up is None:
            vote_threshold_up = vote_threshold if vote_threshold is not None else 0.55
        if vote_threshold_dn is None:
            vote_threshold_dn = (
                (1.0 - vote_threshold) if vote_threshold is not None else 0.45
            )

        if not (0.5 <= vote_threshold_up <= 1.0):
            raise ValueError(
                f"vote_threshold_up={vote_threshold_up} must be in [0.5, 1.0]"
            )
        if not (0.0 <= vote_threshold_dn <= 0.5):
            raise ValueError(
                f"vote_threshold_dn={vote_threshold_dn} must be in [0.0, 0.5]"
            )

        self._models = list(models)
        self._min_votes = int(min_votes)
        self._direction = direction
        self._thr_up = float(vote_threshold_up)
        self._thr_dn = float(vote_threshold_dn)

    @property
    def name(self) -> str:
        return "multi_model_consensus"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        votes_up = 0
        votes_dn = 0
        available = 0
        per_model: dict[str, Optional[float]] = {}

        for m in self._models:
            field = _MODEL_FIELD_MAP[m]
            p = getattr(surface, field, None)
            per_model[m] = p
            if p is None:
                continue
            available += 1
            if p >= self._thr_up:
                votes_up += 1
            elif p <= self._thr_dn:
                votes_dn += 1

        data = {
            "votes_up": votes_up,
            "votes_dn": votes_dn,
            "available": available,
            "models_total": len(self._models),
            "thresholds": {"up": self._thr_up, "dn": self._thr_dn},
            "per_model": per_model,
        }

        # Direction-specific pass logic
        if self._direction == "UP":
            if votes_up >= self._min_votes:
                return GateResult(
                    passed=True,
                    gate_name=self.name,
                    reason=f"UP {votes_up}/{available} >= {self._min_votes}",
                    data=data,
                )
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=(
                    f"UP {votes_up}/{available} < {self._min_votes} "
                    f"(thresholds up>={self._thr_up} dn<={self._thr_dn})"
                ),
                data=data,
            )

        if self._direction == "DOWN":
            if votes_dn >= self._min_votes:
                return GateResult(
                    passed=True,
                    gate_name=self.name,
                    reason=f"DOWN {votes_dn}/{available} >= {self._min_votes}",
                    data=data,
                )
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=(
                    f"DOWN {votes_dn}/{available} < {self._min_votes} "
                    f"(thresholds up>={self._thr_up} dn<={self._thr_dn})"
                ),
                data=data,
            )

        # Either-side mode: whichever side reaches min_votes first wins. If
        # both sides reach the threshold (rare — would require two opposing
        # majorities, only possible with an even split-ish), prefer the
        # larger count; on tie, prefer UP (matches surface.probability_up's
        # default direction interpretation).
        if votes_up >= self._min_votes and votes_up >= votes_dn:
            data["direction"] = "UP"
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason=f"UP {votes_up}/{available} (either-side mode)",
                data=data,
            )
        if votes_dn >= self._min_votes:
            data["direction"] = "DOWN"
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason=f"DOWN {votes_dn}/{available} (either-side mode)",
                data=data,
            )
        return GateResult(
            passed=False,
            gate_name=self.name,
            reason=(
                f"no majority (UP {votes_up}, DOWN {votes_dn}, "
                f"available {available}/{len(self._models)}, min_votes={self._min_votes})"
            ),
            data=data,
        )
