"""Custom hooks for v4_fusion_v5_9 strategy."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_base_module():
    base_path = Path(__file__).with_name("v4_fusion.py")
    spec = importlib.util.spec_from_file_location(
        "strategy_hooks.v4_fusion_base", base_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


_base = _load_base_module()


_STRATEGY_ID = "v4_fusion_v5_9"
_VERSION = "5.0.0"


def _rewrite(decision):
    decision.strategy_id = _STRATEGY_ID
    decision.strategy_version = _VERSION
    return decision


def evaluate_polymarket_v2(surface):
    decision = _base.evaluate_polymarket_v2(surface)
    if decision is None:
        return None
    return _rewrite(decision)
