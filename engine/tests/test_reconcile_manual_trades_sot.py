"""
Tests for engine/reconciliation/reconciler.py::CLOBReconciler.reconcile_manual_trades_sot
— POLY-SOT.

These tests enforce the source-of-truth invariants for manual_trades rows:

  1. AGREES — engine says executed AND Polymarket has the same order in a
     terminal state with matching fill_price (within 0.5% tolerance).
     Result: state='agrees', no alert.

  2. ENGINE_OPTIMISTIC — engine says executed but Polymarket has no record
     (place_order timed out, retried, or hit auth issue). Result:
     state='engine_optimistic', loud Telegram alert.

  3. DIVERGED — engine and Polymarket both have the order but fill_price
     differs by more than the 0.5% tolerance. Result: state='diverged',
     alert.

  4. UNRECONCILED — Polymarket has the order but it's not yet terminal
     (still pending). Result: state='unreconciled', no alert.

  5. POLYMARKET_ONLY — engine says failed but Polymarket has a filled
     order. Should never happen but caught defensively. Alert.

  6. NO ORDER ID — engine row has no polymarket_order_id and was created
     more than 2 minutes ago: state='engine_optimistic' + alert. Younger
     rows get state='unreconciled', no alert.

  7. PAPER MODE SYNTHETIC IDS — `manual-paper-*` order IDs are recognised
     by the polymarket client and resolve to a synthetic filled status
     so paper-mode trades exercise the same code path.

The tests use a stub `PolymarketClient` and a stub `_PoolDBClient` so the
reconciler runs without any real CLOB or PostgreSQL. The DB stub records
every update so we can assert state transitions deterministically.

Each case follows the same shape as test_eval_offset_bounds_gate.py and
test_source_agreement_spot_only.py — fresh stubs per case, no shared state.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import AsyncMock

import pytest

from execution.polymarket_client import PolyOrderStatus
from reconciliation import reconciler as reconciler_mod
from reconciliation.reconciler import CLOBReconciler, ReconciliationSummary


# ─── Stubs ──────────────────────────────────────────────────────────────────


@dataclass
class FakeRow:
    """A single manual_trades row as the SOT reconciler sees it."""
    trade_id: str
    polymarket_order_id: Optional[str]
    status: str
    direction: str = "UP"
    entry_price: Optional[float] = 0.55
    stake_usd: Optional[float] = 4.0
    mode: str = "live"
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc) - timedelta(minutes=5)
    )
    polymarket_confirmed_status: Optional[str] = None
    polymarket_confirmed_fill_price: Optional[float] = None
    polymarket_confirmed_size: Optional[float] = None
    polymarket_confirmed_at: Optional[datetime] = None
    polymarket_last_verified_at: Optional[datetime] = None
    sot_reconciliation_state: Optional[str] = None
    sot_reconciliation_notes: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "polymarket_order_id": self.polymarket_order_id,
            "status": self.status,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "stake_usd": self.stake_usd,
            "mode": self.mode,
            "created_at": self.created_at,
            "polymarket_confirmed_status": self.polymarket_confirmed_status,
            "polymarket_confirmed_fill_price": self.polymarket_confirmed_fill_price,
            "polymarket_confirmed_size": self.polymarket_confirmed_size,
            "polymarket_confirmed_at": self.polymarket_confirmed_at,
            "polymarket_last_verified_at": self.polymarket_last_verified_at,
            "sot_reconciliation_state": self.sot_reconciliation_state,
            "sot_reconciliation_notes": self.sot_reconciliation_notes,
        }


class StubPoolDBClient:
    """Test stand-in for `_PoolDBClient` — returns canned rows and records updates."""

    def __init__(self, rows: list[FakeRow]) -> None:
        self._rows = rows
        self.updates: list[dict] = []

    async def fetch_manual_trades_for_sot_check(
        self,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[dict]:
        return [r.as_dict() for r in self._rows[:limit]]

    async def update_manual_trade_sot(
        self,
        trade_id: str,
        *,
        polymarket_confirmed_status,
        polymarket_confirmed_fill_price,
        polymarket_confirmed_size,
        polymarket_confirmed_at,
        sot_reconciliation_state,
        sot_reconciliation_notes,
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
            }
        )


class StubPolymarketClient:
    """Test stand-in for PolymarketClient — returns canned OrderStatus responses."""

    def __init__(self, orders: dict[str, Optional[PolyOrderStatus]]) -> None:
        # Map order_id -> PolyOrderStatus or None (no record)
        self._orders = orders
        self.calls: list[str] = []

    async def get_order_status_sot(self, order_id: str) -> Optional[PolyOrderStatus]:
        self.calls.append(order_id)
        return self._orders.get(order_id)

    async def list_recent_orders(self, since=None, limit=50):  # noqa: ARG002 - test helper signature
        return [o for o in self._orders.values() if o is not None]


class StubAlerter:
    """Records every Telegram alert sent — supports `await send_raw_message(text)`."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_raw_message(self, text: str) -> None:
        self.messages.append(text)


