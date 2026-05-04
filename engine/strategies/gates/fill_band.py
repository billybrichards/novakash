"""FillBandGate -- enforce a CLOB ask floor / ceiling per direction.

Mirrors ``v9_ensemble.py`` step 13 (``fill_band``) and step 14 (UP / DOWN
fill floors). The strict gate-list strategies were missing this guard,
which contributed to the 2026-05-04 ~10:55 UTC regime-flip loss: there
was nothing to keep them out of fill prices outside the ``[0.18, 0.82]``
historical-PnL band.

For a given direction the gate looks up the corresponding CLOB ask
(``clob_up_ask`` for UP, ``clob_down_ask`` for DOWN) and fails when:

  * the ask is None and ``require_clob`` is True (default),
  * the ask is below ``min_fill_price``, or
  * the ask is above ``max_fill_price``.

Falls back to ``surface.poly_max_entry_price`` when the CLOB ask is
None and ``require_clob`` is False (matches v9_ensemble behaviour).

YAML usage::

    - type: fill_band
      params:
        direction: UP
        min_fill_price: 0.20
        max_fill_price: 0.82

For DOWN strategies::

    - type: fill_band
      params:
        direction: DOWN
        min_fill_price: 0.15
        max_fill_price: 0.82
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


_VALID_DIRECTIONS = ("UP", "DOWN")


class FillBandGate(Gate):
    """CLOB-ask band gate: floor + ceiling per direction.

    Defaults match the v9_ensemble production knobs at the time this
    gate was extracted (2026-05-04):
      * ``min_fill_price`` for UP defaults to 0.20 (legacy ``up_min_fill_price``)
      * ``min_fill_price`` for DOWN defaults to 0.15 (legacy ``down_min_fill_price``)
      * ``max_fill_price`` defaults to 0.82 (legacy ``fill_band_max``)

    The defaults are overridable per-strategy in YAML so different risk
    appetites can tighten or loosen the band without touching code.
    """

    _DIRECTION_DEFAULTS_MIN = {"UP": 0.20, "DOWN": 0.15}
    _DEFAULT_MAX = 0.82

    def __init__(
        self,
        direction: str,
        min_fill_price: Optional[float] = None,
        max_fill_price: Optional[float] = None,
        require_clob: bool = True,
    ):
        if direction not in _VALID_DIRECTIONS:
            raise ValueError(
                f"FillBandGate: direction={direction!r} must be one of "
                f"{_VALID_DIRECTIONS}"
            )
        self._direction = direction

        floor = (
            float(min_fill_price)
            if min_fill_price is not None
            else self._DIRECTION_DEFAULTS_MIN[direction]
        )
        ceiling = (
            float(max_fill_price)
            if max_fill_price is not None
            else self._DEFAULT_MAX
        )

        if not (0.0 <= floor < ceiling <= 1.0):
            raise ValueError(
                f"FillBandGate: require 0 <= min_fill_price ({floor}) < "
                f"max_fill_price ({ceiling}) <= 1.0"
            )

        self._min = floor
        self._max = ceiling
        self._require_clob = bool(require_clob)

    @property
    def name(self) -> str:
        return "fill_band"

    def _ask_for_direction(self, surface: "FullDataSurface") -> tuple[Optional[float], str]:
        """Return (price, source) — source ∈ {"clob","poly_max_entry","none"}."""
        attr = "clob_up_ask" if self._direction == "UP" else "clob_down_ask"
        clob_ask = getattr(surface, attr, None)
        if clob_ask is not None:
            return float(clob_ask), "clob"
        if not self._require_clob:
            poly = getattr(surface, "poly_max_entry_price", None)
            if poly is not None:
                return float(poly), "poly_max_entry"
        return None, "none"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        price, source = self._ask_for_direction(surface)

        if price is None:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=(
                    f"no fill price for {self._direction} "
                    f"(clob ask None, require_clob={self._require_clob})"
                ),
                data={
                    "direction": self._direction,
                    "min": self._min,
                    "max": self._max,
                    "source": source,
                },
            )

        if price < self._min:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=(
                    f"{self._direction} fill={price:.3f} < floor {self._min:.2f}"
                ),
                data={
                    "direction": self._direction,
                    "fill_price": price,
                    "min": self._min,
                    "max": self._max,
                    "source": source,
                },
            )

        if price > self._max:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=(
                    f"{self._direction} fill={price:.3f} > ceiling {self._max:.2f}"
                ),
                data={
                    "direction": self._direction,
                    "fill_price": price,
                    "min": self._min,
                    "max": self._max,
                    "source": source,
                },
            )

        return GateResult(
            passed=True,
            gate_name=self.name,
            reason=(
                f"{self._direction} fill={price:.3f} in "
                f"[{self._min:.2f},{self._max:.2f}]"
            ),
            data={
                "direction": self._direction,
                "fill_price": price,
                "min": self._min,
                "max": self._max,
                "source": source,
            },
        )
