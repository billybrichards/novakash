"""
Presentation layer for the margin engine.

Contains HTTP API routes, request/response schemas, and
adapters for external consumers (dashboards, monitoring).
"""
from margin_engine.presentation.api.routes.status import StatusServer

__all__ = ["StatusServer"]
