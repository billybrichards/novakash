"""
CFG-03 — tests for hub/api/config_v2.py.

Verifies:
  1. Type coercion at the API layer (TEXT in DB → real bool/int/float for the wire).
  2. _row_to_key_dict shapes the response correctly.
  3. The four GET endpoints + the POST stub return the expected status codes
     and shapes when called against a mock SQLAlchemy session.

The test bypasses the auth middleware by overriding the FastAPI
dependency, the same pattern used by FastAPI's own docs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.config_v2 import _coerce_value, _row_to_key_dict, router
from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session


# ─── Pure-function tests ──────────────────────────────────────────────────────


def test_coerce_value_bool():
    assert _coerce_value("true", "bool") is True
    assert _coerce_value("True", "bool") is True
    assert _coerce_value("TRUE", "bool") is True
    assert _coerce_value("1", "bool") is True
    assert _coerce_value("yes", "bool") is True
    assert _coerce_value("on", "bool") is True
    assert _coerce_value("false", "bool") is False
    assert _coerce_value("False", "bool") is False
    assert _coerce_value("0", "bool") is False
    assert _coerce_value("no", "bool") is False


def test_coerce_value_int():
    assert _coerce_value("42", "int") == 42
    assert _coerce_value("-7", "int") == -7
    assert _coerce_value("0", "int") == 0


def test_coerce_value_float():
    assert _coerce_value("3.14", "float") == pytest.approx(3.14)
    assert _coerce_value("0.025", "float") == pytest.approx(0.025)
    assert _coerce_value("500", "float") == pytest.approx(500.0)


def test_coerce_value_string_passthrough():
    assert _coerce_value("opinion", "string") == "opinion"
    assert _coerce_value("BTC,ETH,SOL", "csv") == "BTC,ETH,SOL"
    assert _coerce_value("oak", "enum") == "oak"


def test_coerce_value_handles_none_and_empty():
    assert _coerce_value(None, "float") is None
    assert _coerce_value("", "int") is None


def test_coerce_value_returns_raw_on_parse_failure():
    """A bad numeric string should return the raw string, not crash."""
    assert _coerce_value("not-a-number", "int") == "not-a-number"
    assert _coerce_value("not-a-number", "float") == "not-a-number"


def test_row_to_key_dict_full_shape():
    now = datetime(2026, 4, 11, 14, 23, 0, tzinfo=timezone.utc)
    raw_row = {
        "id": 7,
        "service": "engine",
        "key": "V10_6_ENABLED",
        "type": "bool",
        "category": "gates",
        "description": "v10.6 master flag",
        "default_value": "false",
        "restart_required": True,
        "editable_via_ui": True,
        "enum_values": None,
        "min_value": None,
        "max_value": None,
        "current_value_raw": "true",
        "set_by": "billybrichards",
        "set_at": now,
    }
    out = _row_to_key_dict(raw_row)
    assert out["service"] == "engine"
    assert out["key"] == "V10_6_ENABLED"
    assert out["type"] == "bool"
    assert out["category"] == "gates"
    assert out["description"] == "v10.6 master flag"
    assert out["default_value"] is False
    assert out["default_value_raw"] == "false"
    assert out["current_value"] is True
    assert out["current_value_raw"] == "true"
    assert out["is_at_default"] is False
    assert out["restart_required"] is True
    assert out["editable_via_ui"] is True
    assert out["set_by"] == "billybrichards"
    assert out["set_at"] == now.isoformat()


def test_row_to_key_dict_at_default_when_no_value():
    raw_row = {
        "id": 1,
        "service": "engine",
        "key": "BET_FRACTION",
        "type": "float",
        "category": "sizing",
        "description": "Kelly fraction",
        "default_value": "0.025",
        "restart_required": False,
        "editable_via_ui": True,
        "current_value_raw": None,  # never set
        "set_by": None,
        "set_at": None,
    }
    out = _row_to_key_dict(raw_row)
    assert out["current_value"] is None
    assert out["is_at_default"] is True
    assert out["set_at"] is None


# ─── HTTP endpoint tests via mock session ─────────────────────────────────────


def _make_mock_session(rows_for_query=None):
    """Build a MagicMock that pretends to be an AsyncSession.

    rows_for_query: dict mapping a discriminator (the second-positional arg
    to .execute() if provided, else 'default') → list of row dicts.
    """
    rows_for_query = rows_for_query or {}

    session = MagicMock()

    async def fake_execute(stmt, params=None):
        result = MagicMock()
        # Pick the most relevant row set based on params. Order matters:
        # for the /history endpoint the first call binds {service, key} and
        # we want to dispatch on the *key* name (more specific). The second
        # call binds {key_id, limit}, dispatch on key_id.
        key = "default"
        if params:
            if params.get("key_id") is not None:
                key = params["key_id"]
            elif params.get("key"):
                key = params["key"]
            elif params.get("service"):
                key = params["service"]
        rows = rows_for_query.get(key, rows_for_query.get("default", []))
        # mock the chained .mappings().all() / .first() calls
        mappings = MagicMock()
        mappings.all = MagicMock(return_value=rows)
        mappings.first = MagicMock(return_value=rows[0] if rows else None)
        result.mappings = MagicMock(return_value=mappings)
        return result

    session.execute = AsyncMock(side_effect=fake_execute)
    return session


def _build_app_with_mock_session(session):
    """Build a minimal FastAPI app that mounts the config_v2 router and
    overrides the get_session + get_current_user dependencies."""
    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def fake_get_session():
        yield session

    async def fake_get_current_user():
        return TokenData(user_id=1, username="testuser", token_type="access")

    app.dependency_overrides[get_session] = fake_get_session
    app.dependency_overrides[get_current_user] = fake_get_current_user
    return app


def test_get_services_returns_expected_shape():
    rows = [
        {
            "service": "engine",
            "key_count": 111,
            "restart_required_count": 30,
            "last_changed": None,
        },
        {
            "service": "margin_engine",
            "key_count": 51,
            "restart_required_count": 0,
            "last_changed": None,
        },
    ]
    session = _make_mock_session({"default": rows})
    client = TestClient(_build_app_with_mock_session(session))

    res = client.get("/api/v58/config/services")
    assert res.status_code == 200
    body = res.json()
    assert "services" in body
    assert len(body["services"]) == 2
    assert body["services"][0]["service"] == "engine"
    assert body["services"][0]["key_count"] == 111
    assert body["services"][0]["restart_required_count"] == 30
    assert body["services"][1]["service"] == "margin_engine"


def test_get_config_for_service_groups_by_category():
    rows = [
        {
            "id": 1,
            "service": "engine",
            "key": "BET_FRACTION",
            "type": "float",
            "category": "sizing",
            "description": "Kelly",
            "default_value": "0.025",
            "restart_required": False,
            "editable_via_ui": True,
            "enum_values": None,
            "min_value": None,
            "max_value": None,
            "current_value_raw": None,
            "set_by": None,
            "set_at": None,
        },
        {
            "id": 2,
            "service": "engine",
            "key": "MAX_POSITION_USD",
            "type": "float",
            "category": "sizing",
            "description": "Hard cap",
            "default_value": "500.0",
            "restart_required": False,
            "editable_via_ui": True,
            "enum_values": None,
            "min_value": None,
            "max_value": None,
            "current_value_raw": None,
            "set_by": None,
            "set_at": None,
        },
        {
            "id": 3,
            "service": "engine",
            "key": "V10_6_ENABLED",
            "type": "bool",
            "category": "gates",
            "description": "v10.6 master",
            "default_value": "false",
            "restart_required": True,
            "editable_via_ui": True,
            "enum_values": None,
            "min_value": None,
            "max_value": None,
            "current_value_raw": None,
            "set_by": None,
            "set_at": None,
        },
    ]
    session = _make_mock_session({"engine": rows})
    client = TestClient(_build_app_with_mock_session(session))

    res = client.get("/api/v58/config?service=engine")
    assert res.status_code == 200
    body = res.json()
    assert body["service"] == "engine"
    assert body["key_count"] == 3
    cats = {c["id"]: c for c in body["categories"]}
    assert "sizing" in cats
    assert "gates" in cats
    assert cats["sizing"]["key_count"] == 2
    assert cats["gates"]["key_count"] == 1
    # Spot-check that the bool defaulted to false was coerced
    gate_key = cats["gates"]["keys"][0]
    assert gate_key["key"] == "V10_6_ENABLED"
    assert gate_key["default_value"] is False
    assert gate_key["restart_required"] is True


def test_get_config_for_unknown_service_returns_empty():
    """An unknown service id returns an empty categories array, not 404,
    so the UI can render a blank tab without erroring."""
    session = _make_mock_session({"default": []})
    client = TestClient(_build_app_with_mock_session(session))

    res = client.get("/api/v58/config?service=ghost")
    assert res.status_code == 200
    body = res.json()
    assert body["service"] == "ghost"
    assert body["key_count"] == 0
    assert body["categories"] == []


def test_get_schema_for_service_returns_keys_no_values():
    rows = [
        {
            "id": 1,
            "service": "margin_engine",
            "key": "MARGIN_BET_FRACTION",
            "type": "float",
            "category": "sizing",
            "description": "per-trade fraction",
            "default_value": "0.02",
            "restart_required": False,
            "editable_via_ui": True,
            "enum_values": None,
            "min_value": None,
            "max_value": None,
            "current_value_raw": None,
            "set_by": None,
            "set_at": None,
        }
    ]
    session = _make_mock_session({"margin_engine": rows})
    client = TestClient(_build_app_with_mock_session(session))

    res = client.get("/api/v58/config/schema?service=margin_engine")
    assert res.status_code == 200
    body = res.json()
    assert body["service"] == "margin_engine"
    assert body["key_count"] == 1
    assert body["keys"][0]["key"] == "MARGIN_BET_FRACTION"
    assert body["keys"][0]["current_value"] is None


def test_get_history_for_known_key_returns_empty_list():
    """history endpoint for a key with no edits returns empty history
    array but still includes the key metadata in the response."""
    key_row = {
        "id": 7,
        "service": "engine",
        "key": "V10_6_ENABLED",
        "type": "bool",
        "default_value": "false",
        "description": "v10.6 master",
        "category": "gates",
        "restart_required": True,
    }
    session = _make_mock_session({
        "V10_6_ENABLED": [key_row],  # first .execute() lookup by key name
        7: [],                        # second .execute() lookup by key_id → no history rows
    })
    client = TestClient(_build_app_with_mock_session(session))

    res = client.get(
        "/api/v58/config/history?service=engine&key=V10_6_ENABLED&limit=50"
    )
    assert res.status_code == 200
    body = res.json()
    assert body["service"] == "engine"
    assert body["key"] == "V10_6_ENABLED"
    assert body["type"] == "bool"
    assert body["restart_required"] is True
    assert body["history"] == []


def test_get_history_for_unknown_key_returns_404():
    session = _make_mock_session({"default": []})
    client = TestClient(_build_app_with_mock_session(session))

    res = client.get(
        "/api/v58/config/history?service=engine&key=DOES_NOT_EXIST"
    )
    assert res.status_code == 404
    assert "unknown config key" in res.json()["detail"]


def test_post_config_returns_400_with_endpoint_pointers():
    """The generic POST now returns 400 pointing callers to the specific
    write endpoints (upsert, rollback, reset) added in CFG-04."""
    session = _make_mock_session()
    client = TestClient(_build_app_with_mock_session(session))

    res = client.post(
        "/api/v58/config",
        json={"service": "engine", "key": "BET_FRACTION", "value": 0.05},
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "upsert" in detail
    assert "rollback" in detail
    assert "reset" in detail
