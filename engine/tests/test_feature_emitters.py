"""
Tests for engine/signals/feature_emitters.py — the DB-backed emitters
for v9.3 BTC + v9.5 ETH+XRP feature coverage (PR #582).

These tests focus on the soft-failure contract: every emitter must
return None (or all-None dict for multi-output) on EVERY error path,
because the calling site in `five_min_vpin._evaluate_window` cannot
tolerate exceptions inside the scoring critical path.

We mock the asyncpg pool with a thin fake — these are unit tests, not
integration tests. The `tests/integration/` directory is where we'd put
end-to-end variants once an RDS fixture exists.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from typing import Any, Optional

import pytest

from signals.feature_emitters import (
    DEFAULT_PRE_EVENT_SECONDS,
    compute_clob_pre_aggregates,
    compute_oi_delta_cumulative,
)


# ────────────────────────────────────────────────────────────────────
#  Pool fakes — minimal asyncpg-pool surface for unit tests
# ────────────────────────────────────────────────────────────────────


class _FakeConnection:
    def __init__(self, fetchrow_result: Any = None, raise_exc: Optional[Exception] = None):
        self._fetchrow_result = fetchrow_result
        self._raise_exc = raise_exc
        self.calls: list[tuple] = []

    async def fetchrow(self, sql: str, *args):
        self.calls.append((sql, args))
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._fetchrow_result


class _FakeAcquireCtx:
    def __init__(self, conn: _FakeConnection):
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakePool:
    def __init__(self, conn: _FakeConnection):
        self._conn = conn

    def acquire(self):
        return _FakeAcquireCtx(self._conn)


def _make_pool(fetchrow_result: Any = None, raise_exc: Optional[Exception] = None) -> _FakePool:
    return _FakePool(_FakeConnection(fetchrow_result=fetchrow_result, raise_exc=raise_exc))


# ────────────────────────────────────────────────────────────────────
#  compute_oi_delta_cumulative
# ────────────────────────────────────────────────────────────────────


class TestComputeOiDeltaCumulative:
    @pytest.mark.asyncio
    async def test_none_pool_returns_none(self):
        result = await compute_oi_delta_cumulative(
            pool=None, asset="BTC", window_ts=1748000000
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_none_window_ts_returns_none(self):
        pool = _make_pool(fetchrow_result={"s": 0.5})
        result = await compute_oi_delta_cumulative(
            pool=pool, asset="BTC", window_ts=None
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_asset_returns_none(self):
        pool = _make_pool(fetchrow_result={"s": 0.5})
        result = await compute_oi_delta_cumulative(
            pool=pool, asset="", window_ts=1748000000
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_happy_path_returns_sum(self):
        pool = _make_pool(fetchrow_result={"s": -0.0345})
        result = await compute_oi_delta_cumulative(
            pool=pool, asset="ETH", window_ts=1748000000
        )
        assert result == pytest.approx(-0.0345)

    @pytest.mark.asyncio
    async def test_zero_sum_is_valid(self):
        # 0.0 is a real signal ("net-flat OI") — must NOT degrade to None.
        pool = _make_pool(fetchrow_result={"s": 0.0})
        result = await compute_oi_delta_cumulative(
            pool=pool, asset="BTC", window_ts=1748000000
        )
        assert result == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_null_sum_returns_none(self):
        # Empty window → SUM(...) is NULL → asyncpg gives us None.
        pool = _make_pool(fetchrow_result={"s": None})
        result = await compute_oi_delta_cumulative(
            pool=pool, asset="XRP", window_ts=1748000000
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_db_exception_returns_none(self):
        pool = _make_pool(raise_exc=RuntimeError("connection lost"))
        result = await compute_oi_delta_cumulative(
            pool=pool, asset="BTC", window_ts=1748000000
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_passes_window_ts_as_bigint(self):
        # Smoke that the SQL gets the right positional args; ensures
        # any future signature drift fails this test loudly.
        conn = _FakeConnection(fetchrow_result={"s": 0.1})
        pool = _FakePool(conn)
        await compute_oi_delta_cumulative(
            pool=pool, asset="BTC", window_ts=1748000000
        )
        assert len(conn.calls) == 1
        _, args = conn.calls[0]
        assert args[0] == "BTC"
        assert args[1] == 1748000000
        # arg[2] is `now` (datetime) — verify type, not value
        assert isinstance(args[2], _dt.datetime)


# ────────────────────────────────────────────────────────────────────
#  compute_clob_pre_aggregates
# ────────────────────────────────────────────────────────────────────


class TestComputeClobPreAggregates:
    EMPTY_KEYS = (
        "clob_pre_imbalance",
        "clob_pre_vig",
        "clob_up_pre_stdev",
        "clob_dn_pre_stdev",
        "clob_up_pre_n",
    )

    @pytest.mark.asyncio
    async def test_none_pool_returns_empty(self):
        result = await compute_clob_pre_aggregates(
            pool=None, asset="BTC", window_ts=1748000000
        )
        for k in self.EMPTY_KEYS:
            assert result[k] is None

    @pytest.mark.asyncio
    async def test_none_window_ts_returns_empty(self):
        pool = _make_pool(fetchrow_result={"imb": 0.1, "vig": 0.02, "up_std": 0.001, "dn_std": 0.001, "n": 10})
        result = await compute_clob_pre_aggregates(
            pool=pool, asset="BTC", window_ts=None
        )
        for k in self.EMPTY_KEYS:
            assert result[k] is None

    @pytest.mark.asyncio
    async def test_zero_window_returns_empty(self):
        pool = _make_pool(fetchrow_result=None)
        result = await compute_clob_pre_aggregates(
            pool=pool, asset="BTC", window_ts=1748000000, pre_event_seconds=0
        )
        for k in self.EMPTY_KEYS:
            assert result[k] is None

    @pytest.mark.asyncio
    async def test_happy_path_returns_all_floats(self):
        pool = _make_pool(fetchrow_result={
            "imb": 0.123,
            "vig": 0.045,
            "up_std": 0.0034,
            "dn_std": 0.0028,
            "n": 42,
        })
        result = await compute_clob_pre_aggregates(
            pool=pool, asset="BTC", window_ts=1748000000
        )
        assert result["clob_pre_imbalance"] == pytest.approx(0.123)
        assert result["clob_pre_vig"] == pytest.approx(0.045)
        assert result["clob_up_pre_stdev"] == pytest.approx(0.0034)
        assert result["clob_dn_pre_stdev"] == pytest.approx(0.0028)
        assert result["clob_up_pre_n"] == pytest.approx(42.0)

    @pytest.mark.asyncio
    async def test_empty_window_yields_zero_n(self):
        # No CLOB ticks observed → SQL gives all NULL for the aggregates
        # AND a non-NULL count of 0. `clob_up_pre_n` should be 0.0 (a
        # valid signal), not None. The other aggregates stay None.
        pool = _make_pool(fetchrow_result={
            "imb": None, "vig": None, "up_std": None, "dn_std": None, "n": 0,
        })
        result = await compute_clob_pre_aggregates(
            pool=pool, asset="BTC", window_ts=1748000000
        )
        assert result["clob_pre_imbalance"] is None
        assert result["clob_pre_vig"] is None
        assert result["clob_up_pre_stdev"] is None
        assert result["clob_dn_pre_stdev"] is None
        assert result["clob_up_pre_n"] == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_db_exception_returns_empty(self):
        pool = _make_pool(raise_exc=ValueError("bad SQL"))
        result = await compute_clob_pre_aggregates(
            pool=pool, asset="ETH", window_ts=1748000000
        )
        for k in self.EMPTY_KEYS:
            assert result[k] is None

    @pytest.mark.asyncio
    async def test_token_id_filter_changes_sql_path(self):
        # When BOTH token_ids supplied, the narrower SQL runs with 5
        # positional args. Without token_ids, the broader SQL runs with
        # 3 positional args. Smoke that the args reflect the choice.
        conn = _FakeConnection(fetchrow_result={
            "imb": 0.1, "vig": 0.02, "up_std": 0.001, "dn_std": 0.001, "n": 5,
        })
        pool = _FakePool(conn)
        await compute_clob_pre_aggregates(
            pool=pool, asset="BTC", window_ts=1748000000,
            up_token_id="0xUP", down_token_id="0xDN",
        )
        assert len(conn.calls) == 1
        _, args = conn.calls[0]
        assert args == ("BTC", 1748000000, DEFAULT_PRE_EVENT_SECONDS, "0xUP", "0xDN")

    @pytest.mark.asyncio
    async def test_default_pre_event_window_is_60s(self):
        # Pin the default — if someone changes it without bumping the
        # training-side query, this test fails loudly.
        assert DEFAULT_PRE_EVENT_SECONDS == 60
