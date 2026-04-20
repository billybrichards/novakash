"""
Tests for hub/api/wallet.py — Wallet v2 (note #189).

Covers:
  1. classify_stuck_reason(): 4 categories + the null (engine-is-slow) case.
  2. _sot_rank(): worst-rank aggregation (spec §5).
  3. /api/wallet/snapshot: shape + missing-table tolerance.
  4. /api/wallet/pending:  shape + stuck_reason classification via SQL.
  5. /api/wallet/history:  shape, transport/initiator passthrough, paging.

Pattern follows hub/tests/test_positions_api.py — dependency_overrides for
auth + session, no live DB, no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from api.wallet import (
    _sot_rank,
    classify_stuck_reason,
    router,
)
from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session


# ─── stuck_reason classifier unit tests ───────────────────────────────────────


def test_stuck_reason_negrisk_unresolved_when_not_redeemable():
    r = classify_stuck_reason(
        redeemable=False,
        payout_denominator=0,
        cooldown_active=False,
        quota_used_today=0,
        daily_quota_limit=100,
    )
    assert r == "negrisk_unresolved"


def test_stuck_reason_engine_cooldown():
    r = classify_stuck_reason(
        redeemable=True,
        payout_denominator=1,
        cooldown_active=True,
        quota_used_today=5,
        daily_quota_limit=100,
    )
    assert r == "engine_cooldown"


def test_stuck_reason_relayer_quota_exhausted():
    r = classify_stuck_reason(
        redeemable=True,
        payout_denominator=1,
        cooldown_active=False,
        quota_used_today=85,  # ≥ 80% of 100 → exhausted
        daily_quota_limit=100,
    )
    assert r == "relayer_quota_exhausted"


def test_stuck_reason_none_when_engine_should_handle_it():
    """Redeemable, no cooldown, quota fine → engine should be redeeming.
    None = "no stuck reason, if it's sitting it's just slow-engine signal."""
    r = classify_stuck_reason(
        redeemable=True,
        payout_denominator=1,
        cooldown_active=False,
        quota_used_today=3,
        daily_quota_limit=100,
    )
    assert r is None


def test_stuck_reason_cooldown_beats_quota():
    """Priority: cooldown > quota (both true → cooldown wins)."""
    r = classify_stuck_reason(
        redeemable=True,
        payout_denominator=1,
        cooldown_active=True,
        quota_used_today=95,
        daily_quota_limit=100,
    )
    assert r == "engine_cooldown"


# ─── _sot_rank aggregation unit tests ─────────────────────────────────────────


def test_sot_rank_all_ones_is_one():
    assert _sot_rank(1, 1, 1) == 1


def test_sot_rank_mixed_returns_max():
    assert _sot_rank(1, 3, 5) == 5
    assert _sot_rank(2, 4) == 4


def test_sot_rank_empty_is_worst():
    assert _sot_rank() == 5


def test_sot_rank_ignores_none():
    assert _sot_rank(1, None, 2) == 2


# ─── Endpoint integration tests (mocked DB session) ───────────────────────────


class _FakeUndefinedTable(Exception):
    sqlstate = "42P01"


def _missing_table_error(name: str) -> ProgrammingError:
    return ProgrammingError(
        "SELECT ...", params={}, orig=_FakeUndefinedTable(f'relation "{name}"')
    )


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


def _make_session(responses):
    """Build a mock session that returns each entry in `responses` in order.

    Each entry is either a list[dict] (rows) or an Exception (raised).
    """
    idx = {"i": 0}
    session = MagicMock()

    async def fake_execute(stmt, params=None):
        i = idx["i"]
        idx["i"] += 1
        if i >= len(responses):
            rows = []
        else:
            resp = responses[i]
            if isinstance(resp, Exception):
                raise resp
            rows = resp

        result = MagicMock()
        mappings = MagicMock()
        mappings.all = MagicMock(return_value=rows)
        mappings.first = MagicMock(return_value=rows[0] if rows else None)
        result.mappings = MagicMock(return_value=mappings)
        return result

    session.execute = AsyncMock(side_effect=fake_execute)
    return session


# ── /snapshot ────────────────────────────────────────────────────────────────


SNAPSHOT_TOP_KEYS = {"balance", "unredeemed", "redeemer_health", "deltas", "_meta"}


def test_snapshot_shape_all_tables_present():
    now = datetime.now(timezone.utc)
    # Query order (see wallet.py::get_wallet_snapshot):
    #  1. system_state           → cash_usdc
    #  2. poly_pending_wins      → pending totals
    #  3. redeemer_state         → cooldown + quota
    #  4. trades (wins_missed)   → 24h manual sweeper count
    #  5. trades (delta_24h)     → 24h pnl sum
    session = _make_session(
        [
            [{"state": {"wallet_balance_usdc": 65.10, "paper_mode": False}}],
            [
                {"value": 5.03, "overdue_seconds": 400},
                {"value": 2.10, "overdue_seconds": 30},
            ],
            [
                {
                    "cooldown_active": False,
                    "cooldown_remaining_seconds": 0,
                    "cooldown_resets_at": None,
                    "cooldown_reason": None,
                    "daily_quota_limit": 100,
                    "quota_used_today": 4,
                    "observed_at": now - timedelta(minutes=3),
                }
            ],
            [{"n": 1}],          # wins_missed
            [{"pnl": 12.34}],    # delta_24h
        ]
    )
    client = TestClient(_build_app(session))
    res = client.get("/api/wallet/snapshot")
    assert res.status_code == 200, res.text
    body = res.json()

    assert SNAPSHOT_TOP_KEYS.issubset(body.keys())
    assert body["balance"]["cash_usdc_onchain"] == 65.10
    assert body["balance"]["pending_wins_value"] == 7.13
    assert body["balance"]["effective_total"] == 72.23
    assert body["unredeemed"]["redeemable_now"] == 2
    assert body["unredeemed"]["overdue_5min"] == 1
    assert body["redeemer_health"]["wins_missed_by_engine_24h"] == 1
    assert body["redeemer_health"]["quota_used_today"] == 4
    assert body["redeemer_health"]["quota_limit"] == 100
    assert body["deltas"]["vs_24h_ago"] == 12.34

    # sot_rank: sub-queries include rank 4 (wins_missed from trades) + rank 5
    # (everything else). Worst = 5.
    assert body["_meta"]["sot_rank"] == 5
    assert body["_meta"]["sources_used"] == ["db"]
    assert body["_meta"]["data_stale"] is False


def test_snapshot_handles_missing_pending_wins_table():
    now = datetime.now(timezone.utc)
    session = _make_session(
        [
            [{"state": {"wallet_balance_usdc": 65.10}}],
            _missing_table_error("poly_pending_wins"),
            [
                {
                    "cooldown_active": False,
                    "cooldown_remaining_seconds": 0,
                    "cooldown_resets_at": None,
                    "cooldown_reason": None,
                    "daily_quota_limit": 100,
                    "quota_used_today": 0,
                    "observed_at": now,
                }
            ],
            [{"n": 0}],
            [{"pnl": 0.0}],
        ]
    )
    client = TestClient(_build_app(session))
    res = client.get("/api/wallet/snapshot")
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["balance"]["cash_usdc_onchain"] == 65.10
    assert body["balance"]["pending_wins_value"] == 0.0
    assert body["unredeemed"]["redeemable_now"] == 0
    assert body["_meta"]["data_stale"] is True
    assert "poly_pending_wins" in body["_meta"]["missing_tables"]


# ── /pending ─────────────────────────────────────────────────────────────────


