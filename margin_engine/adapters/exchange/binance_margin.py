"""
Binance cross-margin adapter — Ed25519 signed REST API.

Implements ExchangePort for real Binance 5x cross-margin trading.
All requests are signed with an Ed25519 private key per Binance docs.

Important: This adapter must run from a Binance-friendly geography
(UK/EU). The Montreal Polymarket engine CANNOT use this adapter.

Ground-truth contract:
  - get_mark() queries bookTicker (real bid/ask), never the last-trade ticker,
    so stop/TP evaluation matches the price you'd actually cross on close.
  - place_market_order() and close_position() return FillResult with
    commission extracted from the order's `fills` array — this is Binance's
    authoritative record of what we paid, not our estimate.
  - get_unrealised_pnl() uses those stored actual commissions + real bid/ask
    for a honest "what's this position worth right now" number. Per-position
    borrow interest is still an estimate because Binance cross-margin reports
    interest only at the account level, not per position.
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

from margin_engine.domain.entities.position import Position
from margin_engine.domain.ports import ExchangePort
from margin_engine.domain.value_objects import Money, Price, TradeSide
from margin_engine.domain.ports import FillResult

logger = logging.getLogger(__name__)

BINANCE_BASE_URL = "https://api.binance.com"

# Commission assets we can convert to USDT without another API call.
# USDT trivially; BNB/BTC require a spot price lookup and we handle those
# opportunistically in _commission_to_usdt.
_KNOWN_COMMISSION_ASSETS = {"USDT", "BUSD", "USDC"}


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

    # ─── Fill parsing helpers ────────────────────────────────────────────

    async def _commission_to_usdt(
        self,
        amount: float,
        asset: str,
    ) -> tuple[float, bool]:
        """
        Convert a commission amount from any asset to USDT.
        Returns (usdt_amount, is_actual).

        is_actual is True when the conversion was trivial (asset already
        USDT-equivalent) or succeeded via a spot ticker lookup. False when
        we had to fall back to treating the amount as USDT verbatim.
        """
        if amount <= 0:
            return 0.0, True
        if asset in _KNOWN_COMMISSION_ASSETS:
            return amount, True

        # BNB or BTC (or anything else with a USDT pair) — convert via ticker.
        # This is one extra public call per fill, worth it for accuracy.
        conversion_symbol = f"{asset}USDT"
        try:
            px = await self.get_current_price(conversion_symbol)
            return amount * px.value, True
        except Exception as e:
            logger.warning(
                "commission conversion failed for %s %s: %s — treating as USDT fallback",
                amount,
                asset,
                e,
            )
            return amount, False

    async def _parse_fill_response(
        self,
        symbol: str,
        data: dict,
        fallback_notional: float,
    ) -> FillResult:
        """
        Pull order_id, volume-weighted fill price, filled notional, and
        real commission out of a Binance margin order response.

        Binance's margin order response shape (type=MARKET):
        {
          "orderId": 12345,
          "fills": [
            {"price": "80000.00", "qty": "0.001", "commission": "0.08", "commissionAsset": "USDT"},
            ...
          ]
        }
        """
        order_id = str(data.get("orderId", ""))
        fills = data.get("fills") or []

        if not fills:
            # Shouldn't happen for MARKET orders, but guard anyway
            fallback_price = float(data.get("price") or 0) or 0.0
            logger.warning(
                "Binance order %s returned no fills — using fallback price %.2f",
                order_id,
                fallback_price,
            )
            return FillResult(
                order_id=order_id,
                fill_price=Price(value=max(fallback_price, 1e-8), pair=symbol),
                filled_notional=fallback_notional,
                commission=0.0,
                commission_asset="UNKNOWN",
                commission_is_actual=False,
            )

        # Volume-weighted average fill price
        total_qty = 0.0
        quote_sum = 0.0  # sum of price * qty → actual filled notional in USDT
        commission_usdt = 0.0
        any_conversion_failed = False
        dominant_asset = "USDT"

        for f in fills:
            qty = float(f.get("qty", 0))
            price = float(f.get("price", 0))
            total_qty += qty
            quote_sum += qty * price

            c_amount = float(f.get("commission", 0))
            c_asset = f.get("commissionAsset", "USDT")
            if c_amount > 0:
                dominant_asset = c_asset  # last-wins; they're usually uniform
                converted, ok = await self._commission_to_usdt(c_amount, c_asset)
                commission_usdt += converted
                if not ok:
                    any_conversion_failed = True

        avg_price = quote_sum / total_qty if total_qty > 0 else 0.0

        return FillResult(
            order_id=order_id,
            fill_price=Price(value=max(avg_price, 1e-8), pair=symbol),
            filled_notional=quote_sum,
            commission=commission_usdt,
            commission_asset=dominant_asset,
            commission_is_actual=not any_conversion_failed,
        )

    # ─── ExchangePort implementation ─────────────────────────────────────

    async def place_market_order(
        self,
        symbol: str,
        side: TradeSide,
        notional: Money,
    ) -> FillResult:
        """Place a cross-margin market order."""
        session = await self._ensure_session()

        binance_side = "BUY" if side == TradeSide.LONG else "SELL"
        params = self._sign(
            {
                "symbol": symbol,
                "side": binance_side,
                "type": "MARKET",
                "quoteOrderQty": f"{notional.amount:.2f}",
                "sideEffectType": "MARGIN_BUY",  # auto-borrow for margin
                "isIsolated": "FALSE",  # cross margin
            }
        )

        url = f"{self._base_url}/sapi/v1/margin/order"
        async with session.post(url, params=params) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise RuntimeError(f"Binance order failed: {data}")

        fill = await self._parse_fill_response(
            symbol=symbol,
            data=data,
            fallback_notional=notional.amount,
        )
        logger.info(
            "Binance margin order filled: %s %s req=%.2f filled=%.2f @ %.2f commission=%.4f %s (actual=%s)",
            binance_side,
            symbol,
            notional.amount,
            fill.filled_notional,
            fill.fill_price.value,
            fill.commission,
            fill.commission_asset,
            fill.commission_is_actual,
        )
        return fill

    async def close_position(
        self,
        symbol: str,
        side: TradeSide,
        notional: Money,
    ) -> FillResult:
        """Close by placing opposite-side market order with AUTO_REPAY."""
        session = await self._ensure_session()

        close_side = "SELL" if side == TradeSide.LONG else "BUY"
        params = self._sign(
            {
                "symbol": symbol,
                "side": close_side,
                "type": "MARKET",
                "quoteOrderQty": f"{notional.amount:.2f}",
                "sideEffectType": "AUTO_REPAY",  # auto-repay borrowed assets
                "isIsolated": "FALSE",
            }
        )

        url = f"{self._base_url}/sapi/v1/margin/order"
        async with session.post(url, params=params) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise RuntimeError(f"Binance close order failed: {data}")

        fill = await self._parse_fill_response(
            symbol=symbol,
            data=data,
            fallback_notional=notional.amount,
        )
        logger.info(
            "Binance margin close filled: %s %s filled=%.2f @ %.2f commission=%.4f %s (actual=%s)",
            close_side,
            symbol,
            fill.filled_notional,
            fill.fill_price.value,
            fill.commission,
            fill.commission_asset,
            fill.commission_is_actual,
        )
        return fill

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
        """Last-trade ticker — fine for quick balance conversions and reference,
        NOT for stop/TP evaluation (use get_mark for that)."""
        session = await self._ensure_session()
        url = f"{self._base_url}/api/v3/ticker/price"
        async with session.get(url, params={"symbol": symbol}) as resp:
            data = await resp.json()
            return Price(value=float(data["price"]), pair=symbol)

    async def get_mark(self, symbol: str, side: TradeSide) -> Price:
        """Return the bid (for LONG close) or ask (for SHORT close).

        This is the price we'd actually cross on a market close, not the
        last-trade print. During fast moves the difference between the two
        can exceed our stop-loss threshold — using bookTicker keeps
        stop/TP evaluation honest.
        """
        session = await self._ensure_session()
        url = f"{self._base_url}/api/v3/ticker/bookTicker"
        async with session.get(url, params={"symbol": symbol}) as resp:
            data = await resp.json()
            if "bidPrice" not in data or "askPrice" not in data:
                raise RuntimeError(f"Binance bookTicker malformed: {data}")

        price_str = data["bidPrice"] if side == TradeSide.LONG else data["askPrice"]
        return Price(value=float(price_str), pair=symbol)

    async def get_unrealised_pnl(self, position: Position) -> float:
        """Compute unrealised P&L at the real close-side mark.

        Uses Position.unrealised_pnl_net which factors in entry commission
        (real, from stored FillResult), estimated exit commission, and
        accrued borrow interest.
        """
        if not position.asset:
            return 0.0
        mark = await self.get_mark(f"{position.asset}USDT", position.side)
        return position.unrealised_pnl_net(mark.value)

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
