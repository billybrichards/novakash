"""ConsecutivePassTicksGate -- requires N consecutive evals with same direction.

Adds noise-robustness to declarative-gate strategies (v15m_up_basic etc) that
lack inline-hook tick-confirmation logic. Mirrors v9_ensemble's
``min_consecutive_pass_ticks`` and v8_champion_lgb_only's ``check_confirmation``
behaviour, but as a reusable Gate so any YAML strategy can opt in.

The gate must be the LAST gate in the strategy's pipeline -- it relies on the
short-circuit semantics of registry._evaluate_one (returns SKIP at first
failing gate). When this gate is invoked, all upstream gates have passed for
that tick.

Counter logic
-------------
Per-instance state, keyed by ``window_ts``:

* First eval for a (window_ts, direction): ``count = 1`` (gate fails -- need
  >= min_ticks).
* Subsequent eval, same window_ts AND same direction AND time-gap from last
  eval <= ``MAX_GAP_SECONDS``: ``count += 1``.
* Direction change OR gap > MAX_GAP_SECONDS: reset ``count = 1``.
* ``count >= min_ticks``: gate passes.

The gap check is what makes this *robust to upstream gate failures*. When an
upstream gate fails (e.g. direction flips, confidence dips), this gate is
NOT invoked, but the counter is preserved. When upstream gates pass again,
the gap check detects the discontinuity (>5s = ~2-3 missed ticks) and resets,
ensuring the count truly reflects N CONSECUTIVE passes, not N intermittent
ones.

State scoping
-------------
Counter is namespaced by ``id(self)`` -- each strategy registers its own gate
instance, so two different strategies with the same YAML config get distinct
counters. No cross-strategy interference.

Periodic cleanup
----------------
When state grows past 50 entries (which would represent ~50 distinct windows
in flight, a pathological state), entries older than ~2 windows are dropped.
Bounded memory regardless of uptime.

YAML usage
----------
::

    gates:
      ...
      - type: consecutive_pass_ticks
        params: { min_ticks: 12 }    # 12 x 2s = 24s sustained signal

Choosing ``min_ticks``
----------------------
* 5m strategies (v8_champion_lgb_only, v9_ensemble) use ``3`` ticks (= 6s)
  because their entry window is short (~150-180s) and a 6s confirmation is a
  meaningful slice of it.
* 15m strategies have a ~360s entry band (T-540 to T-180), so a longer
  confirmation can be afforded. ``12`` ticks (= 24s) is the recommended
  starting point: long enough to filter single-tick blips in
  ``probability_up`` from the LGB blend, short enough that we don't miss
  the entry window.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


# If the gap between two evaluations of this gate is greater than this,
# the counter resets. The engine evaluates strategies every ~2s, so 5s
# corresponds to ~2-3 missed ticks (i.e. an upstream gate failed for that
# duration). This keeps the "consecutive" semantic strict.
_MAX_GAP_SECONDS: float = 5.0

# Cleanup threshold -- once state grows past this many windows, drop the
# stale entries. 50 distinct windows in flight is already pathological
# (a 15m window resolves every 900s, so > 30min of stale state).
_CLEANUP_AT_SIZE: int = 50

# When cleaning up, drop windows older than this (in seconds) relative to
# the most-recently-seen window_ts. Two 15m windows = 1800s.
_STALE_WINDOW_AGE_S: int = 2 * 900


class ConsecutivePassTicksGate(Gate):
    """Pass when N consecutive evaluations have all upstream gates passing
    AND the same direction."""

    def __init__(self, min_ticks: int):
        if not isinstance(min_ticks, int) or min_ticks < 1:
            raise ValueError(
                "ConsecutivePassTicksGate: min_ticks must be a positive int, "
                f"got {min_ticks!r}"
            )
        self._min_ticks: int = min_ticks
        # state[window_ts] = (count, direction, last_eval_at)
        self._state: dict[int, tuple[int, str, float]] = {}

    @property
    def name(self) -> str:
        return "consecutive_pass_ticks"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        # Direction comes from the surface. For UP/DOWN-only strategies the
        # direction gate would have already filtered to a single direction
        # before this gate runs. For direction-agnostic strategies, the
        # consecutive logic still works -- we count consecutive passes
        # within the same direction and reset on flip.
        #
        # Hub #546 regression fix (2026-05-25): 15m /v4/snapshot payloads do
        # not include the polymarket_live_recommended_outcome block, so
        # poly_direction is always None for 15m classifier strategies.  Fall
        # back to deriving direction from probability_classifier: prob > 0.5 →
        # UP, prob < 0.5 → DOWN.  This unblocks v_eth/v_xrp × top10/top20
        # from the consecutive-tick gate which was the last barrier to firing
        # after the ConfidenceGate source=classifier fix in PR #558.
        direction = getattr(surface, "poly_direction", None)
        if direction not in ("UP", "DOWN"):
            p_cls = getattr(surface, "probability_classifier", None)
            if p_cls is not None and p_cls != 0.5:
                direction = "UP" if p_cls > 0.5 else "DOWN"
        if direction not in ("UP", "DOWN"):
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=f"poly_direction={getattr(surface, 'poly_direction', None)!r} not actionable and probability_classifier unavailable",
            )

        window_ts = getattr(surface, "window_ts", None)
        if not window_ts:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason="window_ts missing or zero",
            )

        now = time.time()
        prev = self._state.get(window_ts)
        if (
            prev is not None
            and prev[1] == direction
            and (now - prev[2]) <= _MAX_GAP_SECONDS
        ):
            count = prev[0] + 1
        else:
            # First eval for this (window_ts, direction), OR direction
            # changed, OR gap too large (= upstream gate failed in between).
            count = 1
        self._state[window_ts] = (count, direction, now)

        # Bounded-memory cleanup: drop entries older than 2 windows.
        if len(self._state) > _CLEANUP_AT_SIZE:
            cutoff = window_ts - _STALE_WINDOW_AGE_S
            self._state = {
                k: v for k, v in self._state.items() if k >= cutoff
            }

        if count >= self._min_ticks:
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason=(
                    f"{count}/{self._min_ticks} consecutive pass ticks "
                    f"confirmed (direction={direction})"
                ),
                data={
                    "count": count,
                    "min_ticks": self._min_ticks,
                    "direction": direction,
                },
            )
        return GateResult(
            passed=False,
            gate_name=self.name,
            reason=(
                f"{count}/{self._min_ticks} consecutive pass ticks "
                f"(direction={direction}, need more)"
            ),
            data={
                "count": count,
                "min_ticks": self._min_ticks,
                "direction": direction,
            },
        )
