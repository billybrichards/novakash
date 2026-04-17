"""
Task #222 — tests for hub/api/v58_monitor.py::strategy_decisions.

Covers the change that enriches the endpoint response with
``outcome / pnl_usd / resolved_at / sot_reconciliation_state`` by
reading from the ``strategy_decisions_resolved`` view (see the view
definition in ``hub/main.py`` startup and the canonical migration
``hub/db/migrations/versions/20260417_02_strategy_decisions_resolved_view.sql``).

Scope:

  1. Response shape — every row carries the four new keys, nullable when
     the underlying trade hasn't resolved yet.
  2. ``?resolved=true`` narrows server-side to rows where ``outcome IS
     NOT NULL`` — this is the call the FE WR matrix makes.
  3. ``?resolved=false`` narrows to unresolved (SKIPs + pre-resolve
     TRADEs).
  4. Existing ``?strategy_id=`` filter composes with ``?resolved=``.
  5. DB error path still returns the graceful ``{"decisions": [], "error":
     "..."}`` shape — we do not want a 500 tearing down the dashboard
     when the view is temporarily unavailable (e.g. mid-migration).

Auth + session are stubbed via FastAPI dependency_overrides, same
pattern as tests/test_positions_api.py + tests/test_config_api.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v58_monitor import router
from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session


# ─── Test helpers ────────────────────────────────────────────────────────────


def _decision_row(
    *,
    strategy_id: str = "v4_fusion",
    action: str = "TRADE",
    direction: Optional[str] = "UP",
    order_id: Optional[str] = "0xabc",
    outcome: Optional[str] = None,
    pnl_usd: Optional[float] = None,
    sot_state: Optional[str] = None,
    resolved_at: Optional[datetime] = None,
) -> dict:
    """Build a single row that mirrors the ``strategy_decisions_resolved``
    view projection. Keep this close to the real column list so tests
    break loudly if we rename things.
    """
    return {
        "strategy_id": strategy_id,
        "strategy_version": "v4.3.0",
        "mode": "LIVE",
        "asset": "BTC",
        "window_ts": 1_776_399_900,
        "timeframe": "5m",
        "eval_offset": 60,
        "action": action,
        "direction": direction,
        "confidence": "HIGH",
        "confidence_score": 0.82,
        "entry_cap": 0.73,
        "collateral_pct": 0.025,
        "entry_reason": None if action == "SKIP" else "polymarket_trade",
        "skip_reason": "regime_risk_off" if action == "SKIP" else None,
        "executed": action == "TRADE",
        "order_id": order_id,
        "fill_price": 0.73 if action == "TRADE" else None,
        "fill_size": 5.0 if action == "TRADE" else None,
        "metadata_json": '{"window_ts": 1776399900}',
        "evaluated_at": datetime(2026, 4, 17, 8, 40, 0, tzinfo=timezone.utc),
        # New — from the view's LEFT-JOINed trades row
        "outcome": outcome,
        "pnl_usd": pnl_usd,
        "resolved_at": resolved_at,
        "sot_reconciliation_state": sot_state,
    }


def _build_app(session: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def _override_session():
        yield session

    async def _override_user():
        return TokenData(user_id=1, username="test", token_type="access")

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_user] = _override_user
    return app


def _make_session(rows: List[dict], raise_exc: Optional[Exception] = None):
    """Build a MagicMock that returns ``rows`` from the single SELECT the
    endpoint issues. If ``raise_exc`` is set, ``session.execute`` raises
    it instead (simulates missing view / DB outage / etc.)."""
    session = MagicMock()

    last_stmt = {"stmt": None, "params": None}

    async def fake_execute(stmt, params=None):
        last_stmt["stmt"] = str(stmt)
        last_stmt["params"] = params or {}
        if raise_exc is not None:
            raise raise_exc
        result = MagicMock()
        result.mappings.return_value.all.return_value = rows
        return result

    session.execute = fake_execute
    session._last_stmt = last_stmt  # expose for assertions
    return session


# ─── Response-shape tests ────────────────────────────────────────────────────


def test_response_shape_carries_four_new_keys_nullable():
    """Every decision row must have outcome / pnl_usd / resolved_at /
    sot_reconciliation_state — null when the underlying order hasn't
    resolved yet (the common SKIP + pre-resolve case)."""
    rows = [
        _decision_row(action="SKIP", order_id=None),  # SKIP — no order at all
        _decision_row(action="TRADE", outcome=None),  # placed, not yet resolved
    ]
    client = TestClient(_build_app(_make_session(rows)))

    res = client.get("/api/v58/strategy-decisions")
    assert res.status_code == 200, res.text
    body = res.json()
    assert "decisions" in body
    assert len(body["decisions"]) == 2

    for dec in body["decisions"]:
        for key in ("outcome", "pnl_usd", "resolved_at", "sot_reconciliation_state"):
            assert key in dec, f"missing {key!r} in response"
        assert dec["outcome"] is None
        assert dec["pnl_usd"] is None
        assert dec["resolved_at"] is None
        assert dec["sot_reconciliation_state"] is None


def test_resolved_row_populates_all_enrichment_fields():
    """A row with a real resolved trade populates all four fields with
    correct types."""
    resolved_ts = datetime(2026, 4, 17, 8, 45, 0, tzinfo=timezone.utc)
    rows = [
        _decision_row(
            outcome="WIN",
            pnl_usd=1.47,
            sot_state="agrees",
            resolved_at=resolved_ts,
        )
    ]
    client = TestClient(_build_app(_make_session(rows)))

    body = client.get("/api/v58/strategy-decisions").json()
    dec = body["decisions"][0]
    assert dec["outcome"] == "WIN"
    assert dec["pnl_usd"] == pytest.approx(1.47)
    assert dec["sot_reconciliation_state"] == "agrees"
    # resolved_at is ISO-serialised
    assert dec["resolved_at"] == resolved_ts.isoformat()


def test_phantom_fill_is_distinguishable_from_real_loss():
    """A decision with outcome='LOSS' AND sot_reconciliation_state=
    'engine_optimistic' is an accounting-only loss (the phantom-fill
    class that drove overnight 2026-04-17). The FE must be able to
    distinguish this from a real wallet-impacting loss.
    """
    rows = [
        _decision_row(outcome="LOSS", pnl_usd=-4.50, sot_state="agrees"),               # real
        _decision_row(outcome="LOSS", pnl_usd=-3.75, sot_state="engine_optimistic"),    # phantom
    ]
    client = TestClient(_build_app(_make_session(rows)))

    decs = client.get("/api/v58/strategy-decisions").json()["decisions"]
    real, phantom = decs
    assert real["sot_reconciliation_state"] == "agrees"
    assert phantom["sot_reconciliation_state"] == "engine_optimistic"
    # Both have outcome='LOSS' — the FE MUST branch on sot_reconciliation_state
    # to compute honest wallet-impact P&L.
    assert real["outcome"] == "LOSS"
    assert phantom["outcome"] == "LOSS"


# ─── Query-param tests ───────────────────────────────────────────────────────


def test_resolved_true_narrows_to_outcome_not_null():
    """``?resolved=true`` must append ``outcome IS NOT NULL`` to the
    WHERE clause. The FE WR matrix uses this call to compute WR
    against only the resolved subset."""
    session = _make_session([])
    client = TestClient(_build_app(session))

    res = client.get("/api/v58/strategy-decisions?resolved=true")
    assert res.status_code == 200
    sql = session._last_stmt["stmt"]
    assert "outcome IS NOT NULL" in sql
    assert "strategy_decisions_resolved" in sql  # reading from the view, not the base table


def test_resolved_false_narrows_to_outcome_null():
    session = _make_session([])
    client = TestClient(_build_app(session))

    client.get("/api/v58/strategy-decisions?resolved=false")
    sql = session._last_stmt["stmt"]
    assert "outcome IS NULL" in sql


def test_resolved_omitted_does_not_filter():
    """No ``?resolved=`` param — no outcome clause at all. FE explorer
    default = show everything."""
    session = _make_session([])
    client = TestClient(_build_app(session))

    client.get("/api/v58/strategy-decisions")
    sql = session._last_stmt["stmt"]
    assert "outcome IS NOT NULL" not in sql
    assert "outcome IS NULL" not in sql


def test_strategy_id_and_resolved_compose():
    """Both filters compose with AND — needed for per-strategy WR tiles."""
    session = _make_session([])
    client = TestClient(_build_app(session))

    client.get("/api/v58/strategy-decisions?strategy_id=v4_fusion&resolved=true")
    sql = session._last_stmt["stmt"]
    params = session._last_stmt["params"]
    assert "strategy_id = :sid" in sql
    assert "outcome IS NOT NULL" in sql
    assert params["sid"] == "v4_fusion"


# ─── View-source assertion ───────────────────────────────────────────────────


def test_endpoint_reads_from_view_not_base_table():
    """Critical invariant: the endpoint must query the view, not the
    raw strategy_decisions table. Breaking this undoes the whole
    reason this PR exists (FE gets outcome for free)."""
    session = _make_session([])
    client = TestClient(_build_app(session))
    client.get("/api/v58/strategy-decisions")
    sql = session._last_stmt["stmt"]
    assert "FROM strategy_decisions_resolved" in sql
    assert "FROM strategy_decisions\n" not in sql  # not the base table


# ─── Error-path test ─────────────────────────────────────────────────────────


def test_graceful_degradation_on_db_error():
    """If the view is temporarily unavailable (mid-migration, DB blip),
    the endpoint must return an empty decisions list + error field,
    not 500. The FE shows a red banner, dashboard keeps working."""
    session = _make_session([], raise_exc=RuntimeError("view does not exist"))
    client = TestClient(_build_app(session))

    res = client.get("/api/v58/strategy-decisions")
    assert res.status_code == 200
    body = res.json()
    assert body["decisions"] == []
    assert "error" in body
    assert "view does not exist" in body["error"]
