"""Regression tests for /api/v58/strategy-windows endpoint enrichment.

Complements PR #296 (FE Window Results redesign). The endpoint was already
returning mode / action / direction / skip_reason / entry_reason /
confidence_score / eval_offset / outcome. This change adds the enrichment
fields the FE redesign expects:

  - fill_price / fill_size / entry_cap / executed / order_id
    (raw columns already on strategy_decisions)
  - metadata  — full metadata_json blob (for conviction_bucket, path1_age_s,
    gate_results, probability_*, lgb, path1, …)
  - pnl_usd / trade_outcome
    (LEFT JOIN on trades — null for GHOST strategies that never fill)

Auth + session are stubbed via FastAPI dependency_overrides, same pattern
as tests/test_strategy_decisions_resolved.py.
"""

from __future__ import annotations

from typing import Any, List, Optional
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v58_monitor import router
from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _row(
    *,
    window_ts: int = 1_776_399_900,
    asset: str = "BTC",
    strategy_id: Optional[str] = "v6_sniper",
    mode: str = "LIVE",
    action: Optional[str] = "TRADE",
    strategy_direction: Optional[str] = "UP",
    actual_direction: Optional[str] = "UP",
    skip_reason: Optional[str] = None,
    entry_reason: Optional[str] = "polymarket_trade",
    confidence_score: Optional[float] = 0.82,
    eval_offset: Optional[int] = 120,
    fill_price: Optional[float] = 0.73,
    fill_size: Optional[float] = 5.0,
    entry_cap: Optional[float] = 0.75,
    executed: Optional[bool] = True,
    order_id: Optional[str] = "0xabc",
    metadata_json: Any = None,
    trade_pnl_usd: Optional[float] = None,
    trade_outcome: Optional[str] = None,
) -> dict:
    """Build a single row that mirrors the SELECT projection of
    strategy_windows. metadata_json is a dict in real asyncpg rows;
    tests pass whatever the exercise expects."""
    return {
        "window_ts": window_ts,
        "asset": asset,
        "open_price": 65_000.0,
        "close_price": 65_100.0,
        "actual_direction": actual_direction,
        "vpin": 0.55,
        "regime": "NORMAL",
        "delta_source": "chainlink",
        "strategy_id": strategy_id,
        "mode": mode,
        "action": action,
        "strategy_direction": strategy_direction,
        "skip_reason": skip_reason,
        "entry_reason": entry_reason,
        "confidence_score": confidence_score,
        "eval_offset": eval_offset,
        "fill_price": fill_price,
        "fill_size": fill_size,
        "entry_cap": entry_cap,
        "executed": executed,
        "order_id": order_id,
        "metadata_json": metadata_json,
        "trade_pnl_usd": trade_pnl_usd,
        "trade_outcome": trade_outcome,
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


def _make_session(rows: List[dict]) -> MagicMock:
    session = MagicMock()

    async def fake_execute(stmt, params=None):
        result = MagicMock()
        result.mappings.return_value.all.return_value = rows
        return result

    session.execute = fake_execute
    return session


# ─── Tests ───────────────────────────────────────────────────────────────────


def test_response_includes_enrichment_fields_for_filled_trade():
    """A TRADE decision with matching trades row must surface
    fill_price / fill_size / entry_cap / executed / order_id,
    the raw metadata blob (dict — not string), and the trades pnl_usd
    + outcome from the LEFT JOIN."""
    metadata = {
        "conviction_bucket": "agree_strong",
        "path1_age_s": 4.7,
        "path1_age_source": "inferred_at",
        "probability_raw": 0.811,
        "probability_calibrated": 0.762,
        "read_probability_source": "path1",
        "lgb": 0.78,
        "path1": 0.81,
        "gate_results": [
            {"gate": "macro_risk_off", "passed": True, "reason": None},
            {"gate": "path1_freshness", "passed": True, "reason": "age=4.7s"},
        ],
    }
    rows = [
        _row(
            metadata_json=metadata,
            trade_pnl_usd=2.5,
            trade_outcome="WIN",
        )
    ]

    client = TestClient(_build_app(_make_session(rows)))
    res = client.get("/api/v58/strategy-windows?asset=BTC")
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["count"] == 1
    window = body["windows"][0]
    strat = window["strategies"]["v6_sniper"]

    # Existing fields preserved — backward compat.
    assert strat["mode"] == "LIVE"
    assert strat["action"] == "TRADE"
    assert strat["direction"] == "UP"
    assert strat["outcome"] == "WIN"  # derived shadow outcome (UP matches UP)

    # ── New enrichment fields ──
    assert strat["fill_price"] == 0.73
    assert strat["fill_size"] == 5.0
    assert strat["entry_cap"] == 0.75
    assert strat["executed"] is True
    assert strat["order_id"] == "0xabc"

    # metadata returned as a native dict (JSON-safe), not a string
    assert isinstance(strat["metadata"], dict)
    assert strat["metadata"]["conviction_bucket"] == "agree_strong"
    assert strat["metadata"]["path1_age_s"] == 4.7
    assert strat["metadata"]["path1_age_source"] == "inferred_at"
    assert strat["metadata"]["probability_calibrated"] == 0.762
    assert strat["metadata"]["lgb"] == 0.78
    assert strat["metadata"]["path1"] == 0.81
    # gate_results is a list of dicts (not flattened/stringified)
    gates = strat["metadata"]["gate_results"]
    assert isinstance(gates, list)
    assert gates[0]["gate"] == "macro_risk_off"
    assert gates[0]["passed"] is True

    # Authoritative trade values from LEFT JOIN
    assert strat["pnl_usd"] == 2.5
    assert strat["trade_outcome"] == "WIN"


def test_ghost_strategy_has_null_trade_fields():
    """Shadow / GHOST strategies never produce a trades row. Their
    pnl_usd + trade_outcome must be null — the FE should fall back
    to the derived `outcome` field in that case."""
    rows = [
        _row(
            strategy_id="v10_gate",
            mode="GHOST",
            action="TRADE",
            strategy_direction="DOWN",
            actual_direction="UP",
            fill_price=None,
            fill_size=None,
            executed=False,
            order_id=None,
            metadata_json={"conviction_bucket": "pegged_path1"},
            # No matching trades row
            trade_pnl_usd=None,
            trade_outcome=None,
        )
    ]

    client = TestClient(_build_app(_make_session(rows)))
    body = client.get("/api/v58/strategy-windows").json()
    strat = body["windows"][0]["strategies"]["v10_gate"]

    assert strat["fill_price"] is None
    assert strat["fill_size"] is None
    assert strat["order_id"] is None
    assert strat["executed"] is False
    assert strat["pnl_usd"] is None
    assert strat["trade_outcome"] is None
    # Derived shadow outcome still present (direction vs actual)
    assert strat["outcome"] == "LOSS"
    # metadata still surfaced even for ghost rows
    assert strat["metadata"]["conviction_bucket"] == "pegged_path1"


def test_metadata_string_is_coerced_to_dict():
    """Some older rows or alternate DB adapters return metadata_json as
    a JSON-encoded string. The endpoint must coerce it to a dict so the
    FE can always do `decision.metadata.conviction_bucket`."""
    rows = [
        _row(
            metadata_json='{"conviction_bucket": "agree_strong", "gate_results": []}'
        )
    ]
    client = TestClient(_build_app(_make_session(rows)))
    body = client.get("/api/v58/strategy-windows").json()
    strat = body["windows"][0]["strategies"]["v6_sniper"]

    assert isinstance(strat["metadata"], dict)
    assert strat["metadata"]["conviction_bucket"] == "agree_strong"
    assert strat["metadata"]["gate_results"] == []


def test_malformed_metadata_becomes_none():
    """Defensive: if metadata_json is an unparseable string, surface it
    as null rather than 500-ing or leaking the raw string."""
    rows = [_row(metadata_json="this is not json")]
    client = TestClient(_build_app(_make_session(rows)))
    body = client.get("/api/v58/strategy-windows").json()
    strat = body["windows"][0]["strategies"]["v6_sniper"]
    assert strat["metadata"] is None


def test_backward_compat_existing_fields_unchanged():
    """The existing response shape (mode/action/direction/skip_reason/
    entry_reason/confidence_score/eval_offset/outcome plus top-level
    windows/count/known_strategies) must be preserved. Only ADD fields."""
    rows = [_row(metadata_json={})]
    client = TestClient(_build_app(_make_session(rows)))
    body = client.get("/api/v58/strategy-windows").json()

    # Top-level shape preserved
    assert set(body.keys()) >= {"windows", "count", "known_strategies"}

    w = body["windows"][0]
    # Window-level fields preserved
    for key in (
        "window_ts", "asset", "open_price", "close_price",
        "actual_direction", "vpin", "regime", "delta_source", "strategies",
    ):
        assert key in w

    strat = w["strategies"]["v6_sniper"]
    # Original strategy-level fields preserved
    for key in (
        "mode", "action", "direction", "skip_reason", "entry_reason",
        "confidence_score", "eval_offset", "outcome",
    ):
        assert key in strat
