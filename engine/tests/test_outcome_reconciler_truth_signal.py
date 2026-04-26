"""Regression tests for outcome resolution truth signal (audit #314).

Audit #314: the v8 outcome backfill script and reconciler treated the
``outcome`` field on a Polymarket data-api position record as WIN/LOSS,
but Polymarket actually uses that field to label which SIDE the bet was
on (``"Up"`` / ``"Down"``). 124 trades were silently marked WIN across
7 days because every position with ``outcome="Up"`` was read as a win.

The deterministic truth signal is ``curPrice``:
  * curPrice <= 0.01 → LOSS
  * curPrice >= 0.99 → WIN
  * 0.01 < curPrice < 0.99 → market not resolved on-chain, do NOT classify

This test file pins the resolver in ``scripts/ops/backfill_v8_outcomes.py``
to the curPrice-based contract. If anyone reintroduces the bug by reading
``outcome`` again, these tests fail loudly.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


# Make the scripts/ops module importable as a top-level helper.
_SCRIPTS_OPS = Path(__file__).resolve().parent.parent.parent / "scripts" / "ops"
if str(_SCRIPTS_OPS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_OPS))


def _trade(stake_usd: float = 10.0, token_id: str = "0xabc123def456") -> dict:
    """Minimal trade row matching the backfill query result shape."""
    return {
        "stake_usd": stake_usd,
        "metadata": {"token_id": token_id},
    }


def _position(
    *,
    asset_or_token_id: str,
    cur_price: float,
    outcome_label: str = "Up",
    realized_pnl: float = 0.0,
    pnl: float = 0.0,
) -> dict:
    """Polymarket data-api /positions row shape (relevant fields only)."""
    return {
        "asset": asset_or_token_id,
        "tokenId": asset_or_token_id,
        "outcome": outcome_label,  # NB: SIDE label, NOT win/loss
        "curPrice": cur_price,
        "realizedPnl": realized_pnl,
        "pnl": pnl,
    }


# ── core invariant: outcome label must NEVER drive WIN/LOSS ────────────────


class TestOutcomeLabelIsIgnored:
    """Audit #314: ``outcome`` field is the SIDE, not the result."""

    def test_outcome_up_with_loss_curprice_classifies_loss(self):
        """Position with outcome='Up' but curPrice=0.0 must be LOSS."""
        from backfill_v8_outcomes import resolve_from_tier1

        trade = _trade()
        positions = [
            _position(
                asset_or_token_id="0xabc123def456",
                cur_price=0.0,
                outcome_label="Up",  # the bug-bait — looks like a win
                realized_pnl=0.0,
            )
        ]
        result = resolve_from_tier1(trade, positions)
        assert result is not None, "resolver must classify"
        outcome, pnl, reason = result
        assert outcome == "LOSS"
        assert pnl == -10.0
        assert "curPrice" in reason

    def test_outcome_up_with_win_curprice_classifies_win(self):
        """Position with outcome='Up' AND curPrice=1.0 is genuinely a WIN."""
        from backfill_v8_outcomes import resolve_from_tier1

        trade = _trade(stake_usd=20.0)
        positions = [
            _position(
                asset_or_token_id="0xabc123def456",
                cur_price=1.0,
                outcome_label="Up",
                realized_pnl=15.50,  # truth from data-api
            )
        ]
        result = resolve_from_tier1(trade, positions)
        assert result is not None
        outcome, pnl, reason = result
        assert outcome == "WIN"
        assert pnl == 15.50  # prefers realizedPnl over a stake-derived guess

    def test_outcome_down_with_loss_curprice_classifies_loss(self):
        """Symmetric check: a DOWN-side bet that lost (curPrice=0.0) is LOSS,
        regardless of the outcome label saying 'Down'."""
        from backfill_v8_outcomes import resolve_from_tier1

        trade = _trade()
        positions = [
            _position(
                asset_or_token_id="0xabc123def456",
                cur_price=0.0,
                outcome_label="Down",
                realized_pnl=0.0,
            )
        ]
        result = resolve_from_tier1(trade, positions)
        assert result is not None
        outcome, _pnl, _reason = result
        assert outcome == "LOSS"


# ── unresolved markets must NOT be classified ──────────────────────────────


class TestMidRangeCurPriceSkipped:
    """A still-trading market (0.01 < curPrice < 0.99) cannot be classified."""

    @pytest.mark.parametrize("price", [0.5, 0.49, 0.51, 0.10, 0.90, 0.98])
    def test_midrange_returns_none(self, price: float):
        from backfill_v8_outcomes import resolve_from_tier1

        trade = _trade()
        positions = [
            _position(
                asset_or_token_id="0xabc123def456",
                cur_price=price,
            )
        ]
        result = resolve_from_tier1(trade, positions)
        assert result is None, f"curPrice={price} must not classify"

    def test_boundary_zero_zero_one_is_loss(self):
        """0.01 boundary is treated as LOSS (matches docstring contract)."""
        from backfill_v8_outcomes import resolve_from_tier1

        trade = _trade()
        positions = [
            _position(asset_or_token_id="0xabc123def456", cur_price=0.01)
        ]
        result = resolve_from_tier1(trade, positions)
        assert result is not None
        assert result[0] == "LOSS"

    def test_boundary_zero_nine_nine_is_win(self):
        """0.99 boundary is treated as WIN (matches docstring contract)."""
        from backfill_v8_outcomes import resolve_from_tier1

        trade = _trade()
        positions = [
            _position(
                asset_or_token_id="0xabc123def456",
                cur_price=0.99,
                realized_pnl=8.0,
            )
        ]
        result = resolve_from_tier1(trade, positions)
        assert result is not None
        assert result[0] == "WIN"


# ── tier1 falls through cleanly when token mismatches ───────────────────────


class TestTokenMatchingIsRequired:
    """No token match → fall through to tier2/tier3, do NOT classify."""

    def test_no_token_match_returns_none(self):
        from backfill_v8_outcomes import resolve_from_tier1

        trade = _trade(token_id="0xdeadbeef")
        positions = [
            _position(
                asset_or_token_id="0xabc123def456",
                cur_price=0.0,
                outcome_label="Up",
            )
        ]
        result = resolve_from_tier1(trade, positions)
        assert result is None

    def test_empty_token_returns_none(self):
        from backfill_v8_outcomes import resolve_from_tier1

        trade = {"stake_usd": 10.0, "metadata": {}}
        positions = [
            _position(
                asset_or_token_id="0xabc123def456",
                cur_price=0.0,
            )
        ]
        result = resolve_from_tier1(trade, positions)
        assert result is None


# ── pnl precedence: realizedPnl > pnl > 0 ──────────────────────────────────


class TestWinPnLPrecedence:
    def test_realized_pnl_preferred(self):
        from backfill_v8_outcomes import resolve_from_tier1

        positions = [
            _position(
                asset_or_token_id="0xabc",
                cur_price=1.0,
                realized_pnl=12.34,
                pnl=99.0,  # noisy, must not win
            )
        ]
        result = resolve_from_tier1(_trade(token_id="0xabc"), positions)
        assert result is not None
        assert result[1] == 12.34

    def test_pnl_fallback_when_realized_zero(self):
        from backfill_v8_outcomes import resolve_from_tier1

        positions = [
            _position(
                asset_or_token_id="0xabc",
                cur_price=1.0,
                realized_pnl=0.0,
                pnl=7.5,
            )
        ]
        result = resolve_from_tier1(_trade(token_id="0xabc"), positions)
        assert result is not None
        assert result[1] == 7.5
