"""OracleDirectionGate -- enforce that named price oracles agree with the
trade direction.

Closes the safety gap surfaced by the 2026-05-04 ~10:55 UTC regime flip:
chainlink and tiingo deltas disagreed on direction, the dict-style v9_ensemble
``skip_on_oracle_disagree`` check correctly skipped, but the strict
gate-list strategies (``v_v12_extreme_*``, ``v_v9_1_strong_*``, …) had no
equivalent guard and fired into the disagreement (real-money loss).

Mirrors ``v9_ensemble.py`` step 12 (``oracle_direction``):
  * fail when ``surface.delta_chainlink`` sign disagrees with ``direction``,
  * fail when ``surface.delta_tiingo`` sign disagrees with ``direction``,
  * pass when both oracles align with the strategy's intended trade
    direction.

This gate is direction-anchored at construction time — the YAML must set
``params.direction: UP`` or ``params.direction: DOWN``. That keeps the
contract identical to the surrounding ``direction`` gate in the strict
yamls and avoids any ambiguity about "which direction are we comparing?".

Optional knobs:
  * ``require_both`` (default True): when False, only require that
    *available* oracles agree (still fails on disagreement). When True
    (default), fail on null deltas — same null-block semantics as
    ``SourceAgreementGate.require_*_null_block``.
  * ``zero_is_pass`` (default True): treat exactly-zero delta as
    agreement. v9_ensemble's check uses ``> 0``/``< 0`` only, so ``0.0``
    counts as DOWN there; we keep parity by defaulting to True (any sign
    matches when the delta is exactly zero) which is slightly more
    permissive but avoids spurious skips on cold-start surfaces where
    deltas haven't ticked yet — the timing/consecutive_pass_ticks gates
    already catch those windows separately.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


_VALID_DIRECTIONS = ("UP", "DOWN")


def _delta_dir(delta: float | None, zero_is_pass: bool, target: str) -> str | None:
    """Return ``"UP"``/``"DOWN"`` for a signed delta, or ``None`` when missing.

    When ``zero_is_pass`` is True, a delta of exactly 0.0 returns
    ``target`` (i.e. matches whichever direction was asked for). This
    avoids penalising mid-tick zero-crossings during quiet periods.
    """
    if delta is None:
        return None
    if delta > 0:
        return "UP"
    if delta < 0:
        return "DOWN"
    return target if zero_is_pass else "DOWN"


class OracleDirectionGate(Gate):
    """Require that chainlink and tiingo oracles agree with ``direction``.

    Pass conditions (with ``require_both=True``, the default):
      1. ``surface.delta_chainlink`` is not None.
      2. ``surface.delta_tiingo`` is not None.
      3. ``sign(delta_chainlink)`` matches ``direction``.
      4. ``sign(delta_tiingo)`` matches ``direction``.

    With ``require_both=False`` only (3) and (4) are enforced, and only
    over whichever oracles are populated — but if BOTH are populated and
    one disagrees, the gate still fails (we never tolerate active
    disagreement).
    """

    def __init__(
        self,
        direction: str,
        require_both: bool = True,
        zero_is_pass: bool = True,
    ):
        if direction not in _VALID_DIRECTIONS:
            raise ValueError(
                f"OracleDirectionGate: direction={direction!r} must be "
                f"one of {_VALID_DIRECTIONS}"
            )
        self._direction = direction
        self._require_both = bool(require_both)
        self._zero_is_pass = bool(zero_is_pass)

    @property
    def name(self) -> str:
        return "oracle_direction"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        cl = getattr(surface, "delta_chainlink", None)
        ti = getattr(surface, "delta_tiingo", None)

        # Null-block: both feeds must be live in strict mode.
        if self._require_both:
            missing = []
            if cl is None:
                missing.append("chainlink")
            if ti is None:
                missing.append("tiingo")
            if missing:
                return GateResult(
                    passed=False,
                    gate_name=self.name,
                    reason=f"oracle delta None for {','.join(missing)}",
                    data={
                        "missing": missing,
                        "delta_chainlink": cl,
                        "delta_tiingo": ti,
                        "direction": self._direction,
                    },
                )

        cl_dir = _delta_dir(cl, self._zero_is_pass, self._direction)
        ti_dir = _delta_dir(ti, self._zero_is_pass, self._direction)

        # Active-disagreement check: whenever a delta is populated, its
        # sign must match the strategy direction. A populated delta with
        # the WRONG sign is always a fail, regardless of require_both.
        disagreers = []
        if cl_dir is not None and cl_dir != self._direction:
            disagreers.append(("chainlink", cl, cl_dir))
        if ti_dir is not None and ti_dir != self._direction:
            disagreers.append(("tiingo", ti, ti_dir))

        if disagreers:
            details = ", ".join(
                f"{src}={d:+.6f}({sign})" for src, d, sign in disagreers
            )
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=(
                    f"oracle disagrees with {self._direction}: {details}"
                ),
                data={
                    "direction": self._direction,
                    "delta_chainlink": cl,
                    "delta_tiingo": ti,
                    "chainlink_dir": cl_dir,
                    "tiingo_dir": ti_dir,
                    "disagreers": [src for src, _, _ in disagreers],
                },
            )

        return GateResult(
            passed=True,
            gate_name=self.name,
            reason=(
                f"oracles agree with {self._direction} "
                f"(cl={cl_dir}, ti={ti_dir})"
            ),
            data={
                "direction": self._direction,
                "delta_chainlink": cl,
                "delta_tiingo": ti,
                "chainlink_dir": cl_dir,
                "tiingo_dir": ti_dir,
            },
        )
