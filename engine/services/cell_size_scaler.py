"""Per-cell bet-size multiplier (cell-aware Kelly multiplier).

Background
----------
30d real-trades alpha analysis (memory `project_mass_combo_2026_05_01.md`
+ session recap) shows several `(strategy_id, session, direction)` cells
with persistent positive WR + payoff-clearing PnL. The two strongest:

    us_open × v12_lgb_combo  × YES   77% WR n=30 +$167  →  1.5x
    asian_late × v12_combo   × YES   75% WR n=24 +$62   →  1.25x

Rather than spawn one strategy clone per cell (operational drag, multi-
plier of YAML files, flaky 4-row override merges), we plumb a single
multiplier function into the central stake calculator. Strategies whose
runtime overrides declare a `cell_size_multipliers` map get cell-aware
sizing; everyone else returns 1.0 and is unaffected.

Shape of the override (per-strategy `params.cell_size_multipliers`)::

    {
      # Most specific (4-axis)
      "asian_late:DOWN:T-121-180:CASCADE": 2.0,
      # 3-axis combinations
      "asian_late:DOWN:T-121-180": 1.5,
      "asian_late:DOWN:CASCADE": 1.3,
      # 2-axis (original behaviour)
      "asian_late:DOWN": 1.25,
      # Direction-only (strategy-wide)
      "DOWN": 1.25,
    }

Lookup priority (most specific → least specific → default 1.0)
--------------------------------------------------------------
1. ``session:dir:tband:regime``  — e.g. ``asian_late:DOWN:T-121-180:CASCADE``
2. ``session:dir:tband``         — e.g. ``asian_late:DOWN:T-121-180``
3. ``session:dir:regime``        — e.g. ``asian_late:DOWN:CASCADE``
4. ``session:dir``               — e.g. ``asian_late:DOWN`` (original behaviour)
5. ``dir:tband:regime``          — e.g. ``DOWN:T-121-180:CASCADE``
6. ``tband:regime``              — e.g. ``T-121-180:CASCADE`` (regime+timing only)
7. Default: 1.0

First key that matches wins. No multiplication or stacking — an exact
match returns the multiplier and the chain stops.

t_band helper
-------------
``eval_offset`` uses engine T-minus convention (sec-to-close, verified
2026-05-01). ``_t_band(eval_offset)`` converts to canonical bucket labels
(T-0-30, T-31-60, …, T-241-300). These labels match the Hub-side analysis
SQL and ``cell_bucketing.t_band()`` from PR #494.

TODO: once PR #494 (feat/hour-blocks-source-agreement-cell-pause) merges
into this branch, remove ``_t_band`` and import from
``services.cell_bucketing`` instead.

Keys are ``<session>:<direction>`` where session ∈ session_label.session_for_hour
and direction ∈ {UP, DOWN, YES, NO}. UP/YES and DOWN/NO are accepted
synonyms — the cell tables historically used YES/NO but engine code uses
UP/DOWN. Both forms resolve.

Safety bounds
-------------
* Returned multiplier is clamped to ``[1.0, cell_size_multiplier_max]``
  (default cap = 2.0). Negative or sub-1 entries in the override map
  are silently treated as 1.0 — this gate is only ever a stake AMP, never
  a discount, so a bad config can't shrink positions or short.
* The caller (``_calculate_stake``) is responsible for the absolute
  ``runtime.max_position_usd`` and ``MIN_BET_USD`` clamps after applying
  the multiplier — see ``engine/use_cases/execute_trade.py``.
* If the override layer is unreachable / errors / returns nonsense, the
  function returns 1.0 (preserves existing behaviour).

Telemetry
---------
``apply_cell_size_multiplier`` returns a dict of all relevant values for
caller logging. The caller emits a structured log line + Telegram alert
when ``multiplier > 1.0`` so we can audit boosted fires after the fact.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

try:
    import structlog  # type: ignore[import]
    _log = structlog.get_logger(__name__)
except Exception:  # pragma: no cover — tests without structlog
    import logging
    _log = logging.getLogger(__name__)

# Import canonical bucketing helpers from cell_bucketing (PR #494).
# This resolves the TODO comment from the original PR — the inline
# _t_band() is replaced by the shared implementation so boundaries
# never drift between the scaler and the auto-pause system.
from services.cell_bucketing import t_band as _t_band_canonical
from services.cell_bucketing import session_label as _session_label_canonical


def _t_band(eval_offset: Optional[int]) -> Optional[str]:
    """Wrapper that returns None for None (scaler skip-axis semantics).

    cell_bucketing.t_band() returns "T-unknown" for None, but the scaler
    uses None to mean "skip this axis entirely" in the lookup chain.
    """
    if eval_offset is None:
        return None
    return _t_band_canonical(eval_offset)


_DEFAULT_MULTIPLIER_MAX = 2.0  # bumped from 1.5 to support up-to-2x sniper cells
_DEFAULT_MULTIPLIER = 1.0

# Direction synonyms — UP/YES and DOWN/NO map to the same cell.
_DIR_SYNONYMS: dict[str, tuple[str, ...]] = {
    "UP": ("UP", "YES"),
    "DOWN": ("DOWN", "NO"),
    "YES": ("YES", "UP"),
    "NO": ("NO", "DOWN"),
}


# ── Lookup priority chain ────────────────────────────────────────────────
# Each lambda receives (session, dir, tband, regime) — all may be None.
# Returns None when the combo doesn't produce a valid key (skipped in loop).
#
# Priority: most-specific first so first match wins.

def _build_lookup_chain(
    session: Optional[str],
    direction_candidates: tuple[str, ...],
    tband: Optional[str],
    regime: Optional[str],
) -> List[str]:
    """Return candidate lookup keys in priority order (most specific first).

    Multiple direction synonyms are tried at each priority level so that
    a key stored as "asian_late:YES:T-121-180:CASCADE" matches when the
    engine passes direction="UP".
    """
    keys: List[str] = []

    def _add(parts: List[Optional[str]]) -> None:
        """Add one candidate per direction synonym, skip if any part is None."""
        # Replace the sentinel _DIR placeholder index 1 with each synonym
        for d in direction_candidates:
            candidate_parts = [p if p != "__DIR__" else d for p in parts]
            if any(p is None for p in candidate_parts):
                continue
            keys.append(":".join(candidate_parts))  # type: ignore[arg-type]

    # 1. session:dir:tband:regime
    _add([session, "__DIR__", tband, regime])
    # 2. session:dir:tband
    _add([session, "__DIR__", tband])
    # 3. session:dir:regime
    _add([session, "__DIR__", regime])
    # 4. session:dir  (original behaviour)
    _add([session, "__DIR__"])
    # 5. dir:tband:regime
    _add(["__DIR__", tband, regime])
    # 6. tband:regime
    if tband is not None and regime is not None:
        keys.append(f"{tband}:{regime}")

    return keys


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _read_override_map(
    strategy_id: str,
    override_provider: Optional[Any],
) -> tuple[Dict[str, float], float]:
    """Pull (multipliers_map, multiplier_max) from the runtime override
    layer for ``strategy_id``. Returns ({}, default_max) on any error.

    ``override_provider`` is a duck-typed object with
    ``get_effective_params(strategy_id, default) -> dict``. In production
    this is the singleton ``RuntimeOverrideManager``; in tests it can be
    any callable wrapper / mock.
    """
    if override_provider is None:
        return {}, _DEFAULT_MULTIPLIER_MAX
    try:
        params = override_provider.get_effective_params(strategy_id, {}) or {}
    except Exception as exc:  # pragma: no cover — defensive
        _log.debug(
            "cell_size_scaler: override read failed; defaulting to 1.0",
            strategy_id=strategy_id,
            error=str(exc),
        )
        return {}, _DEFAULT_MULTIPLIER_MAX

    raw_map = params.get("cell_size_multipliers") or {}
    if not isinstance(raw_map, dict):
        return {}, _DEFAULT_MULTIPLIER_MAX

    # Coerce values to floats (drop garbage).
    cleaned: Dict[str, float] = {}
    for key, val in raw_map.items():
        if not isinstance(key, str):
            continue
        f = _coerce_float(val, 1.0)
        cleaned[key] = f

    cap = _coerce_float(
        params.get("cell_size_multiplier_max", _DEFAULT_MULTIPLIER_MAX),
        _DEFAULT_MULTIPLIER_MAX,
    )
    if cap < 1.0:
        cap = _DEFAULT_MULTIPLIER_MAX
    return cleaned, cap


def cell_size_multiplier(
    strategy_id: Optional[str],
    session: Optional[str],
    direction: Optional[str],
    override_provider: Optional[Any] = None,
    *,
    t_band: Optional[int] = None,
    regime: Optional[str] = None,
) -> float:
    """Return the bet-size multiplier for a cell, using a fallback chain.

    Resolution order (most-specific wins):
    1. ``session:dir:tband:regime``
    2. ``session:dir:tband``
    3. ``session:dir:regime``
    4. ``session:dir``              — original behaviour
    5. ``dir:tband:regime``
    6. ``tband:regime``
    7. Default 1.0

    Parameters
    ----------
    strategy_id:
        Strategy identifier for override map lookup.
    session:
        Trading session label (e.g. ``asian_late``, ``us_open``).
        If None the lookup skips all session-specific layers.
    direction:
        Trade direction. UP/YES and DOWN/NO are accepted synonyms.
    override_provider:
        Duck-typed RuntimeOverrideManager. None → always 1.0.
    t_band:
        eval_offset (sec-to-close, T-minus convention). Passed as int and
        converted to label internally via ``_t_band()``. None skips the
        t_band axis so behaviour is backward-compatible with pre-multi-axis
        callers.
    regime:
        VPIN regime label (e.g. ``CASCADE``, ``TRANSITION``). None skips
        the regime axis.
    """
    if not strategy_id or not direction:
        return _DEFAULT_MULTIPLIER

    multipliers, cap = _read_override_map(strategy_id, override_provider)
    if not multipliers:
        return _DEFAULT_MULTIPLIER

    direction_norm = direction.strip().upper()
    dir_candidates = _DIR_SYNONYMS.get(direction_norm, (direction_norm,))

    tband_label = _t_band(t_band)
    regime_norm = regime.strip().upper() if regime else None

    lookup_keys = _build_lookup_chain(session, dir_candidates, tband_label, regime_norm)

    for key in lookup_keys:
        if key in multipliers:
            found = multipliers[key]
            # Clamp [1.0, cap] — multiplier is amp-only, never a discount.
            if found < 1.0:
                return _DEFAULT_MULTIPLIER
            return min(found, cap)

    return _DEFAULT_MULTIPLIER


def apply_cell_size_multiplier(
    base_stake: float,
    *,
    strategy_id: Optional[str],
    session: Optional[str],
    direction: Optional[str],
    absolute_max_bet: float,
    min_bet_usd: float,
    override_provider: Optional[Any] = None,
    t_band: Optional[int] = None,
    regime: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute the boosted stake plus an audit envelope for logging.

    Returned dict::

        {
          "multiplier": 1.5,
          "base_stake": 5.00,
          "scaled_stake": 7.50,
          "final_stake": 7.50,        # after absolute_max_bet/min clamps
          "session": "us_open",
          "direction": "UP",
          "strategy_id": "v12_lgb_combo",
          "t_band": "T-121-180",      # None when eval_offset not provided
          "regime": "CASCADE",        # None when not provided
          "matched_key": null,        # which key won the lookup (for telemetry)
          "clamp_reason": null,       # or "absolute_max_bet" / "min_bet_floor"
        }

    The caller uses ``final_stake`` for execution and the rest for logs/TG.
    """
    multiplier = cell_size_multiplier(
        strategy_id, session, direction, override_provider,
        t_band=t_band, regime=regime,
    )
    scaled = round(base_stake * multiplier, 4)

    final = scaled
    clamp_reason = None
    if final > absolute_max_bet:
        final = absolute_max_bet
        clamp_reason = "absolute_max_bet"
    if final < min_bet_usd:
        final = min_bet_usd
        clamp_reason = "min_bet_floor"

    return {
        "multiplier": multiplier,
        "base_stake": round(base_stake, 4),
        "scaled_stake": scaled,
        "final_stake": round(final, 4),
        "session": session,
        "direction": direction,
        "strategy_id": strategy_id,
        "t_band": _t_band(t_band),
        "regime": regime,
        "clamp_reason": clamp_reason,
    }
