"""PR #619 review BLOCKER B1 — mutex resolver runs BEFORE execute.

Before the fix, ``StrategyRegistry.evaluate_all`` ran the mutex-group
resolver at the tail of the method — AFTER every per-strategy
``execute_uc.execute(...)`` call and ``_fire_trade_attempt_card``. At
LIVE this meant three sister TickFormer strategies sharing
``mutex_group=tickformer`` would all fire real orders before the
resolver demoted two of them. SHADOW was safe because
``shadow_only=1`` short-circuits the hook to SKIP first.

This test wires three trivial strategies with the same mutex_group,
all returning TRADE at GHOST (no execute path), and asserts:

  * The returned ``decisions`` list shows exactly ONE TRADE + two
    SKIP(mutex_group_lost) — the resolver ran.
  * The loser SKIP rows carry ``mutex_pre_resolution_action='TRADE'``
    so the audit trail in ``strategy_decisions`` shows BOTH the
    original TRADE intent AND the resolved SKIP (review choice (a)).

It also asserts that ``_execute_uc.execute`` is called AT MOST ONCE
when three LIVE strategies share a mutex group — i.e. demotion
prevents stacked exposure / fill competition. This is the LIVE
go-live invariant the placement fix targets.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from strategies.data_surface import DataSurfaceManager
from strategies.registry import StrategyRegistry

from test_registry_regime_write import (  # type: ignore  # noqa: E402
    _FakeWindow,
    _RecordingDecisionRepo,
    _make_surface,
)


def _mutex_trade_hook_py(strategy_id: str, score: float) -> str:
    """Hook returning TRADE with mutex_group=tickformer + confidence_score."""
    return (
        "from domain.value_objects import StrategyDecision\n"
        f"def hook(surface):\n"
        f"    return StrategyDecision(\n"
        f"        action='TRADE', direction='UP',\n"
        f"        confidence='HIGH', confidence_score={score},\n"
        f"        entry_cap=0.93, collateral_pct=0.025,\n"
        f"        strategy_id='{strategy_id}', strategy_version='1.0.0',\n"
        f"        entry_reason='{strategy_id}_pass', skip_reason=None,\n"
        f"        metadata={{'mutex_group': 'tickformer'}}\n"
        f"    )\n"
    )


def _yaml_for(name: str, mode: str = "GHOST") -> dict:
    return {
        "name": name,
        "strategy_id": name,
        "version": "1.0.0",
        "mode": mode,
        "asset": "BTC",
        "timescale": "5m",
        "gates": [],
        "sizing": {"type": "fixed_kelly", "fraction": 0.025},
        "hooks_file": f"{name}.py",
        "pre_gate_hook": "hook",
    }


def _build_three_strategy_registry(
    tmp_path: Path,
    *,
    mode: str = "GHOST",
    execute_uc=None,
    scores: tuple[float, float, float] = (0.70, 0.92, 0.80),
):
    names = ("mutex_alpha", "mutex_beta", "mutex_gamma")
    for name, score in zip(names, scores):
        (tmp_path / f"{name}.yaml").write_text(yaml.dump(_yaml_for(name, mode)))
        (tmp_path / f"{name}.py").write_text(
            _mutex_trade_hook_py(name, score)
        )

    mgr = DataSurfaceManager(v4_base_url="http://fake")
    repo = _RecordingDecisionRepo()
    reg = StrategyRegistry(
        str(tmp_path),
        mgr,
        decision_repo=repo,
        execute_trade_uc=execute_uc,
    )
    reg.load_all()
    surface = _make_surface()
    reg._data_surface.get_surface = lambda window, eval_offset: surface
    return reg, repo, names


def test_three_trades_demoted_to_one_with_audit_trail(tmp_path):
    """Pass A produces 3 TRADEs; mutex resolver demotes 2 to SKIP
    before Pass B; demoted rows carry mutex_pre_resolution_action."""
    reg, repo, names = _build_three_strategy_registry(tmp_path)

    decisions = asyncio.run(reg.evaluate_all(_FakeWindow(), None))
    asyncio.run(asyncio.sleep(0.05))

    # In-memory decisions: 1 TRADE + 2 SKIP.
    by_id = {d.strategy_id: d for d in decisions}
    actions = {sid: by_id[sid].action for sid in names}
    trade_count = sum(1 for a in actions.values() if a == "TRADE")
    skip_count = sum(1 for a in actions.values() if a == "SKIP")
    assert trade_count == 1, actions
    assert skip_count == 2, actions

    # Winner is the highest score (mutex_beta @ 0.92).
    winners = [sid for sid, a in actions.items() if a == "TRADE"]
    assert winners == ["mutex_beta"], winners

    # Losers carry mutex_group_lost + pre-resolution audit trail.
    losers = [d for d in decisions if d.skip_reason == "mutex_group_lost"]
    assert len(losers) == 2
    for loser in losers:
        meta = loser.metadata or {}
        assert meta.get("mutex_pre_resolution_action") == "TRADE", meta
        assert meta.get("mutex_pre_resolution_direction") == "UP", meta
        assert meta.get("mutex_group_winner") == "mutex_beta", meta

    # strategy_decisions writes also reflect the demoted final action.
    writes_by_id = {w.strategy_id: w for w in repo.writes}
    assert writes_by_id["mutex_beta"].action == "TRADE"
    for sid in ("mutex_alpha", "mutex_gamma"):
        assert writes_by_id[sid].action == "SKIP"
        assert writes_by_id[sid].skip_reason == "mutex_group_lost"
        meta = json.loads(writes_by_id[sid].metadata_json)
        assert meta.get("mutex_pre_resolution_action") == "TRADE"


def test_execute_uc_called_at_most_once_with_three_live_mutex_contenders(
    tmp_path,
):
    """Three LIVE strategies sharing mutex_group → execute_uc.execute
    is called AT MOST ONCE (the winner). This is the LIVE go-live
    invariant the BLOCKER B1 fix protects: previously all three would
    fire real orders before the resolver could demote two."""
    execute_uc = MagicMock()
    # AsyncMock returns a result object compatible with the engine's
    # expected ExecutionResult shape — only the attrs touched by the
    # post-execute branch matter.
    fake_result = MagicMock()
    fake_result.success = True
    fake_result.order_id = "test-order"
    fake_result.fill_price = 0.65
    fake_result.fill_size = 5.0
    fake_result.token_id = "tok"
    fake_result.execution_mode = "LIVE"
    fake_result.size_matched = 5.0
    execute_uc.execute = AsyncMock(return_value=fake_result)

    reg, _repo, _names = _build_three_strategy_registry(
        tmp_path, mode="LIVE", execute_uc=execute_uc
    )
    # The engine defaults to paper_mode=True (safe default) — explicitly
    # turn it off so the LIVE-execute path is reachable.
    reg.set_paper_mode(False)

    # Window with market token IDs so the LIVE execute branch fires.
    # window_ts must be recent — the registry blocks past-close dispatch.
    import time as _t
    _now = int(_t.time())
    _window_ts = _now - 60  # 1 minute into a fresh 5m window
    window = _FakeWindow(window_ts=_window_ts)
    # _execute_uc.execute is gated on ``window_market is not None``.
    # The registry's evaluate_all wires this via the keyword arg.
    window_market = MagicMock()
    window_market.up_token_id = "up_tok"
    window_market.down_token_id = "down_tok"

    asyncio.run(
        reg.evaluate_all(
            window,
            None,
            window_market=window_market,
            current_btc_price=84500.0,
            open_price=84000.0,
        )
    )
    asyncio.run(asyncio.sleep(0.05))

    # At most one LIVE execute despite 3 mutex contenders.
    assert execute_uc.execute.await_count <= 1, (
        f"expected <=1 execute call, got {execute_uc.execute.await_count}"
    )
    # Specifically: exactly one winner executes.
    assert execute_uc.execute.await_count == 1


def test_mutex_resolver_runs_before_strategy_decisions_write(tmp_path):
    """strategy_decisions rows for mutex losers must record SKIP(lost)
    as the FINAL action (the engine fires Pass A→Mutex→Pass B). This
    test asserts the writer never sees TRADE for a demoted loser."""
    reg, repo, names = _build_three_strategy_registry(tmp_path)

    asyncio.run(reg.evaluate_all(_FakeWindow(), None))
    asyncio.run(asyncio.sleep(0.05))

    # Find every loser write — it must be SKIP.
    for w in repo.writes:
        if w.strategy_id == "mutex_beta":
            assert w.action == "TRADE"
        else:
            assert w.action == "SKIP", (
                f"loser {w.strategy_id} written as {w.action}; "
                "mutex resolver did not run before strategy_decisions write"
            )
            assert w.skip_reason == "mutex_group_lost"
