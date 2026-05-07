"""Tests for audit #397 review fix #1 — runtime_config.timesfm_enabled URL-implies-enabled.

Mirrors the composition.py enable matrix here at the runtime level. Five strategy
gates check `runtime.timesfm_enabled` (e.g. five_min_vpin.py:~900); without this
mirror the engine still skipped the forecast call even after composition fixed
its enable logic.

Matrix covered:
  TIMESFM_ENABLED unset + URL unset                     -> False
  TIMESFM_ENABLED unset + URL set                       -> True   (URL = intent)
  TIMESFM_ENABLED=false + URL set                       -> False  (explicit override)
  TIMESFM_ENABLED=true  + URL unset                     -> False  (logs error)
  TIMESFM_ENABLED=true  + URL set                       -> True
  TIMESFM_ENABLED=FALSE + URL set                       -> False  (case-insensitive)
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))


def _reload_runtime():
    """Force-reload runtime_config so it re-reads env vars."""
    if "config.runtime_config" in sys.modules:
        del sys.modules["config.runtime_config"]
    return importlib.import_module("config.runtime_config")


@pytest.mark.parametrize(
    "url,enabled,expected_enabled,expected_url_set",
    [
        # (a) Both unset -> disabled
        ("", "", False, False),
        # (b) URL set, ENABLED unset -> enabled (URL = intent)
        ("http://3.96.151.28:8080", "", True, True),
        # (c) URL set, ENABLED=true -> enabled
        ("http://3.96.151.28:8080", "true", True, True),
        # (d) URL set, ENABLED=false -> explicitly disabled
        ("http://3.96.151.28:8080", "false", False, True),
        # (e) URL unset, ENABLED=true -> disabled (logs error)
        ("", "true", False, False),
        # (f) URL set, ENABLED=FALSE (case-insensitive) -> disabled
        ("http://3.96.151.28:8080", "FALSE", False, True),
        # (g) URL set, ENABLED=TRUE (case-insensitive) -> enabled
        ("http://3.96.151.28:8080", "TRUE", True, True),
        # (h) URL unset, ENABLED=FALSE (case-insensitive) -> disabled
        ("", "FALSE", False, False),
    ],
)
def test_timesfm_enable_matrix(monkeypatch, url, enabled, expected_enabled, expected_url_set):
    """Exercise every combination of (TIMESFM_URL, TIMESFM_ENABLED)."""
    if url:
        monkeypatch.setenv("TIMESFM_URL", url)
    else:
        monkeypatch.delenv("TIMESFM_URL", raising=False)
    if enabled:
        monkeypatch.setenv("TIMESFM_ENABLED", enabled)
    else:
        monkeypatch.delenv("TIMESFM_ENABLED", raising=False)

    mod = _reload_runtime()
    rc = mod.RuntimeConfig()

    assert rc.timesfm_enabled is expected_enabled, (
        f"For TIMESFM_URL={url!r} TIMESFM_ENABLED={enabled!r} "
        f"expected timesfm_enabled={expected_enabled} got {rc.timesfm_enabled}"
    )
    if expected_url_set:
        assert rc.timesfm_url == url, (
            f"timesfm_url should equal {url!r}; got {rc.timesfm_url!r}"
        )
    else:
        # No URL set — falls back to the canonical 3.96.151.28 default.
        assert "16.52.14.182" not in rc.timesfm_url, (
            "Legacy IP must not appear as fallback"
        )


def test_no_legacy_ip_in_runtime_config():
    """Static guard: runtime_config.py source must not contain legacy IP."""
    src = (ENGINE_ROOT / "config" / "runtime_config.py").read_text()
    assert "16.52.14.182" not in src, (
        "engine/config/runtime_config.py still contains legacy IP 16.52.14.182. "
        "Use 3.96.151.28 (audit #397 review fix 2)."
    )
