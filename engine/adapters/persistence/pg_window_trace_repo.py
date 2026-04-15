"""PostgreSQL repository for structured window/gate traces."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

import asyncpg
import structlog

from domain.ports import WindowTraceRepository
from domain.value_objects import GateCheckTrace, WindowEvaluationTrace

log = structlog.get_logger(__name__)


class PgWindowTraceRepository(WindowTraceRepository):
    """asyncpg-backed repository for window evaluation and gate traces."""

    def __init__(
        self,
        pool: Optional[asyncpg.Pool] = None,
        db_client: Optional[object] = None,
    ) -> None:
        self._pool = pool
        self._db_client = db_client

    def _get_pool(self) -> Optional[asyncpg.Pool]:
        if self._pool:
            return self._pool
        if self._db_client:
            return getattr(self._db_client, "_pool", None)
        return None

    @staticmethod
    def _norm_offset(offset: Optional[int]) -> int:
        return int(offset) if offset is not None else -1

    @staticmethod
    def _ts(epoch: float) -> datetime:
        if epoch:
            return datetime.fromtimestamp(epoch, tz=timezone.utc)
        return datetime.now(timezone.utc)

    async def ensure_tables(self) -> None:
        pool = self._get_pool()
        if not pool:
            return
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS window_evaluation_traces (
                        asset        VARCHAR(10) NOT NULL,
                        window_ts    BIGINT NOT NULL,
                        timeframe    VARCHAR(10) NOT NULL,
                        eval_offset  INTEGER NOT NULL,
                        surface_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        assembled_at TIMESTAMPTZ NOT NULL,
                        created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (asset, window_ts, timeframe, eval_offset)
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS gate_check_traces (
                        asset         VARCHAR(10) NOT NULL,
                        window_ts     BIGINT NOT NULL,
                        timeframe     VARCHAR(10) NOT NULL,
                        eval_offset   INTEGER NOT NULL,
                        strategy_id   VARCHAR(64) NOT NULL,
                        gate_order    INTEGER NOT NULL,
                        gate_name     VARCHAR(64) NOT NULL,
                        passed        BOOLEAN NOT NULL,
                        mode          VARCHAR(10) NOT NULL,
                        action        VARCHAR(10) NOT NULL,
                        direction     VARCHAR(10),
                        reason        TEXT,
                        skip_reason   TEXT,
                        observed_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        config_json   JSONB NOT NULL DEFAULT '{}'::jsonb,
                        evaluated_at  TIMESTAMPTZ NOT NULL,
                        created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (
                            asset, window_ts, timeframe, eval_offset,
                            strategy_id, gate_order
                        )
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_gate_check_traces_window
                    ON gate_check_traces (asset, window_ts, timeframe, eval_offset)
                    """
                )
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_gate_check_traces_strategy
                    ON gate_check_traces (strategy_id, timeframe, evaluated_at)
                    """
                )
            log.info("pg_window_trace_repo.tables_ensured")
        except Exception as exc:
            log.warning(
                "pg_window_trace_repo.ensure_tables_failed", error=str(exc)[:200]
            )

    async def write_window_evaluation_trace(self, trace: WindowEvaluationTrace) -> None:
        pool = self._get_pool()
        if not pool:
            return
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO window_evaluation_traces (
                        asset, window_ts, timeframe, eval_offset,
                        surface_json, assembled_at
                    ) VALUES ($1, $2, $3, $4, $5::jsonb, $6)
                    ON CONFLICT (asset, window_ts, timeframe, eval_offset)
                    DO UPDATE SET
                        surface_json = EXCLUDED.surface_json,
                        assembled_at = EXCLUDED.assembled_at
                    """,
                    trace.asset,
                    trace.window_ts,
                    trace.timeframe,
                    self._norm_offset(trace.eval_offset),
                    json.dumps(trace.surface_data or {}),
                    self._ts(trace.assembled_at),
                )
        except Exception as exc:
            log.warning(
                "pg_window_trace_repo.write_window_trace_failed",
                error=str(exc)[:200],
            )

    async def get_window_evaluation_trace(
        self,
        asset: str,
        window_ts: int,
        timeframe: str,
        eval_offset: Optional[int] = None,
    ) -> Optional[WindowEvaluationTrace]:
        pool = self._get_pool()
        if not pool:
            return None
        try:
            async with pool.acquire() as conn:
                if eval_offset is None:
                    row = await conn.fetchrow(
                        """
                        SELECT asset, window_ts, timeframe, eval_offset,
                               surface_json, assembled_at
                        FROM window_evaluation_traces
                        WHERE asset = $1 AND window_ts = $2 AND timeframe = $3
                        ORDER BY eval_offset DESC
                        LIMIT 1
                        """,
                        asset,
                        window_ts,
                        timeframe,
                    )
                else:
                    row = await conn.fetchrow(
                        """
                        SELECT asset, window_ts, timeframe, eval_offset,
                               surface_json, assembled_at
                        FROM window_evaluation_traces
                        WHERE asset = $1 AND window_ts = $2 AND timeframe = $3 AND eval_offset = $4
                        LIMIT 1
                        """,
                        asset,
                        window_ts,
                        timeframe,
                        self._norm_offset(eval_offset),
                    )
            if not row:
                return None
            return WindowEvaluationTrace(
                asset=row["asset"],
                window_ts=row["window_ts"],
                timeframe=row["timeframe"],
                eval_offset=None if row["eval_offset"] == -1 else row["eval_offset"],
                surface_data=row["surface_json"] or {},
                assembled_at=row["assembled_at"].timestamp()
                if row["assembled_at"]
                else 0.0,
            )
        except Exception as exc:
            log.warning(
                "pg_window_trace_repo.read_window_trace_failed",
                error=str(exc)[:200],
            )
            return None

    async def write_gate_check_traces(self, traces: list[GateCheckTrace]) -> None:
        pool = self._get_pool()
        if not pool or not traces:
            return
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    for trace in traces:
                        await conn.execute(
                            """
                            INSERT INTO gate_check_traces (
                                asset, window_ts, timeframe, eval_offset,
                                strategy_id, gate_order, gate_name,
                                passed, mode, action, direction,
                                reason, skip_reason, observed_json,
                                config_json, evaluated_at
                            ) VALUES (
                                $1, $2, $3, $4,
                                $5, $6, $7,
                                $8, $9, $10, $11,
                                $12, $13, $14::jsonb,
                                $15::jsonb, $16
                            )
                            ON CONFLICT (
                                asset, window_ts, timeframe, eval_offset,
                                strategy_id, gate_order
                            ) DO UPDATE SET
                                gate_name = EXCLUDED.gate_name,
                                passed = EXCLUDED.passed,
                                mode = EXCLUDED.mode,
                                action = EXCLUDED.action,
                                direction = EXCLUDED.direction,
                                reason = EXCLUDED.reason,
                                skip_reason = EXCLUDED.skip_reason,
                                observed_json = EXCLUDED.observed_json,
                                config_json = EXCLUDED.config_json,
                                evaluated_at = EXCLUDED.evaluated_at
                            """,
                            trace.asset,
                            trace.window_ts,
                            trace.timeframe,
                            self._norm_offset(trace.eval_offset),
                            trace.strategy_id,
                            trace.gate_order,
                            trace.gate_name,
                            trace.passed,
                            trace.mode,
                            trace.action,
                            trace.direction,
                            trace.reason,
                            trace.skip_reason,
                            json.dumps(trace.observed_data or {}),
                            json.dumps(trace.config_data or {}),
                            self._ts(trace.evaluated_at),
                        )
        except Exception as exc:
            log.warning(
                "pg_window_trace_repo.write_gate_checks_failed",
                error=str(exc)[:200],
            )

    async def get_gate_check_traces(
        self,
        asset: str,
        window_ts: int,
        timeframe: str,
    ) -> list[GateCheckTrace]:
        pool = self._get_pool()
        if not pool:
            return []
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT asset, window_ts, timeframe, eval_offset,
                           strategy_id, gate_order, gate_name,
                           passed, mode, action, direction,
                           reason, skip_reason, observed_json,
                           config_json, evaluated_at
                    FROM gate_check_traces
                    WHERE asset = $1 AND window_ts = $2 AND timeframe = $3
                    ORDER BY eval_offset DESC, strategy_id, gate_order ASC
                    """,
                    asset,
                    window_ts,
                    timeframe,
                )
            return [
                GateCheckTrace(
                    asset=row["asset"],
                    window_ts=row["window_ts"],
                    timeframe=row["timeframe"],
                    eval_offset=None
                    if row["eval_offset"] == -1
                    else row["eval_offset"],
                    strategy_id=row["strategy_id"],
                    gate_order=row["gate_order"],
                    gate_name=row["gate_name"],
                    passed=row["passed"],
                    mode=row["mode"],
                    action=row["action"],
                    direction=row["direction"],
                    reason=row["reason"] or "",
                    skip_reason=row["skip_reason"],
                    observed_data=row["observed_json"] or {},
                    config_data=row["config_json"] or {},
                    evaluated_at=row["evaluated_at"].timestamp()
                    if row["evaluated_at"]
                    else 0.0,
                )
                for row in rows
            ]
        except Exception as exc:
            log.warning(
                "pg_window_trace_repo.read_gate_checks_failed",
                error=str(exc)[:200],
            )
            return []

    async def get_window_evaluation_traces_in_range(
        self,
        *,
        asset: str,
        timeframe: str,
        start_window_ts: int,
        end_window_ts: int,
    ) -> list[WindowEvaluationTrace]:
        pool = self._get_pool()
        if not pool:
            return []
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT asset, window_ts, timeframe, eval_offset,
                           surface_json, assembled_at
                    FROM window_evaluation_traces
                    WHERE asset = $1
                      AND timeframe = $2
                      AND window_ts BETWEEN $3 AND $4
                    ORDER BY window_ts DESC, eval_offset DESC
                    """,
                    asset,
                    timeframe,
                    start_window_ts,
                    end_window_ts,
                )
            return [
                WindowEvaluationTrace(
                    asset=row["asset"],
                    window_ts=row["window_ts"],
                    timeframe=row["timeframe"],
                    eval_offset=None
                    if row["eval_offset"] == -1
                    else row["eval_offset"],
                    surface_data=row["surface_json"] or {},
                    assembled_at=row["assembled_at"].timestamp()
                    if row["assembled_at"]
                    else 0.0,
                )
                for row in rows
            ]
        except Exception as exc:
            log.warning(
                "pg_window_trace_repo.read_window_trace_range_failed",
                error=str(exc)[:200],
            )
            return []
