"""30-minute activity digest — pure classification + fetcher happy path."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from alerts.activity_digest import (
    ActivityDigestFetcher,
    DigestPayload,
    build_digest,
)


NOW = 1_712_400_000


def _trade(ts, side="BUY", usdc=5.0, price=0.55, cid="c1"):
    return {
        "type": "TRADE",
        "side": side,
        "timestamp": ts,
        "usdcSize": usdc,
        "size": 10.0,
        "price": price,
        "conditionId": cid,
    }


def _redeem(ts, usdc=0.0, cid="c2"):
    return {
        "type": "REDEEM",
        "timestamp": ts,
        "usdcSize": usdc,
        "size": 10.0,
        "conditionId": cid,
    }


class TestBuildDigest:
    def test_empty(self):
        d = build_digest([], now_ts=NOW)
        assert d.rows == []
        assert d.trade_buy_count == 0
        assert d.redeem_win_count == 0

    def test_single_buy(self):
        d = build_digest([_trade(NOW - 60, "BUY", 5.0)], now_ts=NOW)
        assert d.trade_buy_count == 1
        assert d.trade_buy_usd == 5.0
        assert d.rows[0].kind == "TRADE_BUY"

    def test_single_win_redeem(self):
        d = build_digest([_redeem(NOW - 100, usdc=12.5)], now_ts=NOW)
        assert d.redeem_win_count == 1
        assert d.redeem_win_usd == 12.5

    def test_dust_redeem_classified_separately(self):
        d = build_digest([_redeem(NOW - 60, usdc=0.0)], now_ts=NOW)
        assert d.redeem_dust_count == 1
        assert d.redeem_win_count == 0

    def test_mixed(self):
        rows = [
            _trade(NOW - 60, "BUY", 5.0),
            _trade(NOW - 120, "SELL", 4.0),
            _redeem(NOW - 200, 12.0),
            _redeem(NOW - 240, 0.0),
        ]
        d = build_digest(rows, now_ts=NOW)
        assert d.trade_buy_count == 1
        assert d.trade_sell_count == 1
        assert d.redeem_win_count == 1
        assert d.redeem_dust_count == 1
        assert d.trade_buy_usd == 5.0
        assert d.trade_sell_usd == 4.0
        assert d.redeem_win_usd == 12.0

    def test_cutoff_filters_old_rows(self):
        rows = [
            _trade(NOW - 60, "BUY"),       # in window
            _trade(NOW - 10_000, "BUY"),   # ancient — filtered
        ]
        d = build_digest(rows, now_ts=NOW)
        assert d.trade_buy_count == 1
        assert len(d.rows) == 1

    def test_unknown_type_ignored(self):
        d = build_digest([{"type": "OTHER", "timestamp": NOW - 60}], now_ts=NOW)
        assert d.rows == []

    def test_malformed_row_skipped(self):
        rows = [
            {"type": "TRADE", "side": "BUY", "timestamp": "not-a-number"},
            _trade(NOW - 60, "BUY"),
        ]
        d = build_digest(rows, now_ts=NOW)
        assert d.trade_buy_count == 1


class TestFetcher:
    @pytest.mark.asyncio
    async def test_fetch_uses_build_digest(self):
        """Verify the fetcher round-trips raw data through build_digest."""
        fetcher = ActivityDigestFetcher(proxy_address="0xdeadbeef")
        fetcher.fetch_raw = AsyncMock(
            return_value=[_trade(NOW - 60, "BUY", 5.0)]
        )
        payload = await fetcher.fetch(now_ts=NOW)
        assert isinstance(payload, DigestPayload)
        assert payload.trade_buy_count == 1

    @pytest.mark.asyncio
    async def test_fetch_returns_empty_payload_on_client_error(self):
        import aiohttp

        fetcher = ActivityDigestFetcher(proxy_address="0xdeadbeef")
        fetcher.fetch_raw = AsyncMock(side_effect=aiohttp.ClientError("boom"))
        payload = await fetcher.fetch(now_ts=NOW)
        assert payload.rows == []
        assert payload.now_ts == NOW
