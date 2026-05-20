"""Unit tests for ``use_cases.exposure_caps.check_exposure_caps`` (Hub #554).

Three caps, three tests — one cap firing per test. Each test uses a
``FakeExposureRepository`` with hard-coded totals so the cap arithmetic is
the only variable under test.

Defaults-OFF guarantee is also exercised explicitly: with an empty
``ExposureCapConfig()`` the check returns ``None`` and never queries the
repository (no method gets called).
"""

from __future__ import annotations

import pytest

from use_cases.exposure_caps import (
    ExposureCapConfig,
    check_exposure_caps,
)


class _FakeExposureRepository:
    """Hard-coded totals + call counters for assert tracking."""

    def __init__(
        self,
        *,
        window_total: float = 0.0,
        asset_total: float = 0.0,
        daily_total: float = 0.0,
    ) -> None:
        self.window_total = window_total
        self.asset_total = asset_total
        self.daily_total = daily_total
        self.window_calls = 0
        self.asset_calls = 0
        self.daily_calls = 0

    async def get_window_fresh_stake_usd(
        self, asset: str, window_ts: int, timeframe: str
    ) -> float:
        self.window_calls += 1
        return self.window_total

    async def get_asset_open_exposure_usd(self, asset: str) -> float:
        self.asset_calls += 1
        return self.asset_total

    async def get_daily_fresh_stake_usd(self, asset: str) -> float:
        self.daily_calls += 1
        return self.daily_total


# ─── Cap-firing tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_window_cap_blocks_when_existing_plus_proposed_exceeds_cap():
    """Per-window cap fires first. Existing $8 + proposed $5 > $10 cap."""
    config = ExposureCapConfig(
        max_stake_per_window_usd=10.0,
        max_open_exposure_per_asset_usd=100.0,  # not under test — high
        max_daily_fresh_stake_usd=1000.0,        # not under test — high
    )
    repo = _FakeExposureRepository(
        window_total=8.0,
        asset_total=0.0,
        daily_total=0.0,
    )

    reason = await check_exposure_caps(
        config=config,
        repo=repo,
        asset="BTC",
        window_ts=1779293100,
        timeframe="5m",
        proposed_stake_usd=5.0,
    )

    assert reason is not None
    assert reason.startswith("exposure_cap_window:")
    assert "$8.00" in reason  # existing
    assert "$5.00" in reason  # proposed
    assert "$10.00" in reason  # cap
    # Short-circuit: only window-cap query was made.
    assert repo.window_calls == 1
    assert repo.asset_calls == 0
    assert repo.daily_calls == 0


@pytest.mark.asyncio
async def test_asset_cap_blocks_when_window_passes_but_asset_exceeds():
    """Per-asset cap fires when window passes but asset open exposure > cap."""
    config = ExposureCapConfig(
        max_stake_per_window_usd=10.0,
        max_open_exposure_per_asset_usd=20.0,
        max_daily_fresh_stake_usd=1000.0,
    )
    # window: $0 + $5 ≤ $10 → pass.
    # asset:  $18 + $5 > $20 → block.
    repo = _FakeExposureRepository(
        window_total=0.0,
        asset_total=18.0,
        daily_total=0.0,
    )

    reason = await check_exposure_caps(
        config=config,
        repo=repo,
        asset="BTC",
        window_ts=1779293100,
        timeframe="5m",
        proposed_stake_usd=5.0,
    )

    assert reason is not None
    assert reason.startswith("exposure_cap_asset:")
    assert "$18.00" in reason
    assert "$5.00" in reason
    assert "$20.00" in reason
    assert repo.window_calls == 1
    assert repo.asset_calls == 1
    # Daily NOT queried — asset cap short-circuited.
    assert repo.daily_calls == 0


@pytest.mark.asyncio
async def test_daily_cap_blocks_when_window_and_asset_pass_but_daily_exceeds():
    """Daily cap fires when window + asset pass but daily total > cap."""
    config = ExposureCapConfig(
        max_stake_per_window_usd=10.0,
        max_open_exposure_per_asset_usd=100.0,
        max_daily_fresh_stake_usd=50.0,
    )
    # window: $0 + $7 ≤ $10 → pass.
    # asset:  $0 + $7 ≤ $100 → pass.
    # daily:  $48 + $7 > $50 → block.
    repo = _FakeExposureRepository(
        window_total=0.0,
        asset_total=0.0,
        daily_total=48.0,
    )

    reason = await check_exposure_caps(
        config=config,
        repo=repo,
        asset="BTC",
        window_ts=1779293100,
        timeframe="5m",
        proposed_stake_usd=7.0,
    )

    assert reason is not None
    assert reason.startswith("exposure_cap_daily:")
    assert "$48.00" in reason
    assert "$7.00" in reason
    assert "$50.00" in reason
    assert repo.window_calls == 1
    assert repo.asset_calls == 1
    assert repo.daily_calls == 1


# ─── Defaults-OFF guarantee ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_config_short_circuits_without_querying_repo():
    """Empty ExposureCapConfig() → all caps OFF → no repo calls, no block."""
    config = ExposureCapConfig()
    assert config.all_disabled is True

    repo = _FakeExposureRepository(
        window_total=999.0,  # would breach if queried.
        asset_total=999.0,
        daily_total=999.0,
    )

    reason = await check_exposure_caps(
        config=config,
        repo=repo,
        asset="BTC",
        window_ts=1779293100,
        timeframe="5m",
        proposed_stake_usd=10_000.0,  # absurd; would breach any sane cap.
    )

    assert reason is None
    # Repo never touched.
    assert repo.window_calls == 0
    assert repo.asset_calls == 0
    assert repo.daily_calls == 0


@pytest.mark.asyncio
async def test_exactly_at_cap_passes():
    """existing + proposed == cap MUST pass (strict > comparison)."""
    config = ExposureCapConfig(max_stake_per_window_usd=10.0)
    repo = _FakeExposureRepository(window_total=7.0)

    reason = await check_exposure_caps(
        config=config,
        repo=repo,
        asset="BTC",
        window_ts=1779293100,
        timeframe="5m",
        proposed_stake_usd=3.0,  # 7 + 3 = 10 = cap → must pass
    )

    assert reason is None
