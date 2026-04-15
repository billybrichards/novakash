"""Regression test for Phase 1 Settings split.

TestSettings MUST have defaults for every required field in Settings so
importing it produces no ValidationError even when no env vars are set.
"""
from __future__ import annotations

import os


def test_test_settings_instantiates_without_env(monkeypatch):
    """TestSettings() must not raise in a bare env."""
    # Strip every env var that Settings might read
    for key in list(os.environ):
        if key.startswith(("DATABASE_", "POLY_", "BINANCE_", "COINGLASS_",
                           "TIINGO_", "POLYGON_", "TELEGRAM_", "OPINION_",
                           "OPENROUTER_", "PAPER_", "STARTING_")):
            monkeypatch.delenv(key, raising=False)

    from config.settings import TestSettings
    s = TestSettings()
    assert s.database_url.startswith("sqlite")
    assert s.paper_mode is True


def test_get_settings_returns_settings_instance():
    """get_settings() returns a Settings (not TestSettings) in prod mode."""
    from config.settings import Settings, get_settings
    # Set DATABASE_URL so prod Settings() validates
    import os
    os.environ["DATABASE_URL"] = "postgresql://test"
    try:
        s = get_settings()
        assert isinstance(s, Settings)
    finally:
        del os.environ["DATABASE_URL"]


def test_module_level_settings_is_lazy():
    """Importing config.settings must NOT instantiate Settings eagerly."""
    # If the module-level `settings = Settings()` still exists and env is
    # unset, this import would raise. We only import here; no attribute access.
    import config.settings  # noqa: F401
