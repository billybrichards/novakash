"""Regression tests for fok_ladder._build_result — audit #260.

Context
-------
100% of recent trades show `sot_reconciliation_state='diverged'` with a
structural ~+14¢ gap between DB `fill_price` and on-chain
`polymarket_confirmed_fill_price`, and a ~-2.5% gap on size. Forensics
(Hub note #202, 2026-04-20) traced the root cause to:

    engine/execution/fok_ladder.py::_build_result
        fill_price = round(stake_usd / size_matched, 4) if size_matched > 0 else price

The formula uses the strategy's pre-trade STAKE budget divided by the
CLOB-returned `takingAmount` (shares). Two ways this mis-represents the
actual fill economics:

1. Partial fills: if only part of the order matched, `stake_usd` is the
   BUDGET (what we wanted to spend) and NOT what we actually spent.
   `making_amount` (returned by the CLOB alongside `taking_amount`) is
   the USDC that actually matched. Dividing stake by taking ignores the
   partial-fill case entirely.

2. Price improvement / slippage: when the CLOB hits a better ask than
   our limit, the real per-share price is `making_amount / taking_amount`.
   Using `stake_usd / taking_amount` ignores this and anchors to the
   limit-price-implied value.

The fix keeps behaviour identical when making_amount ≈ stake_usd (fully
filled FAK at our limit) but uses the CLOB's making_amount / taking_amount
when they disagree. This aligns engine-side fill_price with the economic
reality that will eventually be confirmed by the poly_fills reconciler.

What this does NOT fix
----------------------
The fundamental on-chain divergence (negrisk-splitting causing a ~14¢
gap between the CLOB match terms and the on-chain settled price) is a
data-integrity issue that can only be resolved by overwriting trades
with `polymarket_confirmed_*` values post-reconciliation — that is
Phase 3 backfill territory and requires Billy's approval.

This test covers ONLY the forward-path correctness of
_build_result's fill_price / shares computation given a CLOB response.
"""
from __future__ import annotations

import pytest

from execution.fok_ladder import FOKLadder


class _StubPoly:
    """Minimal poly client stub for FOKLadder constructor."""


@pytest.fixture
def ladder() -> FOKLadder:
    return FOKLadder(_StubPoly())


