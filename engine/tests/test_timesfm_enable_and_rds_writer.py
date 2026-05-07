"""Tests for audit #397 — TimesFM re-enable + ticks_timesfm RDS writer.

Three concerns pinned here:

1. CompositionRoot enable logic
   a. TIMESFM_URL set -> client wired, orchestrator.timesfm_v6_enabled logged.
   b. TIMESFM_URL absent -> client stays None, orchestrator.timesfm_v6_disabled logged.
   c. TIMESFM_URL set + TIMESFM_ENABLED=false -> explicitly disabled.
   d. TIMESFM_URL absent + TIMESFM_ENABLED=true -> warns + skips cleanly.

2. Static source guards
   - composition.py must not contain the legacy IP 16.52.14.182 as a fallback.
   - runtime.py must not contain the legacy IP 16.52.14.182 as a fallback.
   - tick_recorder.py docstring must NOT say 'Railway PostgreSQL'.
   - composition.py must emit orchestrator.timesfm_v6_enabled.

3. TickRecorder.record_timesfm_forecast writer
   - With a mock asyncpg pool -> asserts INSERT INTO ticks_timesfm fires.
   - With pool=None -> returns silently, never raises.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ENGINE_ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPOSITION_PATH = ENGINE_ROOT / "infrastructure" / "composition.py"
RUNTIME_PATH = ENGINE_ROOT / "infrastructure" / "runtime.py"
TICK_RECORDER_PATH = ENGINE_ROOT / "persistence" / "tick_recorder.py"

LEGACY_IP = "16.52.14.182"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_tick_recorder():
    """Load TickRecorder from source without triggering the full import graph."""
    spec = importlib.util.spec_from_file_location("_tick_recorder_ut", TICK_RECORDER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ===========================================================================
# Part 1 — Static source guards
# ===========================================================================


def test_composition_no_legacy_ip_fallback() -> None:
    """composition.py must not use the legacy TimesFM IP as a hardcoded fallback."""
    src = _read(COMPOSITION_PATH)
    assert LEGACY_IP not in src, (
        f"infrastructure/composition.py still contains the stale legacy IP "
        f"'{LEGACY_IP}'. Update all fallback URLs to 3.96.151.28 (audit #397)."
    )


def test_runtime_no_legacy_ip_fallback() -> None:
    """runtime.py must not use the legacy TimesFM IP as a hardcoded fallback."""
    src = _read(RUNTIME_PATH)
    assert LEGACY_IP not in src, (
        f"infrastructure/runtime.py still contains the stale legacy IP "
        f"'{LEGACY_IP}'. Update all fallback URLs to 3.96.151.28 (audit #397)."
    )


def test_engine_tree_no_legacy_ip_in_executable_code() -> None:
    """Audit #397 review fix #3: scan the entire engine tree for legacy IP
    and fail if any non-comment / non-docstring matches found.

    Scope: every .py and .yaml file under engine/ (excluding the tests
    directory which deliberately mentions the IP for guarding purposes).

    Acceptable matches:
      - Lines whose first non-whitespace char is '#' (comment)
      - Lines inside triple-quoted strings (docstrings)
      - Lines inside a YAML comment ('#')
    Anything else fails the test.
    """
    import re

    py_or_yaml = []
    for ext in ("*.py", "*.yaml", "*.yml"):
        py_or_yaml.extend(ENGINE_ROOT.rglob(ext))

    failures: list[str] = []
    for path in py_or_yaml:
        # Skip our own test directory — it intentionally references the IP
        # in test assertions / docstrings.
        if "tests" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if LEGACY_IP not in text:
            continue

        # Heuristic: walk line by line, track whether we're inside a Python
        # triple-quoted string. Any IP occurrence on a code line is a fail.
        in_triple = False
        triple_quote = ""
        for lineno, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            # Triple-quote tracker (handles """ and ''' on same line)
            if not in_triple:
                for q in ('"""', "'''"):
                    if q in line:
                        # Count occurrences — even count means closes on same line
                        if line.count(q) % 2 == 1:
                            in_triple = True
                            triple_quote = q
                            break
            else:
                if triple_quote in line:
                    in_triple = False
                    triple_quote = ""
                    continue  # the closing line itself is still in-string

            if LEGACY_IP not in raw:
                continue
            # Inside triple-quoted string -> docstring -> ok
            if in_triple:
                continue
            # Comment line -> ok
            if line.startswith("#"):
                continue
            # YAML inline comment -> ok if IP appears AFTER a '#'
            comment_pos = raw.find("#")
            ip_pos = raw.find(LEGACY_IP)
            if comment_pos >= 0 and ip_pos > comment_pos:
                continue
            failures.append(f"{path.relative_to(ENGINE_ROOT)}:{lineno}: {raw.rstrip()}")

    assert not failures, (
        "Legacy IP {ip} found in engine code (executable, not comments/docstrings):\n  "
        + "\n  ".join(failures)
        + "\n\nReplace with 3.96.151.28 or remove the hardcoded fallback (audit #397)."
    ).format(ip=LEGACY_IP)


