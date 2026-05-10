from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from adapters.execution.fak_ladder_executor import (
    FAKLadderExecutor,
    GTC_EXPIRY_GRACE_SECONDS,
    _WINDOW_DURATION_SECONDS,
)

RUNTIME_PATH = (
    Path(__file__).parent.parent.parent.parent / "infrastructure" / "runtime.py"
)


class _FakePolyClient:
    def __init__(self):
        self.place_order_calls = 0
        self.status_calls = 0
        self.rfq_calls = 0
        # Configurable best-ask for the price-ladder probe added in PR #361
        # (aggressive sell exit). Default to None → ladder logs a benign
        # "book_error: no ask" but proceeds with the configured FAK
        # ladder prices, matching the prior test contract before the
        # probe was added.
        self.best_ask_price: float | None = None

    async def place_rfq_order(self, **kwargs):
        self.rfq_calls += 1
        return None, None

    async def place_order(self, **kwargs):
        self.place_order_calls += 1
        return "0xgtc-open"

    async def get_order_status(self, order_id):
        self.status_calls += 1
        return {"status": "LIVE", "size_matched": 0}

    async def get_clob_best_ask(self, token_id: str):
        """Stub for the FAK price-ladder probe (PR #361).

        Returns the configured best-ask. None → caller logs ``book_error``
        but proceeds with the static ladder. Tests that exercise the
        probe explicitly should set ``self.best_ask_price`` first.
        """
        return self.best_ask_price


@pytest.mark.asyncio
async def test_gtc_fallback_disabled_by_default_returns_failure_no_order_placed():
    """Default behaviour after audit-task #218 (2026-04-17 phantom-fill incident).

    With GTC fallback disabled (the new default), the executor MUST NOT
    place a resting GTC order when FAK + RFQ are exhausted. It returns
    success=False with execution_mode='none' so the caller does not record
    a phantom trade in the trades table.
    """
    client = _FakePolyClient()
    executor = FAKLadderExecutor(
        poly_client=client,
        gtc_poll_interval=0,
        gtc_max_wait=0,
        # enable_gtc_fallback omitted -> default False
    )

    result = await executor.execute_order(
        token_id="token",
        side="YES",
        stake_usd=1.8,
        entry_cap=0.71,
        price_floor=0.30,
    )

    assert result.success is False
    assert result.execution_mode == "none"
    assert result.fill_price is None
    assert result.fill_size is None
    assert result.order_id is None
    assert "gtc_fallback_disabled" in (result.failure_reason or "")
    assert client.place_order_calls == 0  # GTC place_order never invoked
    assert client.status_calls == 0  # poll loop never invoked


@pytest.mark.asyncio
async def test_gtc_open_order_counts_as_successful_placement_when_explicitly_enabled():
    """Legacy behaviour preserved when caller explicitly opts in.

    Used by experimentation / back-compat. Caller must pass
    enable_gtc_fallback=True or set FAK_LADDER_ENABLE_GTC=true env var.
    """
    client = _FakePolyClient()
    executor = FAKLadderExecutor(
        poly_client=client,
        gtc_poll_interval=0,
        gtc_max_wait=0,
        enable_gtc_fallback=True,
    )

    result = await executor.execute_order(
        token_id="token",
        side="YES",
        stake_usd=1.8,
        entry_cap=0.71,
        price_floor=0.30,
    )

    assert result.success is True
    assert result.order_id == "0xgtc-open"
    assert result.execution_mode == "gtc_resting"
    assert result.fill_price is None
    assert result.fill_size is None


@pytest.mark.asyncio
async def test_env_var_re_enables_gtc_fallback(monkeypatch):
    """Env var FAK_LADDER_ENABLE_GTC=true re-enables legacy behaviour
    without changing call sites — for back-compat / staged rollout."""
    monkeypatch.setenv("FAK_LADDER_ENABLE_GTC", "true")
    client = _FakePolyClient()
    executor = FAKLadderExecutor(
        poly_client=client,
        gtc_poll_interval=0,
        gtc_max_wait=0,
        # enable_gtc_fallback omitted -> falls back to env var -> True
    )

    result = await executor.execute_order(
        token_id="token",
        side="YES",
        stake_usd=1.8,
        entry_cap=0.71,
        price_floor=0.30,
    )

    assert result.success is True
    assert result.execution_mode == "gtc_resting"


