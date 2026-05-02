"""
/desk Phase 2 — /api/desk/clob-book endpoint tests.

Covers:
  - Happy path: row with CLOB fields returns 1-deep book shape.
  - Empty book: no matching row returns 200 with empty asks/bids.
  - Stale flag: age_s > 60 sets stale=True; fresh row sets stale=False.
  - Missing table (42P01): degrades to empty 200.
  - Partial data: only up_ask populated, down side empty.
  - NULL clob columns with non-NULL row: treated as empty.

Follows hub/tests/test_desk_endpoints.py pattern — FastAPI dependency
overrides for session + auth; mock SQLAlchemy AsyncSession.
"""

from __future__ import annotations

import datetime
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from api.desk import router, _CLOB_STALE_THRESHOLD_S
from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session


# ── Helpers ─────────────────────────────────────────────────────────────────

class _FakeUndefinedTable(Exception):
    sqlstate = "42P01"


def _missing_table_error(table: str) -> ProgrammingError:
    orig = _FakeUndefinedTable(f'relation "{table}" does not exist')
    return ProgrammingError("SELECT ...", params={}, orig=orig)


def _build_app(session) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def fake_get_session():
        yield session

    async def fake_get_current_user():
        return TokenData(user_id=1, username="billy", token_type="access")

    app.dependency_overrides[get_session] = fake_get_session
    app.dependency_overrides[get_current_user] = fake_get_current_user
    return app


def _mk_session(script):
    """script: list of callables/values; each execute() pops one."""
    session = MagicMock()
    session.commit = AsyncMock(return_value=None)
    i = {"n": 0}

    async def fake_execute(stmt, params=None):
        idx = i["n"]
        i["n"] += 1
        item = script[idx] if idx < len(script) else None
        if isinstance(item, Exception):
            raise item
        return item

    session.execute = AsyncMock(side_effect=fake_execute)
    return session


def _mk_result(rows):
    result = MagicMock()
    mappings = MagicMock()
    mappings.all = MagicMock(return_value=rows)
    mappings.first = MagicMock(return_value=rows[0] if rows else None)
    result.mappings = MagicMock(return_value=mappings)
    result.first = MagicMock(return_value=rows[0] if rows else None)
    return result


def _fresh_ts() -> datetime.datetime:
    """Timezone-aware timestamp 5 seconds ago — within stale threshold."""
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=5)


def _stale_ts() -> datetime.datetime:
    """Timezone-aware timestamp 120 seconds ago — beyond stale threshold."""
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=120)


# ── Tests ────────────────────────────────────────────────────────────────────

def test_clob_book_happy_path():
    """Full row returns 1-deep book with correct prices."""
    row = {
        "clob_up_bid": 0.53,
        "clob_up_ask": 0.55,
        "clob_down_bid": 0.43,
        "clob_down_ask": 0.45,
        "clob_implied_up": 0.54,
        "created_at": _fresh_ts(),
    }
    session = _mk_session([_mk_result([row])])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.get("/api/desk/clob-book", params={"window_epoch": 1_700_000_100, "asset": "BTC"})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["source"] == "window_snapshots"
    assert body["yes"]["asks"] == [{"price": 0.55, "size": 0.0}]
    assert body["yes"]["bids"] == [{"price": 0.53, "size": 0.0}]
    assert body["no"]["asks"] == [{"price": 0.45, "size": 0.0}]
    assert body["no"]["bids"] == [{"price": 0.43, "size": 0.0}]
    assert body["implied_p_up"] == pytest.approx(0.54)
    assert body["spread"] == pytest.approx(0.02, rel=1e-5)
    assert body["imbalance"] is None
    assert body["stale"] is False
    assert body["age_s"] is not None
    assert body["age_s"] < 30  # fresh ts = ~5s old


