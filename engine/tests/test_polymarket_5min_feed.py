import asyncio
import time

import pytest

from data.feeds.polymarket_5min import Polymarket5MinFeed, WindowInfo


@pytest.mark.asyncio
async def test_emit_window_signal_does_not_block_and_preserves_eval_offset():
    received = asyncio.Event()
    seen = {}

    async def slow_callback(window: WindowInfo) -> None:
        await asyncio.sleep(0.05)
        seen["eval_offset"] = window.eval_offset
        seen["window_ts"] = window.window_ts
        received.set()

    feed = Polymarket5MinFeed(on_window_signal=slow_callback)
    window = WindowInfo(
        window_ts=123456, asset="BTC", duration_secs=300, eval_offset=62
    )

    start = time.perf_counter()
    await feed._emit_window_signal(window)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.02

    window.eval_offset = 60

    await asyncio.wait_for(received.wait(), timeout=0.2)
    assert seen == {"eval_offset": 62, "window_ts": 123456}