@pytest.mark.asyncio
async def test_env_var_falsy_keeps_gtc_disabled(monkeypatch):
    """Falsy env values keep the safe default (GTC disabled)."""
    for falsy in ("", "0", "false", "no", "off", "False"):
        monkeypatch.setenv("FAK_LADDER_ENABLE_GTC", falsy)
        client = _FakePolyClient()
        executor = FAKLadderExecutor(
            poly_client=client,
            gtc_poll_interval=0,
            gtc_max_wait=0,
        )

        result = await executor.execute_order(
            token_id="token",
            side="YES",
            stake_usd=1.8,
            entry_cap=0.71,
            price_floor=0.30,
        )

        assert result.success is False, f"falsy env {falsy!r} should keep disabled"
        assert result.execution_mode == "none"
        assert client.place_order_calls == 0


# ──────────────────────────────────────────────────────────────────────
# FAK_LADDER_ENABLE_RFQ toggle (2026-04-17, incident: RFQ endpoint
# returning 404s with "market not found for token X" for valid token IDs,
# burning the circuit-breaker budget).
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rfq_enabled_by_default_when_env_unset(monkeypatch):
    """RFQ default is True — with no env override, Phase 2 runs."""
    monkeypatch.delenv("FAK_LADDER_ENABLE_RFQ", raising=False)
    client = _FakePolyClient()
    executor = FAKLadderExecutor(
        poly_client=client,
        gtc_poll_interval=0,
        gtc_max_wait=0,
    )
    assert executor._enable_rfq is True


@pytest.mark.asyncio
async def test_rfq_disabled_by_env_false(monkeypatch):
    """FAK_LADDER_ENABLE_RFQ=false disables Phase 2 RFQ call."""
    monkeypatch.setenv("FAK_LADDER_ENABLE_RFQ", "false")
    client = _FakePolyClient()
    executor = FAKLadderExecutor(
        poly_client=client,
        gtc_poll_interval=0,
        gtc_max_wait=0,
    )
    result = await executor.execute_order(
        token_id="token",
        side="YES",
        stake_usd=1.8,
        entry_cap=0.71,
        price_floor=0.30,
    )
    # With RFQ off + GTC off (default) + FAK exhausted, executor returns
    # success=False, execution_mode='none'. Crucially: RFQ was NOT called.
    assert result.success is False
    assert result.execution_mode == "none"
    assert client.rfq_calls == 0


@pytest.mark.asyncio
async def test_rfq_disabled_by_constructor_arg():
    """Explicit enable_rfq=False overrides env, guarantees Phase 2 skipped."""
    client = _FakePolyClient()
    executor = FAKLadderExecutor(
        poly_client=client,
        gtc_poll_interval=0,
        gtc_max_wait=0,
        enable_rfq=False,
    )
    result = await executor.execute_order(
        token_id="token",
        side="YES",
        stake_usd=1.8,
        entry_cap=0.71,
        price_floor=0.30,
    )
    assert client.rfq_calls == 0
    assert result.success is False


@pytest.mark.asyncio
async def test_rfq_env_case_insensitive_and_truthy_variants(monkeypatch):
    """Only explicit falsy variants disable. Anything else → enabled."""
    # Falsy variants disable:
    for falsy in ("0", "false", "no", "off", "False", "NO", "OFF"):
        monkeypatch.setenv("FAK_LADDER_ENABLE_RFQ", falsy)
        ex = FAKLadderExecutor(poly_client=_FakePolyClient())
        assert ex._enable_rfq is False, f"env {falsy!r} should disable"
    # Truthy / unknown variants keep default enabled:
    for truthy in ("1", "true", "yes", "on", "True", "anything"):
        monkeypatch.setenv("FAK_LADDER_ENABLE_RFQ", truthy)
        ex = FAKLadderExecutor(poly_client=_FakePolyClient())
        assert ex._enable_rfq is True, f"env {truthy!r} should stay enabled"
    # Blank → enabled (default):
    monkeypatch.setenv("FAK_LADDER_ENABLE_RFQ", "")
    ex = FAKLadderExecutor(poly_client=_FakePolyClient())
    assert ex._enable_rfq is True


@pytest.mark.asyncio
async def test_rfq_and_gtc_both_enabled_still_works_end_to_end(monkeypatch):
    """Both phases enabled — the legacy happy path still routes through."""
    monkeypatch.setenv("FAK_LADDER_ENABLE_GTC", "true")
    monkeypatch.setenv("FAK_LADDER_ENABLE_RFQ", "true")
    client = _FakePolyClient()
    executor = FAKLadderExecutor(
        poly_client=client,
        gtc_poll_interval=0,
        gtc_max_wait=0,
    )
    result = await executor.execute_order(
        token_id="token",
        side="YES",
        stake_usd=1.8,
        entry_cap=0.71,
        price_floor=0.30,
    )
    # RFQ attempted, returned None,None (fake). Then GTC placed.
    assert client.rfq_calls >= 1
    assert client.place_order_calls >= 1
    assert result.success is True
    assert result.execution_mode == "gtc_resting"


