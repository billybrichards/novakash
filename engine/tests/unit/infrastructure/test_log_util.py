"""Unit tests for ``infrastructure.log_util.exc_log_fields``.

The helper is the single canonical way to emit ``error=`` / ``error_type=``
fields from a caught exception inside the engine. The whole reason it exists
is that ``str(asyncio.TimeoutError())`` is the empty string, which was
silently producing useless ``error=`` warnings everywhere the pool's
``acquire(timeout=N)`` was timing out (see RDS note for context).

These tests pin the contract so any future refactor that breaks the
"never emit an empty error string" guarantee fails fast.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from infrastructure.log_util import DEFAULT_MAX_LEN, exc_log_fields


# ── Behaviour ────────────────────────────────────────────────────────────


def test_returns_both_error_and_error_type_for_message_bearing_exc():
    fields = exc_log_fields(ValueError("boom"))
    assert fields["error"] == "boom"
    assert fields["error_type"] == "ValueError"


def test_falls_back_to_repr_when_str_is_empty():
    # asyncio.TimeoutError has empty __str__ — this is THE bug we're fixing.
    fields = exc_log_fields(asyncio.TimeoutError())
    assert fields["error"], "error must never be empty for a real exception"
    assert "TimeoutError" in fields["error"]
    assert fields["error_type"] == "TimeoutError"


def test_falls_back_to_repr_for_custom_empty_str_exception():
    class _Silent(RuntimeError):
        def __str__(self) -> str:  # noqa: D401
            return ""

    fields = exc_log_fields(_Silent())
    # repr is "_Silent()" — proves we don't return an empty string.
    assert fields["error"] != ""
    assert fields["error_type"] == "_Silent"


def test_truncates_to_max_len():
    long = "x" * 5000
    fields = exc_log_fields(ValueError(long), max_len=50)
    assert len(fields["error"]) == 50
    assert fields["error_type"] == "ValueError"


def test_default_max_len_constant_is_used():
    long = "y" * (DEFAULT_MAX_LEN * 2)
    fields = exc_log_fields(ValueError(long))
    assert len(fields["error"]) == DEFAULT_MAX_LEN


def test_preserves_non_ascii_message():
    fields = exc_log_fields(ValueError("café — résumé"))
    assert "café" in fields["error"]
    assert fields["error_type"] == "ValueError"


def test_handles_base_exception_subclass():
    # We accept BaseException so callers can pass GeneratorExit / KeyboardInterrupt
    # safely — the helper is also used in cancellation paths.
    fields = exc_log_fields(KeyboardInterrupt())
    assert fields["error_type"] == "KeyboardInterrupt"
    assert fields["error"] != ""
