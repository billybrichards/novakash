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
async def test_gtc_open_order_counts_as_successful_placement():
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

    assert result.success is True
    assert result.order_id == "0xgtc-open"
    assert result.execution_mode == "gtc"
    assert result.fill_price == 0.74
    assert result.fill_size is not None
