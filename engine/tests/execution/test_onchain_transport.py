"""
Tests for OnChainRedemptionTransport — direct factory.proxy() execution.

Verifies:
  - Nonce management: lazy init, increment on success, refetch on nonce-too-low.
  - redeem() builds correct calldata structure (inner + outer).
  - Successful tx receipt is parsed for USDC payout.
  - Reverted tx returns success=False with tx_hash.
  - Nonce-too-low triggers one retry with refetched nonce.
  - Insufficient funds does NOT increment nonce.
  - Unconnected transport returns error without crashing.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from execution.onchain_transport import (
    OnChainRedemptionTransport,
    RedeemResult,
    FACTORY_ADDRESS,
    CTF_ADDRESS,
    USDC_ADDRESS,
    GAS_LIMIT,
)

FAKE_RPC = "https://rpc.polygon.test"
FAKE_KEY = "0x" + "a1" * 32
FAKE_PROXY = "0x" + "B" * 40
FAKE_CONDITION = "ab" * 32


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_transport() -> OnChainRedemptionTransport:
    return OnChainRedemptionTransport(
        rpc_url=FAKE_RPC,
        private_key=FAKE_KEY,
        proxy_address=FAKE_PROXY,
    )


def _make_connected_transport(nonce: int = 10) -> OnChainRedemptionTransport:
    """Create a transport with mocked internals as if connect() ran."""
    t = _make_transport()
    t._w3 = MagicMock()
    t._eoa = "0x" + "C" * 40
    t._chain_id = 137
    t._local_nonce = nonce
    return t


# ── Unconnected transport ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_redeem_without_connect():
    """Calling redeem() before connect() returns a clear error."""
    t = _make_transport()
    result = await t.redeem(FAKE_CONDITION)
    assert result.success is False
    assert "not connected" in result.error


# ── Nonce management ────────────────────────────────────────────────────────


def test_nonce_increment():
    """_increment_nonce bumps the local counter."""
    t = _make_connected_transport(nonce=10)
    t._increment_nonce()
    assert t._local_nonce == 11


def test_nonce_increment_none_is_noop():
    """_increment_nonce does nothing if nonce is None."""
    t = _make_transport()
    assert t._local_nonce is None
    t._increment_nonce()
    assert t._local_nonce is None


# ── Calldata building ────────────────────────────────────────────────────────


def test_build_redeem_calldata_not_empty():
    """_build_redeem_calldata produces non-empty bytes."""
    t = _make_transport()
    calldata = t._build_redeem_calldata(FAKE_CONDITION)
    assert isinstance(calldata, bytes)
    assert len(calldata) > 4  # at least selector + some args


def test_build_redeem_calldata_starts_with_proxy_selector():
    """Outer calldata starts with the proxy() function selector."""
    from eth_utils import keccak

    t = _make_transport()
    calldata = t._build_redeem_calldata(FAKE_CONDITION)
    expected_selector = keccak(b"proxy((uint8,address,uint256,bytes)[])")[:4]
    assert calldata[:4] == expected_selector


def test_build_redeem_calldata_deterministic():
    """Same condition ID always produces the same calldata."""
    t = _make_transport()
    cd1 = t._build_redeem_calldata(FAKE_CONDITION)
    cd2 = t._build_redeem_calldata(FAKE_CONDITION)
    assert cd1 == cd2


def test_build_redeem_calldata_different_conditions():
    """Different condition IDs produce different calldata."""
    t = _make_transport()
    cd1 = t._build_redeem_calldata("aa" * 32)
    cd2 = t._build_redeem_calldata("bb" * 32)
    assert cd1 != cd2


def test_build_redeem_calldata_handles_0x_prefix():
    """0x-prefixed and bare hex produce the same calldata."""
    t = _make_transport()
    cd_bare = t._build_redeem_calldata("ab" * 32)
    cd_prefixed = t._build_redeem_calldata("0x" + "ab" * 32)
    assert cd_bare == cd_prefixed


# ── Successful redeem ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_redeem_success():
    """Successful on-chain redemption returns correct RedeemResult."""
    t = _make_connected_transport(nonce=10)

    fake_receipt = {
        "transactionHash": b"\xde\xad" * 16,
        "gasUsed": 250000,
        "status": 1,
        "logs": [],  # no USDC transfer in this test
    }

    async def mock_send_tx(calldata, nonce):
        return fake_receipt

    t._send_tx = mock_send_tx
    result = await t.redeem(FAKE_CONDITION)

    assert result.success is True
    assert result.gas_used == 250000
    assert result.payout_usdc == 0.0  # no Transfer logs
    assert t._local_nonce == 11  # incremented


# ── Reverted tx ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_redeem_reverted():
    """Reverted tx returns success=False but still has tx_hash."""
    t = _make_connected_transport(nonce=10)

    fake_receipt = {
        "transactionHash": b"\xbe\xef" * 16,
        "gasUsed": 100000,
        "status": 0,  # reverted
        "logs": [],
    }

    async def mock_send_tx(calldata, nonce):
        return fake_receipt

    t._send_tx = mock_send_tx
    result = await t.redeem(FAKE_CONDITION)

    assert result.success is False
    assert "reverted" in result.error
    assert result.gas_used == 100000
    assert t._local_nonce == 11  # still incremented (tx was mined)


# ── Nonce-too-low retry ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_redeem_nonce_too_low_retries():
    """Nonce-too-low triggers refetch and one retry."""
    t = _make_connected_transport(nonce=5)

    call_count = 0

    async def mock_send_tx(calldata, nonce):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("nonce too low")
        return {
            "transactionHash": b"\xca\xfe" * 16,
            "gasUsed": 200000,
            "status": 1,
            "logs": [],
        }

    t._send_tx = mock_send_tx

    async def mock_refetch():
        t._local_nonce = 10
        return 10

    t._refetch_nonce = mock_refetch

    result = await t.redeem(FAKE_CONDITION)

    assert result.success is True
    assert call_count == 2
    assert t._local_nonce == 11  # 10 from refetch + 1 increment


# ── Insufficient funds ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_redeem_insufficient_funds_no_increment():
    """Insufficient funds error does NOT increment nonce."""
    t = _make_connected_transport(nonce=10)

    async def mock_send_tx(calldata, nonce):
        raise Exception("insufficient funds for gas * price + value")

    t._send_tx = mock_send_tx

    result = await t.redeem(FAKE_CONDITION)
    assert result.success is False
    assert "insufficient funds" in result.error
    assert t._local_nonce == 10  # NOT incremented


# ── Generic error ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_redeem_generic_error():
    """Generic RPC error returns failure without increment."""
    t = _make_connected_transport(nonce=10)

    async def mock_send_tx(calldata, nonce):
        raise Exception("connection refused")

    t._send_tx = mock_send_tx

    result = await t.redeem(FAKE_CONDITION)
    assert result.success is False
    assert "connection refused" in result.error
    # Nonce not incremented (tx was never accepted)
    assert t._local_nonce == 10


# ── USDC payout parsing ─────────────────────────────────────────────────────


def test_parse_usdc_payout_from_logs():
    """_parse_usdc_payout extracts amount from Transfer event logs."""
    from eth_utils import keccak

    t = _make_transport()
    t._proxy_address = "0x" + "B" * 40

    transfer_topic = keccak(b"Transfer(address,address,uint256)")
    # "to" address padded to 32 bytes
    to_padded = bytes.fromhex("B" * 40).rjust(32, b"\x00")
    from_padded = bytes.fromhex("0" * 40).rjust(32, b"\x00")
    # amount = 5_000_000 (5 USDC at 6 decimals)
    amount_bytes = (5_000_000).to_bytes(32, "big")

    receipt = {
        "logs": [
            {
                "address": USDC_ADDRESS,
                "topics": [
                    transfer_topic,
                    from_padded,
                    to_padded,
                ],
                "data": amount_bytes,
            }
        ]
    }

    payout = t._parse_usdc_payout(receipt)
    assert payout == 5.0


def test_parse_usdc_payout_no_matching_logs():
    """No matching Transfer logs returns 0.0."""
    t = _make_transport()
    t._proxy_address = "0x" + "B" * 40

    receipt = {"logs": []}
    assert t._parse_usdc_payout(receipt) == 0.0


def test_parse_usdc_payout_wrong_contract():
    """Transfer from non-USDC contract is ignored."""
    from eth_utils import keccak

    t = _make_transport()
    t._proxy_address = "0x" + "B" * 40

    transfer_topic = keccak(b"Transfer(address,address,uint256)")
    to_padded = bytes.fromhex("B" * 40).rjust(32, b"\x00")
    from_padded = bytes.fromhex("0" * 40).rjust(32, b"\x00")
    amount_bytes = (1_000_000).to_bytes(32, "big")

    receipt = {
        "logs": [
            {
                "address": "0x" + "F" * 40,  # NOT USDC
                "topics": [transfer_topic, from_padded, to_padded],
                "data": amount_bytes,
            }
        ]
    }

    assert t._parse_usdc_payout(receipt) == 0.0


def test_parse_usdc_payout_wrong_recipient():
    """Transfer to a different address (not proxy) is ignored."""
    from eth_utils import keccak

    t = _make_transport()
    t._proxy_address = "0x" + "B" * 40

    transfer_topic = keccak(b"Transfer(address,address,uint256)")
    to_padded = bytes.fromhex("A" * 40).rjust(32, b"\x00")  # NOT proxy
    from_padded = bytes.fromhex("0" * 40).rjust(32, b"\x00")
    amount_bytes = (1_000_000).to_bytes(32, "big")

    receipt = {
        "logs": [
            {
                "address": USDC_ADDRESS,
                "topics": [transfer_topic, from_padded, to_padded],
                "data": amount_bytes,
            }
        ]
    }

    assert t._parse_usdc_payout(receipt) == 0.0
