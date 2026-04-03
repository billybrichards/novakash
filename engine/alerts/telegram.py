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

    async def send_entry_alert(self, order: "Order") -> None:
        """Send detailed alert when a trade is PLACED (before resolution)."""
        if not self.trade_alerts_enabled:
            return

        try:
            meta = order.metadata or {}
            asset = meta.get("market_slug", order.market_id or "").split("-")[0].upper() or "BTC"
            tf = meta.get("timeframe", "5m")
            direction_emoji = "📈" if order.direction == "YES" else "📉"
            mode_tag = "📄 PAPER" if self._paper_mode else "💰 LIVE"
            market_slug = meta.get("market_slug", order.market_id or "—")
            entry_label = meta.get("entry_reason") or meta.get("entry_label", "—")
            delta_pct = meta.get("delta_pct")
            confidence = meta.get("confidence", "—")
            # Handle both float confidence (0.0-1.0 from new evaluator) and string ("HIGH"/"MODERATE")
            if isinstance(confidence, (int, float)):
                conf_pct = int(confidence * 100) if confidence <= 1.0 else int(confidence)
                tier = meta.get("tier", "")
                confidence = f"{tier}" if tier else f"{conf_pct}%"
            else:
                conf_pct = self._confidence_to_int(confidence)
            window_open = meta.get("window_open_price")
            data_source = "LIVE execution" if not self._paper_mode else "REAL prices, PAPER execution"

            tp = float(order.price) if order.price else 0.50
            shares = order.stake_usd / tp if tp > 0 else 0
            potential = shares * 1.0 - order.stake_usd
            potential_pct = (potential / order.stake_usd * 100) if order.stake_usd > 0 else 0

            vpin = meta.get("vpin")
            score = meta.get("score")
            
            lines = [
                f"{direction_emoji} *BET PLACED — {asset} {tf}* ({mode_tag})",
                f"",
                f"📊 *Signal*",
                f"Direction: `{order.direction}` {'(UP)' if order.direction == 'YES' else '(DOWN)'}",
                f"Entry: `{entry_label}`",
                f"Delta: `{delta_pct:+.4f}%`" if delta_pct is not None else None,
                f"🔬 VPIN: `{vpin:.4f}`" if isinstance(vpin, (int, float)) else None,
                f"Score: `{score:.1f}`" if isinstance(score, (int, float)) else None,
                f"Confidence: `{conf_pct}%` ({confidence})",
                f"",
                f"💰 *Pricing* ({data_source})",
                f"Token Price: `${tp:.4f}`",
                f"Shares: `{shares:.2f}`",
                f"Stake: `${order.stake_usd:.2f}`",
                f"Potential Win: `+${potential:.2f}` (`+{potential_pct:.0f}%`)",
                f"",
                f"🔗 *Market*",
                f"Venue: `{order.venue}`",
                f"Market: `{market_slug}`",
                f"Window Open: `${window_open:,.2f}`" if window_open else None,
                f"Status: `{order.status.value}`",
            ]

            clob_id = meta.get("clob_order_id")
            if clob_id:
                lines.append(f"CLOB Order: `{clob_id}`")

            lines = [l for l in lines if l is not None]

            # Wallet & portfolio info
            lines.append(f"")
            if self._risk_manager:
                try:
                    status = self._risk_manager.get_status()
                    bankroll = status.get("current_bankroll", 0)
                    daily_pnl = status.get("daily_pnl", 0)
                    open_exposure = status.get("open_exposure_usd", 0)
                    daily_sign = "+" if daily_pnl >= 0 else ""
                    lines.append(f"💼 Bankroll: `${bankroll:.2f}`")
                    lines.append(f"📅 Daily P&L: `{daily_sign}${daily_pnl:.2f}`")
                    if open_exposure > 0:
                        lines.append(f"📊 Open Exposure: `${open_exposure:.2f}`")
                except Exception:
                    pass

            if self._poly_client and not self._paper_mode:
                try:
                    wallet_balance = await self._poly_client.get_balance()
                    lines.append(f"🏦 Available: `${wallet_balance:.2f}` USDC")
                except Exception:
                    pass

            await self._send("\n".join(lines))
        except Exception as exc:
            self._log.warning("telegram.send_entry_alert_failed", error=str(exc))

    async def send_trade_alert(self, order: "Order") -> None:
        """Send detailed alert when a trade RESOLVES (win or loss)."""
        if not self.trade_alerts_enabled:
            return

        try:
            meta = order.metadata or {}
            asset = meta.get("market_slug", order.market_id or "").split("-")[0].upper() or "BTC"
            tf = meta.get("timeframe", "5m")
            direction_emoji = "📈" if order.direction == "YES" else "📉"
            mode_tag = "📄 PAPER" if self._paper_mode else "💰 LIVE"
            market_slug = meta.get("market_slug", order.market_id or "—")
            confidence = meta.get("confidence", "—")
            conf_pct = self._confidence_to_int(confidence)
            delta_pct = meta.get("delta_pct")
            window_open = meta.get("window_open_price")
            entry_label = meta.get("entry_label", "—")
            data_source = "LIVE execution" if not self._paper_mode else "REAL prices, PAPER execution"

            if order.outcome == "WIN":
                result_emoji = "✅"
                result_text = "WIN"
            else:
                result_emoji = "❌"
                result_text = "LOSS"

            tp = float(order.price) if order.price else 0.50
            shares = order.stake_usd / tp if tp > 0 else 0

            lines = [
                f"{result_emoji} *{result_text} — {asset} {tf}* ({mode_tag})",
                f"",
                f"📊 *Signal*",
                f"Direction: `{order.direction}` {'(UP)' if order.direction == 'YES' else '(DOWN)'}",
                f"Entry: `{entry_label}`",
                f"Delta: `{delta_pct:+.4f}%`" if delta_pct is not None else None,
                f"Confidence: `{conf_pct}%` ({confidence})",
                f"",
                f"💰 *Pricing* ({data_source})",
                f"Token Price: `${tp:.4f}`",
                f"Shares: `{shares:.2f}`",
                f"Stake: `${order.stake_usd:.2f}`",
                f"",
                f"🔗 *Market*",
                f"Venue: `{order.venue}`",
                f"Market: `{market_slug}`",
                f"Window Open: `${window_open:,.2f}`" if window_open else None,
                f"Status: `{order.status.value}`",
            ]

            clob_id = meta.get("clob_order_id")
            tx_hash = meta.get("tx_hash")
            block_ref = meta.get("block_number")
            if clob_id:
                lines.append(f"CLOB Order: `{clob_id}`")
            if tx_hash:
                lines.append(f"Tx: `{tx_hash}`")
            if block_ref:
                lines.append(f"Block: `{block_ref}`")

            lines = [l for l in lines if l is not None]

            if order.pnl_usd is not None:
                pnl_sign = "+" if order.pnl_usd >= 0 else ""
                lines.append(f"")
                lines.append(f"{result_emoji} *PnL: `{pnl_sign}${order.pnl_usd:.2f}`*")

            # Running totals
            lines.append(f"")
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

            if self._poly_client and not self._paper_mode:
                try:
                    wallet_balance = await self._poly_client.get_balance()
                    lines.append(f"🏦 Poly Wallet: `${wallet_balance:.2f}` USDC")
                except Exception:
                    pass

            await self._send("\n".join(lines))
        except Exception as exc:
            self._log.warning("telegram.send_trade_alert_failed", error=str(exc))

    @staticmethod
    def _confidence_to_int(confidence: str) -> int:
        """Convert confidence word to integer percentage."""
        mapping = {"HIGH": 85, "MODERATE": 65, "LOW": 45, "NONE": 20}
        return mapping.get(confidence.upper() if isinstance(confidence, str) else "", 50)

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

    def format_coinglass_block(self, snapshot) -> str:
        """
        Format a CoinGlassSnapshot into a detailed multi-line string for sitrep.

        Args:
            snapshot: CoinGlassSnapshot dataclass instance.

        Returns:
            Multi-line formatted string (no trailing newline).
        """
        try:
            if snapshot is None or not snapshot.connected:
                err = ""
                if snapshot is not None and snapshot.last_error:
                    err = f" ({snapshot.last_error[:60]})"
                return f"🔬 *CoinGlass*: ❌ Disconnected{err}"

            cg = snapshot

            # OI
            oi_b = cg.oi_usd / 1_000_000_000
            oi_delta_sign = "+" if cg.oi_delta_pct_1m >= 0 else ""
            oi_line = f"OI: `${oi_b:.1f}B` (Δ `{oi_delta_sign}{cg.oi_delta_pct_1m:.2f}%` 1m)"

            # Liquidations
            liq_total_m = cg.liq_total_usd_1m / 1_000_000
            liq_long_m = cg.liq_long_usd_1m / 1_000_000
            liq_short_m = cg.liq_short_usd_1m / 1_000_000
            liq_line = (
                f"Liq 1m: `${liq_total_m:.1f}M` "
                f"(Long: `${liq_long_m:.1f}M` / Short: `${liq_short_m:.1f}M`)"
            )

            # Long/Short ratio
            ls_line = (
                f"L/S Ratio: `{cg.long_short_ratio:.2f}` "
                f"(Long `{cg.long_pct:.0f}%` / Short `{cg.short_pct:.0f}%`)"
            )

            # Top traders
            top_ratio = cg.top_position_ratio
            top_line = (
                f"Top Traders: Long `{cg.top_position_long_pct:.0f}%` / "
                f"Short `{cg.top_position_short_pct:.0f}%` "
                f"(ratio `{top_ratio:.2f}`)"
            )

            # Taker buy/sell
            taker_buy_m = cg.taker_buy_volume_1m / 1_000_000
            taker_sell_m = cg.taker_sell_volume_1m / 1_000_000
            total_taker = cg.taker_buy_volume_1m + cg.taker_sell_volume_1m
            taker_ratio = (cg.taker_buy_volume_1m / total_taker) if total_taker > 0 else 1.0
            taker_line = (
                f"Taker: Buy `${taker_buy_m:.1f}M` / Sell `${taker_sell_m:.1f}M` "
                f"(ratio `{taker_ratio:.2f}`)"
            )

            # Funding
            funding_pct = cg.funding_rate * 100
            annual_pct = cg.funding_rate_annual * 100 if hasattr(cg, 'funding_rate_annual') else funding_pct * 3 * 365
            funding_line = f"Funding: `{funding_pct:.4f}%` (annual `{annual_pct:.1f}%`)"

            # Status
            status_line = "Status: ✅ Connected"

            return "\n".join([
                "🔬 *CoinGlass*",
                oi_line,
                liq_line,
                ls_line,
                top_line,
                taker_line,
                funding_line,
                status_line,
            ])
        except Exception as exc:
            self._log.warning("telegram.format_coinglass_block_failed", error=str(exc))
            return "🔬 *CoinGlass*: ⚠️ Format error"

    async def send_window_report(
        self,
        window_ts: int,
        asset: str,
        timeframe: str,
        open_price: float,
        close_price: float,
        delta_pct: float,
        vpin: float,
        regime: str,
        cg_snapshot=None,
        direction=None,
        confidence=None,
        trade_placed: bool = False,
        skip_reason: str | None = None,
        cg_modifier: float = 0.0,
    ) -> None:
        """
        Send a per-window summary after each 5m/15m window evaluation.

        Args:
            window_ts:    Unix timestamp of the window open.
            asset:        Asset symbol (e.g. "BTC").
            timeframe:    "5m" or "15m".
            open_price:   Window open price.
            close_price:  Current / close price.
            delta_pct:    Price delta % over the window.
            vpin:         VPIN value at evaluation time.
            regime:       CASCADE / TRANSITION / NORMAL / CALM.
            cg_snapshot:  CoinGlassSnapshot at evaluation time (may be None).
            direction:    "UP" or "DOWN" (None if no signal).
            confidence:   Signal confidence string or float.
            trade_placed: Whether a trade was actually placed.
            skip_reason:  Reason trade was skipped (if not placed).
            cg_modifier:  CoinGlass confidence modifier applied.
        """
        try:
            from datetime import datetime, timezone
            ts_str = datetime.fromtimestamp(window_ts, tz=timezone.utc).strftime("%H:%M UTC")

            delta_sign = "+" if delta_pct >= 0 else ""
            trade_emoji = "✅" if trade_placed else "⏭️"
            dir_str = f"`{direction}`" if direction else "`—`"

            if isinstance(confidence, float) and confidence <= 1.0:
                conf_str = f"{int(confidence * 100)}%"
            elif isinstance(confidence, float):
                conf_str = f"{int(confidence)}%"
            else:
                conf_str = f"`{confidence}`" if confidence else "`—`"

            # Regime emoji
            regime_emoji = {
                "CASCADE": "🌊",
                "TRANSITION": "🔄",
                "NORMAL": "📊",
                "CALM": "😴",
            }.get(regime, "❓")

            lines = [
                f"📋 *Window Report — {asset} {timeframe}*",
                f"",
                f"⏰ `{ts_str}` | Open: `${open_price:,.2f}` → Close: `${close_price:,.2f}`",
                f"Δ: `{delta_sign}{delta_pct:.4f}%`",
                f"",
                f"🔬 VPIN: `{vpin:.4f}` | Regime: {regime_emoji} `{regime}`",
                f"",
            ]

            # CoinGlass block
            if cg_snapshot is not None:
                lines.append(self.format_coinglass_block(cg_snapshot))
                lines.append(f"")

            # Signal
            lines += [
                f"📡 *Signal*",
                f"Direction: {dir_str} | Confidence: {conf_str}",
                f"CG Modifier: `{cg_modifier:+.2f}`",
                f"",
                f"{trade_emoji} Trade placed: `{'YES' if trade_placed else 'NO'}`",
            ]
            if not trade_placed and skip_reason:
                lines.append(f"Skip reason: `{skip_reason}`")

            await self._send("\n".join(lines))
        except Exception as exc:
            self._log.warning("telegram.send_window_report_failed", error=str(exc))

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
