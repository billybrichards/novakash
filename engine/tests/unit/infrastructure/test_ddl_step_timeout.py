"""Regression test: _ensure_ddls_bg must apply per-step timeouts.

Bug discovered 2026-05-09 (audit #413, Hub note #431):
  EngineRuntime's `_ensure_ddls_bg` ran a sequence of `await fn()` calls
  with no timeout. `ensure_v8_trade_columns` issued ALTER TABLE on the
  `trades` hot table, which held `AccessExclusiveLock` for 13m41s while
  contending with concurrent writers. The engine event loop stalled
  until SIGKILL.

This test extracts the same loop logic and asserts:
 1. A hung step does NOT block the chain — subsequent steps still run.
 2. The timeout is logged with the step name (`ddl.timeout` / `ddl_timeout`).
 3. The chain-level cap halts further steps once cumulative elapsed
    exceeds the global cap.

We don't import EngineRuntime (its import graph requires the engine
runtime). Instead, we replicate the exact loop body and verify behaviour
end-to-end.
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest


# --- Replica of _ensure_ddls_bg loop body (kept in sync with runtime.py) -----

DDL_STEP_TIMEOUT_S = 10.0
DDL_CHAIN_TIMEOUT_S = 60.0


async def _run_ddl_chain(steps, logs, step_timeout=DDL_STEP_TIMEOUT_S, chain_timeout=DDL_CHAIN_TIMEOUT_S):
    """Mirror of the inner loop in `_ensure_ddls_bg`. Records events to
    `logs` instead of structlog so the test can assert on them."""
    t0 = time.monotonic()
    for name, fn in steps:
        step_t0 = time.monotonic()
        chain_elapsed = time.monotonic() - t0
        if chain_elapsed >= chain_timeout:
            logs.append(("ddl_chain_timeout", name))
            break
        try:
            await asyncio.wait_for(fn(), timeout=step_timeout)
            logs.append(("ddl_done", name, int((time.monotonic() - step_t0) * 1000)))
        except asyncio.TimeoutError:
            logs.append(("ddl_timeout", name))
        except Exception as exc:  # pragma: no cover — defensive
            logs.append(("ddl_failed", name, str(exc)))


# --- Tests -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hung_step_does_not_block_chain():
    """A step that hangs past the per-step timeout must not block
    subsequent steps. The hang must produce a `ddl_timeout` log entry
    with the step name.
    """
    completed = []

    async def fast():
        completed.append("fast")

    async def hung():
        # Simulate the AccessExclusiveLock hang. We sleep well past the
        # short timeout we'll use in the test.
        await asyncio.sleep(60)
        completed.append("hung")  # should never reach here

    async def after_hang():
        completed.append("after_hang")

    logs: list = []
    steps = [
        ("step_fast", fast),
        ("step_hung", hung),
        ("step_after_hang", after_hang),
    ]

    # Use very short timeouts so the test runs in <1s.
    await _run_ddl_chain(steps, logs, step_timeout=0.1, chain_timeout=10.0)

    # Both non-hung steps ran.
    assert "fast" in completed
    assert "after_hang" in completed
    # Hung step did NOT complete.
    assert "hung" not in completed

    # The timeout was logged with the step name.
    timeouts = [entry for entry in logs if entry[0] == "ddl_timeout"]
    assert len(timeouts) == 1, f"expected 1 timeout log, got {logs!r}"
    assert timeouts[0][1] == "step_hung"


@pytest.mark.asyncio
async def test_chain_cap_aborts_remaining_steps():
    """If cumulative elapsed exceeds the chain cap, remaining steps are
    skipped (logged as `ddl_chain_timeout`)."""
    completed = []

    async def slow_but_within_step_cap():
        # 0.15s per step — under per-step cap (0.5s) but cumulative
        # blows the chain cap (0.2s) after the first.
        await asyncio.sleep(0.15)
        completed.append("ran")

    logs: list = []
    steps = [
        ("step1", slow_but_within_step_cap),
        ("step2", slow_but_within_step_cap),
        ("step3", slow_but_within_step_cap),
    ]

    await _run_ddl_chain(steps, logs, step_timeout=0.5, chain_timeout=0.2)

    # First step ran (chain_elapsed was 0 at start).
    assert len(completed) >= 1
    # At least one chain_timeout was logged for a skipped step.
    chain_timeouts = [e for e in logs if e[0] == "ddl_chain_timeout"]
    assert len(chain_timeouts) >= 1, (
        f"expected chain_timeout log for skipped steps, got {logs!r}"
    )


def test_runtime_source_has_ddl_step_timeout():
    """Static check: the runtime.py source must wrap DDL fn() calls in
    asyncio.wait_for. Without this, a future refactor that drops the
    timeout would silently re-introduce the 13min DDL hang.
    """
    runtime_path = os.path.join(
        os.path.dirname(__file__),
        "..", "..", "..",
        "infrastructure",
        "runtime.py",
    )
    runtime_path = os.path.abspath(runtime_path)
    with open(runtime_path, "r") as fh:
        src = fh.read()

    # The timeout constants must exist.
    assert "DDL_STEP_TIMEOUT_S" in src, (
        "runtime._ensure_ddls_bg must declare DDL_STEP_TIMEOUT_S. "
        "See audit #413 — without per-step timeout, ALTER TABLE on the "
        "trades hot table can hang the engine for 13+ minutes."
    )
    assert "DDL_CHAIN_TIMEOUT_S" in src, (
        "runtime._ensure_ddls_bg must declare DDL_CHAIN_TIMEOUT_S to "
        "bound cumulative elapsed across the chain."
    )
    # The wait_for wrapper must be present.
    assert "asyncio.wait_for(fn()" in src, (
        "DDL step calls must be wrapped in asyncio.wait_for(...) so a "
        "stuck step doesn't block the chain or hang the event loop."
    )
    # And the timeout-handler branch must log under a stable event name.
    assert "orchestrator.ddl_timeout" in src, (
        "Timeouts must emit a stable structured-log event "
        "`orchestrator.ddl_timeout` with the step name (audit #413)."
    )
