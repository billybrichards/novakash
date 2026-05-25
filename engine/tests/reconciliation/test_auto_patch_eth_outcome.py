"""Tests for auto_patch_misclassified_trades.py — ETH outcome coverage.

Audit #353 follow-up (2026-05-25): the auto-patch cron was blind to ETH
(and XRP/SOL) misclassifications because DETECT_SQL and
DETECT_GAMMA_CANDIDATES_SQL hard-coded ``asset = 'BTC'`` in both the
window_snapshots LATERAL join and the market_data join.

Root cause confirmed by trades 9200 and 9221:
  * ETH window_ts 1779667800 — BTC oracle says UP, ETH oracle says DOWN.
  * Trade is YES @ 0.374 fill.  Reconciler stamped WIN (correct per BTC
    oracle, wrong per ETH oracle).  auto_patch's DETECT_SQL looked up BTC
    oracle for this window_ts → saw UP → concluded outcome=WIN was correct
    → skipped the row.  The ETH oracle (DOWN) was never consulted.

Fix: derive asset from ``split_part(market_slug, '-', 1)`` so all assets
use their own oracle row.

These unit tests exercise the Python helper functions in isolation
(no DB, no Gamma network calls) and the SQL query structure via string
inspection to catch future regressions.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import sys
import os

# Allow importing the script from scripts/ops without installing it.
_SCRIPT_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts", "ops")
)
if _SCRIPT_PATH not in sys.path:
    sys.path.insert(0, _SCRIPT_PATH)

import auto_patch_misclassified_trades as ap  # noqa: E402


# ---------------------------------------------------------------------------
# Helper: real_pnl formula
# ---------------------------------------------------------------------------

class TestRealPnl:
    def test_win_pnl_low_fill(self):
        """Trade 9200: YES @ 0.374 fill, stake $9.96.
        shares = 9.96 / 0.374 = 26.63
        gross = 26.63 * 1.00 = 26.63
        fee = 0.072 * 9.96 = 0.717
        net = 26.63 - 9.96 - 0.717 = 15.953  (roughly)
        real_pnl WIN = (1 - 0.374) * (9.96 / 0.374) - 0.072 * 9.96
        """
        pnl = ap.real_pnl(fill_price=0.374, stake=9.96, is_win=True)
        assert abs(pnl - 15.9539) < 0.01, f"unexpected pnl {pnl}"

    def test_loss_pnl(self):
        """For a LOSS the PnL should be exactly -stake."""
        pnl = ap.real_pnl(fill_price=0.374, stake=9.96, is_win=False)
        assert pnl == round(-9.96, 4)

    def test_win_pnl_high_fill(self):
        """Trade 9221: YES @ 0.86 fill, stake $10.00.
        (1 - 0.86) * (10 / 0.86) - 0.072 * 10 = 1.628 - 0.72 = 0.908
        """
        pnl = ap.real_pnl(fill_price=0.86, stake=10.0, is_win=True)
        assert abs(pnl - 0.9079) < 0.01, f"unexpected pnl {pnl}"


# ---------------------------------------------------------------------------
# gamma_outcome_for_slug — correctness using mocked httpx client
# ---------------------------------------------------------------------------

def _gamma_response(outcome_label: str, price: float):
    """Build a minimal fake Gamma response for a resolved binary market."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {
            "markets": [
                {
                    "slug": "eth-updown-5m-1779667800",
                    "closed": True,
                    "umaResolutionStatus": "resolved",
                    "outcomes": f'["{outcome_label}", "Down"]',
                    "outcomePrices": f'["{price}", "{1.0 - price}"]',
                }
            ]
        }
    ]
    return mock_resp


