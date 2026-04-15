"""System clock adapter -- real wall-clock time via ``time.time()``.

Implements :class:`engine.domain.ports.Clock`.  The simplest possible
adapter -- one method, one stdlib call.  Exists so that use cases and
domain services can depend on the ``Clock`` port abstraction and tests
can inject a deterministic fake clock.

Identical in spirit to ``margin_engine/adapters/clock/system.py``.

Phase 2 deliverable (CA-02).  Nothing imports this adapter yet.
"""

from __future__ import annotations

import time

import structlog

from use_cases.ports.clock import Clock

log = structlog.get_logger(__name__)


class SystemClock(Clock):
    """Wall-clock implementation of :class:`Clock`.

    Returns ``time.time()`` -- Unix epoch seconds as a float.
    No caching, no rounding, no timezone gymnastics.
    """

    def now(self) -> float:
        """Return current Unix epoch seconds."""
        return time.time()
