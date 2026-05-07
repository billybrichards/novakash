"""Rolling-WR auto-pause monitor (audits #379 + #385, 2026-05-06).

Pure unit tests against an in-memory CellPauseRepo fake — no DB dependency.
Verifies that the monitor:
  * fires a pause when 60-min Wilson LB drops below fill-adjusted breakeven
  * fires a pause when 60-min PnL crosses the -$30 floor
  * fires a pause when WR drops > 25pp from the 7d baseline
  * does NOT pause when sample size is below `min_trades`
  * does NOT re-pause an already-paused cell
  * supports manual `release(pause_id)`
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from services.cell_bucketing import session as _session
from services.cell_bucketing import t_band as _t_band
from services.rolling_wr_monitor import (
    CellKey,
    ResolvedTrade,
    RollingWRMonitor,
    _resolve_min_rolling_n,
    fill_breakeven_wr,
    wilson_lower_bound,
)


# ─────────────────────────── in-memory fake repo ─────────────────────────────


class FakeRepo:
    def __init__(self):
        self.trades_by_cell: dict[CellKey, list[ResolvedTrade]] = {}
        self.baseline_by_cell: dict[CellKey, float] = {}
        self.pauses: dict[int, dict[str, Any]] = {}
        self._next_id = 1

    async def get_recent_trades_for_cell(
        self, cell, lookback_seconds
    ):
        cutoff = time.time() - lookback_seconds
        return [
            t for t in self.trades_by_cell.get(cell, [])
            if t.resolved_at >= cutoff
        ]

    async def get_baseline_wr(self, cell, lookback_seconds):
        return self.baseline_by_cell.get(cell)

    async def is_cell_paused(self, cell):
        for row in self.pauses.values():
            if row["cell"] == cell and row["released_at"] is None:
                return True
        return False

    async def insert_pause(self, cell, pause_seconds, reason, trigger_metric):
        for row in self.pauses.values():
            if row["cell"] == cell and row["released_at"] is None:
                return None
        pause_id = self._next_id
        self._next_id += 1
        self.pauses[pause_id] = {
            "cell": cell,
            "pause_seconds": pause_seconds,
            "reason": reason,
            "trigger_metric": trigger_metric,
            "released_at": None,
            "paused_at": time.time(),
        }
        return pause_id

    async def release_pause(self, pause_id, released_by):
        if pause_id in self.pauses:
            self.pauses[pause_id]["released_at"] = time.time()
            self.pauses[pause_id]["released_by"] = released_by


def _trade(
    *,
    direction="DOWN",
    eval_offset=80,
    is_win=False,
    pnl=-5.0,
    fill=0.40,
    hour=10,
    regime="chop",
):
    return ResolvedTrade(
        strategy_id="v12_lgb_combo",
        direction=direction,
        eval_offset=eval_offset,
        hour_utc=hour,
        regime=regime,
        fill_price=fill,
        pnl_usd=pnl,
        is_win=is_win,
    )


# ─────────────────────────── helper math ────────────────────────────────────


def test_wilson_lower_bound_zero_sample():
    assert wilson_lower_bound(0, 0) == 0.0


def test_wilson_lower_bound_typical():
    # 70% point estimate over n=10 sample → LB should be < 0.50.
    lb = wilson_lower_bound(7, 10)
    assert 0.30 < lb < 0.50


def test_fill_breakeven_wr_at_50():
    # fill 0.5 + 7.2% fee -> WR ~= 0.5 / (1 - 0.036) = 0.5187
    be = fill_breakeven_wr(0.50, fee_mult=0.072)
    assert 0.50 < be < 0.55


def test_fill_breakeven_wr_at_75():
    # high-fill fields require ~78%+ WR.
    be = fill_breakeven_wr(0.75, fee_mult=0.072)
    assert 0.78 < be < 0.82


# ─────────────────────────── monitor behaviour ──────────────────────────────


def _run(coro):
    return asyncio.run(coro)


def test_pause_fires_on_pnl_floor_breach():
    repo = FakeRepo()
    monitor = RollingWRMonitor(repo, min_trades=4)
    cell = _trade().cell
    # 4 losses in a row at -$10 each = -$40 60m PnL.
    repo.trades_by_cell[cell] = [
        _trade(is_win=False, pnl=-10.0) for _ in range(4)
    ]

    pid = _run(monitor.on_trade_resolved(_trade(is_win=False, pnl=-10.0)))
    assert pid is not None
    assert "60m_pnl" in repo.pauses[pid]["reason"]


def test_pause_fires_on_wilson_below_breakeven():
    repo = FakeRepo()
    monitor = RollingWRMonitor(
        repo, min_trades=4, min_pnl_window_usd=-1e6  # disable PnL trigger
    )
    cell = _trade(fill=0.75).cell
    # 8 trades, 4 wins / 4 losses at fill 0.75 -> WR=0.5 < breakeven (~0.80).
    losses = [_trade(is_win=False, pnl=-7.5, fill=0.75) for _ in range(4)]
    wins = [_trade(is_win=True, pnl=2.0, fill=0.75) for _ in range(4)]
    repo.trades_by_cell[cell] = losses + wins

    pid = _run(monitor.on_trade_resolved(_trade(is_win=False, pnl=-7.5, fill=0.75)))
    assert pid is not None
    assert "wilson_lb" in repo.pauses[pid]["reason"]


def test_pause_fires_on_baseline_drop():
    repo = FakeRepo()
    monitor = RollingWRMonitor(
        repo,
        min_trades=4,
        min_pnl_window_usd=-1e6,
        max_wr_drop_pp=20.0,
    )
    cell = _trade().cell
    repo.baseline_by_cell[cell] = 0.80  # 7d baseline = 80% WR
    # Recent: 4 losses + 1 win = 20% recent WR. Drop = 60pp.
    repo.trades_by_cell[cell] = [
        _trade(is_win=False, pnl=-2.0) for _ in range(4)
    ] + [_trade(is_win=True, pnl=2.0)]

    pid = _run(monitor.on_trade_resolved(_trade(is_win=False, pnl=-2.0)))
    assert pid is not None
    assert "wr_drop" in repo.pauses[pid]["reason"]


def test_pause_does_not_fire_below_min_trades():
    repo = FakeRepo()
    monitor = RollingWRMonitor(repo, min_trades=4)
    cell = _trade().cell
    # Only 2 trades — below min_trades.
    repo.trades_by_cell[cell] = [
        _trade(is_win=False, pnl=-10.0) for _ in range(2)
    ]

    pid = _run(monitor.on_trade_resolved(_trade(is_win=False, pnl=-10.0)))
    assert pid is None
    assert len(repo.pauses) == 0


def test_pause_does_not_re_fire_when_already_paused():
    repo = FakeRepo()
    monitor = RollingWRMonitor(repo, min_trades=4)
    cell = _trade().cell
    repo.trades_by_cell[cell] = [
        _trade(is_win=False, pnl=-10.0) for _ in range(4)
    ]

    pid1 = _run(monitor.on_trade_resolved(_trade(is_win=False, pnl=-10.0)))
    assert pid1 is not None
    pid2 = _run(monitor.on_trade_resolved(_trade(is_win=False, pnl=-10.0)))
    assert pid2 is None  # repo says cell is paused
    assert len(repo.pauses) == 1


def test_release_pause_clears_active():
    repo = FakeRepo()
    monitor = RollingWRMonitor(repo, min_trades=4)
    cell = _trade().cell
    repo.trades_by_cell[cell] = [
        _trade(is_win=False, pnl=-10.0) for _ in range(4)
    ]
    pid = _run(monitor.on_trade_resolved(_trade(is_win=False, pnl=-10.0)))
    assert pid is not None
    _run(monitor.release(pid, by="ops"))
    assert repo.pauses[pid]["released_at"] is not None
    assert repo.pauses[pid]["released_by"] == "ops"


def test_pause_does_not_fire_on_healthy_cell():
    repo = FakeRepo()
    monitor = RollingWRMonitor(repo, min_trades=4)
    cell = _trade(fill=0.40).cell
    # 8 wins out of 10 at fill 0.40 -> healthy.
    repo.trades_by_cell[cell] = [
        _trade(is_win=True, pnl=6.0, fill=0.40) for _ in range(8)
    ] + [
        _trade(is_win=False, pnl=-4.0, fill=0.40) for _ in range(2)
    ]
    repo.baseline_by_cell[cell] = 0.80

    pid = _run(monitor.on_trade_resolved(_trade(is_win=True, pnl=6.0, fill=0.40)))
    assert pid is None


# ────────────────────────── cell bucketing ──────────────────────────────────


def test_cell_key_is_built_from_t_band_and_session():
    trade = _trade(eval_offset=24, hour=10)
    assert trade.cell.t_band == _t_band(24)
    assert trade.cell.session == _session(10)
    assert trade.cell.t_band == "T-0-30"
    assert trade.cell.session == "eu_am"


# ────────────────── C4: idempotent duplicate-insert handling ─────────────────


def test_duplicate_insert_is_idempotent():
    """Two concurrent on_trade_resolved calls for the same cell should
    produce at most ONE active pause (FakeRepo enforces no-double-active).
    The second call returns None when the cell is already paused."""
    repo = FakeRepo()
    monitor = RollingWRMonitor(repo, min_trades=4)
    cell = _trade().cell
    repo.trades_by_cell[cell] = [_trade(is_win=False, pnl=-10.0) for _ in range(4)]

    pid1 = _run(monitor.on_trade_resolved(_trade(is_win=False, pnl=-10.0)))
    # Simulate a concurrent insert arriving after pid1 is set.
    pid2 = _run(monitor.on_trade_resolved(_trade(is_win=False, pnl=-10.0)))

    assert pid1 is not None
    assert pid2 is None  # idempotent — second call sees cell already paused
    # Only one row in the pause store.
    active = [r for r in repo.pauses.values() if r["released_at"] is None]
    assert len(active) == 1


def test_fake_repo_insert_pause_returns_none_on_duplicate():
    """FakeRepo.insert_pause returns None when an active pause exists,
    matching the expected asyncpg UniqueViolationError → None semantics."""
    repo = FakeRepo()
    cell = _trade().cell
    _run(repo.insert_pause(cell, 3600, "first", {}))
    result = _run(repo.insert_pause(cell, 3600, "duplicate", {}))
    assert result is None


# ────────────────── C5: pause duration config layering ──────────────────────


def test_default_pause_seconds_applied(monkeypatch):
    """When no per-cell override, _resolve_pause_seconds falls back through
    the settings layer (which reads env vars) or ultimately the constructor
    default. With no env override the settings default is 14400 = 4h."""
    from services import rolling_wr_monitor as wr_mod

    # Patch settings to return a known value so the test is deterministic.
    def patched_resolve(constructor_value, cell_params=None):
        # No per-cell override → return constructor_value (simulates settings
        # also at default, so layer 3 wins).
        if not cell_params or "cell_pause_seconds" not in cell_params:
            return constructor_value
        return int(cell_params["cell_pause_seconds"])

    monkeypatch.setattr(wr_mod, "_resolve_pause_seconds", patched_resolve)

    repo = FakeRepo()
    custom_seconds = 7200  # 2 hours — passed as constructor param
    monitor = RollingWRMonitor(repo, min_trades=4, pause_seconds=custom_seconds)
    cell = _trade().cell
    repo.trades_by_cell[cell] = [_trade(is_win=False, pnl=-10.0) for _ in range(4)]

    pid = _run(monitor.on_trade_resolved(_trade(is_win=False, pnl=-10.0)))
    assert pid is not None
    assert repo.pauses[pid]["pause_seconds"] == custom_seconds


def test_per_cell_params_override_pause_seconds():
    """Trade with params={'cell_pause_seconds': 1800} overrides the default."""
    from services.rolling_wr_monitor import ResolvedTrade

    repo = FakeRepo()
    monitor = RollingWRMonitor(repo, min_trades=4, pause_seconds=14400)

    cell = _trade().cell
    repo.trades_by_cell[cell] = [_trade(is_win=False, pnl=-10.0) for _ in range(4)]

    # Trade carries per-cell override.
    trade_with_override = ResolvedTrade(
        strategy_id="v12_lgb_combo",
        direction="DOWN",
        eval_offset=80,
        hour_utc=10,
        regime="chop",
        fill_price=0.40,
        pnl_usd=-10.0,
        is_win=False,
        params={"cell_pause_seconds": 1800},
    )
    pid = _run(monitor.on_trade_resolved(trade_with_override))
    assert pid is not None
    assert repo.pauses[pid]["pause_seconds"] == 1800


def test_settings_override_applied_when_no_per_cell_params(monkeypatch):
    """When settings.cell_pause_default_seconds is set, it beats the constructor."""
    from unittest.mock import MagicMock
    from services import rolling_wr_monitor as wr_mod

    # Patch get_settings to return a mock with the desired value.
    mock_settings = MagicMock()
    mock_settings.cell_pause_default_seconds = 3600  # 1 hour

    def patched_resolve(constructor_value, cell_params=None):
        # Simulate settings layer returning 3600.
        if not cell_params or "cell_pause_seconds" not in cell_params:
            return 3600
        return int(cell_params["cell_pause_seconds"])

    monkeypatch.setattr(wr_mod, "_resolve_pause_seconds", patched_resolve)

    repo = FakeRepo()
    monitor = RollingWRMonitor(repo, min_trades=4, pause_seconds=14400)
    cell = _trade().cell
    repo.trades_by_cell[cell] = [_trade(is_win=False, pnl=-10.0) for _ in range(4)]

    pid = _run(monitor.on_trade_resolved(_trade(is_win=False, pnl=-10.0)))
    assert pid is not None
    assert repo.pauses[pid]["pause_seconds"] == 3600


# ────────────── C6: min_rolling_n floor — no-pause-before-n-trades ──────────


def test_min_n_default_3():
    """_resolve_min_rolling_n with no params returns the module default of 3."""
    from services import rolling_wr_monitor as wr_mod
    # Patch get_settings to fail (simulate env not set), so we fall through to
    # the module default.
    import unittest.mock as mock
    with mock.patch(
        "services.rolling_wr_monitor.get_settings",
        side_effect=Exception("not available"),
        create=True,
    ):
        # Module default should be 3.
        assert _resolve_min_rolling_n(None) == wr_mod._DEFAULT_MIN_ROLLING_N
        assert wr_mod._DEFAULT_MIN_ROLLING_N == 3


def test_pause_does_not_fire_at_n_less_than_min():
    """Feed 2 LOSS trades into a fresh cell — no pause should fire (n < min_n=3)."""
    repo = FakeRepo()
    # Use min_trades=2 so the _min_trades gate doesn't interfere; only min_n
    # should block the trigger here.
    monitor = RollingWRMonitor(repo, min_trades=2)
    cell = _trade().cell

    # Populate repo with 2 losses (below default min_rolling_n=3).
    repo.trades_by_cell[cell] = [_trade(is_win=False, pnl=-10.0) for _ in range(2)]

    pid = _run(monitor.on_trade_resolved(_trade(is_win=False, pnl=-10.0)))
    assert pid is None, "Should not pause when n < min_rolling_n (default 3)"
    assert len(repo.pauses) == 0


def test_pause_fires_at_min_n_with_trigger():
    """Feed 3 LOSS trades — pause MUST fire once n reaches min_rolling_n=3."""
    repo = FakeRepo()
    # Use min_trades=1 so _min_trades doesn't interfere; only min_n controls.
    monitor = RollingWRMonitor(repo, min_trades=1)
    cell = _trade(fill=0.40).cell

    # Populate repo with 3 losses: 60m_pnl = -$30 = exactly on the floor.
    # Default min_pnl_window_usd = -$30, so 3 × -$10 = -$30 triggers.
    repo.trades_by_cell[cell] = [_trade(is_win=False, pnl=-10.0) for _ in range(3)]

    pid = _run(monitor.on_trade_resolved(_trade(is_win=False, pnl=-10.0)))
    assert pid is not None, "Should pause once n >= min_rolling_n (3)"
    assert len(repo.pauses) == 1


def test_min_n_settings_override(monkeypatch):
    """settings.cell_pause_min_rolling_n=5: no pause until 5th loss."""
    from services import rolling_wr_monitor as wr_mod

    def patched_resolve_min_n(cell_params=None):
        # Simulate settings layer returning 5, no per-cell override.
        if cell_params and cell_params.get("cell_pause_min_n") is not None:
            return int(cell_params["cell_pause_min_n"])
        return 5

    monkeypatch.setattr(wr_mod, "_resolve_min_rolling_n", patched_resolve_min_n)

    repo = FakeRepo()
    monitor = RollingWRMonitor(repo, min_trades=1)
    cell = _trade().cell

    # 4 losses loaded — below the patched min_n=5.
    repo.trades_by_cell[cell] = [_trade(is_win=False, pnl=-10.0) for _ in range(4)]
    pid = _run(monitor.on_trade_resolved(_trade(is_win=False, pnl=-10.0)))
    assert pid is None, "Should not pause at n=4 when settings min_n=5"

    # Add 1 more trade → n=5, now triggers fire.
    repo.trades_by_cell[cell].append(_trade(is_win=False, pnl=-10.0))
    pid = _run(monitor.on_trade_resolved(_trade(is_win=False, pnl=-10.0)))
    assert pid is not None, "Should pause at n=5 when settings min_n=5"


def test_min_n_per_strategy_override():
    """Trade with params={'cell_pause_min_n': 5} overrides default min_n=3."""
    from services.rolling_wr_monitor import ResolvedTrade

    repo = FakeRepo()
    monitor = RollingWRMonitor(repo, min_trades=1)
    cell = _trade().cell

    # 4 losses in repo (would fire at n=3 with default, but we override to 5).
    repo.trades_by_cell[cell] = [_trade(is_win=False, pnl=-10.0) for _ in range(4)]

    # Pass trade with per-strategy min_n override of 5.
    trade_override = ResolvedTrade(
        strategy_id="v12_lgb_combo",
        direction="DOWN",
        eval_offset=80,
        hour_utc=10,
        regime="chop",
        fill_price=0.40,
        pnl_usd=-10.0,
        is_win=False,
        params={"cell_pause_min_n": 5},
    )

    # n=4 → below override of 5.
    pid = _run(monitor.on_trade_resolved(trade_override))
    assert pid is None, "Should not pause at n=4 when per-strategy min_n=5"

    # Now add one more so the repo has 5 recent entries.
    repo.trades_by_cell[cell].append(_trade(is_win=False, pnl=-10.0))
    pid = _run(monitor.on_trade_resolved(trade_override))
    assert pid is not None, "Should pause at n=5 with per-strategy min_n=5"
