"""Tests for engine/strategies/runtime_override.py — audit #291.

Covers:
  1. Empty cache is a no-op; YAML mode/params win when no override row.
  2. Override mode returned when DB has a non-null mode.
  3. NULL mode in override row means "inherit YAML" (no override).
  4. Params shallow-merge: YAML keys absent from override survive;
     keys present in override win; keys only in override are added.
  5. None params in override row means YAML params pass through verbatim.
  6. Invalid mode in override (defensive — bad DB row) falls through
     to YAML mode with a warning; never raises.
  7. ``refresh_once`` picks up new rows + removes gone rows + logs a
     structured delta summary.
  8. Singleton wiring is sticky and safe to re-get.
  9. Graceful degradation: DB pool None → refresh_once returns False
     quietly, get_effective_* still work.
 10. ``apply_runtime_overrides`` convenience returns a (mode, params) tuple.
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from strategies.runtime_override import (  # noqa: E402
    RuntimeOverride,
    RuntimeOverrideManager,
    apply_runtime_overrides,
    get_runtime_override_manager,
    reset_singleton_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Give every test a fresh module singleton so ordering is irrelevant."""
    reset_singleton_for_tests()
    yield
    reset_singleton_for_tests()


def _build_pool(rows: list[dict]):
    """Assemble an asyncpg-style mock pool whose acquire() yields a
    connection that returns *rows* from fetch().
    """

    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)

    class AcquireCtx:
        async def __aenter__(self_inner):
            return conn

        async def __aexit__(self_inner, *a):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=AcquireCtx())
    return pool, conn


# ─── Empty cache / no override ────────────────────────────────────────────────


def test_empty_cache_returns_yaml_mode():
    mgr = RuntimeOverrideManager(db_pool=None)
    assert mgr.get_effective_mode("v4_fusion", "LIVE") == "LIVE"
    assert mgr.get_effective_mode("v4_fusion", "GHOST") == "GHOST"


def test_empty_cache_returns_yaml_params_copy():
    mgr = RuntimeOverrideManager(db_pool=None)
    yaml_params = {"min_dist": 0.15, "window": 90}
    out = mgr.get_effective_params("v4_fusion", yaml_params)
    assert out == yaml_params
    # Must return a copy, not the same dict reference — protects the
    # YAML config from mutation by hook code.
    assert out is not yaml_params


def test_get_runtime_override_returns_none_when_absent():
    mgr = RuntimeOverrideManager(db_pool=None)
    assert mgr.get_runtime_override("v4_fusion") is None


# ─── Mode override ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mode_override_flips_yaml():
    pool, _ = _build_pool(
        [{"strategy_id": "v8_champion", "mode": "LIVE", "params": None}]
    )
    mgr = RuntimeOverrideManager(db_pool=pool)
    assert await mgr.refresh_once() is True

    # YAML says GHOST, override says LIVE — LIVE wins.
    assert mgr.get_effective_mode("v8_champion", "GHOST") == "LIVE"


@pytest.mark.asyncio
async def test_null_mode_in_override_inherits_yaml():
    pool, _ = _build_pool(
        [{"strategy_id": "v8_champion", "mode": None, "params": None}]
    )
    mgr = RuntimeOverrideManager(db_pool=pool)
    await mgr.refresh_once()

    # Row present but mode=None → inherit YAML.
    assert mgr.get_effective_mode("v8_champion", "GHOST") == "GHOST"


@pytest.mark.asyncio
async def test_invalid_mode_in_override_falls_back_to_yaml():
    """Defensive: a malformed DB row (corrupted / external write)
    must never break evaluation."""
    pool, _ = _build_pool(
        [{"strategy_id": "v8_champion", "mode": "EXPLODE", "params": None}]
    )
    mgr = RuntimeOverrideManager(db_pool=pool)
    await mgr.refresh_once()
    assert mgr.get_effective_mode("v8_champion", "GHOST") == "GHOST"


# ─── Params merge ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_params_shallow_merge_yaml_base():
    """YAML keys absent from override survive; override keys win; new
    override keys are added."""
    pool, _ = _build_pool(
        [
            {
                "strategy_id": "v4_fusion",
                "mode": None,
                "params": {"min_dist": 0.20, "new_key": True},
            }
        ]
    )
    mgr = RuntimeOverrideManager(db_pool=pool)
    await mgr.refresh_once()

    yaml_params = {"min_dist": 0.15, "window": 90}
    merged = mgr.get_effective_params("v4_fusion", yaml_params)
    assert merged == {"min_dist": 0.20, "window": 90, "new_key": True}


