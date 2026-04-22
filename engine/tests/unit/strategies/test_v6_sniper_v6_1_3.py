"""Tests for v6_sniper v6.1.3 — EMERGENCY DOWN-only (UP blocked via impossible thresholds).

Incident (2026-04-22, Montreal live):
  * v6 UP direction: 0W/4L, -$16.70, 100% loss rate at fills 0.36-0.44.
    Classifier/ensemble appears systematically contrarian on UP today.
  * v6 DOWN direction: 1W/0L (100% WR).
  * Decision: block UP completely; keep DOWN trading.

Mechanism (YAML-only, no python hook changes):
  * up_bucket_abs_dist_strong: 0.28 → 1.01
      `is_agree_strong` requires |probability_up - 0.5| >= 1.01.
      Since |p-0.5| maxes at 0.5, this is unreachable.
  * up_bucket_path1_extreme_high: 0.95 → 1.01
      `is_pegged_path1` upper-bound requires p_path1 >= 1.01.
      Since path1 maxes at 1.0, this is unreachable.
  * `up_require_both_buckets: true` (unchanged v6.1.1) forces UP trades to
    satisfy BOTH is_agree_strong AND is_pegged_path1 — both impossible →
    UP always hits the `direction_asym_up_insufficient_conviction` skip.

DOWN direction uses the shared bucket knobs (0.22 / 0.92 / 0.08) and is
completely unaffected.

To re-enable UP: restore up_bucket_abs_dist_strong → 0.28 and
up_bucket_path1_extreme_high → 0.95 (v6.1.2 tuning).
"""

from __future__ import annotations

from pathlib import Path

import yaml

V6_YAML = (
    Path(__file__).resolve().parents[3]
    / "strategies"
    / "configs"
    / "v6_sniper.yaml"
)


def test_up_down_only_emergency_thresholds_block_up():
    """The two UP-only knobs must be set to impossible values (1.01) so UP
    trades cannot pass the direction_asym_up double-bucket gate.

    Regression guard — if a future tuning PR tries to re-enable UP without
    explicitly overriding both knobs to in-range values AND bumping the
    YAML version past 6.1.3, this test will fail and flag the escape.
    """
    data = yaml.safe_load(V6_YAML.read_text())
    gp = data["gate_params"]
    assert gp["up_bucket_abs_dist_strong"] == 1.01, (
        "emergency DOWN-only: up_bucket_abs_dist_strong must be 1.01 "
        f"(impossible); got {gp['up_bucket_abs_dist_strong']}"
    )
    assert gp["up_bucket_path1_extreme_high"] == 1.01, (
        "emergency DOWN-only: up_bucket_path1_extreme_high must be 1.01 "
        f"(impossible); got {gp['up_bucket_path1_extreme_high']}"
    )
    # Double-bucket requirement must still be on, otherwise EITHER condition
    # being True would let UP trade.
    assert gp["up_require_both_buckets"] is True, (
        "up_require_both_buckets must be True to make the emergency block "
        "work; got False"
    )


def test_down_thresholds_unchanged():
    """DOWN uses shared bucket knobs — v6.1.0/v6.1.2 values must be intact.

    Any accidental tightening/loosening of these would change DOWN behaviour,
    which is explicitly out of scope for the v6.1.3 emergency fix.
    """
    data = yaml.safe_load(V6_YAML.read_text())
    gp = data["gate_params"]
    assert gp["bucket_abs_dist_strong"] == 0.22, (
        f"DOWN bucket_abs_dist_strong must stay 0.22; got {gp['bucket_abs_dist_strong']}"
    )
    assert gp["bucket_path1_extreme_high"] == 0.92, (
        f"DOWN bucket_path1_extreme_high must stay 0.92; "
        f"got {gp['bucket_path1_extreme_high']}"
    )
    assert gp["bucket_path1_extreme_low"] == 0.08, (
        f"DOWN bucket_path1_extreme_low must stay 0.08; "
        f"got {gp['bucket_path1_extreme_low']}"
    )


def test_version_bumped_to_613():
    """YAML version must reflect the v6.1.3 emergency bump."""
    data = yaml.safe_load(V6_YAML.read_text())
    assert data["version"] == "6.1.3", (
        f"expected 6.1.3 (emergency DOWN-only), got {data['version']}"
    )


def test_prior_safety_patches_preserved():
    """v6.1.3 is additive — prior Montreal safety patches must stay in place."""
    data = yaml.safe_load(V6_YAML.read_text())
    gp = data["gate_params"]
    assert gp["risk_off_override_enabled"] is False, (
        "risk_off_override_enabled must stay False (Montreal hotfix); "
        f"got {gp['risk_off_override_enabled']}"
    )
    assert gp["tradeable_v4_regimes"] == ["volatile_trend"], (
        "tradeable_v4_regimes must stay ['volatile_trend'] (Montreal parity); "
        f"got {gp['tradeable_v4_regimes']}"
    )
    assert gp["max_offset_sec"] == 200, (
        f"max_offset_sec must stay 200; got {gp['max_offset_sec']}"
    )
