"""
Structlog Configuration

Call configure_logging() once at startup before any other imports
that use structlog loggers.
"""

from __future__ import annotations

import logging
import structlog


def configure_logging(level: str = "INFO") -> None:
    """
    Configure structlog with ISO timestamps, log level, and a dev console renderer.

    Args:
        level: Python logging level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )
