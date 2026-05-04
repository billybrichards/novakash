"""Regression tests for audit #350 / hub note #334 — resolver synthetic-price bug.

Background
----------
Between engine restart 2026-05-04 21:47:33 UTC and the manual hot-patches
of trades 7378 / 7379 / 7380, every winning NO bet on a DOWN window was
written to the trades table as ``outcome='LOSS'`` because the resolver
fell through to a synthetic-price fallback (``open=$100,000`` →
``close=$100,001``) whose mathematics guarantee an UP outcome.

These tests pin the canonical fallback chain so the regression cannot
return:

  1. ``window_snapshots.actual_direction``  (labeler-stamped)
  2. ``window_snapshots.oracle_outcome``     (Polymarket Gamma)
  3. ``window_snapshots.outcome``            (legacy live-resolve writer)
  4. derived from ``open_price`` vs ``close_price``
  5. ``None`` — caller MUST skip + retry; never synthesize.

We test ``_coerce_direction`` (pure logic) and exercise the three
manually-patched trades (7378-7380) as known-good fixtures — given the
window_snapshots row each one had at the time of the bug, the resolver
must produce ``outcome='WIN'``.
"""
from __future__ import annotations

import pytest

from adapters.persistence.pg_window_repo import _coerce_direction


# ---------------------------------------------------------------------------
# _coerce_direction — pure-logic fallback chain
# ---------------------------------------------------------------------------

class TestCoerceDirection:
    """Guard the canonical UP/DOWN fallback chain (audit #350)."""

    def test_prefers_actual_direction_when_set(self):
        row = {
            "actual_direction": "DOWN",
            "oracle_outcome": "UP",       # disagree — actual_direction wins
            "outcome": "UP",
            "open_price": 100.0,
            "close_price": 200.0,         # would imply UP
        }
        assert _coerce_direction(row) == "DOWN"

    def test_falls_back_to_oracle_outcome(self):
        row = {
            "actual_direction": None,
            "oracle_outcome": "DOWN",
            "outcome": "UP",
            "open_price": 100.0,
            "close_price": 200.0,
        }
        assert _coerce_direction(row) == "DOWN"

    def test_falls_back_to_outcome_column(self):
        row = {
            "actual_direction": None,
            "oracle_outcome": None,
            "outcome": "DOWN",
            "open_price": None,
            "close_price": None,
        }
        assert _coerce_direction(row) == "DOWN"

    def test_derives_up_from_prices(self):
        row = {
            "actual_direction": None,
            "oracle_outcome": None,
            "outcome": None,
            "open_price": 80267.93,
            "close_price": 80300.50,  # close > open → UP
        }
        assert _coerce_direction(row) == "UP"

    def test_derives_down_from_prices(self):
        # Trade 7378's window — exact prices from audit #350 reproducer
        row = {
            "actual_direction": None,
            "oracle_outcome": None,
            "outcome": None,
            "open_price": 80267.93,
            "close_price": 80250.10,  # close < open → DOWN
        }
        assert _coerce_direction(row) == "DOWN"

    def test_returns_none_when_prices_equal(self):
        # Exactly-equal close == open is undecidable; do NOT guess.
        row = {
            "actual_direction": None,
            "oracle_outcome": None,
            "outcome": None,
            "open_price": 100_000.0,
            "close_price": 100_000.0,
        }
        assert _coerce_direction(row) is None

    def test_returns_none_when_no_oracle_signal(self):
        row = {
            "actual_direction": None,
            "oracle_outcome": None,
            "outcome": None,
            "open_price": None,
            "close_price": None,
        }
        assert _coerce_direction(row) is None

    def test_returns_none_when_row_is_none(self):
        assert _coerce_direction(None) is None

    def test_synthetic_100k_placeholder_does_not_imply_up(self):
        """The pre-fix bug: $100,000 → $100,001 was the synthetic fallback.

        We allow ``_coerce_direction`` to read those values from a real
        snapshot row (it would correctly infer UP), but the structural
        defense lives upstream: the telegram emitter and the resolver
        must NEVER manufacture this pair when prices are missing. This
        test pins that ``_coerce_direction`` itself is honest about
        what the price columns say — the burden of "skip when unknown"
        lives in the caller, not in this helper.
        """
        # If a window genuinely has open=$100,000 close=$100,001, that's UP.
        row = {
            "actual_direction": None,
            "oracle_outcome": None,
            "outcome": None,
            "open_price": 100_000.0,
            "close_price": 100_001.0,
        }
        assert _coerce_direction(row) == "UP"
        # But when prices are NULL, the helper returns None — never UP.
        row_null = {
            "actual_direction": None,
            "oracle_outcome": None,
            "outcome": None,
            "open_price": None,
            "close_price": None,
        }
        assert _coerce_direction(row_null) is None

    def test_handles_lowercase_and_whitespace(self):
        row = {
            "actual_direction": " up ",
            "oracle_outcome": None,
            "outcome": None,
            "open_price": None,
            "close_price": None,
        }
        assert _coerce_direction(row) == "UP"

    def test_ignores_invalid_string_values(self):
        # A garbage value falls through to the next tier rather than
        # short-circuiting.
        row = {
            "actual_direction": "MAYBE",
            "oracle_outcome": "DOWN",
            "outcome": None,
            "open_price": None,
            "close_price": None,
        }
        assert _coerce_direction(row) == "DOWN"

    def test_ignores_non_positive_prices(self):
        row = {
            "actual_direction": None,
            "oracle_outcome": None,
            "outcome": None,
            "open_price": 0.0,
            "close_price": 100.0,
        }
        assert _coerce_direction(row) is None
        row2 = {
            "actual_direction": None,
            "oracle_outcome": None,
            "outcome": None,
            "open_price": 100.0,
            "close_price": -1.0,
        }
        assert _coerce_direction(row2) is None


