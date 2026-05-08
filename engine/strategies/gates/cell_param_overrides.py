"""Per-cell parameter override lookup gate helper (hub #402, 2026-05-08).

Resolves a strategy parameter for the current evaluation cell, with the
following fallback chain:
    per-cell override → (caller falls back to strategy-level param → global config)

Key format: ``"{direction}:{t_band}:{regime}:{session}"``
Example:     ``"UP:T-121-180:TRANSITION:eu_am"``

The cell key is constructed from the current window context using the same
``cell_bucketing`` service already used by the ``block_cells`` gate, so
t_band / session labels are guaranteed to match the Hub analysis SQL that
generated the override candidates.

DB-side format (in ``strategy_runtime_overrides.params``):

.. code-block:: json

    {
      "param_overrides_by_cell": {
        "UP:T-121-180:TRANSITION:eu_am": {
          "lgb_dist_min_up": 0.10
        },
        "DOWN:T-121-180:CASCADE:eu_am": {
          "lgb_dist_min_down": 0.10
        }
      }
    }

Usage in a strategy config::

    from strategies.gates.cell_param_overrides import get_cell_param_overrides

    _cell_overrides = get_cell_param_overrides(
        params=_gp.get_dict("param_overrides_by_cell", default={}),
        direction=direction,
        eval_offset=offset,
        regime=vpin_regime,
        window_ts=surface.window_ts,
    )
    # Override dist_min if a cell-level override exists for this window
    if direction == "UP":
        min_dist = _cell_overrides.get("lgb_dist_min_up", _lgb_dist_min_up())
    else:
        min_dist = _cell_overrides.get("lgb_dist_min_down", _lgb_dist_min_down())

Matching rules:
- An exact key match is required: all four components (direction, t_band,
  regime, session) must match.
- Most-specific match wins: if multiple keys match (this can only occur if
  the caller has stored duplicate or wildcard-style keys), the first match
  in dict iteration order is used.
- Wildcard: keys with empty/None components are NOT supported in this version.
  Every populated key component must match exactly. This keeps the lookup
  O(1) and avoids ambiguous precedence.
- Fails OPEN: any exception during lookup returns an empty dict so a
  misconfigured override never silences the strategy.

See Hub note #402 and ``docs/PR_B_ENGINE_SCOPE.md`` for full spec and
backing analysis.

NOT bypassable by VHC — cell parameter tuning is a structural alpha
decision, not a conviction question.
"""
from __future__ import annotations

import datetime as _dt
import logging as _logging
from typing import Any, Optional

from services.cell_bucketing import session_label as _session_label
from services.cell_bucketing import t_band as _t_band

_log = _logging.getLogger(__name__)


def get_cell_param_overrides(
    params: dict[str, Any],
    direction: str,
    eval_offset: Optional[int],
    regime: Optional[str],
    window_ts: Optional[int] = None,
) -> dict[str, Any]:
    """Return the per-cell parameter override dict for the current context.

    Args:
        params:      Dict of cell key -> override dict, from
                     ``_gp.get_dict("param_overrides_by_cell", default={})``.
                     Keys are ``"{direction}:{t_band}:{regime}:{session}"``.
        direction:   Trade direction "UP" or "DOWN".
        eval_offset: Seconds to window close (engine T-minus convention).
        regime:      Current VPIN regime label (e.g. "CASCADE", "TRANSITION").
                     May be None; a None regime never matches a non-empty key.
        window_ts:   Window epoch timestamp (seconds UTC) used to derive the
                     UTC hour for session bucketing.  If None, session resolves
                     to the ``cell_bucketing.session_label(None)`` sentinel.

    Returns:
        A dict of parameter name -> override value for this cell, or an empty
        dict if no override key matches. The caller is responsible for the
        fallback to strategy-level params.

    Fails OPEN:
        Any unexpected exception (malformed key, non-dict value stored in
        ``params``, etc.) is caught, logged at WARNING, and an empty dict is
        returned. This ensures a misconfigured override never blocks trading.
    """
    if not params:
        return {}

    try:
        return _resolve(params, direction, eval_offset, regime, window_ts)
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "cell_param_overrides: lookup failed (failing open) — %s",
            exc,
            exc_info=True,
        )
        return {}


def _resolve(
    params: dict[str, Any],
    direction: str,
    eval_offset: Optional[int],
    regime: Optional[str],
    window_ts: Optional[int],
) -> dict[str, Any]:
    """Inner lookup — may raise; callers wrap in try/except."""
    cell_t_band = _t_band(eval_offset)

    hour_utc: Optional[int] = None
    if window_ts is not None:
        try:
            hour_utc = _dt.datetime.fromtimestamp(
                int(window_ts), _dt.timezone.utc
            ).hour
        except (OSError, OverflowError, ValueError):
            hour_utc = None

    cell_session = _session_label(hour_utc)
    cell_regime = regime or ""

    # Construct the exact cell key from the current context.
    cell_key = f"{direction}:{cell_t_band}:{cell_regime}:{cell_session}"

    override = params.get(cell_key)
    if override is None:
        return {}

    if not isinstance(override, dict):
        _log.warning(
            "cell_param_overrides: key=%r has non-dict value %r — ignoring",
            cell_key,
            override,
        )
        return {}

    return override


def build_cell_key(
    direction: str,
    eval_offset: Optional[int],
    regime: Optional[str],
    window_ts: Optional[int] = None,
) -> str:
    """Build the canonical cell key string for a given evaluation context.

    Useful for constructing override keys during analysis and for test
    assertions.  The key format matches the ``get_cell_param_overrides``
    lookup so that::

        key = build_cell_key(direction, eval_offset, regime, window_ts)
        overrides = get_cell_param_overrides({key: {...}}, ...)
        # overrides will match iff the same context is passed

    Args:
        direction:   "UP" or "DOWN".
        eval_offset: Seconds to window close (engine T-minus convention).
        regime:      VPIN regime label, or None / "" for missing regime.
        window_ts:   Window epoch timestamp (seconds UTC), or None.

    Returns:
        Cell key string: ``"{direction}:{t_band}:{regime}:{session}"``.
    """
    cell_t_band = _t_band(eval_offset)

    hour_utc: Optional[int] = None
    if window_ts is not None:
        try:
            hour_utc = _dt.datetime.fromtimestamp(
                int(window_ts), _dt.timezone.utc
            ).hour
        except (OSError, OverflowError, ValueError):
            hour_utc = None

    cell_session = _session_label(hour_utc)
    cell_regime = regime or ""

    return f"{direction}:{cell_t_band}:{cell_regime}:{cell_session}"
