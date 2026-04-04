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

import asyncio
import os
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
        # Read Anthropic API key once at construction — never per-call
        self._anthropic_api_key: str | None = os.environ.get("ANTHROPIC_API_KEY") or None
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

            # Use actual fill price if available (FOK fills at market, not cap)
            _afp = meta.get("actual_fill_price")
            tp = float(_afp) if _afp else (float(order.price) if order.price else 0.50)
            _shares_matched = meta.get("shares_matched")
            shares = float(_shares_matched) if _shares_matched else (order.stake_usd / tp if tp > 0 else 0)
            potential = shares * 1.0 - order.stake_usd
            potential_pct = (potential / order.stake_usd * 100) if order.stake_usd > 0 else 0

            vpin = meta.get("vpin")
            score = meta.get("score")
            
            lines = [
                f"{direction_emoji} *{'✅ FILLED' if meta.get('filled') or meta.get('actual_fill_price') else '⏳ PLACED'} — {asset} {tf}* ({mode_tag})",
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

    async def send_trade_alert(self, order: "Order", cg_snapshot=None) -> None:
        """Send detailed alert when a trade RESOLVES (win or loss).

        Args:
            order:       The resolved Order dataclass.
            cg_snapshot: CoinGlassSnapshot at resolution time (optional).
        """
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
            entry_reason_detail = meta.get("entry_reason_detail")
            vpin = meta.get("vpin")
            cg_modifier = meta.get("cg_modifier")
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
                f"🔬 VPIN: `{vpin:.4f}`" if isinstance(vpin, (int, float)) else None,
                f"Confidence: `{conf_pct}%` ({confidence})",
                f"CG Modifier: `{cg_modifier:+.2f}`" if isinstance(cg_modifier, (int, float)) else None,
            ]

            # Original entry reason detail
            if entry_reason_detail:
                lines.append(f"")
                lines.append(f"📝 *Entry Reason*")
                lines.append(f"`{entry_reason_detail}`")

            # Entry vs outcome comparison
            lines.append(f"")
            lines.append(f"📈 *Entry vs Outcome*")
            if delta_pct is not None and window_open:
                direction_word = "UP" if order.direction == "YES" else "DOWN"
                prediction_correct = (
                    (order.direction == "YES" and order.outcome == "WIN") or
                    (order.direction == "NO" and order.outcome == "WIN")
                )
                correct_str = "✅ Prediction correct" if prediction_correct else "❌ Prediction wrong"
                lines.append(
                    f"Entered `{direction_word}` at δ`{delta_pct:+.4f}%` "
                    f"from open `${window_open:,.2f}`. {correct_str}."
                )

            lines += [
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

            # CoinGlass data AT RESOLUTION TIME
            if cg_snapshot is not None:
                lines.append(f"")
                lines.append(f"🔬 *CoinGlass @ Resolution*")
                lines.append(self.format_coinglass_block(cg_snapshot))

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

            # AI outcome assessment (non-blocking, uses Claude)
            try:
                assessment = await self._generate_outcome_assessment(order, cg_snapshot)
                if assessment:
                    lines.append(f"")
                    lines.append(f"🤖 *AI Assessment*")
                    lines.append(assessment)
            except Exception:
                pass

            await self._send("\n".join(lines))
        except Exception as exc:
            self._log.warning("telegram.send_trade_alert_failed", error=str(exc))

    async def _generate_outcome_assessment(self, order: "Order", cg_snapshot=None) -> str:
        """
        Call Claude API to generate a 2-3 sentence expert assessment of the trade outcome.

        Uses claude-opus-4-6 with a 60-second timeout. Returns a fallback string if
        the API key is missing, the call fails, or the timeout is hit.

        The ANTHROPIC_API_KEY is read once at TelegramAlerter construction — never per call.
        """
        if not self._anthropic_api_key:
            return "Assessment unavailable (no ANTHROPIC_API_KEY set)."

        try:
            meta = order.metadata or {}

            # ── Build a rich prompt with ALL available data ─────────────────
            asset = meta.get("market_slug", order.market_id or "").split("-")[0].upper() or "BTC"
            tf = meta.get("timeframe", "5m")
            direction_word = "UP (YES)" if order.direction == "YES" else "DOWN (NO)"
            delta_pct = meta.get("delta_pct")
            vpin = meta.get("vpin")
            confidence = meta.get("confidence", "—")
            cg_modifier = meta.get("cg_modifier")
            entry_reason_detail = meta.get("entry_reason_detail", "—")
            window_open = meta.get("window_open_price")
            tp = float(order.price) if order.price else 0.50
            outcome = order.outcome or "UNKNOWN"
            pnl = order.pnl_usd

            # Format CoinGlass metrics if available
            cg_lines: list[str] = []
            if cg_snapshot is not None and getattr(cg_snapshot, "connected", False):
                cg = cg_snapshot
                try:
                    total_taker = cg.taker_buy_volume_1m + cg.taker_sell_volume_1m
                    taker_sell_ratio = (cg.taker_sell_volume_1m / total_taker) if total_taker > 0 else 0.5
                    cg_lines = [
                        f"  OI: ${cg.oi_usd / 1e9:.2f}B (Δ {cg.oi_delta_pct_1m:+.3f}% 1m)",
                        f"  Liq 1m: ${cg.liq_total_usd_1m / 1e6:.2f}M "
                        f"(Long ${cg.liq_long_usd_1m / 1e6:.2f}M / Short ${cg.liq_short_usd_1m / 1e6:.2f}M)",
                        f"  L/S Ratio: {cg.long_short_ratio:.3f} "
                        f"(Long {cg.long_pct:.1f}% / Short {cg.short_pct:.1f}%)",
                        f"  Top Traders: Long {cg.top_position_long_pct:.1f}% / Short {cg.top_position_short_pct:.1f}%",
                        f"  Taker Sell Ratio: {taker_sell_ratio:.3f} "
                        f"(Buy ${cg.taker_buy_volume_1m / 1e6:.2f}M / Sell ${cg.taker_sell_volume_1m / 1e6:.2f}M)",
                        f"  Funding Rate: {cg.funding_rate * 100:.4f}%",
                    ]
                except Exception:
                    cg_lines = ["  CoinGlass data parse error"]
            else:
                cg_lines = ["  CoinGlass: not available at resolution"]

            user_content = (
                f"TRADE SUMMARY — {asset} {tf} Polymarket prediction market\n\n"
                f"Direction bet: {direction_word}\n"
                f"Token price paid: ${tp:.4f}\n"
                f"Stake: ${order.stake_usd:.2f}\n"
                f"Window open price: ${window_open:,.2f}" if window_open else f"Window open price: unknown"
            )
            user_content += (
                f"\nOutcome: {outcome}"
                f"\nPnL: {'+' if (pnl or 0) >= 0 else ''}${pnl:.2f}" if pnl is not None else f"\nPnL: unknown"
            )
            user_content += (
                f"\n\nSIGNAL DATA AT ENTRY:\n"
                f"  VPIN: {vpin:.4f}" if isinstance(vpin, (int, float)) else "\n  VPIN: n/a"
            )
            user_content += (
                f"\n  Delta: {delta_pct:+.4f}%" if delta_pct is not None else "\n  Delta: n/a"
            )
            user_content += (
                f"\n  Confidence: {confidence}"
                f"\n  CoinGlass modifier: {cg_modifier:+.2f}" if isinstance(cg_modifier, (int, float)) else "\n  CoinGlass modifier: n/a"
            )
            user_content += f"\n  Entry reason: {entry_reason_detail}"
            user_content += f"\n\nCOINGLASS AT RESOLUTION:\n" + "\n".join(cg_lines)

            payload = {
                "model": "claude-opus-4-6",
                "max_tokens": 200,
                "system": (
                    "You are an expert crypto derivatives trading analyst reviewing a "
                    "5-minute BTC prediction market trade on Polymarket. Assess the entry "
                    "decision quality, signal alignment, and outcome. Write 2-3 sentences."
                ),
                "messages": [{"role": "user", "content": user_content}],
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.anthropic.com/v1/messages",
                    json=payload,
                    headers={
                        "x-api-key": self._anthropic_api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        self._log.warning(
                            "telegram.claude_api_error",
                            status=resp.status,
                            body=body[:200],
                        )
                        return "Assessment unavailable (API error)."
                    data = await resp.json()
                    text = data.get("content", [{}])[0].get("text", "").strip()
                    return text if text else "Assessment unavailable."

        except asyncio.TimeoutError:
            self._log.warning("telegram.claude_timeout")
            return "Assessment unavailable (timeout)."
        except Exception as exc:
            self._log.warning("telegram.claude_failed", error=str(exc))
            return "Assessment unavailable."

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

    async def send_raw_message(self, text: str) -> None:
        """Send a raw markdown message to Telegram."""
        if not self.trade_alerts_enabled:
            return
        try:
            await self._send(text)
        except Exception as exc:
            self._log.warning("telegram.send_raw_failed", error=str(exc)[:100])

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

    async def send_redeem_alert(self, result: dict) -> None:
        """
        Send a redemption sweep result to Telegram.

        Args:
            result: dict returned by PositionRedeemer.redeem_all()
                    Keys: scanned, redeemed, failed, wins, losses,
                          total_pnl, usdc_before, usdc_after, tx_hashes
        """
        try:
            redeemed = result.get("redeemed", 0)
            failed = result.get("failed", 0)
            wins = result.get("wins", 0)
            losses = result.get("losses", 0)
            scanned = result.get("scanned", 0)
            total_pnl = result.get("total_pnl", 0.0)
            usdc_before = result.get("usdc_before", 0.0)
            usdc_after = result.get("usdc_after", 0.0)
            tx_hashes = result.get("tx_hashes", [])
            usdc_delta = usdc_after - usdc_before

            pnl_sign = "+" if total_pnl >= 0 else ""
            delta_sign = "+" if usdc_delta >= 0 else ""
            result_emoji = "✅" if failed == 0 else "⚠️"

            lines = [
                f"{result_emoji} *Redemption Sweep*",
                f"",
                f"📊 *Summary*",
                f"Scanned: `{scanned}` positions",
                f"Redeemed: `{redeemed}` ✅  Failed: `{failed}` ❌",
                f"Wins: `{wins}` 🏆  Losses cleared: `{losses}` 🗑️",
                f"",
                f"💰 *USDC Balance*",
                f"Before: `${usdc_before:.2f}`",
                f"After:  `${usdc_after:.2f}`",
                f"Change: `{delta_sign}${usdc_delta:.2f}`",
                f"",
                f"📈 *P&L*",
                f"Total: `{pnl_sign}${total_pnl:.2f}`",
            ]

            if tx_hashes:
                lines.append(f"")
                lines.append(f"🔗 *Transactions*")
                for h in tx_hashes[:5]:  # max 5 to avoid message overflow
                    lines.append(f"`{h}`")
                if len(tx_hashes) > 5:
                    lines.append(f"_...and {len(tx_hashes) - 5} more_")

            await self._send("\n".join(lines))
        except Exception as exc:
            self._log.warning("telegram.send_redeem_alert_failed", error=str(exc))

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
        twap_result=None,
        timesfm_forecast=None,
        gamma_bid: float | None = None,
        gamma_ask: float | None = None,
        bankroll: float = 160.0,
        max_bet: float = 32.0,
    ) -> None:
        """
        Enhanced window report with P&L scenarios and clear market prices.

        Shows what would happen if we followed TimesFM vs v5.7c, including realistic
        fill prices from Gamma API and post-fee P&L.

        Args:
            gamma_bid, gamma_ask:  Real Gamma market prices (bid/ask)
            bankroll:              Paper bankroll in USD
            max_bet:               Max bet per trade in USD
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

            regime_emoji = {
                "CASCADE": "🌊",
                "TRANSITION": "🔄",
                "NORMAL": "📊",
                "CALM": "😴",
            }.get(regime, "❓")

            lines = [
                f"🎯 *{asset} {timeframe} Window*",
                f"",
                f"⏰ {ts_str} | Open `${open_price:,.2f}` → Close `${close_price:,.2f}` | Δ `{delta_sign}{delta_pct:+.4f}%`",
                f"",
                f"📊 *Market State*",
                f"VPIN: `{vpin:.4f}` | Regime: {regime_emoji} `{regime}`",
            ]

            # Real Gamma prices (critical info)
            if gamma_bid is not None and gamma_ask is not None:
                gamma_mid = (gamma_bid + gamma_ask) / 2
                gamma_spread = gamma_ask - gamma_bid
                lines += [
                    f"Gamma Prices: *`${gamma_bid:.4f}`* bid / *`${gamma_ask:.4f}`* ask | mid: `${gamma_mid:.4f}` | spread: `${gamma_spread:.4f}`",
                ]
            else:
                lines += [f"Gamma Prices: ❌ unavailable"]
            
            lines.append("")

            # CoinGlass block
            if cg_snapshot is not None:
                lines.append(self.format_coinglass_block(cg_snapshot))
                lines.append("")

            # UNIFIED INDICATORS SECTION
            lines.append("🔮 *Indicators (What Each Means)*")
            lines.append("")

            # TWAP/VPIN/Point indicators
            if twap_result is not None:
                _twap_dir = getattr(twap_result, 'twap_direction', '—')
                _gamma_dir = getattr(twap_result, 'gamma_direction', '—')
                _point_dir = getattr(twap_result, 'point_direction', '—')
                _mom_dir = getattr(twap_result, 'momentum_direction', '—')
                _twap_delta = getattr(twap_result, 'twap_delta_pct', 0)
                _mom_pct = getattr(twap_result, 'momentum_pct', 0)
                _trend_pct = getattr(twap_result, 'trend_pct', 0.5)
                _agree_score = getattr(twap_result, 'agreement_score', 0)
                _agree_max = 3
                _agree_bar = "🟢" * _agree_score + "⚫" * (_agree_max - _agree_score)
                _gamma_up = getattr(twap_result, 'gamma_up_price', 0.50)
                _gamma_down = getattr(twap_result, 'gamma_down_price', 0.50)
                _gamma_gate = getattr(twap_result, 'gamma_gate', 'OK')
                _boost = getattr(twap_result, 'confidence_boost', 0)
                _ticks = getattr(twap_result, 'n_ticks', 0)
                _should_skip = getattr(twap_result, 'should_skip', False)

                _gate_emoji = {"BLOCK": "🚫", "SKIP": "⚠️", "REDUCE": "🔻", "OK": "✅", "PRICED_IN": "💸"}.get(_gamma_gate, "❓")
                _trend_filled = int(_trend_pct * 10)
                _trend_bar = "▓" * _trend_filled + "░" * (10 - _trend_filled)

                lines += [
                    f"✓ VPIN `{vpin:.4f}` → {regime_emoji} *{regime}* — [Shows informed trading activity level]",
                    f"✓ TWAP Δ `{_twap_delta:+.4f}%` → `{_twap_dir}` — [Average price moved this much in window]",
                    f"✓ Point Δ `{delta_pct:+.4f}%` → `{_point_dir}` — [Last trade vs window open]",
                    f"✓ Momentum `{_mom_pct:+.3f}%` → `{_mom_dir}` (30s) — [Is price accelerating?]",
                    f"✓ Trend `[{_trend_bar}]` `{_trend_pct:.0%}` above open — [How much of window was bullish?]",
                    f"✓ Gamma `{_gamma_up:.2f}`/`{_gamma_down:.2f}` → `{_gamma_dir}` {_gate_emoji} `{_gamma_gate}` — [Market maker fair prices]",
                    f"✓ Agreement {_agree_bar} `{_agree_score}/3` | Boost `{_boost:+.2f}` — [How many signals agree?]",
                    f"",
                ]

                if _should_skip:
                    _skip_reason = getattr(twap_result, 'skip_reason', '')
                    lines.append(f"⛔ *v5.7c (multi-indicator) → SKIP* — `{_skip_reason}`")
            else:
                lines.append(f"✓ v5.7c: not evaluated")
            
            lines.append("")

            # TimesFM block — only for v6.0 strategy reporting
            if timesfm_forecast is not None:
                _tfm = timesfm_forecast
                if not getattr(_tfm, 'error', ''):
                    _tfm_dir = getattr(_tfm, 'direction', '?')
                    _tfm_conf = getattr(_tfm, 'confidence', 0)
                    _tfm_close = getattr(_tfm, 'predicted_close', 0)
                    _tfm_delta = getattr(_tfm, 'delta_vs_open_pct', 0)
                    _tfm_dir_emoji = "📈" if _tfm_dir == "UP" else "📉"

                    lines += [
                        f"🧠 *TimesFM ML Forecast* — `{_tfm_dir}` | Confidence `{_tfm_conf*100:.0f}%` — [Neural net predicts close price]",
                        f"   Predicted close: `${_tfm_close:,.2f}` (Δ `{_tfm_delta:+.4f}%`)",
                        f"",
                    ]

            # P&L SCENARIOS SECTION
            lines.append("💡 *What If We Followed Each Strategy?*")
            lines.append("")

            # TimesFM P&L scenario
            if timesfm_forecast is not None and not getattr(_tfm, 'error', ''):
                _tfm_dir = getattr(_tfm, 'direction', '?')
                _tfm_conf = getattr(_tfm, 'confidence', 0)
                _tfm_close = getattr(_tfm, 'predicted_close', 0)

                if gamma_mid := ((gamma_bid or 0.50) + (gamma_ask or 0.50)) / 2:
                    _entry_price = gamma_mid
                    _exit_price = _tfm_close
                    
                    # Assuming binary: if direction is DOWN, bet on NO (price goes down); if UP, bet on YES
                    if _tfm_dir == "DOWN":
                        # Bet NO, entry at ask (gamma_ask), exit if price falls to _tfm_close
                        _raw_pnl = ((_entry_price - _exit_price) * max_bet)
                        _fee_pct = 0.02  # ~2% fee on win
                        _net_pnl = _raw_pnl * (1 - _fee_pct)
                    elif _tfm_dir == "UP":
                        # Bet YES, entry at bid (gamma_bid), exit if price rises to _tfm_close
                        _raw_pnl = ((_exit_price - _entry_price) * max_bet)
                        _fee_pct = 0.02
                        _net_pnl = _raw_pnl * (1 - _fee_pct)
                    else:
                        _raw_pnl = 0
                        _net_pnl = 0

                    _dir_label = "NO" if _tfm_dir == "DOWN" else "YES"
                    _pnl_emoji = "📈" if _net_pnl > 0 else ("📉" if _net_pnl < 0 else "➖")
                    
                    lines += [
                        f"*TimesFM-Only Strategy*",
                        f"  → Bet `{_dir_label}` at `${_entry_price:.4f}` (Gamma mid)",
                        f"  → Exit at `${_exit_price:.4f}` (predicted close)",
                        f"  → Raw P&L: {_pnl_emoji} `${_raw_pnl:+.2f}` | After 2% fee: `${_net_pnl:+.2f}`",
                        f"",
                    ]

            # v5.7c P&L scenario
            if twap_result is not None and not _should_skip:
                _v57_dir = _gamma_dir  # Use gamma direction as the signal
                _v57_conf = 0.90  # Assumption: high confidence if all agree

                if gamma_mid := ((gamma_bid or 0.50) + (gamma_ask or 0.50)) / 2:
                    _entry_price = gamma_bid if _v57_dir == "UP" else gamma_ask
                    
                    # What would be a realistic exit? Use close price as proxy
                    _exit_price = close_price if close_price > 0 else _entry_price

                    if _v57_dir == "DOWN":
                        _raw_pnl = ((_entry_price - _exit_price) * max_bet)
                        _fee_pct = 0.02
                        _net_pnl = _raw_pnl * (1 - _fee_pct)
                    elif _v57_dir == "UP":
                        _raw_pnl = ((_exit_price - _entry_price) * max_bet)
                        _fee_pct = 0.02
                        _net_pnl = _raw_pnl * (1 - _fee_pct)
                    else:
                        _raw_pnl = 0
                        _net_pnl = 0

                    _dir_label = "NO" if _v57_dir == "DOWN" else "YES"
                    _pnl_emoji = "📈" if _net_pnl > 0 else ("📉" if _net_pnl < 0 else "➖")

                    lines += [
                        f"*v5.7c (Multi-Indicator) Strategy*",
                        f"  → Bet `{_dir_label}` at `${_entry_price:.4f}` (Gamma {('bid' if _v57_dir == 'UP' else 'ask')})",
                        f"  → Realistic exit: `${_exit_price:.4f}` (window close)",
                        f"  → Raw P&L: {_pnl_emoji} `${_raw_pnl:+.2f}` | After 2% fee: `${_net_pnl:+.2f}`",
                        f"",
                    ]

            # DECISION SECTION
            lines += [
                f"⚡ *Decision*",
                f"Trade placed: `{'YES ✅' if trade_placed else 'NO ⏭️'}`",
            ]
            
            if not trade_placed and skip_reason:
                lines.append(f"Reason: `{skip_reason}`")

            lines.append(f"Bankroll: `${bankroll:.2f}` | Max bet: `${max_bet:.2f}`")

            await self._send("\n".join(lines))
        except Exception as exc:
            self._log.warning("telegram.send_window_report_failed", error=str(exc))

    async def send_timesfm_window_report(
        self,
        window_ts: int,
        asset: str,
        timeframe: str,
        open_price: float,
        close_price: float,
        delta_pct: float,
        forecast=None,
        twap_result=None,
        trade_placed: bool = False,
        skip_reason: str | None = None,
        spread_data: dict | None = None,
    ) -> None:
        """
        v6.0 window report — TimesFM-only strategy with full comparison data.

        Shows:
        1. TimesFM forecast (direction, confidence, predicted close)
        2. TWAP / Point / Gamma comparison (for analysis)
        3. Real spread/liquidity data
        4. Agreement matrix
        """
        try:
            from datetime import datetime, timezone
            ts_str = datetime.fromtimestamp(window_ts, tz=timezone.utc).strftime("%H:%M UTC")

            delta_sign = "+" if delta_pct >= 0 else ""
            trade_emoji = "✅" if trade_placed else "⏭️"

            lines = [
                f"🤖 *v6.0 TimesFM Window — {asset} {timeframe}*",
                f"",
                f"⏰ `{ts_str}` | Open: `${open_price:,.2f}` → Close: `${close_price:,.2f}`",
                f"Point Δ: `{delta_sign}{delta_pct:.4f}%`",
                f"",
            ]

            # ── TimesFM Forecast Block ────────────────────────────────
            if forecast and not forecast.error:
                _tf_dir = forecast.direction
                _tf_conf = forecast.confidence
                _tf_close = forecast.predicted_close
                _tf_delta = forecast.delta_vs_open_pct
                _tf_spread = forecast.spread
                _tf_p10 = getattr(forecast, 'p10', 0)
                _tf_p50 = getattr(forecast, 'p50', 0)
                _tf_p90 = getattr(forecast, 'p90', 0)
                _tf_latency = getattr(forecast, 'fetch_latency_ms', 0)
                _tf_stale = getattr(forecast, 'is_stale', False)

                _conf_bar_filled = int(_tf_conf * 10)
                _conf_bar = "█" * _conf_bar_filled + "░" * (10 - _conf_bar_filled)
                _dir_emoji = "📈" if _tf_dir == "UP" else "📉"
                _stale_tag = " ⚠️ STALE" if _tf_stale else ""

                lines += [
                    f"🧠 *TimesFM Forecast*{_stale_tag}",
                    f"Direction: {_dir_emoji} `{_tf_dir}` | Confidence: `[{_conf_bar}]` `{_tf_conf:.2f}`",
                    f"Predicted Close: `${_tf_close:,.2f}` (δ `{_tf_delta:+.4f}%` vs open)",
                    f"P10: `${_tf_p10:,.2f}` | P50: `${_tf_p50:,.2f}` | P90: `${_tf_p90:,.2f}`",
                    f"Spread: `${_tf_spread:.2f}` | Latency: `{_tf_latency:.0f}ms`",
                    f"",
                ]
            elif forecast and forecast.error:
                lines += [
                    f"🧠 *TimesFM*: ❌ `{forecast.error[:80]}`",
                    f"",
                ]

            # ── Direction Agreement Matrix ─────────────────────────────
            point_dir = "UP" if delta_pct > 0 else "DOWN"
            timesfm_dir = forecast.direction if forecast and not forecast.error else "?"
            twap_dir = getattr(twap_result, 'twap_direction', '?') if twap_result else "?"
            gamma_dir = getattr(twap_result, 'gamma_direction', '?') if twap_result else "?"

            def _agree_emoji(a, b):
                if a == "?" or b == "?":
                    return "⚫"
                return "🟢" if a == b else "🔴"

            lines += [
                f"📊 *Direction Agreement*",
                f"```",
                f"TimesFM:  {timesfm_dir:>4}  {_agree_emoji(timesfm_dir, point_dir)} Point  {_agree_emoji(timesfm_dir, twap_dir)} TWAP  {_agree_emoji(timesfm_dir, gamma_dir)} Gamma",
                f"Point:    {point_dir:>4}  {'──':>6}  {_agree_emoji(point_dir, twap_dir)} TWAP  {_agree_emoji(point_dir, gamma_dir)} Gamma",
                f"TWAP:     {twap_dir:>4}  {'──':>6}  {'──':>6}  {_agree_emoji(twap_dir, gamma_dir)} Gamma",
                f"Gamma:    {gamma_dir:>4}",
                f"```",
                f"",
            ]

            # ── TWAP Block (comparison) ────────────────────────────────
            if twap_result:
                _agree_score = getattr(twap_result, 'agreement_score', 0)
                _twap_delta = getattr(twap_result, 'twap_delta_pct', 0)
                _trend_pct = getattr(twap_result, 'trend_pct', 0.5)
                _stability = getattr(twap_result, 'twap_stability', 0)
                _ticks = getattr(twap_result, 'n_ticks', 0)
                _gamma_up = getattr(twap_result, 'gamma_up_price', 0.50)
                _gamma_down = getattr(twap_result, 'gamma_down_price', 0.50)
                _gamma_gate = getattr(twap_result, 'gamma_gate', 'OK')
                _gate_emoji = {"BLOCK": "🚫", "SKIP": "⚠️", "REDUCE": "🔻", "OK": "✅", "PRICED_IN": "💸"}.get(_gamma_gate, "❓")

                lines += [
                    f"📐 *TWAP/Gamma (comparison only)*",
                    f"TWAP δ: `{_twap_delta:+.4f}%` → `{twap_dir}` ({_ticks} ticks)",
                    f"Gamma: `{_gamma_up:.2f}`/`{_gamma_down:.2f}` → `{gamma_dir}` {_gate_emoji}`{_gamma_gate}`",
                    f"Trend: `{_trend_pct:.0%}` above open | Stability: `{_stability:.2f}`",
                    f"",
                ]

            # ── Spread/Liquidity Block ─────────────────────────────────
            if spread_data:
                _bid = spread_data.get("best_bid", 0)
                _ask = spread_data.get("best_ask", 0)
                _spread = spread_data.get("spread", 0)
                _mid = spread_data.get("mid_price", 0)
                _vol = spread_data.get("volume", 0)
                _liq = spread_data.get("liquidity", 0)

                lines += [
                    f"💧 *Market Spread/Liquidity*",
                    f"Best Bid: `${_bid:.4f}` | Best Ask: `${_ask:.4f}`",
                    f"Spread: `${_spread:.4f}` ({_spread/_mid*100:.2f}%)" if _mid > 0 else f"Spread: `${_spread:.4f}`",
                    f"Mid Price: `${_mid:.4f}`",
                ]
                if _vol > 0:
                    lines.append(f"Volume: `${_vol:,.0f}` | Liquidity: `${_liq:,.0f}`")
                lines.append(f"")
            else:
                lines += [
                    f"💧 *Spread*: ❌ No orderbook data",
                    f"",
                ]

            # ── Trade Decision ─────────────────────────────────────────
            lines += [
                f"{trade_emoji} Trade placed: `{'YES' if trade_placed else 'NO'}` (v6.0 TimesFM-only)",
            ]
            if not trade_placed and skip_reason:
                lines.append(f"Skip: `{skip_reason}`")

            await self._send("\n".join(lines))
        except Exception as exc:
            self._log.warning("telegram.send_timesfm_window_report_failed", error=str(exc))

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
