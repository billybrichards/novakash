"""
Telegram Alerter

Sends trading notifications to a configured Telegram chat via the Bot API.

Alert types:
  - Trade alerts: order filled, resolved, PnL
  - Cascade alerts: state transitions in the CascadeDetector FSM
  - System alerts: engine start/stop, kill-switch triggered, connectivity issues
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
import aiohttp
import structlog

from config.settings import Settings
from execution.order_manager import Order

log = structlog.get_logger(__name__)

AlertLevel = Literal["info", "warning", "critical"]

_LEVEL_EMOJI = {
    "info": "ℹ️",
    "warning": "⚠️",
    "critical": "🚨",
}


class TelegramAlerter:
    """
    Async Telegram notification client.

    Messages are sent as Markdown via the sendMessage Bot API endpoint.
    Failures are logged but never raised (alerts must not crash the engine).
    """

    def __init__(self, settings: Settings) -> None:
        self._token = settings.TELEGRAM_BOT_TOKEN
        self._chat_id = settings.TELEGRAM_CHAT_ID
        self._base_url = f"https://api.telegram.org/bot{self._token}"
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self) -> None:
        """Create the aiohttp session."""
        self._session = aiohttp.ClientSession()

    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session:
            await self._session.close()

    # ─── Public Alert Methods ─────────────────────────────────────────────────

    async def send_trade_alert(self, order: Order) -> None:
        """
        Send a trade notification with order details and PnL.

        Args:
            order: The resolved (or newly opened) order.
        """
        emoji = "✅" if order.pnl_usd and order.pnl_usd >= 0 else "❌"
        pnl_str = f"${order.pnl_usd:+.2f}" if order.pnl_usd is not None else "pending"

        message = (
            f"{emoji} *Trade Alert*\n"
            f"Strategy: `{order.strategy}`\n"
            f"Market: `{order.market_slug}`\n"
            f"Direction: `{order.direction}`\n"
            f"Stake: `${order.stake_usd:.2f}`\n"
            f"Status: `{order.status.value}`\n"
            f"PnL: `{pnl_str}`\n"
            f"_at {datetime.utcnow().strftime('%H:%M:%S UTC')}_"
        )

        await self._send(message)

    async def send_cascade_alert(
        self,
        state: str,
        direction: Optional[str],
        vpin: float,
        oi_delta_pct: float,
        liq_volume_usd: float,
    ) -> None:
        """
        Send a cascade state-machine transition alert.

        Args:
            state:          New FSM state (CASCADE_DETECTED, BET_SIGNAL, etc.)
            direction:      CASCADE direction (UP/DOWN/None)
            vpin:           Current VPIN value.
            oi_delta_pct:   OI change percentage.
            liq_volume_usd: Liquidation volume in USD.
        """
        level: AlertLevel = "warning" if state in ("CASCADE_DETECTED", "EXHAUSTING") else "critical"
        emoji = _LEVEL_EMOJI[level]
        dir_str = direction or "N/A"

        message = (
            f"{emoji} *Cascade Alert — {state}*\n"
            f"Direction: `{dir_str}`\n"
            f"VPIN: `{vpin:.4f}`\n"
            f"OI Δ: `{oi_delta_pct:+.2%}`\n"
            f"Liq Vol: `${liq_volume_usd:,.0f}`\n"
            f"_at {datetime.utcnow().strftime('%H:%M:%S UTC')}_"
        )

        await self._send(message)

    async def send_system_alert(self, text: str, level: AlertLevel = "info") -> None:
        """
        Send a generic system alert.

        Args:
            text:  Alert message text.
            level: Severity — "info" | "warning" | "critical"
        """
        emoji = _LEVEL_EMOJI[level]
        message = f"{emoji} *System Alert*\n{text}\n_at {datetime.utcnow().strftime('%H:%M:%S UTC')}_"
        await self._send(message)

    # ─── Internal ─────────────────────────────────────────────────────────────

    async def _send(self, text: str) -> None:
        """Send a raw Markdown message. Errors are swallowed to protect the engine."""
        if not self._session:
            log.warning("telegram.no_session", hint="Call start() first")
            return

        url = f"{self._base_url}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }

        try:
            async with self._session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning("telegram.send_failed", status=resp.status, body=body[:200])
                else:
                    log.debug("telegram.sent", length=len(text))
        except Exception as exc:
            log.error("telegram.send_error", error=str(exc))
