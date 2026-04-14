"""
API layer within the presentation layer.

Contains HTTP routes and dependencies for external API consumption.
"""
from margin_engine.presentation.api.routes.status import StatusServer
from margin_engine.presentation.api.dependencies import (
    get_portfolio,
    get_exchange,
    get_log_repo,
    get_position_repo,
)

__all__ = [
    "StatusServer",
    "get_portfolio",
    "get_exchange",
    "get_log_repo",
    "get_position_repo",
]
