"""Unit tests for the tickformer_v16/v17/v18/v20 SHADOW-strategy family.

Validates that the post-FIX-1 thin wrappers behave identically to the
pre-refactor near-duplicate hooks: same SKIP reasons, same metadata
keys, same SHADOW kill-switch semantics. Each variant gets at least
two cases (HOLD signal path + opposite-signal block + missing-prob
SKIP + threshold-cross + eval_offset-out-of-band + gate not passed),
distributed across the 3 sister strategies for ~6+ total cases.

Mutex-group + tier-lookup coverage live in their own dedicated test
modules (test_mutex_resolver.py + test_tickformer_tier_lookup.py).
"""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

# Engine root path (mirrors test_v9_5_xrp_late_band_AB_blend.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

os.environ.setdefault(
    "DATABASE_URL", "postgresql://test:test@localhost:5432/test"
)

from strategies import gate_params as _gp  # noqa: E402
from strategies.configs import _tickformer_base  # noqa: E402
from strategies.configs.tickformer_v16_pure import (  # noqa: E402
    evaluate_tickformer_v16_pure,
)
from strategies.configs.tickformer_v17_sniper import (  # noqa: E402
    evaluate_tickformer_v17_sniper,
)
from strategies.configs.tickformer_v18_t180 import (  # noqa: E402
    evaluate_tickformer_v18_t180,
)
from strategies.configs.tickformer_v20_adaptive_early import (  # noqa: E402
    evaluate_tickformer_v20_adaptive_early,
)


@contextmanager
def _gp_active(params):
    """Bind gate_params for the duration of the context."""
    token = _gp.set_active(params)
    try:
        yield
    finally:
        _gp.reset_active(token)


@pytest.fixture(autouse=True)
def _reset_consec_state():
    """Each test starts with a clean consec-tick counter."""
    _tickformer_base._consec_state.clear()
    yield
    _tickformer_base._consec_state.clear()


def _surface(
    prob_field: str,
    prob_value,
    *,
    eval_offset: int = 120,
    trade_signal=None,
    asset: str = "BTC",
    window_ts: int = 1779000000,
    fill_price: float = 0.70,
):
    """Build a minimal SimpleNamespace surface.

    The hooks read everything via ``getattr(..., default=None)`` so a
    bare SimpleNamespace stands in for FullDataSurface without needing
    to populate ~80 unrelated fields.
    """
    ns = SimpleNamespace(
        asset=asset,
        window_ts=window_ts,
        eval_offset=eval_offset,
        tickformer_trade_signal=trade_signal,
        clob_implied_up=fill_price,
        fill_price=fill_price,
    )
    setattr(ns, prob_field, prob_value)
    # Unique window_ts so consec counters don't bleed across tests.
    ns.window_ts = window_ts + int(time.time_ns() % 1_000_000)
    return ns


# ── HOLD signal path (v16) ────────────────────────────────────────────


def test_v16_hold_signal_does_not_block_up_trade():
    """HOLD on tickformer_trade_signal passes through; probability wins."""
    surface = _surface(
        "probability_tickformer_v16",
        0.92,
        eval_offset=180,
        trade_signal="HOLD",
    )
    with _gp_active({"shadow_only": 0}):
        dec = evaluate_tickformer_v16_pure(surface)
    assert dec.action == "TRADE"
    assert dec.direction == "UP"
    assert dec.strategy_id == "tickformer_v16_pure"
    assert dec.entry_reason == "tickformer_v16_pure_pass"


