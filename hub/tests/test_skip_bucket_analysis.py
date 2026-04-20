"""
Audit-task #255 F3 — tests for hub/api/v58_monitor.py::skip_bucket_analysis.

Scope:

  1. Response shape — required top-level keys (strategy, hours, buckets,
     total_windows, trade_windows, skip_windows).
  2. Per-bucket fields — distinct_windows, re_eval_hits, resolved_count,
     hypo_wr, hypo_pnl_usd, bands{HIGH,MEDIUM,LOW}.
  3. ``hypo_wr`` math — wins / resolved.
  4. ``hypo_pnl_usd`` math — +1 per WIN, -1 per LOSS, 0 per unresolved.
  5. The TRADE bucket is surfaced separately from SKIP buckets.
  6. Graceful degradation when the matview is missing / DB blip.
  7. The ``/refresh`` endpoint invokes REFRESH MATERIALIZED VIEW
     CONCURRENTLY.

Pattern mirrors test_strategy_decisions_resolved.py — dependency_overrides
for auth + session.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v58_monitor import router
from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session


# ─── Test helpers ────────────────────────────────────────────────────────────


def _bucket_row(
    *,
    bucket: str,
    distinct_windows: int = 10,
    re_eval_hits: int = 300,
    resolved_count: int = 8,
    hypo_wins: int = 5,
    hypo_losses: int = 3,
    band_high: int = 3,
    band_high_wins: int = 2,
    band_high_resolved: int = 3,
    band_med: int = 4,
    band_med_wins: int = 2,
    band_med_resolved: int = 4,
    band_low: int = 3,
    band_low_wins: int = 1,
    band_low_resolved: int = 1,
    hypo_pnl_unit: Optional[float] = None,
) -> dict:
    """Build a row shaped like the SELECT projection from
    skip_bucket_analysis's bucket_q. Unspecified ``hypo_pnl_unit`` is
    auto-derived to match ``hypo_wins - hypo_losses``.
    """
    if hypo_pnl_unit is None:
        hypo_pnl_unit = float(hypo_wins - hypo_losses)
    return {
        "bucket": bucket,
        "distinct_windows": distinct_windows,
        "re_eval_hits": re_eval_hits,
        "resolved_count": resolved_count,
        "hypo_wins": hypo_wins,
        "hypo_losses": hypo_losses,
        "band_high": band_high,
        "band_high_wins": band_high_wins,
        "band_high_resolved": band_high_resolved,
        "band_med": band_med,
        "band_med_wins": band_med_wins,
        "band_med_resolved": band_med_resolved,
        "band_low": band_low,
        "band_low_wins": band_low_wins,
        "band_low_resolved": band_low_resolved,
        "hypo_pnl_unit": hypo_pnl_unit,
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
    """Mock AsyncSession.execute → result.mappings().all() returning rows.

    For the refresh endpoint we also expose the list of statements so the
    test can verify the REFRESH MATERIALIZED VIEW CONCURRENTLY was issued.
    """
    session = MagicMock()
    stmts: list[str] = []

    async def fake_execute(stmt, params=None):
        stmts.append(str(stmt))
        if raise_exc is not None:
            raise raise_exc
        result = MagicMock()
        result.mappings.return_value.all.return_value = rows
        return result

    async def fake_commit():
        pass

    session.execute = fake_execute
    session.commit = fake_commit
    session._stmts = stmts
    return session


# ─── Response-shape tests ────────────────────────────────────────────────────


def test_top_level_shape_has_required_fields():
    rows = [_bucket_row(bucket="source_disagree")]
    client = TestClient(_build_app(_make_session(rows)))
    body = client.get("/api/v58/skip-bucket-analysis?strategy=v6_sniper").json()

    for key in ("strategy", "hours", "buckets", "total_windows",
                "trade_windows", "skip_windows", "matview"):
        assert key in body, f"missing top-level key {key!r}"
    assert body["strategy"] == "v6_sniper"
    assert body["hours"] == 48  # default


def test_bucket_fields_present():
    rows = [_bucket_row(bucket="source_disagree")]
    client = TestClient(_build_app(_make_session(rows)))
    body = client.get("/api/v58/skip-bucket-analysis?strategy=v6_sniper").json()

    assert len(body["buckets"]) == 1
    b = body["buckets"][0]
    for key in ("skip_reason", "distinct_windows", "re_eval_hits",
                "resolved_count", "hypo_wins", "hypo_losses",
                "hypo_wr", "hypo_pnl_usd", "bands"):
        assert key in b, f"missing bucket key {key!r}"
    for band_name in ("HIGH", "MEDIUM", "LOW"):
        assert band_name in b["bands"]


def test_hypo_wr_math():
    """hypo_wr = wins / resolved_count (the SKIP-but-shadow-resolved set)."""
    rows = [
        _bucket_row(bucket="regime_risk_off",
                    resolved_count=10, hypo_wins=7, hypo_losses=3),
    ]
    client = TestClient(_build_app(_make_session(rows)))
    body = client.get("/api/v58/skip-bucket-analysis?strategy=v4_fusion").json()
    assert body["buckets"][0]["hypo_wr"] == 0.7


def test_hypo_wr_none_when_unresolved():
    rows = [_bucket_row(bucket="cold_start",
                        resolved_count=0, hypo_wins=0, hypo_losses=0)]
    client = TestClient(_build_app(_make_session(rows)))
    body = client.get("/api/v58/skip-bucket-analysis?strategy=v10_gate").json()
    assert body["buckets"][0]["hypo_wr"] is None


def test_hypo_pnl_uses_unit_stake():
    """$1 unit stake — +1 per WIN, -1 per LOSS, 0 per unresolved."""
    rows = [
        _bucket_row(bucket="source_disagree",
                    hypo_wins=5, hypo_losses=3, hypo_pnl_unit=2.0),
    ]
    client = TestClient(_build_app(_make_session(rows)))
    body = client.get("/api/v58/skip-bucket-analysis?strategy=v6_sniper").json()
    assert body["buckets"][0]["hypo_pnl_usd"] == 2.0


def test_trade_bucket_surfaced_as_null_skip_reason():
    """The __trade__ sentinel row maps to skip_reason=None in the response
    so the FE can distinguish TRADE vs SKIP buckets."""
    rows = [
        _bucket_row(bucket="__trade__", distinct_windows=31,
                    resolved_count=28, hypo_wins=19, hypo_losses=9),
        _bucket_row(bucket="regime_risk_off", distinct_windows=142,
                    resolved_count=120, hypo_wins=60, hypo_losses=60),
    ]
    client = TestClient(_build_app(_make_session(rows)))
    body = client.get("/api/v58/skip-bucket-analysis?strategy=v6_sniper").json()

    trade = next(b for b in body["buckets"] if b["bucket"] == "__trade__")
    assert trade["skip_reason"] is None
    assert body["trade_windows"] == 31
    assert body["skip_windows"] == 142
    assert body["total_windows"] == 31 + 142


def test_band_wr_math_within_bucket():
    rows = [_bucket_row(
        bucket="regime_risk_off",
        band_high=10, band_high_wins=8, band_high_resolved=10,
        band_med=5, band_med_wins=2, band_med_resolved=5,
        band_low=3, band_low_wins=0, band_low_resolved=3,
    )]
    client = TestClient(_build_app(_make_session(rows)))
    body = client.get("/api/v58/skip-bucket-analysis?strategy=v4_fusion").json()
    bands = body["buckets"][0]["bands"]
    assert bands["HIGH"]["hypo_wr"] == 0.8
    assert bands["MEDIUM"]["hypo_wr"] == 0.4
    assert bands["LOW"]["hypo_wr"] == 0.0


def test_hours_query_param_respected():
    rows = [_bucket_row(bucket="source_disagree")]
    session = _make_session(rows)
    client = TestClient(_build_app(session))
    body = client.get(
        "/api/v58/skip-bucket-analysis?strategy=v6_sniper&hours=168"
    ).json()
    assert body["hours"] == 168


def test_strategy_required():
    client = TestClient(_build_app(_make_session([])))
    res = client.get("/api/v58/skip-bucket-analysis")
    # FastAPI returns 422 when a required query param is missing.
    assert res.status_code == 422


def test_sql_queries_the_matview_not_base_table():
    """Critical invariant — the endpoint must read from the matview or
    the perf fix is moot."""
    session = _make_session([])
    client = TestClient(_build_app(session))
    client.get("/api/v58/skip-bucket-analysis?strategy=v6_sniper")
    combined = "\n".join(session._stmts)
    assert "FROM strategy_skip_resolved" in combined


def test_graceful_degradation_on_db_error():
    session = _make_session([], raise_exc=RuntimeError("matview missing"))
    client = TestClient(_build_app(session))
    res = client.get("/api/v58/skip-bucket-analysis?strategy=v6_sniper")
    assert res.status_code == 200
    body = res.json()
    assert body["buckets"] == []
    assert "error" in body


# ─── /refresh endpoint tests ─────────────────────────────────────────────────


def test_refresh_endpoint_issues_concurrent_refresh():
    session = _make_session([])
    client = TestClient(_build_app(session))
    res = client.post("/api/v58/skip-bucket-analysis/refresh")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert "refreshed_at" in body
    combined = "\n".join(session._stmts)
    assert "REFRESH MATERIALIZED VIEW CONCURRENTLY strategy_skip_resolved" in combined


def test_refresh_endpoint_gracefully_reports_error():
    session = _make_session([], raise_exc=RuntimeError("concurrent refresh conflict"))
    client = TestClient(_build_app(session))
    res = client.post("/api/v58/skip-bucket-analysis/refresh")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "error"
    assert "concurrent refresh conflict" in body["error"]
