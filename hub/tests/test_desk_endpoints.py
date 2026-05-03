"""
/desk Phase 1 — hub API tests.

Covers:
  - GET /api/windows/current returns correct 5-minute-floored window epoch,
    seconds_remaining matches the math, target_price_chainlink falls back
    to None when ticks_chainlink is missing / empty.
  - POST /api/desk/picks returns created=true on first call, created=false
    (UPSERT branch) on repeat for the same window_epoch.
  - Pick validation: pick ∈ {UP, DOWN, SKIP} is enforced.
  - GET /api/desk/picks returns rows with outcome=null when the view is
    empty / missing.

Follows the hub test pattern from test_positions_api.py — FastAPI
dependency overrides for session + auth; mock SQLAlchemy AsyncSession
with scripted `execute()` side effects.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from api.desk import router, _window_floor, WINDOW_SECONDS
from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session


# ── Test helpers ────────────────────────────────────────────────────────────

class _FakeUndefinedTable(Exception):
    sqlstate = "42P01"


def _missing_table_error(table: str) -> ProgrammingError:
    orig = _FakeUndefinedTable(f'relation "{table}" does not exist')
    return ProgrammingError("SELECT ...", params={}, orig=orig)


def _build_app(session):
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
    """script is a list of callables/values; each call to execute() pops one."""
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
    """Shape a MagicMock like an SQLAlchemy Result."""
    result = MagicMock()
    mappings = MagicMock()
    mappings.all = MagicMock(return_value=rows)
    mappings.first = MagicMock(return_value=rows[0] if rows else None)
    result.mappings = MagicMock(return_value=mappings)
    result.first = MagicMock(return_value=rows[0] if rows else None)
    return result


# ── _window_floor unit ──────────────────────────────────────────────────────

def test_window_floor_rounds_down_to_5min():
    assert _window_floor(0) == 0
    assert _window_floor(299) == 0
    assert _window_floor(300) == 300
    assert _window_floor(301) == 300
    assert _window_floor(1_700_000_123) == (1_700_000_123 // 300) * 300
    assert WINDOW_SECONDS == 300


# ── GET /api/windows/current ────────────────────────────────────────────────

def test_current_window_returns_shape_with_price():
    # 1st query: window_snapshots (canonical) — empty so we exercise fallback.
    # 2nd query: ticks_chainlink — returns one row.
    session = _mk_session([
        _mk_result([]),                          # window_snapshots empty
        _mk_result([(67_500.42,)]),              # ticks_chainlink
    ])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.get("/api/windows/current?asset=BTC")
    assert resp.status_code == 200
    data = resp.json()
    # window_epoch is 5-min aligned
    assert data["window_epoch"] % 300 == 0
    assert data["t_close_ts"] == data["t_open_ts"] + 300
    assert 0 <= data["seconds_remaining"] <= 300
    # Legacy field still populated for back-compat.
    assert data["target_price_chainlink"] == 67_500.42
    # Canonical falls back to chainlink when window_snapshots is empty.
    assert data["target_price"] == 67_500.42
    assert data["target_price_source"] == "chainlink_polygon_fallback"
    assert data["asset"] == "BTC"
    assert data["timeframe"] == "5m"


def test_current_window_prefers_canonical_open_price():
    # Both sources present — canonical (window_snapshots.open_price) wins.
    # Row is (open_price, open_price_source) — 2 columns after PR #desk-pricetobeat-verify.
    session = _mk_session([
        _mk_result([(67_580.99, "polymarket_html_priceToBeat")]),  # window_snapshots canonical
        _mk_result([(67_500.42,)]),                                 # ticks_chainlink (legacy)
    ])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.get("/api/windows/current?asset=BTC")
    assert resp.status_code == 200
    data = resp.json()
    assert data["target_price"] == 67_580.99
    # Source is returned verbatim from the DB (engine taxonomy string).
    assert data["target_price_source"] == "polymarket_html_priceToBeat"
    assert data["target_price_chainlink"] == 67_500.42


def test_current_window_prefers_canonical_no_source_tag():
    # open_price present but open_price_source is NULL (pre-migration row).
    # Hub should infer 'polymarket_canonical' as before.
    session = _mk_session([
        _mk_result([(67_580.99, None)]),         # window_snapshots — no source tag
        _mk_result([(67_500.42,)]),              # ticks_chainlink (legacy)
    ])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.get("/api/windows/current?asset=BTC")
    assert resp.status_code == 200
    data = resp.json()
    assert data["target_price"] == 67_580.99
    assert data["target_price_source"] == "polymarket_canonical"
    assert data["target_price_chainlink"] == 67_500.42


def test_current_window_degrades_when_ticks_chainlink_missing():
    # window_snapshots empty, ticks_chainlink table missing entirely.
    session = _mk_session([
        _mk_result([]),                          # window_snapshots empty
        _missing_table_error("ticks_chainlink"),
    ])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.get("/api/windows/current?asset=BTC")
    assert resp.status_code == 200
    data = resp.json()
    assert data["target_price"] is None
    assert data["target_price_chainlink"] is None
    assert data["target_price_source"] == "unknown"
    # Still returns window clock fields.
    assert data["window_epoch"] % 300 == 0


def test_current_window_degrades_when_chainlink_empty():
    session = _mk_session([
        _mk_result([]),                          # window_snapshots empty
        _mk_result([]),                          # ticks_chainlink empty
    ])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.get("/api/windows/current")
    assert resp.status_code == 200
    body = resp.json()
    assert body["target_price"] is None
    assert body["target_price_chainlink"] is None
    assert body["target_price_source"] == "unknown"


# ── POST /api/desk/picks ────────────────────────────────────────────────────

def test_post_pick_created_true_on_first_insert():
    # UPSERT RETURNING yields (id, created=True)
    session = _mk_session([_mk_result([{"id": 7, "created": True}])])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.post(
        "/api/desk/picks",
        json={
            "window_epoch": 1_700_000_100,
            "pick": "UP",
            "t_remaining_s": 241,
            "notes": "high-conviction classifier",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["pick_id"] == 7
    assert data["created"] is True
    session.commit.assert_awaited()


def test_post_pick_created_false_on_upsert_update():
    # second POST for same window → UPSERT hits the UPDATE branch (xmax != 0)
    session = _mk_session([_mk_result([{"id": 7, "created": False}])])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.post(
        "/api/desk/picks",
        json={"window_epoch": 1_700_000_100, "pick": "DOWN", "t_remaining_s": 90},
    )
    assert resp.status_code == 200
    assert resp.json()["created"] is False


def test_post_pick_rejects_bad_direction():
    session = _mk_session([])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.post(
        "/api/desk/picks",
        json={"window_epoch": 1_700_000_100, "pick": "SIDEWAYS", "t_remaining_s": 60},
    )
    assert resp.status_code == 422  # pydantic pattern-match fail


def test_post_pick_503_when_table_missing():
    session = _mk_session([_missing_table_error("desk_picks")])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.post(
        "/api/desk/picks",
        json={"window_epoch": 1_700_000_100, "pick": "UP", "t_remaining_s": 120},
    )
    assert resp.status_code == 503


# ── GET /api/desk/picks ─────────────────────────────────────────────────────

def test_get_picks_empty_when_no_rows():
    session = _mk_session([_mk_result([])])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.get("/api/desk/picks")
    assert resp.status_code == 200
    assert resp.json()["rows"] == []


def test_get_picks_returns_null_outcome_when_view_missing():
    pick_row = {
        "id": 1,
        "window_epoch": 1_700_000_100,
        "asset": "BTC",
        "timeframe": "5m",
        "pick": "UP",
        "t_remaining_s": 210,
        "notes": None,
        "created_at": None,
        "updated_at": None,
    }
    session = _mk_session([
        _mk_result([pick_row]),
        _missing_table_error("strategy_decisions_resolved"),
    ])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.get("/api/desk/picks")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["pick"] == "UP"
    assert rows[0]["outcome"] is None
    assert rows[0]["actual_direction"] is None


def test_get_picks_503_when_desk_picks_missing_returns_empty_shape():
    session = _mk_session([_missing_table_error("desk_picks")])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.get("/api/desk/picks")
    # we chose to degrade to empty rows with _meta, not 503, so the FE
    # can still render its table skeleton.
    assert resp.status_code == 200
    body = resp.json()
    assert body["rows"] == []
    assert "desk_picks" in body["_meta"]["missing"]
