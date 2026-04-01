"""
Telegram Alerter

Sends trading alerts to a Telegram chat via the Bot API (aiohttp, no library deps).

Alert types:
  - Trade alert: order placed/resolved
  - Cascade alert: liquidation cascade signal
  - System alert: engine status / errors
  - Kill switch alert: emergency stop

All exceptions are caught internally — alerts must never crash the engine.
Uses simple HTTP POST to the Telegram Bot API sendMessage endpoint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import aiohttp
import structlog

if TYPE_CHECKING:
    from data.models import CascadeSignal
    from execution.order_manager import Order

log = structlog.get_logger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"

# Level → emoji mapping
_LEVEL_EMOJI = {
    "info": "🟢",
    "warning": "🟡",
    "error": "🔴",
    "critical": "🔴",
}

# Direction → emoji
_DIRECTION_EMOJI = {
    "YES": "📈",
    "NO": "📉",
    "ARB": "⚖️",
    "down": "📉",
    "up": "📈",
}


class TelegramAlerter:
    """
    Sends Telegram messages via the Bot API using aiohttp.

    All public methods catch all exceptions to ensure alert failures
    never crash or block the engine.

    Notification toggles:
      - alerts_paper: send alerts for paper trades (default: True)
      - alerts_live:  send alerts for live trades (default: False)

    Running totals:
      - Accepts a risk_manager reference to include bankroll + daily P&L
      - Accepts a poly_client reference to include wallet USDC balance
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        alerts_paper: bool = True,
        alerts_live: bool = False,
        paper_mode: bool = True,
        risk_manager=None,
        poly_client=None,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._url = TELEGRAM_API_BASE.format(token=bot_token)
        self._alerts_paper = alerts_paper
        self._alerts_live = alerts_live
        self._paper_mode = paper_mode
        self._risk_manager = risk_manager
        self._poly_client = poly_client
        self._log = log.bind(component="TelegramAlerter")

        if not bot_token or not chat_id:
            self._log.warning("telegram.not_configured", reason="missing bot_token or chat_id")
        else:
            self._log.info(
                "telegram.configured",
                alerts_paper=alerts_paper,
                alerts_live=alerts_live,
                paper_mode=paper_mode,
            )

    def set_risk_manager(self, risk_manager) -> None:
        """Set reference after construction (avoids circular dep)."""
        self._risk_manager = risk_manager

    def set_poly_client(self, poly_client) -> None:
        """Set reference after construction (avoids circular dep)."""
        self._poly_client = poly_client

    @property
    def trade_alerts_enabled(self) -> bool:
        """Check if trade alerts should fire based on current mode + toggles."""
        if self._paper_mode:
            return self._alerts_paper
        return self._alerts_live

    # ─── Public Alert Methods ─────────────────────────────────────────────────

    async def send_trade_alert(self, order: "Order") -> None:
        """
        Send a trade execution / resolution alert.

        Respects alerts_paper / alerts_live toggles.
        Safely handles missing attributes (e.g. market_slug).

        Format:
            📈 [strategy] Trade Alert
            Direction: YES/NO/ARB
            Stake: $X.XX
            Venue: polymarket/opinion
            Status: OPEN/RESOLVED_WIN/RESOLVED_LOSS
            [PnL: $X.XX]
        """
        if not self.trade_alerts_enabled:
            self._log.debug(
                "telegram.trade_alert_skipped",
                reason="disabled_for_mode",
                paper_mode=self._paper_mode,
            )
            return

        try:
            direction_emoji = _DIRECTION_EMOJI.get(order.direction.upper(), "🎯")
            outcome_emoji = ""
            if hasattr(order, "outcome") and order.outcome:
                outcome_emoji = "✅ " if order.outcome == "WIN" else "❌ "

            mode_tag = "📄 PAPER" if self._paper_mode else "💰 LIVE"
            market_slug = getattr(order, "market_slug", None) or order.metadata.get("market_slug", order.market_id or "—")

            # Entry timing and token price from metadata
            entry_label = order.metadata.get("entry_label", "—")
            delta_pct = order.metadata.get("delta_pct")
            confidence = order.metadata.get("confidence", "—")
            token_price = order.price

            lines = [
                f"{direction_emoji} *Trade Alert — {order.strategy}* ({mode_tag})",
                f"Direction: `{order.direction}`",
                f"Entry: `{entry_label}`",
                f"Delta: `{delta_pct:+.4f}%`" if delta_pct is not None else None,
                f"Token Price: `${float(token_price):.4f}`" if token_price else None,
                f"Confidence: `{confidence}`",
                f"Stake: `${order.stake_usd:.2f}`",
                f"Venue: `{order.venue}`",
                f"Market: `{market_slug}`",
                f"Status: `{order.status.value}`",
            ]
            lines = [l for l in lines if l is not None]  # filter None entries

            if order.pnl_usd is not None:
                pnl_sign = "+" if order.pnl_usd >= 0 else ""
                lines.append(f"{outcome_emoji}PnL: `{pnl_sign}${order.pnl_usd:.2f}`")

            # ── Running Totals ────────────────────────────────────────────
            lines.append("")  # blank line separator

            if self._risk_manager:
                try:
                    status = self._risk_manager.get_status()
                    bankroll = status.get("current_bankroll", 0)
                    daily_pnl = status.get("daily_pnl", 0)
                    drawdown = status.get("drawdown_pct", 0)
                    daily_sign = "+" if daily_pnl >= 0 else ""

                    lines.append(f"💼 Bankroll: `${bankroll:.2f}`")
                    lines.append(f"📅 Daily P&L: `{daily_sign}${daily_pnl:.2f}`")
                    if drawdown > 0.01:
                        lines.append(f"📉 Drawdown: `{drawdown:.1%}`")
                except Exception:
                    pass

            if self._poly_client:
                try:
                    wallet_balance = await self._poly_client.get_balance()
                    lines.append(f"🏦 Poly Wallet: `${wallet_balance:.2f}` USDC")
                except Exception:
                    pass  # wallet balance is best-effort

            await self._send("\n".join(lines))

        except Exception as exc:
            self._log.warning("telegram.send_trade_alert_failed", error=str(exc))

    async def send_cascade_alert(self, signal: "CascadeSignal") -> None:
        """
        Send a liquidation cascade FSM state alert.

        Format:
            🌊 CASCADE DETECTED / EXHAUSTING / BET SIGNAL
            Direction: down/up
            VPIN: 0.XX
            OI Delta: ±X.XX%
            Liq Volume: $X.XXM
        """
        try:
            state_labels = {
                "CASCADE_DETECTED": "🌊 CASCADE DETECTED",
                "EXHAUSTING": "🌊 CASCADE EXHAUSTING",
                "BET_SIGNAL": "🎯 CASCADE BET SIGNAL",
                "COOLDOWN": "⏳ CASCADE COOLDOWN",
                "IDLE": "💤 CASCADE IDLE",
            }
            label = state_labels.get(signal.state, f"🌊 {signal.state}")
            direction_str = (
                f"{_DIRECTION_EMOJI.get(signal.direction or '', '')} `{signal.direction}`"
                if signal.direction
                else "`none`"
            )

            liq_m = signal.liq_volume_usd / 1_000_000
            oi_pct = signal.oi_delta_pct * 100

            lines = [
                f"*{label}*",
                f"Direction: {direction_str}",
                f"VPIN: `{signal.vpin:.4f}`",
                f"OI Delta: `{oi_pct:+.2f}%`",
                f"Liq Volume (5m): `${liq_m:.2f}M`",
            ]

            await self._send("\n".join(lines))

        except Exception as exc:
            self._log.warning("telegram.send_cascade_alert_failed", error=str(exc))

    async def send_system_alert(self, message: str, level: str = "info") -> None:
        """
        Send a system status alert.

        Level determines emoji:
          - info    → 🟢
          - warning → 🟡
          - error   → 🔴
        """
        try:
            emoji = _LEVEL_EMOJI.get(level.lower(), "🟢")
            text = f"{emoji} *System Alert*\n`{message}`"
            await self._send(text)
        except Exception as exc:
            self._log.warning("telegram.send_system_alert_failed", error=str(exc))

    async def send_kill_switch_alert(self) -> None:
        """Send an emergency kill switch activation alert."""
        try:
            text = (
                "🛑 *KILL SWITCH ACTIVATED*\n"
                "All trading has been halted.\n"
                "Manual intervention required to resume."
            )
            await self._send(text)
        except Exception as exc:
            self._log.warning("telegram.send_kill_switch_alert_failed", error=str(exc))

    # ─── Internal ─────────────────────────────────────────────────────────────

    async def _send(self, text: str) -> None:
        """
        POST a message to Telegram Bot API.

        Uses parse_mode=MarkdownV2 for formatting.
        Silently logs on failure — never raises.
        """
        if not self._bot_token or not self._chat_id:
            self._log.debug("telegram.skipped", reason="not_configured")
            return

        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        self._log.warning(
                            "telegram.api_error",
                            status=resp.status,
                            body=body[:200],
                        )
                    else:
                        self._log.debug("telegram.sent", chars=len(text))

        except aiohttp.ClientError as exc:
            self._log.warning("telegram.network_error", error=str(exc))
        except Exception as exc:
            self._log.warning("telegram.unexpected_error", error=str(exc))
