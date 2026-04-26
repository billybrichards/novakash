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
    """Start the log server.  Returns the runner (for cleanup) or None on failure."""
    import structlog

    log = structlog.get_logger("log_server")

    app = web.Application()
    app.router.add_get("/logs", _handle_logs)
    app.router.add_get("/health", _handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", LOG_PORT)
    try:
        await site.start()
        log.info("log_server.started", port=LOG_PORT, log_file=LOG_FILE)
        return runner
    except OSError as exc:
        log.warning("log_server.start_failed", error=str(exc))
        await runner.cleanup()
        return None
