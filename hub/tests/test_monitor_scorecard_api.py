"""Contract tests for hub/api/monitor_scorecard.py.

Verifies the /api/monitor/strategies/scorecard endpoint under a mocked
async DB session. Three queries are stubbed via execute side_effect:

  1. strategy_configs join (registry + effective mode)         — phase 1
  2. strategy_comparison latest snapshot rows (24h + 7d)       — phase 1
  3. strategy_decisions last_fire_at GROUP BY                  — phase 2
     (scoped to the strategy_id list from query 1; skipped if
     that list is empty so Postgres doesn't see `IN ()`)

The endpoint runs queries in two phases:
  * phase 1: configs + stats via asyncio.gather (parallel)
  * phase 2: last_fires scoped to ids derived from configs/stats

The mock returns rows deterministically — each .execute() call pops
the next result. Phase-1 ordering inside gather is configs-then-stats
because both coroutines hit their `await db.execute(...)` immediately
and the loop runs them in submission order.
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

    Endpoint issues up to three queries in two phases:
      phase 1: configs, stats  (parallel via asyncio.gather, submission order)
      phase 2: last_fires      (scoped to ids from phase 1; skipped if scope empty)

    Pass result_sets in the same order: (configs, stats, last_fires).
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


# ─── Query-scoping + timeout regression tests (504 fix) ──────────────────────


class TestQueryScoping:
    """Regression tests for the 504 fix.

    The original _Q_LAST_FIRE was an unbounded GROUP BY scan of strategy_decisions
    that took 5+ minutes under concurrent INSERT load and tripped the proxy 504.
    Fix scopes the query to a strategy_id IN (...) list (uses the composite index
    idx_sd_strategy_action_evaluated) and wraps every DB phase in asyncio.wait_for.

    These tests pin the new behaviour so the slow scan can't sneak back in.
    """

    def test_last_fires_query_uses_in_clause(self):
        """_Q_LAST_FIRE must filter by strategy_id IN (...) for index usage."""
        sql = " ".join(ms._Q_LAST_FIRE.split()).lower()
        assert "where strategy_id in :strategy_ids" in sql, (
            "_Q_LAST_FIRE must be scoped via strategy_id IN (...) — "
            "the unscoped variant tripped a 504 in PR #576"
        )

    def test_last_fires_skips_db_when_scope_empty(self):
        """Empty strategy_ids → fetcher returns [] without an execute() call.

        Postgres rejects ``WHERE strategy_id IN ()`` as a syntax error, so the
        fetcher must short-circuit. Mock session has zero execute side effects
        — any call would raise StopIteration.
        """
        import asyncio as _asyncio
        session = MagicMock()
        session.execute = AsyncMock(side_effect=StopIteration("must not be called"))
        result = _asyncio.run(ms._fetch_last_fires(session, []))
        assert result == []
        session.execute.assert_not_called()

    def test_scoped_last_fires_passes_config_ids_as_bindparam(self):
        """Phase-2 execute() receives a strategy_ids bind containing the configs ids.

        Builds the session manually so we can inspect call_args after the
        request — verifies the scoped query was wired with the right ID list.
        """
        configs = [
            _config_row(strategy_id="alpha"),
            _config_row(strategy_id="beta"),
        ]
        stats = [
            _stats_row(strategy_id="alpha"),
            _stats_row(strategy_id="beta"),
        ]
        fires = [
            _last_fire_row(strategy_id="alpha"),
            _last_fire_row(strategy_id="beta"),
        ]
        session = _fake_session(configs, stats, fires)

        app = _make_app()

        async def _session_override():
            yield session

        app.dependency_overrides[get_session] = _session_override
        app.dependency_overrides[get_current_user] = lambda: _fake_user()
        client = TestClient(app)

        resp = client.get("/api/monitor/strategies/scorecard")
        assert resp.status_code == 200
        # Three execute() calls total: configs, stats, last_fires (in order).
        assert session.execute.call_count == 3
        # The third call is the scoped last_fires query — bind dict at args[1].
        third_call_args, third_call_kwargs = session.execute.call_args_list[2]
        # bind dict is the second positional arg
        bind = third_call_args[1] if len(third_call_args) > 1 else third_call_kwargs.get("parameters", {})
        assert "strategy_ids" in bind
        assert set(bind["strategy_ids"]) == {"alpha", "beta"}

    def test_last_fires_timeout_returns_partial_data(self, monkeypatch):
        """On asyncio.TimeoutError in phase 2, endpoint serves partial data.

        Stats + configs still flow through; last_fire_at falls back to None
        on every row. The endpoint must return 200, NOT 504.
        """
        import asyncio as _asyncio

        # Patch asyncio.wait_for IN THE MODULE so phase-2 raises TimeoutError
        # while phase-1 (configs+stats) still resolves normally.
        original_wait_for = _asyncio.wait_for
        call_count = {"n": 0}

        async def _flaky_wait_for(coro, timeout):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Phase 1: let it run normally so configs/stats land.
                return await original_wait_for(coro, timeout)
            # Phase 2: cancel the coroutine and raise TimeoutError so the
            # endpoint's except branch fires. We must close the coroutine
            # to avoid "coroutine was never awaited" warnings.
            coro.close()
            raise _asyncio.TimeoutError()

        monkeypatch.setattr(ms.asyncio, "wait_for", _flaky_wait_for)

        client = _client([_config_row()], [_stats_row()], [_last_fire_row()])
        resp = client.get("/api/monitor/strategies/scorecard")
        assert resp.status_code == 200
        body = resp.json()
        # Endpoint did NOT 504 — payload is well-formed.
        assert "scorecards" in body
        assert len(body["scorecards"]) == 1
        row = body["scorecards"][0]
        # last_fire_at falls back to None because the phase-2 query timed out.
        assert row["last_fire_at"] is None
        # But stats from phase-1 still populated.
        assert row["today"]["fires"] == 28
        # And the timeout path was actually exercised (phase-2 wait_for ran).
        assert call_count["n"] == 2

    def test_phase_one_timeout_returns_empty_envelope(self, monkeypatch):
        """If phase-1 (configs+stats) itself times out, return empty scorecards.

        Last-fires phase still runs (against an empty scope_ids) and returns
        [] without an execute() call — endpoint stays 200.
        """
        import asyncio as _asyncio

        async def _always_timeout(coro, timeout):
            coro.close()
            raise _asyncio.TimeoutError()

        monkeypatch.setattr(ms.asyncio, "wait_for", _always_timeout)

        # Even with mocked rows, the wait_for short-circuit prevents them
        # from landing. We just need a session that won't error on instantiation.
        client = _client([_config_row()], [_stats_row()], [_last_fire_row()])
        resp = client.get("/api/monitor/strategies/scorecard")
        assert resp.status_code == 200
        body = resp.json()
        assert body["scorecards"] == []
        assert body["cache_hit"] is False
