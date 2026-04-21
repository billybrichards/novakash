"""Tests for FAKLadderExecutor total-elapsed timeout (v6 late-fill defense).

Context
-------
2026-04-21 incident: window 1776803100, order 0x9cb301f2 filled at T-16
(16s before 5-min close) while the strategy's min_offset_sec=30 timing
gate should have blocked it. The FAK ladder chewed through ~30s of
wall-clock across Phase 1 (FOK retries) + RFQ + fallback paths, well
past the timing gate's cutoff.

These tests cover the defence-in-depth timeout cap wired in
``engine/adapters/execution/fak_ladder_executor.py``: a single max
elapsed budget for the whole ``execute_order()`` call, overridable via
``FAK_LADDER_MAX_ELAPSED_S`` env var.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from adapters.execution.fak_ladder_executor import FAKLadderExecutor


# ─── Fakes ────────────────────────────────────────────────────────────────


class _FakePolyClient:
    """Minimal PolymarketClientPort stub.

    FOKLadder.execute() inside the executor reaches into ``get_clob_best_ask``
    and ``_submit``. We stub the public surface and rely on the sleep_fn
    parameter injected via monkeypatch to control 'time elapsed' without
    real sleeps.
    """

    def __init__(self) -> None:
        self.place_order_calls = 0
        self.status_calls = 0
        self.rfq_calls = 0
        self.best_ask = 0.65

    async def get_clob_best_ask(self, token_id):
        return self.best_ask

    async def place_rfq_order(self, **kwargs):
        self.rfq_calls += 1
        return None, None

    async def place_order(self, **kwargs):
        self.place_order_calls += 1
        return "0xgtc-unused"

    async def get_order_status(self, order_id):
        self.status_calls += 1
        return {"status": "LIVE", "size_matched": 0}


# ─── Helpers ──────────────────────────────────────────────────────────────


class _ClockDriver:
    """Monkey-patchable replacement for ``time.time`` + ``asyncio.sleep``.

    - ``now()``       — returns current fake wall clock.
    - ``advance(s)``  — bumps the clock; asyncio.sleep() inside the
                         executor is patched to invoke this instead of
                         sleeping for real.
    """

    def __init__(self, start: float = 1_000.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += float(seconds)


# ─── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ladder_exits_after_timeout(monkeypatch):
    """If the FAK ladder consumes more than the configured budget, the
    executor must return an ExecutionResult with execution_mode='none'
    and failure_reason starting with 'fak_ladder_timeout', without
    dispatching any GTC placement."""
    clock = _ClockDriver()

    # Patch time.time in both the executor module and the FOKLadder module
    # so elapsed math inside the executor sees the advancing fake clock.
    import adapters.execution.fak_ladder_executor as mod
    import execution.fok_ladder as fok_mod

    monkeypatch.setattr(mod.time, "time", clock.now)

    # Patch asyncio.sleep inside FOKLadder to advance the clock instead
    # of waiting — simulating a slow ladder that burns wall-clock budget.
    async def _fake_sleep(seconds):
        # Simulate each FOK retry costing ~20s of wall-clock so the
        # ladder as a whole blows past the 20s budget.
        clock.advance(20.0)

    monkeypatch.setattr(fok_mod.asyncio, "sleep", _fake_sleep)

    # Force FOK attempts to return no fill so the ladder always exhausts.
    async def _no_fill(self, token_id, price, stake, order_type, attempt):
        # Each submit itself adds some latency — advance the clock
        # a little bit to mimic CLOB round-trip.
        clock.advance(2.0)
        return {"size_matched": 0, "order_id": None}

    monkeypatch.setattr(fok_mod.FOKLadder, "_submit", _no_fill)

    client = _FakePolyClient()
    executor = FAKLadderExecutor(
        poly_client=client,
        gtc_poll_interval=0,
        gtc_max_wait=0,
        max_ladder_elapsed_s=20.0,  # explicit, bypass env
    )

    result = await executor.execute_order(
        token_id="token-abc",
        side="YES",
        stake_usd=2.0,
        entry_cap=0.65,
        price_floor=0.30,
    )

    assert result.success is False
    assert result.execution_mode == "none"
    assert result.failure_reason is not None
    assert result.failure_reason.startswith("fak_ladder_timeout")
    # GTC must NOT be placed after a timeout
    assert client.place_order_calls == 0


@pytest.mark.asyncio
async def test_ladder_completes_under_timeout(monkeypatch):
    """If the first FAK attempt fills immediately, the timeout guard
    must not interfere with the happy-path fill."""
    clock = _ClockDriver()

    import adapters.execution.fak_ladder_executor as mod
    import execution.fok_ladder as fok_mod

    monkeypatch.setattr(mod.time, "time", clock.now)

    # First attempt returns a filled order — clock barely advances.
    async def _quick_fill(self, token_id, price, stake, order_type, attempt):
        clock.advance(0.1)
        shares = stake / max(price, 0.01)
        return {
            "size_matched": shares,
            "order_id": "0xfilled-first-try",
        }

    monkeypatch.setattr(fok_mod.FOKLadder, "_submit", _quick_fill)

    client = _FakePolyClient()
    executor = FAKLadderExecutor(
        poly_client=client,
        gtc_poll_interval=0,
        gtc_max_wait=0,
        max_ladder_elapsed_s=20.0,
    )

    result = await executor.execute_order(
        token_id="token-ok",
        side="YES",
        stake_usd=2.0,
        entry_cap=0.65,
        price_floor=0.30,
    )

    assert result.success is True
    assert result.execution_mode == "fak"
    assert result.failure_reason is None
    assert result.fill_size and result.fill_size > 0


def test_env_override_max_elapsed_parses(monkeypatch):
    """FAK_LADDER_MAX_ELAPSED_S env var overrides the default."""
    monkeypatch.setenv("FAK_LADDER_MAX_ELAPSED_S", "30")
    executor = FAKLadderExecutor(poly_client=_FakePolyClient())
    assert executor._max_ladder_elapsed_s == 30.0


def test_env_override_malformed_falls_back_to_default(monkeypatch):
    """Malformed FAK_LADDER_MAX_ELAPSED_S must not crash the executor —
    it should fall back to the hard-coded default."""
    monkeypatch.setenv("FAK_LADDER_MAX_ELAPSED_S", "not-a-number")
    from adapters.execution.fak_ladder_executor import _DEFAULT_MAX_LADDER_ELAPSED_S

    executor = FAKLadderExecutor(poly_client=_FakePolyClient())
    assert executor._max_ladder_elapsed_s == _DEFAULT_MAX_LADDER_ELAPSED_S


def test_default_max_elapsed_is_twenty_seconds():
    """Default budget must be 20s — below every 5m-strategy min_offset."""
    from adapters.execution.fak_ladder_executor import _DEFAULT_MAX_LADDER_ELAPSED_S

    assert _DEFAULT_MAX_LADDER_ELAPSED_S == 20.0
