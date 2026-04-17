"""
Tests for hub/api/gate_traces.py — audit task #188.

``GET /api/gate-traces/heatmap`` aggregates ``gate_check_traces`` into a
strategy × gate matrix the FE renders as a heatmap. ``GET
/api/gate-traces/recent`` fetches the latest N chains for the drill-down.

Covers:
  1. Response shapes — both endpoints return **bare dicts** (never wrapped
     under an array-envelope key the FE ``useApiLoader`` would auto-unwrap).
  2. Happy-path aggregation correctness — fired / passed / pass_pct maths
     agree with manual counts from a known fixture.
  3. Top skip reasons — exposed for the failing gates only.
  4. Empty DB returns the full shape with empty lists, not 500.
  5. ``hours`` param validation — reject <1 or >168.
  6. ``timeframe`` param validation — reject anything other than 5m/15m/1h.
  7. Optional ``strategy_id`` filter composes with the base WHERE.
  8. DB exception returns the graceful shape with ``error`` attached.

Auth + session are stubbed via FastAPI ``dependency_overrides`` — same
pattern as ``tests/test_positions_api.py`` and
``tests/test_strategy_decisions_resolved.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.gate_traces import router
from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session


# ─── App / Session harness ────────────────────────────────────────────────────


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


def _make_session(
    responses: List[List[dict]],
    raise_exc: Optional[Exception] = None,
    raise_on_call: Optional[int] = None,
):
    """Build a MagicMock AsyncSession that returns ``responses[i]`` on the
    i-th execute call. Each response is a list of row-dicts.

    ``raise_on_call`` raises ``raise_exc`` on that specific call index
    (0-based) — lets us prove the error path is robust to failures on
    any of the three queries the heatmap endpoint issues.
    """
    session = MagicMock()
    call_log: list[tuple[str, dict]] = []
    counter = {"i": 0}

    async def fake_execute(stmt, params=None):
        idx = counter["i"]
        counter["i"] += 1
        call_log.append((str(stmt), params or {}))
        if raise_exc is not None and (
            raise_on_call is None or raise_on_call == idx
        ):
            raise raise_exc
        rows = responses[idx] if idx < len(responses) else []
        result = MagicMock()
        # Support both .mappings().all() and .mappings().first()
        mapping = MagicMock()
        mapping.all.return_value = rows
        mapping.first.return_value = rows[0] if rows else None
        result.mappings.return_value = mapping
        return result

    session.execute = fake_execute
    session._call_log = call_log
    return session


def _agg_row(strategy_id: str, gate_name: str, fired: int, passed: int) -> dict:
    """Mirror the SELECT projection of the heatmap aggregation query."""
    pct = round(100.0 * passed / fired, 1) if fired else None
    return {
        "strategy_id": strategy_id,
        "gate_name": gate_name,
        "fired": fired,
        "passed": passed,
        "pass_pct": pct,
    }


def _reason_row(strategy_id: str, gate_name: str, reason: str, n: int) -> dict:
    return {
        "strategy_id": strategy_id,
        "gate_name": gate_name,
        "reason_text": reason,
        "n": n,
    }


def _meta_row(
    count: int,
    earliest: Optional[datetime] = None,
    latest: Optional[datetime] = None,
) -> dict:
    return {
        "row_count_raw": count,
        "earliest": earliest,
        "latest": latest,
    }


# ─── /heatmap — response shape ───────────────────────────────────────────────


def test_heatmap_returns_bare_dict_never_wrapped() -> None:
    """The FE ``useApiLoader`` auto-unwraps ``rows / trades / decisions /
    items``. A wrapped response would break the page. The endpoint must
    return a bare dict keyed ``strategies / gates / cells / window``."""
    session = _make_session(
        responses=[
            [_agg_row("v4_fusion", "confidence", fired=100, passed=70)],
            [_reason_row("v4_fusion", "confidence", "dist < 0.12", n=30)],
            [_meta_row(100)],
        ]
    )
    client = TestClient(_build_app(session))

    res = client.get("/api/gate-traces/heatmap")
    assert res.status_code == 200, res.text
    body = res.json()

    # Bare dict, not wrapped under any envelope array key
    assert isinstance(body, dict)
    for k in ("strategies", "gates", "cells", "window"):
        assert k in body, f"heatmap response missing {k!r}"
    # `cells` is an array — must NOT sit under a key the FE auto-unwraps
    assert not any(k in body for k in ("rows", "trades", "decisions", "items"))


def test_heatmap_cells_compute_pass_pct_correctly() -> None:
    """fired=100, passed=70 → pass_pct=70.0. FE feeds this straight into
    ``wrColor(pct/100)`` to colour the heatmap cell."""
    session = _make_session(
        responses=[
            [
                _agg_row("v4_fusion", "confidence", fired=100, passed=70),
                _agg_row("v4_fusion", "direction", fired=50, passed=45),
                _agg_row("v4_up_basic", "timing", fired=200, passed=100),
            ],
            [],
            [_meta_row(350)],
        ]
    )
    client = TestClient(_build_app(session))

    body = client.get("/api/gate-traces/heatmap").json()
    cells_by_pair = {(c["strategy"], c["gate"]): c for c in body["cells"]}
    assert cells_by_pair[("v4_fusion", "confidence")]["pass_pct"] == pytest.approx(70.0)
    assert cells_by_pair[("v4_fusion", "direction")]["pass_pct"] == pytest.approx(90.0)
    assert cells_by_pair[("v4_up_basic", "timing")]["pass_pct"] == pytest.approx(50.0)


def test_heatmap_exposes_strategies_and_gates_sorted() -> None:
    """FE uses ``strategies`` as table-row ids and ``gates`` as column
    headers. Both must be de-duplicated and stably sorted so the grid
    doesn't re-shuffle on every poll."""
    session = _make_session(
        responses=[
            [
                _agg_row("v4_up_basic", "timing", fired=10, passed=5),
                _agg_row("v4_fusion", "confidence", fired=10, passed=5),
                _agg_row("v4_fusion", "timing", fired=10, passed=5),
                _agg_row("v4_up_basic", "confidence", fired=10, passed=5),
            ],
            [],
            [_meta_row(40)],
        ]
    )
    client = TestClient(_build_app(session))
    body = client.get("/api/gate-traces/heatmap").json()
    assert body["strategies"] == ["v4_fusion", "v4_up_basic"]
    assert body["gates"] == ["confidence", "timing"]


