"""
Regression tests for Hub #578 follow-up #1:
``send_fill_confirmed`` Telegram emission when the SOT reconciler
first-stamps ``fill_price`` on a provisional gtc_resting row.

PR #567 introduced a writer that lands a provisional ``trades`` row at
GTC placement time with ``fill_price=NULL`` / ``fill_size=NULL``. When
the on-chain fill lands 30-60 s later, the SOT reconciler matches the
fill via ``poly_fills`` and updates the row. The TG alert was only
emitted by the SYNCHRONOUS ``execute_trade`` path (immediately after
the FAK ladder returned), so a gtc_resting fill that landed via the
reconciler was completely silent — the user was blind to fills.

This module tests that:

  a. Reconciler matches a previously-NULL fill_price row → TG fires once.
  b. Reconciler re-runs on an already-stamped row → TG does NOT fire
     (DB-level guard returns ``first_stamp=False``).
  c. In-memory dedupe (``_sot_alerted_fill_trade_ids``) is a second
     belt-and-braces guard — even if the DB guard ever loosens, the TG
     does not double-fire within a single engine boot.

The synchronous-FAK path is covered by the existing
``tests/unit/use_cases/test_execute_trade.py`` battery — we don't
re-test it here, but we DO verify that the reconciler emission cannot
re-fire on a row that the synchronous path already alerted on
(implicit: provisional rows have ``fill_price=NULL`` and the FAK path
never lands one, so the dedupe namespace is naturally disjoint).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from reconciliation import reconciler as reconciler_mod
from reconciliation.reconciler import CLOBReconciler


# ─── Helpers ────────────────────────────────────────────────────────────────


def _provisional_trade(
    trade_id: int = 9001,
    *,
    fill_price: Optional[float] = None,
    fill_size: Optional[float] = None,
    strategy: str = "v9_2_raw_lgb",
    direction: str = "YES",
    stake_usd: float = 4.0,
    created_at: Optional[datetime] = None,
) -> dict:
    """Build a provisional gtc_resting trade row dict.

    Matches the shape returned by ``fetch_trades_joined_poly_fills``
    after the strategy + metadata SELECT additions. ``fill_price`` and
    ``fill_size`` are NULL by default — the PR #567 provisional row.
    """
    if created_at is None:
        created_at = datetime.now(timezone.utc) - timedelta(minutes=15)
    return {
        "trade_id": trade_id,
        "polymarket_order_id": f"0xclob_{trade_id}",
        "order_id": f"0xorder_{trade_id}",
        "clob_order_id": f"0xclob_{trade_id}",
        "status": "OPEN",
        "direction": direction,
        "entry_price": 0.55,
        "fill_price": fill_price,
        "fill_size": fill_size,
        "stake_usd": stake_usd,
        "mode": "live",
        "execution_mode": "gtc_resting",
        "is_live": True,
        "created_at": created_at,
        "market_slug": f"btc-updown-5m-{1700000000 + trade_id}",
        "poly_outcome": "Up" if direction == "YES" else "Down",
        "polymarket_confirmed_status": None,
        "polymarket_confirmed_fill_price": None,
        "polymarket_confirmed_size": None,
        "polymarket_confirmed_at": None,
        "polymarket_last_verified_at": None,
        "sot_reconciliation_state": None,
        "sot_reconciliation_notes": None,
        # Hub #578 follow-up #1: strategy + metadata columns added to
        # the SELECT so the FILL card can carry strategy_id / window_ts.
        "strategy": strategy,
        "metadata": {"window_ts": 1700000000 + trade_id},
    }


def _matched_fill(
    poly_price: float = 0.55,
    poly_size: float = 7.27,
    tx_hash: str = "0xfilltx_abcdef",
    match_time_utc: Optional[datetime] = None,
) -> dict:
    if match_time_utc is None:
        match_time_utc = datetime.now(timezone.utc)
    return {
        "poly_price": poly_price,
        "poly_size": poly_size,
        "transaction_hash": tx_hash,
        "match_time_utc": match_time_utc,
    }


class _StubTradesPoolDBClient:
    """Trades-table SOT adapter stub.

    Tracks the ``first_stamp`` flag returned by ``update_trade_sot`` so
    tests can simulate the DB-level "previously NULL → now non-NULL"
    transition (or the no-op re-run case).
    """

    def __init__(
        self,
        joined_rows: list[tuple[dict, Optional[dict]]],
        *,
        first_stamp_by_trade_id: Optional[dict[int, bool]] = None,
    ) -> None:
        self._rows = joined_rows
        self._first_stamp_by_trade_id = first_stamp_by_trade_id or {}
        self.updates: list[dict] = []

    async def fetch_trades_joined_poly_fills(
        self,
        since: Optional[datetime] = None,
        limit: int = 200,
    ) -> list[tuple[dict, Optional[dict]]]:
        return list(self._rows[:limit])

    async def fetch_poly_fill_for_trade(
        self,
        market_slug: str,
        poly_outcome: str,
        trade_created_at,
    ) -> Optional[dict]:
        return None

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
    ) -> bool:
        self.updates.append(
            {
                "trade_id": trade_id,
                "polymarket_confirmed_status": polymarket_confirmed_status,
                "polymarket_confirmed_fill_price": polymarket_confirmed_fill_price,
                "polymarket_confirmed_size": polymarket_confirmed_size,
                "sot_reconciliation_state": sot_reconciliation_state,
                "polymarket_tx_hash": polymarket_tx_hash,
            }
        )
        # The real adapter returns True iff this UPDATE first-stamped a
        # previously-NULL fill_price. The stub honours an explicit override
        # per trade_id; default is False (no-op re-run).
        return bool(self._first_stamp_by_trade_id.get(int(trade_id), False))


class _StubAlerter:
    """Captures every TG call so the test can assert on shape + count."""

    def __init__(self) -> None:
        self.fill_confirmed_calls: list[dict] = []
        self.raw_messages: list[str] = []

    async def send_fill_confirmed(
        self,
        *,
        strategy: str,
        window_ts: int,
        side: str,
        price: float,
        shares: float,
        stake_usd: float,
        condition_id=None,
        tx_hash=None,
        pre_fill_wallet=None,
        post_fill_wallet=None,
        timeframe: str = "5m",
    ) -> None:
        self.fill_confirmed_calls.append(
            {
                "strategy": strategy,
                "window_ts": window_ts,
                "side": side,
                "price": price,
                "shares": shares,
                "stake_usd": stake_usd,
                "tx_hash": tx_hash,
                "timeframe": timeframe,
            }
        )

    async def send_raw_message(self, text: str) -> None:
        self.raw_messages.append(text)


def _make_reconciler(
    monkeypatch,
    joined_rows: list[tuple[dict, Optional[dict]]],
    *,
    first_stamp_by_trade_id: Optional[dict[int, bool]] = None,
) -> tuple[CLOBReconciler, _StubTradesPoolDBClient, _StubAlerter]:
    db_stub = _StubTradesPoolDBClient(
        joined_rows,
        first_stamp_by_trade_id=first_stamp_by_trade_id,
    )
    alerter_stub = _StubAlerter()

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


# ─── Case A: provisional row → fill confirmed → TG fires once ───────────────


@pytest.mark.asyncio
async def test_provisional_row_first_stamp_emits_fill_confirmed(monkeypatch):
    """The canonical regression scenario:
      * Engine placed a gtc_resting order; provisional row has fill_price=NULL.
      * poly_fills_reconciler landed the on-chain fill 30s later.
      * SOT pass joins the two → state=agrees, first_stamp=True.
      * Telegram ``send_fill_confirmed`` fires exactly once.
    """
    trade = _provisional_trade(
        trade_id=9001,
        strategy="v9_2_raw_lgb",
        direction="YES",
        stake_usd=4.0,
    )
    fill = _matched_fill(poly_price=0.55, poly_size=7.27, tx_hash="0xfill_9001")

    rec, db, alerter = _make_reconciler(
        monkeypatch,
        [(trade, fill)],
        first_stamp_by_trade_id={9001: True},
    )

    summary = await rec.reconcile_trades_sot()

    assert summary.checked == 1
    assert summary.agrees == 1
    assert len(db.updates) == 1
    assert db.updates[0]["sot_reconciliation_state"] == "agrees"
    assert db.updates[0]["polymarket_confirmed_fill_price"] == pytest.approx(0.55)

    # TG: ONE FILL card with the right strategy / direction / price / stake.
    assert len(alerter.fill_confirmed_calls) == 1
    card = alerter.fill_confirmed_calls[0]
    assert card["strategy"] == "v9_2_raw_lgb"
    assert card["side"] == "UP"  # YES → UP normalisation
    assert card["price"] == pytest.approx(0.55)
    assert card["shares"] == pytest.approx(7.27)
    assert card["stake_usd"] == pytest.approx(4.0)
    assert card["tx_hash"] == "0xfill_9001"


# ─── Case B: re-run on already-stamped row → TG does NOT fire ───────────────


@pytest.mark.asyncio
async def test_already_stamped_row_no_fill_confirmed(monkeypatch):
    """Idempotency: a re-run of the SOT loop on a row whose fill_price
    has ALREADY been stamped must NOT re-emit the FILL card.

    Simulated by ``first_stamp_by_trade_id[9002]=False`` — mirrors what
    the real adapter does when the DB-level CASE WHEN guard sees a
    non-NULL fill_price and skips the assignment.
    """
    trade = _provisional_trade(
        trade_id=9002,
        fill_price=0.55,
        fill_size=7.27,
    )
    fill = _matched_fill(poly_price=0.55, poly_size=7.27, tx_hash="0xfill_9002")

    rec, db, alerter = _make_reconciler(
        monkeypatch,
        [(trade, fill)],
        first_stamp_by_trade_id={9002: False},
    )

    summary = await rec.reconcile_trades_sot()

    assert summary.checked == 1
    assert summary.agrees == 1
    # Update still happens (polymarket_last_verified_at moves forward,
    # confirmed_status / tx_hash get re-affirmed) — but NO fill card.
    assert len(db.updates) == 1
    assert len(alerter.fill_confirmed_calls) == 0
    assert len(alerter.raw_messages) == 0


# ─── Case C: in-memory dedupe — same trade_id twice in one boot ─────────────


@pytest.mark.asyncio
async def test_in_memory_dedupe_blocks_second_emission(monkeypatch):
    """Belt-and-braces: even if the DB-level guard ever flapped and
    returned ``first_stamp=True`` twice for the same trade_id, the
    in-memory ``_sot_alerted_fill_trade_ids`` set must block the
    second emission within a single engine boot.
    """
    trade = _provisional_trade(trade_id=9003, strategy="v9_2_raw_lgb")
    fill = _matched_fill()

    rec, db, alerter = _make_reconciler(
        monkeypatch,
        [(trade, fill)],
        first_stamp_by_trade_id={9003: True},
    )

    # First pass — should fire.
    await rec.reconcile_trades_sot()
    assert len(alerter.fill_confirmed_calls) == 1

    # Second pass — same row, same first_stamp signal. The DB guard
    # is by design impossible in production (fill_price is no longer
    # NULL), but the stub forces True to prove the in-memory dedupe
    # acts as a second line of defence.
    await rec.reconcile_trades_sot()
    assert len(alerter.fill_confirmed_calls) == 1  # still 1, not 2


# ─── Case D: diverged + first_stamp also emits (price mismatch path) ────────


@pytest.mark.asyncio
async def test_diverged_with_first_stamp_still_emits(monkeypatch):
    """A diverged state with a NULL → non-NULL fill_price transition
    SHOULD still emit a FILL card — the on-chain fill DID land, the
    operator still needs to see it. The accompanying POLY-SOT divergence
    alert (fired by ``_fire_sot_alert``) is a separate concern; the FILL
    card is the "your money moved" signal.
    """
    trade = _provisional_trade(
        trade_id=9004,
        strategy="v9_2_raw_lgb",
        direction="NO",
    )
    # Wide divergence (engine entry 0.55 → on-chain 0.61, ~10%)
    trade["entry_price"] = 0.55
    fill = _matched_fill(poly_price=0.61, poly_size=6.55, tx_hash="0xfill_9004")

    rec, db, alerter = _make_reconciler(
        monkeypatch,
        [(trade, fill)],
        first_stamp_by_trade_id={9004: True},
    )

    summary = await rec.reconcile_trades_sot()

    assert summary.diverged == 1
    assert len(alerter.fill_confirmed_calls) == 1
    card = alerter.fill_confirmed_calls[0]
    assert card["side"] == "DOWN"  # NO → DOWN normalisation
    assert card["price"] == pytest.approx(0.61)


# ─── Case E: no first_stamp from a pre-PR-567 row that already had fill ─────


@pytest.mark.asyncio
async def test_legacy_pre_pr567_row_no_emit(monkeypatch):
    """Defensive: legacy rows from BEFORE PR #567 land with fill_price
    already set (FAK path). The reconciler's first_stamp signal is
    False for these — TG must not fire.
    """
    trade = _provisional_trade(
        trade_id=9005,
        fill_price=0.62,  # already stamped by FAK path
        fill_size=6.45,
    )
    trade["execution_mode"] = "live"  # not gtc_resting
    fill = _matched_fill(poly_price=0.62, poly_size=6.45)

    rec, db, alerter = _make_reconciler(
        monkeypatch,
        [(trade, fill)],
        first_stamp_by_trade_id={9005: False},  # already had fill_price
    )

    await rec.reconcile_trades_sot()

    assert len(alerter.fill_confirmed_calls) == 0
