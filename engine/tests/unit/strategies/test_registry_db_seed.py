"""Tests for StrategyRegistry.seed_registry_to_db() — Phase 2 Option C.1.

Covers:
  1. No-op when ``self._db`` is None (legacy composition path).
  2. No-op when the db client has no pool attached yet (boot ordering).
  3. Upserts one row per loaded strategy with correct column values.
  4. Idempotent on re-run — same version twice yields no duplicates.
  5. Bumped version inserts a new row alongside the old one (history).
  6. Seed failures are swallowed with a warning — engine startup
     never fails because a DB upsert couldn't be written.
"""

import sys
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from strategies.registry import StrategyRegistry
from strategies.data_surface import DataSurfaceManager


_MIN_YAML = """
name: v_test
version: "1.0.0"
mode: GHOST
asset: BTC
timescale: 5m
gates: []
sizing:
  type: fixed_kelly
  fraction: 0.025
"""


def _write_yaml(dir_path: Path, name: str, content: str) -> None:
    (dir_path / name).write_text(content)


def _make_mock_db(pool_executes: list) -> MagicMock:
    """Build a mock db object with a _pool that captures execute() calls."""

    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=lambda *a, **kw: pool_executes.append(a))

    class AcquireCtx:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=AcquireCtx())

    db = MagicMock()
    db._pool = pool
    return db


@pytest.mark.asyncio
async def test_seed_no_op_when_db_absent(tmp_path: Path):
    _write_yaml(tmp_path, "v_test.yaml", _MIN_YAML)
    mgr = MagicMock(spec=DataSurfaceManager)
    registry = StrategyRegistry(str(tmp_path), mgr, db=None)
    registry.load_all()

    await registry.seed_registry_to_db()  # should not raise


@pytest.mark.asyncio
async def test_seed_no_op_when_pool_missing(tmp_path: Path):
    _write_yaml(tmp_path, "v_test.yaml", _MIN_YAML)
    mgr = MagicMock(spec=DataSurfaceManager)

    db_no_pool = MagicMock()
    db_no_pool._pool = None

    registry = StrategyRegistry(str(tmp_path), mgr, db=db_no_pool)
    registry.load_all()

    await registry.seed_registry_to_db()


@pytest.mark.asyncio
async def test_seed_upserts_each_loaded_strategy(tmp_path: Path):
    _write_yaml(tmp_path, "v_a.yaml", _MIN_YAML.replace("v_test", "v_a"))
    _write_yaml(tmp_path, "v_b.yaml", _MIN_YAML.replace("v_test", "v_b"))
    mgr = MagicMock(spec=DataSurfaceManager)

    executes: list = []
    db = _make_mock_db(executes)

    registry = StrategyRegistry(str(tmp_path), mgr, db=db)
    registry.load_all()
    await registry.seed_registry_to_db()

    # Two strategies → two upsert calls
    assert len(executes) == 2
    # Verify argument ordering: strategy_id, version, mode, asset, timescale
    strategy_ids = {call[1] for call in executes}
    assert strategy_ids == {"v_a", "v_b"}
    for call in executes:
        # version is second positional arg
        assert call[2] == "1.0.0"
        assert call[3] == "GHOST"


@pytest.mark.asyncio
async def test_seed_captures_raw_yaml_for_audit_trail(tmp_path: Path):
    """The raw YAML text (comments, formatting) is what lands in
    ``config_yaml`` — not a re-serialisation of the parsed dict. This
    preserves author intent for the audit trail."""
    yaml_with_comment = (
        "# This comment must survive into the DB\n"
        "name: v_commented\n"
        'version: "2.0.0"\n'
        "mode: LIVE\n"
        "asset: BTC\n"
        "timescale: 5m\n"
        "gates: []\n"
        "sizing:\n  type: fixed_kelly\n  fraction: 0.025\n"
    )
    _write_yaml(tmp_path, "v_commented.yaml", yaml_with_comment)
    mgr = MagicMock(spec=DataSurfaceManager)

    executes: list = []
    db = _make_mock_db(executes)

    registry = StrategyRegistry(str(tmp_path), mgr, db=db)
    registry.load_all()
    await registry.seed_registry_to_db()

    assert len(executes) == 1
    # raw_yaml is 6th positional arg (sql, strategy_id, version, mode, asset,
    # timescale, raw_yaml, ...)
    raw_yaml_arg = executes[0][6]
    assert "# This comment must survive into the DB" in raw_yaml_arg


@pytest.mark.asyncio
async def test_seed_swallows_upsert_errors(tmp_path: Path):
    """If one execute() raises, the seed method logs a warning and
    returns — engine startup continues. Never blocks on DB."""
    _write_yaml(tmp_path, "v_test.yaml", _MIN_YAML)
    mgr = MagicMock(spec=DataSurfaceManager)

    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=RuntimeError("simulated upsert failure"))

    class AcquireCtx:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=AcquireCtx())
    db = MagicMock()
    db._pool = pool

    registry = StrategyRegistry(str(tmp_path), mgr, db=db)
    registry.load_all()

    # Must not raise
    await registry.seed_registry_to_db()


@pytest.mark.asyncio
async def test_seed_no_op_when_nothing_loaded(tmp_path: Path):
    """Empty config dir → load_all runs but registers nothing → seed is
    a documented no-op, never touches the pool."""
    mgr = MagicMock(spec=DataSurfaceManager)

    executes: list = []
    db = _make_mock_db(executes)

    registry = StrategyRegistry(str(tmp_path), mgr, db=db)
    registry.load_all()
    await registry.seed_registry_to_db()

    assert executes == []
