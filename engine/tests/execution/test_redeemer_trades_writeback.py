"""Redeemer writes redemption state back onto the canonical trades table.

Before this was wired the Builder Relayer redeemer confirmed
redemptions on-chain but never persisted the outcome onto
``trades.redeemed`` / ``redemption_tx`` / ``redeemed_at``, so every
WIN row looked unredeemed forever. Postmortem: April 18 2026 — 1,285
live WIN rows, zero with ``redeemed=true``, zero with a
``redemption_tx`` populated.

This test suite pins:

  - Successful redemption calls ``trades_repo.mark_redeemed`` with the
    condition id and tx hash.
  - A DB failure inside mark_redeemed does NOT flip the redeem_position
    return value — the on-chain sweep already happened; accounting is
    best-effort.
  - Failed redemption (poll did not confirm) does NOT call mark_redeemed.
  - mark_redeemed is not called at all when the redeemer is constructed
    without a trades_repo (backwards compatibility).
"""
from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# The Builder Relayer SDK isn't installed in the unit-test virtualenv;
# stub the module so the import inside redeem_position() succeeds.
if "py_builder_relayer_client" not in sys.modules:
    _stub = ModuleType("py_builder_relayer_client")
    _models = ModuleType("py_builder_relayer_client.models")
    _models.Transaction = lambda **kwargs: SimpleNamespace(**kwargs)
    _stub.models = _models
    sys.modules["py_builder_relayer_client"] = _stub
    sys.modules["py_builder_relayer_client.models"] = _models

if "web3" not in sys.modules:
    _web3 = ModuleType("web3")
    _web3.Web3 = SimpleNamespace(to_checksum_address=lambda a: a)
    sys.modules["web3"] = _web3

from execution.redeemer import PositionRedeemer  # noqa: E402


def _make_redeemer(trades_repo: object | None = None) -> PositionRedeemer:
    """Build a live-mode redeemer with relay+web3 stubs."""
    r = PositionRedeemer(
        rpc_url="https://test.invalid",
        private_key="0x" + "0" * 64,
        proxy_address="0x" + "0" * 40,
        paper_mode=False,
        trades_repo=trades_repo,
    )
    r._relay_client = MagicMock()
    r._ctf = MagicMock()
    # Contract ABI encoder returns any truthy bytes — we never actually
    # sign or submit, the relay_client.execute is stubbed per-test.
    r._ctf.functions.redeemPositions.return_value._encode_transaction_data.return_value = (
        b"\x00"
    )
    return r


class _FakeTradesRepo:
    """Records mark_redeemed calls for assertions; mimics PgTradeRepository."""

    def __init__(self, rows_updated: int = 1, raise_on_call: bool = False):
        self.calls: list[dict] = []
        self._rows_updated = rows_updated
        self._raise = raise_on_call

    async def mark_redeemed(self, condition_id: str, tx_hash: str | None = None) -> int:
        self.calls.append({"condition_id": condition_id, "tx_hash": tx_hash})
        if self._raise:
            raise RuntimeError("simulated DB outage")
        return self._rows_updated


@pytest.mark.asyncio
async def test_successful_redeem_marks_trade_redeemed(monkeypatch):
    """Happy path: on CONFIRMED redemption, trades_repo.mark_redeemed fires
    with the condition id and tx hash extracted from the relayer response."""
    trades_repo = _FakeTradesRepo(rows_updated=2)
    r = _make_redeemer(trades_repo=trades_repo)

    # Stub the sync relay call invoked via asyncio.to_thread.
    async def fake_to_thread(fn, *args, **kwargs):
        if fn is r._relay_client.execute:
            return SimpleNamespace(
                transactionId="tx-id-123",
                transactionHash="0xdeadbeef",
            )
        if fn is r._relay_client.poll_until_state:
            return True  # CONFIRMED
        return None

    monkeypatch.setattr("execution.redeemer.asyncio.to_thread", fake_to_thread)

    condition = "0x" + "ab" * 32
    ok = await r.redeem_position(condition)

    assert ok is True
    assert len(trades_repo.calls) == 1
    assert trades_repo.calls[0]["condition_id"] == condition
    assert trades_repo.calls[0]["tx_hash"] == "0xdeadbeef"


@pytest.mark.asyncio
async def test_failed_redeem_does_not_mark_redeemed(monkeypatch):
    """poll_until_state returning False (not CONFIRMED) must NOT touch the
    trades table — on-chain state is unknown, we'd be lying."""
    trades_repo = _FakeTradesRepo()
    r = _make_redeemer(trades_repo=trades_repo)

    async def fake_to_thread(fn, *args, **kwargs):
        if fn is r._relay_client.execute:
            return SimpleNamespace(transactionId="tx-id-9", transactionHash=None)
        if fn is r._relay_client.poll_until_state:
            return False  # NOT confirmed
        return None

    monkeypatch.setattr("execution.redeemer.asyncio.to_thread", fake_to_thread)

    ok = await r.redeem_position("0x" + "cd" * 32)

    assert ok is False
    assert trades_repo.calls == []


@pytest.mark.asyncio
async def test_db_failure_inside_mark_redeemed_does_not_crash_sweep(monkeypatch):
    """The on-chain redemption already happened — a DB hiccup while
    writing the accounting row must not flip the return value to False."""
    trades_repo = _FakeTradesRepo(raise_on_call=True)
    r = _make_redeemer(trades_repo=trades_repo)

    async def fake_to_thread(fn, *args, **kwargs):
        if fn is r._relay_client.execute:
            return SimpleNamespace(transactionId="tx-id-7", transactionHash="0xabc")
        if fn is r._relay_client.poll_until_state:
            return True
        return None

    monkeypatch.setattr("execution.redeemer.asyncio.to_thread", fake_to_thread)

    ok = await r.redeem_position("0x" + "ef" * 32)

    assert ok is True  # sweep still reports success
    assert len(trades_repo.calls) == 1  # the call was attempted


@pytest.mark.asyncio
async def test_no_trades_repo_means_no_writeback_attempted(monkeypatch):
    """Backwards-compat: a redeemer constructed without a trades_repo
    must still work — just no accounting writeback happens."""
    r = _make_redeemer(trades_repo=None)

    async def fake_to_thread(fn, *args, **kwargs):
        if fn is r._relay_client.execute:
            return SimpleNamespace(transactionId="tx-id-3", transactionHash="0x01")
        if fn is r._relay_client.poll_until_state:
            return True
        return None

    monkeypatch.setattr("execution.redeemer.asyncio.to_thread", fake_to_thread)

    ok = await r.redeem_position("0x" + "12" * 32)

    assert ok is True
    assert r._trades_repo is None
