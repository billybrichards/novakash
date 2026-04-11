"""
LT-04 — Tests for the manual-trade fast path (PostgreSQL LISTEN/NOTIFY
+ safety-net poll) in engine/strategies/orchestrator.py.

These tests pin down the five invariants that make LT-04 safe:

  1. NOTIFY → event fires → poller picks up the trade within
     <500ms (sub-second click-to-execute target).
  2. Stale NOTIFY (trade_id that doesn't match any pending row) is
     a SAFE no-op. We still call poll_pending_live_trades() and just
     get zero rows back — no crash, no double-execute.
  3. LISTEN connection dropped mid-run → safety-net 1s poll still
     fires and picks up the trade (zero regression vs pre-LT-04).
  4. Ring-buffer miss → LT-02 DB fallback still works on the fast
     path. (Regression check on orchestrator.py:2679's DB lookup.)
  5. Multiple NOTIFY events arriving between wakeups → all pending
     rows get processed in one iteration (batch-drain semantics).

Implementation notes
────────────────────
We mock the asyncpg connection + DB client entirely. The real
`engine/persistence/db_client.py::DBClient.listen` opens a
dedicated asyncpg connection to PostgreSQL, which requires a real
DB to test end-to-end. Instead we stub the `listen`,
`ensure_listening`, and `poll_pending_live_trades` methods on a
`FakeDB` and drive the poller directly.

The test setup builds a minimal mock `Orchestrator` with just the
fields the `_manual_trade_poller` method reads: `_db`, `_poly_client`,
`_alerter`, `_five_min_strategy`, `_shutdown_event`, and
`_manual_trade_notify_event`. We then invoke the poller method via
`.__get__(self, Orchestrator)` so we don't have to construct a full
orchestrator (which pulls in the entire settings + feeds + CLOB
pipeline).

Channel name `manual_trade_pending` must match:
  - engine/persistence/db_client.py::MANUAL_TRADE_NOTIFY_CHANNEL
  - hub/api/v58_monitor.py::post_manual_trade
"""

from __future__ import annotations

import asyncio
import sys
import time
from decimal import Decimal
from types import ModuleType
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest


# ────────────────────────────────────────────────────────────────────
#  Module stubs — must run BEFORE importing strategies.orchestrator
# ────────────────────────────────────────────────────────────────────
#
# `strategies.orchestrator` imports `polymarket_browser.service` which
# imports `playwright.async_api`. Playwright is an integration-only
# dependency (we install it in the Dockerfile but skip it in unit
# test environments). Stub it out before the import chain runs so
# that collecting this test file doesn't fail with
# `ModuleNotFoundError: No module named 'playwright'`.
#
# This only affects the test process — production code paths still
# import the real playwright module.


def _install_playwright_stub() -> None:
    if "playwright" in sys.modules and "playwright.async_api" in sys.modules:
        return
    playwright_pkg = ModuleType("playwright")
    playwright_pkg.__path__ = []  # type: ignore[attr-defined]
    async_api = ModuleType("playwright.async_api")
    # Minimal placeholders for the names that service.py imports.
    for name in ("Browser", "BrowserContext", "Page"):
        setattr(async_api, name, type(name, (), {}))

    async def _fake_async_playwright():
        raise RuntimeError("playwright stubbed in test environment")

    async_api.async_playwright = _fake_async_playwright  # type: ignore[attr-defined]
    sys.modules["playwright"] = playwright_pkg
    sys.modules["playwright.async_api"] = async_api


_install_playwright_stub()

from strategies.orchestrator import Orchestrator  # noqa: E402


# ────────────────────────────────────────────────────────────────────
#  Fakes
# ────────────────────────────────────────────────────────────────────


