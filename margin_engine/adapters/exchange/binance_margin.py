"""
Binance cross-margin adapter — Ed25519 signed REST API.

Implements ExchangePort for real Binance 5x cross-margin trading.
All requests are signed with an Ed25519 private key per Binance docs.

Important: This adapter must run from a Binance-friendly geography
(UK/EU). The Montreal Polymarket engine CANNOT use this adapter.
"""
from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import aiohttp
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from margin_engine.domain.ports import ExchangePort
from margin_engine.domain.value_objects import Money, Price, TradeSide

logger = logging.getLogger(__name__)

BINANCE_BASE_URL = "https://api.binance.com"


def _load_ed25519_key(path: str) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from a PEM file."""
    pem_bytes = Path(path).read_bytes()
    key = serialization.load_pem_private_key(pem_bytes, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError(f"Expected Ed25519 private key, got {type(key).__name__}")
    return key


class BinanceMarginAdapter(ExchangePort):
    """
    Binance cross-margin exchange adapter.

    Uses the /sapi/v1/margin/* endpoints for cross-margin trading.
    Requires an Ed25519 API key pair registered on Binance.
    """

    def __init__(
        self,
        api_key: str,
        private_key_path: str,
        base_url: str = BINANCE_BASE_URL,
    ) -> None:
        self._api_key = api_key
        self._private_key = _load_ed25519_key(private_key_path)
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
        """Add timestamp and Ed25519 signature to params."""
        params["timestamp"] = int(time.time() * 1000)
        payload = urlencode(params).encode("utf-8")
        signature = self._private_key.sign(payload)
        params["signature"] = base64.b64encode(signature).decode("utf-8")
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
        """Get current ticker price (unsigned, public endpoint)."""
        session = await self._ensure_session()
        url = f"{self._base_url}/api/v3/ticker/price"
        async with session.get(url, params={"symbol": symbol}) as resp:
            data = await resp.json()
            return Price(value=float(data["price"]), pair=symbol)

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