def test_heatmap_attaches_top_skip_reasons() -> None:
    """Cells for failing gates surface the top N skip reasons so the
    operator can drill down without a second round-trip."""
    session = _make_session(
        responses=[
            [_agg_row("v4_fusion", "confidence", fired=100, passed=20)],
            [
                _reason_row("v4_fusion", "confidence", "dist < 0.12", n=50),
                _reason_row("v4_fusion", "confidence", "score too low", n=25),
                _reason_row("v4_fusion", "confidence", "model abstain", n=5),
            ],
            [_meta_row(100)],
        ]
    )
    client = TestClient(_build_app(session))
    body = client.get("/api/gate-traces/heatmap").json()
    cell = body["cells"][0]
    reasons = cell["top_skip_reasons"]
    assert len(reasons) == 3
    assert reasons[0] == {"reason": "dist < 0.12", "n": 50}
    assert reasons[1] == {"reason": "score too low", "n": 25}
    assert reasons[2] == {"reason": "model abstain", "n": 5}


# ─── /heatmap — empty DB ─────────────────────────────────────────────────────


def test_heatmap_empty_db_returns_full_shape_not_500() -> None:
    """Gate table exists but has no rows for the slice → every array is
    empty, window.row_count_raw=0, no exception."""
    session = _make_session(responses=[[], [], [_meta_row(0)]])
    client = TestClient(_build_app(session))

    res = client.get("/api/gate-traces/heatmap")
    assert res.status_code == 200
    body = res.json()
    assert body["strategies"] == []
    assert body["gates"] == []
    assert body["cells"] == []
    assert body["window"]["row_count_raw"] == 0
    assert body["window"]["earliest"] is None
    assert body["window"]["latest"] is None
    assert "error" not in body


def test_heatmap_meta_row_null_returns_zero_count() -> None:
    """Some drivers return None instead of an empty dict from
    ``.mappings().first()`` when the aggregate selects nothing. The
    endpoint must defend against that (no AttributeError on ``None.get``)."""
    session = _make_session(responses=[[], [], []])  # no meta row at all
    client = TestClient(_build_app(session))
    body = client.get("/api/gate-traces/heatmap").json()
    assert body["window"]["row_count_raw"] == 0


