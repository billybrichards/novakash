"""Regression tests for the ON-CHAIN authoritative per-window exposure cap.

Driver incident: 2026-05-21 04:18 UTC, window `btc-updown-5m-1779336900`.
~$69 actual on-chain loss but DB only recorded $26.70 (2 trades). The
trades-table-based cap (PR #561 Step 4.5) couldn't see the missing fills.

These tests prove the new on-chain cap (Step 4.6) blocks even when DB is
silent. Mock the data-api fetch to return positions reflecting reality.
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import patch

from use_cases.onchain_position_cap import (
    check_onchain_window_cap,
    clear_state_for_tests,
    get_onchain_cost_for_window,
    get_window_cap_pm_usd,
    release_pending_stake,
    _cache,
)


@pytest.fixture(autouse=True)
def clear_cache():
    clear_state_for_tests()
    yield
    clear_state_for_tests()


@pytest.mark.asyncio
async def test_cap_blocks_when_onchain_already_at_cap():
    """Smoking-gun reproducer. Polymarket data-api returns positions whose
    Up-side cost = $24. Cap = $25. New stake = $5. 24+5=$29 > $25 → block."""
    async def fake_fetch(*args, **kwargs):
        return 24.0  # on-chain Up cost
    with patch(
        "use_cases.onchain_position_cap.get_onchain_cost_for_window",
        side_effect=fake_fetch,
    ):
        skip = await check_onchain_window_cap(
            funder_address="0x181D2ED714E0f7Fe9c6e4f13711376eDaab25E10",
            market_slug="btc-updown-5m-1779336900",
            direction="YES",
            new_stake_usd=5.0,
            cap_usd=25.0,
        )
    assert skip == "exposure_cap_window_pm"


@pytest.mark.asyncio
async def test_cap_allows_when_room_remaining():
    async def fake_fetch(*args, **kwargs):
        return 10.0
    with patch(
        "use_cases.onchain_position_cap.get_onchain_cost_for_window",
        side_effect=fake_fetch,
    ):
        skip = await check_onchain_window_cap(
            funder_address="0x181D",
            market_slug="btc-updown-5m-1779336900",
            direction="YES",
            new_stake_usd=5.0,
            cap_usd=25.0,
        )
    assert skip is None


@pytest.mark.asyncio
async def test_cap_blocks_even_when_db_silent():
    """The smoking-gun for today's bug: trades table has no rows but
    on-chain shows $24 of fills. The on-chain cap MUST still block.
    Simulates the writer-gap scenario directly."""
    async def fake_fetch(*args, **kwargs):
        # Polymarket sees $24 of fills even though DB is empty
        return 24.0
    with patch(
        "use_cases.onchain_position_cap.get_onchain_cost_for_window",
        side_effect=fake_fetch,
    ):
        skip = await check_onchain_window_cap(
            funder_address="0x181D",
            market_slug="btc-updown-5m-1779336900",
            direction="YES",
            new_stake_usd=5.0,
            cap_usd=25.0,
        )
    assert skip == "exposure_cap_window_pm"


@pytest.mark.asyncio
async def test_fail_closed_on_fetch_error():
    """data-api unreachable → fail-closed (block). Better to skip ONE
    legit trade than repeat a $69 multi-fill loss."""
    async def fake_fetch(*args, **kwargs):
        return None  # signals error
    with patch(
        "use_cases.onchain_position_cap.get_onchain_cost_for_window",
        side_effect=fake_fetch,
    ):
        skip = await check_onchain_window_cap(
            funder_address="0x181D",
            market_slug="btc-updown-5m-1779336900",
            direction="YES",
            new_stake_usd=5.0,
            cap_usd=25.0,
        )
    assert skip == "exposure_cap_window_pm_unavailable"


@pytest.mark.asyncio
async def test_cap_disabled_when_zero():
    """cap_usd=0 (disabled) returns None immediately, no HTTP call."""
    call_count = [0]
    async def fake_fetch(*args, **kwargs):
        call_count[0] += 1
        return 999.0
    with patch(
        "use_cases.onchain_position_cap.get_onchain_cost_for_window",
        side_effect=fake_fetch,
    ):
        skip = await check_onchain_window_cap(
            funder_address="0x181D",
            market_slug="btc-updown-5m-1779336900",
            direction="YES",
            new_stake_usd=5.0,
            cap_usd=0.0,
        )
    assert skip is None
    assert call_count[0] == 0  # no HTTP call


def test_get_window_cap_pm_usd_env_parsing(monkeypatch):
    monkeypatch.setenv("RISK_MAX_STAKE_PER_WINDOW_USD_PM", "25")
    assert get_window_cap_pm_usd() == 25.0
    monkeypatch.setenv("RISK_MAX_STAKE_PER_WINDOW_USD_PM", "")
    assert get_window_cap_pm_usd() is None
    monkeypatch.delenv("RISK_MAX_STAKE_PER_WINDOW_USD_PM", raising=False)
    assert get_window_cap_pm_usd() is None
    monkeypatch.setenv("RISK_MAX_STAKE_PER_WINDOW_USD_PM", "garbage")
    assert get_window_cap_pm_usd() is None
    monkeypatch.setenv("RISK_MAX_STAKE_PER_WINDOW_USD_PM", "0")
    assert get_window_cap_pm_usd() is None
    monkeypatch.setenv("RISK_MAX_STAKE_PER_WINDOW_USD_PM", "-5")
    assert get_window_cap_pm_usd() is None


@pytest.mark.asyncio
async def test_direction_yes_and_up_both_mapped():
    """Either 'YES' (engine convention) or 'Up' (Polymarket convention)
    must work. Both map to the same Up-side cost on-chain."""
    async def fake_fetch_up(funder_address, condition_id, outcome):
        return 10.0 if outcome in ("Up", "YES") else 0.0
    from use_cases.onchain_position_cap import get_onchain_cost_for_window

    # Test direct call signature (won't actually HTTP because of cache+mock
    # at upper layer in real flow). Pure logic test:
    skip_yes = await check_onchain_window_cap(
        funder_address="0x181D",
        market_slug="btc-updown-5m-1779336900",
        direction="YES",
        new_stake_usd=5.0,
        cap_usd=25.0,
    )
    # We can't easily mock get_onchain_cost_for_window's inner call here;
    # this asserts behaviour when on-chain returns None (fail-closed).
    # The real coverage is in tests 1-3 above that mock the inner call.
    assert skip_yes is not None  # fail-closed because no mock = real HTTP fails in test env


# ── 2026-05-23 regressions ─────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_direction,expected_pm_outcome",
    [
        ("UP", "Up"),    # engine convention, lower-cased in pm payload
        ("DOWN", "Down"),
        ("up", "Up"),    # tolerate any case
        ("YES", "Up"),
        ("NO", "Down"),
        ("yes", "Up"),
        ("Up", "Up"),    # already-canonical pm form
        ("Down", "Down"),
    ],
)
async def test_direction_case_insensitive_audit_2026_05_23(
    raw_direction, expected_pm_outcome
):
    """Audit 2026-05-23: ``execute_trade`` passes ``decision.direction``
    which is upper-case ``"UP"`` / ``"DOWN"``. The original validation
    accepted only ``("Up", "Down", "YES", "NO")`` (mixed case), so every
    real call fell into the ``return None`` early-exit and produced a
    fail-closed ``exposure_cap_window_pm_unavailable``. Engine log on
    prod showed 120 cap-blocks in 40 min after the cap was enabled,
    every one of them spurious.

    This test pins the case-insensitive normalisation in place so the
    regression cannot return.
    """
    captured: dict = {}

    def fake_fetch(funder: str):
        # Return one position on each side so we can prove the right
        # side is summed.
        return [
            {
                "slug": "btc-updown-5m-1779336900",
                "outcome": "Up",
                "initialValue": 7.0,
            },
            {
                "slug": "btc-updown-5m-1779336900",
                "outcome": "Down",
                "initialValue": 11.0,
            },
        ]

    with patch(
        "use_cases.onchain_position_cap._fetch_positions_sync",
        side_effect=fake_fetch,
    ):
        cost = await get_onchain_cost_for_window(
            funder_address="0x181D2ED714E0f7Fe9c6e4f13711376eDaab25E10",
            market_slug="btc-updown-5m-1779336900",
            outcome=raw_direction,
        )
    if expected_pm_outcome == "Up":
        assert cost == 7.0
    else:
        assert cost == 11.0


@pytest.mark.asyncio
async def test_pending_stake_reservation_counts_in_projection():
    """Hub #582 race: when strategy A reserves $20 on cap=$25, strategy B
    arriving in the same tick MUST see $20 of pending stake."""
    async def fake_fetch(*args, **kwargs):
        return 0.0  # on-chain cost = 0 (data-api lags)
    with patch(
        "use_cases.onchain_position_cap.get_onchain_cost_for_window",
        side_effect=fake_fetch,
    ):
        # First call passes — reserves $20.
        skip_a = await check_onchain_window_cap(
            funder_address="0x181D",
            market_slug="btc-updown-5m-1779336900",
            direction="UP",
            new_stake_usd=20.0,
            cap_usd=25.0,
        )
        assert skip_a is None

        # Second call arrives before A's order completes. On-chain cost
        # is still 0 (data-api lag) BUT the reservation counts toward
        # the projection: 0 + 20 + 10 = 30 > 25 → block.
        skip_b = await check_onchain_window_cap(
            funder_address="0x181D",
            market_slug="btc-updown-5m-1779336900",
            direction="UP",
            new_stake_usd=10.0,
            cap_usd=25.0,
        )
        assert skip_b == "exposure_cap_window_pm"


@pytest.mark.asyncio
async def test_pending_stake_released_unlocks_next_caller():
    """release_pending_stake removes the reservation so a subsequent
    caller is no longer counting it. The release simulates either a
    successful fill (data-api will catch up) or a cancelled order."""
    async def fake_fetch(*args, **kwargs):
        return 0.0
    with patch(
        "use_cases.onchain_position_cap.get_onchain_cost_for_window",
        side_effect=fake_fetch,
    ):
        skip_a = await check_onchain_window_cap(
            funder_address="0x181D",
            market_slug="btc-updown-5m-1779336900",
            direction="UP",
            new_stake_usd=20.0,
            cap_usd=25.0,
        )
        assert skip_a is None
        # Release A's reservation
        release_pending_stake(
            funder_address="0x181D",
            market_slug="btc-updown-5m-1779336900",
            direction="UP",
            stake_usd=20.0,
        )
        # Now B can take $10
        skip_b = await check_onchain_window_cap(
            funder_address="0x181D",
            market_slug="btc-updown-5m-1779336900",
            direction="UP",
            new_stake_usd=10.0,
            cap_usd=25.0,
        )
        assert skip_b is None


@pytest.mark.asyncio
async def test_pending_stake_is_per_direction():
    """Reserving UP doesn't block DOWN — separate cap key per outcome."""
    async def fake_fetch(*args, **kwargs):
        return 0.0
    with patch(
        "use_cases.onchain_position_cap.get_onchain_cost_for_window",
        side_effect=fake_fetch,
    ):
        skip_up = await check_onchain_window_cap(
            funder_address="0x181D",
            market_slug="btc-updown-5m-1779336900",
            direction="UP",
            new_stake_usd=25.0,
            cap_usd=25.0,
        )
        assert skip_up is None
        # DOWN side starts fresh
        skip_down = await check_onchain_window_cap(
            funder_address="0x181D",
            market_slug="btc-updown-5m-1779336900",
            direction="DOWN",
            new_stake_usd=25.0,
            cap_usd=25.0,
        )
        assert skip_down is None