def test_v16_missing_probability_skips_not_available():
    surface = _surface("probability_tickformer_v16", None)
    dec = evaluate_tickformer_v16_pure(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "tickformer_v16_not_available"
    assert dec.metadata["probability_tickformer_v16"] is None


# ── Opposite-signal block (v17) ───────────────────────────────────────


def test_v17_opposite_trade_signal_blocks_up():
    """Explicit DOWN trade_signal while prob says UP → SKIP.

    eval_offset=100 → remaining=100, inside v17's sniper band [60,140],
    so the evaluation reaches the trade-signal cross-check gate.
    (Old value was eval_offset=200 which placed remaining=200 outside
    rem_max=140 after the Bug E formula fix.)
    """
    surface = _surface(
        "probability_tickformer_v17",
        0.92,
        eval_offset=100,
        trade_signal="DOWN",
    )
    with _gp_active({"shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "trade_signal_disagrees"
    assert dec.metadata["mismatch_trade_signal"] == "DOWN"


def test_v17_outside_eval_offset_remaining_band_skips():
    """Default v17 band is rem_min=60, rem_max=140. eval_offset=10 → remaining=10 < 60.

    After plan #760 the skip reason is direction-aware: p=0.95 ≥ up_threshold=0.85
    resolves direction=UP first, then the band check emits outside_band_UP.
    """
    # eval_offset=10 means 10 seconds remain → below rem_min=60.
    surface = _surface(
        "probability_tickformer_v17", 0.95, eval_offset=10
    )
    with _gp_active({"shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "SKIP"
    # Direction is resolved before the band check; skip_reason encodes direction.
    assert "outside_band_UP" in dec.skip_reason, dec.skip_reason


# ── v18 cases ─────────────────────────────────────────────────────────


def test_v18_below_threshold_skips_conviction():
    """Default v18 up_threshold is 0.90."""
    surface = _surface(
        "probability_tickformer_v18", 0.85, eval_offset=180
    )
    with _gp_active({"shadow_only": 0}):
        dec = evaluate_tickformer_v18_t180(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "conviction_below_threshold"


def test_v18_down_threshold_crosses_to_trade_when_shadow_off():
    """Symmetric DOWN at p <= 0.10 fires DOWN when shadow_only=0."""
    surface = _surface(
        "probability_tickformer_v18",
        0.05,
        eval_offset=180,
        trade_signal=None,
        fill_price=0.20,  # below entry_cap_down=0.90, so DOWN allowed
    )
    with _gp_active({"shadow_only": 0}):
        dec = evaluate_tickformer_v18_t180(surface)
    assert dec.action == "TRADE", dec.skip_reason
    assert dec.direction == "DOWN"
    assert dec.strategy_id == "tickformer_v18_t180"


def test_v18_shadow_only_default_returns_skip_with_would_trade():
    """SHADOW kill switch: shadow_only=1 (default) → SKIP w/ record."""
    surface = _surface(
        "probability_tickformer_v18", 0.95, eval_offset=180
    )
    # No gate_params override → default shadow_only=1 from base.
    dec = evaluate_tickformer_v18_t180(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "shadow_only_no_trade"
    assert dec.metadata["would_trade"] is True
    assert dec.metadata["would_direction"] == "UP"


def test_v16_shadow_record_metadata_has_required_keys():
    """Decision-record metadata contract for downstream Strategy Lab."""
    surface = _surface(
        "probability_tickformer_v16", 0.95, eval_offset=180
    )
    dec = evaluate_tickformer_v16_pure(surface)
    # SHADOW SKIP (default).
    assert dec.action == "SKIP"
    for key in (
        "probability_tickformer_v16",
        "tickformer_trade_signal",
        "eval_offset",
        "eval_offset_remaining",
        "up_threshold",
        "down_threshold",
        "asset",
        "shadow_only",
        "would_trade",
        "would_direction",
        "strategy_id",
        "strategy_version",
    ):
        assert key in dec.metadata, key


# ── Bug E: _eval_offset_remaining formula correctness ─────────────────
# eval_offset IS seconds_to_close (seconds remaining). The old formula
# computed 300 - eval_offset, producing the mirror image of the correct
# band. After the fix, remaining = eval_offset directly.
# (fix/tickformer-strategies-actually-fire)


def test_eval_offset_remaining_is_eval_offset_not_300_minus():
    """eval_offset_remaining == eval_offset (seconds remaining), not 300-eval_offset."""
    surface = _surface("probability_tickformer_v16", 0.92, eval_offset=150)
    remaining = _tickformer_base._eval_offset_remaining(surface)
    # Must equal the eval_offset (150s remaining), NOT 300-150=150 (coincidence)
    # so let's use an odd value.
    surface2 = _surface("probability_tickformer_v16", 0.92, eval_offset=73)
    remaining2 = _tickformer_base._eval_offset_remaining(surface2)
    assert remaining2 == 73, f"expected 73 (eval_offset), got {remaining2}"


def test_eval_offset_remaining_prefers_explicit_surface_field():
    """If surface has eval_offset_remaining set, it wins over eval_offset."""
    surface = _surface("probability_tickformer_v16", 0.92, eval_offset=73)
    surface.eval_offset_remaining = 200  # explicit field present
    remaining = _tickformer_base._eval_offset_remaining(surface)
    assert remaining == 200


def test_v17_in_band_fires_after_formula_fix():
    """v17 rem_min=60, rem_max=140. eval_offset=100 → remaining=100, in-band → TRADE."""
    surface = _surface(
        "probability_tickformer_v17",
        0.92,
        eval_offset=100,
        trade_signal=None,
    )
    with _gp_active({"shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    # eval_offset=100 → remaining=100, in [60,140] → passes band gate.
    assert dec.action == "TRADE", f"expected TRADE, got SKIP({dec.skip_reason})"
    assert dec.direction == "UP"


def test_v17_low_eval_offset_outside_band_skips():
    """eval_offset=20 → remaining=20 < rem_min=60.

    After plan #760 the skip reason is direction-aware: p=0.92 ≥ up_threshold=0.85
    resolves direction=UP first, then the band check emits outside_band_UP.
    """
    surface = _surface(
        "probability_tickformer_v17",
        0.92,
        eval_offset=20,
    )
    with _gp_active({"shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "SKIP"
    # Direction resolved before band check; skip_reason encodes direction + limits.
    assert "outside_band_UP" in dec.skip_reason, dec.skip_reason
    # Remaining should be 20, below rem_min=60.
    assert dec.metadata["eval_offset_remaining"] == 20


# ── Bug A: asset=ANY registry guard (unit-level check on base) ─────────
# The registry fix is in registry.py and tested there. This test
# validates the strategy itself evaluates correctly for non-BTC assets
# (the base never checks asset — that was always correct; the registry
# was the broken gatekeeper).


def test_v18_evaluates_on_eth_asset():
    """Strategy logic is asset-agnostic; ETH surfaces evaluate cleanly."""
    surface = _surface(
        "probability_tickformer_v18",
        0.95,
        eval_offset=120,
        asset="ETH",
    )
    dec = evaluate_tickformer_v18_t180(surface)
    # SHADOW kill switch active (default shadow_only=1).
    assert dec.action == "SKIP"
    assert dec.skip_reason == "shadow_only_no_trade"
    assert dec.metadata["asset"] == "ETH"


# ── v20 strategy scaffold ─────────────────────────────────────────────
# (fix/tickformer-strategies-actually-fire)


def test_v20_missing_probability_skips_not_available():
    """v20 prob column not yet emitted → clean not_available SKIP."""
    surface = _surface("probability_tickformer_v20", None)
    dec = evaluate_tickformer_v20_adaptive_early(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "tickformer_v20_not_available"
    assert dec.metadata["probability_tickformer_v20"] is None


def test_v20_in_band_fires_when_shadow_off():
    """v20 rem_min=120, rem_max=280. eval_offset=200, p=0.93, shadow_off → TRADE."""
    surface = _surface(
        "probability_tickformer_v20",
        0.93,
        eval_offset=200,
        trade_signal=None,
    )
    with _gp_active({"shadow_only": 0}):
        dec = evaluate_tickformer_v20_adaptive_early(surface)
    assert dec.action == "TRADE", f"expected TRADE, got SKIP({dec.skip_reason})"
    assert dec.direction == "UP"
    assert dec.strategy_id == "tickformer_v20_adaptive_early"


def test_v20_shadow_only_default_skips_with_record():
    """SHADOW kill switch enforced by default shadow_only=1."""
    surface = _surface(
        "probability_tickformer_v20",
        0.95,
        eval_offset=200,
    )
    dec = evaluate_tickformer_v20_adaptive_early(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "shadow_only_no_trade"
    assert dec.metadata["would_trade"] is True


# ── gate_cond_not_settled gate (PR-B) ─────────────────────────────────
# The TICKFORMER_REQUIRE_GATE_COND_READY env var gates the K=6 loop
# readiness check.  Default is "false" so all existing behaviour is
# unchanged when the env var is absent.  Three contract cases:
#   A. env=true + ready=False  → SKIP(gate_cond_not_settled)
#   B. env=true + ready=True   → gate passes, evaluation continues
#   C. env=false (default)     → gate is a no-op regardless of ready flag


def _surface_with_ready(
    prob_value: float = 0.92,
    *,
    ready: bool = False,
    eval_offset: int = 180,
):
    """Build a surface that carries the ``tickformer_gate_cond_ready`` flag."""
    ns = _surface(
        "probability_tickformer_v16",
        prob_value,
        eval_offset=eval_offset,
        trade_signal=None,
    )
    ns.tickformer_gate_cond_ready = ready
    # Unique window_ts already set by _surface helper.
    return ns


def test_gate_cond_not_settled_skips_when_env_true_and_ready_false(monkeypatch):
    """env=true + surface.tickformer_gate_cond_ready=False → gate_cond_not_settled."""
    monkeypatch.setenv("TICKFORMER_REQUIRE_GATE_COND_READY", "true")
    surface = _surface_with_ready(ready=False)
    with _gp_active({"shadow_only": 0}):
        dec = evaluate_tickformer_v16_pure(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "gate_cond_not_settled"
    assert dec.metadata.get("tickformer_gate_cond_ready") is False


def test_gate_cond_ready_passes_gate_when_env_true_and_ready_true(monkeypatch):
    """env=true + surface.tickformer_gate_cond_ready=True → gate passes, reaches shadow."""
    monkeypatch.setenv("TICKFORMER_REQUIRE_GATE_COND_READY", "true")
    surface = _surface_with_ready(ready=True, eval_offset=180)
    # shadow_only=1 (default) so we expect shadow_only_no_trade after the gate
    # passes — confirming that evaluation continued past gate_cond_not_settled.
    dec = evaluate_tickformer_v16_pure(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "shadow_only_no_trade", (
        f"Expected shadow_only_no_trade (gate passed), got {dec.skip_reason!r}"
    )
    assert dec.metadata.get("tickformer_gate_cond_ready") is True


def test_gate_cond_ready_backwards_compat_when_env_false(monkeypatch):
    """env=false (default) → gate is a no-op, strategy evaluates normally."""
    monkeypatch.setenv("TICKFORMER_REQUIRE_GATE_COND_READY", "false")
    # ready=False but gate is off → should still reach shadow_only_no_trade
    surface = _surface_with_ready(ready=False, eval_offset=180)
    dec = evaluate_tickformer_v16_pure(surface)
    # Default shadow_only=1 so it hits the shadow kill-switch, not gate_cond.
    assert dec.action == "SKIP"
    assert dec.skip_reason == "shadow_only_no_trade", (
        f"Expected shadow_only_no_trade (env=false, gate is no-op), got {dec.skip_reason!r}"
    )
    # gate_cond_ready should NOT appear in metadata when the env gate is off.
    assert "tickformer_gate_cond_ready" not in dec.metadata


def test_gate_cond_env_absent_is_treated_as_false(monkeypatch):
    """Env var absent → treated as false → gate is a no-op (backwards compat)."""
    monkeypatch.delenv("TICKFORMER_REQUIRE_GATE_COND_READY", raising=False)
    surface = _surface_with_ready(ready=False, eval_offset=180)
    dec = evaluate_tickformer_v16_pure(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "shadow_only_no_trade", (
        f"Expected shadow_only_no_trade (env absent = no-op), got {dec.skip_reason!r}"
    )


def test_gate_cond_applies_to_v17_and_v18_too(monkeypatch):
    """The gate is in the shared base — all sister strategies inherit it."""
    monkeypatch.setenv("TICKFORMER_REQUIRE_GATE_COND_READY", "true")

    surface_v17 = _surface("probability_tickformer_v17", 0.92, eval_offset=100)
    surface_v17.tickformer_gate_cond_ready = False
    with _gp_active({"shadow_only": 0}):
        dec_v17 = evaluate_tickformer_v17_sniper(surface_v17)
    assert dec_v17.skip_reason == "gate_cond_not_settled"

    surface_v18 = _surface("probability_tickformer_v18", 0.95, eval_offset=180)
    surface_v18.tickformer_gate_cond_ready = False
    with _gp_active({"shadow_only": 0}):
        dec_v18 = evaluate_tickformer_v18_t180(surface_v18)
    assert dec_v18.skip_reason == "gate_cond_not_settled"
