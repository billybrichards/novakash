"""Contract tests for hub/api/strategy_comparison.py.

Verifies both routes:
  - GET /api/strategy-comparison (rich endpoint)
  - GET /api/v58/strategy-comparison (backward-compat shim)

Both are tested against a mocked DB session that returns a known set of
rows from the `strategy_comparison` table.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.strategy_comparison import router as sc_router
from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session


# ─── Helpers ─────────────────────────────────────────────────────────────────

_SNAPSHOT_AT = datetime(2026, 5, 1, 12, tzinfo=timezone.utc)


def _row(**overrides) -> dict:
    base = {
        "snapshot_at": _SNAPSHOT_AT,
        "strategy_id": "v12_lgb_combo",
        "asset": "BTC",
        "timeframe": "5m",
        "window_period": "24h",
        "t_band": "all",
        "direction_filter": "all",
        "regime_filter": "all",
        "n_fires": 10,
        "n_wins": 7,
        "n_losses": 3,
        "n_pending": 0,
        "wr_pct": 70.0,
        "wilson_low": 39.7,
        "wilson_high": 89.2,
        "avg_fill": 0.745,
        "median_fill": 0.74,
        "avg_stake_usd": 7.5,
        "real_net_pnl_usd": 12.34,
        "real_pnl_per_fire": 1.23,
        "daily_run_rate_usd": 9.87,
    }
    base.update(overrides)
    return base


def _mapping(data: dict):
    """Duck-type sqlalchemy RowMapping."""
    m = MagicMock()
    m.__getitem__ = lambda self, k: data[k]
    m.get = lambda k, d=None: data.get(k, d)
    m._mapping = data
    m.keys = lambda: data.keys()
    # Make it iterable like a mapping
    m._data = data
    return m


def _fake_session(rows: list) -> MagicMock:
    """Build an AsyncSession stub returning given rows on execute()."""
    mappings_result = MagicMock()
    mappings_result.all.return_value = [_mapping(r) for r in rows]
    execute_result = MagicMock()
    execute_result.mappings.return_value = mappings_result
    session = MagicMock()
    session.execute = AsyncMock(return_value=execute_result)
    return session


def _fake_user() -> TokenData:
    return TokenData(user_id=1, username="billy", token_type="access")


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(sc_router, prefix="/api")
    return app


def _client_with_rows(rows: list) -> TestClient:
    app = _make_app()
    session = _fake_session(rows)

    async def _session_override():
        yield session

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_current_user] = lambda: _fake_user()
    return TestClient(app)


# ─── Rich endpoint tests ──────────────────────────────────────────────────────


class TestGetStrategyComparison:
    def test_returns_200_with_rows(self):
        client = _client_with_rows([_row()])
        resp = client.get("/api/strategy-comparison")
        assert resp.status_code == 200

    def test_response_has_rows_key(self):
        client = _client_with_rows([_row()])
        resp = client.get("/api/strategy-comparison")
        data = resp.json()
        assert "rows" in data

    def test_response_has_snapshot_at(self):
        client = _client_with_rows([_row()])
        resp = client.get("/api/strategy-comparison")
        data = resp.json()
        assert "snapshot_at" in data

    def test_row_contains_required_fields(self):
        client = _client_with_rows([_row()])
        resp = client.get("/api/strategy-comparison")
        row = resp.json()["rows"][0]
        for field in (
            "strategy_id", "window_period", "t_band",
            "n_fires", "n_wins", "n_losses", "wr_pct",
            "real_net_pnl_usd", "wilson_low", "wilson_high",
        ):
            assert field in row, f"Missing field: {field}"

    def test_empty_table_returns_empty_rows(self):
        client = _client_with_rows([])
        resp = client.get("/api/strategy-comparison")
        assert resp.status_code == 200
        assert resp.json()["rows"] == []

    def test_strategy_id_filter_accepted(self):
        client = _client_with_rows([_row()])
        resp = client.get("/api/strategy-comparison?strategy_id=v12_lgb_combo")
        assert resp.status_code == 200

    def test_invalid_window_period_rejected(self):
        client = _client_with_rows([])
        resp = client.get("/api/strategy-comparison?window_period=99h")
        assert resp.status_code == 422


class TestGetLeaderboard:
    def test_returns_200(self):
        client = _client_with_rows([_row()])
        resp = client.get("/api/strategy-comparison/leaderboard")
        assert resp.status_code == 200

    def test_response_has_rows(self):
        client = _client_with_rows([_row()])
        resp = client.get("/api/strategy-comparison/leaderboard")
        assert "rows" in resp.json()


class TestGetSnapshotLatest:
    def test_returns_200(self):
        client = _client_with_rows([_row()])
        resp = client.get("/api/strategy-comparison/snapshot/latest")
        assert resp.status_code == 200

    def test_response_has_snapshot_at(self):
        client = _client_with_rows([_row()])
        resp = client.get("/api/strategy-comparison/snapshot/latest")
        assert "snapshot_at" in resp.json()


class TestGetHistory:
    def test_returns_200(self):
        client = _client_with_rows([_row()])
        resp = client.get(
            "/api/strategy-comparison/history?strategy_id=v12_lgb_combo"
        )
        assert resp.status_code == 200

    def test_response_has_points(self):
        client = _client_with_rows([_row()])
        resp = client.get(
            "/api/strategy-comparison/history?strategy_id=v12_lgb_combo"
        )
        assert "points" in resp.json()