class FakeDB:
    """Stand-in for `engine.persistence.db_client.DBClient`.

    Only the methods the manual-trade poller uses are implemented:
      - poll_pending_live_trades
      - get_token_ids_from_market_data
      - update_manual_trade_status
      - listen / ensure_listening / stop_listening / is_listening

    `listen` captures the callback so tests can fire it manually to
    simulate a NOTIFY arriving on the pinned LISTEN connection.
    """

    def __init__(self) -> None:
        # Queue of batches returned by poll_pending_live_trades. Each
        # call pops one batch; if the queue is empty, returns [].
        self._pending_batches: list[list[dict]] = []
        # Record of status transitions so tests can assert the
        # execute → open flow happened.
        self.status_updates: list[tuple[str, str]] = []
        # LISTEN state
        self._listen_callback: Optional[Callable] = None
        self._listen_channel: Optional[str] = None
        self._listen_alive: bool = False
        self._listen_ever_succeeded: bool = False
        # ensure_listening can be forced to fail (simulates dropped
        # connection that can't reconnect).
        self.force_listen_failure: bool = False
        # Seeded market_data fallback rows — maps (asset, window_ts)
        # → {"up_token_id": ..., "down_token_id": ...}.
        self.market_data_rows: dict[tuple, dict] = {}
        # Track calls for assertions.
        self.poll_call_count: int = 0
        self.ensure_listening_call_count: int = 0

    def queue_pending(self, batch: list[dict]) -> None:
        """Enqueue a batch of pending rows for the next poll call."""
        self._pending_batches.append(batch)

    async def poll_pending_live_trades(self) -> list:
        self.poll_call_count += 1
        if self._pending_batches:
            return self._pending_batches.pop(0)
        return []

    async def get_token_ids_from_market_data(
        self, asset: str, window_ts: int, timeframe: str = "5m",
    ) -> Optional[dict]:
        return self.market_data_rows.get((asset, int(window_ts)))

    async def update_manual_trade_status(
        self, trade_id: str, status: str, **_kwargs,
    ) -> None:
        self.status_updates.append((trade_id, status))

    async def listen(self, channel: str, callback: Callable) -> None:
        if self.force_listen_failure:
            raise RuntimeError("simulated LISTEN connection failure")
        self._listen_channel = channel
        self._listen_callback = callback
        self._listen_alive = True
        self._listen_ever_succeeded = True

    async def stop_listening(self) -> None:
        self._listen_alive = False
        self._listen_callback = None
        self._listen_channel = None

    def is_listening(self) -> bool:
        return self._listen_alive

    async def ensure_listening(
        self, channel: str, callback: Callable,
    ) -> bool:
        self.ensure_listening_call_count += 1
        if self.is_listening() and self._listen_channel == channel:
            return True
        try:
            await self.listen(channel, callback)
            return True
        except Exception:
            return False

    # ── Test helpers ─────────────────────────────────────────────
    def fire_notify(self, payload: str = "") -> None:
        """Simulate the hub emitting pg_notify. Calls the captured
        callback synchronously the way asyncpg would."""
        assert self._listen_callback is not None, (
            "listen() must have been called before fire_notify()"
        )
        self._listen_callback(
            conn=None, pid=1, channel=self._listen_channel or "",
            payload=payload,
        )

    def drop_listen_connection(self) -> None:
        """Simulate the LISTEN connection being torn down."""
        self._listen_alive = False


class FakePolyClient:
    """Stand-in for `engine.execution.polymarket_client.PolymarketClient`.

    Records place_order calls so tests can assert that the engine
    actually attempted execution, and returns a fake CLOB id with a
    configurable latency to simulate real Polymarket API timing.
    """

    def __init__(self, paper_mode: bool = True, place_order_latency_s: float = 0.0) -> None:
        self.paper_mode = paper_mode
        self._place_order_latency = place_order_latency_s
        self.place_order_calls: list[dict] = []
        self.place_order_timestamps: list[float] = []

    async def place_order(
        self,
        market_slug: str,
        direction: str,
        price: Decimal,
        stake_usd: float,
        token_id: Optional[str] = None,
    ) -> str:
        self.place_order_calls.append({
            "market_slug": market_slug,
            "direction": direction,
            "price": price,
            "stake_usd": stake_usd,
            "token_id": token_id,
        })
        self.place_order_timestamps.append(time.monotonic())
        if self._place_order_latency > 0:
            await asyncio.sleep(self._place_order_latency)
        return f"clob-{len(self.place_order_calls):04d}"