class TestBuildResultFillPrice:
    """Covers the _build_result computation of fill_price and shares."""

    def test_full_fill_at_limit_price_uses_clob_making_amount(self, ladder):
        """CLOB response at limit 0.58 with full fill: making=3.37, taking=5.82.

        Both formulas produce the same answer here, so fill_price should
        match either. Establishes the baseline: when the order filled fully
        at our exact limit, engine fill_price agrees with CLOB terms.
        """
        clob_result = {
            "size_matched": 5.821916,
            "taking_amount": 5.821916,
            "making_amount": 3.37,
            "order_id": "0xabc123",
        }
        out = ladder._build_result(
            result=clob_result,
            price=0.58,
            stake_usd=3.37,
            attempt=1,
            attempted_prices=[0.58],
            order_type="FAK",
        )
        assert out.filled is True
        assert out.shares == pytest.approx(5.821916, rel=1e-6)
        assert out.fill_price == pytest.approx(0.5788, abs=0.0005)

    def test_partial_fill_uses_actual_usdc_not_budget(self, ladder):
        """Partial fill — CLOB matched less than we asked for.

        Stake budget was $3.37 for ~5.82 shares @ 0.58. The book only had
        3 shares at that price, so making=1.74, taking=3.0.

        BUG-BEHAVIOUR: stake_usd / size_matched = 3.37 / 3.0 = 1.1233 →
        nonsensical fill_price > 1.0 for a binary-outcome token.

        FIXED-BEHAVIOUR: making_amount / taking_amount = 1.74 / 3.0 = 0.58
        matches the limit price we submitted and actually paid per share.
        """
        clob_result = {
            "size_matched": 3.0,
            "taking_amount": 3.0,
            "making_amount": 1.74,
            "order_id": "0xpartial",
        }
        out = ladder._build_result(
            result=clob_result,
            price=0.58,
            stake_usd=3.37,
            attempt=1,
            attempted_prices=[0.58],
            order_type="FAK",
        )
        assert out.filled is True
        assert out.shares == pytest.approx(3.0, rel=1e-6)
        # Critical: fill_price must NOT exceed 1.0 (binary token ceiling),
        # and must reflect the actual making_amount / taking_amount ratio.
        assert out.fill_price is not None
        assert out.fill_price <= 1.0, (
            f"fill_price {out.fill_price} > 1.0 on a partial fill — "
            f"this is the audit-#260 bug signature (using stake budget "
            f"instead of actual making_amount)"
        )
        assert out.fill_price == pytest.approx(0.58, abs=0.005)
        assert out.partial is True

    def test_price_improvement_uses_actual_clob_terms(self, ladder):
        """CLOB filled at a BETTER price than our limit (rare but possible).

        We asked for 5.82 shares at 0.58, CLOB matched at 0.55 → taking=5.82
        but making=3.201 (5.82 * 0.55). True fill_price = 0.55, not 0.58.

        BUG-BEHAVIOUR: stake_usd / size_matched = 3.37 / 5.82 = 0.5788
        (over-reports the fill price — we paid 0.55 per share, not 0.58).

        FIXED-BEHAVIOUR: making_amount / taking_amount = 3.201 / 5.82 = 0.55
        matches reality.
        """
        clob_result = {
            "size_matched": 5.821916,
            "taking_amount": 5.821916,
            "making_amount": 3.201,  # 5.821916 * 0.55
            "order_id": "0xbetter",
        }
        out = ladder._build_result(
            result=clob_result,
            price=0.58,
            stake_usd=3.37,
            attempt=1,
            attempted_prices=[0.58],
            order_type="FAK",
        )
        assert out.filled is True
        assert out.shares == pytest.approx(5.821916, rel=1e-6)
        assert out.fill_price == pytest.approx(0.55, abs=0.005), (
            "fill_price must reflect actual making_amount / taking_amount, "
            "not stake_usd / taking_amount"
        )

    def test_missing_making_amount_falls_back_gracefully(self, ladder):
        """Legacy CLOB response without making_amount must not crash.

        Some older CLOB SDK responses may not include making_amount (it
        became standard in the Apr-2026 SDK upgrade per the Apr-10 bug
        fix comment in polymarket_client.py). The code path must fall
        back to the old stake/size formula rather than raising.
        """
        clob_result = {
            "size_matched": 5.821916,
            "order_id": "0xlegacy",
            # NOTE: no making_amount / taking_amount keys
        }
        out = ladder._build_result(
            result=clob_result,
            price=0.58,
            stake_usd=3.37,
            attempt=1,
            attempted_prices=[0.58],
            order_type="FAK",
        )
        assert out.filled is True
        # Fallback uses stake_usd / size_matched as before.
        assert out.fill_price == pytest.approx(3.37 / 5.821916, abs=0.0005)
        assert out.shares == pytest.approx(5.821916, rel=1e-6)

    def test_zero_taking_amount_falls_back_to_price(self, ladder):
        """Defensive: taking_amount=0 should not divide-by-zero."""
        clob_result = {
            "size_matched": 0.0,
            "taking_amount": 0.0,
            "making_amount": 0.0,
            "order_id": "0xzero",
        }
        out = ladder._build_result(
            result=clob_result,
            price=0.58,
            stake_usd=3.37,
            attempt=1,
            attempted_prices=[0.58],
            order_type="FAK",
        )
        # Legacy behaviour: fallback fill_price to the limit price.
        assert out.fill_price == pytest.approx(0.58, abs=1e-6)

    def test_empirical_trade_5118_partial_fill_reproduction(self, ladder):
        """Reproduces trade #5118 (v6_sniper, 2026-04-20) signature.

        From Hub note #202 forensics:
          DB: fill_price=0.5788, fill_size=5.821916, stake=3.37
          On-chain: price=0.73, size=5.7087

        The on-chain divergence (price 0.58 → 0.73) is NOT something the
        _build_result formula can fix — that's negrisk-splitting seen only
        at settlement. But the engine's own fill_price should at least
        reflect the CLOB's making_amount/taking_amount rather than the
        strategy's pre-trade stake budget.

        For this test we simulate the CLOB returning what it actually
        returned: making=3.37 (limit-price-implied USDC), taking=5.821916.
        Both formulas produce 0.5788 in this scenario — the test's value
        is documentary: it pins the behaviour so if someone changes
        _build_result incorrectly in the future, this test flags it.
        """
        clob_result = {
            "size_matched": 5.821916,
            "taking_amount": 5.821916,
            "making_amount": 3.37,
            "order_id": "0xfe5fc8b90cd81195625850db965bf2e65c1fcf180ee7885e8e8b309ede71e376",
        }
        out = ladder._build_result(
            result=clob_result,
            price=0.58,
            stake_usd=3.37,
            attempt=1,
            attempted_prices=[0.58],
            order_type="FAK",
        )
        assert out.fill_price == pytest.approx(0.5788, abs=0.0005)
        assert out.shares == pytest.approx(5.821916, rel=1e-6)
