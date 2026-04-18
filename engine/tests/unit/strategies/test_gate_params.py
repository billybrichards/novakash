"""Unit tests for strategies.gate_params contextvar + registry wiring.

Covers:
  1. get_* helpers return YAML override when present, fall back to env,
     else to default.
  2. set_active/reset_active scope params correctly — a second call
     doesn't leak into the first's bag.
  3. Registry binds config.gate_params around hook invocation so the
     same shared hook sees strategy-specific overrides.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from strategies import gate_params
from strategies.data_surface import DataSurfaceManager, FullDataSurface
from strategies.registry import StrategyConfig, StrategyRegistry


def _make_surface(**overrides) -> FullDataSurface:
    """Minimal FullDataSurface builder for these tests.

    Mirrors the field coverage of ``tests/unit/strategies/test_strategy_configs
    ._make_surface`` without importing it — cross-test-module imports make
    pytest treat the worktree root as a rootdir candidate (breaks
    ``tests/test_no_dual_import.py``).
    """
    defaults: dict[str, Any] = dict(
        asset="BTC", timescale="5m", window_ts=1713000000,
        eval_offset=120, assembled_at=time.time(),
        current_price=84500.0, open_price=84000.0,
        delta_binance=0.005, delta_tiingo=0.004, delta_chainlink=0.005,
        delta_pct=0.004, delta_source="tiingo_rest_candle",
        vpin=0.55, regime="NORMAL", twap_delta=0.003,
        v2_probability_up=0.38, v2_probability_raw=0.36,
        v2_quantiles_p10=None, v2_quantiles_p50=None, v2_quantiles_p90=None,
        v3_5m_composite=None, v3_15m_composite=None, v3_1h_composite=None,
        v3_4h_composite=None, v3_24h_composite=None, v3_48h_composite=None,
        v3_72h_composite=None, v3_1w_composite=None, v3_2w_composite=None,
        v3_sub_elm=None, v3_sub_cascade=None, v3_sub_taker=None,
        v3_sub_oi=None, v3_sub_funding=None, v3_sub_vpin=None,
        v3_sub_momentum=None,
        v4_regime="calm_trend", v4_regime_confidence=0.85,
        v4_regime_persistence=0.9, v4_macro_bias="BULL",
        v4_macro_direction_gate="ALLOW_ALL", v4_macro_size_modifier=1.0,
        v4_consensus_safe_to_trade=True, v4_consensus_agreement_score=0.8,
        v4_consensus_max_divergence_bps=50.0,
        v4_conviction="HIGH", v4_conviction_score=0.85,
        poly_direction="DOWN", poly_trade_advised=True, poly_confidence=0.38,
        poly_confidence_distance=0.12, poly_timing="optimal",
        poly_max_entry_price=0.65, poly_reason="strong_signal",
        v4_recommended_side="DOWN", v4_recommended_collateral_pct=0.025,
        v4_sub_signals=None, v4_quantiles=None,
        clob_up_bid=0.46, clob_up_ask=0.48, clob_down_bid=0.52,
        clob_down_ask=0.54, clob_implied_up=0.47,
        gamma_up_price=0.45, gamma_down_price=0.55,
        cg_oi_usd=50_000_000.0, cg_funding_rate=0.0001,
        cg_taker_buy_vol=800_000.0, cg_taker_sell_vol=1_200_000.0,
        cg_liq_total=500_000.0, cg_liq_long=300_000.0,
        cg_liq_short=200_000.0, cg_long_short_ratio=1.2,
        timesfm_expected_move_bps=50.0, timesfm_vol_forecast_bps=80.0,
        hour_utc=12, seconds_to_close=120,
        probability_lgb=None, probability_classifier=None, ensemble_config=None,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


# ──────────────────────────────────────────────────────────────────────
# gate_params.get_* unit tests
# ──────────────────────────────────────────────────────────────────────


def test_get_bool_yaml_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOO_FLAG", "false")
    token = gate_params.set_active({"foo_flag": True})
    try:
        assert gate_params.get_bool("foo_flag", "FOO_FLAG", default=False) is True
    finally:
        gate_params.reset_active(token)


def test_get_bool_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOO_FLAG", "true")
    token = gate_params.set_active({})  # empty bag
    try:
        assert gate_params.get_bool("foo_flag", "FOO_FLAG", default=False) is True
    finally:
        gate_params.reset_active(token)


def test_get_bool_default_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FOO_FLAG", raising=False)
    token = gate_params.set_active({})
    try:
        assert gate_params.get_bool("foo_flag", "FOO_FLAG", default=True) is True
    finally:
        gate_params.reset_active(token)


def test_get_float_yaml_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOO_NUM", "0.20")
    token = gate_params.set_active({"foo_num": 0.15})
    try:
        assert gate_params.get_float("foo_num", "FOO_NUM", default=0.10) == 0.15
    finally:
        gate_params.reset_active(token)


def test_get_str_default_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOO_MODE", raising=False)
    # No active bag set — defaults apply.
    assert gate_params.get_str("foo_mode", "FOO_MODE", default="degraded") == "degraded"


def test_none_env_var_still_uses_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    # env_var=None → pure YAML + default only.
    token = gate_params.set_active({"x": 0.5})
    try:
        assert gate_params.get_float("x", None, default=0.0) == 0.5
    finally:
        gate_params.reset_active(token)


# ──────────────────────────────────────────────────────────────────────
# set_active / reset_active scoping
# ──────────────────────────────────────────────────────────────────────


def test_nested_set_active_restores_on_reset() -> None:
    outer = gate_params.set_active({"k": "outer"})
    assert gate_params.get_str("k", None, default="") == "outer"

    inner = gate_params.set_active({"k": "inner"})
    assert gate_params.get_str("k", None, default="") == "inner"
    gate_params.reset_active(inner)

    assert gate_params.get_str("k", None, default="") == "outer"
    gate_params.reset_active(outer)
    # Back to module default (empty dict).
    assert gate_params.get_str("k", None, default="fallback") == "fallback"


# ──────────────────────────────────────────────────────────────────────
# StrategyRegistry wiring
# ──────────────────────────────────────────────────────────────────────


def _write_yaml(dir_path: Path, name: str, body: dict) -> None:
    (dir_path / f"{name}.yaml").write_text(yaml.safe_dump(body))


def _write_hook(dir_path: Path, name: str, body: str) -> None:
    (dir_path / f"{name}.py").write_text(body)


@pytest.fixture()
def _configs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "configs"
    d.mkdir()
    return d


@pytest.fixture()
def _surface() -> FullDataSurface:
    return _make_surface()


def test_yaml_gate_params_reaches_hook_via_contextvar(
    _configs_dir: Path,
    _surface: FullDataSurface,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hook reads get_float('threshold', env_var=None) and the yaml
    block sets threshold=0.42 → decision.metadata exposes the observed
    value."""
    hook_src = """
from strategies import gate_params
from domain.value_objects import StrategyDecision

def my_hook(surface):
    threshold = gate_params.get_float("threshold", None, default=0.0)
    return StrategyDecision(
        action="SKIP",
        direction=None, confidence=None, confidence_score=None,
        entry_cap=None, collateral_pct=None,
        strategy_id="probe", strategy_version="1",
        entry_reason="", skip_reason="probe",
        metadata={"observed_threshold": threshold},
    )
"""
    _write_hook(_configs_dir, "probe_hook", hook_src)
    _write_yaml(
        _configs_dir,
        "probe",
        {
            "name": "probe",
            "version": "1",
            "mode": "GHOST",
            "asset": "BTC",
            "timescale": "5m",
            "gates": [],
            "sizing": {"type": "fixed_kelly", "fraction": 0.025},
            "hooks_file": "probe_hook.py",
            "pre_gate_hook": "my_hook",
            "gate_params": {"threshold": 0.42},
        },
    )

    reg = StrategyRegistry(
        config_dir=str(_configs_dir),
        data_surface=DataSurfaceManager(),
    )
    reg.load_all()
    decision = reg._evaluate_one("probe", reg.configs["probe"], _surface)

    assert decision.metadata["observed_threshold"] == pytest.approx(0.42)


