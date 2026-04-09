"""
Binance cross-margin adapter — HMAC-SHA256 signed REST API.

Implements ExchangePort for real Binance 5x cross-margin trading.
All requests are signed with the API secret per Binance docs.

Important: This adapter must run from a Binance-friendly geography
(UK/EU). The Montreal Polymarket engine CANNOT use this adapter.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Optional
from urllib.parse import urlencode

import aiohttp

from margin_engine.domain.ports import ExchangePort
from margin_engine.domain.value_objects import Money, Price, TradeSide

logger = logging.getLogger(__name__)

BINANCE_BASE_URL = "https://api.binance.com"


class BinanceMarginAdapter(ExchangePort):
    """
    Binance cross-margin exchange adapter.

    Uses the /sapi/v1/margin/* endpoints for cross-margin trading.
    Requires BINANCE_API_KEY and BINANCE_API_SECRET.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str = BINANCE_BASE_URL,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self._base_url = base_url
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"X-MBX-APIKEY": self._api_key},
                timeout=aiohttp.ClientTimeout(total=10),
            )
        return self._session

    def _sign(self, params: dict) -> dict:
        """Add timestamp and HMAC-SHA256 signature to params."""
        params["timestamp"] = int(time.time() * 1000)
        query = urlencode(params)
        signature = hmac.new(self._api_secret, query.encode("utf-8"), hashlib.sha256).hexdigest()
        params["signature"] = signature
        return params

    # ─── ExchangePort implementation ─────────────────────────────────────

    async def place_market_order(
        self,
        symbol: str,
        side: TradeSide,
        notional: Money,
    ) -> tuple[str, Price]:
        """Place a cross-margin market order."""
        session = await self._ensure_session()

        binance_side = "BUY" if side == TradeSide.LONG else "SELL"
        params = self._sign({
            "symbol": symbol,
            "side": binance_side,
            "type": "MARKET",
            "quoteOrderQty": f"{notional.amount:.2f}",
            "sideEffectType": "MARGIN_BUY",  # auto-borrow for margin
            "isIsolated": "FALSE",  # cross margin
        })

        url = f"{self._base_url}/sapi/v1/margin/order"
        async with session.post(url, params=params) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise RuntimeError(f"Binance order failed: {data}")

        order_id = str(data["orderId"])
        # Weighted average fill price from fills array
        fills = data.get("fills", [])
        if fills:
            total_qty = sum(float(f["qty"]) for f in fills)
            avg_price = sum(float(f["price"]) * float(f["qty"]) for f in fills) / total_qty
        else:
            avg_price = float(data.get("price", 0))

        logger.info(
            "Binance margin order filled: %s %s %.2f USDT @ %.2f",
            binance_side, symbol, notional.amount, avg_price,
        )
        return order_id, Price(value=avg_price, pair=symbol)

    async def close_position(
        self,
        symbol: str,
        side: TradeSide,
        notional: Money,
    ) -> tuple[str, Price]:
        """Close by placing opposite-side market order with AUTO_REPAY."""
        session = await self._ensure_session()

        close_side = "SELL" if side == TradeSide.LONG else "BUY"
        params = self._sign({
            "symbol": symbol,
            "side": close_side,
            "type": "MARKET",
            "quoteOrderQty": f"{notional.amount:.2f}",
            "sideEffectType": "AUTO_REPAY",  # auto-repay borrowed assets
            "isIsolated": "FALSE",
        })

        url = f"{self._base_url}/sapi/v1/margin/order"
        async with session.post(url, params=params) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise RuntimeError(f"Binance close order failed: {data}")

        order_id = str(data["orderId"])
        fills = data.get("fills", [])
        if fills:
            total_qty = sum(float(f["qty"]) for f in fills)
            avg_price = sum(float(f["price"]) * float(f["qty"]) for f in fills) / total_qty
        else:
            avg_price = float(data.get("price", 0))

        logger.info(
            "Binance margin close filled: %s %s @ %.2f",
            close_side, symbol, avg_price,
        )
        return order_id, Price(value=avg_price, pair=symbol)

    async def get_balance(self) -> Money:
        """Get total cross-margin USDT balance."""
        session = await self._ensure_session()
        params = self._sign({})
        url = f"{self._base_url}/sapi/v1/margin/account"

        async with session.get(url, params=params) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise RuntimeError(f"Binance balance query failed: {data}")

        # Find USDT in userAssets
        for asset in data.get("userAssets", []):
            if asset["asset"] == "USDT":
                free = float(asset.get("free", 0))
                return Money.usd(free)

        return Money.zero()

    async def get_current_price(self, symbol: str) -> Price:
        """Get current ticker price."""
        session = await self._ensure_session()
        url = f"{self._base_url}/api/v3/ticker/price"
        async with session.get(url, params={"symbol": symbol}) as resp:
            data = await resp.json()
            return Price(value=float(data["price"]), pair=symbol)

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
