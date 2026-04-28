"""Tests for CLOBReconciler wallet balance reading (PR fix/clob-wallet-and-fak-retry).

Context
-------
2026-04-27: USER REPORT — engine queries CLOB for wallet balance and gets
$0, but USDC.balanceOf(proxy) on Polygon RPC returns $295. The CLOB
``get_balance_allowance`` indexer is wedged. The reconciler reports $0 to
operators (TG card) and rebaselines bankroll on $0 — completely gating
trading.

Fix
----
``CLOBReconciler._read_wallet_balance()`` is now dual-source:
  1. Try ``WalletRPCReader.get_balance()`` — direct on-chain via 2-of-3
     consensus, never goes stale.
  2. Fall back to ``poly_client.get_balance()`` (CLOB) only when the
     reader returns None (no LKG yet AND every RPC failed).

These tests verify the precedence and fallback semantics at the
reconciler boundary.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_reconciler(*, wallet_rpc_reader=None, clob_balance=42.0):
    """Build a CLOBReconciler with mocked deps for balance-source tests."""
    from reconciliation.reconciler import CLOBReconciler

    mock_poly = AsyncMock()
    mock_poly.get_balance.return_value = clob_balance

    return CLOBReconciler(
        poly_client=mock_poly,
        db_pool=None,
        alerter=AsyncMock(),
        shutdown_event=asyncio.Event(),
        wallet_rpc_reader=wallet_rpc_reader,
    )


@pytest.mark.asyncio
async def test_reads_onchain_when_reader_returns_value():
    """Happy path: WalletRPCReader returns a real number, CLOB is never
    asked. This is the fix for the $0 CLOB reading.
    """
    reader = AsyncMock()
    reader.get_balance.return_value = 295.42
    rec = _make_reconciler(wallet_rpc_reader=reader, clob_balance=0.0)

    bal = await rec._read_wallet_balance()
    assert bal == 295.42
    reader.get_balance.assert_awaited_once()
    rec._poly.get_balance.assert_not_called()


@pytest.mark.asyncio
async def test_falls_back_to_clob_when_reader_returns_none():
    """Cold start path: reader has no LKG and every RPC failed -> None.
    Fall through to CLOB rather than crashing the poll loop.
    """
    reader = AsyncMock()
    reader.get_balance.return_value = None
    rec = _make_reconciler(wallet_rpc_reader=reader, clob_balance=42.0)

    bal = await rec._read_wallet_balance()
    assert bal == 42.0
    rec._poly.get_balance.assert_awaited_once()


@pytest.mark.asyncio
async def test_falls_back_to_clob_when_reader_raises():
    """If the on-chain reader raises (e.g. malformed RPC URL), the
    reconciler must not crash the poll loop — fall through to CLOB.
    """
    reader = AsyncMock()
    reader.get_balance.side_effect = RuntimeError("bad rpc url")
    rec = _make_reconciler(wallet_rpc_reader=reader, clob_balance=99.0)

    bal = await rec._read_wallet_balance()
    assert bal == 99.0


@pytest.mark.asyncio
async def test_uses_clob_when_reader_not_provided():
    """Back-compat: existing callers that didn't pass wallet_rpc_reader
    keep the legacy CLOB-only behaviour. No regressions in tests / non-
    production callers that haven't been updated.
    """
    rec = _make_reconciler(wallet_rpc_reader=None, clob_balance=11.11)

    bal = await rec._read_wallet_balance()
    assert bal == 11.11
    rec._poly.get_balance.assert_awaited_once()


@pytest.mark.asyncio
async def test_preserves_real_zero_balance():
    """A real $0 reading from on-chain (e.g. wallet drained) must be
    returned as-is, not interpreted as "fall through to CLOB". The
    fallback only triggers on ``None``, not ``0.0``.
    """
    reader = AsyncMock()
    reader.get_balance.return_value = 0.0
    rec = _make_reconciler(wallet_rpc_reader=reader, clob_balance=999.0)

    bal = await rec._read_wallet_balance()
    assert bal == 0.0
    rec._poly.get_balance.assert_not_called()