def test_pending_shape_with_rows():
    now = datetime.now(timezone.utc)
    session = _make_session(
        [
            # redeemer_state (for classifier inputs)
            [
                {
                    "cooldown_active": True,
                    "daily_quota_limit": 100,
                    "quota_used_today": 5,
                }
            ],
            # rows
            [
                {
                    "condition_id": "0xea72",
                    "title": "btc-up-or-down-apr-20",
                    "side": "Up",
                    "size": 5.03,
                    "cost_usd": 3.60,
                    "current_value_usd": 5.03,
                    "window_end_utc": now - timedelta(hours=1),
                    "overdue_seconds": 3600,
                    "strategy": "v4_fusion",
                    "metadata": {"redeemable": True, "payout_denominator": 1},
                }
            ],
        ]
    )
    client = TestClient(_build_app(session))
    res = client.get("/api/wallet/pending")
    assert res.status_code == 200, res.text
    body = res.json()

    assert "rows" in body
    assert "stuck_reason_legend" in body
    assert body["_meta"]["sot_rank"] == 5

    row = body["rows"][0]
    assert row["condition_id"] == "0xea72"
    assert row["strategy"] == "v4_fusion"
    # Cooldown active → classifier returns engine_cooldown
    assert row["stuck_reason"] == "engine_cooldown"
    assert row["overdue_seconds"] == 3600


def test_pending_missing_redeemer_state_still_returns_rows():
    now = datetime.now(timezone.utc)
    session = _make_session(
        [
            _missing_table_error("redeemer_state"),
            [
                {
                    "condition_id": "0xabc",
                    "title": "btc-market",
                    "side": "Down",
                    "size": 1.0,
                    "cost_usd": 0.5,
                    "current_value_usd": 1.0,
                    "window_end_utc": now,
                    "overdue_seconds": 10,
                    "strategy": "v6_sniper",
                    "metadata": {"redeemable": True, "payout_denominator": 1},
                }
            ],
        ]
    )
    client = TestClient(_build_app(session))
    res = client.get("/api/wallet/pending")
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["rows"]) == 1
    assert "redeemer_state" in body["_meta"]["missing_tables"]


# ── /history ─────────────────────────────────────────────────────────────────


def test_history_shape_and_passthrough():
    now = datetime.now(timezone.utc)
    session = _make_session(
        [
            [
                {
                    "id": 101,
                    "condition_id": "0x9c7a",
                    "resolved_at": now - timedelta(hours=2),
                    "title": "btc-market",
                    "side": "Up",
                    "cost_usd": 4.36,
                    "payout_usd": 4.96,
                    "pnl_usd": 0.60,
                    "transport": "onchain_matic",
                    "initiator": "manual_billy",
                    "strategy": "v4_fusion",
                    "metadata": {"redeem_tx": "0xdead", "gas_matic": 0.005},
                },
                {
                    "id": 102,
                    "condition_id": "0xabc",
                    "resolved_at": now - timedelta(hours=5),
                    "title": "btc-market",
                    "side": "Down",
                    "cost_usd": 4.0,
                    "payout_usd": 0.0,
                    "pnl_usd": -4.0,
                    "transport": None,   # pre-migration row
                    "initiator": None,
                    "strategy": "v6_sniper",
                    "metadata": {},
                },
            ]
        ]
    )
    client = TestClient(_build_app(session))
    res = client.get("/api/wallet/history?limit=300")
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["rows"]) == 2

    r0 = body["rows"][0]
    assert r0["transport"] == "onchain_matic"
    assert r0["initiator"] == "manual_billy"
    assert r0["redeem_tx"] == "0xdead"
    assert r0["gas_matic"] == 0.005
    assert r0["pnl_usd"] == 0.60

    # Pre-migration row renders NULL for new columns (FE handles "—")
    r1 = body["rows"][1]
    assert r1["transport"] is None
    assert r1["initiator"] is None

    assert body["_meta"]["sot_rank"] == 5
    assert body["_meta"]["limit"] == 300


def test_history_empty():
    session = _make_session([[]])
    client = TestClient(_build_app(session))
    res = client.get("/api/wallet/history")
    assert res.status_code == 200
    assert res.json()["rows"] == []
