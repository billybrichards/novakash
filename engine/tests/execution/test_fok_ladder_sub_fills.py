"""Regression tests for sub-fill capture in fok_ladder._build_result.

Context
-------
Hub note 2026-05-21 ("Sub-fill writer audit — 2026-05-20 21:00 ETH demote
root cause"). A single FAK order against Polymarket CLOB can sweep N maker
offers, producing N on-chain transactions reported as
``transactionsHashes`` + ``tradeIDs`` arrays in the ``post_order`` response.

Pre-fix, both arrays were dropped on the floor: only the aggregate
``makingAmount`` / ``takingAmount`` survived into the trade row, so when
Billy audited the 2026-05-20 21:00 ETH demote ("3-fill loss on window
1779312000 ... DB only recorded 2 of 3 fills") there was no way to see the
individual on-chain matches. The poly_fills reconciler runs separately
against data-api.polymarket.com and only sees the per-tx flattened view —
so a true 3-maker single-FAK fill could be recorded as 1 or 2 fills
depending on how Polymarket batches the on-chain ``OrdersMatched`` events.

This test pins behaviour so the arrays survive into ``FOKResult`` and
downstream into ``ExecutionResult`` + trade metadata.
"""
from __future__ import annotations

import pytest

from execution.fok_ladder import FOKLadder


class _StubPoly:
    """Minimal poly client stub for FOKLadder constructor."""


@pytest.fixture
def ladder() -> FOKLadder:
    return FOKLadder(_StubPoly())


class TestSubFillCapture:
    """Covers transactionsHashes / tradeIDs propagation through _build_result."""

    def test_single_maker_fill_empty_arrays(self, ladder):
        """One maker matched: arrays are length 1 (or empty if the SDK omits)."""
        clob_result = {
            "size_matched": 5.5,
            "taking_amount": 5.5,
            "making_amount": 4.84,
            "order_id": "0xsingle",
            "transactions_hashes": ["0xabc111"],
            "trade_ids": ["trade-1"],
        }
        out = ladder._build_result(
            result=clob_result,
            price=0.88,
            stake_usd=4.84,
            attempt=1,
            attempted_prices=[0.88],
            order_type="FAK",
        )
        assert out.transactions_hashes == ["0xabc111"]
        assert out.trade_ids == ["trade-1"]

    def test_three_maker_fill_arrays_preserved(self, ladder):
        """3-maker FAK fill: all 3 tx hashes + trade IDs survive in the result.

        Smoking-gun scenario from the 2026-05-20 21:00 ETH demote: a single
        FAK at $0.86 swept three maker offers at $0.77/$0.77/$0.78. Before
        this fix the arrays were dropped, leaving only the aggregate
        making/taking. Test pins that all 3 sub-fill identifiers survive.
        """
        clob_result = {
            "size_matched": 7.818178,
            "taking_amount": 7.818178,
            "making_amount": 6.02,
            "order_id": "0xthree-maker",
            "transactions_hashes": [
                "0x8705c8bb7cd156cfb9a96c70d0d9f21d1b39356d5940c3cc2dff036e03183427",
                "0xfake_second_maker_tx_hash_for_test_purposes_only_000000000000000",
                "0xfake_third_maker_tx_hash_for_test_purposes_only_0000000000000000",
            ],
            "trade_ids": ["clob-trade-A", "clob-trade-B", "clob-trade-C"],
        }
        out = ladder._build_result(
            result=clob_result,
            price=0.86,
            stake_usd=6.11,
            attempt=1,
            attempted_prices=[0.86],
            order_type="FAK",
        )
        assert out.filled is True
        assert len(out.transactions_hashes) == 3
        assert len(out.trade_ids) == 3
        # First tx hash should be the actual on-chain hash from the
        # smoking-gun trade (8743) so audits can grep for it.
        assert out.transactions_hashes[0].startswith("0x8705c8bb")

    def test_missing_sub_fill_arrays_defaults_to_empty_lists(self, ladder):
        """Legacy SDK / paper-mode responses without the arrays must not crash."""
        clob_result = {
            "size_matched": 5.5,
            "taking_amount": 5.5,
            "making_amount": 4.84,
            "order_id": "0xlegacy",
            # NOTE: no transactions_hashes / trade_ids keys.
        }
        out = ladder._build_result(
            result=clob_result,
            price=0.88,
            stake_usd=4.84,
            attempt=1,
            attempted_prices=[0.88],
            order_type="FAK",
        )
        assert out.filled is True
        assert out.transactions_hashes == []
        assert out.trade_ids == []
