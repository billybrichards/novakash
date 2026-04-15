"""Gamma API market discovery adapter.

Implements MarketDiscoveryPort using Polymarket's Gamma events endpoint.
Logic extracted from engine/data/feeds/polymarket_5min.py::_fetch_market_data.
"""
from __future__ import annotations

import json as _json
from typing import Optional

import httpx
import structlog

from domain.value_objects import Asset, Timeframe, WindowMarket
from use_cases.ports.market_discovery import MarketDiscoveryPort

log = structlog.get_logger(__name__)

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"


class GammaMarketDiscovery(MarketDiscoveryPort):
    """Polymarket Gamma API adapter.

    Builds slug ``{asset}-updown-{5m|15m}-{window_ts}``, fetches event,
    returns a WindowMarket VO. Never raises — returns None on any error.
    """

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http = http_client

    async def find_window_market(
        self, asset: Asset, tf: Timeframe, window_ts: int
    ) -> Optional[WindowMarket]:
        slug = f"{asset.symbol.lower()}-updown-{tf.label}-{window_ts}"
        try:
            resp = await self._http.get(GAMMA_EVENTS_URL, params={"slug": slug})
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("gamma.http_error", slug=slug, error=str(exc)[:200])
            return None

        if not isinstance(data, list) or not data:
            return None

        event = data[0]
        markets = event.get("markets") or []
        if not markets:
            return None
        market = markets[0]

        raw_tokens = market.get("clobTokenIds") or []
        if isinstance(raw_tokens, str):
            try:
                raw_tokens = _json.loads(raw_tokens)
            except (ValueError, TypeError):
                raw_tokens = []
        if len(raw_tokens) < 2:
            return None

        return WindowMarket(
            condition_id=str(market.get("conditionId") or ""),
            up_token_id=str(raw_tokens[0]),
            down_token_id=str(raw_tokens[1]),
            market_slug=slug,
            active=bool(market.get("active", True)),
        )