@pytest.mark.asyncio
async def test_null_params_inherits_yaml_verbatim():
    pool, _ = _build_pool(
        [{"strategy_id": "v4_fusion", "mode": None, "params": None}]
    )
    mgr = RuntimeOverrideManager(db_pool=pool)
    await mgr.refresh_once()

    yaml_params = {"min_dist": 0.15, "window": 90}
    merged = mgr.get_effective_params("v4_fusion", yaml_params)
    assert merged == yaml_params


@pytest.mark.asyncio
async def test_params_json_string_decoded():
    """Some DB drivers return JSONB as a decoded string. Ensure we decode."""
    import json
    pool, _ = _build_pool(
        [
            {
                "strategy_id": "v4_fusion",
                "mode": None,
                "params": json.dumps({"min_dist": 0.22}),
            }
        ]
    )
    mgr = RuntimeOverrideManager(db_pool=pool)
    await mgr.refresh_once()

    merged = mgr.get_effective_params("v4_fusion", {"min_dist": 0.15})
    assert merged == {"min_dist": 0.22}


# ─── Cache refresh lifecycle ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_picks_up_new_rows():
    """Second refresh adding a row is visible to later get_* calls."""
    pool, conn = _build_pool([])

    mgr = RuntimeOverrideManager(db_pool=pool)
    await mgr.refresh_once()
    assert mgr.get_runtime_override("v8_champion") is None

    # Mutate the fetch return value and re-refresh — the cache must
    # swap in cleanly.
    conn.fetch.return_value = [
        {"strategy_id": "v8_champion", "mode": "LIVE", "params": None}
    ]
    await mgr.refresh_once()
    ov = mgr.get_runtime_override("v8_champion")
    assert ov is not None
    assert ov.mode == "LIVE"


@pytest.mark.asyncio
async def test_refresh_removes_gone_rows():
    """A row that drops out of the DB disappears from the cache."""
    pool, conn = _build_pool(
        [{"strategy_id": "v8_champion", "mode": "LIVE", "params": None}]
    )
    mgr = RuntimeOverrideManager(db_pool=pool)
    await mgr.refresh_once()
    assert mgr.get_runtime_override("v8_champion") is not None

    conn.fetch.return_value = []
    await mgr.refresh_once()
    assert mgr.get_runtime_override("v8_champion") is None


@pytest.mark.asyncio
async def test_refresh_db_error_is_graceful():
    """A DB error (table missing, connection dropped) must not raise —
    cache stays at its last-known-good state."""
    pool = MagicMock()

    class BrokenAcquire:
        async def __aenter__(self):
            raise RuntimeError("simulated connection error")

        async def __aexit__(self, *a):
            return False

    pool.acquire = MagicMock(return_value=BrokenAcquire())

    mgr = RuntimeOverrideManager(db_pool=pool)
    ok = await mgr.refresh_once()
    assert ok is False
    # Readers still work — they just return the empty cache.
    assert mgr.get_effective_mode("anything", "GHOST") == "GHOST"


@pytest.mark.asyncio
async def test_refresh_no_pool_returns_false():
    mgr = RuntimeOverrideManager(db_pool=None)
    assert await mgr.refresh_once() is False


# ─── Singleton wiring ─────────────────────────────────────────────────────────


def test_singleton_is_sticky():
    a = get_runtime_override_manager()
    b = get_runtime_override_manager()
    assert a is b


def test_singleton_upgrades_pool_when_supplied_later():
    a = get_runtime_override_manager(db_pool=None)
    assert a._db_pool is None
    pool = MagicMock()
    b = get_runtime_override_manager(db_pool=pool)
    assert a is b
    assert b._db_pool is pool


def test_apply_runtime_overrides_returns_tuple():
    """Registry calls this helper once per evaluate — the contract is
    a (mode, params_dict) tuple."""
    mode, params = apply_runtime_overrides(
        "v4_fusion", yaml_mode="GHOST", yaml_params={"k": 1}
    )
    # No override seeded — echoes YAML baseline.
    assert mode == "GHOST"
    assert params == {"k": 1}


# ─── RuntimeOverride dataclass sanity ─────────────────────────────────────────


def test_runtime_override_is_immutable():
    ov = RuntimeOverride(strategy_id="v4_fusion", mode="LIVE", params=None)
    with pytest.raises(Exception):
        ov.mode = "GHOST"  # type: ignore[misc]