class FakeAlerter:
    """Swallows all alert calls — Telegram is not part of LT-04."""

    def __init__(self) -> None:
        self.alerts: list[tuple[str, str]] = []

    async def send_system_alert(self, message: str, level: str = "info") -> None:
        self.alerts.append((level, message[:80]))


class FakeFiveMinStrategy:
    """Exposes _recent_windows as an empty deque so the ring-buffer
    lookup always misses and we exercise the LT-02 DB fallback."""

    def __init__(self) -> None:
        self._recent_windows: list = []


# ────────────────────────────────────────────────────────────────────
#  Mock orchestrator factory
# ────────────────────────────────────────────────────────────────────


class MockOrchestrator:
    """Minimal orchestrator stand-in that has just the fields the
    manual-trade poller reads. We attach the real
    `Orchestrator._on_manual_trade_notify` and
    `Orchestrator._manual_trade_poller` methods to this object via
    __get__ so we're testing the real code path without having to
    construct a full Orchestrator (which pulls in settings, feeds,
    the CLOB client, etc.)."""

    # Bind real Orchestrator methods as unbound functions. The
    # instance methods will use them via self._manual_trade_poller()
    # and self._on_manual_trade_notify(...).
    _manual_trade_poller = Orchestrator._manual_trade_poller  # type: ignore[assignment]
    _on_manual_trade_notify = Orchestrator._on_manual_trade_notify  # type: ignore[assignment]

    def __init__(
        self,
        *,
        paper_mode: bool = True,
        place_order_latency_s: float = 0.0,
    ) -> None:
        self._db = FakeDB()
        self._poly_client = FakePolyClient(
            paper_mode=paper_mode,
            place_order_latency_s=place_order_latency_s,
        )
        self._alerter = FakeAlerter()
        self._five_min_strategy = FakeFiveMinStrategy()
        self._shutdown_event = asyncio.Event()
        self._manual_trade_notify_event = asyncio.Event()

    async def run_poller(self) -> None:
        """Run the real _manual_trade_poller method as a bound method
        on this mock instance."""
        await self._manual_trade_poller()


def _make_pending_row(
    *,
    trade_id: str = "manual_test_0001",
    direction_raw: str = "UP",
    entry_price: float = 0.52,
    stake_usd: float = 4.0,
    window_ts: int = 1_711_900_800,
    asset: str = "BTC",
) -> dict:
    """Build a row shaped exactly like `DBClient.poll_pending_live_trades`
    returns (see engine/persistence/db_client.py:1108)."""
    return {
        "trade_id": trade_id,
        "window_ts": window_ts,
        "asset": asset,
        "direction": direction_raw,
        "entry_price": entry_price,
        "gamma_up_price": 0.52,
        "gamma_down_price": 0.48,
        "stake_usd": stake_usd,
    }


