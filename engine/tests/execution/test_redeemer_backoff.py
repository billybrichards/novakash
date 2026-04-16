"""Task #196 — Redeemer exponential backoff on 429.

Verifies:
  - Backoff doubles on consecutive 429s (30s → 60s → 120s → …).
  - Backoff caps at 30 min (1800s).
  - ``_consecutive_429`` resets on a non-429 success.
  - ``redeem_position`` skips the call while inside the backoff window and
    emits ``redeemer.backoff_skip`` without consuming a quota unit.
  - ``cooldown_status()`` exposes ``backoff_active``,
    ``backoff_remaining_seconds``, ``consecutive_429_count``.
  - ``upsert_redeemer_state`` writes all three new columns to the DB.

Mocks the relay client + web3 contract so no network or chain calls fire.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from execution.redeemer import (
    PositionRedeemer,
    _BACKOFF_BASE_SECONDS,
    _BACKOFF_CAP_SECONDS,
)


def _make_redeemer() -> PositionRedeemer:
    """Build a paper-mode redeemer with relay+web3 stubs so redeem_position
    can reach its try-block without touching network/chain."""
    r = PositionRedeemer(
        rpc_url="https://test.invalid",
        private_key="0x" + "0" * 64,
        proxy_address="0x" + "0" * 40,
        paper_mode=False,  # we need the real guard paths
    )
    # Minimal relay + ctf stubs. The actual redeem_position() path below
    # will be intercepted before it reaches real calldata, because each
    # test either primes cooldown/backoff state or forces a 429 via
    # `asyncio.to_thread(execute, …)` raising.
    r._relay_client = MagicMock()
    r._ctf = MagicMock()
    # Contract call path returns an opaque object with _encode_transaction_data;
    # we short-circuit via monkeypatched to_thread in the success test below.
    return r


# ── Backoff doubling ──────────────────────────────────────────────────────────


def test_backoff_doubles_on_consecutive_429s():
    """Each 429 doubles the sleep window: 30s, 60s, 120s, …"""
    r = _make_redeemer()

    assert r._consecutive_429 == 0
    assert r._backoff_until is None

    # First 429
    r._trip_cooldown("RelayerApiException[status_code=429, error_message={"
                     "'error': 'quota exceeded: 0 units remaining, resets in 100 seconds'}]")
    assert r._consecutive_429 == 1
    first_delta = (r._backoff_until - datetime.now(timezone.utc)).total_seconds()
    assert abs(first_delta - _BACKOFF_BASE_SECONDS) < 2  # ≈ 30s

    # Second 429 — doubles to 60s
    r._trip_cooldown("429 quota exceeded")
    assert r._consecutive_429 == 2
    second_delta = (r._backoff_until - datetime.now(timezone.utc)).total_seconds()
    assert abs(second_delta - (_BACKOFF_BASE_SECONDS * 2)) < 2  # ≈ 60s

    # Third — 120s
    r._trip_cooldown("429 quota exceeded")
    assert r._consecutive_429 == 3
    third_delta = (r._backoff_until - datetime.now(timezone.utc)).total_seconds()
    assert abs(third_delta - (_BACKOFF_BASE_SECONDS * 4)) < 2  # ≈ 120s


def test_backoff_caps_at_30_minutes():
    """After enough consecutive 429s, backoff clamps at MAX (30 min)."""
    r = _make_redeemer()

    # 30 × 2^N is monotonically growing; fire 10 times to blow past the cap.
    for _ in range(10):
        r._trip_cooldown("429 quota exceeded")

    assert r._consecutive_429 == 10
    delta = (r._backoff_until - datetime.now(timezone.utc)).total_seconds()
    assert delta <= _BACKOFF_CAP_SECONDS + 1  # cap respected
    assert delta >= _BACKOFF_CAP_SECONDS - 2  # close to cap


def test_backoff_resets_on_success():
    """A non-429 success clears ``_consecutive_429`` and the backoff window."""
    r = _make_redeemer()
    r._trip_cooldown("429")
    r._trip_cooldown("429")
    assert r._consecutive_429 == 2
    assert r._backoff_until is not None

    r._clear_backoff_on_success()

    assert r._consecutive_429 == 0
    assert r._backoff_until is None
    # And subsequent trip starts fresh at BASE, not at BASE × 2^2.
    r._trip_cooldown("429")
    assert r._consecutive_429 == 1
    delta = (r._backoff_until - datetime.now(timezone.utc)).total_seconds()
    assert abs(delta - _BACKOFF_BASE_SECONDS) < 2


# ── Skip-tick behaviour ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_redeem_position_skips_while_backoff_active(monkeypatch):
    """When inside a backoff window, redeem_position returns False WITHOUT
    submitting to the relayer (no quota unit burned)."""
    r = _make_redeemer()
    # Prime: backoff is active but outer cooldown has drained.
    r._backoff_until = datetime.now(timezone.utc) + timedelta(seconds=60)
    r._consecutive_429 = 2
    r._rate_limit_until = None  # base cooldown cleared

    # Sentinel — if redeem_position ever reaches relay_client.execute we fail.
    def _never_called(*_args, **_kwargs):
        raise AssertionError("relay_client.execute must NOT be called while in backoff")

    r._relay_client.execute = _never_called

    result = await r.redeem_position("0x" + "ab" * 32)

    assert result is False
    # Still in backoff (unchanged by the skip)
    assert r._in_backoff()
    assert r._consecutive_429 == 2


@pytest.mark.asyncio
async def test_redeem_position_logs_backoff_skip(monkeypatch):
    """Skipping while in backoff emits a ``redeemer.backoff_skip`` log line."""
    r = _make_redeemer()
    r._backoff_until = datetime.now(timezone.utc) + timedelta(seconds=45)
    r._consecutive_429 = 1
    r._rate_limit_until = None

    logged = []
    monkeypatch.setattr(r, "_log", SimpleNamespace(
        info=lambda event, **kw: logged.append((event, kw)),
        warning=lambda event, **kw: logged.append((event, kw)),
        error=lambda event, **kw: logged.append((event, kw)),
        debug=lambda event, **kw: logged.append((event, kw)),
    ))

    await r.redeem_position("0x" + "cd" * 32)

    events = [e for (e, _kw) in logged]
    assert "redeemer.backoff_skip" in events
    # And the call passed along the consecutive_429 counter for ops visibility
    skip_kw = dict(
        [(e, kw) for (e, kw) in logged if e == "redeemer.backoff_skip"][0][1]
    )
    assert skip_kw.get("consecutive_429") == 1


# ── cooldown_status() new fields ──────────────────────────────────────────────


def test_cooldown_status_exposes_backoff_fields_when_idle():
    """When neither cooldown nor backoff is active, all three new keys are
    present with default/zero values."""
    r = _make_redeemer()
    cd = r.cooldown_status()
    assert "backoff_active" in cd
    assert "backoff_remaining_seconds" in cd
    assert "consecutive_429_count" in cd
    assert cd["backoff_active"] is False
    assert cd["backoff_remaining_seconds"] == 0
    assert cd["consecutive_429_count"] == 0


def test_cooldown_status_exposes_backoff_fields_when_active():
    """After a 429, cooldown_status reports backoff-active with the live
    remaining seconds + counter."""
    r = _make_redeemer()
    r._trip_cooldown("429 quota exceeded")

    cd = r.cooldown_status()
    assert cd["backoff_active"] is True
    # First 429 sleeps BASE (≈30s); we allow ±2s slack for wall-clock drift
    assert _BACKOFF_BASE_SECONDS - 2 <= cd["backoff_remaining_seconds"] <= _BACKOFF_BASE_SECONDS + 1
    assert cd["consecutive_429_count"] == 1


# ── upsert_redeemer_state persists new backoff columns ────────────────────────


@pytest.mark.asyncio
async def test_upsert_redeemer_state_writes_backoff_fields():
    """Task #196 DB contract — upsert_redeemer_state must persist the 3
    new backoff columns (Task spec: schema additive, not breaking).
    """
    from persistence.db_client import DBClient

    client = DBClient.__new__(DBClient)

    # Fake pool + conn that records the INSERT args.
    captured = {}

    class _FakeConn:
        async def execute(self, sql, *args):
            captured["sql"] = sql
            captured["args"] = args

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    class _Acquire:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, *a):
            return None

    class _FakePool:
        def acquire(self):
            return _Acquire()

    client._pool = _FakePool()

    cooldown = {
        "active": True,
        "remaining_seconds": 600,
        "resets_at": "2026-04-16T12:00:00Z",
        "reason": "quota exceeded",
        "backoff_active": True,
        "backoff_remaining_seconds": 120,
        "consecutive_429_count": 2,
    }
    await client.upsert_redeemer_state(cooldown, 100, 7)

    assert "backoff_active" in captured["sql"]
    assert "backoff_remaining_seconds" in captured["sql"]
    assert "consecutive_429_count" in captured["sql"]

    # Final three positional args should be the backoff values.
    args = captured["args"]
    # Order in the INSERT: cooldown_active, remaining, resets, reason,
    # limit, used, backoff_active, backoff_remaining, consecutive_429.
    assert args[6] is True           # backoff_active
    assert args[7] == 120            # backoff_remaining_seconds
    assert args[8] == 2              # consecutive_429_count


@pytest.mark.asyncio
async def test_upsert_redeemer_state_defaults_missing_backoff_fields():
    """Backward compat — old callers that pass a 4-key cooldown still work.
    Missing backoff keys default to False/0."""
    from persistence.db_client import DBClient

    client = DBClient.__new__(DBClient)

    captured = {}

    class _FakeConn:
        async def execute(self, sql, *args):
            captured["args"] = args

    class _Acquire:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, *a):
            return None

    class _FakePool:
        def acquire(self):
            return _Acquire()

    client._pool = _FakePool()

    legacy_cooldown = {
        "active": False,
        "remaining_seconds": 0,
        "resets_at": None,
        "reason": "",
    }
    await client.upsert_redeemer_state(legacy_cooldown, 100, 0)

    args = captured["args"]
    assert args[6] is False
    assert args[7] == 0
    assert args[8] == 0
