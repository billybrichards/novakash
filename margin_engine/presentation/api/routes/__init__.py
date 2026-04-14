"""
API routes for the presentation layer.

Each route module handles a specific endpoint or feature:
- status.py: Portfolio status, health, logs, and history
"""
from margin_engine.presentation.api.routes.status import StatusServer

__all__ = ["StatusServer"]
