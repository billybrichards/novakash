"""Backward-compat shim — real implementation in infrastructure/runtime.py.

DO NOT add new code here. Import from infrastructure.runtime instead.
"""
from infrastructure.runtime import EngineRuntime as Orchestrator  # noqa: F401
