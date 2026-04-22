"""Unit tests for TiingoFeed multi-asset polling + env-driven subset.

Covers the post-PR-321 concern: ``v7_15m_sniper_{eth,sol,xrp}`` windows
read ``surface.delta_tiingo`` which comes from ``TiingoFeed.latest_prices``.
The feed already polls all four assets in a single top-of-book request, so
these tests pin that behaviour plus the ``TIINGO_ASSETS`` rollback knob.

Mocks Tiingo's ``/tiingo/crypto/top`` response shape directly — no network.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from data.feeds.tiingo_feed import (
    ALL_TICKERS,
    TiingoFeed,
    _resolve_tickers,
)


# ── Fixtures ──────────────────────────────────────────────────────────────

def _tob_payload(ticker: str, last: float) -> dict:
    """Build a single Tiingo top-of-book payload for one ticker."""
    return {
        "ticker": ticker,
        "topOfBookData": [
            {
                "lastPrice": last,
                "bidPrice": last - 0.5,
                "askPrice": last + 0.5,
                "bidExchange": "binance",
                "askExchange": "binance",
                "lastExchange": "binance",
            }
        ],
    }


def _full_4asset_response() -> list[dict]:
    return [
        _tob_payload("btcusd", 84500.0),
        _tob_payload("ethusd", 3200.0),
        _tob_payload("solusd", 180.0),
        _tob_payload("xrpusd", 0.52),
    ]


# ── _resolve_tickers tests ────────────────────────────────────────────────

class TestResolveTickers:
    def test_default_returns_all_four_assets(self, monkeypatch):
        monkeypatch.delenv("TIINGO_ASSETS", raising=False)
        assert _resolve_tickers() == ALL_TICKERS

    def test_explicit_list_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("TIINGO_ASSETS", "BTC")
        out = _resolve_tickers(explicit_assets=["ETH", "SOL"])
        assert set(out.keys()) == {"ETH", "SOL"}

    def test_env_subset_btc_only(self, monkeypatch):
        monkeypatch.setenv("TIINGO_ASSETS", "BTC")
        assert _resolve_tickers() == {"BTC": "btcusd"}

    def test_env_subset_btc_eth(self, monkeypatch):
        monkeypatch.setenv("TIINGO_ASSETS", "BTC,ETH")
        out = _resolve_tickers()
        assert set(out.keys()) == {"BTC", "ETH"}

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("TIINGO_ASSETS", "btc,eth")
        out = _resolve_tickers()
        assert set(out.keys()) == {"BTC", "ETH"}

    def test_unknown_asset_dropped_with_fallback(self, monkeypatch):
        # BOGUS is unknown but BTC is valid — return only BTC.
        monkeypatch.delenv("TIINGO_ASSETS", raising=False)
        out = _resolve_tickers(explicit_assets=["BTC", "BOGUS"])
        assert out == {"BTC": "btcusd"}

    def test_all_unknown_falls_back_to_all(self, monkeypatch):
        # Empty resolved set must NOT become a no-op feed.
        monkeypatch.delenv("TIINGO_ASSETS", raising=False)
        out = _resolve_tickers(explicit_assets=["DOGE", "LINK"])
        assert out == ALL_TICKERS

    def test_empty_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("TIINGO_ASSETS", "")
        assert _resolve_tickers() == ALL_TICKERS


# ── TiingoFeed construction / ticker param ────────────────────────────────

class TestTiingoFeedInit:
    def test_default_polls_all_four_assets(self, monkeypatch):
        monkeypatch.delenv("TIINGO_ASSETS", raising=False)
        feed = TiingoFeed(api_key="test", pool=None)
        assert set(feed._tickers.keys()) == {"BTC", "ETH", "SOL", "XRP"}
        # Tickers param feeds the URL — must include all four.
        assert "btcusd" in feed._tickers_param
        assert "ethusd" in feed._tickers_param
        assert "solusd" in feed._tickers_param
        assert "xrpusd" in feed._tickers_param

    def test_explicit_btc_only_backcompat(self, monkeypatch):
        monkeypatch.delenv("TIINGO_ASSETS", raising=False)
        feed = TiingoFeed(api_key="test", pool=None, assets=["BTC"])
        assert feed._tickers == {"BTC": "btcusd"}
        assert feed._tickers_param == "btcusd"

    def test_env_btc_only_rollback(self, monkeypatch):
        monkeypatch.setenv("TIINGO_ASSETS", "BTC")
        feed = TiingoFeed(api_key="test", pool=None)
        assert feed._tickers == {"BTC": "btcusd"}


# ── _poll behaviour (mocked HTTP) ─────────────────────────────────────────

class _FakeResp:
    def __init__(self, json_body, status=200):
        self._body = json_body
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self):
        return self._body


class _FakeSession:
    def __init__(self, json_body, status=200):
        self._body = json_body
        self._status = status

    def get(self, url, timeout=None):
        return _FakeResp(self._body, self._status)


@pytest.mark.asyncio
class TestTiingoFeedPollMultiAsset:
    async def test_full_4asset_response_populates_latest_prices(self, monkeypatch):
        monkeypatch.delenv("TIINGO_ASSETS", raising=False)
        feed = TiingoFeed(api_key="test", pool=None)
        session = _FakeSession(_full_4asset_response())

        await feed._poll(session)

        assert feed.latest_prices["BTC"] == 84500.0
        assert feed.latest_prices["ETH"] == 3200.0
        assert feed.latest_prices["SOL"] == 180.0
        assert feed.latest_prices["XRP"] == 0.52

    async def test_partial_response_missing_eth_logs_missing_asset(self, monkeypatch):
        monkeypatch.delenv("TIINGO_ASSETS", raising=False)
        feed = TiingoFeed(api_key="test", pool=None)
        # Tiingo plan that doesn't cover ETH — responds with 3 assets only.
        partial = [
            _tob_payload("btcusd", 84500.0),
            _tob_payload("solusd", 180.0),
            _tob_payload("xrpusd", 0.52),
        ]
        session = _FakeSession(partial)

        await feed._poll(session)

        assert "BTC" in feed.latest_prices
        assert "SOL" in feed.latest_prices
        assert "XRP" in feed.latest_prices
        assert "ETH" not in feed.latest_prices  # genuine miss, not parse bug
        # Observability: the received-set should have been logged (covered
        # by structlog capture — behaviour-level assertion is on state).
        assert feed._last_received_assets == frozenset({"BTC", "SOL", "XRP"})

    async def test_btc_only_feed_ignores_non_btc_response_items(self, monkeypatch):
        monkeypatch.delenv("TIINGO_ASSETS", raising=False)
        feed = TiingoFeed(api_key="test", pool=None, assets=["BTC"])
        # Tiingo happens to return all four — the feed must drop non-BTC
        # because this instance was configured BTC-only.
        session = _FakeSession(_full_4asset_response())

        await feed._poll(session)

        assert feed.latest_prices == {"BTC": 84500.0}

    async def test_backcompat_btc_path_unchanged(self, monkeypatch):
        """Regression guard — the pre-existing BTC path must still work."""
        monkeypatch.delenv("TIINGO_ASSETS", raising=False)
        feed = TiingoFeed(api_key="test", pool=None)
        session = _FakeSession([_tob_payload("btcusd", 84500.0)])

        await feed._poll(session)

        assert feed.latest_prices["BTC"] == 84500.0

    async def test_empty_response_does_not_crash(self, monkeypatch):
        monkeypatch.delenv("TIINGO_ASSETS", raising=False)
        feed = TiingoFeed(api_key="test", pool=None)
        session = _FakeSession([])

        # Must not raise — just leaves latest_prices empty.
        await feed._poll(session)
        assert feed.latest_prices == {}

    async def test_received_set_log_updates_on_change(self, monkeypatch):
        """Second poll missing an asset should update _last_received_assets."""
        monkeypatch.delenv("TIINGO_ASSETS", raising=False)
        feed = TiingoFeed(api_key="test", pool=None)

        # First poll — all four.
        await feed._poll(_FakeSession(_full_4asset_response()))
        assert feed._last_received_assets == frozenset({"BTC", "ETH", "SOL", "XRP"})

        # Second poll — ETH drops out. _last_received_assets updates and
        # the INFO log fires again (asserted by state transition).
        await feed._poll(_FakeSession([
            _tob_payload("btcusd", 84500.0),
            _tob_payload("solusd", 180.0),
            _tob_payload("xrpusd", 0.52),
        ]))
        assert feed._last_received_assets == frozenset({"BTC", "SOL", "XRP"})