def test_env_fallback_when_yaml_omits_param(
    _configs_dir: Path,
    _surface: FullDataSurface,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROBE_THRESHOLD", "0.77")

    hook_src = """
from strategies import gate_params
from domain.value_objects import StrategyDecision

def my_hook(surface):
    t = gate_params.get_float("threshold", "PROBE_THRESHOLD", default=0.0)
    return StrategyDecision(
        action="SKIP", direction=None, confidence=None, confidence_score=None,
        entry_cap=None, collateral_pct=None,
        strategy_id="probe", strategy_version="1",
        entry_reason="", skip_reason="probe",
        metadata={"observed_threshold": t},
    )
"""
    _write_hook(_configs_dir, "probe_hook", hook_src)
    _write_yaml(
        _configs_dir,
        "probe",
        {
            "name": "probe",
            "version": "1",
            "mode": "GHOST",
            "asset": "BTC",
            "timescale": "5m",
            "gates": [],
            "sizing": {"type": "fixed_kelly", "fraction": 0.025},
            "hooks_file": "probe_hook.py",
            "pre_gate_hook": "my_hook",
            # gate_params intentionally absent → env wins
        },
    )

    reg = StrategyRegistry(
        config_dir=str(_configs_dir),
        data_surface=DataSurfaceManager(),
    )
    reg.load_all()
    decision = reg._evaluate_one("probe", reg.configs["probe"], _surface)

    assert decision.metadata["observed_threshold"] == pytest.approx(0.77)


def test_two_strategies_share_hook_but_see_own_params(
    _configs_dir: Path,
    _surface: FullDataSurface,
) -> None:
    """Two yaml configs pointing at the same hook file but declaring
    different gate_params must observe their own value."""
    hook_src = """
from strategies import gate_params
from domain.value_objects import StrategyDecision

def my_hook(surface):
    t = gate_params.get_float("threshold", None, default=0.0)
    return StrategyDecision(
        action="SKIP", direction=None, confidence=None, confidence_score=None,
        entry_cap=None, collateral_pct=None,
        strategy_id="probe", strategy_version="1",
        entry_reason="", skip_reason="probe",
        metadata={"observed_threshold": t},
    )
"""
    _write_hook(_configs_dir, "shared_hook", hook_src)
    for stratname, val in (("strict", 0.20), ("relaxed", 0.12)):
        _write_yaml(
            _configs_dir,
            stratname,
            {
                "name": stratname,
                "version": "1",
                "mode": "GHOST",
                "asset": "BTC",
                "timescale": "5m",
                "gates": [],
                "sizing": {"type": "fixed_kelly", "fraction": 0.025},
                "hooks_file": "shared_hook.py",
                "pre_gate_hook": "my_hook",
                "gate_params": {"threshold": val},
            },
        )

    reg = StrategyRegistry(
        config_dir=str(_configs_dir),
        data_surface=DataSurfaceManager(),
    )
    reg.load_all()

    strict = reg._evaluate_one("strict", reg.configs["strict"], _surface)
    relaxed = reg._evaluate_one("relaxed", reg.configs["relaxed"], _surface)

    assert strict.metadata["observed_threshold"] == pytest.approx(0.20)
    assert relaxed.metadata["observed_threshold"] == pytest.approx(0.12)

    # Outside an evaluate_one call the contextvar is reset → default path.
    assert gate_params.get_float("threshold", None, default=99.0) == 99.0


def test_invalid_gate_params_yaml_raises(_configs_dir: Path) -> None:
    _write_yaml(
        _configs_dir,
        "bad",
        {
            "name": "bad",
            "version": "1",
            "mode": "GHOST",
            "asset": "BTC",
            "timescale": "5m",
            "gates": [],
            "sizing": {"type": "fixed_kelly"},
            "gate_params": ["not", "a", "dict"],
        },
    )
    reg = StrategyRegistry(
        config_dir=str(_configs_dir),
        data_surface=DataSurfaceManager(),
    )
    # load_all swallows per-file errors by design → config never registers.
    reg.load_all()
    assert "bad" not in reg.strategy_names
