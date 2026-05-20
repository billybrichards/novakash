"""Use case: exposure caps — risk barriers checked before every order.

Hub #554 / branch fix/risk-exposure-caps-and-sub-fill-writer.

Three independent caps, evaluated in this order before the engine
resolves the token ID + dispatches the order in
``ExecuteTradeUseCase``:

  1. Per-window stake cap (``RISK_MAX_STAKE_PER_WINDOW_USD``)
       Sum of fresh stake already placed in the same
       (asset, window_ts, timeframe) by any strategy. Defends
       against the cross-strategy correlated-double-down problem when
       v9 / v9.2 / v12_combo all fire on the same DOWN signal.

  2. Per-asset open exposure cap (``RISK_MAX_OPEN_EXPOSURE_PER_ASSET_USD``)
       Sum of fresh stake on still-open positions (no resolved_at)
       for the same asset. Bounds blast radius across windows when
       the engine retries after partial fills.

  3. Daily fresh stake cap (``RISK_MAX_DAILY_FRESH_STAKE_USD``)
       Sum of fresh stake placed since UTC midnight (today). Cuts the
       worst-case daily drawdown if every signal happens to fire.

All caps default OFF when the corresponding env var is unset or
≤ 0. When OFF the check function returns ``None`` instantly,
preserving byte-identical behaviour on the BTC 5 m hot path.

"Fresh stake" excludes any trade that the writer has tagged with
``is_secondary_fill=true`` (Hub #554 Layer 1) — those rows already
have a primary fill row carrying the same stake; counting them
would double-count exposure.

The repository half lives in
``engine/adapters/persistence/pg_exposure_repo.py`` so this module
stays pure (config + arithmetic, no DB). Tests use a
``FakeExposureRepository`` to exercise the cap logic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Protocol


# ── Env var names (centralised so the test suite + docs reference one place) ─

_ENV_WINDOW = "RISK_MAX_STAKE_PER_WINDOW_USD"
_ENV_ASSET = "RISK_MAX_OPEN_EXPOSURE_PER_ASSET_USD"
_ENV_DAILY = "RISK_MAX_DAILY_FRESH_STAKE_USD"


def _read_cap(env_name: str) -> Optional[float]:
    """Parse a positive float from ``os.environ[env_name]``.

    Returns ``None`` when the env var is unset, empty, non-numeric, or
    ≤ 0 — all of which disable the corresponding cap. This keeps the
    "defaults OFF" guarantee tight: an operator typo (negative,
    "off", "false") does NOT silently enable a tiny cap.
    """
    raw = os.environ.get(env_name)
    if raw is None or raw.strip() == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value


@dataclass(frozen=True)
class ExposureCapConfig:
    """Frozen snapshot of the three caps, read once at use-case wiring.

    Defaults all-None → all caps OFF. This means a unit-test or
    composition root that just instantiates ``ExposureCapConfig()``
    gets the byte-identical pre-fix behaviour (caller short-circuits
    without ever calling the repository).

    ``from_env()`` is the production path — used by ``engine/main.py``
    once at boot.
    """

    max_stake_per_window_usd: Optional[float] = None
    max_open_exposure_per_asset_usd: Optional[float] = None
    max_daily_fresh_stake_usd: Optional[float] = None

    @classmethod
    def from_env(cls) -> "ExposureCapConfig":
        """Read all three caps from ``os.environ`` (defaults OFF)."""
        return cls(
            max_stake_per_window_usd=_read_cap(_ENV_WINDOW),
            max_open_exposure_per_asset_usd=_read_cap(_ENV_ASSET),
            max_daily_fresh_stake_usd=_read_cap(_ENV_DAILY),
        )

    @property
    def all_disabled(self) -> bool:
        """True when every cap is OFF — caller can skip DB queries entirely."""
        return (
            self.max_stake_per_window_usd is None
            and self.max_open_exposure_per_asset_usd is None
            and self.max_daily_fresh_stake_usd is None
        )


class ExposureRepository(Protocol):
    """Async port the cap check reads from.

    Concrete production impl: ``PgExposureCapRepository``.
    Tests: ``FakeExposureRepository`` with hard-coded totals.

    All three methods return USD stake totals (≥ 0) excluding
    ``trades.is_secondary_fill = TRUE`` rows (those are already
    captured by their primary fill — counting both would double the
    apparent exposure).
    """

    async def get_window_fresh_stake_usd(
        self,
        asset: str,
        window_ts: int,
        timeframe: str,
    ) -> float: ...

    async def get_asset_open_exposure_usd(self, asset: str) -> float: ...

    async def get_daily_fresh_stake_usd(self, asset: str) -> float: ...


async def check_exposure_caps(
    *,
    config: ExposureCapConfig,
    repo: ExposureRepository,
    asset: str,
    window_ts: int,
    timeframe: str,
    proposed_stake_usd: float,
) -> Optional[str]:
    """Return ``None`` if the trade may proceed, else a skip-reason string.

    Skip-reason format (stable for log-parsing + audit alerts):
      ``"exposure_cap_window: $existing + $proposed > $cap"``
      ``"exposure_cap_asset:  $existing + $proposed > $cap"``
      ``"exposure_cap_daily:  $existing + $proposed > $cap"``

    Evaluation order: window → asset → daily. The first cap to fail
    short-circuits — we never query the next repo method, keeping the
    happy-path query cost at zero queries when every cap is disabled
    and three queries (one per cap) when every cap is on. Each query
    is a single SUM over an indexed time-bounded scan on ``trades``,
    so even at 5 m cadence the hot path stays well under the 5 s
    ``EXECUTE_TRADE_DB_TIMEOUT_S`` budget.

    Args:
        config: cap configuration (defaults all OFF).
        repo: ExposureRepository implementation (PG or fake).
        asset: e.g. ``"BTC"``.
        window_ts: integer unix-second window start, e.g. 1779293100.
        timeframe: e.g. ``"5m"``.
        proposed_stake_usd: the stake about to be placed by this call,
            BEFORE any clipping.

    Returns:
        ``None`` on pass, or a stable skip-reason string on first cap
        breach. Never raises — repo errors propagate to the caller's
        ``except Exception`` block; this function is purely arithmetic
        plus three awaits.
    """
    if config.all_disabled:
        return None

    proposed = max(0.0, float(proposed_stake_usd))

    # 1. Per-window cap (most specific — same asset+window+timeframe).
    if config.max_stake_per_window_usd is not None:
        cap = config.max_stake_per_window_usd
        existing = await repo.get_window_fresh_stake_usd(
            asset=asset,
            window_ts=window_ts,
            timeframe=timeframe,
        )
        if existing + proposed > cap:
            return (
                f"exposure_cap_window: ${existing:.2f} + ${proposed:.2f} > ${cap:.2f}"
            )

    # 2. Per-asset open exposure cap (still-open positions across windows).
    if config.max_open_exposure_per_asset_usd is not None:
        cap = config.max_open_exposure_per_asset_usd
        existing = await repo.get_asset_open_exposure_usd(asset)
        if existing + proposed > cap:
            return (
                f"exposure_cap_asset: ${existing:.2f} + ${proposed:.2f} > ${cap:.2f}"
            )

    # 3. Daily fresh-stake cap (rolling-UTC-day).
    if config.max_daily_fresh_stake_usd is not None:
        cap = config.max_daily_fresh_stake_usd
        existing = await repo.get_daily_fresh_stake_usd(asset)
        if existing + proposed > cap:
            return (
                f"exposure_cap_daily: ${existing:.2f} + ${proposed:.2f} > ${cap:.2f}"
            )

    return None
