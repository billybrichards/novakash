"""Shared TickFormer SHADOW-strategy evaluation core.

Underscore-prefixed module so the strategy-registry YAML auto-loader
DOES NOT treat it as a strategy itself. The three sister strategies
(tickformer_v16_pure / tickformer_v17_sniper / tickformer_v18_t180)
all delegate to ``evaluate_tickformer_strategy`` below — they differ
only in:

  * which probability column they read off the FullDataSurface
    (``probability_tickformer_v16`` / ``_v17`` / ``_v18``);
  * their default operating point (threshold + eval_offset_remaining
    band) which YAML always overrides at promotion time;
  * their ``_STRATEGY_ID`` label for decision-record attribution.

Behaviour is identical to the previous three near-duplicate hooks (PR
#619 review FIX 1) — same SKIP reasons, same metadata keys, same
SHADOW kill-switch semantics. Tests live in
``engine/tests/unit/strategies/test_tickformer_strategies.py``.

Multi-pocket support (feat/tickformer-multi-pocket): if
``gate_params.pockets`` is a non-empty list, it overrides the legacy
single-pocket fields (``up_threshold``, ``down_threshold``,
``eval_offset_remaining_min``, ``eval_offset_remaining_max``).  Each
pocket entry requires ``up_threshold``, ``rem_min``, ``rem_max`` and
optionally ``down_threshold`` (defaults to ``1 - up_threshold``).
When multiple pockets match the current ``eval_offset_remaining``, the
strictest one wins (highest ``up_threshold`` for UP direction, lowest
``down_threshold`` for DOWN direction).  The matched pocket's index and
spec are recorded in ``meta.pocket_index`` / ``meta.pocket_spec`` for
introspection.  If ``pockets`` is absent or empty, the legacy
single-pocket path is used unchanged (full backwards compatibility).

Tier-lookup support (FIX 3, hardened post-review): if
``gate_params.tier`` is set to one of the keys in
``tickformer_tiers.yaml`` (TIER_A/B/C/D), that tier's threshold +
eval_offset_remaining band WIN over any YAML/runtime-override
gate_param values for ``up_threshold`` /
``eval_offset_remaining_min`` / ``eval_offset_remaining_max``. This
is the post-PR-#619 review fix — previously YAML pre-populated those
keys (operator-wins precedence) which silently neutered any runtime
``tier: TIER_X`` flip. New rule: **if you set ``tier`` you MUST
either accept the tier's values, OR clear ``tier`` and set the
explicit knobs**. The runtime override DB path is the canonical way
to promote a strategy from Tier C → B without a YAML redeploy.

Mutex-group support (FIX 2): a strategy can declare
``gate_params.mutex_group = "tickformer"``. The metadata field
``mutex_group`` is propagated into the decision so the engine-side
resolver (``strategies.mutex_resolver``) can demote losers to SKIP
after every strategy in a window evaluates.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import yaml

logger = logging.getLogger(__name__)

# Track strategies that have already triggered the implicit-DOWN
# symmetry warning so we don't spam logs every eval tick. PR #619
# review SHOULD-FIX 3.
_DOWN_SYMMETRY_WARNED: set[str] = set()

from domain.value_objects import StrategyDecision
from strategies import gate_params as _gp

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


# ── Shared constants (mirror the original three hooks) ────────────────

_DEFAULT_ENTRY_CAP = 0.93
_DEFAULT_ENTRY_FLOOR_UP = 0.60
_DEFAULT_ENTRY_CAP_DOWN = 0.90
_DEFAULT_COLLATERAL_PCT = 0.025
_DEFAULT_GTC_CAP = 0.96
_DEFAULT_MIN_CONSEC_TICKS = 1
_DEFAULT_SHADOW_ONLY = True
_MAX_GAP_S = 5.0

# Per-(strategy_id, window_ts, direction) consecutive-tick state.
# Module-local so the three magic-model strategies' counters cannot
# collide with v9_2_eth_solo / v9_5_xrp_up_solo etc.
_consec_state: dict[tuple[str, int, str], tuple[int, float]] = {}


def _bump_and_check(
    strategy_id: str, window_ts: int, direction: str
) -> int:
    """Return current consecutive-pass tick count for the (strategy,
    window, direction) triple. Mirrors the previous per-hook impl but
    namespaced by strategy_id so the v16/v17/v18 counters stay
    independent (a v16 pass tick must not satisfy v18's
    min_consecutive_pass_ticks)."""
    now = time.time()
    key = (strategy_id, int(window_ts), direction)
    other = "DOWN" if direction == "UP" else "UP"
    _consec_state.pop((strategy_id, int(window_ts), other), None)
    prev = _consec_state.get(key)
    if prev is None or (now - prev[1]) > _MAX_GAP_S:
        count = 1
    else:
        count = prev[0] + 1
    _consec_state[key] = (count, now)
    if len(_consec_state) > 200:
        cutoff = now - 600.0
        for k in [k for k, v in _consec_state.items() if v[1] < cutoff]:
            _consec_state.pop(k, None)
    return count


def _resolve_band_for_direction(
    direction: str, *, sym_min: int, sym_max: int
) -> tuple[int, int]:
    """Return ``(band_min, band_max)`` for ``direction`` using the waterfall:

    For each direction X ∈ {'up', 'down'}:
      1. ``eval_offset_remaining_{min,max}_X`` in active gate-params bag
         (runtime override or YAML direction-specific key — highest
         precedence).
      2. Else ``sym_min`` / ``sym_max`` — the already-resolved symmetric
         band (from runtime override bare key → YAML bare key → default).

    Back-compat guarantee: when neither ``eval_offset_remaining_max_up``
    nor ``eval_offset_remaining_max_down`` is present in the active params
    bag, ``sym_min`` / ``sym_max`` are returned unchanged, preserving the
    existing symmetric-band behaviour for all existing strategies and
    runtime overrides.
    """
    suffix = direction.lower()  # 'up' or 'down'
    band_min = _gp.get_int(f"eval_offset_remaining_min_{suffix}", None, sym_min)
    band_max = _gp.get_int(f"eval_offset_remaining_max_{suffix}", None, sym_max)
    return (band_min, band_max)


def _eval_offset_remaining(surface: "FullDataSurface") -> Optional[int]:
    """Return seconds-remaining in the 5m window.

    Prefers an explicit ``eval_offset_remaining`` surface field if
    present (forward-compat with a future surface column); otherwise
    returns ``eval_offset`` directly.

    IMPORTANT: ``eval_offset`` on FullDataSurface is set equal to
    ``seconds_to_close`` (see data_surface.py line: ``seconds_to_close =
    eval_offset``).  It is therefore already "seconds remaining", NOT
    "seconds elapsed since window open".  An earlier version of this
    function incorrectly computed ``300 - eval_offset``, which produced
    values in the range [60, 276] for the typical late-window ticks
    (eval_offset ≈ 24–240) — the mirror image of the correct band.

    With that formula, a tick at eval_offset=200 (200s remaining) was
    treated as remaining=100, and a tick at eval_offset=24 (24s
    remaining, near window close) was treated as remaining=276 (near
    window open) — systematically outside every strategy's configured
    eval_offset_remaining band.  Fix: return eval_offset as-is.
    (fix/tickformer-strategies-actually-fire, Bug E)
    """
    explicit = getattr(surface, "eval_offset_remaining", None)
    if explicit is not None:
        try:
            return int(explicit)
        except (TypeError, ValueError):
            return None
    eval_offset = getattr(surface, "eval_offset", None)
    if eval_offset is None:
        return None
    try:
        # eval_offset == seconds_to_close (see FullDataSurface definition).
        # Return it directly — no "300 - eval_offset" transformation.
        return max(0, int(eval_offset))
    except (TypeError, ValueError):
        return None


# ── Tier-lookup (FIX 3) ───────────────────────────────────────────────

# Lives one level up (engine/strategies/) so the registry's
# `configs/*.yaml` glob does NOT auto-load it as a strategy.
_TIERS_PATH = Path(__file__).resolve().parent.parent / "tickformer_tiers.yaml"
_TIERS_CACHE: Optional[dict] = None


def _load_tiers() -> dict:
    """Load the tickformer_tiers.yaml preset map (cached)."""
    global _TIERS_CACHE
    if _TIERS_CACHE is None:
        try:
            with _TIERS_PATH.open("r") as fh:
                data = yaml.safe_load(fh) or {}
            _TIERS_CACHE = data.get("tiers", {}) if isinstance(data, dict) else {}
        except (OSError, yaml.YAMLError):
            _TIERS_CACHE = {}
    return _TIERS_CACHE


def _resolve_tier_overrides(
    tier_key: Optional[str],
) -> tuple[Optional[float], Optional[int], Optional[int]]:
    """Return (up_threshold, rem_min, rem_max) from the tier preset, or
    (None, None, None) when no tier or unknown tier. Operator-supplied
    explicit overrides in gate_params still win (handled by caller via
    ``_gp.get_*`` lookups which read YAML first)."""
    if not tier_key:
        return (None, None, None)
    tiers = _load_tiers()
    preset = tiers.get(tier_key)
    if not isinstance(preset, dict):
        return (None, None, None)
    up = preset.get("up_threshold")
    rem_min = preset.get("eval_offset_remaining_min")
    rem_max = preset.get("eval_offset_remaining_max")
    return (
        float(up) if up is not None else None,
        int(rem_min) if rem_min is not None else None,
        int(rem_max) if rem_max is not None else None,
    )


# ── Multi-pocket helpers ─────────────────────────────────────────────


def _normalise_pocket(raw: dict, index: int) -> Optional[dict]:
    """Validate and normalise a single pocket entry from gate_params.pockets.

    Returns a normalised dict with keys:
      up_threshold (float), down_threshold (float), rem_min (int), rem_max (int)
    or None if the entry is malformed (missing required keys / wrong types).

    Logs a WARNING and returns None on malformed entries so a bad pocket
    never silently swallows valid pockets that follow it — fail-open.
    """
    try:
        up = float(raw["up_threshold"])
        rem_min = int(raw["rem_min"])
        rem_max = int(raw["rem_max"])
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "tickformer.pocket_malformed index=%d entry=%r exc=%s — pocket skipped",
            index,
            raw,
            exc,
        )
        return None
    down_raw = raw.get("down_threshold")
    down = float(down_raw) if down_raw is not None else max(0.0, min(1.0, 1.0 - up))
    if rem_min > rem_max:
        logger.warning(
            "tickformer.pocket_invalid_band index=%d rem_min=%d > rem_max=%d — pocket skipped",
            index,
            rem_min,
            rem_max,
        )
        return None
    return {
        "up_threshold": up,
        "down_threshold": down,
        "rem_min": rem_min,
        "rem_max": rem_max,
    }


def _match_pocket(
    pockets: list[dict], remaining: int, p: float
) -> tuple[Optional[str], Optional[int], Optional[dict]]:
    """Find the best-matching pocket for the current tick.

    Args:
        pockets: list of normalised pocket dicts (from _normalise_pocket).
        remaining: current eval_offset_remaining (seconds).
        p: tickformer probability for this tick.

    Returns:
        (direction, pocket_index, pocket_spec) where direction is "UP",
        "DOWN", or None (below threshold in all matching pockets).
        pocket_index is the index into ``pockets``; pocket_spec is the
        normalised pocket dict.  Returns (None, None, None) when no
        pocket's band contains ``remaining``.
    """
    # Collect all pockets whose band contains ``remaining``.
    in_band: list[tuple[int, dict]] = [
        (i, pk)
        for i, pk in enumerate(pockets)
        if pk["rem_min"] <= remaining <= pk["rem_max"]
    ]
    if not in_band:
        return (None, None, None)

    # Among in-band pockets, check thresholds.  If multiple pockets match
    # the same direction, pick the strictest:
    #   UP:   highest up_threshold (hardest to cross → most conservative)
    #   DOWN: lowest down_threshold (farthest from 0.5 → most conservative)
    best_up: Optional[tuple[int, dict]] = None
    best_down: Optional[tuple[int, dict]] = None

    for idx, pk in in_band:
        if p >= pk["up_threshold"]:
            if best_up is None or pk["up_threshold"] > best_up[1]["up_threshold"]:
                best_up = (idx, pk)
        if p <= pk["down_threshold"]:
            if best_down is None or pk["down_threshold"] < best_down[1]["down_threshold"]:
                best_down = (idx, pk)

    # Prefer whichever direction has the strictest qualifying pocket.
    # Tie-break: UP wins (arbitrary but stable).
    if best_up is not None:
        return ("UP", best_up[0], best_up[1])
    if best_down is not None:
        return ("DOWN", best_down[0], best_down[1])

    # In-band but probability crossed no pocket's threshold.
    return ("__in_band_no_cross__", None, None)


# ── Decision factories ───────────────────────────────────────────────


def _skip(
    reason: str, metadata: dict, *, strategy_id: str, version: str
) -> StrategyDecision:
    return StrategyDecision(
        action="SKIP",
        direction=None,
        confidence=None,
        confidence_score=0.0,
        entry_cap=0.0,
        collateral_pct=0.0,
        strategy_id=strategy_id,
        strategy_version=version,
        entry_reason="",
        skip_reason=reason,
        metadata=metadata,
    )


# ── Main entry point ─────────────────────────────────────────────────


def evaluate_tickformer_strategy(
    surface: "FullDataSurface",
    *,
    prob_column: str,
    strategy_id: str,
    version: str = "1.0.0",
    default_up_threshold: float = 0.85,
    default_rem_min: int = 60,
    default_rem_max: int = 240,
    entry_reason_label: Optional[str] = None,
    not_available_reason: Optional[str] = None,
) -> StrategyDecision:
    """Shared SHADOW-strategy evaluation for the TickFormer family.

    Args:
        surface: FullDataSurface for this window/eval_offset.
        prob_column: name of the probability attribute on the surface
            (e.g. ``"probability_tickformer_v17"``).
        strategy_id: stable id used for ``strategy_configs.strategy_id``,
            decision-record attribution, and the namespaced consec
            counter. e.g. ``"tickformer_v17_sniper"``.
        version: strategy_version stamped on the decision record.
        default_up_threshold: fallback when neither YAML gate_params
            nor a tier preset specifies ``up_threshold``.
        default_rem_min / default_rem_max: fallback eval_offset_remaining
            band (seconds-remaining-in-5m-window) used when neither
            YAML nor a tier preset specifies them.
        entry_reason_label: human-readable label for the TRADE
            decision's ``entry_reason``. Defaults to
            ``f"{strategy_id}_pass"``.
        not_available_reason: skip_reason returned when the probability
            column is missing on the surface. Defaults to
            ``f"{strategy_id}_not_available"`` to match the legacy
            ``tickformer_v16_not_available`` token.

    Returns:
        StrategyDecision with action TRADE (if all gates pass and
        ``shadow_only`` is false) or SKIP (otherwise).
    """
    entry_reason_label = entry_reason_label or f"{strategy_id}_pass"
    not_available_reason = (
        not_available_reason or f"{strategy_id}_not_available"
    )

    p = getattr(surface, prob_column, None)
    trade_signal = getattr(surface, "tickformer_trade_signal", None)
    eval_offset = getattr(surface, "eval_offset", None)
    asset = getattr(surface, "asset", None)
    # window_ts is needed for meta['window_ts'] — required by
    # has_fill_for_strategy_window_direction to detect existing trades.
    # Moved before the if/else paths so it's available in both meta dicts.
    window_ts = getattr(surface, "window_ts", None)

    if p is None:
        return _skip(
            not_available_reason,
            {prob_column: None, "asset": asset},
            strategy_id=strategy_id,
            version=version,
        )
    p = float(p)

    # ── Multi-pocket resolution (feat/tickformer-multi-pocket) ───
    # If gate_params.pockets is a non-empty list, it completely overrides
    # the legacy single-pocket fields (up_threshold, down_threshold,
    # eval_offset_remaining_{min,max}) and tier preset.  Backwards compat:
    # pockets absent or [] → fall through to the legacy single-pocket path.
    raw_pockets = _gp.get_list("pockets", [])
    valid_pockets: list[dict] = []
    if raw_pockets:
        for _idx, _raw in enumerate(raw_pockets):
            if isinstance(_raw, dict):
                _normed = _normalise_pocket(_raw, _idx)
                if _normed is not None:
                    valid_pockets.append(_normed)

    shadow_only = bool(
        _gp.get_int("shadow_only", None, int(_DEFAULT_SHADOW_ONLY))
    )
    mutex_group = _gp.get_str("mutex_group", None, "")

    remaining = _eval_offset_remaining(surface)

    if valid_pockets:
        # ── MULTI-POCKET PATH ─────────────────────────────────────
        # Build a compact meta record; the matched pocket will contribute
        # additional fields below.
        meta: dict = {
            prob_column: p,
            "tickformer_trade_signal": trade_signal,
            "eval_offset": eval_offset,
            "eval_offset_remaining": remaining,
            "pockets": valid_pockets,
            "asset": asset,
            "shadow_only": shadow_only,
            # window_ts is required by has_fill_for_strategy_window_direction
            # (pg_trade_repo.py) which queries COALESCE(metadata->>'window_ts',
            # '') against the window epoch. Without it the query always returns
            # False (empty-string != epoch integer), so the hard lock at
            # ExecuteTradeUseCase Step -0.5 cannot detect an existing trade and
            # a concurrent/retry evaluate_all fires a duplicate order.
            # Incident: tickformer_v16_pure double-fire 2026-05-29 21:58 UTC
            # (trades 9442 + 9443, -$10 instead of -$5).
            "window_ts": window_ts,
        }
        if mutex_group:
            meta["mutex_group"] = mutex_group

        if remaining is None:
            return _skip(
                "outside_eval_offset_remaining_band",
                meta,
                strategy_id=strategy_id,
                version=version,
            )

        direction, matched_idx, matched_pocket = _match_pocket(
            valid_pockets, remaining, p
        )

        if direction is None:
            # No pocket band covers this remaining value.
            return _skip(
                "outside_eval_offset_remaining_band",
                meta,
                strategy_id=strategy_id,
                version=version,
            )

        if direction == "__in_band_no_cross__":
            # In-band but probability didn't cross any pocket's threshold.
            return _skip(
                "conviction_below_threshold",
                meta,
                strategy_id=strategy_id,
                version=version,
            )

        # Populate meta with the winning pocket's params for introspection.
        meta["pocket_index"] = matched_idx
        meta["pocket_spec"] = matched_pocket
        meta["up_threshold"] = matched_pocket["up_threshold"]
        meta["down_threshold"] = matched_pocket["down_threshold"]
        meta["eval_offset_remaining_min"] = matched_pocket["rem_min"]
        meta["eval_offset_remaining_max"] = matched_pocket["rem_max"]

    else:
        # ── LEGACY SINGLE-POCKET PATH (backwards compatible) ─────
        # Resolution order:
        #   1. If gate_params.tier is set AND resolves to a valid preset,
        #      the tier's up_threshold / eval_offset_remaining_{min,max}
        #      WIN over any YAML/runtime gate_param values for those keys.
        #   2. Otherwise, fall through to gate_params.up_threshold /
        #      eval_offset_remaining_* (YAML / runtime override), then to
        #      the per-strategy hard defaults.
        tier_key = _gp.get_str("tier", None, "")
        tier_up, tier_rem_min, tier_rem_max = _resolve_tier_overrides(
            tier_key or None
        )

        if tier_up is not None:
            up_threshold = float(tier_up)
        else:
            up_threshold = _gp.get_float("up_threshold", None, default_up_threshold)

        # ── DOWN-side symmetry (review SHOULD-FIX 3) ─────────────────
        # If the operator did not set ``down_threshold`` explicitly we
        # default to ``1 - up_threshold``. Emit a one-shot WARN per
        # strategy so an operator who sets only ``up_threshold=0.92``
        # is not silently given down_threshold=0.08 without noticing.
        _down_default = max(0.0, min(1.0, 1.0 - up_threshold))
        _down_in_active = "down_threshold" in _gp._ACTIVE.get()
        down_threshold = _gp.get_float("down_threshold", None, _down_default)
        if not _down_in_active and strategy_id not in _DOWN_SYMMETRY_WARNED:
            _DOWN_SYMMETRY_WARNED.add(strategy_id)
            logger.warning(
                "tickformer.implicit_down_symmetry strategy=%s up=%.3f down=%.3f "
                "(set gate_params.down_threshold to silence)",
                strategy_id,
                up_threshold,
                _down_default,
            )

        if tier_rem_min is not None:
            rem_min = int(tier_rem_min)
        else:
            rem_min = _gp.get_int(
                "eval_offset_remaining_min", None, default_rem_min
            )
        if tier_rem_max is not None:
            rem_max = int(tier_rem_max)
        else:
            rem_max = _gp.get_int(
                "eval_offset_remaining_max", None, default_rem_max
            )

        meta: dict = {
            prob_column: p,
            "tickformer_trade_signal": trade_signal,
            "eval_offset": eval_offset,
            "eval_offset_remaining": remaining,
            "up_threshold": up_threshold,
            "down_threshold": down_threshold,
            "eval_offset_remaining_min": rem_min,
            "eval_offset_remaining_max": rem_max,
            "asset": asset,
            "shadow_only": shadow_only,
            # window_ts: required for has_fill_for_strategy_window_direction.
            # See multi-pocket path comment above for full incident context.
            "window_ts": window_ts,
        }
        if tier_key:
            meta["tier"] = tier_key
        if mutex_group:
            meta["mutex_group"] = mutex_group

        # Guard: no remaining value at all → can't evaluate any band.
        if remaining is None:
            return _skip(
                "outside_eval_offset_remaining_band",
                meta,
                strategy_id=strategy_id,
                version=version,
            )

        # NOTE: the symmetric band check (remaining < rem_min or remaining > rem_max)
        # has been moved AFTER direction resolution so that per-direction bands
        # (eval_offset_remaining_{min,max}_{up,down}) can be applied.  See
        # _resolve_band_for_direction().  Back-compat: when no direction-specific
        # keys are present in the active params bag, rem_min/rem_max are used
        # unchanged for both directions.
        direction = None  # resolved below after the gate-cond check
        up_threshold = up_threshold  # already set above
        down_threshold = down_threshold
        # Carry symmetric band forward for _resolve_band_for_direction().
        _sym_min = rem_min
        _sym_max = rem_max

    # ── Gate-cond readiness check (PR-B, fix/tickformer-gate-cond-ready-gate) ──
    # The K=6 multi-step inference loop needs ≥69 ticks of lookback warmup
    # before it produces gate-cond-corrected probabilities.  Before that,
    # the emitted probability is the K=0 single-shot value — NOT the value
    # the model was trained against.  Trading on K=0 probability causes the
    # ~21 phantom crossings/hour observed pre-PR-A (probability looks high
    # because the model's calibration assumes gate-cond context).
    #
    # This gate is **off by default** (env var unset / "false") so PR-B can
    # land BEFORE PR-A (feat/tickformer-ready-flag-and-single-writer on the
    # timesfm repo) is merged and deployed.  Operator activation sequence:
    #   1. PR-A merges on timesfm repo.
    #   2. timesfm-service redeploys on classifier-gpu.
    #   3. Verify /v4/snapshot top-level has ``tickformer_gate_cond_ready: true``.
    #   4. Set TICKFORMER_REQUIRE_GATE_COND_READY=true on the engine box.
    #   5. Restart engine.  Gate activates; K=0 phantom crossings suppressed.
    #
    # When env is false (default), this block is a no-op — zero behaviour
    # change for ALL strategies, including non-tickformer / LGB / ETH / SOL / XRP.
    _require_ready = os.environ.get(
        "TICKFORMER_REQUIRE_GATE_COND_READY", "false"
    ).strip().lower() in ("1", "true", "yes", "on")
    if _require_ready:
        gate_cond_ready = bool(getattr(surface, "tickformer_gate_cond_ready", False))
        meta["tickformer_gate_cond_ready"] = gate_cond_ready
        if not gate_cond_ready:
            return _skip(
                "gate_cond_not_settled",
                meta,
                strategy_id=strategy_id,
                version=version,
            )

    # ── Direction resolution (legacy single-pocket path only) ───────────
    # In the multi-pocket path, direction was already resolved by
    # _match_pocket (and conviction_below_threshold was returned there).
    # In the legacy path, direction was set to None above and needs the
    # threshold comparison here.
    if not valid_pockets:
        if p >= up_threshold:
            direction = "UP"
        elif p <= down_threshold:
            direction = "DOWN"
        else:
            return _skip(
                "conviction_below_threshold",
                meta,
                strategy_id=strategy_id,
                version=version,
            )

        # ── Direction-aware band check (plan #760) ───────────────────
        # Now that direction is known, resolve the per-direction band and
        # check whether ``remaining`` falls inside it.  _resolve_band_for_direction
        # applies the waterfall:
        #   1. eval_offset_remaining_{min,max}_{up,down} from active params bag
        #   2. Symmetric eval_offset_remaining_{min,max} (_sym_min/_sym_max)
        # When no direction-specific keys are present, _sym_min/_sym_max are
        # returned unchanged — identical to the previous symmetric check.
        band_min, band_max = _resolve_band_for_direction(
            direction, sym_min=_sym_min, sym_max=_sym_max
        )
        meta["band_min"] = band_min
        meta["band_max"] = band_max
        if remaining < band_min or remaining > band_max:
            return _skip(
                f"outside_band_{direction} ({band_min}-{band_max})",
                meta,
                strategy_id=strategy_id,
                version=version,
            )

    # Trade-signal cross-check: only an explicit opposite label blocks.
    # HOLD / None pass through (the probability is the primary edge).
    if trade_signal in ("UP", "DOWN") and direction != trade_signal:
        meta["mismatch_trade_signal"] = trade_signal
        return _skip(
            "trade_signal_disagrees",
            meta,
            strategy_id=strategy_id,
            version=version,
        )

    meta["direction"] = direction

    min_consec = _gp.get_int(
        "min_consecutive_pass_ticks", None, _DEFAULT_MIN_CONSEC_TICKS
    )
    consec_count = _bump_and_check(strategy_id, window_ts or 0, direction)
    meta["consec_tick_count"] = consec_count
    meta["min_consecutive_pass_ticks"] = min_consec
    if consec_count < min_consec:
        return _skip(
            f"awaiting_consec_ticks ({consec_count}/{min_consec})",
            meta,
            strategy_id=strategy_id,
            version=version,
        )

    # Direction-aware fill-band gate (RDS note #664).
    # UP and DOWN are distinct CLOB tokens with independent order books.
    # Use the actual leg price for each direction's cap/floor — not the UP
    # price as a proxy for both. (Bug fixed 2026-05-29: prior version checked
    # UP price against DOWN cap, allowing NO buys at >$0.90 when UP was cheap.)
    fill_price = getattr(surface, "fill_price", None)
    if fill_price is None:
        fill_price = getattr(surface, "clob_implied_up", None)
    if fill_price is not None:
        try:
            fill_price = float(fill_price)
        except (TypeError, ValueError):
            fill_price = None
    down_fill_price = getattr(surface, "clob_down_ask", None)
    if down_fill_price is not None:
        try:
            down_fill_price = float(down_fill_price)
        except (TypeError, ValueError):
            down_fill_price = None
    if down_fill_price is None and fill_price is not None:
        # Fallback: synthesize NO price from UP leg (UP+DOWN ≈ 1.00 via arb).
        down_fill_price = 1.0 - fill_price
    if fill_price is not None:
        entry_floor_up = float(
            _gp.get_float("entry_floor_up", None, _DEFAULT_ENTRY_FLOOR_UP)
        )
        entry_cap_down = float(
            _gp.get_float("entry_cap_down", None, _DEFAULT_ENTRY_CAP_DOWN)
        )
        _efd_raw = _gp._lookup("entry_floor_down", None, None)
        entry_floor_down = float(_efd_raw) if _efd_raw is not None else None
        meta["fill_price"] = fill_price
        meta["down_fill_price"] = down_fill_price
        meta["entry_floor_up"] = entry_floor_up
        meta["entry_cap_down"] = entry_cap_down
        meta["entry_floor_down"] = entry_floor_down
        if direction == "UP" and fill_price < entry_floor_up:
            return _skip(
                f"fill_below_up_floor:{fill_price:.3f}<{entry_floor_up:.3f}",
                meta,
                strategy_id=strategy_id,
                version=version,
            )
        if (
            direction == "DOWN"
            and down_fill_price is not None
            and down_fill_price > entry_cap_down
        ):
            return _skip(
                f"fill_above_down_cap:{down_fill_price:.3f}>{entry_cap_down:.3f}",
                meta,
                strategy_id=strategy_id,
                version=version,
            )
        if (
            direction == "DOWN"
            and down_fill_price is not None
            and entry_floor_down is not None
            and down_fill_price < entry_floor_down
        ):
            return _skip(
                f"entry_floor_down ({down_fill_price:.3f} < {entry_floor_down})",
                meta,
                strategy_id=strategy_id,
                version=version,
            )

    confidence_score = float(abs(p - 0.5) * 2.0)
    confidence = "HIGH" if confidence_score >= 0.40 else "MODERATE"

    entry_cap = _gp.get_float("entry_cap", None, _DEFAULT_ENTRY_CAP)
    collateral_pct = _gp.get_float(
        "collateral_pct", None, _DEFAULT_COLLATERAL_PCT
    )
    gtc_cap = _gp.get_float("gtc_cap", None, _DEFAULT_GTC_CAP)
    meta["entry_cap"] = entry_cap
    meta["collateral_pct"] = collateral_pct
    meta["gtc_cap"] = gtc_cap
    meta["strategy_id"] = strategy_id
    meta["strategy_version"] = version

    # SHADOW kill switch — emit decision record but NEVER trade live.
    if shadow_only:
        meta["would_trade"] = True
        meta["would_direction"] = direction
        meta["would_confidence_score"] = confidence_score
        return _skip(
            "shadow_only_no_trade",
            meta,
            strategy_id=strategy_id,
            version=version,
        )

    return StrategyDecision(
        action="TRADE",
        direction=direction,
        confidence=confidence,
        confidence_score=confidence_score,
        entry_cap=entry_cap,
        collateral_pct=collateral_pct,
        gtc_cap=gtc_cap,
        strategy_id=strategy_id,
        strategy_version=version,
        entry_reason=entry_reason_label,
        skip_reason=None,
        metadata=meta,
    )
