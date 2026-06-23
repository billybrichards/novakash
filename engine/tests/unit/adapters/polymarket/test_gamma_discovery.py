"""Unit test for GammaMarketDiscovery adapter. No real HTTP."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from adapters.polymarket.gamma_discovery import GammaMarketDiscovery
from domain.value_objects import Asset, Timeframe


@pytest.mark.asyncio
async def test_find_window_market_btc_5m_returns_tokens():
    mock_http = MagicMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(
        return_value=[
            {
                "slug": "btc-updown-5m-1776201300",
                "markets": [
                    {
                        "conditionId": "0xdeadbeef",
                        "clobTokenIds": '["111", "222"]',
                        "bestAsk": "0.52",
                    }
                ],
            }
        ]
    )
    mock_http.get = AsyncMock(return_value=mock_resp)

    discovery = GammaMarketDiscovery(mock_http)
    result = await discovery.find_window_market(Asset("BTC"), Timeframe(300), 1776201300)

    assert result is not None
    assert result.up_token_id == "111"
    assert result.down_token_id == "222"
    assert result.condition_id == "0xdeadbeef"
    assert result.market_slug == "btc-updown-5m-1776201300"
    mock_http.get.assert_awaited_once()
    call_kwargs = mock_http.get.call_args.kwargs
    assert call_kwargs["params"] == {"slug": "btc-updown-5m-1776201300"}


@pytest.mark.asyncio
async def test_find_window_market_eth_15m_slug_format():
    mock_http = MagicMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=[])
    mock_http.get = AsyncMock(return_value=mock_resp)

    discovery = GammaMarketDiscovery(mock_http)
    result = await discovery.find_window_market(Asset("ETH"), Timeframe(900), 1776202200)

    assert result is None
    assert (
        mock_http.get.call_args.kwargs["params"]["slug"]
        == "eth-updown-15m-1776202200"
    )


@pytest.mark.asyncio
async def test_find_window_market_empty_response_returns_none():
    mock_http = MagicMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=[])
    mock_http.get = AsyncMock(return_value=mock_resp)

    discovery = GammaMarketDiscovery(mock_http)
    result = await discovery.find_window_market(Asset("BTC"), Timeframe(300), 1776201300)
    assert result is None


@pytest.mark.asyncio
async def test_find_window_market_http_exception_returns_none():
    mock_http = MagicMock()
    mock_http.get = AsyncMock(side_effect=Exception("boom"))

    discovery = GammaMarketDiscovery(mock_http)
    result = await discovery.find_window_market(Asset("BTC"), Timeframe(300), 1776201300)
    assert result is None