# ─── Helper to construct a fresh reconciler ─────────────────────────────────


def _make_reconciler(
    monkeypatch,
    rows: list[FakeRow],
    orders: dict[str, Optional[PolyOrderStatus]],
) -> tuple[CLOBReconciler, StubPoolDBClient, StubPolymarketClient, StubAlerter]:
    """Construct a CLOBReconciler with stubs for db, polymarket and alerter.

    Patches `reconciliation.reconciler._PoolDBClient` so the reconciler uses
    the StubPoolDBClient even though the real one would try to acquire
    connections from a real asyncpg pool.
    """
    db_stub = StubPoolDBClient(rows)
    poly_stub = StubPolymarketClient(orders)
    alerter_stub = StubAlerter()

    # Patch _PoolDBClient inside the reconciler module so the constructor
    # call inside reconcile_manual_trades_sot returns our stub.
    monkeypatch.setattr(
        reconciler_mod,
        "_PoolDBClient",
        lambda pool: db_stub,
    )

    rec = CLOBReconciler(
        poly_client=poly_stub,
        db_pool=object(),  # any truthy sentinel — never accessed
        alerter=alerter_stub,
        shutdown_event=asyncio.Event(),
    )
    return rec, db_stub, poly_stub, alerter_stub


# ─── Case 1: AGREES — engine + polymarket match ─────────────────────────────


@pytest.mark.asyncio
async def test_agrees_engine_executed_polymarket_filled_matching(monkeypatch):
    """Engine says executed, Polymarket says filled with matching fill_price → agrees."""
    rows = [
        FakeRow(
            trade_id="manual_aaaaaaaaaaaaaaaa",
            polymarket_order_id="0xclob_order_1",
            status="executed",
            entry_price=0.5500,
            stake_usd=4.0,
        )
    ]
    orders = {
        "0xclob_order_1": PolyOrderStatus(
            order_id="0xclob_order_1",
            status="matched",
            fill_price=0.5510,  # 0.18% diff — within 0.5% tolerance
            fill_size=7.27,
            timestamp=datetime.now(timezone.utc),
        )
    }
    rec, db, poly, alerter = _make_reconciler(monkeypatch, rows, orders)

    summary = await rec.reconcile_manual_trades_sot()

    assert summary.checked == 1
    assert summary.agrees == 1
    assert summary.engine_optimistic == 0
    assert summary.diverged == 0
    assert summary.alerts_fired == 0
    assert len(alerter.messages) == 0
    # DB should have been stamped with state='agrees'
    assert len(db.updates) == 1
    upd = db.updates[0]
    assert upd["trade_id"] == "manual_aaaaaaaaaaaaaaaa"
    assert upd["sot_reconciliation_state"] == "agrees"
    assert upd["polymarket_confirmed_status"] == "matched"
    assert upd["polymarket_confirmed_fill_price"] == pytest.approx(0.5510)
    # And the Polymarket call should have been made exactly once
    assert poly.calls == ["0xclob_order_1"]


# ─── Case 2: ENGINE_OPTIMISTIC — Polymarket has no record ───────────────────


