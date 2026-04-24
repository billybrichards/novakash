"""Classifier shadow logging — registry writes pc/pl/latency to metadata.

Note #226 phase 1a: every ``strategy_decisions`` row should carry the
classifier + LGB probability snapshots plus fetch telemetry so the
48 h burn-in can compute p95 latency + pc mean/saturation keyed by
(asset, window_ts, eval_offset).

This is in addition to the regime-write invariant tested in
``test_registry_regime_write.py`` — we share the registry + fake hook
shape here.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
# Co-located in tests/unit/strategies — add this dir so sibling import works.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from strategies.data_surface import DataSurfaceManager
from strategies.registry import StrategyRegistry

from test_registry_regime_write import (  # reuse existing helpers
    _FakeWindow,
    _RecordingDecisionRepo,
    _make_surface,
    _trivial_hook_py,
    _trivial_strategy_yaml,
)


def _build(tmp_path: Path, telemetry: dict | None = None):
    name = "trade_strat"
    cfg = _trivial_strategy_yaml(name)
    (tmp_path / f"{name}.yaml").write_text(yaml.dump(cfg))
    (tmp_path / f"{name}.py").write_text(_trivial_hook_py())

    mgr = DataSurfaceManager(v4_base_url="http://fake")
    # Stub telemetry accessor — simulates a prior successful fetch.
    if telemetry is not None:
        mgr.get_fetch_telemetry = lambda asset: dict(telemetry)  # type: ignore
    repo = _RecordingDecisionRepo()
    reg = StrategyRegistry(str(tmp_path), mgr, decision_repo=repo)
    reg.load_all()
    return reg, repo


def _run_once(reg):
    asyncio.run(reg.evaluate_all(_FakeWindow(), None))
    asyncio.run(asyncio.sleep(0.05))


def test_pc_and_pl_stamped_when_present(tmp_path):
    reg, repo = _build(
        tmp_path,
        telemetry={"pc_fetch_latency_ms": 42.0, "pc_fetch_source": "primary"},
    )
    surface = _make_surface(
        probability_lgb=0.71,
        probability_classifier=0.62,
        v4_regime="volatile_trend",
    )
    reg._data_surface.get_surface = lambda window, eval_offset: surface

    _run_once(reg)

    assert repo.writes
    meta = json.loads(repo.writes[-1].metadata_json)
    assert meta.get("probability_classifier") == pytest.approx(0.62)
    assert meta.get("probability_lgb") == pytest.approx(0.71)
    assert meta.get("pc_fetch_latency_ms") == pytest.approx(42.0)
    assert meta.get("pc_fetch_source") == "primary"


def test_pc_absent_leaves_field_absent(tmp_path):
    """When pc is None (classifier box down / no fallback), the key is
    omitted from metadata entirely — readers that grep for missing pc
    can count None vs present cleanly."""
    reg, repo = _build(tmp_path, telemetry={})
    surface = _make_surface(
        probability_lgb=0.71,
        probability_classifier=None,
        v4_regime="volatile_trend",
    )
    reg._data_surface.get_surface = lambda window, eval_offset: surface

    _run_once(reg)

    assert repo.writes
    meta = json.loads(repo.writes[-1].metadata_json)
    assert "probability_classifier" not in meta
    # pl still stamped because it is populated.
    assert meta.get("probability_lgb") == pytest.approx(0.71)
    # Telemetry keys absent when source hasn't succeeded.
    assert "pc_fetch_latency_ms" not in meta
    assert "pc_fetch_source" not in meta


def test_hook_supplied_pc_pl_not_overwritten(tmp_path):
    """If a strategy hook already set pc/pl (e.g. v9_ensemble with its
    own snapshot before a later surface refresh), the registry must NOT
    clobber that value with the current surface read."""
    name = "hook_with_pc"
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
        "        metadata={'probability_classifier': 0.99, 'probability_lgb': 0.01}\n"
        "    )\n"
    )
    mgr = DataSurfaceManager(v4_base_url="http://fake")
    repo = _RecordingDecisionRepo()
    reg = StrategyRegistry(str(tmp_path), mgr, decision_repo=repo)
    reg.load_all()
    # Surface carries different values — must not win over hook.
    surface = _make_surface(
        probability_classifier=0.55,
        probability_lgb=0.45,
        v4_regime="volatile_trend",
    )
    reg._data_surface.get_surface = lambda window, eval_offset: surface

    _run_once(reg)

    assert repo.writes
    meta = json.loads(repo.writes[-1].metadata_json)
    assert meta.get("probability_classifier") == pytest.approx(0.99)
    assert meta.get("probability_lgb") == pytest.approx(0.01)
