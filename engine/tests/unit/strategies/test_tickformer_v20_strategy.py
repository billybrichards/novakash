"""Unit tests for the tickformer_v20_adaptive_early SHADOW strategy.

Mirrors the v16/v17/v18 test patterns in test_tickformer_strategies.py.
Coverage:
  - happy-path UP signal at prob > 0.90 fires SHADOW (would_trade=True)
  - below-threshold (0.89) SKIP with conviction_below_threshold reason
  - eval_offset out-of-band SKIP with outside_eval_offset_remaining_band
  - shadow_only=0 + prob >= 0.90 allows TRADE direction=UP
  - missing probability column SKIPs cleanly (forward-compat sanity)
  - shadow-record metadata contract (downstream Strategy Lab keys)
"""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

# Engine root path (mirrors test_tickformer_strategies.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

os.environ.setdefault(
    "DATABASE_URL", "postgresql://test:test@localhost:5432/test"
)

from strategies import gate_params as _gp  # noqa: E402
from strategies.configs import _tickformer_base  # noqa: E402
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
    prob_value,
    *,
    eval_offset: int = 80,
    trade_signal=None,
    asset: str = "BTC",
    window_ts: int = 1779000000,
    fill_price: float = 0.70,
):
    """Build a minimal SimpleNamespace surface for v20.

    eval_offset=80 puts eval_offset_remaining=220 — well within v20's
    [60, 240] default band and near the t-217s expected fire eo.
    """
    ns = SimpleNamespace(
        asset=asset,
        window_ts=window_ts,
        eval_offset=eval_offset,
        tickformer_trade_signal=trade_signal,
        clob_implied_up=fill_price,
        fill_price=fill_price,
    )
    setattr(ns, "probability_tickformer_v20", prob_value)
    ns.window_ts = window_ts + int(time.time_ns() % 1_000_000)
    return ns


# ── Happy path ───────────────────────────────────────────────────────


def test_v20_happy_path_up_signal_at_prob_above_threshold_fires_shadow():
    """prob 0.92 at fire-eo ~220s with shadow_only=1 → SKIP w/ would_trade=True."""
    surface = _surface(0.92, eval_offset=80)
    # Default gate_params from base → shadow_only=1.
    dec = evaluate_tickformer_v20_adaptive_early(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "shadow_only_no_trade"
    assert dec.metadata["would_trade"] is True
    assert dec.metadata["would_direction"] == "UP"
    assert dec.strategy_id == "tickformer_v20_adaptive_early"


# ── Threshold gating ─────────────────────────────────────────────────


def test_v20_prob_below_threshold_skips_conviction():
    """prob 0.89 < default up_threshold 0.90 → conviction_below_threshold."""
    surface = _surface(0.89, eval_offset=80)
    with _gp_active({"shadow_only": 0}):
        dec = evaluate_tickformer_v20_adaptive_early(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "conviction_below_threshold"


# ── Eval-offset band gating ──────────────────────────────────────────


def test_v20_eval_offset_out_of_band_skips():
    """eval_offset=30 → remaining=270, above rem_max=240 → out-of-band."""
    surface = _surface(0.95, eval_offset=30)
    with _gp_active({"shadow_only": 0}):
        dec = evaluate_tickformer_v20_adaptive_early(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "outside_eval_offset_remaining_band"


# ── shadow_only=0 + LIVE path fires ──────────────────────────────────


def test_v20_shadow_only_off_with_prob_above_threshold_fires_up():
    """shadow_only=0 + prob 0.93 in-band → TRADE UP."""
    surface = _surface(0.93, eval_offset=80, fill_price=0.70)
    with _gp_active({"shadow_only": 0}):
        dec = evaluate_tickformer_v20_adaptive_early(surface)
    assert dec.action == "TRADE", dec.skip_reason
    assert dec.direction == "UP"
    assert dec.strategy_id == "tickformer_v20_adaptive_early"
    assert dec.entry_reason == "tickformer_v20_adaptive_early_pass"


# ── Forward-compat: missing prob column ──────────────────────────────


def test_v20_missing_probability_skips_not_available():
    """probability_tickformer_v20=None → forward-compatible SKIP."""
    surface = _surface(None, eval_offset=80)
    dec = evaluate_tickformer_v20_adaptive_early(surface)
    assert dec.action == "SKIP"
    assert dec.skip_reason == "tickformer_v20_not_available"
    assert dec.metadata["probability_tickformer_v20"] is None


# ── Shadow-record metadata contract ──────────────────────────────────


def test_v20_shadow_record_metadata_has_required_keys():
    """Decision-record metadata contract for downstream Strategy Lab."""
    surface = _surface(0.95, eval_offset=80)
    dec = evaluate_tickformer_v20_adaptive_early(surface)
    assert dec.action == "SKIP"
    for key in (
        "probability_tickformer_v20",
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