@pytest.mark.asyncio
async def test_engine_optimistic_engine_executed_polymarket_no_record(monkeypatch):
    """Engine says executed, Polymarket has no record → engine_optimistic + alert."""
    rows = [
        FakeRow(
            trade_id="manual_bbbbbbbbbbbbbbbb",
            polymarket_order_id="0xclob_order_2",
            status="executed",
            entry_price=0.62,
            stake_usd=5.0,
        )
    ]
    orders = {
        "0xclob_order_2": None,  # Polymarket returns None — no such order
    }
    rec, db, poly, alerter = _make_reconciler(monkeypatch, rows, orders)

    summary = await rec.reconcile_manual_trades_sot()

    assert summary.checked == 1
    assert summary.engine_optimistic == 1
    assert summary.agrees == 0
    assert summary.alerts_fired == 1
    # Telegram alert was sent — must mention engine optimistic / divergence
    assert len(alerter.messages) == 1
    msg = alerter.messages[0]
    assert "ENGINE OPTIMISTIC" in msg or "engine_optimistic" in msg.lower()
    assert "manual_bbbbbbbbbbbbbbbb"[:16] in msg
    # DB stamped with state='engine_optimistic'
    assert len(db.updates) == 1
    assert db.updates[0]["sot_reconciliation_state"] == "engine_optimistic"


# ─── Case 3: DIVERGED — fill_price mismatch beyond tolerance ────────────────


@pytest.mark.asyncio
async def test_diverged_fill_price_mismatch_beyond_tolerance(monkeypatch):
    """Engine says executed at 0.55, Polymarket says filled at 0.561 (2% diff) → diverged."""
    rows = [
        FakeRow(
            trade_id="manual_cccccccccccccccc",
            polymarket_order_id="0xclob_order_3",
            status="executed",
            entry_price=0.5500,  # engine recorded 0.55
            stake_usd=4.0,
        )
    ]
    orders = {
        "0xclob_order_3": PolyOrderStatus(
            order_id="0xclob_order_3",
            status="matched",
            fill_price=0.5610,  # 2% diff — exceeds 0.5% tolerance
            fill_size=7.13,
            timestamp=datetime.now(timezone.utc),
        )
    }
    rec, db, poly, alerter = _make_reconciler(monkeypatch, rows, orders)

    summary = await rec.reconcile_manual_trades_sot()

    assert summary.checked == 1
    assert summary.diverged == 1
    assert summary.agrees == 0
    assert summary.alerts_fired == 1
    assert len(alerter.messages) == 1
    msg = alerter.messages[0]
    assert "DIVERGED" in msg or "diverged" in msg.lower()
    # Notes should mention the price diff
    assert len(db.updates) == 1
    assert db.updates[0]["sot_reconciliation_state"] == "diverged"
    notes = db.updates[0]["sot_reconciliation_notes"] or ""
    assert "price diff" in notes.lower()


# ─── Case 4: UNRECONCILED — Polymarket order not yet terminal ───────────────


@pytest.mark.asyncio
async def test_unreconciled_polymarket_order_pending(monkeypatch):
    """Engine says executed, Polymarket says pending → unreconciled, no alert."""
    rows = [
        FakeRow(
            trade_id="manual_dddddddddddddddd",
            polymarket_order_id="0xclob_order_4",
            status="executed",
        )
    ]
    orders = {
        "0xclob_order_4": PolyOrderStatus(
            order_id="0xclob_order_4",
            status="pending",  # not terminal
            fill_price=None,
            fill_size=0,
            timestamp=datetime.now(timezone.utc),
        )
    }
    rec, db, poly, alerter = _make_reconciler(monkeypatch, rows, orders)

    summary = await rec.reconcile_manual_trades_sot()

    assert summary.checked == 1
    assert summary.unreconciled == 1
    assert summary.engine_optimistic == 0
    assert summary.alerts_fired == 0
    assert len(alerter.messages) == 0
    # DB stamped with state='unreconciled' AND polymarket_confirmed_status='pending'
    assert len(db.updates) == 1
    upd = db.updates[0]
    assert upd["sot_reconciliation_state"] == "unreconciled"
    assert upd["polymarket_confirmed_status"] == "pending"


# ─── Case 5: POLYMARKET_ONLY — engine failed but polymarket has fill ────────


