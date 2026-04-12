"""
Tests for PredictionRecorder (PE-06).

The prediction recorder writes a row per (asset, delta) to
`ticks_elm_predictions`, whose `feature_age_ms` column is JSONB. The
recorder was formerly serialising the freshness dict with `str()`, which
produces Python-literal repr (single-quoted keys/strings). Postgres's
JSONB parser rejects that with:

    invalid input syntax for type json
    DETAIL: Token "'" is invalid.

The handler in `_record_sweep` catches the exception and logs
`prediction_recorder.write_error`, so the failure is silent and drops every
row in the batch. That silently biases the V10.6 backtest evidence
base (865 outcomes) — this is an observability-path bug with model
evaluation consequences.

The tests below pin the fix:

  1. The value passed to asyncpg for the JSONB column MUST be valid
     JSON text — parseable by `json.loads` and containing only
     double-quoted strings (no Python-repr single quotes).
  2. Payloads whose values contain single quotes (`"it's"`,
     `"market's move"`, etc.) must round-trip without blowing up.
  3. Nested dicts/lists with single-quoted strings must also
     round-trip — this is the bug-class variant to guard against.

These tests fail against the unfixed `str(result.get(...))` code
and pass once `json.dumps` is used instead. Run both directions to
confirm before merging.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import sys
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest


# Load the recorder module directly from its file path so we don't have
# to execute `data/feeds/__init__.py`, which pulls in optional third-party
# deps (web3, aiohttp, etc.) unrelated to this unit test.
_RECORDER_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "data"
    / "feeds"
    / "prediction_recorder.py"
)
_spec = importlib.util.spec_from_file_location(
    "_prediction_recorder_under_test", _RECORDER_PATH
)
assert _spec is not None and _spec.loader is not None
_recorder_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _recorder_mod
_spec.loader.exec_module(_recorder_mod)

ASSETS = _recorder_mod.ASSETS
DELTAS = _recorder_mod.DELTAS
PredictionRecorder = _recorder_mod.PredictionRecorder


# ────────────────────────────────────────────────────────────────────
#  Mock pool / connection plumbing
# ────────────────────────────────────────────────────────────────────


class _MockConnection:
    """A stand-in asyncpg Connection that records calls to executemany.

    We implement executemany so the recorder's batched insert path is
    exercised, plus execute so `_ensure_table` doesn't blow up when the
    recorder is constructed with `table_ensured=True` already set.
    """

    def __init__(self) -> None:
        self.executemany_calls: List[tuple[str, List[tuple]]] = []
        self.execute_calls: List[str] = []

    async def executemany(self, query: str, rows: List[tuple]) -> None:
        self.executemany_calls.append((query, rows))

    async def execute(self, query: str, *args: Any) -> None:
        self.execute_calls.append(query)


class _MockPool:
    """Async-context-manager pool that hands out a single _MockConnection."""

    def __init__(self) -> None:
        self.conn = _MockConnection()

    def acquire(self) -> "_AcquireCtx":
        return _AcquireCtx(self.conn)


class _AcquireCtx:
    def __init__(self, conn: _MockConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _MockConnection:
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _StubClient:
    """Canned ML client: returns a fixed payload for every call."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload
        self.calls: List[Dict[str, Any]] = []

    async def get_probability(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        return self._payload


# ────────────────────────────────────────────────────────────────────
#  Helpers
# ────────────────────────────────────────────────────────────────────


def _make_recorder(payload: Dict[str, Any]) -> tuple[PredictionRecorder, _MockPool]:
    pool = _MockPool()
    client = _StubClient(payload)
    shutdown = asyncio.Event()
    rec = PredictionRecorder(
        elm_client=client, db_pool=pool, shutdown_event=shutdown
    )
    # Skip the CREATE TABLE round-trip — not under test here.
    rec._table_ensured = True
    return rec, pool


def _extract_jsonb_values(rows: List[tuple]) -> List[Any]:
    """The recorder's INSERT has feature_age_ms as the LAST bound value."""
    return [row[-1] for row in rows]


# ────────────────────────────────────────────────────────────────────
#  PE-06 regression tests
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_feature_freshness_is_valid_json_text() -> None:
    """The JSONB bind must be parseable by json.loads — no Python repr."""
    payload = {
        "probability_up": 0.55,
        "probability_down": 0.45,
        "probability_raw": 0.54,
        "model_version": "sequoia-v5.2-test",
        "delta_bucket": 60,
        "feature_freshness_ms": {"BTC": 123.4, "ETH": 56.7},
    }
    rec, pool = _make_recorder(payload)

    await rec._record_sweep()

    # One executemany call containing one row per (asset, delta).
    assert len(pool.conn.executemany_calls) == 1
    query, rows = pool.conn.executemany_calls[0]
    assert "$7::jsonb" in query
    assert len(rows) == len(ASSETS) * len(DELTAS)

    for value in _extract_jsonb_values(rows):
        # Must be a string (asyncpg JSONB bind format), NOT a dict and
        # NOT a Python-repr of a dict.
        assert isinstance(value, str)
        # Python dict repr starts with `{'` — that's the bug signature.
        assert not value.startswith("{'"), (
            f"value={value!r} looks like str(dict) output. Use json.dumps."
        )
        # Must round-trip through json.loads without raising.
        parsed = json.loads(value)
        assert parsed == {"BTC": 123.4, "ETH": 56.7}


@pytest.mark.asyncio
async def test_single_quote_in_string_value_survives() -> None:
    """A value containing a single quote must not break the JSONB bind.

    This is the canonical shape that triggered PE-06 in production: the
    upstream scorer returned a string like `"market's stale"` and the
    recorder's `str(dict)` wrapped it in single quotes, yielding
    `{'BTC': "market's stale"}` — a Postgres-illegal literal.
    """
    payload = {
        "probability_up": 0.5,
        "probability_down": 0.5,
        "probability_raw": 0.5,
        "model_version": "sequoia-v5.2",
        "delta_bucket": 60,
        "feature_freshness_ms": {
            "BTC": "market's stale",
            "ETH": "can't compute",
            "note": "it's fine",
        },
    }
    rec, pool = _make_recorder(payload)

    await rec._record_sweep()

    query, rows = pool.conn.executemany_calls[0]
    assert "$7::jsonb" in query

    for value in _extract_jsonb_values(rows):
        assert isinstance(value, str)
        # Valid JSON must use double quotes for strings. The unfixed
        # code produced `{'BTC': "market's stale", ...}` which would
        # fail this parse.
        parsed = json.loads(value)
        assert parsed["BTC"] == "market's stale"
        assert parsed["ETH"] == "can't compute"
        assert parsed["note"] == "it's fine"


@pytest.mark.asyncio
async def test_nested_single_quoted_strings_survive() -> None:
    """Nested dicts/lists with single-quoted strings must round-trip.

    Guards against the bug-class variant where a top-level json.dumps
    would fix the outer payload but a nested str(dict) somewhere would
    still inject repr-style quoting.
    """
    payload = {
        "probability_up": 0.7,
        "probability_down": 0.3,
        "probability_raw": 0.69,
        "model_version": "sequoia-v5.2",
        "delta_bucket": 90,
        "feature_freshness_ms": {
            "BTC": {"age_ms": 12.5, "note": "it's fresh"},
            "sources": ["binance's ws", "tiingo's ws", "chainlink's rpc"],
        },
    }
    rec, pool = _make_recorder(payload)

    await rec._record_sweep()

    _query, rows = pool.conn.executemany_calls[0]
    for value in _extract_jsonb_values(rows):
        assert isinstance(value, str)
        parsed = json.loads(value)
        assert parsed["BTC"]["note"] == "it's fresh"
        assert parsed["sources"] == [
            "binance's ws",
            "tiingo's ws",
            "chainlink's rpc",
        ]


@pytest.mark.asyncio
async def test_missing_feature_freshness_ms_defaults_to_empty_object() -> None:
    """If the scorer omits or nulls the field, we should write `{}` not `None`."""
    payload = {
        "probability_up": 0.5,
        "probability_down": 0.5,
        "probability_raw": 0.5,
        "model_version": "sequoia-v5.2",
        "delta_bucket": 60,
        # feature_freshness_ms intentionally missing
    }
    rec, pool = _make_recorder(payload)

    await rec._record_sweep()

    _query, rows = pool.conn.executemany_calls[0]
    for value in _extract_jsonb_values(rows):
        assert isinstance(value, str)
        # Must be parseable — `None` or the string "None" would fail.
        parsed = json.loads(value)
        assert parsed == {}


@pytest.mark.asyncio
async def test_executemany_is_called_once_with_all_rows() -> None:
    """Sanity check: the recorder still batches the full asset×delta grid."""
    payload = {
        "probability_up": 0.5,
        "probability_down": 0.5,
        "probability_raw": 0.5,
        "model_version": "sequoia-v5.2",
        "delta_bucket": 60,
        "feature_freshness_ms": {},
    }
    rec, pool = _make_recorder(payload)

    await rec._record_sweep()

    assert len(pool.conn.executemany_calls) == 1
    _query, rows = pool.conn.executemany_calls[0]
    assert len(rows) == len(ASSETS) * len(DELTAS)
