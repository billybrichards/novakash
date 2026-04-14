"""
FastAPI dependencies for the presentation layer API.

These dependencies provide access to domain services and repositories
for use in route handlers.
"""
from __future__ import annotations

from typing import Callable, Optional


def get_portfolio() -> Optional[Callable]:
    """
    Get the portfolio instance.
    
    This should be wired up in the main application setup.
    """
    pass


def get_exchange() -> Optional[Callable]:
    """
    Get the exchange adapter instance.
    
    This should be wired up in the main application setup.
    """
    pass


def get_log_repo() -> Optional[Callable]:
    """
    Get the log repository instance.
    
    This should be wired up in the main application setup.
    """
    pass


def get_position_repo() -> Optional[Callable]:
    """
    Get the position repository instance.
    
    This should be wired up in the main application setup.
    """
    pass
