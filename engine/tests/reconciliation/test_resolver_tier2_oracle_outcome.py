"""Tier 2 sub-priority tests: ``oracle_outcome`` > ``actual_direction``.

Follow-up to PR #483. Verifies the canonical resolver:

  * Prefers ``window_snapshots.oracle_outcome`` (Polymarket on-chain truth)
    over ``window_snapshots.actual_direction`` (engine-internal Chainlink
    sample) when BOTH are populated within Tier 2.
  * Falls back to ``actual_direction`` when ``oracle_outcome`` is NULL.
  * Logs ``canonical_resolver.snapshot_field_disagree`` at WARN when the
    two columns disagree (operator-visible bug-pattern monitor).
  * Stays silent (no WARN) when the two columns agree.
  * Returns ``None`` when both columns are NULL and Tier 3+4 also miss
    (Tier 2 should NOT spuriously hit).

Bug context — BTC 5m window 1777938300 (23:45-23:50 UTC):

    window_snapshots.actual_direction:  DOWN   (engine sample, wrong)
    window_snapshots.oracle_outcome:    UP     (Polymarket on-chain truth)
    market_data.outcome:                UP

Trade 7386 (YES @ 0.66, betting UP) was marked LOSS in DB even though
Polymarket resolved UP (the trade actually WON on-chain). Root cause:
canonical_resolver Tier 2 was reading ``actual_direction`` only.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from reconciliation.canonical_resolver import (
    SOURCE_DATA_API_CURPRICE,
    SOURCE_WINDOW_SNAPSHOTS,
    SUBSOURCE_ACTUAL_DIRECTION,
    SUBSOURCE_ORACLE_OUTCOME,
    CanonicalResolver,
    WindowSnapshotResolution,
)


# ─── Helpers ────────────────────────────────────────────────────────────────


def _build_resolver(*, oracle_outcome=None, actual_direction=None):
    """Build a CanonicalResolver where HTML and on-chain tiers always miss
    so Tier 2 is exercised in isolation.

    The window-snapshots lookup returns a ``WindowSnapshotResolution`` with
    the supplied ``oracle_outcome`` / ``actual_direction``.
    """
    html_fetcher = MagicMock()
    html_fetcher.fetch_resolution = AsyncMock(return_value=None)

    if oracle_outcome is None and actual_direction is None:
        ws_lookup = AsyncMock(return_value=None)
    else:
        ws_lookup = AsyncMock(
            return_value=WindowSnapshotResolution(
                oracle_outcome=oracle_outcome,
                actual_direction=actual_direction,
            )
        )

    onchain_lookup = AsyncMock(return_value=None)

    return CanonicalResolver(
        html_fetcher=html_fetcher,
        window_snapshots_lookup=ws_lookup,
        onchain_ctf_lookup=onchain_lookup,
    )


# ─── Test 1: oracle_outcome=UP, actual_direction=DOWN ───────────────────────
#
# This is the trade-7386 scenario — Polymarket says UP, engine sample says
# DOWN. The resolver MUST return UP and emit the disagreement WARN.


@pytest.mark.asyncio
async def test_oracle_up_actual_down_returns_up_and_warns(capsys):
    r = _build_resolver(oracle_outcome="UP", actual_direction="DOWN")

    out = await r.resolve_window_outcome_canonical(
        asset="BTC", timeframe="5m", window_ts=1_777_938_300,
    )

    assert out is not None
    assert out.direction == "UP", "oracle_outcome must win over actual_direction"
    assert out.source == SOURCE_WINDOW_SNAPSHOTS
    assert out.sub_source == SUBSOURCE_ORACLE_OUTCOME

    # Disagreement WARN must fire (structlog renders to stdout).
    captured = capsys.readouterr()
    blob = captured.out + captured.err
    assert "snapshot_field_disagree" in blob, (
        "expected WARN log when oracle_outcome and actual_direction disagree, "
        f"got: {blob}"
    )
    assert "actual_direction=DOWN" in blob
    assert "oracle_outcome=UP" in blob
    assert "using=oracle_outcome" in blob
    assert "window_ts=1777938300" in blob


# ─── Test 2: oracle_outcome=DOWN, actual_direction=UP ───────────────────────


@pytest.mark.asyncio
async def test_oracle_down_actual_up_returns_down_and_warns(capsys):
    r = _build_resolver(oracle_outcome="DOWN", actual_direction="UP")

    out = await r.resolve_window_outcome_canonical(
        asset="BTC", timeframe="5m", window_ts=1_777_938_300,
    )

    assert out is not None
    assert out.direction == "DOWN", "oracle_outcome wins regardless of direction"
    assert out.source == SOURCE_WINDOW_SNAPSHOTS
    assert out.sub_source == SUBSOURCE_ORACLE_OUTCOME

    captured = capsys.readouterr()
    blob = captured.out + captured.err
    assert "snapshot_field_disagree" in blob


# ─── Test 3: oracle_outcome=NULL, actual_direction=UP ───────────────────────
#
# When oracle hasn't been polled yet (engine just started, or Polymarket
# Gamma is slow), fall back to actual_direction. No WARN.


@pytest.mark.asyncio
async def test_oracle_null_actual_up_falls_back_no_warn(capsys):
    r = _build_resolver(oracle_outcome=None, actual_direction="UP")

    out = await r.resolve_window_outcome_canonical(
        asset="BTC", timeframe="5m", window_ts=1_777_938_300,
    )

    assert out is not None
    assert out.direction == "UP"
    assert out.source == SOURCE_WINDOW_SNAPSHOTS
    assert out.sub_source == SUBSOURCE_ACTUAL_DIRECTION

    captured = capsys.readouterr()
    blob = captured.out + captured.err
    assert "snapshot_field_disagree" not in blob, (
        "no disagreement log expected when oracle_outcome is NULL"
    )


# ─── Test 4: oracle_outcome=NULL, actual_direction=NULL ─────────────────────
#
# Tier 2 misses entirely. With no Tier 3 (no condition_id) and no Tier 4
# fallback the resolver returns None.


@pytest.mark.asyncio
async def test_both_null_tier2_misses_returns_none():
    r = _build_resolver(oracle_outcome=None, actual_direction=None)

    out = await r.resolve_window_outcome_canonical(
        asset="BTC", timeframe="5m", window_ts=1_777_938_300,
        condition_id=None,
        fallback_curprice_outcome=None,
    )

    assert out is None, (
        "Tier 2 must NOT hit when both columns are NULL — caller must not "
        "write outcome"
    )


# ─── Test 5: oracle_outcome=UP, actual_direction=UP ─────────────────────────
#
# Both columns agree — return UP, NO WARN log.


@pytest.mark.asyncio
async def test_oracle_up_actual_up_no_warn(capsys):
    r = _build_resolver(oracle_outcome="UP", actual_direction="UP")

    out = await r.resolve_window_outcome_canonical(
        asset="BTC", timeframe="5m", window_ts=1_777_938_300,
    )

    assert out is not None
    assert out.direction == "UP"
    assert out.source == SOURCE_WINDOW_SNAPSHOTS
    assert out.sub_source == SUBSOURCE_ORACLE_OUTCOME

    captured = capsys.readouterr()
    blob = captured.out + captured.err
    assert "snapshot_field_disagree" not in blob, (
        "agreement case must NOT emit the disagreement WARN"
    )


# ─── Test 6: legacy str return shape still supported ────────────────────────
#
# Existing call sites that pass a callable returning a bare string should
# still work — treat the string as actual_direction (sub_source = actual).


@pytest.mark.asyncio
async def test_legacy_str_lookup_treated_as_actual_direction():
    html_fetcher = MagicMock()
    html_fetcher.fetch_resolution = AsyncMock(return_value=None)
    ws_lookup = AsyncMock(return_value="DOWN")  # legacy str shape
    r = CanonicalResolver(
        html_fetcher=html_fetcher,
        window_snapshots_lookup=ws_lookup,
        onchain_ctf_lookup=AsyncMock(return_value=None),
    )

    out = await r.resolve_window_outcome_canonical(
        asset="BTC", timeframe="5m", window_ts=1_777_938_300,
    )

    assert out is not None
    assert out.direction == "DOWN"
    assert out.source == SOURCE_WINDOW_SNAPSHOTS
    assert out.sub_source == SUBSOURCE_ACTUAL_DIRECTION


# ─── Integration test: Trade 7386 reproduction ──────────────────────────────
#
# Simulates BTC 5m window 1777938300 with the actual production state:
#   oracle_outcome     = UP     (Polymarket on-chain truth)
#   actual_direction   = DOWN   (engine sample at close)
#
# A YES bet @ 0.66 (betting UP) MUST resolve as a WIN.  Before this fix
# the resolver returned DOWN and the trade was mis-labeled LOSS.


@pytest.mark.asyncio
async def test_trade_7386_yes_bet_resolves_win():
    """Trade 7386: YES @ 0.66 on window 1777938300 → must resolve WIN."""
    r = _build_resolver(oracle_outcome="UP", actual_direction="DOWN")

    out = await r.resolve_window_outcome_canonical(
        asset="BTC", timeframe="5m", window_ts=1_777_938_300,
    )

    assert out is not None
    canonical_direction = out.direction
    assert canonical_direction == "UP"
    assert out.source == SOURCE_WINDOW_SNAPSHOTS
    assert out.sub_source == SUBSOURCE_ORACLE_OUTCOME

    # The trade was YES (= betting UP). With canonical direction = UP:
    bet_direction = "UP"
    won = bet_direction == canonical_direction
    assert won is True, (
        "Trade 7386 (YES @ 0.66 on window 1777938300) must resolve WIN — "
        "before this fix it was incorrectly marked LOSS because the "
        "resolver read actual_direction=DOWN instead of oracle_outcome=UP"
    )


# ─── Anti-regression: HTML still wins over Tier 2 oracle_outcome ────────────


@pytest.mark.asyncio
async def test_html_tier1_still_wins_over_oracle_outcome():
    """Tier 2a (oracle_outcome) must never short-circuit Tier 1 (HTML)."""
    from data.feeds.polymarket_html_resolution import ResolvedWindow

    html_fetcher = MagicMock()
    html_fetcher.fetch_resolution = AsyncMock(
        return_value=ResolvedWindow(
            outcome="DOWN",
            price_to_beat=78_675.76,
            close_price=78_660.20,
        )
    )
    ws_lookup = AsyncMock(
        return_value=WindowSnapshotResolution(
            oracle_outcome="UP",  # would mislead if consulted
            actual_direction="UP",
        )
    )
    r = CanonicalResolver(
        html_fetcher=html_fetcher,
        window_snapshots_lookup=ws_lookup,
        onchain_ctf_lookup=AsyncMock(return_value=None),
    )

    out = await r.resolve_window_outcome_canonical(
        asset="BTC", timeframe="5m", window_ts=1_777_938_300,
    )

    assert out is not None
    assert out.direction == "DOWN", "HTML Tier 1 must override Tier 2 oracle_outcome"
    assert out.source != SOURCE_WINDOW_SNAPSHOTS
    ws_lookup.assert_not_called()
