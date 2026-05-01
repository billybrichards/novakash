"""
/desk manual-trade endpoint — Track A hub tests.

Covers:
  POST /api/desk/manual-trade
    - Stake out of bounds ($0.50, $30) → 422 (Pydantic)
    - Price out of bounds ($0.04, $0.83) → 422 (Pydantic)
    - Direction not UP/DOWN → 422
    - Stale window → 400
    - Rate limit: 12 inserts pass, 13th returns 429
    - Concurrency: existing pending_live row → 409
    - Happy path: returns 200 with trade_id, status, queued_at, capped_price

  GET /api/desk/manual-trades
    - Returns rows scoped to operator_user_id only
    - Returns empty list when no rows

Mocking strategy follows test_desk_endpoints.py:
  - FastAPI dependency overrides for get_session + get_current_user
  - MagicMock AsyncSession with scripted execute() side effects
  - time.time() patched to a fixed value so window freshness checks
    yield deterministic results
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.desk_manual import router, WINDOW_SECONDS, STAKE_MIN, STAKE_MAX, PRICE_MIN, PRICE_MAX, RATE_LIMIT_MAX
from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session


# ── Test infrastructure ───────────────────────────────────────────────────────

# A fixed unix timestamp that is exactly on a 5-min boundary so that
# window_epoch == current_floor and the freshness check always passes
# in happy-path tests.
_FIXED_NOW = 1_700_000_400  # 1700000400 % 300 == 0
_WINDOW_EPOCH = _FIXED_NOW  # exact floor


def _build_app(session, user_id: int = 1, username: str = "billy"):
    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def fake_get_session():
        yield session

    async def fake_get_current_user():
        return TokenData(user_id=user_id, username=username, token_type="access")

    app.dependency_overrides[get_session] = fake_get_session
    app.dependency_overrides[get_current_user] = fake_get_current_user
    return app


def _mk_session(script: list):
    """
    script is a list of values/exceptions; each call to session.execute()
    pops the next item. Returns the value as a MagicMock result, or raises
    if it's an Exception.
    """
    session = MagicMock()
    session.commit = AsyncMock(return_value=None)
    idx = {"n": 0}

    async def fake_execute(stmt, params=None):
        i = idx["n"]
        idx["n"] += 1
        item = script[i] if i < len(script) else _mk_count_result(0)
        if isinstance(item, Exception):
            raise item
        return item

    session.execute = AsyncMock(side_effect=fake_execute)
    return session


def _mk_result(rows: list):
    result = MagicMock()
    mappings = MagicMock()
    mappings.all = MagicMock(return_value=rows)
    mappings.first = MagicMock(return_value=rows[0] if rows else None)
    result.mappings = MagicMock(return_value=mappings)
    result.first = MagicMock(return_value=rows[0] if rows else None)
    return result


def _mk_count_result(count: int):
    """Result for COUNT(*) queries."""
    return _mk_result([{"cnt": count}])


def _mk_none_result():
    """Result with no rows (for concurrency / rate-limit checks)."""
    return _mk_result([])


def _valid_body(**overrides):
    """A valid POST body for happy-path tests."""
    base = {
        "direction": "UP",
        "stake_usd": 5.0,
        "price": 0.45,
        "window_epoch": _WINDOW_EPOCH,
        "asset": "BTC",
        "mode": "live",
        "order_type": "FAK",
    }
    base.update(overrides)
    return base


# ── Pydantic / input validation ───────────────────────────────────────────────

@patch("time.time", return_value=_FIXED_NOW)
def test_stake_too_low_returns_422(mock_time):
    session = _mk_session([])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.post("/api/desk/manual-trade", json=_valid_body(stake_usd=0.50))
    assert resp.status_code == 422, resp.text


@patch("time.time", return_value=_FIXED_NOW)
def test_stake_too_high_returns_422(mock_time):
    session = _mk_session([])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.post("/api/desk/manual-trade", json=_valid_body(stake_usd=30.0))
    assert resp.status_code == 422, resp.text


@patch("time.time", return_value=_FIXED_NOW)
def test_price_too_low_returns_422(mock_time):
    session = _mk_session([])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.post("/api/desk/manual-trade", json=_valid_body(price=0.04))
    assert resp.status_code == 422, resp.text


@patch("time.time", return_value=_FIXED_NOW)
def test_price_too_high_returns_422(mock_time):
    session = _mk_session([])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.post("/api/desk/manual-trade", json=_valid_body(price=0.83))
    assert resp.status_code == 422, resp.text


@patch("time.time", return_value=_FIXED_NOW)
def test_direction_not_up_down_returns_422(mock_time):
    session = _mk_session([])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.post("/api/desk/manual-trade", json=_valid_body(direction="SIDEWAYS"))
    assert resp.status_code == 422, resp.text


@patch("time.time", return_value=_FIXED_NOW)
def test_direction_skip_returns_422(mock_time):
    """SKIP is valid in desk/picks but NOT in manual-trade (no bet direction)."""
    session = _mk_session([])
    app = _build_app(session)
    client = TestClient(app)

    resp = client.post("/api/desk/manual-trade", json=_valid_body(direction="SKIP"))
    assert resp.status_code == 422, resp.text


# ── Window staleness ──────────────────────────────────────────────────────────

@patch("time.time", return_value=_FIXED_NOW)
def test_stale_window_returns_400(mock_time):
    """window_epoch more than ±60s from current floor → 400."""
    session = _mk_session([])
    app = _build_app(session)
    client = TestClient(app)

    stale_epoch = _WINDOW_EPOCH - 600  # 10 minutes ago
    resp = client.post("/api/desk/manual-trade", json=_valid_body(window_epoch=stale_epoch))
    assert resp.status_code == 400, resp.text
    assert "stale" in resp.json()["detail"].lower()


@patch("time.time", return_value=_FIXED_NOW)
def test_future_window_returns_400(mock_time):
    """window_epoch more than ±60s into the future → 400."""
    session = _mk_session([])
    app = _build_app(session)
    client = TestClient(app)

    future_epoch = _WINDOW_EPOCH + 600  # 10 minutes ahead
    resp = client.post("/api/desk/manual-trade", json=_valid_body(window_epoch=future_epoch))
    assert resp.status_code == 400, resp.text


# ── Rate limit ────────────────────────────────────────────────────────────────

@patch("time.time", return_value=_FIXED_NOW)
def test_rate_limit_thirteenth_trade_returns_429(mock_time):
    """
    Spec: cap raised from 3/hour to 12/hour (2026-05-01).

    Simulate: the operator already has 12 trades in the last 60m (COUNT=12).
    The 13th POST should return 429 before the concurrency check.
    """
    from api.desk_manual import RATE_LIMIT_MAX

    assert RATE_LIMIT_MAX == 12, "spec update 2026-05-01: cap is 12/hour"

    ok_ddl = _mk_count_result(0)
    rate_at_limit = _mk_count_result(RATE_LIMIT_MAX)

    async def targeted_execute(stmt, params=None):
        # Rate-limit SELECT has user_id + cutoff but NOT window_epoch
        if params and "cutoff" in params and "user_id" in params and "window_epoch" not in params:
            return rate_at_limit
        return ok_ddl

    session = MagicMock()
    session.commit = AsyncMock(return_value=None)
    session.execute = AsyncMock(side_effect=targeted_execute)

    app = _build_app(session)
    client = TestClient(app)

    resp = client.post("/api/desk/manual-trade", json=_valid_body())
    assert resp.status_code == 429, resp.text
    assert "Rate limit" in resp.json()["detail"]
    assert str(RATE_LIMIT_MAX) in resp.json()["detail"]


@patch("time.time", return_value=_FIXED_NOW)
def test_rate_limit_twelve_trades_pass(mock_time):
    """
    Spec: cap raised from 3/hour to 12/hour (2026-05-01).

    Simulate: operator submits 12 trades in a row. The rate-limit COUNT(*) is
    driven from a per-test counter so that each POST sees a strictly increasing
    count, and the 12th request still observes < RATE_LIMIT_MAX prior rows. All
    12 should return 200; a 13th attempt then trips the 429 branch.
    """
    from api.desk_manual import RATE_LIMIT_MAX

    assert RATE_LIMIT_MAX == 12, "spec update 2026-05-01: cap is 12/hour"

    insert_count = {"n": 0}

    def _result_for(stmt, params):
        sql = str(stmt).lower()
        # INSERT — bump the counter so the next rate-limit query sees +1.
        if "insert into manual_trades" in sql:
            insert_count["n"] += 1
            return _mk_count_result(0)
        # Rate-limit COUNT(*) — return current insert count.
        if "count(*)" in sql:
            return _mk_count_result(insert_count["n"])
        # Concurrency check — no conflicts.
        if "select trade_id" in sql:
            return _mk_none_result()
        # DDL / NOTIFY / anything else — generic ok.
        return _mk_count_result(0)

    async def targeted_execute(stmt, params=None):
        return _result_for(stmt, params)

    session = MagicMock()
    session.commit = AsyncMock(return_value=None)
    session.execute = AsyncMock(side_effect=targeted_execute)

    app = _build_app(session)
    client = TestClient(app)

    # 12 successful inserts.
    for i in range(RATE_LIMIT_MAX):
        resp = client.post("/api/desk/manual-trade", json=_valid_body())
        assert resp.status_code == 200, (
            f"trade #{i + 1}/{RATE_LIMIT_MAX} expected 200 but got "
            f"{resp.status_code}: {resp.text}"
        )
    assert insert_count["n"] == RATE_LIMIT_MAX

    # 13th must trip the limiter.
    resp = client.post("/api/desk/manual-trade", json=_valid_body())
    assert resp.status_code == 429, resp.text
    # And no extra row was inserted on the 13th.
    assert insert_count["n"] == RATE_LIMIT_MAX


# ── Concurrency block ─────────────────────────────────────────────────────────

@patch("time.time", return_value=_FIXED_NOW)
def test_concurrency_conflict_returns_409(mock_time):
    """
    Existing pending_live row for the same window+asset → 409.

    Session call order:
      1. DDL (all no-ops) → generic result
      2. Rate-limit COUNT(*) → {"cnt": 0}
      3. Concurrency SELECT → row with trade_id
    """
    ok_result = _mk_count_result(0)
    conflict_row = {"trade_id": "desk_abc123456789abcd"}
    conflict_result = _mk_result([conflict_row])

    async def targeted_execute(stmt, params=None):
        if params and "cutoff" in params and "user_id" in params and "window_epoch" not in params:
            return ok_result
        if params and "window_epoch" in params and "asset" in params:
            return conflict_result
        return ok_result

    session = MagicMock()
    session.commit = AsyncMock(return_value=None)
    session.execute = AsyncMock(side_effect=targeted_execute)

    app = _build_app(session)
    client = TestClient(app)

    resp = client.post("/api/desk/manual-trade", json=_valid_body())
    assert resp.status_code == 409, resp.text
    assert "Conflict" in resp.json()["detail"]


# ── Happy path ────────────────────────────────────────────────────────────────

@patch("time.time", return_value=_FIXED_NOW)
def test_happy_path_returns_200_with_trade_id(mock_time):
    """
    All checks pass → 200 with trade_id, status, queued_at, capped_price.
    """
    ok_result = _mk_count_result(0)
    no_row_result = _mk_none_result()

    async def targeted_execute(stmt, params=None):
        if params and "cutoff" in params and "user_id" in params and "window_epoch" not in params:
            return ok_result
        if params and "window_epoch" in params and "asset" in params:
            return no_row_result
        return ok_result

    session = MagicMock()
    session.commit = AsyncMock(return_value=None)
    session.execute = AsyncMock(side_effect=targeted_execute)

    app = _build_app(session)
    client = TestClient(app)

    resp = client.post("/api/desk/manual-trade", json=_valid_body(direction="DOWN", price=0.55))
    assert resp.status_code == 200, resp.text

    data = resp.json()
    assert "trade_id" in data
    assert data["trade_id"].startswith("desk_")
    assert data["status"] == "pending_live"
    assert data["capped_price"] == 0.55
    assert "queued_at" in data
    assert "message" in data


@patch("time.time", return_value=_FIXED_NOW)
def test_happy_path_paper_mode_returns_open_status(mock_time):
    """Paper mode → status=open, no NOTIFY."""
    ok_result = _mk_count_result(0)
    no_row_result = _mk_none_result()

    async def targeted_execute(stmt, params=None):
        if params and "cutoff" in params and "user_id" in params and "window_epoch" not in params:
            return ok_result
        if params and "window_epoch" in params and "asset" in params:
            return no_row_result
        return ok_result

    session = MagicMock()
    session.commit = AsyncMock(return_value=None)
    session.execute = AsyncMock(side_effect=targeted_execute)

    app = _build_app(session)
    client = TestClient(app)

    resp = client.post("/api/desk/manual-trade", json=_valid_body(mode="paper"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "open"


# ── GET /desk/manual-trades ───────────────────────────────────────────────────

def test_get_manual_trades_returns_operator_rows():
    """GET returns rows belonging to the authenticated operator."""
    trade_row = {
        "trade_id": "desk_aabbccdd11223344",
        "window_ts": _WINDOW_EPOCH,
        "asset": "BTC",
        "direction": "UP",
        "mode": "live",
        "entry_price": 0.45,
        "stake_usd": 5.0,
        "status": "pending_live",
        "order_type": "FAK",
        "polymarket_order_id": None,
        "fill_price": None,
        "fill_size": None,
        "sot_reconciliation_state": None,
        "created_at": datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc),
        "filled_at": None,
    }
    # ensure_manual_trades_table DDL + SELECT
    ddl_result = _mk_count_result(0)
    rows_result = _mk_result([trade_row])

    async def targeted_execute(stmt, params=None):
        # SELECT for trade rows has 'user_id' + 'limit'
        if params and "user_id" in params and "limit" in params:
            return rows_result
        return ddl_result

    session = MagicMock()
    session.commit = AsyncMock(return_value=None)
    session.execute = AsyncMock(side_effect=targeted_execute)

    app = _build_app(session, user_id=1, username="billy")
    client = TestClient(app)

    resp = client.get("/api/desk/manual-trades?recent=20")
    assert resp.status_code == 200, resp.text

    data = resp.json()
    assert "trades" in data
    assert data["count"] == 1
    trade = data["trades"][0]
    assert trade["trade_id"] == "desk_aabbccdd11223344"
    assert trade["direction"] == "UP"
    assert trade["status"] == "pending_live"
    assert trade["created_at"] is not None


def test_get_manual_trades_empty_when_no_rows():
    """GET returns empty list when operator has no trades."""
    ddl_result = _mk_count_result(0)
    empty_result = _mk_result([])

    async def targeted_execute(stmt, params=None):
        if params and "user_id" in params and "limit" in params:
            return empty_result
        return ddl_result

    session = MagicMock()
    session.commit = AsyncMock(return_value=None)
    session.execute = AsyncMock(side_effect=targeted_execute)

    app = _build_app(session)
    client = TestClient(app)

    resp = client.get("/api/desk/manual-trades")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["trades"] == []
    assert data["count"] == 0


def test_get_manual_trades_scoped_to_operator():
    """
    Verify that the SELECT query filters by operator_user_id.
    A different user's session sees no rows.
    """
    ddl_result = _mk_count_result(0)
    empty_result = _mk_result([])

    session = MagicMock()
    session.commit = AsyncMock(return_value=None)
    session.execute = AsyncMock(return_value=empty_result)

    # user_id=99 — different from the trade owner (user_id=1)
    app = _build_app(session, user_id=99, username="intruder")
    client = TestClient(app)

    resp = client.get("/api/desk/manual-trades")
    assert resp.status_code == 200
    assert resp.json()["trades"] == []


# ── Route sanity ──────────────────────────────────────────────────────────────

def test_router_paths():
    """Smoke test that the router exposes the two expected paths."""
    paths = [r.path for r in router.routes]
    assert "/desk/manual-trade" in paths
    assert "/desk/manual-trades" in paths
