"""Tests for ``infrastructure.wallet_rpc.WalletRPCReader``.

The reader's contract: NEVER return 0.0 unless the on-chain balance is
genuinely 0. Specifically:

  - All 3 RPCs fail and no LKG → returns None
  - All 3 RPCs fail but LKG cached → returns LKG
  - One RPC returns a stray value → consensus picks the majority
  - Three readings all disagree → keeps LKG (warn), no fresh value emitted

These guarantees fix the "Wallet: $0.00" + WALLET DRIFT false-alarm bug.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from infrastructure.wallet_rpc import WalletRPCReader


def _make(lkg: float | None = None) -> WalletRPCReader:
    r = WalletRPCReader(
        wallet_address="0x000000000000000000000000000000000000dEaD",
        primary_rpc="https://rpc-primary.test",
        fallback_rpcs=("https://rpc-fb1.test", "https://rpc-fb2.test"),
    )
    if lkg is not None:
        r._last_known_good = lkg
    return r


@pytest.mark.asyncio
async def test_all_three_agree_returns_value():
    r = _make()
    with patch.object(WalletRPCReader, "_fetch_one", side_effect=[42.5, 42.5, 42.5]):
        v = await r.get_balance()
    assert v == 42.5
    assert r.last_known_good == 42.5


@pytest.mark.asyncio
async def test_two_of_three_agree_returns_majority():
    r = _make()
    # One RPC returns a stale value, two agree on fresh.
    with patch.object(
        WalletRPCReader, "_fetch_one", side_effect=[42.5, 42.5, 99.0]
    ):
        v = await r.get_balance()
    assert v == 42.5


@pytest.mark.asyncio
async def test_all_disagree_keeps_lkg_returns_lkg():
    r = _make(lkg=37.0)
    with patch.object(
        WalletRPCReader, "_fetch_one", side_effect=[10.0, 20.0, 30.0]
    ):
        v = await r.get_balance()
    # No 2-of-3 agreement → must NOT return any of those values, must
    # keep LKG to avoid false WALLET DRIFT.
    assert v == 37.0


@pytest.mark.asyncio
async def test_all_three_fail_returns_lkg_when_cached():
    r = _make(lkg=12.34)
    with patch.object(WalletRPCReader, "_fetch_one", side_effect=[None, None, None]):
        v = await r.get_balance()
    assert v == 12.34


@pytest.mark.asyncio
async def test_all_three_fail_returns_none_when_no_lkg():
    r = _make(lkg=None)
    with patch.object(WalletRPCReader, "_fetch_one", side_effect=[None, None, None]):
        v = await r.get_balance()
    # The critical bug being fixed: never coerce to 0.0.
    assert v is None


@pytest.mark.asyncio
async def test_all_three_fail_does_not_return_zero():
    """Regression guard for the exact bug being fixed: a triple RPC
    failure used to fall through to ``float(... or 0)`` returning 0.0,
    which made the snapshot card show "Wallet: $0.00" and triggered a
    bogus WALLET DRIFT alert.
    """
    r = _make(lkg=None)
    with patch.object(WalletRPCReader, "_fetch_one", side_effect=[None, None, None]):
        v = await r.get_balance()
    assert v != 0.0
    assert v is None


@pytest.mark.asyncio
async def test_genuine_zero_balance_returned():
    """Sanity check: when the wallet really IS empty, we surface that.

    Otherwise we'd be replacing one wrong-zero with another wrong-zero.
    """
    r = _make()
    with patch.object(WalletRPCReader, "_fetch_one", side_effect=[0.0, 0.0, 0.0]):
        v = await r.get_balance()
    assert v == 0.0
    assert r.last_known_good == 0.0


@pytest.mark.asyncio
async def test_only_one_rpc_succeeds_returns_that_value():
    """When 2 of 3 RPCs fail and only 1 returns, we accept it (no peer
    to disagree with). Better than dropping the read entirely."""
    r = _make()
    with patch.object(
        WalletRPCReader, "_fetch_one", side_effect=[None, 55.5, None]
    ):
        v = await r.get_balance()
    assert v == 55.5


@pytest.mark.asyncio
async def test_consensus_tolerates_subcent_drift():
    """Different Polygon nodes round at slightly different blocks; we
    treat <1¢ apart as agreement."""
    r = _make()
    with patch.object(
        WalletRPCReader,
        "_fetch_one",
        side_effect=[42.501, 42.502, 99.0],
    ):
        v = await r.get_balance()
    # Two within tolerance — majority emerges.
    assert v in (42.501, 42.502)


@pytest.mark.asyncio
async def test_unconfigured_reader_returns_lkg_without_calling_rpcs():
    r = WalletRPCReader(
        wallet_address=None,
        primary_rpc=None,
        fallback_rpcs=(),
    )
    r._last_known_good = 5.0
    v = await r.get_balance()
    assert v == 5.0
