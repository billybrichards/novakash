"""Engine test conftest — sys.path + TestSettings env bootstrap.

Engine modules use absolute bare imports (`from domain.ports import X`,
`from use_cases.reconcile_positions import Y`) so the test process needs
`engine/` on sys.path. The PARENT of engine/ must NOT be on sys.path —
that would enable `from engine.domain.ports import X` style imports,
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
if str(REPO_ROOT) in sys.path:
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

# --- pytest configuration -------------------------------------------------
import pytest  # noqa: E402  (after sys.path munging)


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers + pytest-asyncio mode."""
    config.addinivalue_line("markers", "unit: pure domain/VO test, no I/O")
    config.addinivalue_line("markers", "use_case: use case test with mocked ports")
    config.addinivalue_line(
        "markers",
        "integration: real DB or real adapter — nightly only",
    )
    config.addinivalue_line("markers", "slow: >1s runtime — excluded from PR fast subset")