# ── GTC lifecycle hardening tests (2026-05-10) ────────────────────────────


class _FakePolyClientWithCancel(_FakePolyClient):
    """Extends fake client with cancel_order tracking."""

    def __init__(self):
        super().__init__()
        self.cancel_calls: list[str] = []

    async def cancel_order(self, order_id: str) -> bool:
        self.cancel_calls.append(order_id)
        return True


@pytest.mark.asyncio
async def test_gtc_dedup_blocks_second_attempt():
    """A second execute_order for the same token_id+side is blocked if a GTC
    is already resting on the book.  The second call returns success=False
    with failure_reason containing 'gtc_dedup_blocked', and place_order is
    NOT called a second time.
    """
    client = _FakePolyClient()
    executor = FAKLadderExecutor(
        poly_client=client,
        gtc_poll_interval=0,
        gtc_max_wait=0,
        enable_gtc_fallback=True,
    )

    # First call — places GTC, returns gtc_resting.
    result1 = await executor.execute_order(
        token_id="token-abc-yes",
        side="YES",
        stake_usd=5.0,
        entry_cap=0.65,
        price_floor=0.30,
    )
    assert result1.success is True
    assert result1.execution_mode == "gtc_resting"
    assert client.place_order_calls == 1

    # Second call for the same token+side — dedup must block.
    result2 = await executor.execute_order(
        token_id="token-abc-yes",
        side="YES",
        stake_usd=5.0,
        entry_cap=0.65,
        price_floor=0.30,
    )
    assert result2.success is False
    assert "gtc_dedup_blocked" in (result2.failure_reason or "")
    # place_order must NOT have been called a second time.
    assert client.place_order_calls == 1


@pytest.mark.asyncio
async def test_gtc_dedup_allows_different_side():
    """Dedup is per (token_id, side). YES and NO are independent slots."""
    client = _FakePolyClient()
    executor = FAKLadderExecutor(
        poly_client=client,
        gtc_poll_interval=0,
        gtc_max_wait=0,
        enable_gtc_fallback=True,
    )

    result_yes = await executor.execute_order(
        token_id="token-abc",
        side="YES",
        stake_usd=5.0,
        entry_cap=0.65,
        price_floor=0.30,
    )
    result_no = await executor.execute_order(
        token_id="token-abc",
        side="NO",
        stake_usd=5.0,
        entry_cap=0.65,
        price_floor=0.30,
    )
    # Both should succeed — different sides are independent.
    assert result_yes.success is True
    assert result_no.success is True
    assert client.place_order_calls == 2


@pytest.mark.asyncio
async def test_gtc_window_expired_cancel_removes_resting_order():
    """cancel_expired_gtc_orders() cancels the resting GTC and clears the
    dedup registry so a future GTC for the same token_id+side is allowed.
    """
    client = _FakePolyClientWithCancel()
    executor = FAKLadderExecutor(
        poly_client=client,
        gtc_poll_interval=0,
        gtc_max_wait=0,
        enable_gtc_fallback=True,
    )

    # Place a GTC.
    result = await executor.execute_order(
        token_id="token-expiring",
        side="YES",
        stake_usd=5.0,
        entry_cap=0.65,
        price_floor=0.30,
    )
    assert result.success is True
    assert result.execution_mode == "gtc_resting"
    assert ("token-expiring", "YES") in executor._active_gtc

    # Simulate window close.
    await executor.cancel_expired_gtc_orders(["token-expiring"])

    # Cancel must have been called with the order_id.
    assert len(client.cancel_calls) == 1
    assert "0xgtc-open" in client.cancel_calls[0]

    # Dedup registry must be cleared — new GTC attempt should be allowed.
    assert ("token-expiring", "YES") not in executor._active_gtc


@pytest.mark.asyncio
async def test_gtc_window_expired_cancel_noop_when_no_active_gtc():
    """cancel_expired_gtc_orders with no active GTC is a clean no-op."""
    client = _FakePolyClientWithCancel()
    executor = FAKLadderExecutor(
        poly_client=client,
        gtc_poll_interval=0,
        gtc_max_wait=0,
        enable_gtc_fallback=True,
    )

    # No GTC placed — cancel on an unrecognised token_id must not raise.
    await executor.cancel_expired_gtc_orders(["token-never-used"])
    assert len(client.cancel_calls) == 0


