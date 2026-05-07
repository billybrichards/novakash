"""
VPIN rolling-window buffer (60s) — engine-side analogue of the
training-time SQL aggregate at `training/queries.py:379-382`:

    SELECT
      avg(vpin)    AS vpin_mean_60s,
      stddev(vpin) AS vpin_std_60s,
      min(vpin)    AS vpin_min_60s,
      max(vpin)    AS vpin_max_60s
    FROM ticks_binance
    WHERE asset = $X
      AND ts BETWEEN t_target - INTERVAL '60 seconds' AND t_target

PostgreSQL's `STDDEV` defaults to `STDDEV_SAMP` (n-1 denominator), so the
engine MUST also use sample stddev to keep train/serve parity. Anything
else (numpy.std with ddof=0, statistics.pstdev, etc.) silently shifts
the feature distribution under the booster.

Mirror of `app/v2_scorer.py::SmoothingBuffer` in the timesfm-service
repo: deque-based rolling window with eviction-by-timestamp on every
push. Lifetime is per-strategy (per-asset), matching the lifetime of
the engine's :class:`signals.vpin.VPINCalculator`.

Missing / NaN VPIN samples are skipped (None vpin = no measurement —
the per-tick VPIN calculator returns 0.0 before the first bucket
completes, which the strategy will simply not push). The training-side
SQL filters NULLs implicitly, so this matches that behaviour.

When the buffer holds fewer than 2 samples we return None for ALL FOUR
stats (not just stddev). The training pipeline treats <2 samples as
NaN-across-the-row via the SQL window, so this all-or-nothing rule
keeps the missing-value distribution consistent. LightGBM's
missing-default branch then dispatches the same way it did at fit
time.
"""

from __future__ import annotations

import collections
import math
from typing import Optional, Union


# Hard cap on deque length. With ~1 tick/sec and a 60s window we expect
# ~60 samples; 256 catches replay/time-warp edge cases without letting
# the buffer grow without bound. Time-based eviction in `push()` is the
# primary mechanism — `maxlen` is defence in depth.
_MAX_DEQUE_LEN = 256


class VpinRollingBuffer:
    """
    Per-asset rolling buffer of (ts, vpin) tuples over a fixed time window.

    Parameters
    ----------
    window_s:
        Window length in seconds (default 60). Samples with
        ``ts < now - window_s`` are evicted on every push.
    """

    __slots__ = ("_window_s", "_buf")

    def __init__(self, window_s: float = 60.0) -> None:
        self._window_s = float(window_s)
        # deque[(ts: float, vpin: float)]
        self._buf: collections.deque[tuple[float, float]] = collections.deque(
            maxlen=_MAX_DEQUE_LEN
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def push(self, ts: Union[float, int], vpin: Optional[float]) -> None:
        """
        Append one (ts, vpin) sample and evict samples older than the window.

        ``ts`` is a unix timestamp (seconds). ``vpin`` may be None (no
        measurement) — those samples are skipped without raising and
        without disturbing eviction.

        Time-warp tolerance: eviction uses the LATEST ts seen so far, so
        replays / clock skews don't permanently flush the buffer. The
        ``maxlen`` cap is the safety net.
        """
        if vpin is None:
            return
        try:
            v = float(vpin)
        except (TypeError, ValueError):
            return
        if math.isnan(v) or math.isinf(v):
            return

        try:
            t = float(ts)
        except (TypeError, ValueError):
            return
        if math.isnan(t) or math.isinf(t):
            return

        self._buf.append((t, v))

        # Evict relative to the newest ts we've ever seen — using the
        # incoming ts directly would be wrong if a stale tick arrives
        # late. Take the max of the incoming ts and the newest in-buffer
        # ts to be safe.
        newest = max(t, self._buf[-1][0])
        cutoff = newest - self._window_s
        while self._buf and self._buf[0][0] < cutoff:
            self._buf.popleft()

    def compute(self) -> dict[str, Optional[float]]:
        """
        Return the four 60s aggregate stats from the current buffer.

        Returns ``{"vpin_mean_60s", "vpin_std_60s", "vpin_min_60s",
        "vpin_max_60s"}`` mapping to floats, or None for ALL FOUR when
        the buffer holds fewer than 2 samples (sample-stddev is
        undefined for n=1; we apply the rule to the whole row for
        train/serve consistency).

        Standard deviation uses Bessel's correction (n-1 denominator)
        to match PostgreSQL's ``STDDEV_SAMP`` semantics.
        """
        if len(self._buf) < 2:
            return {
                "vpin_mean_60s": None,
                "vpin_std_60s": None,
                "vpin_min_60s": None,
                "vpin_max_60s": None,
            }

        vals = [v for _, v in self._buf]
        n = len(vals)
        mean = sum(vals) / n
        # Sample variance (n-1 denominator) — matches STDDEV_SAMP.
        sq_dev = sum((x - mean) ** 2 for x in vals)
        std = math.sqrt(sq_dev / (n - 1))
        return {
            "vpin_mean_60s": mean,
            "vpin_std_60s": std,
            "vpin_min_60s": min(vals),
            "vpin_max_60s": max(vals),
        }

    # ------------------------------------------------------------------
    # Introspection helpers (for tests + diagnostics)
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._buf)

    @property
    def window_s(self) -> float:
        return self._window_s

    @property
    def maxlen(self) -> int:
        return _MAX_DEQUE_LEN