# ────────────────────────────────────────────────────────────────────
#  Test 1 — NOTIFY fires → poller executes within 500ms
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notify_triggers_execute_within_500ms():
    """Happy path: a NOTIFY arrives and the engine picks up the
    pending row and calls place_order in <500ms from event set.

    This is the core LT-04 invariant: sub-second click-to-execute
    latency end-to-end (ignoring the Polymarket API round trip
    itself, which we can't control). We use paper_mode=False so
    the poller takes the live code path and calls FakePolyClient
    .place_order — this is the real place_order latency contract.
    """
    mock = MockOrchestrator(paper_mode=False)

    # Seed the DB with one pending row AND the market_data fallback
    # so the LT-02 token_id lookup succeeds.
    row = _make_pending_row()
    mock._db.queue_pending([row])
    mock._db.market_data_rows[("BTC", row["window_ts"])] = {
        "up_token_id": "0xdeadbeef_up",
        "down_token_id": "0xdeadbeef_down",
    }

    # Start the poller in a background task.
    poller_task = asyncio.create_task(mock.run_poller())

    try:
        # Give the poller one tick to set up LISTEN (it calls
        # ensure_listening before entering the wait loop).
        await asyncio.sleep(0.05)

        # Fire the NOTIFY — this is what the hub does after INSERT.
        t_notify = time.monotonic()
        mock._db.fire_notify(payload=row["trade_id"])

        # Wait for place_order to land. Poll every 10ms for up to 500ms.
        deadline = t_notify + 0.5
        while time.monotonic() < deadline:
            if mock._poly_client.place_order_calls:
                break
            await asyncio.sleep(0.01)

        elapsed_ms = (time.monotonic() - t_notify) * 1000.0

        assert len(mock._poly_client.place_order_calls) == 1, (
            f"place_order was NOT called within 500ms of NOTIFY. "
            f"status_updates={mock._db.status_updates}, "
            f"elapsed_ms={elapsed_ms:.1f}"
        )
        assert elapsed_ms < 500.0, (
            f"NOTIFY → place_order took {elapsed_ms:.1f}ms, exceeds 500ms target"
        )
        # Verify the place_order call forwarded the right token_id
        # from the LT-02 DB fallback.
        call = mock._poly_client.place_order_calls[0]
        assert call["direction"] == "YES"
        assert call["token_id"] == "0xdeadbeef_up"
    finally:
        mock._shutdown_event.set()
        poller_task.cancel()
        try:
            await poller_task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_notify_paper_mode_fills_within_500ms():
    """Paper-mode variant: status transitions to 'open' within 500ms
    of NOTIFY. In paper mode we never hit place_order (it short-circuits
    to a fake clob_id), so we assert on the status update instead."""
    mock = MockOrchestrator(paper_mode=True)
    row = _make_pending_row(trade_id="manual_paper_0001")
    mock._db.queue_pending([row])
    mock._db.market_data_rows[("BTC", row["window_ts"])] = {
        "up_token_id": "0x_up",
        "down_token_id": "0x_down",
    }

    poller_task = asyncio.create_task(mock.run_poller())
    try:
        await asyncio.sleep(0.05)
        t_notify = time.monotonic()
        mock._db.fire_notify(payload=row["trade_id"])

        deadline = t_notify + 0.5
        while time.monotonic() < deadline:
            if any(
                tid == row["trade_id"] and st == "open"
                for (tid, st) in mock._db.status_updates
            ):
                break
            await asyncio.sleep(0.01)

        elapsed_ms = (time.monotonic() - t_notify) * 1000.0
        transitions = [st for (tid, st) in mock._db.status_updates if tid == row["trade_id"]]
        assert "executing" in transitions, f"missing 'executing', got {transitions}"
        assert "open" in transitions, (
            f"trade never reached 'open' within 500ms — transitions={transitions}"
        )
        assert elapsed_ms < 500.0, f"took {elapsed_ms:.1f}ms, exceeds 500ms target"
    finally:
        mock._shutdown_event.set()
        poller_task.cancel()
        try:
            await poller_task
        except (asyncio.CancelledError, Exception):
            pass


# ────────────────────────────────────────────────────────────────────
#  Test 2 — Stale NOTIFY is a safe no-op
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_notify_is_safe_noop():
    """A NOTIFY fires but poll_pending_live_trades returns zero rows
    (trade_id already executed, or notification was misrouted). The
    poller must NOT crash and must NOT double-execute anything."""
    mock = MockOrchestrator(paper_mode=True)

    # No pending rows — fire_notify should trigger a poll that
    # returns [] and the loop should quietly continue.
    poller_task = asyncio.create_task(mock.run_poller())
    try:
        await asyncio.sleep(0.05)
        mock._db.fire_notify(payload="manual_ghost_0000")
        await asyncio.sleep(0.1)

        assert mock._db.poll_call_count >= 1, (
            "poller never polled after NOTIFY"
        )
        assert mock._poly_client.place_order_calls == [], (
            "stale NOTIFY triggered a spurious place_order!"
        )
        assert mock._db.status_updates == [], (
            f"stale NOTIFY caused status updates: {mock._db.status_updates}"
        )
    finally:
        mock._shutdown_event.set()
        poller_task.cancel()
        try:
            await poller_task
        except (asyncio.CancelledError, Exception):
            pass