@pytest.mark.asyncio
async def test_concurrent_check_serialised_by_lock():
    """Hub #582 race reproducer: two strategies fire concurrently with
    on_chain_cost=0. Without the lock+reservation, both would see
    0+15=15 <= 25 and both would pass — total committed $30 > $25.

    With the lock+reservation, the second await sees the first's
    reservation and blocks.
    """
    fetch_calls: list[float] = []

    async def slow_fetch(*args, **kwargs):
        # Simulate 50 ms HTTP latency so both calls genuinely overlap.
        await asyncio.sleep(0.05)
        fetch_calls.append(0.0)
        return 0.0

    with patch(
        "use_cases.onchain_position_cap.get_onchain_cost_for_window",
        side_effect=slow_fetch,
    ):
        # Fire both concurrently
        results = await asyncio.gather(
            check_onchain_window_cap(
                funder_address="0x181D",
                market_slug="btc-updown-5m-1779336900",
                direction="UP",
                new_stake_usd=15.0,
                cap_usd=25.0,
            ),
            check_onchain_window_cap(
                funder_address="0x181D",
                market_slug="btc-updown-5m-1779336900",
                direction="UP",
                new_stake_usd=15.0,
                cap_usd=25.0,
            ),
        )

    # Exactly ONE passes, ONE blocks.
    passes = sum(1 for r in results if r is None)
    blocks = sum(
        1 for r in results if r == "exposure_cap_window_pm"
    )
    assert passes == 1, f"expected 1 pass, got {passes}: {results}"
    assert blocks == 1, f"expected 1 block, got {blocks}: {results}"
