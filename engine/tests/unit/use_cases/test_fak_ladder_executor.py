from __future__ import annotations

import pytest

from adapters.execution.fak_ladder_executor import FAKLadderExecutor


class _FakePolyClient:
    def __init__(self):
        self.place_order_calls = 0
        self.status_calls = 0

    async def place_rfq_order(self, **kwargs):
        return None, None

    async def place_order(self, **kwargs):
        self.place_order_calls += 1
        return "0xgtc-open"

    async def get_order_status(self, order_id):
        self.status_calls += 1
        return {"status": "LIVE", "size_matched": 0}


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
