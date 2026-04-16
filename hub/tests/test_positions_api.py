"""
Task 8 — tests for hub/api/positions.py.

GET /api/positions/snapshot returns the dict shape the Telegram-page top
bar consumes. The endpoint must:

  1. Require JWT auth (we override the dep here, but real client without
     a token would receive 401 — covered implicitly by reusing the same
     get_current_user dep used everywhere else in the hub).
  2. Return the full 13-key shape even when the source-of-truth tables
     (poly_wallet_balance, poly_pending_wins, redeemer_state) don't exist
     yet — the per-table queries are wrapped in try/except so a missing
     table returns sensible zeros instead of a 500.
  3. Aggregate pending wins correctly when rows are present (sum, count,
     overdue count > 5 minutes past window_end_utc).

Auth + session are stubbed via FastAPI dependency_overrides — the same
pattern used by tests/test_config_api.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from api.positions import router
from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session


class _FakeUndefinedTable(Exception):
    """Mimics asyncpg.UndefinedTableError: has a `sqlstate` attr = '42P01'."""

    sqlstate = "42P01"


def _missing_table_error(table_name: str) -> ProgrammingError:
    """Build a SQLAlchemy ProgrammingError whose `.orig` reports pgcode 42P01,
    the real shape raised by Postgres when a SELECT hits a missing relation.
    """
    orig = _FakeUndefinedTable(f'relation "{table_name}" does not exist')
    return ProgrammingError("SELECT ...", params={}, orig=orig)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_session(*, wallet=None, pending=None, redeemer=None, raise_on=None):
    """Build a MagicMock that pretends to be an AsyncSession.

    The endpoint runs three sequential SELECT statements:
      1. wallet (poly_wallet_balance)
      2. pending wins (poly_pending_wins)
      3. redeemer state (redeemer_state)

    `raise_on` is a set of stage names ("wallet", "pending", "redeemer")
    whose query should raise (simulates a missing table — Postgres would
    raise an UndefinedTable / ProgrammingError).
    """
    raise_on = raise_on or set()
    call_order = ["wallet", "pending", "redeemer"]
    # Translate the per-stage rows into fixtures our fake_execute returns
    fixtures = {
        "wallet": [wallet] if wallet is not None else [],
        "pending": list(pending or []),
        "redeemer": [redeemer] if redeemer is not None else [],
    }
    counter = {"i": 0}

    session = MagicMock()

    async def fake_execute(stmt, params=None):
        idx = counter["i"]
        counter["i"] += 1
        stage = call_order[idx] if idx < len(call_order) else "extra"

        if stage in raise_on:
            # Match the real shape: SQLAlchemy ProgrammingError with
            # pgcode 42P01 (undefined_table). The endpoint discriminates
            # between "missing table" (degraded 200 + _meta) and other
            # DB errors (500).
            raise _missing_table_error(stage)

        result = MagicMock()
        rows = fixtures.get(stage, [])
        mappings = MagicMock()
        mappings.all = MagicMock(return_value=rows)
        mappings.first = MagicMock(return_value=rows[0] if rows else None)
        result.mappings = MagicMock(return_value=mappings)
        return result

    session.execute = AsyncMock(side_effect=fake_execute)
    return session


def _build_app(session):
    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def fake_get_session():
        yield session

    async def fake_get_current_user():
        return TokenData(user_id=1, username="testuser", token_type="access")

    app.dependency_overrides[get_session] = fake_get_session
    app.dependency_overrides[get_current_user] = fake_get_current_user
    return app


EXPECTED_KEYS = (
    "wallet_usdc",
    "pending_wins",
    "pending_count",
    "pending_total_usd",
    "overdue_count",
    "effective_balance",
    "open_orders",
    "open_orders_count",
    "cooldown",
    "daily_quota_limit",
    "quota_used_today",
    "quota_remaining",
)


# ─── Tests ────────────────────────────────────────────────────────────────────


def test_snapshot_returns_expected_shape_with_data():
    """All 12 spec keys plus now_utc must be present and typed correctly."""
    now = datetime.now(timezone.utc)
    # Real canonical wallet table column name (balance_usdc, not usdc_balance).
    wallet = {"balance_usdc": 123.45}
    pending = [
        {
            "condition_id": "0xaaa",
            "value": 5.0,
            "window_end_utc": now - timedelta(seconds=600),  # overdue
            "overdue_seconds": 600,
        },
        {
            "condition_id": "0xbbb",
            "value": 2.5,
            "window_end_utc": now - timedelta(seconds=60),   # not overdue
            "overdue_seconds": 60,
        },
    ]
    redeemer = {
        "cooldown_active": True,
        "cooldown_remaining_seconds": 1800,
        "cooldown_resets_at": now + timedelta(seconds=1800),
        "cooldown_reason": "rate_limit_429",
        "daily_quota_limit": 100,
        "quota_used_today": 7,
    }
    session = _make_session(wallet=wallet, pending=pending, redeemer=redeemer)
    client = TestClient(_build_app(session))

    res = client.get("/api/positions/snapshot")
    assert res.status_code == 200, res.text
    body = res.json()

    for k in EXPECTED_KEYS:
        assert k in body, f"missing key: {k}"
    assert "now_utc" in body  # bonus key

    assert body["wallet_usdc"] == 123.45
    assert body["pending_count"] == 2
    assert body["pending_total_usd"] == 7.5
    assert body["overdue_count"] == 1  # only the 600s-overdue one
    assert body["effective_balance"] == round(123.45 + 7.5, 2)
    assert body["open_orders"] == []
    assert body["open_orders_count"] == 0
    assert body["cooldown"]["active"] is True
    assert body["cooldown"]["remaining_seconds"] == 1800
    assert body["cooldown"]["reason"] == "rate_limit_429"
    assert body["daily_quota_limit"] == 100
    assert body["quota_used_today"] == 7
    assert body["quota_remaining"] == 93


def test_snapshot_handles_all_tables_missing():
    """Endpoint must NOT 500 when poly_wallet_balance / poly_pending_wins /
    redeemer_state don't exist yet. Per-table queries discriminate between
    "missing table" (pgcode 42P01 → degraded 200 with _meta sentinel) and
    other DB errors (propagate to 500). A degraded payload surfaces the
    absent tables under `_meta.missing_tables` so the frontend can render
    a red "migration missing" banner instead of an all-zero "healthy" UI.
    """
    session = _make_session(raise_on={"wallet", "pending", "redeemer"})
    client = TestClient(_build_app(session))

    res = client.get("/api/positions/snapshot")
    assert res.status_code == 200, res.text
    body = res.json()

    for k in EXPECTED_KEYS:
        assert k in body, f"missing key: {k}"

    assert body["wallet_usdc"] == 0.0
    assert body["pending_wins"] == []
    assert body["pending_count"] == 0
    assert body["pending_total_usd"] == 0
    assert body["overdue_count"] == 0
    assert body["effective_balance"] == 0.0
    assert body["open_orders"] == []
    assert body["open_orders_count"] == 0
    assert body["cooldown"]["active"] is False
    assert body["cooldown"]["remaining_seconds"] == 0
    assert body["cooldown"]["resets_at"] is None
    assert body["cooldown"]["reason"] == ""
    # Default quota: limit=100, used=0, remaining=100
    assert body["daily_quota_limit"] == 100
    assert body["quota_used_today"] == 0
    assert body["quota_remaining"] == 100

    # _meta must flag the degraded state so the frontend doesn't render
    # all-zeros as if the system were healthy (2026-04-16 TimesFM lesson).
    assert body["_meta"]["data_stale"] is True
    assert set(body["_meta"]["missing_tables"]) == {
        "wallet_snapshots",
        "poly_pending_wins",
        "redeemer_state",
    }


def test_snapshot_handles_empty_tables():
    """Tables exist but contain no rows — should still return all keys and
    NOT flag data_stale (the tables are just genuinely empty, not missing).
    """
    session = _make_session(wallet=None, pending=[], redeemer=None)
    client = TestClient(_build_app(session))

    res = client.get("/api/positions/snapshot")
    assert res.status_code == 200, res.text
    body = res.json()

    for k in EXPECTED_KEYS:
        assert k in body
    assert body["wallet_usdc"] == 0.0
    assert body["pending_wins"] == []
    assert body["pending_count"] == 0
    assert body["effective_balance"] == 0.0
    assert body["cooldown"]["active"] is False

    # Empty != missing — tables exist, no degraded flag.
    assert body["_meta"]["data_stale"] is False
    assert body["_meta"]["missing_tables"] == []


def test_snapshot_propagates_non_schema_db_errors():
    """If a query fails for a reason OTHER than missing-table (auth,
    network, column drift), we MUST 500 rather than quietly returning
    zeros. Narrower exception handling prevents the TimesFM-shaped
    "silent fallback indistinguishable from healthy" trap.
    """
    # Use a custom session whose execute raises a generic RuntimeError.
    session = MagicMock()

    async def fake_execute(*_a, **_k):
        raise RuntimeError("connection refused")

    session.execute = AsyncMock(side_effect=fake_execute)
    client = TestClient(_build_app(session), raise_server_exceptions=False)

    res = client.get("/api/positions/snapshot")
    assert res.status_code == 500, res.text


def test_snapshot_partial_table_missing():
    """Only `poly_pending_wins` missing — wallet + redeemer_state present.
    Frontend still gets real wallet + cooldown values; only pending goes
    to zero. _meta lists exactly one missing table.
    """
    now = datetime.now(timezone.utc)
    session = _make_session(
        wallet={"balance_usdc": 50.0},
        pending=[],
        redeemer={
            "cooldown_active": False,
            "cooldown_remaining_seconds": 0,
            "cooldown_resets_at": None,
            "cooldown_reason": "",
            "daily_quota_limit": 100,
            "quota_used_today": 3,
        },
        raise_on={"pending"},
    )
    client = TestClient(_build_app(session))

    res = client.get("/api/positions/snapshot")
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["wallet_usdc"] == 50.0
    assert body["pending_wins"] == []
    assert body["pending_count"] == 0
    assert body["quota_used_today"] == 3
    assert body["_meta"]["data_stale"] is True
    assert body["_meta"]["missing_tables"] == ["poly_pending_wins"]