# ---------------------------------------------------------------------------
# Trade 7378-7380 fixtures — must resolve to WIN given canonical window data
# ---------------------------------------------------------------------------

# Audit #350 / hub note #334: these three trades were misclassified as LOSS
# by the buggy resolver and manually patched to WIN. We pin them as known-good
# fixtures — given the window_snapshots row each one had, the canonical
# direction MUST be DOWN, which makes the NO bet a WIN.

PATCHED_TRADES = [
    # (trade_id, window_ts, direction, snapshot_row, expected_outcome)
    (
        7378, 1777933200, "NO",
        {
            "actual_direction": None,
            "oracle_outcome": "DOWN",
            "outcome": "DOWN",
            "open_price": 80267.93,
            "close_price": 80250.10,
        },
        "WIN",
    ),
    (
        7379, 1777934100, "NO",
        {
            "actual_direction": "DOWN",
            "oracle_outcome": "DOWN",
            "outcome": "DOWN",
            "open_price": None,
            "close_price": None,
        },
        "WIN",
    ),
    (
        7380, 1777934100, "NO",
        {
            # actual_direction NOT yet labeled, but oracle/outcome populated
            "actual_direction": None,
            "oracle_outcome": None,
            "outcome": "DOWN",
            "open_price": None,
            "close_price": None,
        },
        "WIN",
    ),
]


@pytest.mark.parametrize(
    "trade_id,window_ts,direction,snapshot_row,expected_outcome",
    PATCHED_TRADES,
    ids=[f"trade-{t[0]}" for t in PATCHED_TRADES],
)
def test_patched_trades_resolve_correctly_via_canonical_chain(
    trade_id, window_ts, direction, snapshot_row, expected_outcome
):
    """Each manually-patched trade must produce WIN via the canonical chain.

    Re-runs the resolver's WIN/LOSS decision against a window_snapshots
    row reconstructed from the bug-time data. With the fix, all three
    NO-on-DOWN trades resolve to WIN. Pre-fix, they resolved to LOSS
    because the synthetic-price fallback overrode the real direction.
    """
    actual_direction = _coerce_direction(snapshot_row)
    assert actual_direction == "DOWN", (
        f"trade {trade_id}: snapshot row should yield DOWN, got "
        f"{actual_direction!r}"
    )
    # Direction conversion mirrors _resolve_paper_batch / live resolver.
    trade_expects = "DOWN" if direction == "NO" else "UP"
    outcome = "WIN" if trade_expects == actual_direction else "LOSS"
    assert outcome == expected_outcome


def test_no_bet_on_up_window_is_loss():
    """Symmetry check: when the window resolves UP, the NO bet IS a LOSS.

    Without this, a too-aggressive fix could flip the polarity and make
    every NO bet a WIN. This test pins the inverse case.
    """
    snapshot_row = {
        "actual_direction": None,
        "oracle_outcome": "UP",
        "outcome": None,
        "open_price": None,
        "close_price": None,
    }
    actual_direction = _coerce_direction(snapshot_row)
    assert actual_direction == "UP"
    # NO bet on UP window = LOSS
    trade_expects = "DOWN"
    outcome = "WIN" if trade_expects == actual_direction else "LOSS"
    assert outcome == "LOSS"


def test_yes_bet_on_up_window_is_win():
    snapshot_row = {
        "actual_direction": "UP",
        "oracle_outcome": None,
        "outcome": None,
        "open_price": None,
        "close_price": None,
    }
    actual_direction = _coerce_direction(snapshot_row)
    assert actual_direction == "UP"
    trade_expects = "UP"
    outcome = "WIN" if trade_expects == actual_direction else "LOSS"
    assert outcome == "WIN"


def test_yes_bet_on_down_window_is_loss():
    snapshot_row = {
        "actual_direction": "DOWN",
        "oracle_outcome": None,
        "outcome": None,
        "open_price": None,
        "close_price": None,
    }
    actual_direction = _coerce_direction(snapshot_row)
    assert actual_direction == "DOWN"
    trade_expects = "UP"
    outcome = "WIN" if trade_expects == actual_direction else "LOSS"
    assert outcome == "LOSS"


def test_resolver_skips_when_no_oracle_signal():
    """When window_snapshots has no oracle data, the resolver returns None.

    Caller is contractually required to SKIP (retry next pass) rather
    than fall through to a synthetic ``$100,000 → $100,001`` placeholder.
    """
    snapshot_row = {
        "actual_direction": None,
        "oracle_outcome": None,
        "outcome": None,
        "open_price": None,
        "close_price": None,
    }
    actual_direction = _coerce_direction(snapshot_row)
    assert actual_direction is None  # caller skips
