"""Tests for the ``gate_params.prob_column`` override in _tickformer_base.

Lets a strategy YAML / runtime_overrides row swap the source surface
attribute (e.g. ``probability_tickformer_v18`` → ``calibrated_probability_
tickformer_v18``) without editing the Python module.

Hub note #842 — BTC tickformer iso migration plan.

Covers:
  1. No override set → reads default prob_column (back-compat unchanged).
  2. Override set to a sibling cal column → reads that instead, fires.
  3. Override set but surface lacks that attribute → SKIP with
     not_available_reason (matches the existing missing-column path).
  4. Empty-string override → ignored, falls through to default.
"""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

os.environ.setdefault(
    "DATABASE_URL", "postgresql://test:test@localhost:5432/test"
)

from strategies import gate_params as _gp  # noqa: E402
from strategies.configs import _tickformer_base  # noqa: E402
from strategies.configs.tickformer_v18_t180 import (  # noqa: E402
    evaluate_tickformer_v18_t180,
)


@contextmanager
def _gp_active(params):
    token = _gp.set_active(params)
    try:
        yield
    finally:
        _gp.reset_active(token)


@pytest.fixture(autouse=True)
def _reset_state():
    _tickformer_base._consec_state.clear()
    _tickformer_base._DOWN_SYMMETRY_WARNED.discard("tickformer_v18_t180")
    yield
    _tickformer_base._consec_state.clear()


def _surface(eval_offset: int = 150, **prob_attrs):
    """Build a minimal surface with whatever probability attributes are passed.

    e.g. ``_surface(probability_tickformer_v18=0.95)`` for raw,
         ``_surface(calibrated_probability_tickformer_v18=0.05)`` for cal,
         ``_surface(probability_tickformer_v18=0.9, calibrated_probability_tickformer_v18=0.05)``
         for both (override should pick cal).
    """
    ns = SimpleNamespace(
        asset="BTC",
        window_ts=int(time.time_ns() % 1_000_000) + 1_779_000_000,
        eval_offset=eval_offset,
        tickformer_trade_signal=None,
        clob_implied_up=0.85,
        fill_price=0.85,
    )
    for k, v in prob_attrs.items():
        setattr(ns, k, v)
    return ns


class TestProbColumnOverride:
    def test_no_override_reads_default_prob_column(self):
        """Without gate_params.prob_column, falls back to passed default
        (probability_tickformer_v18). Back-compat unchanged."""
        with _gp_active(
            {
                "up_threshold": 0.85,
                "down_threshold": 0.10,
                "shadow_only": 0,
                "min_consecutive_pass_ticks": 1,
                "eval_offset_remaining_max": 220,
                "eval_offset_remaining_min": 0,
                # NOTE: no prob_column override
            }
        ):
            d = evaluate_tickformer_v18_t180(
                _surface(probability_tickformer_v18=0.95)
            )
        assert d.action == "TRADE", f"got {d.action}: {d.skip_reason}"
        assert d.direction == "UP"
        # Meta carries the column we actually read from — confirms no override.
        assert "probability_tickformer_v18" in (d.metadata or {})

    def test_override_to_calibrated_column_fires_on_that(self):
        """With gate_params.prob_column='calibrated_probability_tickformer_v18',
        the evaluator reads the cal column instead. raw=0.90 (would have fired
        UP under default) is ignored; cal=0.05 fires DOWN per cal threshold."""
        with _gp_active(
            {
                "prob_column": "calibrated_probability_tickformer_v18",
                "up_threshold": 0.85,
                "down_threshold": 0.10,
                "shadow_only": 0,
                "min_consecutive_pass_ticks": 1,
                "eval_offset_remaining_max": 220,
                "eval_offset_remaining_min": 0,
            }
        ):
            d = evaluate_tickformer_v18_t180(
                _surface(
                    probability_tickformer_v18=0.90,
                    calibrated_probability_tickformer_v18=0.05,
                )
            )
        assert d.action == "TRADE", f"got {d.action}: {d.skip_reason}"
        assert d.direction == "DOWN"
        assert "calibrated_probability_tickformer_v18" in (d.metadata or {})

    def test_override_to_missing_column_skips_not_available(self):
        """Override set to a column the surface lacks → clean SKIP via the
        existing not-available path, no exception."""
        with _gp_active(
            {
                "prob_column": "calibrated_probability_tickformer_v18",
                "up_threshold": 0.85,
                "down_threshold": 0.10,
                "shadow_only": 0,
                "min_consecutive_pass_ticks": 1,
                "eval_offset_remaining_max": 220,
                "eval_offset_remaining_min": 0,
            }
        ):
            # Surface has raw but NOT the cal column. Override points at
            # cal — expect not_available SKIP not an exception.
            d = evaluate_tickformer_v18_t180(
                _surface(probability_tickformer_v18=0.90)
            )
        assert d.action == "SKIP"
        assert d.skip_reason == "tickformer_v18_not_available"

    def test_empty_string_override_falls_back_to_default(self):
        """Empty-string prob_column behaves identically to no override.
        Same back-compat as a deleted runtime_overrides row."""
        with _gp_active(
            {
                "prob_column": "",  # empty / unset semantics
                "up_threshold": 0.85,
                "down_threshold": 0.10,
                "shadow_only": 0,
                "min_consecutive_pass_ticks": 1,
                "eval_offset_remaining_max": 220,
                "eval_offset_remaining_min": 0,
            }
        ):
            d = evaluate_tickformer_v18_t180(
                _surface(probability_tickformer_v18=0.95)
            )
        assert d.action == "TRADE", f"got {d.action}: {d.skip_reason}"
        assert "probability_tickformer_v18" in (d.metadata or {})
