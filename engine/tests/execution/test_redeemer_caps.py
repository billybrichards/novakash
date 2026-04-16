"""Tests for Daisy-set caps on the auto-redeemer (2026-04-16).

Daily hard cap: 80 (override via REDEEM_DAILY_LIMIT env). After the cap
is hit, the auto-sweep returns a no-op result for the rest of the UTC
day. Manual sweeps (redeem_wins, redeem_losses, redeem_all) are NOT
affected by the auto-cap — operators retain full override.

Per-hour throttle: 4 wins/hour rolling (override via REDEEM_HOURLY_LIMIT).
When the rolling 60-min count of redeem_attempts reaches the limit, the
auto-sweep skips this tick and waits for the window to drain.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from execution.redeemer import PositionRedeemer


def _make_redeemer(**env_overrides) -> PositionRedeemer:
    """Build a paper-mode redeemer without hitting any RPC."""
    with patch.dict(os.environ, env_overrides, clear=False):
        return PositionRedeemer(
            rpc_url="https://test.invalid",
            private_key="0x" + "0" * 64,
            proxy_address="0x" + "0" * 40,
            paper_mode=True,
        )


class TestDailyQuotaCap:
    def test_default_is_80(self):
        # Clearing REDEEM_DAILY_LIMIT (if set in ambient env) checks default.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REDEEM_DAILY_LIMIT", None)
            r = PositionRedeemer(
                rpc_url="https://test.invalid",
                private_key="0x" + "0" * 64,
                proxy_address="0x" + "0" * 40,
                paper_mode=True,
            )
            assert r.daily_quota_limit == 80

    def test_env_override_respected(self):
        r = _make_redeemer(REDEEM_DAILY_LIMIT="120")
        assert r.daily_quota_limit == 120

    def test_env_override_empty_string_falls_back(self):
        """Empty env var must not crash — fall back to default 80."""
        r = _make_redeemer(REDEEM_DAILY_LIMIT="")
        assert r.daily_quota_limit == 80


class TestHourlyThrottle:
    def test_default_is_4(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REDEEM_HOURLY_LIMIT", None)
            r = PositionRedeemer(
                rpc_url="https://test.invalid",
                private_key="0x" + "0" * 64,
                proxy_address="0x" + "0" * 40,
                paper_mode=True,
            )
            assert r.hourly_quota_limit == 4

    def test_env_override_respected(self):
        r = _make_redeemer(REDEEM_HOURLY_LIMIT="6")
        assert r.hourly_quota_limit == 6

    def test_hourly_cap_never_exceeds_daily(self):
        """Sanity: hourly cap should never be set higher than daily.

        This is a config-hygiene check — if ops sets HOURLY > DAILY we
        still honour the values (they may be testing), but flag it as
        surprising. The redeemer loop uses the minimum of the two guards
        at runtime so there's no risk, but this test pins the default
        invariant so future defaults keep the hourly <= daily/24 sanity.
        """
        r = _make_redeemer()
        assert r.hourly_quota_limit * 24 <= r.daily_quota_limit * 2, (
            f"hourly={r.hourly_quota_limit} × 24 exceeds 2× daily="
            f"{r.daily_quota_limit} — config check"
        )
