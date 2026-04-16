"""Regression guard: canonical imports only.

Dual-load happens when both `domain.x` and `engine.domain.x` appear in
sys.modules — Python treats them as distinct modules, making isinstance()
false across the duplicate. This test asserts we have exactly one copy
loaded, with the canonical bare name.
"""
from __future__ import annotations

import sys

import pytest


# Modules that MUST only appear under the bare name in sys.modules.
_CANONICAL_MODULES = [
    "domain.value_objects",
    "domain.ports",
    "use_cases.reconcile_positions",
    "use_cases.execute_trade",
    "use_cases.publish_heartbeat",
    "config.settings",
]


@pytest.mark.parametrize("mod_name", _CANONICAL_MODULES)
def test_no_engine_prefixed_duplicate(mod_name: str):
    """`engine.{mod_name}` must NOT be in sys.modules after importing the canonical form."""
    # Ensure the canonical module is loaded
    __import__(mod_name)
    duplicate = f"engine.{mod_name}"
    assert duplicate not in sys.modules, (
        f"Dual-load detected: both `{mod_name}` and `{duplicate}` are in "
        f"sys.modules. This breaks isinstance() checks. "
        f"Check sys.path for the engine/ parent directory."
    )


def test_sys_path_does_not_contain_engine_parent():
    """engine/ parent (repo root) must NOT be on sys.path.

    Conftest removes it; if something puts it back, this test fires.
    """
    from pathlib import Path

    import config.settings  # trigger a known canonical import
    engine_root = Path(config.settings.__file__).parent.parent  # engine/
    repo_root = engine_root.parent

    assert str(repo_root) not in sys.path, (
        f"Repo root {repo_root} is on sys.path — enables `from engine.X` "
        f"imports that dual-load canonical modules."
    )
