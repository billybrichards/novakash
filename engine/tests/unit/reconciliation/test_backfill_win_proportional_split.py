"""Unit tests for the WIN-side pnl proportional-split logic.

Covers:
- ``scripts/ops/backfill_pnl_win_inflation.reconcile_group`` —
  proportional split + idempotency floor.
- The mathematical contract that the reconciler / position_monitor
  patches must also satisfy.

The contract: when N>=2 trades share a token_id and resolve as WIN,
the sum of pnl_usd across all rows must equal
``onchain_payout - sum(stake_usd)`` to within rounding (a few cents).
Each row's pnl_usd is its proportional share of the on-chain cashflow,
weighted by stake.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


def _load_backfill_module():
    """Import the backfill script as a module without running its CLI."""
    repo_root = Path(__file__).resolve().parents[4]
    script_path = repo_root / "scripts" / "ops" / "backfill_pnl_win_inflation.py"
    spec = importlib.util.spec_from_file_location(
        "backfill_pnl_win_inflation", str(script_path)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["backfill_pnl_win_inflation"] = module
    spec.loader.exec_module(module)
    return module


bf = _load_backfill_module()


def _make_group(stakes, pnls, slug="btc-updown-5m-1777564200", direction="UP"):
    """Build a synthetic dual-strategy WIN group as fetched from DB."""
    n = len(stakes)
    return {
        "slug": slug,
        "dir": direction,
        "window_ts": 1777564200,
        "n": n,
        "ids": list(range(100, 100 + n)),
        "strategies": [f"v{9+i}_lgb_only" for i in range(n)],
        "stakes": list(stakes),
        "pnls": list(pnls),
        "tx_hashes": [None] * n,
        "token_ids": ["0xtok"] * n,
    }


def test_dual_strategy_proportional_split_simple():
    """Equal stakes + 50/50 split → equal pnls.

    sum_stake = 20, payout = 30, true_cashflow = +10
    Each row gets +5 pnl.
    """
    group = _make_group(stakes=[10.0, 10.0], pnls=[8.0, 8.0])
    redeems = {"0xcond_eq": 30.0}
    slug_to_cond = {"btc-updown-5m-1777564200": "0xcond_eq"}

    r = bf.reconcile_group(group, redeems, slug_to_cond)

    assert r["onchain_payout"] == 30.0
    assert r["sum_stake"] == 20.0
    assert r["true_cashflow"] == 10.0
    # Each row's new pnl = (10 * 10/20) = 5.0
    assert r["new_pnls"] == [5.0, 5.0]
    # Sum of new pnls equals true_cashflow (the contract)
    assert sum(r["new_pnls"]) == r["true_cashflow"]


def test_dual_strategy_proportional_split_unequal_stakes():
    """Unequal stakes → proportional share by stake.

    stakes=[6.89, 6.55], payout=19.61 (real-life trade #6511/6513).
    sum_stake = 13.44, true_cashflow = 19.61 - 13.44 = 6.17.
    row0 gets 6.17 * 6.89/13.44 = 3.163
    row1 gets 6.17 * 6.55/13.44 = 3.007
    """
    group = _make_group(stakes=[6.89, 6.55], pnls=[12.7222, 3.6375])
    redeems = {"0xcond_x": 19.61}
    slug_to_cond = {"btc-updown-5m-1777564200": "0xcond_x"}

    r = bf.reconcile_group(group, redeems, slug_to_cond)

    assert r["sum_stake"] == 13.44
    assert r["onchain_payout"] == 19.61
    assert r["true_cashflow"] == 6.17
    # Sum of new pnls equals true_cashflow within rounding
    assert abs(sum(r["new_pnls"]) - r["true_cashflow"]) < 0.01
    # Larger stake → larger share
    assert r["new_pnls"][0] > r["new_pnls"][1]


def test_inflation_detected_when_db_overstates():
    """sum_pnl_db > true_cashflow → inflation > 0, not already_clean."""
    group = _make_group(stakes=[6.89, 6.55], pnls=[12.7222, 3.6375])
    # sum_pnl_db = 16.36, true_cashflow = 6.17 → inflation +10.19
    redeems = {"0xcond_x": 19.61}
    slug_to_cond = {"btc-updown-5m-1777564200": "0xcond_x"}

    r = bf.reconcile_group(group, redeems, slug_to_cond)

    assert r["inflation"] > 9.0  # ~$10.19 of inflation
    assert r["already_clean"] is False
    assert r["resolvable"] is True


def test_already_clean_skips_correction():
    """If sum_pnl_db ≈ true_cashflow within $0.50 → already_clean=True."""
    # sum_stake = 10, payout = 14, true_cashflow = 4
    # DB pnls already sum to 4 (clean)
    group = _make_group(stakes=[5.0, 5.0], pnls=[2.0, 2.0])
    redeems = {"0xcond_clean": 14.0}
    slug_to_cond = {"btc-updown-5m-1777564200": "0xcond_clean"}

    r = bf.reconcile_group(group, redeems, slug_to_cond)

    assert r["already_clean"] is True
    assert abs(r["inflation"]) < 0.50


def test_unresolvable_when_no_redemption_found():
    """If condition_id maps to no REDEEM → onchain_payout=0, resolvable=False.

    new_pnls falls back to original (no correction).
    """
    group = _make_group(stakes=[5.0, 5.0], pnls=[2.0, 2.0])
    # No mapping for our slug → cid lookup returns None
    redeems = {}
    slug_to_cond = {}

    r = bf.reconcile_group(group, redeems, slug_to_cond)

    assert r["resolvable"] is False
    assert r["onchain_payout"] == 0.0
    # When unresolvable, new_pnls equals original (no change)
    assert r["new_pnls"] == [2.0, 2.0]


def test_conservative_floor_never_inflates_further():
    """new_pnl must never exceed original pnl_usd.

    Construct an edge case where proportional math would inflate one row:
    if true_cashflow is high and stake distribution would put more weight
    on a row that originally had low pnl_db, we MUST cap it at original.
    """
    # Synthetic: row 0 has tiny original pnl 0.5, row 1 has large 10.0
    # Payout=20, stake=[5,5] → proportional new pnl = (10*5/10)=5 each
    # Row 0: 5 > 0.5 (would inflate) → must be capped to 0.5
    # Row 1: 5 < 10.0 (would reduce) → keep 5
    group = _make_group(stakes=[5.0, 5.0], pnls=[0.5, 10.0])
    redeems = {"0xc": 20.0}
    slug_to_cond = {"btc-updown-5m-1777564200": "0xc"}

    r = bf.reconcile_group(group, redeems, slug_to_cond)

    # Row 0 must NOT exceed original 0.5
    assert r["new_pnls"][0] <= 0.5
    # Row 1 reduced from 10.0 to 5.0
    assert r["new_pnls"][1] == 5.0


def test_three_strategy_split_distributes_proportionally():
    """N=3 case: 3 strategies on the same token_id."""
    group = _make_group(
        stakes=[10.0, 5.0, 15.0],
        pnls=[20.0, 20.0, 20.0],
    )
    redeems = {"0xc": 60.0}
    slug_to_cond = {"btc-updown-5m-1777564200": "0xc"}

    r = bf.reconcile_group(group, redeems, slug_to_cond)

    # sum_stake = 30, payout = 60, true_cashflow = +30
    assert r["sum_stake"] == 30.0
    assert r["true_cashflow"] == 30.0
    # Proportional split:
    #   row0: 30 * 10/30 = 10  (capped < original 20)
    #   row1: 30 * 5/30  = 5   (capped < original 20)
    #   row2: 30 * 15/30 = 15  (capped < original 20)
    assert r["new_pnls"][0] == 10.0
    assert r["new_pnls"][1] == 5.0
    assert r["new_pnls"][2] == 15.0
    assert sum(r["new_pnls"]) == r["true_cashflow"]


def test_zero_stake_does_not_crash():
    """Defensive: a row with stake=0 must not divide-by-zero."""
    group = _make_group(stakes=[0.0, 0.0], pnls=[0.0, 0.0])
    redeems = {"0xc": 0.0}
    slug_to_cond = {"btc-updown-5m-1777564200": "0xc"}

    r = bf.reconcile_group(group, redeems, slug_to_cond)

    # No payout → resolvable=False, new_pnls = original
    assert r["resolvable"] is False
    assert r["new_pnls"] == [0.0, 0.0]


def test_build_redemption_map_dedupes_and_aggregates():
    """build_redemption_map should sum REDEEMs and map slugs."""
    activity = [
        {
            "type": "REDEEM",
            "conditionId": "0xc1",
            "slug": "slug-a",
            "usdcSize": 10.0,
        },
        {
            "type": "REDEEM",
            "conditionId": "0xc1",
            "slug": "slug-a",
            "usdcSize": 5.0,
        },
        {
            "type": "TRADE",
            "conditionId": "0xc2",
            "slug": "slug-b",
            "usdcSize": 100.0,
            "side": "BUY",
        },
    ]
    redeems, slug_to_cond = bf.build_redemption_map(activity)
    assert redeems == {"0xc1": 15.0}
    assert slug_to_cond == {"slug-a": "0xc1", "slug-b": "0xc2"}
