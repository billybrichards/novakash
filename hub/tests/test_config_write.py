"""
CFG-04 — tests for config write endpoints in hub/api/config_v2.py.

Covers:
  1. POST /api/v58/config/upsert — valid value, type mismatch, unknown key
  2. POST /api/v58/config/rollback — valid rollback, missing history entry
  3. POST /api/v58/config/reset — reset to default
  4. History append verification
  5. Type validation helper (_validate_value_for_type)
  6. The legacy POST stub now returns 400 instead of 501

Uses the same mock-session + dependency-override pattern as test_config_api.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.config_v2 import _validate_value_for_type, router
from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session


# ─── Pure-function tests: _validate_value_for_type ───────────────────────────


def test_validate_bool_accepts_valid():
    assert _validate_value_for_type("true", "bool") == "true"
    assert _validate_value_for_type("False", "bool") == "false"
    assert _validate_value_for_type("1", "bool") == "1"
    assert _validate_value_for_type("yes", "bool") == "yes"


def test_validate_bool_rejects_invalid():
    with pytest.raises(ValueError, match="expected bool"):
        _validate_value_for_type("banana", "bool")


def test_validate_int_accepts_valid():
    assert _validate_value_for_type("42", "int") == "42"
    assert _validate_value_for_type("-7", "int") == "-7"


def test_validate_int_rejects_invalid():
    with pytest.raises(ValueError):
        _validate_value_for_type("3.14", "int")


def test_validate_float_accepts_valid():
    assert _validate_value_for_type("3.14", "float") == "3.14"
    assert _validate_value_for_type("500", "float") == "500"


def test_validate_float_rejects_invalid():
    with pytest.raises(ValueError):
        _validate_value_for_type("not-a-number", "float")


def test_validate_enum_accepts_valid():
    assert _validate_value_for_type("oak", "enum", ["oak", "maple", "pine"]) == "oak"


def test_validate_enum_rejects_invalid():
    with pytest.raises(ValueError, match="expected one of"):
        _validate_value_for_type("birch", "enum", ["oak", "maple", "pine"])


def test_validate_string_passthrough():
    assert _validate_value_for_type("anything", "string") == "anything"


# ─── Mock session factory ────────────────────────────────────────────────────


def _make_write_mock_session(
    key_row=None,
    current_value_row=None,
    history_row=None,
    history_lookup_row=None,
):
    """Build a mock AsyncSession that handles the multi-query flow
    of the write endpoints.

    The write endpoints issue multiple queries sequentially:
      1. _resolve_config_key → SELECT from config_keys
      2. _get_current_value → SELECT from config_values
      3. (rollback only) SELECT from config_history
      4. _write_config_value → UPDATE/DELETE + INSERT + RETURNING

    We track call count to dispatch the right mock response.
    """
    session = MagicMock()
    call_count = {"n": 0}

    # Default history return row (for RETURNING clause)
    now = datetime(2026, 4, 12, 10, 0, 0, tzinfo=timezone.utc)
    default_history = {
        "id": 1,
        "previous_value": None,
        "new_value": "true",
        "changed_by": "testuser",
        "changed_at": now,
        "comment": "test",
    }

    async def fake_execute(stmt, params=None):
        call_count["n"] += 1
        n = call_count["n"]
        result = MagicMock()
        mappings = MagicMock()

        stmt_text = str(stmt.text) if hasattr(stmt, "text") else str(stmt)

        # Dispatch based on SQL content rather than call order for clarity
        if "FROM config_keys" in stmt_text:
            row = key_row
            mappings.first = MagicMock(return_value=row)
            mappings.all = MagicMock(return_value=[row] if row else [])
        elif "FROM config_history" in stmt_text and history_lookup_row is not None:
            mappings.first = MagicMock(return_value=history_lookup_row)
            mappings.all = MagicMock(return_value=[history_lookup_row] if history_lookup_row else [])
        elif "FROM config_values" in stmt_text:
            mappings.first = MagicMock(return_value=current_value_row)
            mappings.all = MagicMock(return_value=[current_value_row] if current_value_row else [])
        elif "RETURNING" in stmt_text:
            h = history_row or default_history
            mappings.first = MagicMock(return_value=h)
            mappings.all = MagicMock(return_value=[h])
        else:
            # UPDATE / DELETE / INSERT without RETURNING
            mappings.first = MagicMock(return_value=None)
            mappings.all = MagicMock(return_value=[])

        result.mappings = MagicMock(return_value=mappings)
        return result

    session.execute = AsyncMock(side_effect=fake_execute)
    session.commit = AsyncMock()
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


# ─── POST /upsert tests ─────────────────────────────────────────────────────


def test_upsert_valid_bool():
    now = datetime(2026, 4, 12, 10, 0, 0, tzinfo=timezone.utc)
    key_row = {
        "id": 7,
        "service": "engine",
        "key": "V10_6_ENABLED",
        "type": "bool",
        "default_value": "false",
        "description": "v10.6 master",
        "category": "gates",
        "restart_required": True,
        "editable_via_ui": True,
        "enum_values": None,
    }
    history_row = {
        "id": 1,
        "previous_value": None,
        "new_value": "true",
        "changed_by": "testuser",
        "changed_at": now,
        "comment": "Activating V10.6",
    }
    session = _make_write_mock_session(
        key_row=key_row,
        current_value_row=None,  # no prior value
        history_row=history_row,
    )
    client = TestClient(_build_app(session))

    res = client.post("/api/v58/config/upsert", json={
        "service": "engine",
        "key": "V10_6_ENABLED",
        "value": "true",
        "reason": "Activating V10.6",
    })
    assert res.status_code == 200
    body = res.json()
    assert body["service"] == "engine"
    assert body["key"] == "V10_6_ENABLED"
    assert body["current_value"] is True
    assert body["current_value_raw"] == "true"
    assert body["previous_value"] is None
    assert body["type"] == "bool"
    assert body["history_entry"]["id"] == 1
    assert body["history_entry"]["new_value"] == "true"


def test_upsert_valid_float():
    now = datetime(2026, 4, 12, 10, 0, 0, tzinfo=timezone.utc)
    key_row = {
        "id": 1,
        "service": "engine",
        "key": "BET_FRACTION",
        "type": "float",
        "default_value": "0.025",
        "description": "Kelly fraction",
        "category": "sizing",
        "restart_required": False,
        "editable_via_ui": True,
        "enum_values": None,
    }
    history_row = {
        "id": 2,
        "previous_value": "0.025",
        "new_value": "0.05",
        "changed_by": "testuser",
        "changed_at": now,
        "comment": "doubling kelly",
    }
    session = _make_write_mock_session(
        key_row=key_row,
        current_value_row={"value": "0.025"},
        history_row=history_row,
    )
    client = TestClient(_build_app(session))

    res = client.post("/api/v58/config/upsert", json={
        "service": "engine",
        "key": "BET_FRACTION",
        "value": "0.05",
        "reason": "doubling kelly",
    })
    assert res.status_code == 200
    body = res.json()
    assert body["current_value"] == pytest.approx(0.05)
    assert body["previous_value"] == pytest.approx(0.025)


def test_upsert_type_mismatch_returns_422():
    key_row = {
        "id": 1,
        "service": "engine",
        "key": "BET_FRACTION",
        "type": "float",
        "default_value": "0.025",
        "description": "Kelly fraction",
        "category": "sizing",
        "restart_required": False,
        "editable_via_ui": True,
        "enum_values": None,
    }
    session = _make_write_mock_session(key_row=key_row)
    client = TestClient(_build_app(session))

    res = client.post("/api/v58/config/upsert", json={
        "service": "engine",
        "key": "BET_FRACTION",
        "value": "not-a-number",
        "reason": "bad value",
    })
    assert res.status_code == 422
    assert "type validation failed" in res.json()["detail"]


def test_upsert_unknown_key_returns_404():
    session = _make_write_mock_session(key_row=None)
    client = TestClient(_build_app(session))

    res = client.post("/api/v58/config/upsert", json={
        "service": "engine",
        "key": "DOES_NOT_EXIST",
        "value": "42",
        "reason": "test",
    })
    assert res.status_code == 404
    assert "unknown config key" in res.json()["detail"]


# ─── POST /rollback tests ───────────────────────────────────────────────────


def test_rollback_valid():
    now = datetime(2026, 4, 12, 10, 0, 0, tzinfo=timezone.utc)
    key_row = {
        "id": 7,
        "service": "engine",
        "key": "V10_6_ENABLED",
        "type": "bool",
        "default_value": "false",
        "description": "v10.6 master",
        "category": "gates",
        "restart_required": True,
        "editable_via_ui": True,
        "enum_values": None,
    }
    history_lookup = {
        "id": 42,
        "previous_value": "false",
        "new_value": "true",
        "config_key_id": 7,
    }
    rollback_history = {
        "id": 43,
        "previous_value": "true",
        "new_value": "false",
        "changed_by": "testuser",
        "changed_at": now,
        "comment": "rollback to history_id=42",
    }
    session = _make_write_mock_session(
        key_row=key_row,
        current_value_row={"value": "true"},
        history_lookup_row=history_lookup,
        history_row=rollback_history,
    )
    client = TestClient(_build_app(session))

    res = client.post("/api/v58/config/rollback", json={
        "service": "engine",
        "key": "V10_6_ENABLED",
        "history_id": 42,
    })
    assert res.status_code == 200
    body = res.json()
    assert body["rolled_back_to_value"] is False
    assert body["rolled_back_to_value_raw"] == "false"
    assert body["history_entry"]["comment"] == "rollback to history_id=42"


def test_rollback_missing_history_returns_404():
    key_row = {
        "id": 7,
        "service": "engine",
        "key": "V10_6_ENABLED",
        "type": "bool",
        "default_value": "false",
        "description": "v10.6 master",
        "category": "gates",
        "restart_required": True,
        "editable_via_ui": True,
        "enum_values": None,
    }
    session = _make_write_mock_session(
        key_row=key_row,
        current_value_row={"value": "true"},
        history_lookup_row=None,
    )
    client = TestClient(_build_app(session))

    res = client.post("/api/v58/config/rollback", json={
        "service": "engine",
        "key": "V10_6_ENABLED",
        "history_id": 9999,
    })
    assert res.status_code == 404
    assert "not found" in res.json()["detail"]


def test_rollback_wrong_key_returns_422():
    key_row = {
        "id": 7,
        "service": "engine",
        "key": "V10_6_ENABLED",
        "type": "bool",
        "default_value": "false",
        "description": "v10.6 master",
        "category": "gates",
        "restart_required": True,
        "editable_via_ui": True,
        "enum_values": None,
    }
    history_lookup = {
        "id": 42,
        "previous_value": "0.025",
        "new_value": "0.05",
        "config_key_id": 99,  # different key
    }
    session = _make_write_mock_session(
        key_row=key_row,
        current_value_row={"value": "true"},
        history_lookup_row=history_lookup,
    )
    client = TestClient(_build_app(session))

    res = client.post("/api/v58/config/rollback", json={
        "service": "engine",
        "key": "V10_6_ENABLED",
        "history_id": 42,
    })
    assert res.status_code == 422
    assert "belongs to a different key" in res.json()["detail"]


# ─── POST /reset tests ──────────────────────────────────────────────────────


def test_reset_to_default():
    now = datetime(2026, 4, 12, 10, 0, 0, tzinfo=timezone.utc)
    key_row = {
        "id": 1,
        "service": "engine",
        "key": "BET_FRACTION",
        "type": "float",
        "default_value": "0.025",
        "description": "Kelly fraction",
        "category": "sizing",
        "restart_required": False,
        "editable_via_ui": True,
        "enum_values": None,
    }
    reset_history = {
        "id": 5,
        "previous_value": "0.05",
        "new_value": None,
        "changed_by": "testuser",
        "changed_at": now,
        "comment": "reset to default",
    }
    session = _make_write_mock_session(
        key_row=key_row,
        current_value_row={"value": "0.05"},
        history_row=reset_history,
    )
    client = TestClient(_build_app(session))

    res = client.post("/api/v58/config/reset", json={
        "service": "engine",
        "key": "BET_FRACTION",
    })
    assert res.status_code == 200
    body = res.json()
    assert body["is_default"] is True
    assert body["current_value"] == pytest.approx(0.025)
    assert body["current_value_raw"] == "0.025"
    assert body["history_entry"]["comment"] == "reset to default"


def test_reset_unknown_key_returns_404():
    session = _make_write_mock_session(key_row=None)
    client = TestClient(_build_app(session))

    res = client.post("/api/v58/config/reset", json={
        "service": "engine",
        "key": "DOES_NOT_EXIST",
    })
    assert res.status_code == 404
    assert "unknown config key" in res.json()["detail"]


# ─── POST /v58/config stub ──────────────────────────────────────────────────


def test_post_config_stub_returns_400():
    """The generic POST now returns 400 pointing to specific endpoints."""
    session = _make_write_mock_session()
    client = TestClient(_build_app(session))

    res = client.post("/api/v58/config", json={"service": "engine"})
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "upsert" in detail
    assert "rollback" in detail
    assert "reset" in detail


# ─── History append verification ─────────────────────────────────────────────


def test_upsert_calls_commit():
    """Verify the session.commit() is called (transactional integrity)."""
    now = datetime(2026, 4, 12, 10, 0, 0, tzinfo=timezone.utc)
    key_row = {
        "id": 7,
        "service": "engine",
        "key": "V10_6_ENABLED",
        "type": "bool",
        "default_value": "false",
        "description": "v10.6 master",
        "category": "gates",
        "restart_required": True,
        "editable_via_ui": True,
        "enum_values": None,
    }
    history_row = {
        "id": 1,
        "previous_value": None,
        "new_value": "true",
        "changed_by": "testuser",
        "changed_at": now,
        "comment": "test",
    }
    session = _make_write_mock_session(
        key_row=key_row,
        history_row=history_row,
    )
    client = TestClient(_build_app(session))

    res = client.post("/api/v58/config/upsert", json={
        "service": "engine",
        "key": "V10_6_ENABLED",
        "value": "true",
        "reason": "test",
    })
    assert res.status_code == 200
    session.commit.assert_called_once()
