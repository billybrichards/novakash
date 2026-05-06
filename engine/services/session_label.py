"""UTC-hour → session-block label.

Cell analysis (memory `project_strategy_ledger.md` + Hub note #288 era)
buckets trade outcomes by session windows. The bands have been stable
across multiple analyses so are pinned in code as the canonical mapping
the cell_size_scaler keys against.

Bands (UTC hour, inclusive lower / exclusive upper, wrapped at 24):

    asian_early : [21, 02)   # ~21:00 UTC → 02:00 UTC (Tokyo open prep)
    asian_late  : [02, 07)   # 02:00 → 07:00 UTC      (Tokyo / HK / SG)
    eu_open     : [07, 12)   # 07:00 → 12:00 UTC      (London open)
    us_open     : [12, 17)   # 12:00 → 17:00 UTC      (NY pre/open)
    us_late     : [17, 21)   # 17:00 → 21:00 UTC      (NY close → after)

A wrong-by-one-hour drift would mis-attribute boost cells, so unit
tests pin the boundary cases.
"""
from __future__ import annotations

from datetime import datetime, timezone


# Inclusive lower-bound → label, evaluated in order. The wrap from 21
# back through 02 is handled by `session_for_hour` explicitly.
_BANDS: tuple[tuple[int, int, str], ...] = (
    (2, 7, "asian_late"),
    (7, 12, "eu_open"),
    (12, 17, "us_open"),
    (17, 21, "us_late"),
    # asian_early wraps midnight: hour >= 21 OR hour < 2
)


def session_for_hour(utc_hour: int) -> str:
    """Map a UTC hour [0, 23] to a session-block label."""
    if utc_hour < 0 or utc_hour > 23:
        raise ValueError(f"utc_hour out of range: {utc_hour}")
    if utc_hour >= 21 or utc_hour < 2:
        return "asian_early"
    for lo, hi, label in _BANDS:
        if lo <= utc_hour < hi:
            return label
    # Defensive — should be unreachable given the bands cover [0, 24).
    return "unknown"


def session_label(now_utc: datetime | None = None) -> str:
    """Return the session label for the given UTC datetime (defaults to now)."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        # Treat naive as UTC — same convention as the rest of the engine.
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return session_for_hour(now_utc.hour)
