"""Unit tests for the tickformer_v16/v17/v18 SHADOW-strategy family.

Validates that the post-FIX-1 thin wrappers behave identically to the
pre-refactor near-duplicate hooks: same SKIP reasons, same metadata
keys, same SHADOW kill-switch semantics. Each variant gets at least
two cases (HOLD signal path + opposite-signal block + missing-prob
SKIP + threshold-cross + eval_offset-out-of-band + gate not passed),
distributed across the 3 sister strategies for ~6+ total cases.

Mutex-group + tier-lookup coverage live in their own dedicated test
modules (added in FIX 2 / FIX 3 commits).
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
    """Explicit DOWN trade_signal while prob says UP → SKIP."""
    surface = _surface(
        "probability_tickformer_v17",
        0.92,
        eval_offset=200,
        trade_signal="DOWN",
    )
    with _gp_active({"shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "trade_signal_disagrees"
    assert dec.metadata["mismatch_trade_signal"] == "DOWN"


def test_v17_outside_eval_offset_remaining_band_skips():
    """Default v17 band is rem_max=140 (eval_offset >= 160 fails)."""
    # eval_offset=10 → remaining=290, far above rem_max=140.
    surface = _surface(
        "probability_tickformer_v17", 0.95, eval_offset=10
    )
    with _gp_active({"shadow_only": 0}):
        dec = evaluate_tickformer_v17_sniper(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "outside_eval_offset_remaining_band"


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