# ── Periodic expiry logic: get_expired_token_ids (PR #525 follow-up) ─────────


@pytest.mark.asyncio
async def test_cancel_expired_gtc_skips_unexpired():
    """get_expired_token_ids() must NOT include a GTC whose close_ts is in the future.

    Token placed at t=0, window closes at t=300. At t=310 (< close_ts + grace=30)
    the GTC is NOT yet expired.
    """
    client = _FakePolyClientWithCancel()
    executor = FAKLadderExecutor(
        poly_client=client,
        gtc_poll_interval=0,
        gtc_max_wait=0,
        enable_gtc_fallback=True,
    )

    # Seed the registry directly — avoids running execute_order (FAK/RFQ path).
    now = time.time()
    close_ts = now + 200  # window closes in 200s from now
    executor._active_gtc[("token-future", "YES")] = {
        "order_id": "0xorder-future",
        "placed_at": now,
        "close_ts": close_ts,
    }

    # Query at now — close_ts + grace is still in the future.
    expired = executor.get_expired_token_ids(now=now)
    assert "token-future" not in expired, (
        "Unexpired GTC (close_ts in future) must not appear in expired list"
    )


@pytest.mark.asyncio
async def test_cancel_expired_gtc_includes_expired_with_grace():
    """get_expired_token_ids() must include a GTC past close_ts + grace.

    Token placed at t=0, close_ts=t+300. At t = close_ts + grace + 1
    the GTC must be returned as expired.
    """
    client = _FakePolyClientWithCancel()
    executor = FAKLadderExecutor(
        poly_client=client,
        gtc_poll_interval=0,
        gtc_max_wait=0,
        enable_gtc_fallback=True,
    )

    past_close = time.time() - _WINDOW_DURATION_SECONDS - GTC_EXPIRY_GRACE_SECONDS - 5
    executor._active_gtc[("token-stale", "YES")] = {
        "order_id": "0xorder-stale",
        "placed_at": past_close - 100,
        "close_ts": past_close,
    }

    expired = executor.get_expired_token_ids()
    assert "token-stale" in expired, (
        "GTC past close_ts + grace must be in the expired list"
    )


@pytest.mark.asyncio
async def test_cancel_expired_gtc_clears_registry():
    """cancel_expired_gtc_orders() removes the entry from _active_gtc.

    After cancellation, the dedup registry must be clear so a future
    GTC placement for the same (token_id, side) is permitted.
    """
    client = _FakePolyClientWithCancel()
    executor = FAKLadderExecutor(
        poly_client=client,
        gtc_poll_interval=0,
        gtc_max_wait=0,
        enable_gtc_fallback=True,
    )

    # Seed the registry with a stale entry.
    past_close = time.time() - _WINDOW_DURATION_SECONDS - GTC_EXPIRY_GRACE_SECONDS - 5
    executor._active_gtc[("token-clear", "YES")] = {
        "order_id": "0xorder-clear",
        "placed_at": past_close - 100,
        "close_ts": past_close,
    }
    assert ("token-clear", "YES") in executor._active_gtc

    # Run cancel.
    await executor.cancel_expired_gtc_orders(["token-clear"])

    # Registry must be empty for that token+side.
    assert ("token-clear", "YES") not in executor._active_gtc, (
        "_active_gtc must not retain the entry after cancel_expired_gtc_orders"
    )
    # cancel_order must have been called once.
    assert len(client.cancel_calls) == 1


def test_periodic_task_starts_at_engine_boot():
    """The GTC cancel loop must be wired as an asyncio task at engine startup.

    Static source check — if a refactor removes the create_task call for
    ``_gtc_cancel_expired_loop`` from ``infrastructure/runtime.py``, this
    test fails immediately (no need to spin up the full runtime).
    """
    src = RUNTIME_PATH.read_text()

    # Verify the method definition exists.
    assert "async def _gtc_cancel_expired_loop" in src, (
        "_gtc_cancel_expired_loop method missing from runtime.py"
    )

    # Verify it is scheduled as an asyncio task.
    pattern = re.compile(
        r'asyncio\.create_task\(\s*self\._gtc_cancel_expired_loop\(\)',
        re.DOTALL,
    )
    assert pattern.search(src), (
        "asyncio.create_task(self._gtc_cancel_expired_loop()) missing from "
        "runtime.py — periodic GTC cancel loop must be started at engine boot"
    )
