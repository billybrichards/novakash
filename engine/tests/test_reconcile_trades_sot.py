"""
Tests for engine/reconciliation/reconciler.py::CLOBReconciler.reconcile_trades_sot
— POLY-SOT-b + POLY-SOT-d (poly_fills join model).

The reconciler now uses a DB LEFT JOIN against `poly_fills` instead of
calling the Polymarket API per row.  Decision matrix mirrors
``reconcile_manual_trades_sot`` — see test_reconcile_manual_trades_sot.py
for the full explanation.

The POLY-SOT-c backfill tests at the bottom exercise ``_decide_for_row``
from ``scripts/backfill_sot_reconciliation.py`` directly (different code
path, kept separate).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from execution.polymarket_client import PolyOrderStatus
from reconciliation import reconciler as reconciler_mod
from reconciliation.reconciler import CLOBReconciler, ReconciliationSummary


# ─── Stubs ──────────────────────────────────────────────────────────────────


def _trade_dict(
    trade_id: int = 1001,
    status: str = "FILLED",
    entry_price: float = 0.55,
    fill_price: Optional[float] = None,
    fill_size: Optional[float] = None,
    stake_usd: float = 4.0,
    mode: str = "live",
    execution_mode: str = "live",
    order_id: str = "0xorder1",
    polymarket_order_id: Optional[str] = "0xclob_auto_1",
    created_at: Optional[datetime] = None,
    sot_reconciliation_state: Optional[str] = None,
) -> dict:
    """Build a minimal trades row dict (automatic trades table)."""
    if created_at is None:
        created_at = datetime.now(timezone.utc) - timedelta(minutes=15)
    return {
        "trade_id": trade_id,
        "polymarket_order_id": polymarket_order_id,
        "order_id": order_id,
        "status": status,
        "direction": "YES",
        "entry_price": entry_price,
        "fill_price": fill_price,
        "fill_size": fill_size,
        "stake_usd": stake_usd,
        "mode": mode,
        "execution_mode": execution_mode,
        "is_live": True,
        "created_at": created_at,
        "polymarket_confirmed_status": None,
        "polymarket_confirmed_fill_price": None,
        "polymarket_confirmed_size": None,
        "polymarket_confirmed_at": None,
        "polymarket_last_verified_at": None,
        "sot_reconciliation_state": sot_reconciliation_state,
        "sot_reconciliation_notes": None,
    }


def _fill_dict(
    poly_price: float = 0.5510,
    poly_size: float = 7.27,
    transaction_hash: str = "0xdeadbeef",
    match_time_utc: Optional[datetime] = None,
) -> dict:
    """Build a minimal poly_fills row dict."""
    if match_time_utc is None:
        match_time_utc = datetime.now(timezone.utc)
    return {
        "poly_price": poly_price,
        "poly_size": poly_size,
        "transaction_hash": transaction_hash,
        "match_time_utc": match_time_utc,
    }


class StubTradesPoolDBClient:
    """Test stand-in for `_TradesPoolDBClient`."""

    def __init__(self, joined_rows: list[tuple[dict, Optional[dict]]]) -> None:
        self._rows = joined_rows
        self.updates: list[dict] = []

    async def fetch_trades_joined_poly_fills(
        self,
        since: Optional[datetime] = None,
        limit: int = 200,
    ) -> list[tuple[dict, Optional[dict]]]:
        return list(self._rows[:limit])

    async def fetch_trades_for_sot_check(
        self,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[dict]:
        return [t for t, _ in self._rows[:limit]]

    async def update_trade_sot(
        self,
        trade_id,
        *,
        polymarket_confirmed_status,
        polymarket_confirmed_fill_price,
        polymarket_confirmed_size,
        polymarket_confirmed_at,
        sot_reconciliation_state,
        sot_reconciliation_notes,
        polymarket_tx_hash: Optional[str] = None,
    ) -> None:
        self.updates.append(
            {
                "trade_id": trade_id,
                "polymarket_confirmed_status": polymarket_confirmed_status,
                "polymarket_confirmed_fill_price": polymarket_confirmed_fill_price,
                "polymarket_confirmed_size": polymarket_confirmed_size,
                "polymarket_confirmed_at": polymarket_confirmed_at,
                "sot_reconciliation_state": sot_reconciliation_state,
                "sot_reconciliation_notes": sot_reconciliation_notes,
                "polymarket_tx_hash": polymarket_tx_hash,
            }
        )


class StubPolymarketClient:
    """Stub for backfill tests that still call get_order_status_sot."""

    def __init__(self, orders: dict[str, Optional[PolyOrderStatus]]) -> None:
        self._orders = orders
        self.calls: list[str] = []

    async def get_order_status_sot(self, order_id: str) -> Optional[PolyOrderStatus]:
        self.calls.append(order_id)
        return self._orders.get(order_id)

    async def list_recent_orders(self, since=None, limit=50):
        return [o for o in self._orders.values() if o is not None]


class StubAlerter:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_raw_message(self, text: str) -> None:
        self.messages.append(text)


def _make_reconciler(
    monkeypatch,
    joined_rows: list[tuple[dict, Optional[dict]]],
) -> tuple[CLOBReconciler, StubTradesPoolDBClient, StubAlerter]:
    db_stub = StubTradesPoolDBClient(joined_rows)
    alerter_stub = StubAlerter()

    monkeypatch.setattr(
        reconciler_mod,
        "_TradesPoolDBClient",
        lambda pool: db_stub,
    )

    rec = CLOBReconciler(
        poly_client=None,
        db_pool=object(),
        alerter=alerter_stub,
        shutdown_event=asyncio.Event(),
    )
    return rec, db_stub, alerter_stub


# ─── Case 1: AGREES — fill present, price within tolerance ──────────────────


@pytest.mark.asyncio
async def test_agrees_engine_executed_polymarket_filled_matching(monkeypatch):
    """Engine fill at 0.55, poly_fills row at 0.5510 (0.18% → within 0.5% tolerance)."""
    trade = _trade_dict(trade_id=1001, entry_price=0.5500, fill_price=0.5500, fill_size=7.27)
    fill = _fill_dict(poly_price=0.5510, poly_size=7.27)
    rec, db, alerter = _make_reconciler(monkeypatch, [(trade, fill)])

    summary = await rec.reconcile_trades_sot()

    assert summary.checked == 1
    assert summary.agrees == 1
    assert summary.engine_optimistic == 0
    assert summary.diverged == 0
    assert summary.alerts_fired == 0
    assert len(alerter.messages) == 0
    assert len(db.updates) == 1
    upd = db.updates[0]
    assert upd["trade_id"] == 1001
    assert upd["sot_reconciliation_state"] == "agrees"
    assert upd["polymarket_confirmed_status"] == "matched"
    assert upd["polymarket_confirmed_fill_price"] == pytest.approx(0.5510)


# ─── Case 2: ENGINE_OPTIMISTIC — no fill, trade is old ──────────────────────


@pytest.mark.asyncio
async def test_engine_optimistic_engine_executed_polymarket_no_record(monkeypatch):
    """No poly_fills row for a 15-min-old live trade → engine_optimistic + alert."""
    trade = _trade_dict(
        trade_id=1002,
        status="FILLED",
        entry_price=0.62,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=15),
    )
    rec, db, alerter = _make_reconciler(monkeypatch, [(trade, None)])

    summary = await rec.reconcile_trades_sot()

    assert summary.checked == 1
    assert summary.engine_optimistic == 1
    assert summary.agrees == 0
    assert summary.alerts_fired == 1
    assert len(alerter.messages) == 1
    msg = alerter.messages[0]
    assert "engine_optimistic" in msg.lower() or "engine" in msg.lower()
    assert "AUTO" in msg
    assert "#1002" in msg
    assert len(db.updates) == 1
    assert db.updates[0]["sot_reconciliation_state"] == "engine_optimistic"


# ─── Case 3: DIVERGED — fill present but price exceeds tolerance ─────────────


@pytest.mark.asyncio
async def test_diverged_fill_price_mismatch_beyond_tolerance(monkeypatch):
    """Engine fill at 0.55, poly_fills at 0.5610 (2% diff → exceeds 0.5%)."""
    trade = _trade_dict(trade_id=1003, entry_price=0.5500, fill_price=0.5500, fill_size=7.13)
    fill = _fill_dict(poly_price=0.5610, poly_size=7.13)
    rec, db, alerter = _make_reconciler(monkeypatch, [(trade, fill)])

    summary = await rec.reconcile_trades_sot()

    assert summary.checked == 1
    assert summary.diverged == 1
    assert summary.agrees == 0
    assert summary.alerts_fired == 1
    assert len(alerter.messages) == 1
    assert len(db.updates) == 1
    assert db.updates[0]["sot_reconciliation_state"] == "diverged"
    notes = db.updates[0]["sot_reconciliation_notes"] or ""
    assert "engine fill" in notes.lower() and "polymarket" in notes.lower()


# ─── Case 4: UNRECONCILED — no fill, trade is fresh (<10 min) ───────────────


@pytest.mark.asyncio
async def test_unreconciled_polymarket_order_pending(monkeypatch):
    """No poly_fills row yet, trade is only 3 min old → unreconciled, no alert."""
    trade = _trade_dict(
        trade_id=1004,
        status="FILLED",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=3),
    )
    rec, db, alerter = _make_reconciler(monkeypatch, [(trade, None)])

    summary = await rec.reconcile_trades_sot()

    assert summary.checked == 1
    assert summary.unreconciled == 1
    assert summary.engine_optimistic == 0
    assert summary.alerts_fired == 0
    assert len(alerter.messages) == 0
    assert len(db.updates) == 1
    assert db.updates[0]["sot_reconciliation_state"] == "unreconciled"


# ─── Case 5: fill present when engine marked failed ──────────────────────────


@pytest.mark.asyncio
async def test_polymarket_only_engine_failed_but_poly_filled(monkeypatch):
    """fill_row present when engine has failed status — price match = agrees."""
    trade = _trade_dict(trade_id=1005, status="failed_no_token", entry_price=0.50)
    fill = _fill_dict(poly_price=0.50, poly_size=8.0)
    rec, db, alerter = _make_reconciler(monkeypatch, [(trade, fill)])

    summary = await rec.reconcile_trades_sot()

    assert summary.checked == 1
    # poly_fills model: fill present → check prices. matching prices → agrees.
    assert (summary.agrees + summary.diverged + summary.polymarket_only) == 1


# ─── Case 6a: no fill + old → engine_optimistic ──────────────────────────────


@pytest.mark.asyncio
async def test_no_order_id_old_row_marked_engine_optimistic(monkeypatch):
    """Live trade older than 10min with no poly_fills row → engine_optimistic."""
    trade = _trade_dict(
        trade_id=1006,
        status="FILLED",
        polymarket_order_id="0xold_order",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=20),
    )
    rec, db, alerter = _make_reconciler(monkeypatch, [(trade, None)])

    summary = await rec.reconcile_trades_sot()

    assert summary.checked == 1
    assert summary.engine_optimistic == 1
    assert summary.alerts_fired == 1
    assert len(db.updates) == 1
    assert db.updates[0]["sot_reconciliation_state"] == "engine_optimistic"


# ─── Case 6b: no fill + recent → unreconciled ────────────────────────────────


@pytest.mark.asyncio
async def test_no_order_id_recent_row_marked_unreconciled(monkeypatch):
    """Live trade younger than 10min with no poly_fills row → unreconciled."""
    trade = _trade_dict(
        trade_id=1007,
        status="FILLED",
        created_at=datetime.now(timezone.utc) - timedelta(seconds=45),
    )
    rec, db, alerter = _make_reconciler(monkeypatch, [(trade, None)])

    summary = await rec.reconcile_trades_sot()

    assert summary.checked == 1
    assert summary.unreconciled == 1
    assert summary.alerts_fired == 0
    assert len(db.updates) == 1
    assert db.updates[0]["sot_reconciliation_state"] == "unreconciled"


# ─── Case 7: Alert dedupe — same trade_id only alerts once ──────────────────


@pytest.mark.asyncio
async def test_alert_dedupe_same_trade_id_only_alerts_once(monkeypatch):
    """Calling reconcile twice on the same engine_optimistic trade fires one alert."""
    trade = _trade_dict(
        trade_id=1008,
        status="FILLED",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=15),
    )
    rec, db, alerter = _make_reconciler(monkeypatch, [(trade, None)])

    s1 = await rec.reconcile_trades_sot()
    db._rows = [(trade, None)]  # re-inject for second pass
    s2 = await rec.reconcile_trades_sot()

    assert s1.engine_optimistic == 1
    assert s2.engine_optimistic == 1
    assert s1.alerts_fired == 1
    assert s2.alerts_fired == 0
    assert len(alerter.messages) == 1


# ─── Case 8: PAPER — execution_mode=paper → paper terminal state ─────────────


@pytest.mark.asyncio
async def test_engine_failed_polymarket_no_record_marks_agrees(monkeypatch):
    """Paper-mode trade (execution_mode='paper') resolves immediately to 'paper'."""
    trade = _trade_dict(
        trade_id=1009,
        status="FILLED",
        mode="paper",
        execution_mode="paper",
        polymarket_order_id="5min-paper-aaaa",
    )
    rec, db, alerter = _make_reconciler(monkeypatch, [(trade, None)])

    summary = await rec.reconcile_trades_sot()

    assert summary.checked == 1
    assert summary.paper == 1
    assert summary.alerts_fired == 0
    assert db.updates[0]["sot_reconciliation_state"] == "paper"


# ─── Case 9: PAPER MODE synthetic ID ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_paper_mode_synthetic_id_resolves_to_filled(monkeypatch):
    """A trade with mode='paper' hits the paper terminal state, not engine_optimistic."""
    trade = _trade_dict(
        trade_id=1010,
        status="OPEN",
        mode="paper",
        execution_mode="paper",
        order_id="manual-paper-jjjjjjjjjjjj",
        polymarket_order_id="manual-paper-jjjjjjjjjjjj",
    )
    rec, db, alerter = _make_reconciler(monkeypatch, [(trade, None)])

    summary = await rec.reconcile_trades_sot()

    assert summary.checked == 1
    assert summary.paper == 1
    assert summary.engine_optimistic == 0
    assert summary.alerts_fired == 0


# ─── Case 10: Empty DB — no rows, no work ───────────────────────────────────


@pytest.mark.asyncio
async def test_empty_db_no_rows_returns_empty_summary(monkeypatch):
    """No trades rows → empty summary, no writes, no alerts."""
    rec, db, alerter = _make_reconciler(monkeypatch, [])

    summary = await rec.reconcile_trades_sot()

    assert summary.checked == 0
    assert summary.agrees == 0
    assert summary.alerts_fired == 0
    assert len(db.updates) == 0
    assert len(alerter.messages) == 0


# ─── Case 11: Compare error is counted and does not crash ───────────────────


@pytest.mark.asyncio
async def test_polymarket_fetch_error_does_not_change_state(monkeypatch):
    """A bad fill row (non-dict) triggers an error that is counted, not raised."""
    trade = _trade_dict(trade_id=1011, status="FILLED")
    bad_fill = "not_a_dict"  # fill.get(...) raises AttributeError

    rec, db, alerter = _make_reconciler(monkeypatch, [(trade, bad_fill)])  # type: ignore[arg-type]

    summary = await rec.reconcile_trades_sot()

    assert summary.checked == 1
    assert summary.errors == 1
    assert summary.engine_optimistic == 0
    assert len(db.updates) == 0


# ─── POLY-SOT-c backfill tests ──────────────────────────────────────────────
#
# These exercise the _decide_for_row helper in the backfill script,
# which still uses the old per-order Polymarket API (historical backfill).
# Kept separate from the forward reconciler tests above.


@pytest.mark.asyncio
async def test_backfill_manual_trades_tags_all_null_rows(monkeypatch):
    """The backfill walks NULL-state rows and tags them via the same logic
    as the forward reconciler (but using the old API-based path).
    """
    from scripts.backfill_sot_reconciliation import _decide_for_row

    poly = StubPolymarketClient(
        {
            "0x_old_filled": PolyOrderStatus(
                order_id="0x_old_filled",
                status="matched",
                fill_price=0.55,
                fill_size=7.27,
                timestamp=datetime.now(timezone.utc) - timedelta(hours=4),
            ),
            "0x_old_optimistic": None,
        }
    )
    rec = CLOBReconciler(
        poly_client=poly,
        db_pool=None,
        alerter=StubAlerter(),
        shutdown_event=asyncio.Event(),
    )

    row1 = {
        "trade_id": "manual_z1",
        "polymarket_order_id": "0x_old_filled",
        "status": "executed",
        "entry_price": 0.55,
        "stake_usd": 4.0,
        "created_at": datetime.now(timezone.utc) - timedelta(hours=4),
    }
    state1, _ = await _decide_for_row(
        row1, poly, rec, rate_limit_ms=0, table="manual_trades"
    )
    assert state1 == "agrees"

    row2 = {
        "trade_id": "manual_z2",
        "polymarket_order_id": "0x_old_optimistic",
        "status": "executed",
        "entry_price": 0.62,
        "stake_usd": 5.0,
        "created_at": datetime.now(timezone.utc) - timedelta(hours=5),
    }
    state2, _ = await _decide_for_row(
        row2, poly, rec, rate_limit_ms=0, table="manual_trades"
    )
    assert state2 == "engine_optimistic"


@pytest.mark.asyncio
async def test_backfill_dry_run_no_writes(monkeypatch):
    """Dry-run mode: _decide_for_row returns a decision but does NOT update DB."""
    from scripts.backfill_sot_reconciliation import _decide_for_row

    poly = StubPolymarketClient(
        {
            "0x_dry_filled": PolyOrderStatus(
                order_id="0x_dry_filled",
                status="filled",
                fill_price=0.55,
                fill_size=7.27,
                timestamp=datetime.now(timezone.utc),
            )
        }
    )
    rec = CLOBReconciler(
        poly_client=poly,
        db_pool=None,
        alerter=StubAlerter(),
        shutdown_event=asyncio.Event(),
    )

    db = StubTradesPoolDBClient([])

    row = {
        "trade_id": "manual_dry_1",
        "polymarket_order_id": "0x_dry_filled",
        "status": "executed",
        "entry_price": 0.55,
        "stake_usd": 4.0,
        "created_at": datetime.now(timezone.utc) - timedelta(hours=2),
    }
    state, _decision = await _decide_for_row(
        row, poly, rec, rate_limit_ms=0, table="manual_trades"
    )
    assert state == "agrees"
    assert len(db.updates) == 0


@pytest.mark.asyncio
async def test_backfill_no_order_id_old_row_tags_no_order_id(monkeypatch):
    """A row with no order ID older than 24h gets tagged 'no_order_id'.
    Recent no-order-id rows are skipped.
    """
    from scripts.backfill_sot_reconciliation import _decide_for_row

    poly = StubPolymarketClient({})
    rec = CLOBReconciler(
        poly_client=poly,
        db_pool=None,
        alerter=StubAlerter(),
        shutdown_event=asyncio.Event(),
    )

    old_row = {
        "trade_id": "manual_old",
        "polymarket_order_id": None,
        "status": "executed",
        "entry_price": 0.55,
        "stake_usd": 4.0,
        "created_at": datetime.now(timezone.utc) - timedelta(hours=48),
    }
    state, decision = await _decide_for_row(
        old_row, poly, rec, rate_limit_ms=0, table="manual_trades"
    )
    assert state == "no_order_id"
    assert "no order ID persisted" in (decision.get("notes") or "")

    recent_row = {
        "trade_id": "manual_recent",
        "polymarket_order_id": None,
        "status": "executed",
        "entry_price": 0.55,
        "stake_usd": 4.0,
        "created_at": datetime.now(timezone.utc) - timedelta(hours=2),
    }
    state2, _ = await _decide_for_row(
        recent_row, poly, rec, rate_limit_ms=0, table="manual_trades"
    )
    assert state2 == "skipped"
