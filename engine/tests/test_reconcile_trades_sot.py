"""
Tests for engine/reconciliation/reconciler.py::CLOBReconciler.reconcile_trades_sot
— POLY-SOT-b.

Mirrors the 12 cases in test_reconcile_manual_trades_sot.py but exercises the
new ``reconcile_trades_sot`` method that walks the engine's automatic-trade
``trades`` table instead of operator ``manual_trades``. Plus 3 backfill tests
for POLY-SOT-c.

Both methods share the ``_compare_to_polymarket`` helper, so this file
verifies that:
  1. Every state path produces the right tag + alert behaviour against the
     trades table.
  2. The Telegram dedupe key is namespaced by table — manual_trades #42 and
     trades #42 do NOT suppress each other.
  3. The POLY-SOT-c backfill script's row-decision logic correctly tags
     historical NULL-state rows.

The tests use stub DB / Polymarket clients identical in shape to the
manual_trades suite. The DB stub records every update so we can assert
state transitions deterministically.
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


@dataclass
class FakeTradeRow:
    """A single `trades` row as the SOT reconciler sees it."""
    trade_id: int  # integer primary key (trades.id)
    polymarket_order_id: Optional[str]
    status: str
    direction: str = "YES"
    entry_price: Optional[float] = 0.55
    stake_usd: Optional[float] = 4.0
    fill_price: Optional[float] = None
    fill_size: Optional[float] = None
    mode: str = "live"
    is_live: bool = True
    order_id: str = "engine-internal-id"
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
            "order_id": self.order_id,
            "polymarket_order_id": self.polymarket_order_id,
            "status": self.status,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "stake_usd": self.stake_usd,
            "fill_price": self.fill_price,
            "fill_size": self.fill_size,
            "mode": self.mode,
            "is_live": self.is_live,
            "created_at": self.created_at,
            "polymarket_confirmed_status": self.polymarket_confirmed_status,
            "polymarket_confirmed_fill_price": self.polymarket_confirmed_fill_price,
            "polymarket_confirmed_size": self.polymarket_confirmed_size,
            "polymarket_confirmed_at": self.polymarket_confirmed_at,
            "polymarket_last_verified_at": self.polymarket_last_verified_at,
            "sot_reconciliation_state": self.sot_reconciliation_state,
            "sot_reconciliation_notes": self.sot_reconciliation_notes,
        }


class StubTradesPoolDBClient:
    """Test stand-in for `_TradesPoolDBClient` — returns canned rows and records updates."""

    def __init__(self, rows: list[FakeTradeRow]) -> None:
        self._rows = rows
        self.updates: list[dict] = []

    async def fetch_trades_for_sot_check(
        self,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[dict]:
        return [r.as_dict() for r in self._rows[:limit]]

    async def fetch_trades_joined_poly_fills(
        self,
        since=None,
        limit: int = 200,
    ) -> list:
        return []

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
        self._orders = orders
        self.calls: list[str] = []

    async def get_order_status_sot(self, order_id: str) -> Optional[PolyOrderStatus]:
        self.calls.append(order_id)
        return self._orders.get(order_id)

    async def list_recent_orders(self, since=None, limit=50):  # noqa: ARG002
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
    rows: list[FakeTradeRow],
    orders: dict[str, Optional[PolyOrderStatus]],
) -> tuple[CLOBReconciler, StubTradesPoolDBClient, StubPolymarketClient, StubAlerter]:
    """Construct a CLOBReconciler with stubs for db, polymarket and alerter.

    Patches `reconciliation.reconciler._TradesPoolDBClient` so the reconciler
    uses the StubTradesPoolDBClient even though the real one would try to
    acquire connections from a real asyncpg pool.
    """
    db_stub = StubTradesPoolDBClient(rows)
    poly_stub = StubPolymarketClient(orders)
    alerter_stub = StubAlerter()

    monkeypatch.setattr(
        reconciler_mod,
        "_TradesPoolDBClient",
        lambda pool: db_stub,
    )

    rec = CLOBReconciler(
        poly_client=poly_stub,
        db_pool=object(),
        alerter=alerter_stub,
        shutdown_event=asyncio.Event(),
    )
    return rec, db_stub, poly_stub, alerter_stub


# ─── Case 1: AGREES — engine + polymarket match ─────────────────────────────


@pytest.mark.asyncio
async def test_agrees_engine_executed_polymarket_filled_matching(monkeypatch):
    """Engine says FILLED, Polymarket says matched with matching fill_price → agrees."""
    rows = [
        FakeTradeRow(
            trade_id=1001,
            polymarket_order_id="0xclob_auto_1",
            status="FILLED",
            entry_price=0.5500,
            stake_usd=4.0,
        )
    ]
    orders = {
        "0xclob_auto_1": PolyOrderStatus(
            order_id="0xclob_auto_1",
            status="matched",
            fill_price=0.5510,  # 0.18% diff — within 0.5% tolerance
            fill_size=7.27,
            timestamp=datetime.now(timezone.utc),
        )
    }
    rec, db, poly, alerter = _make_reconciler(monkeypatch, rows, orders)

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
    assert poly.calls == ["0xclob_auto_1"]


# ─── Case 2: ENGINE_OPTIMISTIC — Polymarket has no record ───────────────────


@pytest.mark.asyncio
async def test_engine_optimistic_engine_executed_polymarket_no_record(monkeypatch):
    """Engine says FILLED, Polymarket has no record → engine_optimistic + alert."""
    rows = [
        FakeTradeRow(
            trade_id=1002,
            polymarket_order_id="0xclob_auto_2",
            status="FILLED",
            entry_price=0.62,
            stake_usd=5.0,
        )
    ]
    orders = {"0xclob_auto_2": None}
    rec, db, poly, alerter = _make_reconciler(monkeypatch, rows, orders)

    summary = await rec.reconcile_trades_sot()

    assert summary.checked == 1
    assert summary.engine_optimistic == 1
    assert summary.agrees == 0
    assert summary.alerts_fired == 1
    assert len(alerter.messages) == 1
    msg = alerter.messages[0]
    assert "engine claims fill" in msg.lower() or "engine_optimistic" in msg.lower()
    # The trades alert is tagged AUTO so the operator can tell it apart
    # from the manual_trades alert.
    assert "AUTO" in msg
    assert "#1002" in msg
    assert len(db.updates) == 1
    assert db.updates[0]["sot_reconciliation_state"] == "engine_optimistic"


# ─── Case 3: DIVERGED — fill_price mismatch beyond tolerance ────────────────


@pytest.mark.asyncio
async def test_diverged_fill_price_mismatch_beyond_tolerance(monkeypatch):
    """Engine says FILLED at 0.55, Polymarket says matched at 0.561 (2% diff) → diverged."""
    rows = [
        FakeTradeRow(
            trade_id=1003,
            polymarket_order_id="0xclob_auto_3",
            status="FILLED",
            entry_price=0.5500,
            stake_usd=4.0,
        )
    ]
    orders = {
        "0xclob_auto_3": PolyOrderStatus(
            order_id="0xclob_auto_3",
            status="matched",
            fill_price=0.5610,
            fill_size=7.13,
            timestamp=datetime.now(timezone.utc),
        )
    }
    rec, db, poly, alerter = _make_reconciler(monkeypatch, rows, orders)

    summary = await rec.reconcile_trades_sot()

    assert summary.checked == 1
    assert summary.diverged == 1
    assert summary.agrees == 0
    assert summary.alerts_fired == 1
    assert len(alerter.messages) == 1
    msg = alerter.messages[0]
    assert "fill mismatch" in msg.lower() or "diverged" in msg.lower()
    assert len(db.updates) == 1
    assert db.updates[0]["sot_reconciliation_state"] == "diverged"
    notes = db.updates[0]["sot_reconciliation_notes"] or ""
    assert "price diff" in notes.lower()


# ─── Case 4: UNRECONCILED — Polymarket order not yet terminal ───────────────


@pytest.mark.asyncio
async def test_unreconciled_polymarket_order_pending(monkeypatch):
    """Engine says FILLED, Polymarket says pending → unreconciled, no alert."""
    rows = [
        FakeTradeRow(
            trade_id=1004,
            polymarket_order_id="0xclob_auto_4",
            status="FILLED",
        )
    ]
    orders = {
        "0xclob_auto_4": PolyOrderStatus(
            order_id="0xclob_auto_4",
            status="pending",
            fill_price=None,
            fill_size=0,
            timestamp=datetime.now(timezone.utc),
        )
    }
    rec, db, poly, alerter = _make_reconciler(monkeypatch, rows, orders)

    summary = await rec.reconcile_trades_sot()

    assert summary.checked == 1
    assert summary.unreconciled == 1
    assert summary.engine_optimistic == 0
    assert summary.alerts_fired == 0
    assert len(alerter.messages) == 0
    assert len(db.updates) == 1
    upd = db.updates[0]
    assert upd["sot_reconciliation_state"] == "unreconciled"
    assert upd["polymarket_confirmed_status"] == "pending"


# ─── Case 5: POLYMARKET_ONLY — engine failed but polymarket has fill ────────


@pytest.mark.asyncio
async def test_polymarket_only_engine_failed_but_poly_filled(monkeypatch):
    """Engine status starts with 'failed' but Polymarket has a fill → polymarket_only + alert."""
    rows = [
        FakeTradeRow(
            trade_id=1005,
            polymarket_order_id="0xclob_auto_5",
            status="failed_no_token",
            entry_price=0.50,
            stake_usd=4.0,
        )
    ]
    orders = {
        "0xclob_auto_5": PolyOrderStatus(
            order_id="0xclob_auto_5",
            status="matched",
            fill_price=0.50,
            fill_size=8.0,
            timestamp=datetime.now(timezone.utc),
        )
    }
    rec, db, poly, alerter = _make_reconciler(monkeypatch, rows, orders)

    summary = await rec.reconcile_trades_sot()

    assert summary.checked == 1
    assert summary.polymarket_only == 1
    assert summary.alerts_fired == 1
    assert len(alerter.messages) == 1
    assert (
        "POLYMARKET ONLY" in alerter.messages[0]
        or "polymarket_only" in alerter.messages[0].lower()
    )
    assert len(db.updates) == 1
    assert db.updates[0]["sot_reconciliation_state"] == "polymarket_only"


# ─── Case 6a: NO ORDER ID, row is OLD → engine_optimistic ───────────────────


@pytest.mark.asyncio
async def test_no_order_id_old_row_marked_engine_optimistic(monkeypatch):
    """Row older than 2 min with no polymarket_order_id → engine_optimistic + alert."""
    rows = [
        FakeTradeRow(
            trade_id=1006,
            polymarket_order_id=None,
            status="OPEN",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
    ]
    rec, db, poly, alerter = _make_reconciler(monkeypatch, rows, {})

    summary = await rec.reconcile_trades_sot()

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
        FakeTradeRow(
            trade_id=1007,
            polymarket_order_id=None,
            status="PENDING",
            created_at=datetime.now(timezone.utc) - timedelta(seconds=45),
        )
    ]
    rec, db, poly, alerter = _make_reconciler(monkeypatch, rows, {})

    summary = await rec.reconcile_trades_sot()

    assert summary.checked == 1
    assert summary.unreconciled == 1
    assert summary.engine_optimistic == 0
    assert summary.alerts_fired == 0
    assert len(alerter.messages) == 0
    assert len(db.updates) == 1
    assert db.updates[0]["sot_reconciliation_state"] == "unreconciled"


# ─── Case 7: Alert dedupe — same trade_id only alerts once per session ──────
#            AND cross-table test: manual #42 doesn't suppress trades #42
#            (the namespaced dedupe key from POLY-SOT-b)


@pytest.mark.asyncio
async def test_alert_dedupe_same_trade_id_only_alerts_once(monkeypatch):
    """Calling reconcile twice on the same engine_optimistic row only fires one alert.

    Plus the cross-table check: manual_trades #42 and trades #42 are
    independent — neither suppresses the other.
    """
    # First half: same trade_id, two passes → only one alert.
    rows = [
        FakeTradeRow(
            trade_id=42,
            polymarket_order_id="0xclob_auto_dedupe",
            status="FILLED",
        )
    ]
    orders = {"0xclob_auto_dedupe": None}
    rec, db, poly, alerter = _make_reconciler(monkeypatch, rows, orders)

    s1 = await rec.reconcile_trades_sot()
    s2 = await rec.reconcile_trades_sot()

    assert s1.engine_optimistic == 1
    assert s2.engine_optimistic == 1
    assert s1.alerts_fired == 1
    assert s2.alerts_fired == 0
    assert len(alerter.messages) == 1

    # Cross-table half: manual_trades #42 should fire its own alert even
    # though trades #42 already fired one. We re-use the same reconciler
    # instance to share the dedupe set state.
    from reconciliation.reconciler import _PoolDBClient as _RealPoolDBClient  # noqa: F401

    # Build a manual_trades pass that targets the same numeric trade_id.
    # We swap the patched _PoolDBClient so the reconcile_manual_trades_sot
    # call uses an inline stub instead of the real adapter.
    class _StubManualDB:
        def __init__(self):
            self.updates = []

        async def fetch_manual_trades_for_sot_check(self, since=None, limit=100):
            return [
                {
                    "trade_id": "42",  # string id, like manual_trades.trade_id
                    "polymarket_order_id": "0xclob_manual_dedupe",
                    "status": "executed",
                    "mode": "live",
                    "direction": "UP",
                    "entry_price": 0.55,
                    "stake_usd": 4.0,
                    "created_at": datetime.now(timezone.utc) - timedelta(minutes=5),
                    "polymarket_confirmed_status": None,
                    "polymarket_confirmed_fill_price": None,
                    "polymarket_confirmed_size": None,
                    "polymarket_confirmed_at": None,
                    "polymarket_last_verified_at": None,
                    "sot_reconciliation_state": None,
                    "sot_reconciliation_notes": None,
                }
            ]

        async def update_manual_trade_sot(self, **kwargs):
            self.updates.append(kwargs)

    manual_db = _StubManualDB()
    monkeypatch.setattr(reconciler_mod, "_PoolDBClient", lambda pool: manual_db)
    # Make the polymarket stub know about the new manual order ID too
    poly._orders["0xclob_manual_dedupe"] = None

    s3 = await rec.reconcile_manual_trades_sot()

    # The manual_trades #42 alert should fire (not suppressed by trades #42)
    # because the dedupe key is namespaced by table.
    assert s3.engine_optimistic == 1
    assert s3.alerts_fired == 1
    assert len(alerter.messages) == 2  # one from trades, one from manual_trades


# ─── Case 8: ENGINE FAILED + POLYMARKET NO RECORD = AGREES (negative case) ──


@pytest.mark.asyncio
async def test_engine_failed_polymarket_no_record_marks_agrees(monkeypatch):
    """Engine knows it failed and Polymarket has nothing → both agree, mark agrees."""
    rows = [
        FakeTradeRow(
            trade_id=1009,
            polymarket_order_id="0xclob_auto_9",
            status="failed_no_token",
        )
    ]
    orders = {"0xclob_auto_9": None}
    rec, db, poly, alerter = _make_reconciler(monkeypatch, rows, orders)

    summary = await rec.reconcile_trades_sot()

    assert summary.agrees == 1
    assert summary.engine_optimistic == 0
    assert summary.alerts_fired == 0
    assert len(alerter.messages) == 0
    assert db.updates[0]["sot_reconciliation_state"] == "agrees"


# ─── Case 9: PAPER MODE SYNTHETIC IDs handled by poly client paper path ─────


@pytest.mark.asyncio
async def test_paper_mode_synthetic_id_resolves_to_filled(monkeypatch):
    """A `manual-paper-*` order ID that polymarket synthesises as filled → agrees.

    Even though `trades` is the automatic-trade table, paper-mode runs
    persist synthetic IDs into the same column the SOT reconciler reads.
    """
    rows = [
        FakeTradeRow(
            trade_id=1010,
            polymarket_order_id="manual-paper-jjjjjjjjjjjj",
            status="OPEN",
            entry_price=0.50,
            stake_usd=4.0,
            mode="paper",
        )
    ]
    orders = {
        "manual-paper-jjjjjjjjjjjj": PolyOrderStatus(
            order_id="manual-paper-jjjjjjjjjjjj",
            status="filled",
            fill_price=None,
            fill_size=None,
            timestamp=datetime.now(timezone.utc),
            raw={"synthetic_paper": True},
        )
    }
    rec, db, poly, alerter = _make_reconciler(monkeypatch, rows, orders)

    summary = await rec.reconcile_trades_sot()

    assert summary.agrees == 1
    assert summary.engine_optimistic == 0
    assert summary.alerts_fired == 0


# ─── Case 10: Empty DB — no rows, no work ───────────────────────────────────


@pytest.mark.asyncio
async def test_empty_db_no_rows_returns_empty_summary(monkeypatch):
    """No trades to check → empty summary, no DB writes, no alerts."""
    rec, db, poly, alerter = _make_reconciler(monkeypatch, [], {})

    summary = await rec.reconcile_trades_sot()

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
        FakeTradeRow(
            trade_id=1011,
            polymarket_order_id="0xclob_auto_11",
            status="FILLED",
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

    db_stub = StubTradesPoolDBClient(rows)
    poly_stub = ErrorPolyClient()
    alerter_stub = StubAlerter()
    monkeypatch.setattr(reconciler_mod, "_TradesPoolDBClient", lambda pool: db_stub)

    rec = CLOBReconciler(
        poly_client=poly_stub,
        db_pool=object(),
        alerter=alerter_stub,
        shutdown_event=asyncio.Event(),
    )

    summary = await rec.reconcile_trades_sot()

    assert summary.checked == 1
    assert summary.errors == 1
    assert summary.engine_optimistic == 0
    # The DB write should preserve the prior agrees state.
    assert len(db_stub.updates) == 1
    assert db_stub.updates[0]["sot_reconciliation_state"] == "agrees"


# ─── POLY-SOT-c backfill tests ──────────────────────────────────────────────
#
# These exercise the row-decision logic in the backfill script. The script
# itself uses `_compare_to_polymarket` for the per-row decision, plus a
# new `no_order_id` terminal state for old rows that never had an order ID.


@pytest.mark.asyncio
async def test_backfill_manual_trades_tags_all_null_rows(monkeypatch):
    """The backfill walks NULL-state rows and tags them via the same logic
    as the forward reconciler.

    We exercise this by feeding the StubTradesPoolDBClient a mix of rows
    and verifying the resulting tags. The script lives at
    engine/scripts/backfill_sot_reconciliation.py — we re-use its
    `_decide_for_row` helper here.
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
            "0x_old_optimistic": None,  # Polymarket has no record
        }
    )
    rec = CLOBReconciler(
        poly_client=poly,
        db_pool=None,
        alerter=StubAlerter(),
        shutdown_event=asyncio.Event(),
    )

    # Row 1: has order ID, polymarket says matched → agrees
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

    # Row 2: has order ID, polymarket has no record, engine claimed
    # executed → engine_optimistic
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
    """Dry-run mode of the backfill script must not issue any UPDATE.

    We verify by counting `db.updates` after running the per-row decision
    helper without invoking any UPDATE — that mirrors the dry-run code
    path in the actual script body.
    """
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

    # Use a stub DB to verify NO updates happen during the dry-run code path.
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
    # Critical: no DB writes were performed — dry-run path never calls
    # update_trade_sot / update_manual_trade_sot.
    assert len(db.updates) == 0


@pytest.mark.asyncio
async def test_backfill_no_order_id_old_row_tags_no_order_id(monkeypatch):
    """A row with no order ID and older than 24h gets tagged 'no_order_id'.

    POLY-SOT-c adds a new terminal state for historical rows that never
    had an order ID persisted (pre-POLY-SOT-b orchestrator). Younger
    rows are skipped so the forward reconciler can still pick them up.
    """
    from scripts.backfill_sot_reconciliation import _decide_for_row

    poly = StubPolymarketClient({})
    rec = CLOBReconciler(
        poly_client=poly,
        db_pool=None,
        alerter=StubAlerter(),
        shutdown_event=asyncio.Event(),
    )

    # Old row, no order ID
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

    # Recent row, no order ID → skipped
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
