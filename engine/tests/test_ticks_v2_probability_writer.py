"""
Tests for ticks_v2_probability writer — audit #396.

Verifies that:
  1. TickRecorder.record_v2_probability() issues an INSERT INTO ticks_v2_probability.
  2. The connection is acquired from the SAME asyncpg pool (canonical DATABASE_URL),
     never from a Railway-specific connection.
  3. All key fields (probability_up, probability_raw, model_version, features JSONB)
     are correctly forwarded to the query.
  4. The method is fire-and-forget: asyncpg errors are logged but never propagate.
  5. DBClient warns at __init__ when DATABASE_URL doesn't contain the prod RDS host.
  6. ticks_v2_probability DDL is emitted inside ensure_tables().
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import pathlib
import sys
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ────────────────────────────────────────────────────────────────────
#  Direct module load helpers (avoid importing optional heavy deps)
# ────────────────────────────────────────────────────────────────────

_ENGINE_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_module(rel_path: str, name: str):
    """Load a module from a relative engine path without triggering __init__ side-effects."""
    mod_path = _ENGINE_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, mod_path)
    assert spec is not None and spec.loader is not None, f"Cannot load {mod_path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ────────────────────────────────────────────────────────────────────
#  Mock asyncpg pool / connection
# ────────────────────────────────────────────────────────────────────


class _MockConnection:
    """asyncpg Connection stand-in that records execute/executemany calls."""

    def __init__(self) -> None:
        self.execute_calls: List[tuple] = []
        self.executemany_calls: List[tuple] = []
        self._raise_on_execute: Optional[Exception] = None

    async def execute(self, query: str, *args: Any) -> None:
        if self._raise_on_execute:
            raise self._raise_on_execute
        self.execute_calls.append((query, args))

    async def executemany(self, query: str, rows: Any) -> None:
        self.executemany_calls.append((query, rows))


class _AcquireCtx:
    def __init__(self, conn: _MockConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _MockConnection:
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _MockPool:
    """asyncpg Pool stand-in."""

    def __init__(self) -> None:
        self.conn = _MockConnection()

    def acquire(self) -> _AcquireCtx:
        return _AcquireCtx(self.conn)


# ────────────────────────────────────────────────────────────────────
#  Load TickRecorder
# ────────────────────────────────────────────────────────────────────

_tick_rec_mod = _load_module(
    "persistence/tick_recorder.py",
    "_tick_recorder_under_test",
)
TickRecorder = _tick_rec_mod.TickRecorder


# ════════════════════════════════════════════════════════════════════
#  1.  INSERT INTO ticks_v2_probability is called
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_record_v2_probability_calls_insert() -> None:
    """record_v2_probability() must issue an INSERT INTO ticks_v2_probability."""
    pool = _MockPool()
    recorder = TickRecorder(pool=pool)

    result = {
        "probability_up": 0.72,
        "probability_raw": 0.68,
        "model_version": "v9.2-lgb",
    }

    await recorder.record_v2_probability(
        result=result,
        asset="BTC",
        seconds_to_close=88,
        features_dict={"eval_offset": 88, "vpin": 0.45},
    )

    assert pool.conn.execute_calls, "execute() was never called — INSERT missing"
    query, args = pool.conn.execute_calls[0]
    assert "INSERT INTO ticks_v2_probability" in query, (
        f"Expected INSERT INTO ticks_v2_probability, got: {query[:120]}"
    )


# ════════════════════════════════════════════════════════════════════
#  2.  Field values are forwarded correctly
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_record_v2_probability_fields() -> None:
    """All key columns (probability_up, probability_raw, model_version, features)
    must be passed to the query positional args."""
    pool = _MockPool()
    recorder = TickRecorder(pool=pool)

    features = {"eval_offset": 60, "vpin": 0.55, "regime": "chop"}
    result = {
        "probability_up": 0.65,
        "probability_raw": 0.63,
        "model_version": "v9.1-lgb",
    }

    await recorder.record_v2_probability(
        result=result,
        asset="ETH",
        seconds_to_close=60,
        features_dict=features,
    )

    _query, args = pool.conn.execute_calls[0]

    # args are positional: (ts, asset, seconds_to_close, model_version,
    #                        probability_up, probability_raw, features_json)
    assert args[1] == "ETH", f"asset mismatch: {args[1]}"
    assert args[2] == 60, f"seconds_to_close mismatch: {args[2]}"
    assert args[3] == "v9.1-lgb", f"model_version mismatch: {args[3]}"
    assert abs(float(args[4]) - 0.65) < 1e-6, f"probability_up mismatch: {args[4]}"
    assert abs(float(args[5]) - 0.63) < 1e-6, f"probability_raw mismatch: {args[5]}"

    # features_json must be valid JSON containing the original keys
    features_json = args[6]
    assert features_json is not None, "features JSONB arg is None"
    parsed = json.loads(features_json)
    assert parsed["eval_offset"] == 60
    assert parsed["vpin"] == pytest.approx(0.55)


# ════════════════════════════════════════════════════════════════════
#  3.  Fire-and-forget: asyncpg errors do NOT propagate
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_record_v2_probability_error_swallowed() -> None:
    """asyncpg errors inside record_v2_probability must not propagate to caller."""
    pool = _MockPool()
    pool.conn._raise_on_execute = RuntimeError("simulated asyncpg error")
    recorder = TickRecorder(pool=pool)

    # Must not raise
    await recorder.record_v2_probability(
        result={"probability_up": 0.7},
        asset="BTC",
        seconds_to_close=45,
    )


# ════════════════════════════════════════════════════════════════════
#  4.  No-op when pool is None
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_record_v2_probability_no_pool() -> None:
    """record_v2_probability() is a no-op when the pool is None."""
    recorder = TickRecorder(pool=None)
    # Must not raise
    await recorder.record_v2_probability(
        result={"probability_up": 0.6},
        asset="BTC",
        seconds_to_close=30,
    )


# ════════════════════════════════════════════════════════════════════
#  5.  No-op when result is empty or missing probability_up
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_record_v2_probability_missing_prob_up_is_noop() -> None:
    """record_v2_probability() is a no-op if probability_up is absent."""
    pool = _MockPool()
    recorder = TickRecorder(pool=pool)

    await recorder.record_v2_probability(result={}, asset="BTC", seconds_to_close=60)
    assert not pool.conn.execute_calls, "Should have been a no-op"

    await recorder.record_v2_probability(result=None, asset="BTC", seconds_to_close=60)  # type: ignore[arg-type]
    assert not pool.conn.execute_calls, "Should have been a no-op"


# ════════════════════════════════════════════════════════════════════
#  6.  ensure_tables() emits ticks_v2_probability DDL
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ensure_tables_includes_v2_probability_ddl() -> None:
    """ensure_tables() must CREATE TABLE IF NOT EXISTS ticks_v2_probability."""
    pool = _MockPool()
    recorder = TickRecorder(pool=pool)
    await recorder.ensure_tables()

    all_statements = " ".join(q for q, _ in pool.conn.execute_calls)
    assert "ticks_v2_probability" in all_statements, (
        "ticks_v2_probability DDL not found in ensure_tables() output.\n"
        f"Statements seen: {[q[:80] for q, _ in pool.conn.execute_calls]}"
    )


# ════════════════════════════════════════════════════════════════════
#  7.  Single pool — no separate Railway connection created
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_record_v2_probability_uses_injected_pool_only() -> None:
    """record_v2_probability MUST use the injected pool — not create a new connection.

    This is the core regression guard for audit #396: the Railway-only writer
    used a separate DATABASE_URL (RAILWAY_DATABASE_URL) rather than the canonical
    pool. We verify that asyncpg.create_pool and asyncpg.connect are never called
    by the recorder code path.
    """
    pool = _MockPool()
    recorder = TickRecorder(pool=pool)

    import asyncpg  # noqa: F401 — available in engine venv

    with patch("asyncpg.create_pool", new_callable=AsyncMock) as mock_create_pool, \
         patch("asyncpg.connect", new_callable=AsyncMock) as mock_connect:

        await recorder.record_v2_probability(
            result={"probability_up": 0.75, "model_version": "v9.2"},
            asset="BTC",
            seconds_to_close=90,
        )

        mock_create_pool.assert_not_called()
        mock_connect.assert_not_called()

    # And the INSERT still happened via our mock pool
    assert pool.conn.execute_calls, "INSERT was not issued via the injected pool"


# ════════════════════════════════════════════════════════════════════
#  8.  DBClient warns when DATABASE_URL is not prod RDS
# ════════════════════════════════════════════════════════════════════


def test_db_client_warns_on_non_rds_dsn() -> None:
    """DBClient.__init__ must call log.warning when DATABASE_URL does not contain
    the prod RDS host fragment ('novakash-pg-prod').

    This is the audit #396 regression guard: if someone accidentally sets
    DATABASE_URL to Railway or a local DB, the structlog warning fires at startup
    so ops can catch it before the sidecar writers silently write 0 rows to RDS.
    """
    mock_settings = MagicMock()
    mock_settings.database_url = "postgresql://postgres:pass@localhost:5432/testdb"

    db_client_mod = _load_module(
        "persistence/db_client.py",
        "_db_client_under_test",
    )

    warning_calls: List[tuple] = []

    def _capture_warning(*args: Any, **kwargs: Any) -> None:
        warning_calls.append((args, kwargs))

    with patch.object(db_client_mod, "asyncpg", MagicMock()), \
         patch.object(db_client_mod.log, "warning", side_effect=_capture_warning):
        db_client_mod.DBClient(settings=mock_settings)

    assert warning_calls, "log.warning was never called — audit #396 guard missing"
    # The warning event name should indicate a non-RDS DSN
    event_names = [a[0][0] if a[0] else "" for a in warning_calls]
    assert any("non_rds" in name or "rds" in name.lower() for name in event_names), (
        f"Expected a non-RDS warning event. Got: {event_names}"
    )


# ════════════════════════════════════════════════════════════════════
#  9.  No warning when DATABASE_URL is prod RDS
# ════════════════════════════════════════════════════════════════════


def test_db_client_no_warning_on_rds_dsn() -> None:
    """No 'non_rds_dsn' warning when DATABASE_URL contains the prod RDS host."""
    mock_settings = MagicMock()
    mock_settings.database_url = (
        "postgresql://postgres:secret@"
        "novakash-pg-prod.cpmisy2asv71.ca-central-1.rds.amazonaws.com:5432/novakash"
    )

    db_client_mod = _load_module(
        "persistence/db_client.py",
        "_db_client_rds_test",
    )

    warning_calls: List[tuple] = []

    def _capture_warning(*args: Any, **kwargs: Any) -> None:
        warning_calls.append((args, kwargs))

    with patch.object(db_client_mod, "asyncpg", MagicMock()), \
         patch.object(db_client_mod.log, "warning", side_effect=_capture_warning):
        db_client_mod.DBClient(settings=mock_settings)

    rds_warning_events = [
        a[0][0] for a, _ in warning_calls
        if a and "non_rds" in a[0][0].lower()
    ]
    assert not rds_warning_events, (
        "Unexpected non-RDS warning for a legitimate RDS DSN: "
        + str(rds_warning_events)
    )