def test_clob_book_stale_flag_set_when_old():
    """age_s > 60 → stale=True."""
    row = {
        "clob_up_bid": 0.51,
        "clob_up_ask": 0.53,
        "clob_down_bid": None,
        "clob_down_ask": None,
        "clob_implied_up": None,
        "created_at": _stale_ts(),  # 120 seconds old
    }
    session = _mk_session([_mk_result([row])])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.get("/api/desk/clob-book", params={"window_epoch": 1_700_000_100})
    assert resp.status_code == 200
    body = resp.json()
    assert body["stale"] is True
    assert body["age_s"] >= _CLOB_STALE_THRESHOLD_S


def test_clob_book_empty_when_no_row():
    """No matching window_snapshots row → 200 with empty book."""
    session = _mk_session([_mk_result([])])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.get("/api/desk/clob-book", params={"window_epoch": 1_700_000_100})
    assert resp.status_code == 200
    body = resp.json()
    assert body["yes"]["asks"] == []
    assert body["yes"]["bids"] == []
    assert body["no"]["asks"] == []
    assert body["no"]["bids"] == []
    assert body["source"] == "window_snapshots"
    assert body["implied_p_up"] is None


def test_clob_book_missing_table_degrades():
    """42P01 on window_snapshots → 200 empty book (no 500)."""
    session = _mk_session([_missing_table_error("window_snapshots")])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.get("/api/desk/clob-book", params={"window_epoch": 1_700_000_100})
    assert resp.status_code == 200
    body = resp.json()
    assert body["yes"]["asks"] == []
    assert body["source"] == "window_snapshots"


def test_clob_book_partial_row_up_only():
    """Row with only UP side populated — NO side returns empty."""
    row = {
        "clob_up_bid": 0.52,
        "clob_up_ask": 0.54,
        "clob_down_bid": None,
        "clob_down_ask": None,
        "clob_implied_up": 0.53,
        "created_at": _fresh_ts(),
    }
    session = _mk_session([_mk_result([row])])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.get("/api/desk/clob-book", params={"window_epoch": 1_700_000_100})
    assert resp.status_code == 200
    body = resp.json()
    assert body["yes"]["asks"] == [{"price": 0.54, "size": 0.0}]
    assert body["no"]["asks"] == []
    assert body["no"]["bids"] == []


def test_clob_book_null_clob_columns_treated_as_empty():
    """Row exists but all CLOB columns are NULL → empty book."""
    row = {
        "clob_up_bid": None,
        "clob_up_ask": None,
        "clob_down_bid": None,
        "clob_down_ask": None,
        "clob_implied_up": None,
        "created_at": _fresh_ts(),
    }
    # The SQL WHERE filters NULLs, so this should yield no rows.
    # But even if a row slips through, the endpoint should handle it gracefully.
    session = _mk_session([_mk_result([row])])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.get("/api/desk/clob-book", params={"window_epoch": 1_700_000_100})
    assert resp.status_code == 200
    body = resp.json()
    # All None → empty lists
    assert body["yes"]["asks"] == []
    assert body["yes"]["bids"] == []
    assert body["implied_p_up"] is None


def test_clob_book_requires_auth():
    """Endpoint returns 401/403 without valid JWT (no override)."""
    # Build app WITHOUT the auth override so the real dep is used.
    app = FastAPI()
    app.include_router(router, prefix="/api")
    # Still need a fake session to avoid real DB calls.
    session = _mk_session([_mk_result([])])

    async def fake_get_session():
        yield session

    app.dependency_overrides[get_session] = fake_get_session
    # Do NOT override get_current_user.

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/desk/clob-book", params={"window_epoch": 1_700_000_100})
    # FastAPI raises 422 for missing bearer OR 401 for real auth middleware.
    assert resp.status_code in (401, 403, 422)


def test_clob_book_spread_computed_correctly():
    """spread = up_ask - up_bid rounded to 6dp."""
    row = {
        "clob_up_bid": 0.54,
        "clob_up_ask": 0.56,
        "clob_down_bid": None,
        "clob_down_ask": None,
        "clob_implied_up": 0.55,
        "created_at": _fresh_ts(),
    }
    session = _mk_session([_mk_result([row])])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.get("/api/desk/clob-book", params={"window_epoch": 1_700_000_100})
    assert resp.status_code == 200
    body = resp.json()
    assert body["spread"] == pytest.approx(0.02, rel=1e-5)
