"""Cell bucketing helpers shared between RollingWRMonitor + CellPauseGate.

A "cell" is the tuple (strategy_id, direction, t_band, regime, session) used
by the rolling-WR auto-pause system (audits #379 + #385). Bucket boundaries
match the Hub-side analysis SQL (Hub note #350) so that the engine pauses
the same cells the analysis identifies as drawdown candidates.

Notes:
* `t_band` uses the engine's T-minus convention — `eval_offset` is seconds
  REMAINING UNTIL CLOSE, so `eval_offset=24` => `T-0-30` (last 30s of
  window). See memory `reference_eval_offset_verified.md`.
* `session` buckets by hour-of-day UTC into 7 trading sessions matching the
  Hub note #350 alpha analysis SQL.  The SQL CASE expression is reproduced
  in the session_label() docstring so future drift is caught at review time.
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


def session_label(hour_utc: Optional[int]) -> str:
    """Return the trading-session label for an hour-of-day UTC.

    7-bucket system matching the Hub note #350 analysis SQL. The canonical
    SQL CASE expression (reproduced here for drift detection — any divergence
    between this Python logic and the SQL must be investigated immediately):

        CASE
            WHEN EXTRACT(HOUR FROM ts AT TIME ZONE 'UTC') BETWEEN 0  AND  3
                THEN 'asian_early'
            WHEN EXTRACT(HOUR FROM ts AT TIME ZONE 'UTC') BETWEEN 4  AND  7
                THEN 'asian_late'
            WHEN EXTRACT(HOUR FROM ts AT TIME ZONE 'UTC') BETWEEN 8  AND 11
                THEN 'eu_am'
            WHEN EXTRACT(HOUR FROM ts AT TIME ZONE 'UTC') BETWEEN 12 AND 13
                THEN 'us_open'
            WHEN EXTRACT(HOUR FROM ts AT TIME ZONE 'UTC') BETWEEN 14 AND 17
                THEN 'us_pm'
            WHEN EXTRACT(HOUR FROM ts AT TIME ZONE 'UTC') BETWEEN 18 AND 21
                THEN 'us_late'
            ELSE 'off_hours'   -- hours 22, 23
        END AS session

    Buckets:
        00-03 UTC = asian_early   (Asian morning — Tokyo/Singapore open)
        04-07 UTC = asian_late    (Late Asian / early European pre-market)
        08-11 UTC = eu_am         (European morning — Frankfurt/London open)
        12-13 UTC = us_open       (Pre-NY ramp-up / European close)
        14-17 UTC = us_pm         (US market open at ~13:30 UTC + session peak)
        18-21 UTC = us_late       (US afternoon / NY close)
        22-23 UTC = off_hours     (Thin liquidity / overnight)

    Key insight from note #350: ``us_pm`` (14-17 UTC) is the worst session
    for v12_lgb_combo — both directions bleed despite WR > 50% due to high
    fill prices that make payout math unfavourable.
    """
    if hour_utc is None:
        return "unknown"
    h = int(hour_utc)
    if 0 <= h <= 3:
        return "asian_early"
    if 4 <= h <= 7:
        return "asian_late"
    if 8 <= h <= 11:
        return "eu_am"
    if 12 <= h <= 13:
        return "us_open"
    if 14 <= h <= 17:
        return "us_pm"
    if 18 <= h <= 21:
        return "us_late"
    return "off_hours"  # 22, 23


# Alias for backward compatibility with any callers using the old name.
session = session_label