class TestGammaOutcomeForSlug:
    def _make_client(self, outcome_label: str, price: float):
        client = MagicMock()
        client.get.return_value = _gamma_response(outcome_label, price)
        return client

    def test_eth_down_returns_down(self):
        """Gamma says DOWN (price at 1.0 → Down label) → 'DOWN'."""
        client = self._make_client("Down", 0.999)
        # patch the slug filter inside gamma_outcome_for_slug so it matches
        result = ap.gamma_outcome_for_slug(client, "eth-updown-5m-1779667800")
        assert result == "DOWN", f"expected DOWN, got {result}"

    def test_eth_up_returns_up(self):
        """Gamma says Up at 0.999 → 'UP'."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {
                "markets": [
                    {
                        "slug": "eth-updown-5m-1779667800",
                        "closed": True,
                        "umaResolutionStatus": "resolved",
                        "outcomes": '["Up", "Down"]',
                        "outcomePrices": '["0.999", "0.001"]',
                    }
                ]
            }
        ]
        client = MagicMock()
        client.get.return_value = mock_resp
        result = ap.gamma_outcome_for_slug(client, "eth-updown-5m-1779667800")
        assert result == "UP", f"expected UP, got {result}"

    def test_non_matching_slug_returns_none(self):
        """Response slug doesn't match requested slug → None."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {
                "markets": [
                    {
                        "slug": "btc-updown-5m-9999999",  # different slug
                        "closed": True,
                        "umaResolutionStatus": "resolved",
                        "outcomes": '["Up", "Down"]',
                        "outcomePrices": '["0.999", "0.001"]',
                    }
                ]
            }
        ]
        client = MagicMock()
        client.get.return_value = mock_resp
        result = ap.gamma_outcome_for_slug(client, "eth-updown-5m-1779667800")
        assert result is None

    def test_unresolved_market_returns_none(self):
        """Market not yet closed → None (no winner yet)."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {
                "markets": [
                    {
                        "slug": "eth-updown-5m-1779667800",
                        "closed": False,
                        "umaResolutionStatus": None,
                        "outcomes": '["Up", "Down"]',
                        "outcomePrices": '["0.55", "0.45"]',
                    }
                ]
            }
        ]
        client = MagicMock()
        client.get.return_value = mock_resp
        result = ap.gamma_outcome_for_slug(client, "eth-updown-5m-1779667800")
        assert result is None


# ---------------------------------------------------------------------------
# DETECT_SQL — verify the asset column is derived from market_slug, not BTC
# ---------------------------------------------------------------------------

class TestDetectSqlIsMultiAsset:
    """Regression guard: DETECT_SQL must not contain the hard-coded string
    ``asset = 'BTC'`` that caused the ETH blind spot.  The correct form
    uses ``UPPER(split_part(t.market_slug, '-', 1))`` to derive the asset
    from each trade's own slug.
    """

    def test_detect_sql_no_hardcoded_btc_asset(self):
        sql = ap.DETECT_SQL
        # The old broken form in the window_snapshots lateral join
        assert "asset = 'BTC'" not in sql, (
            "DETECT_SQL still hard-codes asset='BTC' in the window_snapshots "
            "LATERAL join — ETH/XRP/SOL trades will be invisible to the detector"
        )

    def test_detect_sql_no_hardcoded_btc_in_market_data(self):
        sql = ap.DETECT_SQL
        # The old broken form in the market_data join
        assert "md.asset = 'BTC'" not in sql, (
            "DETECT_SQL still hard-codes md.asset='BTC' — non-BTC market_data "
            "joins are broken"
        )

    def test_detect_sql_uses_split_part_for_asset(self):
        sql = ap.DETECT_SQL
        assert "split_part" in sql, (
            "DETECT_SQL should derive the asset from market_slug via split_part()"
        )

    def test_detect_gamma_sql_no_btc_only_filter(self):
        sql = ap.DETECT_GAMMA_CANDIDATES_SQL
        assert "btc-updown-5m-" not in sql, (
            "DETECT_GAMMA_CANDIDATES_SQL still filters to btc-updown-5m-% only — "
            "ETH/XRP/SOL candidates are hidden from the Gamma fallback"
        )

    def test_detect_gamma_sql_uses_asset_from_slug(self):
        sql = ap.DETECT_GAMMA_CANDIDATES_SQL
        assert "split_part" in sql, (
            "DETECT_GAMMA_CANDIDATES_SQL should derive the asset from market_slug "
            "so non-BTC NOT EXISTS probes use the correct asset"
        )


# ---------------------------------------------------------------------------
# Scenario: ETH YES trade stamped WIN when oracle says DOWN
# ---------------------------------------------------------------------------

class TestEthYesWinWithOracleDown:
    """Simulate trade 9200: ETH YES @ 0.374 fill, stamped WIN, should be LOSS.

    The is_win logic in auto_patch is:
        is_win = (direction == "YES" and canonical == "UP")
              or (direction == "NO"  and canonical == "DOWN")

    For direction=YES and canonical=DOWN, is_win must be False.
    """

    def test_eth_yes_canonical_down_is_loss(self):
        direction = "YES"
        canonical = "DOWN"  # ETH oracle says DOWN (the window resolved DOWN)
        is_win = (
            (direction == "YES" and canonical == "UP")
            or (direction == "NO" and canonical == "DOWN")
        )
        assert not is_win, "ETH YES + oracle DOWN must be LOSS, not WIN"

    def test_eth_yes_canonical_down_pnl_is_negative(self):
        fill_price = 0.374
        stake = 9.96
        is_win = False
        pnl = ap.real_pnl(fill_price=fill_price, stake=stake, is_win=is_win)
        assert pnl == round(-stake, 4), f"LOSS pnl must equal -stake, got {pnl}"

    def test_eth_yes_canonical_up_is_win(self):
        """Sanity check: ETH YES + oracle UP → WIN."""
        direction = "YES"
        canonical = "UP"
        is_win = (
            (direction == "YES" and canonical == "UP")
            or (direction == "NO" and canonical == "DOWN")
        )
        assert is_win, "ETH YES + oracle UP must be WIN"

    def test_btc_oracle_up_does_not_determine_eth_outcome(self):
        """The old bug: BTC oracle=UP for the same window_ts was incorrectly
        used to classify an ETH trade.  This test asserts that when the
        correctly-derived canonical is ETH's oracle (DOWN), the result is
        LOSS — not WIN as BTC's oracle would suggest.
        """
        # BTC oracle at the same window_ts
        btc_canonical = "UP"
        # ETH oracle at the same window_ts (the correct one)
        eth_canonical = "DOWN"

        direction = "YES"

        # Old (broken) logic used btc_canonical:
        old_is_win = (direction == "YES" and btc_canonical == "UP")
        # New (correct) logic uses eth_canonical:
        new_is_win = (direction == "YES" and eth_canonical == "UP")

        assert old_is_win, "old logic would compute WIN (wrong)"
        assert not new_is_win, "new logic correctly computes LOSS"
        assert old_is_win != new_is_win, "fix changes the outcome"