@pytest.mark.asyncio
async def test_polymarket_only_engine_failed_but_poly_filled(monkeypatch):
    """Engine says failed, Polymarket says filled → polymarket_only + alert."""
    rows = [
        FakeRow(
            trade_id="manual_eeeeeeeeeeeeeeee",
            polymarket_order_id="0xclob_order_5",
            status="failed_no_token",  # engine status indicates failure
            entry_price=0.50,
            stake_usd=4.0,
        )
    ]
    orders = {
        "0xclob_order_5": PolyOrderStatus(
            order_id="0xclob_order_5",
            status="matched",  # but Polymarket has it
            fill_price=0.50,
            fill_size=8.0,
            timestamp=datetime.now(timezone.utc),
        )
    }
    rec, db, poly, alerter = _make_reconciler(monkeypatch, rows, orders)

    summary = await rec.reconcile_manual_trades_sot()

    assert summary.checked == 1
    assert summary.polymarket_only == 1
    assert summary.alerts_fired == 1
    assert len(alerter.messages) == 1
    assert "POLYMARKET ONLY" in alerter.messages[0] or "polymarket_only" in alerter.messages[0].lower()
    assert len(db.updates) == 1
    assert db.updates[0]["sot_reconciliation_state"] == "polymarket_only"


# ─── Case 6a: NO ORDER ID, row is OLD → engine_optimistic ───────────────────


