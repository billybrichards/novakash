"""Engine test conftest — sys.path + TestSettings env bootstrap.

Engine modules use absolute bare imports (`from domain.ports import X`,
`from use_cases.reconcile_positions import Y`) so the test process needs
`engine/` on sys.path. The PARENT of engine/ must NOT be on sys.path —
that would enable `from domain.ports import X` style imports,
causing the same symbols to load twice under different module paths
(dual-load). The `engine.X`-vs-`X` dual-load is the root cause of the
`test_publish_heartbeat` isinstance quirk; Phase 2 sweeps test imports
to the canonical bare form and adds a regression test.

Run pytest from the engine/ directory:
    cd engine && pytest tests/
or with explicit path:
    pytest engine/tests/
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# --- sys.path canonicalization --------------------------------------------
ENGINE_ROOT = Path(__file__).resolve().parent.parent  # engine/
REPO_ROOT = ENGINE_ROOT.parent                         # repo root

# Insert engine/ first so `from domain.x import Y` resolves.
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

# Remove repo root if present — it enables `from engine.domain.x import Y`
# which collides with the bare form and creates dual-loaded module copies.
# Phase 2 enforces the bare form via CI grep; this is defense in depth.
# Note: pytest may re-add REPO_ROOT because engine/__init__.py makes it
# look like a package. We use `while` to strip all occurrences now and
# the session fixture below re-strips after pytest finishes its own setup.
while str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))

# --- TestSettings env bootstrap -------------------------------------------
# Set env vars BEFORE any engine module imports. Settings() still uses
# env-var lookup as primary source, so this satisfies required fields even
# for code paths that call Settings() directly (pre-sweep leftovers) and
# for the prod get_settings() path when a test explicitly opts out of
# TestSettings.
_DEFAULTS = {
    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
    "POLY_PRIVATE_KEY": "test",
    "POLY_API_KEY": "test",
    "POLY_API_SECRET": "test",
    "POLY_API_PASSPHRASE": "test",
    "POLY_FUNDER_ADDRESS": "0x0000000000000000000000000000000000000000",
    "OPINION_API_KEY": "test",
    "OPINION_WALLET_KEY": "test",
    "BINANCE_API_KEY": "test",
    "BINANCE_API_SECRET": "test",
    "COINGLASS_API_KEY": "test",
    "OPENROUTER_API_KEY": "test",
    "TIINGO_API_KEY": "test",
    "POLYGON_RPC_URL": "https://test.invalid",
    "TELEGRAM_BOT_TOKEN": "test",
    "TELEGRAM_CHAT_ID": "0",
    "PAPER_MODE": "true",
}
for key, value in _DEFAULTS.items():
    os.environ.setdefault(key, value)

# --- Phase 4 triage backlog ----------------------------------------------
# These test files have stale imports (symbols moved or removed) and will
# be triaged in Phase 4 of the engine test infra plan. Skip collection for
# now so Phase 1 can exit cleanly.
# See docs/superpowers/plans/2026-04-15-engine-test-infra.md §Phase 4.
collect_ignore = [
    "test_cascade.py",  # imports COOLDOWN_SECONDS from signals.cascade_detector (lives in config.constants)
    "unit/signals/test_gate_pipeline_immutable.py",  # imports _infer_delta from signals.gates (removed)
]

# --- pytest configuration -------------------------------------------------
import pytest  # noqa: E402  (after sys.path munging)


@pytest.fixture(autouse=True, scope="session")
def _enforce_canonical_sys_path():
    """Re-strip REPO_ROOT from sys.path after pytest finishes its own setup.

    pytest adds the parent of any package-style directory (one with __init__.py)
    to sys.path. Since engine/__init__.py exists, pytest adds the repo root.
    The module-level code above strips it at conftest import time, but pytest
    may re-insert it during its collection phase (before fixtures run). This
    session fixture runs once after collection is complete and removes any
    remaining instances.
    """
    while str(REPO_ROOT) in sys.path:
        sys.path.remove(str(REPO_ROOT))
    yield


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers."""
    config.addinivalue_line("markers", "unit: pure domain/VO test, no I/O")
    config.addinivalue_line("markers", "use_case: use case test with mocked ports")
    config.addinivalue_line(
        "markers",
        "integration: real DB or real adapter — nightly only",
    )
    config.addinivalue_line("markers", "slow: >1s runtime — excluded from PR fast subset")
