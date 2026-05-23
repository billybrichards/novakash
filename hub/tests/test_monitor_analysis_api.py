"""Contract tests for hub/api/monitor_analysis.py.

Verifies /api/monitor/analysis returns a well-shaped envelope under a
mocked async DB session. Five queries fire via asyncio.gather; the mock
returns successive result sets in declared order.

Pattern lifted from test_monitor_scorecard_api.py (PR #576) — same
RowMapping duck-typing, AsyncMock execute, and TTL-cache reset fixture.

Each section is tested in isolation:

  1. live_8h         — per-strategy 8h LIVE roll-up
  2. ghost_24h       — per-strategy 24h GHOST would-fire roll-up
  3. featured        — cross-reference between ghost_24h + featured registry
  4. wallet          — USDC + pUSD note
  5. bankroll_24h    — hourly trajectory points
  6. reconciler_health — today / 5-min counts + healthy flag
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.monitor_analysis as ma
from api.monitor_analysis import router as analysis_router
from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session


_NOW = datetime(2026, 5, 23, 19, 30, tzinfo=timezone.utc)


# ─── Mock plumbing ───────────────────────────────────────────────────────────


def _mapping(data: dict):
    """Duck-type sqlalchemy RowMapping: subscriptable + dict-like."""
    m = MagicMock()
    m.__getitem__ = lambda self, k: data[k]
    m.get = lambda k, d=None: data.get(k, d)
    m.keys = lambda: data.keys()
    return m


def _result_all(rows: list[dict]) -> MagicMock:
    """Build mock for `await db.execute(...).mappings().all()`."""
    mappings_result = MagicMock()
    mappings_result.all.return_value = [_mapping(r) for r in rows]
    mappings_result.first.return_value = _mapping(rows[0]) if rows else None
    execute_result = MagicMock()
    execute_result.mappings.return_value = mappings_result
    return execute_result


def _result_first(row: Optional[dict]) -> MagicMock:
    """Build mock for `await db.execute(...).mappings().first()`."""
    mappings_result = MagicMock()
    mappings_result.first.return_value = _mapping(row) if row else None
    mappings_result.all.return_value = [_mapping(row)] if row else []
    execute_result = MagicMock()
    execute_result.mappings.return_value = mappings_result
    return execute_result


def _fake_session(
    live: list[dict],
    ghost: list[dict],
    wallet: Optional[dict],
    bank: list[dict],
    recon: Optional[dict],
) -> MagicMock:
    """AsyncSession stub returning result sets in the order gather() schedules
    fetchers: live_8h, ghost_24h, wallet_latest, bankroll_24h, reconciler.
    """
    results = [
        _result_all(live),
        _result_all(ghost),
        _result_first(wallet),
        _result_all(bank),
        _result_first(recon),
    ]
    session = MagicMock()
    session.execute = AsyncMock(side_effect=results)
    return session


def _fake_user() -> TokenData:
    return TokenData(user_id=1, username="billy", token_type="access")


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(analysis_router, prefix="/api")
    return app


def _client(
    live: list[dict] = (),
    ghost: list[dict] = (),
    wallet: Optional[dict] = None,
    bank: list[dict] = (),
    recon: Optional[dict] = None,
) -> TestClient:
    app = _make_app()
    session = _fake_session(list(live), list(ghost), wallet, list(bank), recon)

    async def _session_override():
        yield session

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_current_user] = lambda: _fake_user()
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset module-level TTL cache between tests."""
    ma._cache["data"] = None
    ma._cache["ts"] = 0.0
    yield
    ma._cache["data"] = None
    ma._cache["ts"] = 0.0


# ─── Fixture builders ────────────────────────────────────────────────────────


def _live_row(**ov) -> dict:
    base = {
        "strategy": "v9_2_raw_lgb",
        "fires": 11, "wins": 9, "losses": 1, "pending": 1,
        "wr_pct": 90.0, "net_pnl_usd": 4.88, "stake_usd": 49.5,
    }
    base.update(ov)
    return base


def _ghost_row(**ov) -> dict:
    base = {
        "strategy_id": "v9_3_btc_raw_lgb",
        "asset": "BTC",
        "would_fires": 747, "resolved": 747, "wins": 529, "losses": 218,
        "would_wr_pct": 70.8, "avg_fill_price": 0.85,
        "last_fire_at": _NOW,
    }
    base.update(ov)
    return base


def _wallet_row(**ov) -> dict:
    base = {"balance_usdc": 3.0009, "source": "clob_reconciler", "recorded_at": _NOW}
    base.update(ov)
    return base


def _bank_row(hours_ago: int = 0, usdc: float = 3.0) -> dict:
    return {
        "bucket_at": _NOW - timedelta(hours=hours_ago),
        "min_usdc": usdc, "max_usdc": usdc, "avg_usdc": usdc,
    }