# ─── /heatmap — param validation ─────────────────────────────────────────────


@pytest.mark.parametrize("bad_hours", [0, -1, 169, 999999])
def test_heatmap_rejects_invalid_hours(bad_hours: int) -> None:
    session = _make_session(responses=[[], [], []])
    client = TestClient(_build_app(session))
    res = client.get(f"/api/gate-traces/heatmap?hours={bad_hours}")
    assert res.status_code == 400
    assert "hours" in res.json()["detail"]


@pytest.mark.parametrize("good_hours", [1, 24, 72, 168])
def test_heatmap_accepts_valid_hours(good_hours: int) -> None:
    session = _make_session(responses=[[], [], [_meta_row(0)]])
    client = TestClient(_build_app(session))
    res = client.get(f"/api/gate-traces/heatmap?hours={good_hours}")
    assert res.status_code == 200
    assert res.json()["window"]["hours"] == good_hours


@pytest.mark.parametrize("bad_tf", ["30s", "2m", "4h", "daily", ""])
def test_heatmap_rejects_invalid_timeframe(bad_tf: str) -> None:
    session = _make_session(responses=[[], [], []])
    client = TestClient(_build_app(session))
    res = client.get(f"/api/gate-traces/heatmap?timeframe={bad_tf}")
    assert res.status_code == 400
    assert "timeframe" in res.json()["detail"]


def test_heatmap_strategy_id_filter_reaches_sql() -> None:
    """``?strategy_id=v4_fusion`` must narrow all three queries server-side."""
    session = _make_session(responses=[[], [], [_meta_row(0)]])
    client = TestClient(_build_app(session))

    client.get("/api/gate-traces/heatmap?strategy_id=v4_fusion")
    # Every one of the three queries should carry the strategy_id bind
    for stmt, params in session._call_log:
        if "gate_check_traces" in stmt:
            assert "strategy_id = :sid" in stmt
            assert params.get("sid") == "v4_fusion"


def test_heatmap_no_strategy_id_filter_means_no_sid_bind() -> None:
    session = _make_session(responses=[[], [], [_meta_row(0)]])
    client = TestClient(_build_app(session))

    client.get("/api/gate-traces/heatmap")
    for stmt, params in session._call_log:
        if "gate_check_traces" in stmt:
            assert "strategy_id = :sid" not in stmt
            assert "sid" not in params


# ─── /heatmap — error path ───────────────────────────────────────────────────


def test_heatmap_db_error_returns_graceful_shape_not_500() -> None:
    """Mid-migration / view-missing / connection drop — the FE must keep
    working. Same philosophy as ``/v58/strategy-decisions``: return empty
    arrays + an ``error`` field."""
    session = _make_session(
        responses=[],
        raise_exc=RuntimeError("simulated DB outage"),
        raise_on_call=0,
    )
    client = TestClient(_build_app(session))

    res = client.get("/api/gate-traces/heatmap")
    assert res.status_code == 200
    body = res.json()
    assert body["strategies"] == []
    assert body["gates"] == []
    assert body["cells"] == []
    assert "error" in body
    assert "simulated DB outage" in body["error"]


# ─── /recent ─────────────────────────────────────────────────────────────────


def _key_row(strategy_id: str, window_ts: int, offset: int, ts: datetime) -> dict:
    return {
        "strategy_id": strategy_id,
        "window_ts": window_ts,
        "eval_offset": offset,
        "latest": ts,
    }


def _trace_row(
    strategy_id: str,
    window_ts: int,
    offset: int,
    gate_order: int,
    gate_name: str,
    passed: bool,
    *,
    action: str = "TRADE",
    direction: Optional[str] = "UP",
) -> dict:
    return {
        "strategy_id": strategy_id,
        "window_ts": window_ts,
        "eval_offset": offset,
        "gate_order": gate_order,
        "gate_name": gate_name,
        "passed": passed,
        "mode": "LIVE",
        "action": action,
        "direction": direction,
        "reason": "",
        "skip_reason": None,
        "observed_text": '{"delta_pct": 0.001}',
        "config_text": '{"type": "confidence"}',
        "evaluated_at": datetime(2026, 4, 17, 8, 40, 0, tzinfo=timezone.utc),
    }


