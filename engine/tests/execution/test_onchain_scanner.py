"""
Tests for OnChainPositionScanner — multi-RPC consensus reader.

Verifies:
  - Constructor requires >= 2 RPC URLs.
  - scan_redeemable returns positions where payoutDenominator > 0 AND balance > 0.
  - Positions with zero balance or unresolved (payoutDenominator = 0) are excluded.
  - Multi-RPC consensus: disagreeing RPCs cause the position to be skipped.
  - Single RPC failure is tolerated if the other succeeds (but need 2+ successes).
  - RPC errors don't crash the scan.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from execution.onchain_scanner import (
    OnChainPositionScanner,
    RedeemablePosition,
    _compute_position_id,
)

FAKE_RPC_1 = "https://rpc1.polygon.test"
FAKE_RPC_2 = "https://rpc2.polygon.test"
FAKE_PROXY = "0x" + "A" * 40
FAKE_CONDITION = "0x" + "ab" * 32


# ── Constructor validation ───────────────────────────────────────────────────


def test_requires_at_least_two_rpcs():
    with pytest.raises(ValueError, match="at least 2"):
        OnChainPositionScanner(
            rpc_urls=["https://single.rpc"],
            proxy_address=FAKE_PROXY,
        )


def test_accepts_two_rpcs():
    scanner = OnChainPositionScanner(
        rpc_urls=[FAKE_RPC_1, FAKE_RPC_2],
        proxy_address=FAKE_PROXY,
    )
    assert len(scanner._rpc_urls) == 2


# ── Position ID computation ─────────────────────────────────────────────────


def test_compute_position_id_deterministic():
    """Same inputs always produce the same positionId."""
    cid = "ab" * 32
    pid1 = _compute_position_id(cid, 1)
    pid2 = _compute_position_id(cid, 1)
    assert pid1 == pid2
    assert isinstance(pid1, int)
    assert pid1 > 0


def test_compute_position_id_different_index_sets():
    """YES (indexSet=1) and NO (indexSet=2) produce different positionIds."""
    cid = "ab" * 32
    yes_id = _compute_position_id(cid, 1)
    no_id = _compute_position_id(cid, 2)
    assert yes_id != no_id


# ── Scan with mocked RPCs ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scan_resolved_with_balance():
    """A resolved position with non-zero balance is returned as redeemable."""
    scanner = OnChainPositionScanner(
        rpc_urls=[FAKE_RPC_1, FAKE_RPC_2],
        proxy_address=FAKE_PROXY,
    )

    async def mock_query_rpc(rpc_url, condition_id):
        return {
            "payout_denominator": 2,
            "yes_balance": 1000000,
            "no_balance": 0,
        }

    scanner._query_rpc = AsyncMock(side_effect=mock_query_rpc)

    result = await scanner.scan_redeemable([FAKE_CONDITION])
    assert len(result) == 1
    assert result[0].condition_id == FAKE_CONDITION
    assert result[0].yes_balance == 1000000
    assert result[0].no_balance == 0
    assert result[0].is_resolved is True


@pytest.mark.asyncio
async def test_scan_unresolved_excluded():
    """Unresolved positions (payoutDenominator = 0) are NOT redeemable."""
    scanner = OnChainPositionScanner(
        rpc_urls=[FAKE_RPC_1, FAKE_RPC_2],
        proxy_address=FAKE_PROXY,
    )

    async def mock_query_rpc(rpc_url, condition_id):
        return {
            "payout_denominator": 0,
            "yes_balance": 1000000,
            "no_balance": 0,
        }

    scanner._query_rpc = AsyncMock(side_effect=mock_query_rpc)

    result = await scanner.scan_redeemable([FAKE_CONDITION])
    assert len(result) == 0


@pytest.mark.asyncio
async def test_scan_zero_balance_excluded():
    """Resolved positions with zero balance are NOT redeemable."""
    scanner = OnChainPositionScanner(
        rpc_urls=[FAKE_RPC_1, FAKE_RPC_2],
        proxy_address=FAKE_PROXY,
    )

    async def mock_query_rpc(rpc_url, condition_id):
        return {
            "payout_denominator": 2,
            "yes_balance": 0,
            "no_balance": 0,
        }

    scanner._query_rpc = AsyncMock(side_effect=mock_query_rpc)

    result = await scanner.scan_redeemable([FAKE_CONDITION])
    assert len(result) == 0


@pytest.mark.asyncio
async def test_scan_consensus_mismatch_skipped():
    """When RPCs disagree on payoutDenominator, the position is skipped."""
    scanner = OnChainPositionScanner(
        rpc_urls=[FAKE_RPC_1, FAKE_RPC_2],
        proxy_address=FAKE_PROXY,
    )

    call_count = 0

    async def mock_query_rpc(rpc_url, condition_id):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"payout_denominator": 2, "yes_balance": 1000, "no_balance": 0}
        else:
            return {"payout_denominator": 0, "yes_balance": 1000, "no_balance": 0}

    scanner._query_rpc = AsyncMock(side_effect=mock_query_rpc)

    result = await scanner.scan_redeemable([FAKE_CONDITION])
    assert len(result) == 0


@pytest.mark.asyncio
async def test_scan_one_rpc_fails_insufficient():
    """If only 1 out of 2 RPCs responds, consensus fails (need 2+ successes)."""
    scanner = OnChainPositionScanner(
        rpc_urls=[FAKE_RPC_1, FAKE_RPC_2],
        proxy_address=FAKE_PROXY,
    )

    call_count = 0

    async def mock_query_rpc(rpc_url, condition_id):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"payout_denominator": 2, "yes_balance": 1000, "no_balance": 0}
        else:
            raise ConnectionError("RPC timeout")

    scanner._query_rpc = AsyncMock(side_effect=mock_query_rpc)

    result = await scanner.scan_redeemable([FAKE_CONDITION])
    assert len(result) == 0


@pytest.mark.asyncio
async def test_scan_three_rpcs_one_fails():
    """With 3 RPCs and 1 failure, consensus of 2 agreeing RPCs is sufficient."""
    scanner = OnChainPositionScanner(
        rpc_urls=[FAKE_RPC_1, FAKE_RPC_2, "https://rpc3.polygon.test"],
        proxy_address=FAKE_PROXY,
    )

    call_count = 0

    async def mock_query_rpc(rpc_url, condition_id):
        nonlocal call_count
        call_count += 1
        if call_count == 3:
            raise ConnectionError("RPC timeout")
        return {"payout_denominator": 2, "yes_balance": 500, "no_balance": 500}

    scanner._query_rpc = AsyncMock(side_effect=mock_query_rpc)

    result = await scanner.scan_redeemable([FAKE_CONDITION])
    assert len(result) == 1
    assert result[0].yes_balance == 500
    assert result[0].no_balance == 500


@pytest.mark.asyncio
async def test_scan_multiple_conditions():
    """Multiple condition IDs are scanned; only redeemable ones returned."""
    scanner = OnChainPositionScanner(
        rpc_urls=[FAKE_RPC_1, FAKE_RPC_2],
        proxy_address=FAKE_PROXY,
    )

    cid_resolved = "0x" + "aa" * 32
    cid_unresolved = "0x" + "bb" * 32

    async def mock_query_rpc(rpc_url, condition_id):
        if condition_id == cid_resolved:
            return {"payout_denominator": 2, "yes_balance": 1000, "no_balance": 0}
        else:
            return {"payout_denominator": 0, "yes_balance": 500, "no_balance": 500}

    scanner._query_rpc = AsyncMock(side_effect=mock_query_rpc)

    result = await scanner.scan_redeemable([cid_resolved, cid_unresolved])
    assert len(result) == 1
    assert result[0].condition_id == cid_resolved


@pytest.mark.asyncio
async def test_scan_empty_list():
    """Empty condition list returns empty result."""
    scanner = OnChainPositionScanner(
        rpc_urls=[FAKE_RPC_1, FAKE_RPC_2],
        proxy_address=FAKE_PROXY,
    )
    result = await scanner.scan_redeemable([])
    assert result == []