def _recon_row(
    last_age_s: float = 60.0,
    today_redeemed: int = 13,
    recent_5m: int = 0,
    today_payout: float = 71.0,
) -> dict:
    return {
        "today_redeemed": today_redeemed,
        "recent_redeemed_5m": recent_5m,
        "today_payout_usd": today_payout,
        "last_stamp_at": datetime.now(timezone.utc) - timedelta(seconds=last_age_s),
    }


# ─── Envelope ────────────────────────────────────────────────────────────────


class TestEnvelope:
    def test_returns_200(self):
        client = _client(
            live=[_live_row()], ghost=[_ghost_row()],
            wallet=_wallet_row(), bank=[_bank_row()], recon=_recon_row(),
        )
        resp = client.get("/api/monitor/analysis")
        assert resp.status_code == 200

    def test_envelope_keys(self):
        client = _client(
            live=[_live_row()], ghost=[_ghost_row()],
            wallet=_wallet_row(), bank=[_bank_row()], recon=_recon_row(),
        )
        body = client.get("/api/monitor/analysis").json()
        for k in ("live_8h", "ghost_24h", "featured", "wallet",
                  "bankroll_24h", "reconciler_health",
                  "fetched_at", "cache_hit"):
            assert k in body, f"Missing envelope key: {k}"

    def test_cache_hit_false_on_first_call(self):
        client = _client(
            live=[_live_row()], ghost=[_ghost_row()],
            wallet=_wallet_row(), bank=[_bank_row()], recon=_recon_row(),
        )
        body = client.get("/api/monitor/analysis").json()
        assert body["cache_hit"] is False

    def test_cache_hit_true_on_second_call(self):
        client = _client(
            live=[_live_row()], ghost=[_ghost_row()],
            wallet=_wallet_row(), bank=[_bank_row()], recon=_recon_row(),
        )
        client.get("/api/monitor/analysis")
        body = client.get("/api/monitor/analysis").json()
        assert body["cache_hit"] is True

    def test_empty_db_returns_well_formed_envelope(self):
        client = _client(live=[], ghost=[], wallet=None, bank=[], recon=None)
        body = client.get("/api/monitor/analysis").json()
        assert body["live_8h"] == []
        assert body["ghost_24h"] == []
        # featured always returns one row per featured strategy, even when no data
        assert len(body["featured"]) == len(ma._FEATURED_STRATEGIES)
        assert body["wallet"]["balance_usdc"] is None
        assert body["bankroll_24h"] == []
        assert body["reconciler_health"]["healthy"] is False


# ─── live_8h section ─────────────────────────────────────────────────────────


class TestLive8h:
    def test_row_shape(self):
        client = _client(live=[_live_row()], ghost=[], wallet=None,
                         bank=[], recon=None)
        row = client.get("/api/monitor/analysis").json()["live_8h"][0]
        for f in ("strategy", "fires", "wins", "losses", "pending",
                  "wr_pct", "net_pnl_usd", "stake_usd"):
            assert f in row, f"Missing live_8h.{f}"

    def test_values_mapped_correctly(self):
        client = _client(live=[_live_row()], ghost=[], wallet=None,
                         bank=[], recon=None)
        row = client.get("/api/monitor/analysis").json()["live_8h"][0]
        assert row["strategy"] == "v9_2_raw_lgb"
        assert row["fires"] == 11
        assert row["wins"] == 9
        assert row["losses"] == 1
        assert row["pending"] == 1
        assert row["wr_pct"] == 90.0
        assert row["net_pnl_usd"] == 4.88


# ─── ghost_24h section ───────────────────────────────────────────────────────


class TestGhost24h:
    def test_row_shape(self):
        client = _client(live=[], ghost=[_ghost_row()], wallet=None,
                         bank=[], recon=None)
        row = client.get("/api/monitor/analysis").json()["ghost_24h"][0]
        for f in ("strategy_id", "asset", "would_fires", "resolved",
                  "wins", "losses", "would_wr_pct", "avg_fill_price",
                  "last_fire_at"):
            assert f in row, f"Missing ghost_24h.{f}"

    def test_values_mapped_correctly(self):
        client = _client(live=[], ghost=[_ghost_row()], wallet=None,
                         bank=[], recon=None)
        row = client.get("/api/monitor/analysis").json()["ghost_24h"][0]
        assert row["strategy_id"] == "v9_3_btc_raw_lgb"
        assert row["asset"] == "BTC"
        assert row["would_fires"] == 747
        assert row["wins"] == 529
        assert row["would_wr_pct"] == 70.8


# ─── featured section ────────────────────────────────────────────────────────


