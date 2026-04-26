"""Startup-ordering invariants for ``EngineRuntime.start``.

After a 6m 27s silent startup hang in production on 2026-04-26, the
startup sequence in ``infrastructure/runtime.py`` was refactored so:

1. The HTTP log server starts BEFORE any potentially-slow remote await,
   so operators can always tail logs / hit /health while debugging a
   hang. Previously it was the very last step of ``start()``.
2. ``data_surface_mgr.set_feeds()`` + ``warmup_from_db()`` + ``start()``
   run BEFORE the slow recovery / CLOB-reconciler work. Previously the
   data surface was wired AFTER a 5+ minute reconciler backfill,
   leaving strategies to skip with ``source_agreement: chainlink,tiingo
   missing`` for the duration.
3. Slow recovery and reconciler initialisation run as background
   ``asyncio.create_task`` instead of blocking awaits in the main
   ``start()`` coroutine.

These tests are static checks against the source — they do not spin up
a real ``EngineRuntime`` (which has dozens of dependencies). The point
is to make sure a future refactor cannot silently regress the ordering.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from socket import socket, AF_INET, SOCK_STREAM, SOL_SOCKET, SO_REUSEADDR

import pytest


RUNTIME_PATH = Path(__file__).parent.parent / "infrastructure" / "runtime.py"
LOG_SERVER_PATH = (
    Path(__file__).parent.parent / "infrastructure" / "log_server.py"
)


def _read(path: Path) -> str:
    return path.read_text()


def _index_of(needle: str, haystack: str) -> int:
    """Return char offset of ``needle`` in ``haystack``. -1 if absent."""
    return haystack.find(needle)


# ---------------------------------------------------------------------------
# Ordering invariants
# ---------------------------------------------------------------------------


def test_log_server_starts_before_data_surface() -> None:
    """The log-server start must precede the data-surface start.

    If a future refactor moves the log-server back to the bottom of
    ``start()``, this test fails — the operator's only debug surface
    must come up before anything that can hang.
    """
    src = _read(RUNTIME_PATH)
    log_server_idx = _index_of("self._log_server_runner = await start_log_server", src)
    data_surface_idx = _index_of("self._data_surface_mgr.start()", src)
    assert log_server_idx > 0, "log_server start call missing from runtime"
    assert data_surface_idx > 0, "data_surface_mgr.start() call missing from runtime"
    assert log_server_idx < data_surface_idx, (
        "log_server.start_log_server() must execute BEFORE "
        "data_surface_mgr.start() — operators need /logs reachable "
        "even if data-surface warmup stalls."
    )


def test_data_surface_starts_before_clob_reconciler() -> None:
    """Strategies must have feeds wired before the slow reconciler runs.

    Pre-refactor: the CLOB reconciler `_backfill_on_startup` could hang
    for 5+ minutes on Polymarket data-api, and the data-surface didn't
    come up until after that. Strategies skipped every 2s with
    `source_agreement: chainlink,tiingo missing`. The fix: wire the
    data surface first, run the reconciler in a background task.
    """
    src = _read(RUNTIME_PATH)
    surface_idx = _index_of("self._data_surface_mgr.start()", src)
    # The reconciler must be inside a background-task wrapper.
    bg_starter_idx = _index_of("_start_clob_reconciler_bg", src)
    assert surface_idx > 0
    assert bg_starter_idx > 0, (
        "_start_clob_reconciler_bg helper missing — CLOB reconciler "
        "should be spawned via asyncio.create_task, not awaited inline."
    )
    assert surface_idx < bg_starter_idx, (
        "data_surface_mgr.start() must run BEFORE "
        "_start_clob_reconciler_bg is even defined/spawned."
    )


def test_recover_open_trades_runs_in_background() -> None:
    """``recover_open_trades`` must be wrapped in ``asyncio.create_task``.

    Inline awaiting it adds 20+ seconds to startup before the engine
    can take new trades, which is unacceptable when restarts can be
    triggered by ops at any time.
    """
    src = _read(RUNTIME_PATH)
    # Match either of the two acceptable patterns:
    #   asyncio.create_task(_recover_open_trades_bg(), …)
    #   asyncio.create_task(self._order_manager.recover_open_trades(…), …)
    pattern = re.compile(
        r"asyncio\.create_task\(\s*_recover_open_trades_bg\(\)",
        re.DOTALL,
    )
    assert pattern.search(src), (
        "recover_open_trades must run as a background task, not inline."
    )


def test_clob_reconciler_runs_in_background() -> None:
    """The CLOB reconciler ``start()`` call must not be awaited inline."""
    src = _read(RUNTIME_PATH)
    # The bg helper must exist AND it must be scheduled as a task.
    assert "async def _start_clob_reconciler_bg" in src
    assert re.search(
        r"asyncio\.create_task\(\s*_start_clob_reconciler_bg\(\)",
        src,
    ), "CLOB reconciler bg starter must be spawned via create_task"


def test_redeemer_starts_in_background() -> None:
    """The redeemer connect loop must not block ``start()``.

    Both ``redeemer.connect()`` and ``onchain_transport.connect()`` make
    network calls that can hang during RPC degradation. The redeemer is
    only invoked on resolution (not on trade entry), so deferring is
    safe.
    """
    src = _read(RUNTIME_PATH)
    assert "async def _start_redeemer_bg" in src
    assert re.search(
        r"asyncio\.create_task\(\s*_start_redeemer_bg\(\)", src
    ), "redeemer bg starter must be spawned via create_task"


def test_phase_helper_present() -> None:
    """The ``_phase`` async-context-manager helper must exist.

    Each startup phase should log its entry + duration so future hangs
    are diagnosable from the log alone (no Python tracing required).
    """
    src = _read(RUNTIME_PATH)
    assert "def _phase" in src
    assert "orchestrator.phase.start" in src
    assert "orchestrator.phase.done" in src


# ---------------------------------------------------------------------------
# log_server resilience
# ---------------------------------------------------------------------------


def test_log_server_uses_reuse_address() -> None:
    """``start_log_server`` must enable SO_REUSEADDR / SO_REUSEPORT.

    After a fast restart, the previous PID's socket sits in TIME_WAIT
    for ~60s on the same port. Without SO_REUSEADDR, the new engine
    fails to bind once and gives up — operators lose their HTTP log
    surface at the worst possible moment.
    """
    src = _read(LOG_SERVER_PATH)
    assert "reuse_address=True" in src
    assert "reuse_port=True" in src


def test_log_server_retries_on_bind_error() -> None:
    """The log server must retry binding (transient TIME_WAIT race)."""
    src = _read(LOG_SERVER_PATH)
    assert "log_server.bind_retry" in src
    # Loop must iterate ≥ 2 times so a single transient failure doesn't
    # take the server offline for the rest of the engine session.
    assert re.search(r"for attempt in range\((\d+)\)", src)
    match = re.search(r"for attempt in range\((\d+)\)", src)
    assert match and int(match.group(1)) >= 2


# ---------------------------------------------------------------------------
# Behavioural test of the retry loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_server_starts_and_serves_health(monkeypatch, tmp_path) -> None:
    """End-to-end smoke: start_log_server binds and /health returns 200."""
    import contextlib
    import socket as _sock

    import aiohttp

    from infrastructure import log_server as ls

    # Pick a free port (avoid colliding with anything else on the box).
    s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    free_port = s.getsockname()[1]
    s.close()

    monkeypatch.setattr(ls, "LOG_PORT", free_port)
    # Point the log file at a tmp file so /logs has something to read.
    fake_log = tmp_path / "engine.log"
    fake_log.write_text("hello\nworld\n")
    monkeypatch.setattr(ls, "LOG_FILE", str(fake_log))

    runner = await ls.start_log_server()
    assert runner is not None, "log server failed to start on free port"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://127.0.0.1:{free_port}/health", timeout=2
            ) as resp:
                assert resp.status == 200
                body = await resp.json()
                assert body["status"] == "ok"
    finally:
        await runner.cleanup()
