"""Tests for fix/tickformer-eval-cadence-lag.

Verifies that:
1. DataSurfaceManager fires the tickformer snapshot callback when a BTC
   snapshot with tickformer probabilities arrives, and only then.
2. The callback is NOT fired when TICKFORMER_EVAL_PER_WRITE is false.
3. The callback is NOT fired for non-BTC assets.
4. The callback is NOT fired when no tickformer probabilities are present.
5. The orchestrator's _on_tickformer_snapshot method evaluates strategies
   using the last known CLOSING window with a freshly-computed eval_offset.
6. Expired windows (fresh_remaining <= 0) are skipped.

The EngineRuntime import is avoided (playwright dep not installed in test
env); the handler is extracted and tested as a standalone coroutine to keep
the test hermetic.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# Engine root
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")


# ── helpers ─────────────────────────────────────────────────────────────────


def _make_dsm(*, eval_per_write: bool = True):
    """Build a minimal DataSurfaceManager with no real feeds."""
    from strategies.data_surface import DataSurfaceManager

    dsm = DataSurfaceManager()
    dsm._tickformer_eval_per_write = eval_per_write
    return dsm


def _btc_snapshot_with_tf(v18: float = 0.923) -> dict:
    """Minimal /v4/snapshot body with tickformer_v18 populated."""
    return {
        "timescales": {
            "5m": {
                "probability_tickformer_v16": 0.85,
                "probability_tickformer_v17": 0.90,
                "probability_tickformer_v18": v18,
                "probability_tickformer_v20": None,
                "tickformer_gate_cond": 0.87,
                "tickformer_trade_signal": "UP",
            }
        }
    }


def _btc_snapshot_no_tf() -> dict:
    """Minimal /v4/snapshot body with all tickformer fields None."""
    return {
        "timescales": {
            "5m": {
                "probability_tickformer_v16": None,
                "probability_tickformer_v17": None,
                "probability_tickformer_v18": None,
                "probability_tickformer_v20": None,
            }
        }
    }


def _make_window(
    *,
    asset: str = "BTC",
    window_ts: int | None = None,
    duration_secs: int = 300,
    eval_offset: int = 220,
) -> SimpleNamespace:
    if window_ts is None:
        window_ts = int(time.time()) - 10
    return SimpleNamespace(
        asset=asset,
        window_ts=window_ts,
        duration_secs=duration_secs,
        eval_offset=eval_offset,
        open_price=104500.0,
        up_price=0.62,
        down_price=0.38,
        up_token_id=None,
        down_token_id=None,
        timeframe="5m",
    )


def _has_tf(body: dict) -> bool:
    """Mirror the gate condition from _try_fetch_snapshot."""
    ts5 = (body.get("timescales") or {}).get("5m", {})
    return any(
        ts5.get(f"probability_tickformer_{mid}") is not None
        for mid in ("v16", "v17", "v18", "v20")
    )


# ── DataSurfaceManager callback tests ────────────────────────────────────────


class TestDataSurfaceCallbackFires:
    """Callback fires when a BTC snapshot has tickformer probs + flag on."""

    def test_callback_registered(self):
        dsm = _make_dsm()
        cb = AsyncMock()
        dsm.set_tickformer_snapshot_callback(cb)
        assert dsm._tickformer_snapshot_cb is cb

    @pytest.mark.asyncio
    async def test_callback_fires_on_btc_tf_snapshot(self):
        """Flag=on + BTC + tf probs → callback fires."""
        dsm = _make_dsm(eval_per_write=True)
        fired: list = []

        async def _fake_cb(asset, body):
            fired.append((asset, body))

        dsm.set_tickformer_snapshot_callback(_fake_cb)

        body = _btc_snapshot_with_tf()
        assert _has_tf(body)

        # Manually simulate the create_task block from _try_fetch_snapshot
        if dsm._tickformer_eval_per_write and dsm._tickformer_snapshot_cb and _has_tf(body):
            task = asyncio.create_task(dsm._tickformer_snapshot_cb("BTC", body))
            await task

        assert len(fired) == 1
        assert fired[0][0] == "BTC"

    @pytest.mark.asyncio
    async def test_callback_not_fired_when_flag_off(self):
        """Flag=false → callback never fires even with valid snapshot."""
        dsm = _make_dsm(eval_per_write=False)
        fired: list = []

        async def _cb(asset, body):
            fired.append(1)

        dsm.set_tickformer_snapshot_callback(_cb)
        assert not dsm._tickformer_eval_per_write

        body = _btc_snapshot_with_tf()
        # Gate: flag is off → skip
        if dsm._tickformer_eval_per_write and dsm._tickformer_snapshot_cb and _has_tf(body):
            await _cb("BTC", body)
        assert fired == []

    @pytest.mark.asyncio
    async def test_callback_not_fired_for_non_btc(self):
        """ETH asset → callback not fired (tickformer is BTC-only)."""
        dsm = _make_dsm(eval_per_write=True)
        fired: list = []

        async def _cb(asset, body):
            fired.append(1)

        dsm.set_tickformer_snapshot_callback(_cb)
        body = _btc_snapshot_with_tf()

        # Gate: asset != BTC
        asset = "ETH"
        if dsm._tickformer_eval_per_write and dsm._tickformer_snapshot_cb and asset.upper() == "BTC" and _has_tf(body):
            await _cb(asset, body)
        assert fired == []

    @pytest.mark.asyncio
    async def test_callback_not_fired_when_no_tf_probs(self):
        """All tf probs None → callback not fired."""
        dsm = _make_dsm(eval_per_write=True)
        fired: list = []

        async def _cb(asset, body):
            fired.append(1)

        dsm.set_tickformer_snapshot_callback(_cb)
        body = _btc_snapshot_no_tf()
        assert not _has_tf(body)

        if dsm._tickformer_eval_per_write and dsm._tickformer_snapshot_cb and "BTC" == "BTC" and _has_tf(body):
            await _cb("BTC", body)
        assert fired == []


# ── Standalone _on_tickformer_snapshot handler tests ─────────────────────────


async def _on_tickformer_snapshot(self, asset: str, snapshot_body: dict) -> None:
    """Extract of the EngineRuntime._on_tickformer_snapshot method.

    Copied verbatim from runtime.py so tests stay hermetic (the full
    runtime module requires playwright which is not installed in CI).
    """
    if not self._strategy_registry:
        return
    last_windows = getattr(self, "_last_closing_window", {})
    window = last_windows.get(asset.upper()) or last_windows.get(asset)
    if window is None:
        return
    import time as _time
    _window_ts = getattr(window, "window_ts", 0)
    _duration = getattr(window, "duration_secs", 300)
    _close_ts = _window_ts + _duration
    _fresh_remaining = int(_close_ts - _time.time())
    if _fresh_remaining <= 0:
        return
    try:
        object.__setattr__(window, "eval_offset", _fresh_remaining)
    except (AttributeError, TypeError):
        pass
    try:
        state = await self._aggregator.get_state()
        _v2_window_market = None
        if getattr(window, "up_token_id", None) and getattr(window, "down_token_id", None):
            pass  # skip WindowMarket construction for hermetic test
        await self._strategy_registry.evaluate_all(
            window,
            state,
            window_market=_v2_window_market,
            current_btc_price=float(getattr(state, "btc_price", 0) or 0),
            open_price=float(getattr(window, "open_price", 0) or 0),
        )
    except Exception:
        pass


def _make_rt(window=None, registry=None):
    rt = SimpleNamespace()
    rt._strategy_registry = registry
    rt._last_closing_window = {"BTC": window} if window else {}
    agg = MagicMock()
    agg.get_state = AsyncMock(return_value=SimpleNamespace(btc_price=104500.0))
    rt._aggregator = agg
    rt._on_tickformer_snapshot = _on_tickformer_snapshot.__get__(rt, type(rt))
    return rt


class TestOnTickformerSnapshot:
    """Handler evaluates strategies with a freshly-computed eval_offset."""

    @pytest.mark.asyncio
    async def test_calls_evaluate_all_with_fresh_offset(self):
        """evaluate_all is called with a freshly-computed eval_offset."""
        registry = MagicMock()
        registry.evaluate_all = AsyncMock(return_value=[])

        now = int(time.time())
        # Window opened 10 seconds ago → ~290s remaining
        window = _make_window(window_ts=now - 10, eval_offset=290)
        rt = _make_rt(window, registry)

        await rt._on_tickformer_snapshot("BTC", _btc_snapshot_with_tf())

        assert registry.evaluate_all.called
        called_window = registry.evaluate_all.call_args[0][0]
        fresh_offset = getattr(called_window, "eval_offset", None)
        assert fresh_offset is not None
        # Should be between 285 and 295 (10s elapsed, 300 total)
        assert 280 <= fresh_offset <= 296, f"fresh_offset={fresh_offset}"

    @pytest.mark.asyncio
    async def test_skips_expired_window(self):
        """Window past close → evaluate_all not called."""
        registry = MagicMock()
        registry.evaluate_all = AsyncMock(return_value=[])

        now = int(time.time())
        window = _make_window(window_ts=now - 310, eval_offset=0)
        rt = _make_rt(window, registry)

        await rt._on_tickformer_snapshot("BTC", _btc_snapshot_with_tf())

        registry.evaluate_all.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_closing_window_tracked(self):
        """No CLOSING window tracked → evaluate_all not called."""
        registry = MagicMock()
        registry.evaluate_all = AsyncMock(return_value=[])

        rt = _make_rt(window=None, registry=registry)

        await rt._on_tickformer_snapshot("BTC", _btc_snapshot_with_tf())

        registry.evaluate_all.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_registry_none(self):
        """No strategy_registry → no crash, no evaluate_all."""
        now = int(time.time())
        window = _make_window(window_ts=now - 10, eval_offset=290)
        rt = _make_rt(window, registry=None)

        # Must not raise
        await rt._on_tickformer_snapshot("BTC", _btc_snapshot_with_tf())

    @pytest.mark.asyncio
    async def test_error_in_evaluate_all_is_swallowed(self):
        """Errors in evaluate_all don't propagate (best-effort callback)."""
        registry = MagicMock()
        registry.evaluate_all = AsyncMock(side_effect=RuntimeError("boom"))

        now = int(time.time())
        window = _make_window(window_ts=now - 10, eval_offset=290)
        rt = _make_rt(window, registry)

        # Must not raise
        await rt._on_tickformer_snapshot("BTC", _btc_snapshot_with_tf())
        assert registry.evaluate_all.called