@pytest.mark.asyncio
async def test_no_order_id_old_row_marked_engine_optimistic(monkeypatch):
    """Row older than 2 min with no polymarket_order_id → engine_optimistic + alert."""
    rows = [
        FakeRow(
            trade_id="manual_ffffffffffffffff",
            polymarket_order_id=None,  # poller crashed before persisting
            status="open",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
    ]
    rec, db, poly, alerter = _make_reconciler(monkeypatch, rows, {})

    summary = await rec.reconcile_manual_trades_sot()

    assert summary.checked == 1
    assert summary.engine_optimistic == 1
    assert summary.skipped_no_order_id == 1
    assert summary.alerts_fired == 1
    assert len(alerter.messages) == 1
    assert len(db.updates) == 1
    assert db.updates[0]["sot_reconciliation_state"] == "engine_optimistic"


# ─── Case 6b: NO ORDER ID, row is RECENT → unreconciled ─────────────────────


@pytest.mark.asyncio
async def test_no_order_id_recent_row_marked_unreconciled(monkeypatch):
    """Row younger than 2 min with no polymarket_order_id → unreconciled, no alert."""
    rows = [
        FakeRow(
            trade_id="manual_gggggggggggggggg",
            polymarket_order_id=None,
            status="pending_live",
            created_at=datetime.now(timezone.utc) - timedelta(seconds=45),
        )
    ]
    rec, db, poly, alerter = _make_reconciler(monkeypatch, rows, {})

    summary = await rec.reconcile_manual_trades_sot()

    assert summary.checked == 1
    assert summary.unreconciled == 1
    assert summary.engine_optimistic == 0
    assert summary.alerts_fired == 0
    assert len(alerter.messages) == 0
    assert len(db.updates) == 1
    assert db.updates[0]["sot_reconciliation_state"] == "unreconciled"


# ─── Case 7: Alert dedupe — same trade_id only alerts once per session ──────


@pytest.mark.asyncio
async def test_alert_dedupe_same_trade_id_only_alerts_once(monkeypatch):
    """Calling reconcile twice on the same engine_optimistic row only fires one alert."""
    rows = [
        FakeRow(
            trade_id="manual_hhhhhhhhhhhhhhhh",
            polymarket_order_id="0xclob_order_8",
            status="executed",
        )
    ]
    orders = {"0xclob_order_8": None}
    rec, db, poly, alerter = _make_reconciler(monkeypatch, rows, orders)

    s1 = await rec.reconcile_manual_trades_sot()
    s2 = await rec.reconcile_manual_trades_sot()

    assert s1.engine_optimistic == 1
    assert s2.engine_optimistic == 1
    # Each pass writes to DB but only the FIRST pass fires an alert.
    assert s1.alerts_fired == 1
    assert s2.alerts_fired == 0
    assert len(alerter.messages) == 1


# ─── Case 8: ENGINE FAILED + POLYMARKET NO RECORD = AGREES (negative case) ──


@pytest.mark.asyncio
async def test_engine_failed_polymarket_no_record_marks_agrees(monkeypatch):
    """Engine knows it failed and Polymarket has nothing → both agree, mark agrees."""
    rows = [
        FakeRow(
            trade_id="manual_iiiiiiiiiiiiiiii",
            polymarket_order_id="0xclob_order_9",
            status="failed_no_token",
        )
    ]
    orders = {"0xclob_order_9": None}
    rec, db, poly, alerter = _make_reconciler(monkeypatch, rows, orders)

    summary = await rec.reconcile_manual_trades_sot()

    assert summary.agrees == 1
    assert summary.engine_optimistic == 0
    assert summary.alerts_fired == 0
    assert len(alerter.messages) == 0
    assert db.updates[0]["sot_reconciliation_state"] == "agrees"


# ─── Case 9: PAPER MODE SYNTHETIC IDs handled by poly client paper path ─────


@pytest.mark.asyncio
async def test_paper_mode_synthetic_id_resolves_to_filled(monkeypatch):
    """A `manual-paper-*` order ID that polymarket synthesises as filled → agrees."""
    rows = [
        FakeRow(
            trade_id="manual_jjjjjjjjjjjjjjjj",
            polymarket_order_id="manual-paper-jjjjjjjjjjjj",
            status="open",
            entry_price=0.50,
            stake_usd=4.0,
            mode="paper",
        )
    ]
    # Use an OrderStatus that mirrors what get_order_status_sot would return
    # for a paper-mode synthetic ID.
    orders = {
        "manual-paper-jjjjjjjjjjjj": PolyOrderStatus(
            order_id="manual-paper-jjjjjjjjjjjj",
            status="filled",
            fill_price=None,  # paper synthetic — no concrete fill price
            fill_size=None,
            timestamp=datetime.now(timezone.utc),
            raw={"synthetic_paper": True},
        )
    }
    rec, db, poly, alerter = _make_reconciler(monkeypatch, rows, orders)

    summary = await rec.reconcile_manual_trades_sot()

    # Synthetic paper rows should resolve cleanly to agrees because we
    # have nothing to compare prices against — the only divergence we'd
    # detect is engine vs poly fill_price, and poly returns None.
    assert summary.agrees == 1
    assert summary.engine_optimistic == 0
    assert summary.alerts_fired == 0


# ─── Case 10: Empty DB — no rows, no work ───────────────────────────────────


@pytest.mark.asyncio
async def test_empty_db_no_rows_returns_empty_summary(monkeypatch):
    """No manual_trades to check → empty summary, no DB writes, no alerts."""
    rec, db, poly, alerter = _make_reconciler(monkeypatch, [], {})

    summary = await rec.reconcile_manual_trades_sot()

    assert summary.checked == 0
    assert summary.agrees == 0
    assert summary.alerts_fired == 0
    assert len(db.updates) == 0
    assert len(alerter.messages) == 0
    assert poly.calls == []


# ─── Case 11: Polymarket fetch error preserves prior state ──────────────────


@pytest.mark.asyncio
async def test_polymarket_fetch_error_does_not_change_state(monkeypatch):
    """Transient SDK error should not regress an `agrees` row to `engine_optimistic`."""
    rows = [
        FakeRow(
            trade_id="manual_kkkkkkkkkkkkkkkk",
            polymarket_order_id="0xclob_order_11",
            status="executed",
            sot_reconciliation_state="agrees",
            polymarket_confirmed_status="matched",
            polymarket_confirmed_fill_price=0.55,
            polymarket_confirmed_size=7.27,
        )
    ]

    class ErrorPolyClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_order_status_sot(self, order_id: str):
            self.calls.append(order_id)
            raise RuntimeError("simulated network blip")

        async def list_recent_orders(self, since=None, limit=50):  # noqa: ARG002
            return []

    db_stub = StubPoolDBClient(rows)
    poly_stub = ErrorPolyClient()
    alerter_stub = StubAlerter()
    monkeypatch.setattr(reconciler_mod, "_PoolDBClient", lambda pool: db_stub)

    rec = CLOBReconciler(
        poly_client=poly_stub,
        db_pool=object(),
        alerter=alerter_stub,
        shutdown_event=asyncio.Event(),
    )

    summary = await rec.reconcile_manual_trades_sot()

    assert summary.checked == 1
    assert summary.errors == 1
    assert summary.engine_optimistic == 0  # transient error must NOT regress
    # The DB write should preserve the prior agrees state
    assert len(db_stub.updates) == 1
    assert db_stub.updates[0]["sot_reconciliation_state"] == "agrees"
    assert "transient" in (db_stub.updates[0]["sot_reconciliation_notes"] or "")
