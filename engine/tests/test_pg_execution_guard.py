import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from adapters.persistence.pg_execution_guard import PgWindowExecutionGuard

@pytest.mark.asyncio
async def test_has_not_executed_initially():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    guard = PgWindowExecutionGuard(pool)
    result = await guard.has_executed("v4_fusion", 1713000000)
    assert result is False

@pytest.mark.asyncio
async def test_in_memory_cache_hit_after_mark():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    pool.execute = AsyncMock()
    guard = PgWindowExecutionGuard(pool)
    await guard.mark_executed("v4_fusion", 1713000000, "order-123")
    # Second call should hit in-memory cache, NOT DB
    pool.fetchrow.reset_mock()
    result = await guard.has_executed("v4_fusion", 1713000000)
    assert result is True
    pool.fetchrow.assert_not_called()  # cache hit, no DB call

@pytest.mark.asyncio
async def test_fail_closed_on_db_error():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(side_effect=Exception("DB down"))
    guard = PgWindowExecutionGuard(pool)
    result = await guard.has_executed("v4_fusion", 1713000000)
    assert result is True  # FAIL-CLOSED: assume already executed

@pytest.mark.asyncio
async def test_load_recent_warms_cache():
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[
        {"strategy_id": "v4_fusion", "window_ts": 1713000000},
        {"strategy_id": "v4_down_only", "window_ts": 1713000000},
    ])
    pool.fetchrow = AsyncMock(return_value=None)  # For unknown windows
    guard = PgWindowExecutionGuard(pool)
    await guard.load_recent(hours=2)
    # Reset the mock to track calls after cache warming
    pool.fetchrow.reset_mock()
    assert await guard.has_executed("v4_fusion", 1713000000) is True
    assert await guard.has_executed("v4_down_only", 1713000000) is True
    pool.fetchrow.assert_not_called()  # Both should be cache hits
    # For an unknown window, it should call fetchrow (not in cache)
    assert await guard.has_executed("v4_fusion", 9999999999) is False
    pool.fetchrow.assert_called_once()


# ─── Port-contract + coverage gap tests ────────────────────────────────────


@pytest.mark.asyncio
async def test_has_executed_true_when_db_returns_row():
    """DB returns a row -> has_executed returns True and populates cache (lines 28-29)."""
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"strategy_id": "v4_fusion", "window_ts": 1713000000})
    guard = PgWindowExecutionGuard(pool)
    result = await guard.has_executed("v4_fusion", 1713000000)
    assert result is True
    # Verify it's now cached (second call should NOT hit the DB)
    pool.fetchrow.reset_mock()
    result2 = await guard.has_executed("v4_fusion", 1713000000)
    assert result2 is True
    pool.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_mark_executed_adds_to_cache():
    """mark_executed writes to DB and adds key to in-memory cache (lines 45-46)."""
    pool = MagicMock()
    pool.execute = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=None)
    guard = PgWindowExecutionGuard(pool)
    await guard.mark_executed("v4_fusion", 1713000000, "order-abc")
    pool.execute.assert_awaited_once()
    # Cache should contain the key now
    pool.fetchrow.reset_mock()
    result = await guard.has_executed("v4_fusion", 1713000000)
    assert result is True
    pool.fetchrow.assert_not_called()  # Served from cache


@pytest.mark.asyncio
async def test_mark_executed_db_error_still_caches():
    """DB error on mark_executed -> still adds to in-memory cache (line 49)."""
    pool = MagicMock()
    pool.execute = AsyncMock(side_effect=Exception("DB unavailable"))
    pool.fetchrow = AsyncMock(return_value=None)
    guard = PgWindowExecutionGuard(pool)
    await guard.mark_executed("v4_fusion", 1713000000, "order-abc")
    # Cache should still have the key to prevent same-process duplicates
    pool.fetchrow.reset_mock()
    result = await guard.has_executed("v4_fusion", 1713000000)
    assert result is True
    pool.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_load_recent_error_does_not_raise():
    """load_recent DB error is logged and swallowed (lines 61-62)."""
    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=Exception("DB down"))
    guard = PgWindowExecutionGuard(pool)
    await guard.load_recent(hours=2)  # Should not raise
