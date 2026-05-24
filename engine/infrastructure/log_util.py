"""Shared logging helpers for the engine.

``exc_log_fields``
    Build a ``dict`` of structured logging fields that ALWAYS carries enough
    information to identify a caught exception, even when ``str(exc)`` is
    empty.

    Several exception types in the engine's hot path have a falsy
    ``__str__`` — most notably ``asyncio.TimeoutError`` raised by
    ``asyncpg.pool.acquire(timeout=N)``. The pre-existing pattern of
    ``log.warning("foo.failed", error=str(exc)[:200])`` was silently logging
    ``error=`` with an empty value for every pool-acquire timeout, masking
    the real failure mode and making the warnings useless for diagnosis.

    This helper always emits two keys:

    * ``error``      — the exception message (str(exc)). Falls back to
                       ``repr(exc)`` when ``str(exc)`` is empty, so the
                       caller never gets a blank value.
    * ``error_type`` — the exception class name. Cheap, always populated,
                       and the single most useful clue when triaging an
                       unfamiliar warning.

    Both values are truncated to ``max_len`` to stay safe in structlog
    output.

    Use::

        try:
            ...
        except Exception as exc:
            log.warning("db.something_failed", **exc_log_fields(exc))

    instead of::

        except Exception as exc:
            log.warning("db.something_failed", error=str(exc)[:200])
"""

from __future__ import annotations

DEFAULT_MAX_LEN: int = 200


def exc_log_fields(exc: BaseException, *, max_len: int = DEFAULT_MAX_LEN) -> dict:
    """Return ``{"error": ..., "error_type": ...}`` for a caught exception.

    Guarantees the ``error`` value is non-empty when the exception is
    truthy (i.e. has a meaningful class name), by falling back to
    ``repr(exc)`` when ``str(exc)`` is blank.
    """
    msg = str(exc)
    if not msg:
        # asyncio.TimeoutError, some asyncpg pool exceptions, and a few
        # other built-ins have an empty __str__. ``repr`` always yields
        # at least ``ClassName()`` so the operator gets *something*.
        msg = repr(exc)
    return {
        "error": msg[:max_len],
        "error_type": type(exc).__name__,
    }


__all__ = ["exc_log_fields", "DEFAULT_MAX_LEN"]
