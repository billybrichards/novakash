"""
Telegram alert adapter for the margin engine.

Sends formatted trade notifications to the configured Telegram chat.
Same bot as the Polymarket engine but with distinct message formatting
to distinguish margin trades from Polymarket trades.
"""
from __future__ import annotations

import logging
from typing import Optional

import aiohttp

from margin_engine.domain.entities.position import Position
from margin_engine.domain.ports import AlertPort
from margin_engine.domain.value_objects import PositionState

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class TelegramAlertAdapter(AlertPort):
    """Sends margin engine alerts to Telegram."""

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            )
        return self._session

    async def _send(self, text: str) -> None:
        session = await self._ensure_session()
        url = f"{TELEGRAM_API}/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Telegram send failed: %s %s", resp.status, body)
        except Exception as e:
            logger.error("Telegram send error: %s", e)

    # ─── AlertPort implementation ────────────────────────────────────────

    async def send_trade_opened(self, position: Position) -> None:
        price = position.entry_price.value if position.entry_price else 0
        notional = position.notional.amount if position.notional else 0
        collateral = position.collateral.amount if position.collateral else 0

        text = (
            f"🔵 <b>MARGIN {position.side.value}</b> {position.asset}\n"
            f"Entry: ${price:,.2f}\n"
            f"Notional: ${notional:,.2f} ({position.leverage}x)\n"
            f"Collateral: ${collateral:,.2f}\n"
            f"Signal: {position.entry_signal_score:+.3f} ({position.entry_timescale})\n"
            f"SL: ${position.stop_loss.price:,.2f}" if position.stop_loss else ""
        )
        await self._send(text)

    async def send_trade_closed(self, position: Position) -> None:
        exit_price = position.exit_price.value if position.exit_price else 0
        pnl = position.realised_pnl
        emoji = "🟢" if pnl > 0 else "🔴"
        hold = position.hold_duration_s

        text = (
            f"{emoji} <b>MARGIN CLOSE</b> {position.side.value} {position.asset}\n"
            f"Exit: ${exit_price:,.2f}\n"
            f"P&L: ${pnl:+.2f}\n"
            f"Reason: {position.exit_reason.value if position.exit_reason else 'unknown'}\n"
            f"Hold: {int(hold)}s"
        )
        await self._send(text)

    async def send_kill_switch(self, reason: str) -> None:
        text = f"🚨 <b>MARGIN KILL SWITCH</b>\n{reason}"
        await self._send(text)

    async def send_error(self, message: str) -> None:
        text = f"⚠️ <b>MARGIN ERROR</b>\n{message}"
        await self._send(text)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
