"""
/desk Phase 2 — CLOB proxy + condition resolver tests.

Covers:
  - Feature-flag gate: HUB_ALLOW_CLOB_FETCH=false → 503 feature_flag_off.
  - Cache: second call inside TTL hits cache (mock called once).
  - Upstream timeout → 503 upstream_timeout.
  - Upstream 5xx → 503 upstream_5xx.
  - Level parsing: string + list pair shapes both accepted; malformed rejected.
  - Metrics: spread, imbalance, implied_p_up computed correctly.
  - Condition resolver: Gamma events JSON → condition_id + token ids extracted.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import clob as clob_mod
from api.clob import router as clob_router
from auth.jwt import TokenData
from auth.middleware import get_current_user


# ── Helpers ─────────────────────────────────────────────────────────────────

def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(clob_router, prefix="/api")

    async def fake_user():
        return TokenData(user_id=1, username="billy", token_type="access")

    app.dependency_overrides[get_current_user] = fake_user
    return app


@pytest.fixture(autouse=True)
def _clear_cache():
    """Cache is module-global — wipe before each test."""
    clob_mod._book_cache.clear()
    clob_mod._condition_cache.clear()
    yield
    clob_mod._book_cache.clear()
    clob_mod._condition_cache.clear()


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("HUB_ALLOW_CLOB_FETCH", "true")
    yield


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.delenv("HUB_ALLOW_CLOB_FETCH", raising=False)
    yield


def _mock_clob_response(bids, asks):
    """Build a fake httpx Response for CLOB /book."""
    resp = AsyncMock(spec=httpx.Response)
    resp.raise_for_status = lambda: None
    resp.json = lambda: {"bids": bids, "asks": asks}
    return resp


# ── Parsing unit ────────────────────────────────────────────────────────────

def test_parse_level_dict_shape():
    assert clob_mod._parse_level({"price": "0.54", "size": "60"}) == {
        "price": 0.54, "size": 60.0,
    }


def test_parse_level_list_shape():
    assert clob_mod._parse_level(["0.54", "60"]) == {"price": 0.54, "size": 60.0}


def test_parse_level_rejects_out_of_range():
    assert clob_mod._parse_level({"price": "1.5", "size": "60"}) is None
    assert clob_mod._parse_level({"price": "0.5", "size": "-1"}) is None
    assert clob_mod._parse_level({"price": "garbage", "size": "10"}) is None
    assert clob_mod._parse_level(None) is None
    assert clob_mod._parse_level([]) is None


def test_reduce_side_sorts_and_keeps_top_5():
    raw = [
        {"price": "0.50", "size": "10"},
        {"price": "0.54", "size": "30"},
        {"price": "0.51", "size": "20"},
        {"price": "0.53", "size": "40"},
        {"price": "0.52", "size": "5"},
        {"price": "0.49", "size": "100"},
        {"price": "0.48", "size": "5"},  # should be dropped
        "garbage",                          # rejected
    ]
    bids = clob_mod._reduce_side(raw, descending=True, keep=5)
    prices = [b["price"] for b in bids]
    assert prices == [0.54, 0.53, 0.52, 0.51, 0.50]


def test_compute_metrics_happy():
    yes = {
        "bids": [{"price": 0.54, "size": 60}],
        "asks": [{"price": 0.56, "size": 40}],
    }
    no = {"bids": [], "asks": []}
    m = clob_mod._compute_metrics(yes, no)
    assert m["spread"] == pytest.approx(0.02, rel=1e-6)
    assert m["implied_p_up"] == pytest.approx(0.55, rel=1e-6)
    # imbalance = (60 - 40) / (60 + 40) = 0.2
    assert m["imbalance"] == pytest.approx(0.2, rel=1e-6)


def test_compute_metrics_degenerate():
    yes = {"bids": [], "asks": []}
    no = {"bids": [], "asks": []}
    m = clob_mod._compute_metrics(yes, no)
    assert m["spread"] is None
    assert m["imbalance"] is None
    assert m["implied_p_up"] is None


# ── /api/clob/book ──────────────────────────────────────────────────────────

def test_book_flag_off_returns_503(flag_off):
    app = _build_app()
    client = TestClient(app)
    resp = client.get(
        "/api/clob/book",
        params={"condition_id": "0xabc", "token_yes": "tok_yes", "token_no": "tok_no_"},
    )
    assert resp.status_code == 503
    body = resp.json()
    assert body["detail"]["reason"] == "feature_flag_off"
    assert body["detail"]["error"] == "clob_unavailable"


def test_book_happy_path_with_cache(flag_on):
    app = _build_app()
    client = TestClient(app)

    call_count = {"n": 0}

    async def fake_fetch(client, token_id):
        call_count["n"] += 1
        if token_id == "yes-t":
            return {
                "bids": [{"price": "0.54", "size": "60"}, {"price": "0.53", "size": "80"}],
                "asks": [{"price": "0.56", "size": "40"}],
            }
        return {
            "bids": [{"price": "0.43", "size": "50"}],
            "asks": [{"price": "0.45", "size": "70"}],
        }

    with patch.object(clob_mod, "_fetch_clob_book", side_effect=fake_fetch):
        resp = client.get(
            "/api/clob/book",
            params={"condition_id": "0xabc", "token_yes": "yes-t", "token_no": "no-t"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["condition_id"] == "0xabc"
        assert body["yes"]["bids"][0] == {"price": 0.54, "size": 60.0}
        assert body["yes"]["asks"][0] == {"price": 0.56, "size": 40.0}
        assert body["implied_p_up"] == pytest.approx(0.55, rel=1e-6)
        assert body["spread"] == pytest.approx(0.02, rel=1e-6)
        assert "cached_at" in body

        # Second call within TTL — cache should serve it, upstream NOT called again.
        call_count_first = call_count["n"]
        resp2 = client.get(
            "/api/clob/book",
            params={"condition_id": "0xabc", "token_yes": "yes-t", "token_no": "no-t"},
        )
        assert resp2.status_code == 200
        assert call_count["n"] == call_count_first, "cache should have served the second call"


def test_book_upstream_timeout_returns_503(flag_on):
    app = _build_app()
    client = TestClient(app)

    async def fake_fetch(client, token_id):
        raise httpx.TimeoutException("simulated")

    with patch.object(clob_mod, "_fetch_clob_book", side_effect=fake_fetch):
        resp = client.get(
            "/api/clob/book",
            params={"condition_id": "0xabc", "token_yes": "yes-t", "token_no": "no-t"},
        )
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "upstream_timeout"


def test_book_upstream_500_returns_503(flag_on):
    app = _build_app()
    client = TestClient(app)

    async def fake_fetch(client, token_id):
        req = httpx.Request("GET", "https://clob.polymarket.com/book")
        resp500 = httpx.Response(502, request=req)
        raise httpx.HTTPStatusError("bad gateway", request=req, response=resp500)

    with patch.object(clob_mod, "_fetch_clob_book", side_effect=fake_fetch):
        resp = client.get(
            "/api/clob/book",
            params={"condition_id": "0xabc", "token_yes": "yes-t", "token_no": "no-t"},
        )
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "upstream_502"


# ── /api/windows/condition ──────────────────────────────────────────────────

def test_condition_flag_off(flag_off):
    app = _build_app()
    client = TestClient(app)
    resp = client.get("/api/windows/condition", params={"window_epoch": 1777060500})
    assert resp.status_code == 503


def test_condition_happy(flag_on):
    app = _build_app()
    client = TestClient(app)

    # Simulate Gamma event JSON (based on the v58_monitor fetcher's shape).
    fake_gamma = [{
        "markets": [{
            "conditionId": "0xdeadbeef",
            "clobTokenIds": '["tok_yes_123","tok_no_456"]',
            "outcomes": '["Up","Down"]',
        }],
    }]

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return fake_gamma

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, params=None, timeout=None):
            return FakeResp()

    with patch.object(clob_mod.httpx, "AsyncClient", FakeClient):
        resp = client.get(
            "/api/windows/condition",
            params={"window_epoch": 1777060500, "asset": "BTC"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["condition_id"] == "0xdeadbeef"
    assert body["yes_token_id"] == "tok_yes_123"
    assert body["no_token_id"] == "tok_no_456"
    assert body["slug"] == "btc-updown-5m-1777060500"


def test_condition_market_not_found(flag_on):
    app = _build_app()
    client = TestClient(app)

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return []  # no events

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, params=None, timeout=None):
            return FakeResp()

    with patch.object(clob_mod.httpx, "AsyncClient", FakeClient):
        resp = client.get(
            "/api/windows/condition",
            params={"window_epoch": 1777060500},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "market_not_found"


def test_condition_timeout_returns_503(flag_on):
    app = _build_app()
    client = TestClient(app)

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, params=None, timeout=None):
            raise httpx.TimeoutException("gamma slow")

    with patch.object(clob_mod.httpx, "AsyncClient", FakeClient):
        resp = client.get(
            "/api/windows/condition",
            params={"window_epoch": 1777060500},
        )
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "gamma_timeout"
