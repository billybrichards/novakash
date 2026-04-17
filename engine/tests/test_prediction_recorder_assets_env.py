"""
Tests for PREDICTION_RECORDER_ASSETS env gate.

Default: ASSETS == ["BTC"] (BTC-only sweeping — audit #209 workaround while
ETH/SOL/XRP v2 models are missing from S3 and the scorer calls cost TimesFM
CPU that flaps /v4/snapshot past the 60s stale threshold).

Override: PREDICTION_RECORDER_ASSETS="BTC,ETH,SOL,XRP" restores prior sweep.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys


_RECORDER_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "data"
    / "feeds"
    / "prediction_recorder.py"
)


def _load_recorder_with_env(monkeypatch, env_value):
    """Fresh import of prediction_recorder with a specific env var value.

    The module reads ``PREDICTION_RECORDER_ASSETS`` at import time, so we
    must re-execute the module body under the patched env.
    """
    if env_value is None:
        monkeypatch.delenv("PREDICTION_RECORDER_ASSETS", raising=False)
    else:
        monkeypatch.setenv("PREDICTION_RECORDER_ASSETS", env_value)

    mod_name = f"_prediction_recorder_env_{id(env_value)}"
    spec = importlib.util.spec_from_file_location(mod_name, _RECORDER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_default_assets_is_btc_only(monkeypatch):
    """Unset env → BTC only (audit #209 workaround)."""
    mod = _load_recorder_with_env(monkeypatch, None)
    assert mod.ASSETS == ["BTC"]


def test_env_restores_all_assets(monkeypatch):
    """Explicit env override restores full sweep once alt models are back."""
    mod = _load_recorder_with_env(monkeypatch, "BTC,ETH,SOL,XRP")
    assert mod.ASSETS == ["BTC", "ETH", "SOL", "XRP"]


def test_env_normalises_whitespace_and_case(monkeypatch):
    """Robust to operator typos — trims and upper-cases."""
    mod = _load_recorder_with_env(monkeypatch, " btc , Eth , sol ")
    assert mod.ASSETS == ["BTC", "ETH", "SOL"]


def test_env_empty_entries_filtered(monkeypatch):
    """Empty entries from trailing commas etc. are dropped, not kept as ''."""
    mod = _load_recorder_with_env(monkeypatch, "BTC,,XRP,")
    assert mod.ASSETS == ["BTC", "XRP"]


def test_env_single_asset_override(monkeypatch):
    """Single-asset override (e.g. alts-only testing) works."""
    mod = _load_recorder_with_env(monkeypatch, "ETH")
    assert mod.ASSETS == ["ETH"]