class TestFeatured:
    def test_one_row_per_featured_strategy(self):
        client = _client(live=[], ghost=[], wallet=None, bank=[], recon=None)
        body = client.get("/api/monitor/analysis").json()
        assert len(body["featured"]) == len(ma._FEATURED_STRATEGIES)
        sids = [f["strategy_id"] for f in body["featured"]]
        for spec in ma._FEATURED_STRATEGIES:
            assert spec["strategy_id"] in sids

    def test_train_serve_skew_flagged_when_gap_exceeds_5pp(self):
        # ghost row shows 70.8%; v9_3_btc_raw_lgb has training projection 90.2%
        # → skew = 19.4 pp → skew_warning True
        client = _client(
            live=[],
            ghost=[_ghost_row(strategy_id="v9_3_btc_raw_lgb", would_wr_pct=70.8)],
            wallet=None, bank=[], recon=None,
        )
        body = client.get("/api/monitor/analysis").json()
        f = next(x for x in body["featured"] if x["strategy_id"] == "v9_3_btc_raw_lgb")
        assert f["live_would_wr_pct"] == 70.8
        assert f["train_projection_wr_pct"] == 90.2
        assert f["skew_warning"] is True
        assert f["train_serve_skew_pp"] == 19.4

    def test_no_warning_when_gap_within_5pp(self):
        # would_wr 88.0% vs training 90.2% → skew 2.2 pp → no warning
        client = _client(
            live=[],
            ghost=[_ghost_row(strategy_id="v9_3_btc_raw_lgb", would_wr_pct=88.0)],
            wallet=None, bank=[], recon=None,
        )
        body = client.get("/api/monitor/analysis").json()
        f = next(x for x in body["featured"] if x["strategy_id"] == "v9_3_btc_raw_lgb")
        assert f["skew_warning"] is False

    def test_has_data_false_when_strategy_absent(self):
        client = _client(live=[], ghost=[], wallet=None, bank=[], recon=None)
        body = client.get("/api/monitor/analysis").json()
        for f in body["featured"]:
            assert f["has_data"] is False
            assert f["live_would_wr_pct"] is None


# ─── wallet section ──────────────────────────────────────────────────────────


class TestWallet:
    def test_usdc_returned(self):
        client = _client(live=[], ghost=[], wallet=_wallet_row(),
                         bank=[], recon=None)
        w = client.get("/api/monitor/analysis").json()["wallet"]
        assert w["balance_usdc"] == 3.0009
        assert w["source"] == "clob_reconciler"
        assert w["snapshot_at"] is not None

    def test_pusd_note_present(self):
        client = _client(live=[], ghost=[], wallet=_wallet_row(),
                         bank=[], recon=None)
        w = client.get("/api/monitor/analysis").json()["wallet"]
        # The hub box cannot reach Polymarket; PR 3 plumbs the Montreal sidecar.
        assert w["pusd_approx"] is None
        assert "Montreal" in (w["pusd_note"] or "")

    def test_missing_wallet_row_returns_none_with_hint(self):
        client = _client(live=[], ghost=[], wallet=None, bank=[], recon=None)
        w = client.get("/api/monitor/analysis").json()["wallet"]
        assert w["balance_usdc"] is None
        assert w["pusd_note"] is not None


# ─── bankroll_24h section ────────────────────────────────────────────────────


class TestBankroll:
    def test_row_shape(self):
        client = _client(live=[], ghost=[], wallet=None,
                         bank=[_bank_row(hours_ago=0, usdc=3.0)], recon=None)
        row = client.get("/api/monitor/analysis").json()["bankroll_24h"][0]
        for f in ("bucket_at", "min_usdc", "max_usdc", "avg_usdc"):
            assert f in row, f"Missing bankroll_24h.{f}"

    def test_multiple_rows_returned_in_order(self):
        client = _client(
            live=[], ghost=[], wallet=None,
            bank=[_bank_row(hours_ago=2, usdc=5.0),
                  _bank_row(hours_ago=1, usdc=10.0),
                  _bank_row(hours_ago=0, usdc=3.0)],
            recon=None,
        )
        rows = client.get("/api/monitor/analysis").json()["bankroll_24h"]
        assert len(rows) == 3
        # avg_usdc preserved per row
        assert {r["avg_usdc"] for r in rows} == {5.0, 10.0, 3.0}


# ─── reconciler_health section ───────────────────────────────────────────────


class TestReconcilerHealth:
    def test_healthy_when_recent_stamp(self):
        # stamp 60s ago → < 30 min → healthy True
        client = _client(live=[], ghost=[], wallet=None, bank=[],
                         recon=_recon_row(last_age_s=60.0))
        h = client.get("/api/monitor/analysis").json()["reconciler_health"]
        assert h["healthy"] is True

    def test_unhealthy_when_stale_stamp(self):
        # stamp 2h ago → > 30 min → healthy False
        client = _client(live=[], ghost=[], wallet=None, bank=[],
                         recon=_recon_row(last_age_s=7200.0))
        h = client.get("/api/monitor/analysis").json()["reconciler_health"]
        assert h["healthy"] is False

    def test_counts_and_payout_mapped(self):
        client = _client(
            live=[], ghost=[], wallet=None, bank=[],
            recon=_recon_row(
                last_age_s=60.0, today_redeemed=13, recent_5m=2, today_payout=71.0,
            ),
        )
        h = client.get("/api/monitor/analysis").json()["reconciler_health"]
        assert h["today_redeemed"] == 13
        assert h["recent_redeemed_5m"] == 2
        assert h["today_payout_usd"] == 71.0
