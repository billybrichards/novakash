"""Regression tests for the PR that made silenced engine writer warnings visible.

Before this PR, several persistence-layer writers logged
``error=str(exc)[:N]`` on failure. When ``exc`` was ``asyncio.TimeoutError``
(raised by ``asyncpg.pool.acquire(timeout=5)`` whenever the pool was
contended) the resulting warning was

    warning  db.update_signal_evaluations_lgb_v9_5_eth_failed error=

which gave the operator absolutely nothing actionable. The fix routes all
these sites through ``infrastructure.log_util.exc_log_fields`` which
guarantees a non-empty ``error`` and an always-present ``error_type``.

These tests trigger the same exception inside the writer and assert the
emitted log record carries both fields with useful values.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
import structlog
from structlog.testing import LogCapture

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from persistence.db_client import DBClient


# ── Fake pool that raises asyncio.TimeoutError on acquire ────────────────


class _TimeoutPool:
    """Mimics ``asyncpg.Pool`` whose ``acquire(timeout=N)`` always times out.

    Real failure mode in production when the pool is exhausted — the engine's
    sidecar writers were all losing this error message until this PR.
    """

    def acquire(self, **kwargs):
        outer = self

        class _CM:
            async def __aenter__(self_inner):
                raise asyncio.TimeoutError()

            async def __aexit__(self_inner, *exc):
                return None

        return _CM()


def _stub_db_timeout() -> DBClient:
    db = DBClient.__new__(DBClient)
    db._pool = _TimeoutPool()
    return db


@pytest.fixture
def log_capture(monkeypatch):
    """Capture structlog events emitted during the test."""
    capture = LogCapture()
    structlog.configure(processors=[capture])
    yield capture
    structlog.reset_defaults()


# ── Writers patched by this PR ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v9_5_eth_writer_emits_error_type_on_pool_timeout(log_capture):
    db = _stub_db_timeout()
    n = await db.update_signal_evaluations_lgb_v9_5_eth(
        window_ts=1779658500,
        asset="ETH",
        timeframe="5m",
        eval_offset=180,
        probability_lgb_v9_5_eth=0.94,
    )
    assert n == 0  # writer swallows + returns 0

    warning = next(
        e for e in log_capture.entries
        if e["event"] == "db.update_signal_evaluations_lgb_v9_5_eth_failed"
    )
    # The whole point of the PR: error_type tells the operator it was a timeout
    # without needing to read the source.
    assert warning["error_type"] == "TimeoutError"
    # And error is non-empty — the original symptom (``error=``) is gone.
    assert warning["error"], "error field must never be blank"
    assert "TimeoutError" in warning["error"]
    # Context fields preserved.
    assert warning["asset"] == "ETH"
    assert warning["window_ts"] == 1779658500


@pytest.mark.asyncio
async def test_v9_5_xrp_writer_emits_error_type_on_pool_timeout(log_capture):
    db = _stub_db_timeout()
    n = await db.update_signal_evaluations_lgb_v9_5_xrp(
        window_ts=1779658500,
        asset="XRP",
        timeframe="5m",
        eval_offset=180,
        probability_lgb_v9_5_xrp=0.91,
    )
    assert n == 0
    warning = next(
        e for e in log_capture.entries
        if e["event"] == "db.update_signal_evaluations_lgb_v9_5_xrp_failed"
    )
    assert warning["error_type"] == "TimeoutError"
    assert warning["error"]


@pytest.mark.asyncio
async def test_window_ensemble_fields_writer_emits_error_type(log_capture):
    db = _stub_db_timeout()
    await db.update_window_ensemble_fields(
        window_ts=1779658500,
        asset="BTC",
        timeframe="5m",
        eval_offset=180,
        ensemble_fields={"ensemble_p_up": 0.55},
    )
    warning = next(
        e for e in log_capture.entries
        if e["event"] == "db.update_window_ensemble_fields_failed"
    )
    assert warning["error_type"] == "TimeoutError"
    assert warning["error"]
    assert warning["asset"] == "BTC"


@pytest.mark.asyncio
async def test_window_surface_fields_writer_emits_error_type(log_capture):
    db = _stub_db_timeout()
    await db.update_window_surface_fields(
        window_ts=1779658500,
        asset="BTC",
        timeframe="5m",
        eval_offset=180,
        surface_fields={"sub_signal_vpin": 0.7},
    )
    warning = next(
        e for e in log_capture.entries
        if e["event"] == "db.update_window_surface_fields_failed"
    )
    assert warning["error_type"] == "TimeoutError"
    assert warning["error"]
