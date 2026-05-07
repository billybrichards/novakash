"""Per-cell block predicate gate helper (audit #382, 2026-05-07).

Evaluates a list of block predicates against the current evaluation context.
If ANY predicate matches, the strategy should SKIP with a diagnostic reason.

Predicate fields (all optional; all specified fields must match):
    direction  str   "UP" | "DOWN"  — match strategy direction
    t_band     str   "T-61-90" etc — match cell_bucketing.t_band(eval_offset)
    conf_min   float                — match if confidence_score >= conf_min
    conf_max   float                — match if confidence_score < conf_max
    regime     str   "CASCADE" etc — match surface.regime (vpin regime)
    hour_utc   int   0-23           — match hour-of-day from window_ts (UTC)
    session    str   "us_pm" etc   — match cell_bucketing.session_label(hour_utc)

An empty predicate (no fields) matches NOTHING (defensive; prevents accidental
blanket block from a mis-formed override).

Usage in v9_ensemble (and any strategy that delegates here)::

    from strategies.gates.block_cells import check_block_cells_predicate

    reason = check_block_cells_predicate(
        predicates=_gp.get_list("block_cells", default=[]),
        direction=direction,
        eval_offset=offset,
        confidence_score=pl_dist,   # or whichever score is canonical
        regime=vpin_regime,
        window_ts=surface.window_ts,
    )
    if reason is not None:
        return _skip_v9(reason, gates, direction=direction)

See Hub note (ID in PR description) for the 7d cross-tab data motivating the
initial per-strategy block lists.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Optional

from services.cell_bucketing import session_label as _session_label
from services.cell_bucketing import t_band as _t_band


def check_block_cells_predicate(
    predicates: list[dict[str, Any]],
    direction: str,
    eval_offset: Optional[int],
    confidence_score: float,
    *,
    regime: Optional[str] = None,
    window_ts: Optional[int] = None,
) -> Optional[str]:
    """Return a skip reason string when any predicate matches; None to pass.

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
        None   — no predicate matched; strategy may proceed.
        str    — first matching predicate description; strategy should SKIP.
    """
    if not predicates:
        return None

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

    for pred in predicates:
        if not isinstance(pred, dict):
            # Malformed predicate — skip silently rather than crashing.
            continue

        if not pred:
            # Empty predicate is an explicit no-op (defensive).
            continue

        matched = True
        mismatch_reasons: list[str] = []

        # direction field
        if "direction" in pred:
            if pred["direction"] != direction:
                matched = False
                mismatch_reasons.append(f"direction={direction}!={pred['direction']}")

        # t_band field
        if matched and "t_band" in pred:
            if pred["t_band"] != cell_t_band:
                matched = False
                mismatch_reasons.append(
                    f"t_band={cell_t_band}!={pred['t_band']}"
                )

        # conf_min — inclusive lower bound
        if matched and "conf_min" in pred:
            try:
                conf_min = float(pred["conf_min"])
            except (TypeError, ValueError):
                conf_min = None
            if conf_min is not None and confidence_score < conf_min:
                matched = False
                mismatch_reasons.append(
                    f"conf={confidence_score:.4f}<conf_min={conf_min:.4f}"
                )

        # conf_max — exclusive upper bound
        if matched and "conf_max" in pred:
            try:
                conf_max = float(pred["conf_max"])
            except (TypeError, ValueError):
                conf_max = None
            if conf_max is not None and confidence_score >= conf_max:
                matched = False
                mismatch_reasons.append(
                    f"conf={confidence_score:.4f}>=conf_max={conf_max:.4f}"
                )

        # regime field
        if matched and "regime" in pred:
            if pred["regime"] != regime:
                matched = False
                mismatch_reasons.append(f"regime={regime}!={pred['regime']}")

        # hour_utc field — hour-precision blocks (preferred over session
        # for single-hour weak cells where session_label's 4-hour bucket
        # would over-block adjacent fine hours)
        if matched and "hour_utc" in pred:
            try:
                pred_hour = int(pred["hour_utc"])
            except (TypeError, ValueError):
                pred_hour = None
            if pred_hour is None or hour_utc != pred_hour:
                matched = False
                mismatch_reasons.append(
                    f"hour_utc={hour_utc}!={pred.get('hour_utc')}"
                )

        # session field
        if matched and "session" in pred:
            if pred["session"] != cell_session:
                matched = False
                mismatch_reasons.append(
                    f"session={cell_session}!={pred['session']}"
                )

        if matched:
            # Build a compact description of which predicate fired.
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
            predicate_desc = " ".join(parts) if parts else "empty-guard"
            return (
                f"block_cells: matched predicate [{predicate_desc}] "
                f"context: {direction}/{cell_t_band}/conf={confidence_score:.4f}"
                f"/{regime or '*'}/{cell_session}"
            )

    return None
