"""
Lightweight HTTP log server — tails engine.log and serves /health.

Runs as a non-blocking asyncio task alongside the engine.  If it fails
to start (port in use, missing dependency) the engine continues normally.
"""

from __future__ import annotations

import os
import time
from collections import deque

from aiohttp import web

LOG_FILE = os.environ.get("LOG_FILE", "/home/novakash/engine.log")
LOG_PORT = int(os.environ.get("ENGINE_LOG_PORT", "8888"))
_MAX_LINES = 1000
_start_time = time.monotonic()


async def _handle_logs(request: web.Request) -> web.Response:
    n = min(int(request.query.get("lines", "100")), _MAX_LINES)
    filt = request.query.get("filter", "")

    try:
        with open(LOG_FILE, "r") as f:
            tail: deque[str] = deque(maxlen=n if not filt else None)  # type: ignore[arg-type]
            for line in f:
                if filt and filt not in line:
                    continue
                tail.append(line)
            # If filtering, we collected all matches — take last N
            if filt:
                result = list(tail)[-n:]
            else:
                result = list(tail)
    except FileNotFoundError:
        result = [f"Log file not found: {LOG_FILE}\n"]

    return web.Response(
        text="".join(result),
        content_type="text/plain",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET",
        },
    )


async def _handle_health(request: web.Request) -> web.Response:
    import json

    body = json.dumps(
        {
            "status": "ok",
            "pid": os.getpid(),
            "uptime": round(time.monotonic() - _start_time, 1),
        }
    )
    return web.Response(
        text=body,
        content_type="application/json",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET",
        },
    )


async def start_log_server() -> web.AppRunner | None:
    """Start the log server.  Returns the runner (for cleanup) or None on failure.

    Robust against the common "address already in use" race after an engine
    restart — the previous PID's socket may sit in TIME_WAIT for ~60s. We
    enable SO_REUSEADDR and retry briefly so the server comes up reliably,
    even when restarts happen back-to-back.
    """
    import asyncio
    import structlog

    log = structlog.get_logger("log_server")

    app = web.Application()
    app.router.add_get("/logs", _handle_logs)
    app.router.add_get("/health", _handle_health)

    runner = web.AppRunner(app)
    await runner.setup()

    # SO_REUSEADDR + SO_REUSEPORT allow rebinding while the old socket is in
    # TIME_WAIT after a fast restart. Without this, the engine logged
    # "address already in use" once and gave up — leaving operators with no
    # HTTP log tail at the very moment they needed it most.
    site = web.TCPSite(runner, "0.0.0.0", LOG_PORT, reuse_address=True, reuse_port=True)

    last_exc: OSError | None = None
    for attempt in range(5):
        try:
            await site.start()
            log.info(
                "log_server.started",
                port=LOG_PORT,
                log_file=LOG_FILE,
                attempt=attempt + 1,
            )
            return runner
        except OSError as exc:
            last_exc = exc
            log.warning(
                "log_server.bind_retry",
                error=str(exc),
                attempt=attempt + 1,
                max_attempts=5,
            )
            await asyncio.sleep(1.0)

    log.warning("log_server.start_failed", error=str(last_exc))
    await runner.cleanup()
    return None