def test_recent_groups_traces_by_strategy_window_offset() -> None:
    """Every (strategy, window, offset) triple becomes one group with
    its gate rows ordered by gate_order."""
    t = datetime(2026, 4, 17, 8, 40, 0, tzinfo=timezone.utc)
    keys = [
        _key_row("v4_fusion", 1776399900, 60, t),
        _key_row("v4_fusion", 1776399600, 60, t),
    ]
    traces = [
        _trace_row("v4_fusion", 1776399900, 60, 0, "timing", True),
        _trace_row("v4_fusion", 1776399900, 60, 1, "confidence", False),
        _trace_row("v4_fusion", 1776399600, 60, 0, "timing", True),
    ]
    session = _make_session(responses=[keys, traces])
    client = TestClient(_build_app(session))

    body = client.get("/api/gate-traces/recent").json()
    assert body["count"] == 2
    assert len(body["groups"]) == 2
    first = body["groups"][0]
    # The ordering should match the keys query, which is newest-first
    assert first["strategy_id"] == "v4_fusion"
    assert first["window_ts"] == 1776399900
    assert len(first["gates"]) == 2
    assert first["gates"][0]["gate_name"] == "timing"
    assert first["gates"][0]["passed"] is True
    assert first["gates"][1]["passed"] is False


def test_recent_parses_observed_and_config_json_back_to_dict() -> None:
    """JSONB is stringified via ``::text`` in SQL so asyncpg returns a
    str. The endpoint must parse it back to a dict for the FE drill-down
    (nested keys are meaningful: delta_pct, v2_probability_up, etc.)."""
    t = datetime(2026, 4, 17, 8, 40, 0, tzinfo=timezone.utc)
    keys = [_key_row("v4_fusion", 1776399900, 60, t)]
    traces = [
        _trace_row("v4_fusion", 1776399900, 60, 0, "confidence", False),
    ]
    session = _make_session(responses=[keys, traces])
    client = TestClient(_build_app(session))

    body = client.get("/api/gate-traces/recent").json()
    gate = body["groups"][0]["gates"][0]
    # Parsed back from the '{"delta_pct": 0.001}' stringified JSONB
    assert gate["observed"] == {"delta_pct": 0.001}
    assert gate["config"] == {"type": "confidence"}


def test_recent_bare_map_shape_with_groups_key() -> None:
    """Array is intentionally nested under ``groups`` (not ``rows /
    trades / decisions / items``) so ``useApiLoader`` does not auto-
    unwrap it and the FE receives {groups, count} as-is."""
    session = _make_session(responses=[[], []])
    client = TestClient(_build_app(session))
    body = client.get("/api/gate-traces/recent").json()
    assert isinstance(body, dict)
    assert body == {"groups": [], "count": 0}
    assert not any(k in body for k in ("rows", "trades", "decisions", "items"))


@pytest.mark.parametrize("bad_limit", [0, -1, 201, 9999])
def test_recent_rejects_invalid_limit(bad_limit: int) -> None:
    session = _make_session(responses=[[], []])
    client = TestClient(_build_app(session))
    res = client.get(f"/api/gate-traces/recent?limit={bad_limit}")
    assert res.status_code == 400
    assert "limit" in res.json()["detail"]


def test_recent_malformed_json_is_tolerated() -> None:
    """One corrupt JSONB cell must not kill the whole response. The
    parser returns a ``{"raw": ...}`` placeholder for the bad cell."""
    t = datetime(2026, 4, 17, 8, 40, 0, tzinfo=timezone.utc)
    keys = [_key_row("v4_fusion", 1776399900, 60, t)]
    bad_trace = _trace_row("v4_fusion", 1776399900, 60, 0, "confidence", False)
    bad_trace["observed_text"] = "{NOT VALID JSON"
    session = _make_session(responses=[keys, [bad_trace]])
    client = TestClient(_build_app(session))

    body = client.get("/api/gate-traces/recent").json()
    gate = body["groups"][0]["gates"][0]
    # No crash — payload carries a diagnostic raw field instead
    assert "raw" in gate["observed"]


def test_recent_db_error_returns_graceful_shape() -> None:
    session = _make_session(
        responses=[],
        raise_exc=RuntimeError("connection reset"),
        raise_on_call=0,
    )
    client = TestClient(_build_app(session))

    res = client.get("/api/gate-traces/recent")
    assert res.status_code == 200
    body = res.json()
    assert body["groups"] == []
    assert body["count"] == 0
    assert "error" in body
