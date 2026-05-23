"""Contract tests for hub/api/monitor_scorecard.py.

Verifies the /api/monitor/strategies/scorecard endpoint under a mocked
async DB session. Three queries are stubbed via execute side_effect:

  1. strategy_configs join (registry + effective mode)
  2. strategy_comparison latest snapshot rows (24h + 7d)
  3. strategy_decisions last_fire_at GROUP BY

The mock returns successive result sets in the order the endpoint's
asyncio.gather schedules them. The implementation imports and uses
asyncio.gather, but the mock returns rows deterministically regardless
of gather ordering — each .execute() call pops the next result.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.monitor_scorecard as ms
from api.monitor_scorecard import router as scorecard_router
from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session


# ─── Fixture row helpers ─────────────────────────────────────────────────────

_NOW = datetime(2026, 5, 22, 12, tzinfo=timezone.utc)


_DEFAULT_YAML = """
mode: LIVE
asset: BTC
timescale: 5m
gates:
  up_threshold: 0.62
  down_threshold: 0.58
"""


def _config_row(**overrides) -> dict:
    base = {
        "strategy_id": "v9_btc_lgb",
        "config_mode": "LIVE",
        "asset": "BTC",
        "timescale": "5m",
        "config_yaml": _DEFAULT_YAML,
        "updated_at": _NOW,
        "override_mode": None,
    }
    base.update(overrides)
    return base


def _stats_row(**overrides) -> dict:
    base = {
        "strategy_id": "v9_btc_lgb",
        "window_period": "24h",
        "n_fires": 28,
        "n_wins": 22,
        "n_losses": 5,
        "n_pending": 1,
        "wr_pct": 80.8,
        "real_net_pnl_usd": 31.20,
    }
    base.update(overrides)
    return base


def _last_fire_row(**overrides) -> dict:
    base = {"strategy_id": "v9_btc_lgb", "last_fire_at": _NOW}
    base.update(overrides)
    return base


# ─── Mock plumbing ───────────────────────────────────────────────────────────


def _mapping(data: dict):
    """Duck-type sqlalchemy RowMapping: supports both subscript and dict-like."""
    m = MagicMock()
    m.__getitem__ = lambda self, k: data[k]
    m.get = lambda k, d=None: data.get(k, d)
    m.keys = lambda: data.keys()
    return m


def _result(rows: list[dict]) -> MagicMock:
    """Build the chained ``await db.execute(...).mappings().all()`` mock."""
    mappings_result = MagicMock()
    mappings_result.all.return_value = [_mapping(r) for r in rows]
    execute_result = MagicMock()
    execute_result.mappings.return_value = mappings_result
    return execute_result


def _fake_session(*result_sets: list[dict]) -> MagicMock:
    """AsyncSession stub whose execute() returns the next result set on each call.

    The endpoint runs three queries via asyncio.gather; mock returns them
    in declaration order. Order in _fetch_configs/_fetch_stats/_fetch_last_fires
    matches the endpoint's gather() argument order.
    """
    results = [_result(rs) for rs in result_sets]
    session = MagicMock()
    session.execute = AsyncMock(side_effect=results)
    return session


def _fake_user() -> TokenData:
    return TokenData(user_id=1, username="billy", token_type="access")


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(scorecard_router, prefix="/api")
    return app


def _client(configs: list[dict], stats: list[dict], fires: list[dict]) -> TestClient:
    app = _make_app()
    session = _fake_session(configs, stats, fires)

    async def _session_override():
        yield session

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_current_user] = lambda: _fake_user()
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset module-level TTL cache between tests so cache_hit assertions hold."""
    ms._cache["data"] = None
    ms._cache["ts"] = 0.0
    yield
    ms._cache["data"] = None
    ms._cache["ts"] = 0.0


# ─── Tests ───────────────────────────────────────────────────────────────────


