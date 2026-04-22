"""Regression test — regime field is written to StrategyDecisionRecord.

Before this fix, every row in ``strategy_decisions`` had ``regime: None``
because the decision-record build site in ``registry.py`` did not capture
``surface.v4_regime``. Shadow reports that split WR by regime were
consequently empty for every strategy.

The fix injects ``regime`` (+ mirror ``v4_regime``) into
``StrategyDecisionRecord.metadata_json`` on EVERY path (TRADE / SKIP /
ERROR) from the surface that was used to evaluate the decision. This
test asserts the invariant end-to-end by running the real registry with
a fake in-memory decision repo.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from strategies.data_surface import DataSurfaceManager, FullDataSurface
from strategies.registry import StrategyRegistry


def _make_surface(**overrides) -> FullDataSurface:
    """Surface with v4_regime explicitly set to ``volatile_trend``."""
    defaults = dict(
        asset="BTC",
        timescale="5m",
        window_ts=1713000000,
        eval_offset=120,
        assembled_at=time.time(),
        current_price=84500.0,
        open_price=84000.0,
        delta_binance=0.005,
        delta_tiingo=0.004,
        delta_chainlink=0.005,
        delta_pct=0.004,
        delta_source="tiingo_rest_candle",
        vpin=0.45,
        regime="NORMAL",
        twap_delta=0.003,
        v2_probability_up=0.38,
        v2_probability_raw=0.36,
        v2_quantiles_p10=None,
        v2_quantiles_p50=None,
        v2_quantiles_p90=None,
        probability_lgb=None,
        probability_classifier=None,
        ensemble_config=None,
        v3_5m_composite=None,
        v3_15m_composite=None,
        v3_1h_composite=None,
        v3_4h_composite=None,
        v3_24h_composite=None,
        v3_48h_composite=None,
        v3_72h_composite=None,
        v3_1w_composite=None,
        v3_2w_composite=None,
        v3_sub_elm=None,
        v3_sub_cascade=None,
        v3_sub_taker=None,
        v3_sub_oi=None,
        v3_sub_funding=None,
        v3_sub_vpin=None,
        v3_sub_momentum=None,
        v4_regime="volatile_trend",
        v4_regime_confidence=0.85,
        v4_regime_persistence=0.9,
        v4_macro_bias="BULL",
        v4_macro_direction_gate="ALLOW_ALL",
        v4_macro_size_modifier=1.0,
        v4_consensus_safe_to_trade=True,
        v4_consensus_agreement_score=0.8,
        v4_consensus_max_divergence_bps=50.0,
        v4_conviction="HIGH",
        v4_conviction_score=0.85,
        poly_direction="DOWN",
        poly_trade_advised=True,
        poly_confidence=0.38,
        poly_confidence_distance=0.12,
        poly_timing="optimal",
        poly_max_entry_price=0.65,
        poly_reason="strong_signal",
        v4_recommended_side="DOWN",
        v4_recommended_collateral_pct=0.025,
        v4_sub_signals=None,
        v4_quantiles=None,
        clob_up_bid=0.46,
        clob_up_ask=0.48,
        clob_down_bid=0.52,
        clob_down_ask=0.54,
        clob_implied_up=0.47,
        gamma_up_price=0.45,
        gamma_down_price=0.55,
        cg_oi_usd=50_000_000.0,
        cg_funding_rate=0.0001,
        cg_taker_buy_vol=800_000.0,
        cg_taker_sell_vol=1_200_000.0,
        cg_liq_total=500_000.0,
        cg_liq_long=300_000.0,
        cg_liq_short=200_000.0,
        cg_long_short_ratio=1.2,
        timesfm_expected_move_bps=50.0,
        timesfm_vol_forecast_bps=80.0,
        hour_utc=12,
        seconds_to_close=120,
    )
    defaults.update(overrides)
    return FullDataSurface(**defaults)


class _FakeWindow:
    def __init__(self, **kw):
        self.asset = kw.get("asset", "BTC")
        self.window_ts = kw.get("window_ts", 1713000000)
        self.open_price = kw.get("open_price", 84000.0)
        self.eval_offset = kw.get("eval_offset", 120)
        self.up_price = kw.get("up_price", 0.45)
        self.down_price = kw.get("down_price", 0.55)
        self.timeframe = kw.get("timeframe", "5m")


class _RecordingDecisionRepo:
    """Captures every write_decision call for later assertion."""

    def __init__(self) -> None:
        self.writes: list = []

    async def write_decision(self, record):
        self.writes.append(record)


def _trivial_strategy_yaml(name: str) -> dict:
    """Minimal YAML that returns TRADE via a pre_gate_hook."""
    return {
        "name": name,
        "version": "0.0.1",
        "mode": "GHOST",
        "asset": "BTC",
        "timescale": "5m",
        "gates": [],
        "sizing": {"type": "fixed_kelly", "fraction": 0.025},
        "hooks_file": f"{name}.py",
        "pre_gate_hook": "always_trade",
    }


def _trivial_hook_py() -> str:
    return (
        "from domain.value_objects import StrategyDecision\n"
        "def always_trade(surface):\n"
        "    return StrategyDecision(\n"
        "        action='TRADE', direction='DOWN',\n"
        "        confidence='HIGH', confidence_score=0.8,\n"
        "        entry_cap=0.65, collateral_pct=0.025,\n"
        "        strategy_id='trade_strat', strategy_version='0.0.1',\n"
        "        entry_reason='test', skip_reason=None, metadata={}\n"
        "    )\n"
    )


def _trivial_skip_hook_py() -> str:
    return (
        "from domain.value_objects import StrategyDecision\n"
        "def always_trade(surface):\n"
        "    return StrategyDecision(\n"
        "        action='SKIP', direction=None,\n"
        "        confidence=None, confidence_score=None,\n"
        "        entry_cap=None, collateral_pct=None,\n"
        "        strategy_id='skip_strat', strategy_version='0.0.1',\n"
        "        entry_reason='', skip_reason='synthetic_skip', metadata={}\n"
        "    )\n"
    )


def _build_registry(tmp_path: Path, *, skip_variant: bool = False):
    """Build a registry with one fake strategy that always TRADEs (or SKIPs).

    Patches data_surface.get_surface to return a controlled surface so we
    can assert regime propagation without hitting the real v4 port.
    """
    name = "skip_strat" if skip_variant else "trade_strat"
    (tmp_path / f"{name}.yaml").write_text(yaml.dump(_trivial_strategy_yaml(name)))
    (tmp_path / f"{name}.py").write_text(
        _trivial_skip_hook_py() if skip_variant else _trivial_hook_py()
    )
    # Rewrite the yaml name to match the hook + strategy_id above.
    cfg = _trivial_strategy_yaml(name)
    (tmp_path / f"{name}.yaml").write_text(yaml.dump(cfg))

    mgr = DataSurfaceManager(v4_base_url="http://fake")
    repo = _RecordingDecisionRepo()
    reg = StrategyRegistry(str(tmp_path), mgr, decision_repo=repo)
    reg.load_all()
    return reg, repo


def _patch_surface(reg, surface):
    reg._data_surface.get_surface = lambda window, eval_offset: surface


# ── Tests ───────────────────────────────────────────────────────────────────
def test_trade_path_writes_regime(tmp_path):
    reg, repo = _build_registry(tmp_path)
    surface = _make_surface(v4_regime="volatile_trend")
    _patch_surface(reg, surface)

    asyncio.run(reg.evaluate_all(_FakeWindow(), None))
    # Fire-and-forget write — give the loop a moment
    asyncio.run(asyncio.sleep(0.05))

    assert repo.writes, "expected at least one decision write"
    record = repo.writes[-1]
    meta = json.loads(record.metadata_json)
    assert meta.get("regime") == "volatile_trend", meta
    assert meta.get("v4_regime") == "volatile_trend", meta


def test_skip_path_writes_regime(tmp_path):
    reg, repo = _build_registry(tmp_path, skip_variant=True)
    surface = _make_surface(v4_regime="chop")
    _patch_surface(reg, surface)

    asyncio.run(reg.evaluate_all(_FakeWindow(), None))
    asyncio.run(asyncio.sleep(0.05))

    assert repo.writes, "expected at least one decision write (SKIP path)"
    record = repo.writes[-1]
    assert record.action == "SKIP"
    meta = json.loads(record.metadata_json)
    assert meta.get("regime") == "chop", meta


def test_regime_falls_back_to_vpin_regime_when_v4_absent(tmp_path):
    """If v4_regime is None, the fix falls back to surface.regime (vpin)."""
    reg, repo = _build_registry(tmp_path)
    surface = _make_surface(v4_regime=None, regime="CASCADE")
    _patch_surface(reg, surface)

    asyncio.run(reg.evaluate_all(_FakeWindow(), None))
    asyncio.run(asyncio.sleep(0.05))

    assert repo.writes
    meta = json.loads(repo.writes[-1].metadata_json)
    # regime falls back to vpin regime
    assert meta.get("regime") == "CASCADE", meta
    # v4_regime stays None (accurate representation of the input)
    assert meta.get("v4_regime") is None, meta


def test_hook_supplied_regime_is_preserved(tmp_path):
    """If a strategy hook already emits ``regime`` in metadata, preserve it."""
    name = "hook_sets_regime"
    (tmp_path / f"{name}.yaml").write_text(
        yaml.dump(
            {
                "name": name,
                "version": "0.0.1",
                "mode": "GHOST",
                "asset": "BTC",
                "timescale": "5m",
                "gates": [],
                "sizing": {"type": "fixed_kelly", "fraction": 0.025},
                "hooks_file": f"{name}.py",
                "pre_gate_hook": "hook",
            }
        )
    )
    (tmp_path / f"{name}.py").write_text(
        "from domain.value_objects import StrategyDecision\n"
        "def hook(surface):\n"
        "    return StrategyDecision(\n"
        "        action='TRADE', direction='DOWN',\n"
        "        confidence='HIGH', confidence_score=0.8,\n"
        f"        entry_cap=0.65, collateral_pct=0.025,\n"
        f"        strategy_id='{name}', strategy_version='0.0.1',\n"
        "        entry_reason='t', skip_reason=None,\n"
        "        metadata={'regime': 'HOOK_SUPPLIED'}\n"
        "    )\n"
    )
    mgr = DataSurfaceManager(v4_base_url="http://fake")
    repo = _RecordingDecisionRepo()
    reg = StrategyRegistry(str(tmp_path), mgr, decision_repo=repo)
    reg.load_all()
    surface = _make_surface(v4_regime="volatile_trend")
    reg._data_surface.get_surface = lambda window, eval_offset: surface

    asyncio.run(reg.evaluate_all(_FakeWindow(), None))
    asyncio.run(asyncio.sleep(0.05))

    assert repo.writes
    meta = json.loads(repo.writes[-1].metadata_json)
    assert meta.get("regime") == "HOOK_SUPPLIED", meta
    # v4_regime still mirrors surface for audit
    assert meta.get("v4_regime") == "volatile_trend", meta