# ────────────────────────────────────────────────────────────────────
#  Test 3 — Dropped LISTEN connection → safety-net poll fires
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dropped_listen_falls_through_to_poll():
    """If the LISTEN connection dies mid-run and ensure_listening
    keeps failing, the safety-net 1s poll must still pick up new
    rows. This is the zero-regression guarantee.

    We set force_listen_failure=True so ensure_listening always
    returns False, then queue a pending row. The poller should pick
    it up via the fall-through poll within ~1.1s."""
    mock = MockOrchestrator(paper_mode=True)
    mock._db.force_listen_failure = True  # Every listen call fails.

    row = _make_pending_row(trade_id="manual_dropped_0001")
    mock._db.market_data_rows[("BTC", row["window_ts"])] = {
        "up_token_id": "0x_up",
        "down_token_id": "0x_down",
    }
    # Queue the row AFTER a small delay so we can measure fall-through
    # behavior. We pre-queue; the first poll tick will consume it.
    mock._db.queue_pending([row])

    poller_task = asyncio.create_task(mock.run_poller())
    try:
        t0 = time.monotonic()
        # Give the safety-net poll up to 1.5s (one fall-through + margin).
        while time.monotonic() - t0 < 1.5:
            if any(
                tid == row["trade_id"] and st == "open"
                for (tid, st) in mock._db.status_updates
            ):
                break
            await asyncio.sleep(0.05)

        elapsed = time.monotonic() - t0
        transitions = [
            st for (tid, st) in mock._db.status_updates if tid == row["trade_id"]
        ]
        assert "open" in transitions, (
            f"dropped-LISTEN trade never reached 'open' within 1.5s — "
            f"transitions={transitions} elapsed={elapsed:.2f}s"
        )
        # Sanity check: listen never succeeded, so is_listening() is
        # False and ensure_listening was called ≥ once.
        assert not mock._db.is_listening(), (
            "LISTEN should have failed but shows as alive"
        )
        assert mock._db.ensure_listening_call_count >= 1, (
            "poller never attempted to listen"
        )
    finally:
        mock._shutdown_event.set()
        poller_task.cancel()
        try:
            await poller_task
        except (asyncio.CancelledError, Exception):
            pass


# ────────────────────────────────────────────────────────────────────
#  Test 4 — Ring-buffer miss → LT-02 DB fallback still works
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ring_buffer_miss_falls_back_to_db_lookup():
    """LT-02 regression: the ring buffer in FiveMinVPINStrategy can
    be empty right after engine startup. The poller must fall back
    to the market_data table DB lookup. LT-04 must not break this."""
    mock = MockOrchestrator(paper_mode=True)

    # Ring buffer is empty by default on FakeFiveMinStrategy.
    assert mock._five_min_strategy._recent_windows == []

    row = _make_pending_row(
        trade_id="manual_lt02_0001",
        window_ts=1_712_000_000,
    )
    mock._db.queue_pending([row])

    # Seed the DB fallback row — this is the LT-02 fix path.
    mock._db.market_data_rows[("BTC", row["window_ts"])] = {
        "up_token_id": "0xlt02_up",
        "down_token_id": "0xlt02_down",
    }

    poller_task = asyncio.create_task(mock.run_poller())
    try:
        await asyncio.sleep(0.05)
        mock._db.fire_notify(payload=row["trade_id"])

        # Wait for the status transition.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if any(
                tid == row["trade_id"] and st == "open"
                for (tid, st) in mock._db.status_updates
            ):
                break
            await asyncio.sleep(0.02)

        transitions = [
            st for (tid, st) in mock._db.status_updates if tid == row["trade_id"]
        ]
        assert "open" in transitions, (
            f"LT-02 fallback failed: trade never reached 'open' — "
            f"transitions={transitions}"
        )
        # Critical: failed_no_token must NOT have fired (that's the
        # error the LT-02 fix prevents).
        assert "failed_no_token" not in transitions, (
            "LT-02 DB fallback regression: trade hit failed_no_token"
        )
    finally:
        mock._shutdown_event.set()
        poller_task.cancel()
        try:
            await poller_task
        except (asyncio.CancelledError, Exception):
            pass


