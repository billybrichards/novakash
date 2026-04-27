"""Unit tests for ``reconcile_orphan_trades`` — pure-logic, no network/DB.

Covers the two confirmed orphan windows from 2026-04-27 night:

  - 1777263600 (04:23 UTC)  v10 fill 0x79ec...d556df, $7.47, won @ $0.75
  - 1777257600 (02:40 UTC)  DOWN, totalBought=$11.86, realizedPnl=-$3.74,
    residual won @ $1.00 with cv=$0.54

Run with:
    pytest scripts/ops/test_reconcile_orphan_trades.py -v
"""
from __future__ import annotations

import os
import sys

import pytest

# Make the script importable as a module.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import reconcile_orphan_trades as r  # noqa: E402


def _make_position(**overrides):
    """Build a Position from data-api-shaped raw dict."""
    raw = {
        "conditionId": "0xdeadbeef" + "0" * 56,
        "slug": "btc-updown-5m-1777263600",
        "outcome": "Up",
        "totalBought": 7.47,
        "realizedPnl": 0.0,
        "currentValue": 0.0,
        "size": 10.0,
        "redeemable": False,
        "title": "Bitcoin Up or Down 04:25 UTC",
    }
    raw.update(overrides)
    return r.Position.from_api(raw)


def test_position_decodes_window_ts_from_slug():
    p = _make_position(slug="btc-updown-5m-1777263600")
    assert p.window_ts == 1777263600


def test_position_window_ts_none_for_non_5min_slug():
    p = _make_position(slug="some-other-market-slug")
    assert p.window_ts is None


def test_orphan_window_1777263600_v10_up_no_db_row():
    """Confirmed orphan: v10 UP fill at 04:23 UTC, $7.47, won @ $0.75 — no trades row."""
    pos = _make_position(
        slug="btc-updown-5m-1777263600",
        outcome="Up",
        totalBought=7.47,
        realizedPnl=0.0,
        currentValue=7.50,  # won residual still held
        size=10.0,
        redeemable=True,
    )
    findings = r.reconcile([pos], trades_by_window={1777263600: []})
    assert len(findings.orphans) == 1
    assert findings.orphans[0].window_ts == 1777263600
    assert findings.orphans[0].outcome == "Up"
    assert findings.matched == 0
    assert findings.suspicious == []


def test_orphan_window_1777257600_down_partial_residual():
    """Confirmed orphan: DOWN @ 02:40 UTC, totalBought=$11.86, residual cv=$0.54.

    Should appear in BOTH orphans (no DB row) and residuals (partial-sell tail).
    """
    pos = _make_position(
        slug="btc-updown-5m-1777257600",
        outcome="Down",
        totalBought=11.86,
        realizedPnl=-3.74,
        currentValue=0.54,
        size=0.54,
    )
    findings = r.reconcile([pos], trades_by_window={1777257600: []})
    assert len(findings.orphans) == 1
    assert len(findings.residuals) == 1
    assert findings.residuals[0].window_ts == 1777257600


def test_matched_position_with_correct_direction_is_not_orphan():
    pos = _make_position(
        outcome="Up", totalBought=10.0, slug="btc-updown-5m-1777200000"
    )
    trade = r.TradeRow(
        id=1,
        strategy_id="v10_lgb_only",
        direction="UP",
        fill_price=0.50,
        fill_size=20.0,  # 20 * 0.50 = $10.00 notional, perfect match
        status="FILLED",
        outcome="WIN",
    )
    findings = r.reconcile(
        [pos], trades_by_window={1777200000: [trade]}
    )
    assert findings.matched == 1
    assert findings.orphans == []
    assert findings.suspicious == []


def test_direction_mismatch_is_flagged_as_orphan():
    """DB row exists for the window but for the OTHER side — still an orphan."""
    pos = _make_position(
        outcome="Down", totalBought=10.0, slug="btc-updown-5m-1777100000"
    )
    trade = r.TradeRow(
        id=2,
        strategy_id="v10",
        direction="UP",  # opposite side
        fill_price=0.5,
        fill_size=20.0,
        status="FILLED",
        outcome=None,
    )
    findings = r.reconcile([pos], trades_by_window={1777100000: [trade]})
    assert len(findings.orphans) == 1
    assert findings.matched == 0


def test_fill_size_drift_over_20pct_flags_suspicious():
    pos = _make_position(
        outcome="Up", totalBought=10.0, slug="btc-updown-5m-1777150000"
    )
    trade = r.TradeRow(
        id=3,
        strategy_id="v10",
        direction="UP",
        fill_price=0.50,
        fill_size=10.0,  # 10 * 0.50 = $5.00 notional vs $10 bought → 50% drift
        status="FILLED",
        outcome=None,
    )
    findings = r.reconcile([pos], trades_by_window={1777150000: [trade]})
    assert len(findings.suspicious) == 1
    assert findings.matched == 0


def test_small_total_bought_below_threshold_skipped():
    """Positions below min_total_bought should not flag as orphan."""
    pos = _make_position(
        outcome="Up", totalBought=0.10, slug="btc-updown-5m-1777160000"
    )
    findings = r.reconcile(
        [pos],
        trades_by_window={1777160000: []},
        min_total_bought=0.5,
    )
    assert findings.orphans == []
    assert findings.matched == 0


def test_residual_detector_independent_of_orphan_check():
    """A small residual that's also matched in the DB still gets logged as a residual."""
    pos = _make_position(
        outcome="Down",
        totalBought=10.0,
        realizedPnl=-2.5,
        currentValue=0.30,
        size=0.30,
        slug="btc-updown-5m-1777170000",
    )
    trade = r.TradeRow(
        id=4,
        strategy_id="v10",
        direction="DOWN",
        fill_price=0.5,
        fill_size=20.0,  # $10 notional matches
        status="FILLED",
        outcome="LOSS",
    )
    findings = r.reconcile(
        [pos], trades_by_window={1777170000: [trade]}
    )
    assert findings.matched == 1
    assert len(findings.residuals) == 1


def test_render_report_summary_fields_present(capsys):
    pos = _make_position(
        slug="btc-updown-5m-1777263600",
        outcome="Up",
        totalBought=7.47,
        currentValue=7.50,
    )
    findings = r.reconcile([pos], trades_by_window={1777263600: []})
    summary = r.render_report(findings, total_positions=1)
    assert summary["orphan_count"] == 1
    assert summary["orphan_total_bought_usd"] == 7.47
    assert summary["orphan_unredeemed_value_usd"] == 7.5
    assert summary["orphans"][0]["window_ts"] == 1777263600
    out = capsys.readouterr().out
    assert "ORPHAN-TRADE RECONCILER" in out
    assert "JSON SUMMARY" in out


@pytest.mark.parametrize(
    "data_outcome,db_dir,expected",
    [
        ("Up", "UP", True),
        ("Down", "DOWN", True),
        ("Up", "DOWN", False),
        ("Down", "UP", False),
        ("Up", None, False),
        ("up", "UP", True),
    ],
)
def test_direction_match_capitalisation(data_outcome, db_dir, expected):
    assert r._direction_match(data_outcome, db_dir) is expected
