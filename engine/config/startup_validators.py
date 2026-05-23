"""Engine startup validators.

Cheap, side-effect-free checks that run once at boot. They log a warning when
something looks off but never crash the engine — operators can have legitimate
reasons for asymmetric configuration, so the validator's job is to surface the
mismatch loudly, not to enforce it.

Added 2026-05-23 after PR #577 (FIVE_MIN_ASSETS / FIFTEEN_MIN_ASSETS drift
silently dropping XRP from the 5m feed for ~24h). The two env vars are
independent on purpose (some assets may legitimately run on only one
timescale), but the most common mistake is forgetting to update both
together, so we warn whenever they disagree.
"""

from __future__ import annotations

import os
from typing import Optional

import structlog

log = structlog.get_logger(__name__)


def _parse_asset_env(raw: Optional[str]) -> set[str]:
    """Parse a comma-separated asset list env var into a normalised set.

    Empty / missing → empty set. Tokens are upper-cased and stripped; blanks
    are dropped. Returns a set so callers can do symmetric-difference cheaply.
    """
    if not raw:
        return set()
    return {tok.strip().upper() for tok in raw.split(",") if tok.strip()}


def validate_asset_enum_alignment(
    five_min_assets_env: Optional[str] = None,
    fifteen_min_assets_env: Optional[str] = None,
) -> bool:
    """Warn if FIVE_MIN_ASSETS and FIFTEEN_MIN_ASSETS disagree.

    Args may be passed for testability; default behaviour reads from
    os.environ so the caller (engine/main.py) doesn't have to plumb them.

    Returns True if the two sets match, False otherwise. The return value
    is for tests/callers — the engine itself ignores it and keeps booting.
    A False return is ALWAYS accompanied by a `warning`-level log line so
    operators see the mismatch in the boot log.

    Mismatch is NOT an error — some asymmetries are legitimate (e.g. an
    asset on 5m only because 15m data isn't backfilled yet, or vice
    versa). The warning's purpose is to flag the most common operator
    mistake: editing one var and forgetting the other (see PR #577).
    """
    five = _parse_asset_env(
        five_min_assets_env
        if five_min_assets_env is not None
        else os.environ.get("FIVE_MIN_ASSETS")
    )
    fifteen = _parse_asset_env(
        fifteen_min_assets_env
        if fifteen_min_assets_env is not None
        else os.environ.get("FIFTEEN_MIN_ASSETS")
    )

    if five == fifteen:
        log.info(
            "engine.startup.asset_enum_aligned",
            five_min=sorted(five),
            fifteen_min=sorted(fifteen),
        )
        return True

    only_in_five = five - fifteen
    only_in_fifteen = fifteen - five
    log.warning(
        "engine.startup.asset_enum_mismatch",
        five_min=sorted(five),
        fifteen_min=sorted(fifteen),
        only_in_five_min=sorted(only_in_five),
        only_in_fifteen_min=sorted(only_in_fifteen),
        hint=(
            "FIVE_MIN_ASSETS and FIFTEEN_MIN_ASSETS disagree. "
            "If intentional, document why; if not, this is the same "
            "class of drift that caused PR #577 (XRP silently dropped "
            "from 5m feed)."
        ),
    )
    return False
