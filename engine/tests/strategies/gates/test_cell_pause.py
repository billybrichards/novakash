"""CellPauseGate (audits #379 + #385, 2026-05-06).

Verifies the gate consults its injected `lookup` callable, skips when the
cell is paused, passes when it isn't, and stays a no-op when no lookup is
wired (default boot mode).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

from strategies.gates.cell_pause import CellPauseGate


def _hour_to_window_ts(hour_utc: int) -> int:
    return int(
        datetime(2026, 5, 6, hour_utc, 0, 0, tzinfo=timezone.utc).timestamp()
    )


def _surface(direction="DOWN", hour=10, eval_offset=80, regime="chop"):
    return SimpleNamespace(
        direction=direction,
        v4_regime=regime,
        eval_offset=eval_offset,
        hour_utc=hour,
        window_ts=_hour_to_window_ts(hour),
    )


def _make_lookup(active_cells: dict):
    """active_cells is keyed by tuple (strategy, direction, t_band, regime, session).
    Returns the reason string, or None if not active."""

    def lookup(strategy, direction, t_band, regime, session) -> Optional[str]:
        return active_cells.get((strategy, direction, t_band, regime, session))

    return lookup


def test_gate_passes_when_no_lookup_wired():
    """Default boot mode: lookup=None → no-op PASS."""
    gate = CellPauseGate(strategy_id="v12_lgb_combo", lookup=None)
    res = gate.evaluate(_surface())
    assert res.passed
    assert "no lookup" in res.reason.lower()


def test_gate_skips_when_cell_is_paused():
    active = {
        # T-91-120 because eval_offset=100 → T-91-120 bucket.
        ("v12_lgb_combo", "DOWN", "T-91-120", "chop", "eu_am"): "60m_pnl=-$45",
    }
    gate = CellPauseGate(
        strategy_id="v12_lgb_combo", lookup=_make_lookup(active)
    )
    res = gate.evaluate(_surface(direction="DOWN", hour=10, eval_offset=100))
    assert not res.passed
    assert "60m_pnl" in res.reason


def test_gate_passes_when_no_active_pause_for_cell():
    gate = CellPauseGate(
        strategy_id="v12_lgb_combo", lookup=_make_lookup({})
    )
    res = gate.evaluate(_surface(direction="DOWN", hour=10, eval_offset=100))
    assert res.passed


def test_gate_only_skips_for_matching_strategy():
    """An active pause for v9_1 must not affect v12_combo evaluations."""
    active = {
        ("v9_1_lgb_only", "DOWN", "T-91-120", "chop", "eu_am"): "wilson_lb_breach",
    }
    gate = CellPauseGate(
        strategy_id="v12_lgb_combo", lookup=_make_lookup(active)
    )
    res = gate.evaluate(_surface(direction="DOWN", hour=10, eval_offset=100))
    assert res.passed


def test_gate_only_skips_for_matching_direction():
    active = {
        ("v12_lgb_combo", "DOWN", "T-91-120", "chop", "eu_am"): "wilson",
    }
    gate = CellPauseGate(
        strategy_id="v12_lgb_combo", lookup=_make_lookup(active)
    )
    # UP trade in same hour/regime → not matched.
    res = gate.evaluate(_surface(direction="UP", hour=10, eval_offset=100))
    assert res.passed


def test_gate_t_band_buckets_correctly():
    """Verify each t_band bucket maps to the right key."""
    cases = [
        (10, "T-0-30"),
        (45, "T-31-60"),
        (75, "T-61-90"),
        (100, "T-91-120"),
        (150, "T-121-180"),
        (200, "T-181-240"),
        (260, "T-241-300"),
    ]
    for offset, expected_t_band in cases:
        active = {
            ("v12_lgb_combo", "DOWN", expected_t_band, "chop", "eu_am"): "x",
        }
        gate = CellPauseGate(
            strategy_id="v12_lgb_combo", lookup=_make_lookup(active)
        )
        res = gate.evaluate(_surface(direction="DOWN", hour=10, eval_offset=offset))
        assert not res.passed, f"offset={offset} expected_t_band={expected_t_band}"


def test_gate_session_buckets_correctly():
    # Session labels use the 7-bucket system from Hub note #350.
    # Updated from the old 4-bucket system (eu_pm_us_am / us_pm_asian_am
    # no longer exist — see cell_bucketing.py mismatch note).
    cases = [
        (3, "asian_early"),  # was: "asian_late" in old 4-bucket system
        (8, "eu_am"),
        (14, "us_pm"),       # was: "eu_pm_us_am"
        (22, "off_hours"),   # was: "us_pm_asian_am"
    ]
    for hour, expected_session in cases:
        active = {
            ("v12_lgb_combo", "DOWN", "T-91-120", "chop", expected_session): "y",
        }
        gate = CellPauseGate(
            strategy_id="v12_lgb_combo", lookup=_make_lookup(active)
        )
        res = gate.evaluate(_surface(direction="DOWN", hour=hour, eval_offset=100))
        assert not res.passed, f"hour={hour} expected_session={expected_session}"


def test_gate_falls_open_on_lookup_exception():
    """Lookup raising must not break trading — gate fails OPEN with reason."""

    def bad_lookup(*args, **kwargs):
        raise RuntimeError("DB connection lost")

    gate = CellPauseGate(strategy_id="v12_lgb_combo", lookup=bad_lookup)
    res = gate.evaluate(_surface())
    assert res.passed
    assert "raised" in res.reason.lower()


def test_gate_resolves_direction_from_probability_when_field_missing():
    """When `surface.direction` is absent, gate falls back to probability_lgb."""
    surface = SimpleNamespace(
        v4_regime="chop",
        eval_offset=100,
        hour_utc=10,
        window_ts=_hour_to_window_ts(10),
        probability_lgb=0.62,  # > 0.5 → UP
    )
    active = {
        ("v12_lgb_combo", "UP", "T-91-120", "chop", "eu_am"): "x",
    }
    gate = CellPauseGate(
        strategy_id="v12_lgb_combo", lookup=_make_lookup(active)
    )
    res = gate.evaluate(surface)
    assert not res.passed


def test_gate_passes_when_direction_unresolvable():
    surface = SimpleNamespace(
        v4_regime="chop",
        eval_offset=100,
        hour_utc=10,
        window_ts=_hour_to_window_ts(10),
    )
    gate = CellPauseGate(
        strategy_id="v12_lgb_combo", lookup=_make_lookup({})
    )
    res = gate.evaluate(surface)
    assert res.passed
    assert "unresolved" in res.reason.lower()
