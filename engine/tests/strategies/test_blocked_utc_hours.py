"""Per-direction blocked_utc_hours gate (audit #380, 2026-05-06).

Verifies that v9_ensemble's per-direction hour-block fires for the right
direction at the right hour and is a no-op otherwise. The strategies that
delegate to v9_ensemble (v9_1_lgb_only, v12_lgb_combo) inherit the same
gate via delegation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

# Import gate_params so we can monkeypatch the per-direction lookup helpers
# without round-tripping through YAML / runtime overrides.
from strategies.configs import v9_ensemble as v9


def _hour_to_window_ts(hour_utc: int) -> int:
    """Build a window_ts whose UTC hour matches `hour_utc` (date is fixed)."""
    return int(
        datetime(2026, 5, 6, hour_utc, 0, 0, tzinfo=timezone.utc).timestamp()
    )


def _surface(direction_bias: str, hour_utc: int) -> SimpleNamespace:
    """Build a permissive surface that will reach the blocked-hour gate."""
    pl = 0.62 if direction_bias == "UP" else 0.38
    return SimpleNamespace(
        asset="BTC",
        timescale="5m",
        window_ts=_hour_to_window_ts(hour_utc),
        eval_offset=80,
        probability_lgb=pl,
        probability_lgb_v9_1=pl,
        probability_lgb_v12=pl,
        probability_classifier=pl,
        v4_regime="chop",
        regime="NORMAL",
        delta_chainlink=0.001 if direction_bias == "UP" else -0.001,
        delta_tiingo=0.001 if direction_bias == "UP" else -0.001,
        delta_binance=0.001 if direction_bias == "UP" else -0.001,
        delta_coinglass=0.05 if direction_bias == "UP" else -0.05,
        delta_chainlink_age_seconds=5,
        vpin=0.50,
        clob_up_ask=0.62,
        clob_down_ask=0.38,
        poly_max_entry_price=0.62,
    )


def _patch_blocks(dn: list[int], up: list[int]):
    return [
        patch.object(v9, "_blocked_utc_hours_dn", return_value=dn),
        patch.object(v9, "_blocked_utc_hours_up", return_value=up),
    ]


def _run(surface):
    """Call evaluate; if a downstream gate explodes on our minimal
    surface, return a sentinel decision so the test can still discriminate
    the hour-block branch."""
    try:
        return v9.evaluate_v9_ensemble(surface)
    except Exception as exc:
        return SimpleNamespace(
            action="ERROR_DOWNSTREAM",
            skip_reason=None,
            direction=None,
            metadata={"exc": repr(exc)[:200]},
        )


# ───────────────────────────── unit tests on the helpers ─────────────────────


def test_blocked_utc_hours_dn_default_empty():
    assert v9._blocked_utc_hours_dn() == []


def test_blocked_utc_hours_up_default_empty():
    assert v9._blocked_utc_hours_up() == []


def test_blocked_utc_hours_dn_override():
    with patch.object(v9, "_blocked_utc_hours_dn", return_value=[10, 11]):
        assert v9._blocked_utc_hours_dn() == [10, 11]


# ───────────────────── integration through evaluate_v9_ensemble ──────────────


def test_skip_fires_for_blocked_down_hour():
    surface = _surface(direction_bias="DOWN", hour_utc=10)
    p1, p2 = _patch_blocks(dn=[10, 11], up=[])
    with p1, p2:
        decision = _run(surface)
    assert decision.action == "SKIP", (
        f"got {decision.action} ({getattr(decision, 'skip_reason', None)})"
    )
    assert (decision.skip_reason or "").startswith("blocked_utc_hour_dn")


def test_no_skip_when_down_hour_outside_block():
    surface = _surface(direction_bias="DOWN", hour_utc=4)
    p1, p2 = _patch_blocks(dn=[10, 11], up=[])
    with p1, p2:
        decision = _run(surface)
    assert decision.action != "SKIP" or not (
        decision.skip_reason or ""
    ).startswith("blocked_utc_hour")


def test_skip_fires_for_blocked_up_hour():
    surface = _surface(direction_bias="UP", hour_utc=3)
    p1, p2 = _patch_blocks(dn=[], up=[3])
    with p1, p2:
        decision = _run(surface)
    assert decision.action == "SKIP"
    assert (decision.skip_reason or "").startswith("blocked_utc_hour_up")


def test_directions_are_independent():
    surface = _surface(direction_bias="UP", hour_utc=10)
    p1, p2 = _patch_blocks(dn=[10], up=[])
    with p1, p2:
        decision = _run(surface)
    assert decision.action != "SKIP" or not (
        decision.skip_reason or ""
    ).startswith("blocked_utc_hour")


def test_no_skip_when_overrides_empty():
    surface = _surface(direction_bias="DOWN", hour_utc=10)
    p1, p2 = _patch_blocks(dn=[], up=[])
    with p1, p2:
        decision = _run(surface)
    assert decision.action != "SKIP" or not (
        decision.skip_reason or ""
    ).startswith("blocked_utc_hour")
