"""Per-cell block predicate gate helper (audit #382, 2026-05-07).

Evaluates a list of block predicates against the current evaluation context.
If ANY predicate matches, the strategy should SKIP with a diagnostic reason,
OR (new in audit #410) reduce the stake via an optional ``size_multiplier``.

Predicate fields (all optional; all specified fields must match):
    direction       str   "UP" | "DOWN"  -- match strategy direction
    t_band          str   "T-61-90" etc -- match cell_bucketing.t_band(eval_offset)
    conf_min        float                -- match if confidence_score >= conf_min
    conf_max        float                -- match if confidence_score < conf_max
    regime          str   "CASCADE" etc -- match surface.regime (vpin regime)
    hour_utc        int   0-23           -- match hour-of-day from window_ts (UTC)
    session         str   "us_pm" etc   -- match cell_bucketing.session_label(hour_utc)

    size_multiplier float (NEW, audit #410)
        When present on a matching predicate the trade is NOT skipped --
        instead the stake is scaled by this factor before MIN/MAX clamps:
          * 1.0  = full stake (no change -- identical to predicate not matching)
          * 0.5  = half stake
          * 0.0  = full skip (semantically equivalent to a hard block)
        If absent the predicate is treated as a hard block (existing behaviour).

Resolution rules when multiple predicates match:
  * Any hard-block (missing size_multiplier, or size_multiplier == 0.0) wins over
    any partial-size predicate.
  * Among partial-size predicates the MOST RESTRICTIVE (minimum size_multiplier)
    wins -- conservative always takes precedence.

An empty predicate (no fields) matches NOTHING (defensive; prevents accidental
blanket block from a mis-formed override).

Usage in v9_ensemble (and any strategy that delegates here)::

    from strategies.gates.block_cells import (
        check_block_cells_predicate,
        resolve_block_cells_size_multiplier,
        BlockCellsResult,
    )

    # Hard-block path (existing callers -- unchanged API):
    reason = check_block_cells_predicate(
        predicates=_gp.get_list("block_cells", default=[]),
        direction=direction,
        eval_offset=offset,
        confidence_score=pl_dist,
        regime=_vpin_regime_for_block,
        window_ts=getattr(surface, "window_ts", None),
    )
    if reason is not None:
        return _skip_v9(reason, gates, direction=direction)

    # NEW: size-multiplier path (called from _calculate_stake via decision.metadata):
    result = resolve_block_cells_size_multiplier(
        predicates=_gp.get_list("block_cells", default=[]),
        direction=direction,
        eval_offset=offset,
        confidence_score=pl_dist,
        regime=_vpin_regime_for_block,
        window_ts=getattr(surface, "window_ts", None),
    )
    # result.skip_reason is None unless size_multiplier == 0.0
    # result.size_multiplier is the effective multiplier (1.0 if no match)

See Hub note #387 and PR description for the sizing-override plan.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Optional

from services.cell_bucketing import session_label as _session_label
from services.cell_bucketing import t_band as _t_band


@dataclass(frozen=True)
class BlockCellsResult:
    """Result of resolving block-cells predicates for a given context.

    When ``skip_reason`` is set the trade should be skipped entirely.
    When ``skip_reason`` is None the trade may proceed; ``size_multiplier``
    specifies the stake scaling factor (1.0 = no change; 0.5 = half stake).
    ``matched_predicate_desc`` carries a human-readable description of the
    winning predicate for logging (None when no predicate matched).
    """

    skip_reason: Optional[str] = None
    size_multiplier: float = 1.0
    matched_predicate_desc: Optional[str] = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_predicate_desc(
    pred: dict,
    direction: str,
    cell_t_band: str,
    confidence_score: float,
    regime: Optional[str],
    cell_session: str,
) -> str:
    """Build a compact human-readable description of a matched predicate."""
    parts = []
    if "direction" in pred:
        parts.append(f"dir={pred['direction']}")
    if "t_band" in pred:
        parts.append(f"t_band={pred['t_band']}")
    if "conf_min" in pred or "conf_max" in pred:
        lo = pred.get("conf_min", "")
        hi = pred.get("conf_max", "")
        if lo != "" and hi != "":
            parts.append(f"conf=[{lo},{hi})")
        elif lo != "":
            parts.append(f"conf>={lo}")
        else:
            parts.append(f"conf<{hi}")
    if "regime" in pred:
        parts.append(f"regime={pred['regime']}")
    if "hour_utc" in pred:
        parts.append(f"hour_utc={pred['hour_utc']}")
    if "session" in pred:
        parts.append(f"session={pred['session']}")
    if "size_multiplier" in pred:
        parts.append(f"size_mult={pred['size_multiplier']}")
    predicate_desc = " ".join(parts) if parts else "empty-guard"
    return (
        f"block_cells: matched predicate [{predicate_desc}] "
        f"context: {direction}/{cell_t_band}/conf={confidence_score:.4f}"
        f"/{regime or '*'}/{cell_session}"
    )


def _is_predicate_match(
    pred: dict,
    direction: str,
    cell_t_band: str,
    confidence_score: float,
    hour_utc: Optional[int],
    cell_session: str,
    regime: Optional[str],
) -> bool:
    """Return True if all specified fields in pred match the context."""
    if not isinstance(pred, dict) or not pred:
        return False

    # direction field
    if "direction" in pred and pred["direction"] != direction:
        return False

    # t_band field
    if "t_band" in pred and pred["t_band"] != cell_t_band:
        return False

    # conf_min -- inclusive lower bound
    if "conf_min" in pred:
        try:
            conf_min = float(pred["conf_min"])
        except (TypeError, ValueError):
            conf_min = None
        if conf_min is not None and confidence_score < conf_min:
            return False

    # conf_max -- exclusive upper bound
    if "conf_max" in pred:
        try:
            conf_max = float(pred["conf_max"])
        except (TypeError, ValueError):
            conf_max = None
        if conf_max is not None and confidence_score >= conf_max:
            return False

    # regime field
    if "regime" in pred and pred["regime"] != regime:
        return False

    # hour_utc field -- hour-precision blocks (preferred over session for
    # single-hour weak cells where session_label's 4-hour bucket would
    # over-block adjacent fine hours)
    if "hour_utc" in pred:
        try:
            pred_hour = int(pred["hour_utc"])
        except (TypeError, ValueError):
            pred_hour = None
        if pred_hour is None or hour_utc != pred_hour:
            return False

    # session field
    if "session" in pred and pred["session"] != cell_session:
        return False

    return True


def _resolve_context(
    eval_offset: Optional[int],
    window_ts: Optional[int],
) -> tuple:
    """Compute (cell_t_band, hour_utc, cell_session) from raw inputs."""
    cell_t_band = _t_band(eval_offset)

    hour_utc: Optional[int] = None
    if window_ts is not None:
        try:
            hour_utc = _dt.datetime.fromtimestamp(
                int(window_ts), _dt.timezone.utc
            ).hour
        except (TypeError, ValueError, OSError):
            hour_utc = None
    cell_session = _session_label(hour_utc)
    return cell_t_band, hour_utc, cell_session


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_block_cells_predicate(
    predicates: list,
    direction: str,
    eval_offset: Optional[int],
    confidence_score: float,
    *,
    regime: Optional[str] = None,
    window_ts: Optional[int] = None,
) -> Optional[str]:
    """Return a skip reason string when any hard-block predicate matches.

    This is the EXISTING API -- unchanged. Callers that only need hard-block
    behaviour (skip vs proceed) use this function. Predicates that carry a
    ``size_multiplier > 0.0`` are NOT treated as hard blocks here (they pass
    through), preserving full backward compatibility with all current callers.

    For the partial-stake path use ``resolve_block_cells_size_multiplier``.

    Args:
        predicates:        List of predicate dicts (from ``block_cells`` override).
        direction:         Trade direction "UP" or "DOWN".
        eval_offset:       Seconds to window close (engine T-minus convention).
        confidence_score:  The distance score used for confidence banding.
                           Convention: abs(probability - 0.5) so range [0, 0.5].
        regime:            Current VPIN regime label (e.g. "CASCADE", "chop").
        window_ts:         Window epoch timestamp used to derive UTC hour for
                           session bucketing.

    Returns:
        None   -- no hard-block predicate matched; strategy may proceed.
        str    -- first matching hard-block description; strategy should SKIP.
    """
    if not predicates:
        return None

    cell_t_band, hour_utc, cell_session = _resolve_context(eval_offset, window_ts)

    for pred in predicates:
        if not _is_predicate_match(
            pred, direction, cell_t_band, confidence_score, hour_utc, cell_session, regime
        ):
            continue

        # Predicate matched. If it carries size_multiplier > 0, treat as pass
        # (the stake-scaling path handles it separately). Only block when:
        #   * size_multiplier is absent (legacy hard-block), or
        #   * size_multiplier == 0.0 (explicit skip)
        size_mult = pred.get("size_multiplier")
        if size_mult is not None:
            try:
                size_mult_f = float(size_mult)
            except (TypeError, ValueError):
                size_mult_f = 0.0
            if size_mult_f > 0.0:
                # Partial-size predicate -- not a hard block from this function's
                # perspective. Continue scanning for a hard-block predicate.
                continue

        # Hard block (size_mult absent or == 0.0).
        return _build_predicate_desc(
            pred, direction, cell_t_band, confidence_score, regime, cell_session
        )

    return None


def resolve_block_cells_size_multiplier(
    predicates: list,
    direction: str,
    eval_offset: Optional[int],
    confidence_score: float,
    *,
    regime: Optional[str] = None,
    window_ts: Optional[int] = None,
) -> BlockCellsResult:
    """Resolve all matching block-cell predicates and return the net effect.

    Returns a :class:`BlockCellsResult` describing whether to skip or proceed
    (and with what stake multiplier). Resolution rules:

    1. Any hard-block predicate (missing ``size_multiplier``, or
       ``size_multiplier == 0.0``) wins unconditionally -- returns
       ``skip_reason`` set, ``size_multiplier=0.0``.
    2. If no hard-block: take the MINIMUM ``size_multiplier`` across all
       matched partial-size predicates (most conservative wins).
    3. If no predicates match: ``size_multiplier=1.0`` (no effect).

    Args:
        predicates:        List of predicate dicts from ``block_cells`` override.
        direction:         Trade direction "UP" or "DOWN".
        eval_offset:       Seconds to window close (engine T-minus convention).
        confidence_score:  Distance score abs(p-0.5), range [0, 0.5].
        regime:            Current VPIN regime label.
        window_ts:         Window epoch for UTC-hour derivation.

    Returns:
        BlockCellsResult with skip_reason and/or size_multiplier.
    """
    if not predicates:
        return BlockCellsResult()

    cell_t_band, hour_utc, cell_session = _resolve_context(eval_offset, window_ts)

    best_mult: Optional[float] = None   # min across all partial-size matches
    best_desc: Optional[str] = None

    for pred in predicates:
        if not _is_predicate_match(
            pred, direction, cell_t_band, confidence_score, hour_utc, cell_session, regime
        ):
            continue

        desc = _build_predicate_desc(
            pred, direction, cell_t_band, confidence_score, regime, cell_session
        )

        size_mult = pred.get("size_multiplier")
        if size_mult is None:
            # Hard block -- wins immediately, no need to scan further.
            return BlockCellsResult(
                skip_reason=desc,
                size_multiplier=0.0,
                matched_predicate_desc=desc,
            )

        try:
            size_mult_f = float(size_mult)
        except (TypeError, ValueError):
            size_mult_f = 0.0

        if size_mult_f <= 0.0:
            # Explicit size_multiplier=0.0 is equivalent to hard block.
            return BlockCellsResult(
                skip_reason=desc,
                size_multiplier=0.0,
                matched_predicate_desc=desc,
            )

        # Partial-size match: track the most restrictive multiplier.
        if best_mult is None or size_mult_f < best_mult:
            best_mult = size_mult_f
            best_desc = desc

    if best_mult is not None:
        return BlockCellsResult(
            skip_reason=None,
            size_multiplier=best_mult,
            matched_predicate_desc=best_desc,
        )

    return BlockCellsResult()  # no predicate matched
