"""Cell bucketing helpers shared between RollingWRMonitor + CellPauseGate.

A "cell" is the tuple (strategy_id, direction, t_band, regime, session) used
by the rolling-WR auto-pause system (audits #379 + #385). Bucket boundaries
match the Hub-side analysis SQL (Hub note #350) so that the engine pauses
the same cells the analysis identifies as drawdown candidates.

Notes:
* `t_band` uses the engine's T-minus convention — `eval_offset` is seconds
  REMAINING UNTIL CLOSE, so `eval_offset=24` => `T-0-30` (last 30s of
  window). See memory `reference_eval_offset_verified.md`.
* `session` buckets by hour-of-day UTC into 4 trading sessions.
"""

from __future__ import annotations

from typing import Optional


def t_band(eval_offset: Optional[int]) -> str:
    """Return the t_band label for an `eval_offset` (sec-to-close).

    eval_offset uses engine T-minus convention (verified 2026-05-01):
    a value of 24 means 24s remain before close (T-24).
    """
    if eval_offset is None:
        return "T-unknown"
    eo = int(eval_offset)
    if eo <= 30:
        return "T-0-30"
    if eo <= 60:
        return "T-31-60"
    if eo <= 90:
        return "T-61-90"
    if eo <= 120:
        return "T-91-120"
    if eo <= 180:
        return "T-121-180"
    if eo <= 240:
        return "T-181-240"
    return "T-241-300"


def session(hour_utc: Optional[int]) -> str:
    """Return the trading-session label for an hour-of-day UTC.

    Buckets:
        00-05 UTC = asian_late
        06-11 UTC = eu_am
        12-17 UTC = eu_pm_us_am
        18-23 UTC = us_pm_asian_am
    """
    if hour_utc is None:
        return "unknown"
    h = int(hour_utc)
    if 0 <= h < 6:
        return "asian_late"
    if 6 <= h < 12:
        return "eu_am"
    if 12 <= h < 18:
        return "eu_pm_us_am"
    return "us_pm_asian_am"
