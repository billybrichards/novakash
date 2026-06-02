"""Type-strict regression test for registry._write_window_trace signal_evaluations write.

Root cause of the #649 production DataError:

    db.write_signal_evaluation_failed
      error="invalid input for query argument $9: 69021.1 (expected str, got float)"
      error_type=DataError

The production DB column ``signal_evaluations.binance_price`` is TEXT (not DOUBLE
PRECISION).  asyncpg infers column types from the DB schema and rejects a Python
``float`` for a TEXT column.  All existing callers of ``write_signal_evaluation``
passed ``None`` for binance_price, so the ``if data.get("binance_price") is not
None else None`` guard short-circuited to NULL — never reaching the type check.

PR #649 added the FIRST call site that passes a non-None binance_price value
(``surface.current_price``, always a float ~69021.1 for BTC price), triggering
the latent bug.

Fix (in db_client.py + pg_signal_repo.py):
    float(data["binance_price"]) → str(data["binance_price"])

The dict in registry.py is correct as-is (float value is fine — the writer now
coerces to str before handing to asyncpg).

This test catches the bug by using a type-strict fake DB that mimics asyncpg's
behaviour: raises DataError if binance_price is a float (TEXT column), accepts
str.  A plain MagicMock/AsyncMock would accept anything and miss the error.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

_engine = os.path.join(os.path.dirname(__file__), "..", "..", "..")
if _engine not in sys.path:
    sys.path.insert(0, _engine)

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")


# ---------------------------------------------------------------------------
# Type-strict fake DB — mimics asyncpg's column-type enforcement
# ---------------------------------------------------------------------------

class _DataError(Exception):
    """Fake asyncpg DataError for the type-strict stub."""


class _TypeStrictSignalEvalDb:
    """Fake DB whose write_signal_evaluation enforces the prod column types.

    binance_price is TEXT in prod (ordinal_position=10, type=text).
    asyncpg will raise DataError if the caller passes a Python float instead
    of str.  This stub enforces the same rule so the test catches the pre-fix
    bug immediately.
    """

    def __init__(self):
        self.calls: list[dict] = []
        # All other methods that _write_window_trace might invoke
        for method in (
            "update_window_surface_fields",
            "update_window_ensemble_fields",
            "update_signal_evaluations_lgb_v12",
            "update_signal_evaluations_lgb_v9_1",
            "update_signal_evaluations_lgb_v9_2",
            "update_signal_evaluations_lgb_v9_2_post_iso",
            "update_signal_evaluations_lgb_v9_2_eth",
            "update_signal_evaluations_lgb_v9_5_eth",
            "update_signal_evaluations_lgb_v9_5_eth_pure",
            "update_signal_evaluations_lgb_v9_3_btc",
            "update_signal_evaluations_lgb_v9_3_btc_pure",
            "update_signal_evaluations_lgb_v9_2_pure",
            "update_signal_evaluations_lgb_v12_pure",
            "update_signal_evaluations_lgb_v9_5_xrp",
            "update_signal_evaluations_lgb_v9_5_xrp_pure",
            "update_signal_evaluations_tickformer",
            "update_signal_evaluations_tickformer_v16",
            "update_signal_evaluations_tickformer_v17",
            "update_signal_evaluations_tickformer_v18",
            "update_signal_evaluations_v2_meta_gate",
            "update_signal_evaluations_v9_2_meta_gate",
            "update_signal_evaluations_v12_meta_gate",
        ):
            setattr(self, method, AsyncMock())

    async def write_signal_evaluation(self, data: dict) -> None:
        """Type-strict version.  Rejects float for binance_price (TEXT column)."""
        binance_price = data.get("binance_price")
        if binance_price is not None:
            if isinstance(binance_price, float) or isinstance(binance_price, int):
                raise _DataError(
                    f"invalid input for query argument $9: {binance_price!r} "
                    f"(expected str, got {type(binance_price).__name__})"
                )
            # Must be str for the TEXT column
            if not isinstance(binance_price, str):
                raise _DataError(
                    f"binance_price must be str (TEXT column), got {type(binance_price).__name__}"
                )
        self.calls.append(data)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_surface(
    asset: str = "ETH",
    current_price: float = 69021.1,
    open_price: float = 68500.0,
    window_ts: int = 1717372800,
    eval_offset: int = 120,
    delta_pct: float = 0.008,
    delta_binance: Optional[float] = 0.007,
    delta_tiingo: Optional[float] = 0.006,
    delta_chainlink: Optional[float] = 0.005,
    delta_source: str = "tiingo_rest_candle",
    vpin: float = 0.52,
    regime: str = "NORMAL",
    clob_up_bid: Optional[float] = 0.51,
    clob_up_ask: Optional[float] = 0.53,
    clob_down_bid: Optional[float] = 0.43,
    clob_down_ask: Optional[float] = 0.45,
    cg_liq_long: Optional[float] = 1_200_000.0,
    cg_liq_short: Optional[float] = 800_000.0,
    cg_taker_buy_vol: Optional[float] = 5_000_000.0,
    cg_taker_sell_vol: Optional[float] = 4_200_000.0,
    cg_funding_rate: Optional[float] = 0.0001,
    v2_probability_up: Optional[float] = 0.67,
) -> MagicMock:
    s = MagicMock()
    s.asset = asset
    s.timescale = "5m"
    s.window_ts = window_ts
    s.eval_offset = eval_offset
    s.assembled_at = float(window_ts)
    s.current_price = current_price
    s.open_price = open_price
    s.delta_pct = delta_pct
    s.delta_binance = delta_binance
    s.delta_tiingo = delta_tiingo
    s.delta_chainlink = delta_chainlink
    s.delta_source = delta_source
    s.vpin = vpin
    s.regime = regime
    s.clob_up_bid = clob_up_bid
    s.clob_up_ask = clob_up_ask
    s.clob_down_bid = clob_down_bid
    s.clob_down_ask = clob_down_ask
    s.cg_liq_long = cg_liq_long
    s.cg_liq_short = cg_liq_short
    s.cg_taker_buy_vol = cg_taker_buy_vol
    s.cg_taker_sell_vol = cg_taker_sell_vol
    s.cg_funding_rate = cg_funding_rate
    s.v2_probability_up = v2_probability_up
    # Sidecar fields — leave as None/unset so sidecar writers are no-ops
    s.probability_lgb_v9_2 = None
    s.probability_lgb_v12 = None
    return s


async def _invoke_trace(registry: Any, surface: Any) -> None:
    from strategies.registry import StrategyRegistry

    bound = StrategyRegistry._write_window_trace.__get__(registry)
    bound(surface)
    # Drain created asyncio tasks (two passes handles nested tasks)
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def _make_registry(db: Any) -> Any:
    from strategies.registry import StrategyRegistry

    registry = MagicMock()
    registry._db = db
    registry._log_async_write_error = StrategyRegistry._log_async_write_error.__get__(registry)
    registry._surface_trace_data = MagicMock(return_value={})
    registry._trace_repo = MagicMock()
    registry._trace_repo.write_window_evaluation_trace = AsyncMock()
    return registry


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBinancePriceTypeEnforcement:
    """Regression tests for the #649 DataError on binance_price type mismatch."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asset", ["ETH", "XRP", "SOL"])
    async def test_no_type_error_for_non_btc_assets(self, asset):
        """_write_window_trace must NOT raise DataError for ETH/XRP/SOL.

        This is the core regression: before the fix, passing surface.current_price
        (float ~69021.1) for binance_price (TEXT column) raised:
            DataError: invalid input for query argument $9: 69021.1 (expected str, got float)
        """
        db = _TypeStrictSignalEvalDb()
        registry = _make_registry(db)
        surface = _make_surface(asset=asset, current_price=69021.1)

        # Must not raise _DataError
        await _invoke_trace(registry, surface)

        assert len(db.calls) == 1, (
            f"write_signal_evaluation not called for {asset}; "
            f"DataError may have been swallowed by the task callback"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asset", ["ETH", "XRP", "SOL"])
    async def test_binance_price_persisted_as_str(self, asset):
        """binance_price reaches the writer as str (matches the TEXT DB column)."""
        db = _TypeStrictSignalEvalDb()
        registry = _make_registry(db)
        surface = _make_surface(asset=asset, current_price=69021.1)

        await _invoke_trace(registry, surface)

        assert db.calls, "write_signal_evaluation was not called"
        written = db.calls[0]
        bp = written.get("binance_price")
        assert isinstance(bp, str), (
            f"binance_price should be str for TEXT column, got {type(bp).__name__}: {bp!r}"
        )
        # Value should round-trip correctly
        assert float(bp) == pytest.approx(69021.1, rel=1e-6)

    @pytest.mark.asyncio
    async def test_btc_excluded_from_trace_write(self):
        """BTC must NOT trigger write_signal_evaluation from _write_window_trace."""
        db = _TypeStrictSignalEvalDb()
        registry = _make_registry(db)
        surface = _make_surface(asset="BTC", current_price=69021.1)

        await _invoke_trace(registry, surface)

        assert len(db.calls) == 0, (
            "write_signal_evaluation was called for BTC — must be excluded to avoid "
            "race condition with EvaluateStrategiesUseCase writer"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asset", ["ETH", "XRP", "SOL"])
    async def test_none_current_price_stays_none(self, asset):
        """None current_price → None binance_price (no crash, no type error)."""
        db = _TypeStrictSignalEvalDb()
        registry = _make_registry(db)
        surface = _make_surface(asset=asset, current_price=None)

        await _invoke_trace(registry, surface)

        assert len(db.calls) == 1
        assert db.calls[0].get("binance_price") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asset", ["ETH", "XRP", "SOL"])
    async def test_other_float_fields_still_float(self, asset):
        """CLOB and delta fields must remain float (their DB columns are double precision)."""
        db = _TypeStrictSignalEvalDb()
        registry = _make_registry(db)
        surface = _make_surface(
            asset=asset,
            clob_up_bid=0.51,
            delta_pct=0.008,
            vpin=0.55,
        )

        await _invoke_trace(registry, surface)

        assert db.calls
        written = db.calls[0]
        # These should still be numeric (float/None), NOT str
        assert isinstance(written["clob_up_bid"], float), (
            f"clob_up_bid should be float, got {type(written['clob_up_bid'])}"
        )
        assert isinstance(written["delta_pct"], float), (
            f"delta_pct should be float, got {type(written['delta_pct'])}"
        )
        assert isinstance(written["vpin"], float), (
            f"vpin should be float, got {type(written['vpin'])}"
        )
