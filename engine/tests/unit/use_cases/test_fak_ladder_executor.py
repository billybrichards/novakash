from __future__ import annotations

import pytest

from adapters.execution.fak_ladder_executor import FAKLadderExecutor


class _FakePolyClient:
    def __init__(self):
        self.place_order_calls = 0
        self.status_calls = 0
        self.rfq_calls = 0

    async def place_rfq_order(self, **kwargs):
        self.rfq_calls += 1
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
