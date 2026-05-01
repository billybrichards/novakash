"""SQL contract tests for PgStrategyComparisonRepo.

Uses the _FakePool/_FakeConn pattern from test_outcome_writer to verify
that the INSERT and DELETE SQL is issued with correct parameters — without
a real database.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from adapters.persistence.pg_strategy_comparison_repo import PgStrategyComparisonRepo
from domain.strategy_comparison.entities import StrategyComparison
from domain.strategy_comparison.metrics import StrategyMetrics
from domain.strategy_comparison.value_objects import (
    DirectionFilter,
    RegimeFilter,
    TBand,
    WindowPeriod,
)


class _FakeConn:
    def __init__(self) -> None:
        self.execute_calls: list[dict[str, Any]] = []
        self.copy_calls: list[dict[str, Any]] = []

    async def execute(self, sql: str, *args, **kwargs):
        self.execute_calls.append({"sql": sql, "args": args})
        return "OK"

    async def copy_records_to_table(self, table: str, *, records, columns):
        self.copy_calls.append({"table": table, "records": list(records), "columns": columns})
        return len(list(records)) if not isinstance(records, list) else len(records)


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self):
        outer = self

        class _CM:
            async def __aenter__(self_inner):
                return outer.conn

            async def __aexit__(self_inner, *exc):
                return None

        return _CM()


def _snapshot_at() -> datetime:
    return datetime(2026, 5, 1, 12, tzinfo=timezone.utc)


def _make_entity(strategy_id: str = "v12_lgb_combo") -> StrategyComparison:
    return StrategyComparison(
        snapshot_at=_snapshot_at(),
        strategy_id=strategy_id,
        asset="BTC",
        timeframe="5m",
        window_period=WindowPeriod.H24,
        t_band=TBand.ALL,
        direction_filter=DirectionFilter.ALL,
        regime_filter=RegimeFilter.ALL,
        metrics=StrategyMetrics(
            n_fires=10,
            n_wins=7,
            n_losses=3,
            n_pending=0,
            wr_pct=70.0,
            wilson_low=39.7,
            wilson_high=89.2,
            avg_fill=0.745,
            median_fill=0.74,
            avg_stake_usd=7.5,
            real_net_pnl_usd=12.34,
            real_pnl_per_fire=1.23,
            daily_run_rate_usd=9.87,
        ),
    )


class TestPgStrategyComparisonRepoSave:
    @pytest.mark.asyncio
    async def test_save_empty_list_is_noop(self):
        pool = _FakePool()
        repo = PgStrategyComparisonRepo(pool=pool)
        count = await repo.save([])
        assert count == 0
        assert len(pool.conn.execute_calls) == 0
        assert len(pool.conn.copy_calls) == 0

    @pytest.mark.asyncio
    async def test_save_single_entity_writes_to_db(self):
        pool = _FakePool()
        repo = PgStrategyComparisonRepo(pool=pool)
        entity = _make_entity()
        count = await repo.save([entity])
        assert count == 1

    @pytest.mark.asyncio
    async def test_save_uses_copy_records_for_bulk(self):
        """Bulk copy (not row-by-row execute) must be used for snapshot writes."""
        pool = _FakePool()
        repo = PgStrategyComparisonRepo(pool=pool)
        entities = [_make_entity("v12_lgb_combo"), _make_entity("v9_basic")]
        await repo.save(entities)
        assert len(pool.conn.copy_calls) == 1
        assert pool.conn.copy_calls[0]["table"] == "strategy_comparison"
        assert len(pool.conn.copy_calls[0]["records"]) == 2

    @pytest.mark.asyncio
    async def test_save_record_contains_all_required_columns(self):
        pool = _FakePool()
        repo = PgStrategyComparisonRepo(pool=pool)
        entity = _make_entity()
        await repo.save([entity])
        cols = pool.conn.copy_calls[0]["columns"]
        for required in (
            "snapshot_at", "strategy_id", "asset", "timeframe",
            "window_period", "t_band", "direction_filter", "regime_filter",
            "n_fires", "n_wins", "n_losses", "n_pending",
            "wr_pct", "wilson_low", "wilson_high",
            "avg_fill", "median_fill", "avg_stake_usd",
            "real_net_pnl_usd", "real_pnl_per_fire", "daily_run_rate_usd",
        ):
            assert required in cols, f"Missing column: {required}"

    @pytest.mark.asyncio
    async def test_save_record_values_match_entity(self):
        pool = _FakePool()
        repo = PgStrategyComparisonRepo(pool=pool)
        entity = _make_entity("v12_lgb_combo")
        await repo.save([entity])
        records = pool.conn.copy_calls[0]["records"]
        cols = pool.conn.copy_calls[0]["columns"]
        rec = dict(zip(cols, records[0]))
        assert rec["strategy_id"] == "v12_lgb_combo"
        assert rec["window_period"] == "24h"
        assert rec["t_band"] == "all"
        assert rec["n_fires"] == 10
        assert rec["n_wins"] == 7


class TestPgStrategyComparisonRepoPrune:
    @pytest.mark.asyncio
    async def test_prune_issues_delete_sql(self):
        pool = _FakePool()
        repo = PgStrategyComparisonRepo(pool=pool)
        await repo.prune_older_than_days(30)
        assert len(pool.conn.execute_calls) == 1
        sql = pool.conn.execute_calls[0]["sql"]
        assert "DELETE" in sql.upper()
        assert "strategy_comparison" in sql