def test_tick_recorder_docstring_not_railway() -> None:
    """tick_recorder.py docstring must no longer say 'Railway PostgreSQL'."""
    src = _read(TICK_RECORDER_PATH)
    assert "Railway PostgreSQL" not in src, (
        "persistence/tick_recorder.py still says 'Railway PostgreSQL'. "
        "The writer targets RDS via DATABASE_URL after Track-B cutover (audit #397)."
    )


def test_composition_emits_timesfm_v6_enabled_log() -> None:
    """composition.py must emit orchestrator.timesfm_v6_enabled."""
    src = _read(COMPOSITION_PATH)
    assert "orchestrator.timesfm_v6_enabled" in src, (
        "infrastructure/composition.py does not emit 'orchestrator.timesfm_v6_enabled'. "
        "Add log.info('orchestrator.timesfm_v6_enabled', ...) in the enabled branch "
        "(mirror of the disabled branch) — audit #397."
    )


def test_composition_enables_by_url_presence() -> None:
    """The enable logic must derive the default from URL presence."""
    src = _read(COMPOSITION_PATH)
    # The fix must contain bool(timesfm_url) or equivalent in the decision branch.
    assert "bool(timesfm_url)" in src, (
        "infrastructure/composition.py does not appear to enable TimesFM "
        "based on URL presence. The enable logic must set timesfm_enabled=True "
        "when TIMESFM_URL is set and TIMESFM_ENABLED is not explicitly false "
        "(audit #397)."
    )


# ===========================================================================
# Part 2 — Enable logic unit tests (env-var injection via source exec)
# ===========================================================================


def _exec_enable_block(monkeypatch, timesfm_url_val: str, timesfm_enabled_val: str) -> dict:
    """Exec the TimesFM enable block from composition.py in an isolated namespace."""
    if timesfm_url_val:
        monkeypatch.setenv("TIMESFM_URL", timesfm_url_val)
    else:
        monkeypatch.delenv("TIMESFM_URL", raising=False)
    if timesfm_enabled_val:
        monkeypatch.setenv("TIMESFM_ENABLED", timesfm_enabled_val)
    else:
        monkeypatch.delenv("TIMESFM_ENABLED", raising=False)
    monkeypatch.delenv("TIMESFM_MIN_CONFIDENCE", raising=False)

    src = _read(COMPOSITION_PATH)

    start_marker = "# -- v6.0 TimesFM-Only Strategy"
    alt_marker = "# ── v6.0 TimesFM-Only Strategy"
    end_marker = "        # v5.8: Inject TimesFM client"

    i_start = src.find(alt_marker)
    if i_start < 0:
        i_start = src.find(start_marker)
    i_end = src.find(end_marker, i_start)
    assert i_start >= 0 and i_end > i_start, (
        "Could not locate the TimesFM enable block in composition.py. "
        "Check that the section markers are still present."
    )
    block = src[i_start:i_end]

    # Dedent (block is inside a method, indented 8 spaces)
    dedented = "\n".join(
        line[8:] if line.startswith("        ") else line
        for line in block.splitlines()
    )

    import os as _os
    from pathlib import Path as _Path

    captured: list[dict] = []

    class _FakeLog:
        def info(self, event: str, **kwargs) -> None:
            captured.append({"event": event})
        def error(self, event: str, **kwargs) -> None:
            captured.append({"event": event, "level": "error"})
        def warning(self, event: str, **kwargs) -> None:
            captured.append({"event": event, "level": "warning"})

    class _FakeTimesFMClient:
        def __init__(self, base_url: str, timeout_seconds: float):
            self.base_url = base_url

    fake_self = MagicMock()
    fake_self._timesfm_client = None

    namespace = {
        "os": _os,
        "Path": _Path,
        "log": _FakeLog(),
        "TimesFMClient": _FakeTimesFMClient,
        "self": fake_self,
        # composition.py uses Path(__file__).parent.parent / ".env" to find the .env
        # file fallback. Point __file__ at composition.py itself so the .env path
        # resolves to the actual engine root; if no .env exists, the block falls
        # through cleanly (Path.exists() returns False).
        "__file__": str(COMPOSITION_PATH),
    }

    compiled = compile(dedented, "<timesfm_enable_block>", "exec")
    exec(compiled, namespace)  # noqa: S102

    return {
        "timesfm_enabled": namespace.get("timesfm_enabled"),
        "timesfm_url": namespace.get("timesfm_url", ""),
        "log_events": [e["event"] for e in captured],
    }