class TestScorecardEndpoint:
    def test_returns_200(self):
        client = _client([_config_row()], [_stats_row()], [_last_fire_row()])
        resp = client.get("/api/monitor/strategies/scorecard")
        assert resp.status_code == 200

    def test_response_has_scorecards_key(self):
        client = _client([_config_row()], [_stats_row()], [_last_fire_row()])
        resp = client.get("/api/monitor/strategies/scorecard")
        assert "scorecards" in resp.json()

    def test_response_has_fetched_at(self):
        client = _client([_config_row()], [_stats_row()], [_last_fire_row()])
        resp = client.get("/api/monitor/strategies/scorecard")
        assert "fetched_at" in resp.json()
        assert resp.json()["fetched_at"] is not None

    def test_cache_hit_false_on_first_call(self):
        client = _client([_config_row()], [_stats_row()], [_last_fire_row()])
        resp = client.get("/api/monitor/strategies/scorecard")
        assert resp.json()["cache_hit"] is False

    def test_empty_configs_returns_empty_scorecards(self):
        client = _client([], [], [])
        resp = client.get("/api/monitor/strategies/scorecard")
        assert resp.status_code == 200
        assert resp.json()["scorecards"] == []

    def test_scorecard_has_required_fields(self):
        client = _client([_config_row()], [_stats_row()], [_last_fire_row()])
        resp = client.get("/api/monitor/strategies/scorecard")
        row = resp.json()["scorecards"][0]
        for field in (
            "strategy_id", "mode", "asset", "timescale", "is_ghost",
            "today", "week", "last_fire_at",
            "up_threshold", "down_threshold",
        ):
            assert field in row, f"Missing field: {field}"

    def test_today_sub_fields_present(self):
        client = _client([_config_row()], [_stats_row()], [_last_fire_row()])
        resp = client.get("/api/monitor/strategies/scorecard")
        today = resp.json()["scorecards"][0]["today"]
        for field in ("fires", "wins", "losses", "pending", "wr_pct", "net_pnl_usd"):
            assert field in today, f"Missing today.{field}"

    def test_live_mode_sets_is_ghost_false(self):
        client = _client(
            [_config_row(config_mode="LIVE", override_mode=None)],
            [_stats_row()],
            [_last_fire_row()],
        )
        resp = client.get("/api/monitor/strategies/scorecard")
        row = resp.json()["scorecards"][0]
        assert row["mode"] == "LIVE"
        assert row["is_ghost"] is False

    def test_ghost_mode_sets_is_ghost_true(self):
        client = _client(
            [_config_row(config_mode="GHOST")],
            [_stats_row()],
            [_last_fire_row()],
        )
        resp = client.get("/api/monitor/strategies/scorecard")
        row = resp.json()["scorecards"][0]
        assert row["mode"] == "GHOST"
        assert row["is_ghost"] is True

    def test_threshold_parsed_from_yaml(self):
        client = _client([_config_row()], [_stats_row()], [_last_fire_row()])
        resp = client.get("/api/monitor/strategies/scorecard")
        row = resp.json()["scorecards"][0]
        assert row["up_threshold"] == 0.62
        assert row["down_threshold"] == 0.58

    def test_live_sorts_before_ghost(self):
        configs = [
            # GHOST with high pnl
            _config_row(strategy_id="ghost_winner", config_mode="GHOST"),
            # LIVE with lower pnl — should still come first because LIVE-block leads.
            _config_row(strategy_id="live_loser", config_mode="LIVE"),
        ]
        stats = [
            _stats_row(strategy_id="ghost_winner", window_period="24h",
                       real_net_pnl_usd=100.0),
            _stats_row(strategy_id="live_loser", window_period="24h",
                       real_net_pnl_usd=-5.0),
        ]
        fires = [
            _last_fire_row(strategy_id="ghost_winner"),
            _last_fire_row(strategy_id="live_loser"),
        ]
        client = _client(configs, stats, fires)
        rows = client.get("/api/monitor/strategies/scorecard").json()["scorecards"]
        assert rows[0]["strategy_id"] == "live_loser", "LIVE must precede GHOST"
        assert rows[1]["strategy_id"] == "ghost_winner"
        assert rows[0]["mode"] == "LIVE"
        assert rows[1]["mode"] == "GHOST"

    def test_stats_values_mapped_correctly(self):
        client = _client([_config_row()], [_stats_row()], [_last_fire_row()])
        resp = client.get("/api/monitor/strategies/scorecard")
        today = resp.json()["scorecards"][0]["today"]
        assert today["fires"] == 28
        assert today["wins"] == 22
        assert today["losses"] == 5
        assert today["pending"] == 1
        assert today["wr_pct"] == 80.8
        assert today["net_pnl_usd"] == 31.20