# ────────────────────────────────────────────────────────────────────
#  Test 5 — Multiple NOTIFY events → all get processed
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_notifies_all_processed():
    """If three NOTIFYs arrive before the poller wakes up, all three
    rows must be processed in one iteration. asyncio.Event
    coalesces to a single set/clear cycle, but the poller re-fetches
    all pending rows in one call, so the batch semantics should
    naturally drain everything in the first wakeup."""
    mock = MockOrchestrator(paper_mode=True)

    rows = [
        _make_pending_row(
            trade_id=f"manual_multi_{i:04d}",
            window_ts=1_712_100_000 + i * 300,
        )
        for i in range(3)
    ]
    # Queue all three in one poll batch — this matches what the real
    # DB returns for `status='pending_live' LIMIT 5`.
    mock._db.queue_pending(rows)
    for r in rows:
        mock._db.market_data_rows[("BTC", r["window_ts"])] = {
            "up_token_id": f"0x_up_{r['trade_id'][-4:]}",
            "down_token_id": f"0x_down_{r['trade_id'][-4:]}",
        }

    poller_task = asyncio.create_task(mock.run_poller())
    try:
        await asyncio.sleep(0.05)
        # Fire three notifies back-to-back. The event coalesces to
        # one set, and the poller's single poll call returns all
        # three rows — the batch is drained in one wakeup.
        for r in rows:
            mock._db.fire_notify(payload=r["trade_id"])

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            opens = [
                tid for (tid, st) in mock._db.status_updates if st == "open"
            ]
            if len(opens) == len(rows):
                break
            await asyncio.sleep(0.02)

        opens = [tid for (tid, st) in mock._db.status_updates if st == "open"]
        assert len(opens) == len(rows), (
            f"expected {len(rows)} opens, got {len(opens)}: "
            f"{mock._db.status_updates}"
        )
        for r in rows:
            assert r["trade_id"] in opens, (
                f"trade {r['trade_id']} missing from opens={opens}"
            )
    finally:
        mock._shutdown_event.set()
        poller_task.cancel()
        try:
            await poller_task
        except (asyncio.CancelledError, Exception):
            pass


# ────────────────────────────────────────────────────────────────────
#  Test 6 — Poller subscribes to the correct channel on startup
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poller_subscribes_to_notify_channel_on_startup():
    """Sanity check: on startup, the poller calls ensure_listening
    with the 'manual_trade_pending' channel. This catches a refactor
    breakage where the channel name drifts between hub and engine."""
    from persistence.db_client import MANUAL_TRADE_NOTIFY_CHANNEL
    assert MANUAL_TRADE_NOTIFY_CHANNEL == "manual_trade_pending"

    mock = MockOrchestrator(paper_mode=True)
    poller_task = asyncio.create_task(mock.run_poller())
    try:
        await asyncio.sleep(0.1)
        assert mock._db.is_listening(), (
            "poller did not subscribe to LISTEN channel on startup"
        )
        assert mock._db._listen_channel == MANUAL_TRADE_NOTIFY_CHANNEL, (
            f"wrong channel: {mock._db._listen_channel}"
        )
    finally:
        mock._shutdown_event.set()
        poller_task.cancel()
        try:
            await poller_task
        except (asyncio.CancelledError, Exception):
            pass
