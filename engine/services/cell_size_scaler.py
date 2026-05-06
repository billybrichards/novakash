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
      "us_open:UP": 1.5,
      "asian_late:UP": 1.25,
      "us_late:DOWN": 1.25,
      "us_open:DOWN": 1.10
    }

Keys are `<session>:<direction>` where session ∈ session_label.session_for_hour
and direction ∈ {UP, DOWN, YES, NO}. UP/YES and DOWN/NO are accepted
synonyms — the cell tables historically used YES/NO but engine code uses
UP/DOWN. Both forms resolve.

Safety bounds
-------------
* Returned multiplier is clamped to ``[1.0, cell_size_multiplier_max]``
  (default cap = 1.5). Negative or sub-1 entries in the override map
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

from typing import Any, Dict, Optional

try:
    import structlog  # type: ignore[import]
    _log = structlog.get_logger(__name__)
except Exception:  # pragma: no cover — tests without structlog
    import logging
    _log = logging.getLogger(__name__)


_DEFAULT_MULTIPLIER_MAX = 1.5
_DEFAULT_MULTIPLIER = 1.0

# Direction synonyms — UP/YES and DOWN/NO map to the same cell.
_DIR_SYNONYMS: dict[str, tuple[str, ...]] = {
    "UP": ("UP", "YES"),
    "DOWN": ("DOWN", "NO"),
    "YES": ("YES", "UP"),
    "NO": ("NO", "DOWN"),
}


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
) -> float:
    """Return the bet-size multiplier for the (strategy, session, direction)
    cell.

    Resolution order:
    1. If any required arg is None / blank → return 1.0.
    2. Read ``cell_size_multipliers`` from the strategy's runtime overrides.
    3. Try lookup keys ``<session>:<dir_synonym>`` for each synonym of
       ``direction`` (UP/YES and DOWN/NO are equivalent).
    4. Clamp result to ``[1.0, cell_size_multiplier_max]``.
    """
    if not strategy_id or not session or not direction:
        return _DEFAULT_MULTIPLIER

    multipliers, cap = _read_override_map(strategy_id, override_provider)
    if not multipliers:
        return _DEFAULT_MULTIPLIER

    direction_norm = direction.strip().upper()
    candidates = _DIR_SYNONYMS.get(direction_norm, (direction_norm,))

    found: Optional[float] = None
    for d in candidates:
        key = f"{session}:{d}"
        if key in multipliers:
            found = multipliers[key]
            break
    if found is None:
        return _DEFAULT_MULTIPLIER

    # Clamp [1.0, cap] — multiplier is amp-only, never a discount.
    if found < 1.0:
        return _DEFAULT_MULTIPLIER
    return min(found, cap)


def apply_cell_size_multiplier(
    base_stake: float,
    *,
    strategy_id: Optional[str],
    session: Optional[str],
    direction: Optional[str],
    absolute_max_bet: float,
    min_bet_usd: float,
    override_provider: Optional[Any] = None,
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
          "clamp_reason": null,       # or "absolute_max_bet" / "min_bet_floor"
        }

    The caller uses ``final_stake`` for execution and the rest for logs/TG.
    """
    multiplier = cell_size_multiplier(
        strategy_id, session, direction, override_provider
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
        "clamp_reason": clamp_reason,
    }
