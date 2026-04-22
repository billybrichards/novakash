"""Tests for v6_sniper v6.1.4 — surgical UP re-enable via fill-price floor.

This file originally covered the v6.1.3 emergency DOWN-only block (impossible
thresholds 1.01/1.01). v6.1.4 replaces that blanket block with a surgical
fill-price gate informed by n=50 resolved UP trades (Montreal live,
2026-04-20 → 2026-04-22):

    UP fill < 0.55 / volatile_trend: 2W/8L → 20% WR, -$18.54, ROI -60%.
    UP fill ≥ 0.55 / volatile_trend: 12W/3L → 80% WR, +$8.39,  ROI +12%.

Today's 4 UP losses (fills 0.36-0.44) all land in the LOSER bucket → all
blocked by `up_min_fill_price: 0.55`.

Guards here:
  * v6.1.3 impossible thresholds REVERTED (0.28 / 0.95 — v6.1.2 tuning).
  * NEW `up_min_fill_price` knob present and set to 0.55.
  * DOWN shared knobs unchanged (0.22 / 0.92 / 0.08).
  * Version bumped past 6.1.3.
  * Prior Montreal safety patches preserved.
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


def test_up_bucket_thresholds_reverted_from_emergency():
    """v6.1.3 used impossible 1.01/1.01 thresholds as an emergency UP block.
    v6.1.4 replaces that with a surgical fill-price gate, so the impossible
    thresholds MUST be reverted to v6.1.2 values (0.28 / 0.95).
    """
    data = yaml.safe_load(V6_YAML.read_text())
    gp = data["gate_params"]
    assert gp["up_bucket_abs_dist_strong"] == 0.28, (
        "v6.1.4 must revert emergency up_bucket_abs_dist_strong to 0.28 "
        f"(v6.1.2 tuning); got {gp['up_bucket_abs_dist_strong']}"
    )
    assert gp["up_bucket_path1_extreme_high"] == 0.95, (
        "v6.1.4 must revert emergency up_bucket_path1_extreme_high to 0.95 "
        f"(v6.1.2 tuning); got {gp['up_bucket_path1_extreme_high']}"
    )
    # Double-bucket requirement unchanged — stricter UP filter still applies.
    assert gp["up_require_both_buckets"] is True, (
        "up_require_both_buckets must stay True (v6.1.1 direction-asym filter); "
        f"got {gp['up_require_both_buckets']}"
    )


def test_up_min_fill_price_knob_present():
    """v6.1.4 surgical gate: UP trades below a CLOB ask floor are skipped.

    Data: UP fill < 0.55 was 20% WR / -60% ROI (loser bucket). All 4 of
    today's UP losses were at fills 0.36-0.44 and fall below this floor.
    """
    data = yaml.safe_load(V6_YAML.read_text())
    gp = data["gate_params"]
    assert "up_min_fill_price" in gp, (
        "v6.1.4 must add up_min_fill_price knob to gate_params"
    )
    value = gp["up_min_fill_price"]
    # Sanity bounds — must be in (0, 1) and >= 0.50 for meaningful coverage.
    assert 0.50 <= value < 1.0, (
        f"up_min_fill_price must be in [0.50, 1.0); got {value}"
    )
    # Specifically the data-derived 0.55 target.
    assert value == 0.55, (
        f"v6.1.4 spec sets up_min_fill_price to 0.55; got {value}"
    )


def test_down_thresholds_unchanged():
    """DOWN uses shared bucket knobs — v6.1.0/v6.1.2 values intact."""
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


def test_version_bumped_past_613():
    """YAML version must be >= 6.1.4 (past the v6.1.3 emergency block)."""
    data = yaml.safe_load(V6_YAML.read_text())
    parts = tuple(int(x) for x in str(data["version"]).split("."))
    assert parts >= (6, 1, 4), (
        f"expected version >= 6.1.4 (v6.1.4 surgical UP re-enable), "
        f"got {data['version']}"
    )


def test_prior_safety_patches_preserved():
    """v6.1.4 is additive — prior Montreal safety patches stay in place."""
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