@pytest.mark.parametrize("url,enabled_flag,expect_enabled,expect_log_event", [
    # URL set, no explicit flag -> enabled by default (URL presence = intent)
    ("http://3.96.151.28:8080", "", True, "orchestrator.timesfm_v6_enabled"),
    # URL set, explicit true -> enabled
    ("http://3.96.151.28:8080", "true", True, "orchestrator.timesfm_v6_enabled"),
    # URL set, explicit false -> disabled
    ("http://3.96.151.28:8080", "false", False, "orchestrator.timesfm_v6_disabled"),
    # No URL, no flag -> disabled
    ("", "", False, "orchestrator.timesfm_v6_disabled"),
    # No URL, explicit true -> error + skip cleanly
    ("", "true", False, "orchestrator.timesfm_url_missing_skip"),
])
def test_timesfm_enable_logic(
    monkeypatch, url, enabled_flag, expect_enabled, expect_log_event
):
    """Parametrised: exercise all enable/disable paths."""
    result = _exec_enable_block(monkeypatch, url, enabled_flag)
    assert result["timesfm_enabled"] is expect_enabled, (
        f"Expected timesfm_enabled={expect_enabled} for url={url!r}, "
        f"TIMESFM_ENABLED={enabled_flag!r}. Got: {result['timesfm_enabled']!r}"
    )
    assert expect_log_event in result["log_events"], (
        f"Expected log event '{expect_log_event}' but got: {result['log_events']}"
    )


# ===========================================================================
# Part 3 — TickRecorder.record_timesfm_forecast writer
# ===========================================================================


@pytest.fixture
def tick_recorder_mod():
    return _load_tick_recorder()


@pytest.fixture
def mock_pool():
    """Return a minimal asyncpg.Pool-shaped mock."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool, conn


def _make_forecast(**overrides):
    m = MagicMock()
    defaults = {
        "direction": "UP",
        "confidence": 0.72,
        "predicted_close": 65432.1,
        "spread": 0.03,
        "p10": 64000.0,
        "p50": 65432.1,
        "p90": 67000.0,
        "delta_vs_open_pct": 0.8,
        "fetch_latency_ms": 42.0,
        "is_stale": False,
        "horizon": 90,
        "error": None,
    }
    defaults.update(overrides)
    for attr, val in defaults.items():
        setattr(m, attr, val)
    return m


@pytest.mark.asyncio
async def test_record_timesfm_forecast_inserts_row(tick_recorder_mod, mock_pool):
    """With a live pool, record_timesfm_forecast must execute an INSERT
    into ticks_timesfm with the expected columns."""
    pool, conn = mock_pool
    TickRecorder = tick_recorder_mod.TickRecorder
    recorder = TickRecorder(pool=pool)

    forecast = _make_forecast()
    await recorder.record_timesfm_forecast(
        forecast,
        asset="BTC",
        window_ts=1_746_000_000,
        window_close_ts=1_746_000_300,
        seconds_to_close=90,
    )

    conn.execute.assert_called_once()
    sql: str = conn.execute.call_args[0][0]

    assert "INSERT INTO ticks_timesfm" in sql, (
        f"Expected INSERT INTO ticks_timesfm but got:\n{sql}"
    )
    for col in ("ts", "asset", "window_ts", "direction", "confidence",
                "predicted_close", "p10", "p50", "p90", "is_stale"):
        assert col in sql, f"Column '{col}' missing from INSERT:\n{sql}"


@pytest.mark.asyncio
async def test_record_timesfm_forecast_no_pool_is_noop(tick_recorder_mod):
    """With pool=None the method must return silently without raising."""
    TickRecorder = tick_recorder_mod.TickRecorder
    recorder = TickRecorder(pool=None)
    forecast = _make_forecast()
    await recorder.record_timesfm_forecast(forecast, asset="BTC")


@pytest.mark.asyncio
async def test_record_timesfm_forecast_db_error_swallowed(tick_recorder_mod, mock_pool):
    """If the DB execute raises, the error must be swallowed (fire-and-forget)."""
    pool, conn = mock_pool
    conn.execute = AsyncMock(side_effect=Exception("connection timeout"))

    TickRecorder = tick_recorder_mod.TickRecorder
    recorder = TickRecorder(pool=pool)
    forecast = _make_forecast()
    await recorder.record_timesfm_forecast(forecast, asset="BTC")
