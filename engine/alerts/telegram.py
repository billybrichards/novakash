"""
Telegram Alerter — v2

Sends trading alerts to Telegram via Bot API (aiohttp).

Design principles:
  - Value-first: outcome + direction + P&L readable in one glance
  - Skim-optimised: emoji-driven, no code blocks for primary info
  - No duplication: one window report, one trade report, one resolution
  - Rich evaluator: Claude gets full context (regime, TWAP trend, streak, daily P&L)
  - Charts: sparkline PNG sent via sendPhoto for trade entries/exits

Alert types:
  window_report    — every 5-min window (trade or skip), clean summary
  trade_resolved   — when outcome known (WIN/LOSS), includes AI assessment + chart
  cascade_alert    — liquidation cascade signal
  system_alert     — engine status / errors
  kill_switch      — emergency stop
  redeem_alert     — redemption sweep result
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional
import aiohttp
import structlog

if TYPE_CHECKING:
    from data.models import CascadeSignal
    from execution.order_manager import Order

log = structlog.get_logger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_PHOTO_BASE = "https://api.telegram.org/bot{token}/sendPhoto"

_DIR_EMOJI = {"UP": "📈", "DOWN": "📉", "YES": "📈", "NO": "📉"}
_REGIME_EMOJI = {"CASCADE": "🌊", "TRANSITION": "🔄", "NORMAL": "📊", "CALM": "😴", "TIMESFM_ONLY": "⚫"}
_GATE_EMOJI = {"BLOCK": "🚫", "SKIP": "⚠️", "REDUCE": "🔻", "OK": "✅", "PRICED_IN": "💸"}


def _now_utc() -> str:
    return datetime.now(tz=timezone.utc).strftime("%H:%M UTC")


def _ts_str(window_ts: int) -> str:
    return datetime.fromtimestamp(window_ts, tz=timezone.utc).strftime("%H:%M UTC")


def _agree_bar(n: int, total: int = 3) -> str:
    """🟢🟢⚫ style agreement bar."""
    return "🟢" * n + "⚫" * (total - n)


class TelegramAlerter:
    """
    Unified Telegram alerter for the Novakash trading engine.

    Constructor args:
      bot_token       — Telegram bot token
      chat_id         — Telegram chat/user ID
      anthropic_api_key — Claude API key for AI assessments (preferred over os.environ)
      alerts_paper    — send alerts for paper trades (default: True)
      alerts_live     — send alerts for live trades (default: False)
      paper_mode      — current mode flag
      risk_manager    — reference for bankroll/P&L data
      poly_client     — reference for wallet balance
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        anthropic_api_key: str = "",
        alerts_paper: bool = True,
        alerts_live: bool = False,
        paper_mode: bool = True,
        risk_manager=None,
        poly_client=None,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._url = TELEGRAM_API_BASE.format(token=bot_token)
        self._photo_url = TELEGRAM_PHOTO_BASE.format(token=bot_token)
        self._alerts_paper = alerts_paper
        self._alerts_live = alerts_live
        self._paper_mode = paper_mode
        self._risk_manager = risk_manager
        self._poly_client = poly_client

        # Prefer passed-in key, fall back to env, then settings
        self._anthropic_api_key = (
            anthropic_api_key
            or os.environ.get("ANTHROPIC_API_KEY", "")
            or ""
        )

        # Try to get from pydantic settings if not set
        if not self._anthropic_api_key:
            try:
                from config.settings import settings
                self._anthropic_api_key = settings.anthropic_api_key or ""
            except Exception:
                pass

        self._log = log.bind(component="TelegramAlerter")

        if not bot_token or not chat_id:
            self._log.warning("telegram.not_configured")
        else:
            self._log.info(
                "telegram.configured",
                paper_mode=paper_mode,
                has_claude=bool(self._anthropic_api_key),
            )

    def set_risk_manager(self, rm) -> None:
        self._risk_manager = rm

    def set_poly_client(self, pc) -> None:
        self._poly_client = pc

    @property
    def trade_alerts_enabled(self) -> bool:
        return self._alerts_paper if self._paper_mode else self._alerts_live

    # ── Location / identity ────────────────────────────────────────────────────

    _location: str = "MTL"
    _engine_version: str = "v7.1"
    _db_client = None  # injected after construction

    def set_location(self, location: str, version: str = "v7.1") -> None:
        self._location = location
        self._engine_version = version

    def set_db_client(self, db) -> None:
        """Inject DB client for notification logging."""
        self._db_client = db

    def _footer(self, window_id: Optional[str] = None) -> str:
        parts = [f"📍 {self._location}", self._engine_version, self._mode_tag()]
        if window_id:
            parts.insert(1, f"`{window_id}`")
        return "  ".join(parts)

    async def _log_notification(
        self,
        notification_type: str,
        message_text: str,
        window_id: Optional[str] = None,
        has_chart: bool = False,
        telegram_message_id: Optional[int] = None,
    ) -> None:
        """Persist every sent notification to telegram_notifications table."""
        if not self._db_client:
            return
        try:
            from sqlalchemy import text as _text
            async with self._db_client._pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO telegram_notifications
                       (bot_id, location, window_id, notification_type,
                        message_text, has_chart, engine_version, telegram_message_id)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                    "novakash", self._location, window_id, notification_type,
                    message_text[:4000], has_chart, self._engine_version,
                    telegram_message_id,
                )
        except Exception as exc:
            self._log.debug("telegram.log_notification_failed", error=str(exc)[:80])

    # ── Window lifecycle notifications ─────────────────────────────────────────

    async def send_window_open(
        self,
        window_id: str,
        asset: str,
        timeframe: str,
        open_price: float,
        gamma_up: float,
        gamma_down: float,
    ) -> None:
        """Sent once when a new window opens."""
        mode = self._mode_tag()
        g_skew = "BALANCED" if abs(gamma_up - gamma_down) < 0.03 else ("UP leaning" if gamma_up > gamma_down else "DOWN leaning")
        text = (
            f"🪟 *WINDOW OPEN — {asset} {timeframe}*  {mode}\n"
            f"`{window_id}`\n"
            f"\n"
            f"Open: `${open_price:,.2f}`\n"
            f"Gamma: UP `${gamma_up:.3f}` / DOWN `${gamma_down:.3f}`  `{g_skew}`\n"
            f"\n"
            f"{self._footer(window_id)}"
        )
        msg_id = await self._send_with_id(text)
        await self._log_notification("window_open", text, window_id, telegram_message_id=msg_id)

    async def send_window_snapshot(
        self,
        window_id: str,
        t_label: str,
        elapsed_s: int,
        price_ticks: list,
        open_price: float,
        current_price: float,
        delta_pct: float,
        vpin: float,
        vpin_regime: str,
        twap_direction: Optional[str],
        twap_agreement: int,
        timesfm_direction: Optional[str],
        timesfm_confidence: float,
        timesfm_predicted: float,
        gamma_up: float,
        gamma_down: float,
        cg_taker_buy_pct: float = 50.0,
        cg_funding_annual: float = 0.0,
        entry_prices: Optional[dict] = None,
        stake_usd: float = 4.0,
        ai_commentary: Optional[str] = None,
    ) -> None:
        """Send snapshot chart + text card at T-240/T-180/T-120/T-90."""
        from alerts.window_chart import window_snapshot_chart

        delta_sign = "+" if delta_pct >= 0 else ""
        dirs = [d for d in [twap_direction, timesfm_direction,
                             "UP" if delta_pct > 0 else "DOWN"] if d]
        conflict = len(set(dirs)) > 1

        caption_lines = [
            f"⏱ *{t_label}* — {window_id}",
            f"",
            f"{'▲' if delta_pct > 0 else '▼'} `{delta_sign}{delta_pct:.4f}%`  |  VPIN `{vpin:.3f}` `{vpin_regime}`",
        ]
        if twap_direction:
            caption_lines.append(f"TWAP `{twap_direction}` {twap_agreement}/3  |  Gamma UP `${gamma_up:.3f}` / DN `${gamma_down:.3f}`")
        if timesfm_direction:
            caption_lines.append(f"TimesFM `{timesfm_direction}` {timesfm_confidence:.0%}")
        if conflict:
            caption_lines.append(f"⚠ SIGNAL CONFLICT")
        if ai_commentary:
            caption_lines.append(f"")
            caption_lines.append(f"🤖 _{ai_commentary}_")
        caption_lines.append(f"")
        caption_lines.append(self._footer(window_id))

        caption = "\n".join(caption_lines)

        # Generate chart
        chart_bytes = window_snapshot_chart(
            price_ticks=price_ticks or [open_price, current_price],
            open_price=open_price,
            current_price=current_price,
            window_id=window_id,
            t_label=t_label,
            elapsed_s=elapsed_s,
            vpin=vpin,
            vpin_regime=vpin_regime,
            twap_direction=twap_direction,
            twap_agreement=twap_agreement,
            timesfm_direction=timesfm_direction,
            timesfm_confidence=timesfm_confidence,
            timesfm_predicted=timesfm_predicted,
            gamma_up=gamma_up,
            gamma_down=gamma_down,
            delta_pct=delta_pct,
            cg_taker_buy_pct=cg_taker_buy_pct,
            cg_funding_annual=cg_funding_annual,
            entry_prices=entry_prices or {},
            stake_usd=stake_usd,
            location=self._location,
            engine_version=self._engine_version,
        )

        if chart_bytes:
            msg_id = await self._send_photo_with_id(chart_bytes, caption)
        else:
            # Fallback to text only
            msg_id = await self._send_with_id(caption)
        await self._log_notification(
            f"snapshot_{t_label}", caption, window_id,
            has_chart=bool(chart_bytes), telegram_message_id=msg_id,
        )

    async def send_window_resolution(
        self,
        window_id: str,
        asset: str,
        timeframe: str,
        outcome: str,                   # "WIN" or "LOSS"
        direction: str,                 # "UP" or "DOWN" (our bet)
        actual_direction: str,          # "UP" or "DOWN" (what happened)
        entry_price: float,             # actual token entry price
        pnl_usd: float,
        open_price: float,
        close_price: float,
        delta_pct: float,
        vpin: float,
        regime: str,
        twap_result=None,
        # What-if at each T-point
        entry_prices: Optional[dict] = None,  # {"T-240": 0.48, ...}
        stake_usd: float = 4.0,
        win_streak: int = 0,
        loss_streak: int = 0,
        ai_commentary: Optional[str] = None,
    ) -> None:
        """Full resolution report with what-if P&L table at each T-point."""
        result_e = "✅" if outcome == "WIN" else "❌"
        arrow = "▲" if actual_direction == "UP" else "▼"
        correct = actual_direction == direction
        confirm = "✓" if correct else "✗"
        pnl_sign = "+" if pnl_usd >= 0 else ""
        streak_str = (f"  Streak: `{win_streak}W`" if win_streak > 0
                      else f"  Streak: `{loss_streak}L`" if loss_streak > 0 else "")

        lines = [
            f"{result_e} *{outcome} — {asset} {timeframe}*  {self._mode_tag()}",
            f"`{window_id}`",
            f"",
            f"{arrow} {direction} @ `${entry_price:.3f}` → resolved {actual_direction} {confirm}",
            f"P&L: `{pnl_sign}${pnl_usd:.2f}`{streak_str}",
            f"",
            f"*What-if P&L at each entry point:*",
        ]

        # What-if table
        ep = entry_prices or {}
        for label in ["T-240", "T-180", "T-120", "T-90", "T-60"]:
            ep_price = ep.get(label)
            if not ep_price:
                continue
            fee = 0.035 * min(ep_price, 1 - ep_price)
            shares = stake_usd / ep_price
            net_win = shares * (1 - fee) - stake_usd
            net_loss = -stake_usd
            actual_pnl = net_win if correct else net_loss
            pnl_e = "✅" if actual_pnl > 0 else "❌"
            actual_sign = "+" if actual_pnl > 0 else ""
            marker = " ← actual" if label == "T-60" else ""
            lines.append(f"`{label}`  `${ep_price:.3f}`  →  `{actual_sign}${actual_pnl:.2f}` {pnl_e}{marker}")

        if ai_commentary:
            lines += ["", f"🤖 _{ai_commentary}_"]

        pf = self._portfolio_line()
        if pf:
            lines += ["", pf]
        lines += ["", self._footer(window_id)]

        text = "\n".join(lines)
        msg_id = await self._send_with_id(text)
        await self._log_notification(
            "resolution", text, window_id,
            has_chart=False, telegram_message_id=msg_id,
        )

    # ── Portfolio footer ───────────────────────────────────────────────────────

    def _portfolio_line(self) -> str:
        """One-line bankroll + daily P&L summary."""
        try:
            if not self._risk_manager:
                return ""
            s = self._risk_manager.get_status()
            bankroll = s.get("current_bankroll", 0)
            daily_pnl = s.get("daily_pnl", 0)
            sign = "+" if daily_pnl >= 0 else ""
            return f"💼 ${bankroll:.2f}  📅 {sign}${daily_pnl:.2f} today"
        except Exception:
            return ""

    def _mode_tag(self) -> str:
        return "📄 PAPER" if self._paper_mode else "🔴 LIVE"

    # ── MAIN: Window Report ────────────────────────────────────────────────────

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
        direction: Optional[str] = None,
        trade_placed: bool = False,
        skip_reason: Optional[str] = None,
        # Signal sources
        twap_result=None,
        timesfm_forecast=None,
        # Gamma prices
        gamma_up_price: Optional[float] = None,
        gamma_down_price: Optional[float] = None,
        # Trade details (when placed)
        stake_usd: Optional[float] = None,
        token_price: Optional[float] = None,
        bankroll: float = 160.0,
        # Legacy compat
        cg_snapshot=None,
        cg_modifier: float = 0.0,
        price_source: str = "unknown",
        max_bet: float = 32.0,
        confidence: Optional[float] = None,
    ) -> None:
        """
        Unified window report — sent every 5-min window regardless of trade/skip.

        Format (skim-first):
          ⏭ BTC 5m — 15:30 UTC  📄 PAPER

          📊 -0.05% DOWN  |  VPIN 0.69  🌊 CASCADE

          Point ▼  TWAP ▼  Gamma ▼  3/3 ✓
          TimesFM: ▼ DOWN  53%  $66,805

          ⏭ SKIPPED — delta 0.05% < 0.08%

          💼 $162.40  📅 +$3.92 today
        """
        try:
            mode = self._mode_tag()
            ts = _ts_str(window_ts)
            regime_emoji = _REGIME_EMOJI.get(regime, "❓")

            # Direction & delta
            _dir = direction or ("UP" if delta_pct > 0 else "DOWN")
            _dir_arrow = "▲" if _dir == "UP" else "▼"
            _delta_sign = "+" if delta_pct > 0 else ""
            _delta_str = f"{_delta_sign}{delta_pct:.3f}%"

            # Signal source agreement
            _pt_dir = "UP" if delta_pct > 0 else "DOWN"
            _twap_dir = getattr(twap_result, "twap_direction", None) if twap_result else None
            _gamma_dir = getattr(twap_result, "gamma_direction", None) if twap_result else None
            _tfm_dir = getattr(timesfm_forecast, "direction", None) if timesfm_forecast else None
            _tfm_conf = getattr(timesfm_forecast, "confidence", 0) if timesfm_forecast else 0
            _tfm_close = getattr(timesfm_forecast, "predicted_close", 0) if timesfm_forecast else 0
            _tfm_err = getattr(timesfm_forecast, "error", "") if timesfm_forecast else ""

            # Agreement score (Point + TWAP + Gamma)
            _agree_count = 0
            _agree_parts = []
            for src_name, src_dir in [("Point", _pt_dir), ("TWAP", _twap_dir), ("Gamma", _gamma_dir)]:
                if src_dir:
                    arrow = "▲" if src_dir == "UP" else "▼"
                    _agree_parts.append(f"{src_name} {arrow}")
                    if src_dir == _dir:
                        _agree_count += 1
                else:
                    _agree_parts.append(f"{src_name} ?")
            _agree_bar = _agree_bar_fn(_agree_count)

            # TWAP agreement score (internal)
            _twap_agree_score = getattr(twap_result, "agreement_score", 0) if twap_result else 0
            _gamma_gate = getattr(twap_result, "gamma_gate", "OK") if twap_result else "OK"
            _gate_e = _GATE_EMOJI.get(_gamma_gate, "❓")

            # Gamma prices
            _g_up = gamma_up_price or 0.50
            _g_down = gamma_down_price or 0.50

            lines = [
                f"{'⚡' if trade_placed else '⏭'} *{asset} {timeframe} — {ts}*  {mode}",
                f"",
                f"{_dir_arrow} `{_delta_str}` {_dir}  |  VPIN `{vpin:.3f}`  {regime_emoji} `{regime}`",
                f"",
                f"{'  '.join(_agree_parts)}  {_agree_bar}",
            ]

            # TimesFM line
            if _tfm_dir and not _tfm_err:
                _tf_arrow = "▲" if _tfm_dir == "UP" else "▼"
                _conf_pct = int(_tfm_conf * 100)
                _conf_bar = "█" * int(_tfm_conf * 5) + "░" * (5 - int(_tfm_conf * 5))
                _agrees = "✓" if _tfm_dir == _dir else "✗"
                lines.append(
                    f"TimesFM {_tf_arrow} `{_tfm_dir}`  `[{_conf_bar}]` {_conf_pct}%  ${_tfm_close:,.2f}  {_agrees}"
                )
            elif _tfm_err:
                lines.append(f"TimesFM ⚫ error")
            else:
                lines.append(f"TimesFM ⚫ no data")

            # Gamma prices line
            lines.append(f"Gamma UP `${_g_up:.3f}` / DOWN `${_g_down:.3f}`  {_gate_e} `{_gamma_gate}`")

            lines.append(f"")

            # Trade / skip
            if trade_placed:
                _price = token_price or (_g_down if _dir == "DOWN" else _g_up)
                _stake = stake_usd or 4.0
                _token = "NO" if _dir == "DOWN" else "YES"
                _win = (1.0 - _price) * _stake * 0.98
                lines.append(f"⚡ *PLACED {_token} @ `${_price:.3f}`*")
                lines.append(f"Stake `${_stake:.2f}` → Win `+${_win:.2f}`")
            else:
                _short_reason = (skip_reason or "—")[:80]
                lines.append(f"⏭ *SKIP* — `{_short_reason}`")

            # Portfolio
            pf = self._portfolio_line()
            if pf:
                lines.append(f"")
                lines.append(pf)

            await self._send("\n".join(lines))
        except Exception as exc:
            self._log.warning("telegram.window_report_failed", error=str(exc))

    # ── Trade Resolved ─────────────────────────────────────────────────────────

    async def send_trade_resolved(
        self,
        order: "Order",
        window_ts: int,
        asset: str,
        timeframe: str,
        open_price: float,
        close_price: float,
        delta_pct: float,
        vpin: float,
        regime: str,
        twap_result=None,
        timesfm_forecast=None,
        win_streak: int = 0,
        loss_streak: int = 0,
        price_ticks: Optional[list[float]] = None,
    ) -> None:
        """
        Trade resolved alert — sent when outcome known.

        Format:
          ✅ WIN — BTC 5m  📄 PAPER

          ▼ DOWN @ $0.785 → resolved DOWN ✓
          P&L: +$0.84  |  Streak: 7W

          🤖 Entry assessment (2-3 sentences)

          💼 $163.24  📅 +$4.76 today
        """
        if not self.trade_alerts_enabled:
            return
        try:
            meta = order.metadata or {}
            mode = self._mode_tag()
            outcome = order.outcome or "UNKNOWN"
            result_emoji = "✅" if outcome == "WIN" else "❌"
            pnl = order.pnl_usd or 0

            _dir = "UP" if order.direction == "YES" else "DOWN"
            _dir_arrow = "▲" if _dir == "UP" else "▼"
            tp = float(order.price) if order.price else 0.50
            _token = order.direction  # YES or NO
            pnl_sign = "+" if pnl >= 0 else ""
            streak_str = f"{win_streak}W" if win_streak > 0 else (f"{loss_streak}L" if loss_streak > 0 else "")

            # Did actual close confirm direction?
            _actual_dir = "UP" if close_price > open_price else "DOWN"
            _correct = _actual_dir == _dir
            _confirm = "✓" if _correct else "✗"

            lines = [
                f"{result_emoji} *{outcome} — {asset} {timeframe}*  {mode}",
                f"",
                f"{_dir_arrow} {_dir} @ `${tp:.3f}` → resolved {_actual_dir} {_confirm}",
                f"P&L: `{pnl_sign}${pnl:.2f}`  |  Streak: `{streak_str}`" if streak_str else f"P&L: `{pnl_sign}${pnl:.2f}`",
                f"",
            ]

            # AI assessment
            try:
                assessment = await self._generate_assessment(
                    order=order,
                    asset=asset, timeframe=timeframe,
                    open_price=open_price, close_price=close_price,
                    delta_pct=delta_pct, vpin=vpin, regime=regime,
                    twap_result=twap_result, timesfm_forecast=timesfm_forecast,
                    win_streak=win_streak, loss_streak=loss_streak,
                )
                if assessment:
                    lines.append(f"🤖 _{assessment}_")
                    lines.append(f"")
            except Exception:
                pass

            pf = self._portfolio_line()
            if pf:
                lines.append(pf)

            await self._send("\n".join(lines))

            # Send chart if we have price ticks
            if price_ticks and len(price_ticks) > 5:
                try:
                    from alerts.chart_generator import window_sparkline
                    chart = window_sparkline(
                        prices=price_ticks,
                        open_price=open_price,
                        close_price=close_price,
                        direction=_dir,
                        entry_price=tp,
                        outcome=outcome,
                        asset=asset,
                        timeframe=timeframe,
                        window_ts=window_ts,
                        trade_placed=True,
                    )
                    if chart:
                        caption = f"{result_emoji} {asset} {timeframe} — {pnl_sign}${pnl:.2f}"
                        await self._send_photo(chart, caption)
                except Exception as exc:
                    self._log.debug("telegram.chart_failed", error=str(exc))

        except Exception as exc:
            self._log.warning("telegram.trade_resolved_failed", error=str(exc))

    # Backwards-compat aliases for existing engine code
    async def send_trade_alert(self, order: "Order", cg_snapshot=None) -> None:
        """Legacy alias → send_trade_resolved (called without full context)."""
        if not self.trade_alerts_enabled:
            return
        try:
            meta = order.metadata or {}
            asset = (meta.get("market_slug", "") or "BTC").split("-")[0].upper()
            tf = meta.get("timeframe", "5m")
            open_price = meta.get("window_open_price", 0) or 0
            delta_pct = meta.get("delta_pct", 0) or 0
            close_price = open_price * (1 + delta_pct / 100) if open_price else 0

            await self.send_trade_resolved(
                order=order,
                window_ts=int(meta.get("window_ts", 0) or 0),
                asset=asset,
                timeframe=tf,
                open_price=open_price,
                close_price=close_price,
                delta_pct=delta_pct,
                vpin=meta.get("vpin", 0) or 0,
                regime=meta.get("regime", "UNKNOWN"),
            )
        except Exception as exc:
            self._log.warning("telegram.send_trade_alert_failed", error=str(exc))

    async def send_entry_alert(self, order: "Order") -> None:
        """Legacy alias — entry is now included in window_report, this is a no-op."""
        pass

    # Backwards compat for v6.0 window reports
    async def send_timesfm_window_report(self, **kwargs) -> None:
        """Legacy alias → send_window_report."""
        await self.send_window_report(**{
            k: v for k, v in kwargs.items()
            if k in (
                "window_ts", "asset", "timeframe", "open_price", "close_price",
                "delta_pct", "vpin", "regime", "direction", "trade_placed",
                "skip_reason", "twap_result", "timesfm_forecast",
                "gamma_up_price", "gamma_down_price", "stake_usd", "token_price",
            )
        })

    # ── AI Assessment ──────────────────────────────────────────────────────────

    async def _generate_assessment(
        self,
        order: "Order",
        asset: str,
        timeframe: str,
        open_price: float,
        close_price: float,
        delta_pct: float,
        vpin: float,
        regime: str,
        twap_result=None,
        timesfm_forecast=None,
        win_streak: int = 0,
        loss_streak: int = 0,
    ) -> str:
        """
        Generate 2-sentence trade assessment via Claude.
        Uses settings.anthropic_api_key (loaded from .env) — not os.environ.
        """
        if not self._anthropic_api_key:
            return ""
        try:
            meta = order.metadata or {}
            outcome = order.outcome or "UNKNOWN"
            pnl = order.pnl_usd or 0
            direction = "UP" if order.direction == "YES" else "DOWN"
            tp = float(order.price) if order.price else 0.50
            entry_reason = meta.get("entry_reason_detail") or meta.get("entry_label", "—")

            # TWAP context
            twap_ctx = ""
            if twap_result:
                trend_pct = getattr(twap_result, "trend_pct", 0.5)
                agree_score = getattr(twap_result, "agreement_score", 0)
                gamma_gate = getattr(twap_result, "gamma_gate", "OK")
                twap_ctx = (
                    f"TWAP: {getattr(twap_result, 'twap_direction', '?')} "
                    f"(trend {trend_pct:.0%} above open, {agree_score}/3 agree, gate={gamma_gate}). "
                )

            # TimesFM context
            tfm_ctx = ""
            if timesfm_forecast and not getattr(timesfm_forecast, "error", ""):
                tfm_dir = getattr(timesfm_forecast, "direction", "?")
                tfm_conf = getattr(timesfm_forecast, "confidence", 0)
                tfm_ctx = f"TimesFM predicted {tfm_dir} with {tfm_conf:.0%} confidence. "

            # Streak context
            streak_ctx = ""
            if win_streak >= 3:
                streak_ctx = f"Currently on a {win_streak}-win streak. "
            elif loss_streak >= 2:
                streak_ctx = f"Coming off a {loss_streak}-loss streak. "

            prompt = (
                f"{asset} {timeframe} trade on Polymarket prediction market.\n"
                f"Regime: {regime}  VPIN: {vpin:.3f}  Delta: {delta_pct:+.4f}%\n"
                f"Direction bet: {direction}  Entry price: ${tp:.3f}  "
                f"Open: ${open_price:,.2f}  Close: ${close_price:,.2f}\n"
                f"Outcome: {outcome}  PnL: ${pnl:+.2f}\n"
                f"Entry: {entry_reason}\n"
                f"{twap_ctx}{tfm_ctx}{streak_ctx}"
                f"\nWrite 1-2 tight sentences assessing the entry quality and outcome."
            )

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.anthropic.com/v1/messages",
                    json={
                        "model": "claude-haiku-4-5",
                        "max_tokens": 120,
                        "system": "You are a crypto trading analyst. Be concise and specific.",
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    headers={
                        "x-api-key": self._anthropic_api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        return ""
                    data = await resp.json()
                    return data.get("content", [{}])[0].get("text", "").strip()

        except asyncio.TimeoutError:
            return ""
        except Exception:
            return ""

    # ── Cascade Alert ──────────────────────────────────────────────────────────

    async def send_cascade_alert(self, signal: "CascadeSignal") -> None:
        try:
            state_labels = {
                "CASCADE_DETECTED": "🌊 CASCADE",
                "EXHAUSTING": "🌊 EXHAUSTING",
                "BET_SIGNAL": "🎯 CASCADE SIGNAL",
                "COOLDOWN": "⏳ COOLDOWN",
                "IDLE": "💤 IDLE",
            }
            label = state_labels.get(signal.state, f"🌊 {signal.state}")
            dir_str = f"{_DIR_EMOJI.get(signal.direction or '', '')} `{signal.direction}`" if signal.direction else "`none`"

            lines = [
                f"*{label}*",
                f"Direction: {dir_str}",
                f"VPIN: `{signal.vpin:.3f}`",
                f"OI Δ: `{signal.oi_delta_pct * 100:+.2f}%`",
                f"Liq (5m): `${signal.liq_volume_usd / 1e6:.2f}M`",
            ]
            await self._send("\n".join(lines))
        except Exception as exc:
            self._log.warning("telegram.cascade_alert_failed", error=str(exc))

    # ── System / Admin Alerts ──────────────────────────────────────────────────

    async def send_system_alert(self, message: str, level: str = "info") -> None:
        emoji = {"info": "🟢", "warning": "🟡", "error": "🔴", "critical": "🔴"}.get(level, "🟢")
        try:
            await self._send(f"{emoji} *System*\n`{message}`")
        except Exception:
            pass

    async def send_kill_switch_alert(self) -> None:
        try:
            await self._send("🛑 *KILL SWITCH*\nAll trading halted. Manual restart required.")
        except Exception:
            pass

    async def send_raw_message(self, text: str) -> None:
        """Send a raw markdown message — always sends (bypasses trade_alerts_enabled gate).
        Used for sitreps and system notifications that should always go through."""
        try:
            await self._send(text)
        except Exception:
            pass

    async def send_redeem_alert(self, result: dict) -> None:
        try:
            redeemed = result.get("redeemed", 0)
            failed = result.get("failed", 0)
            wins = result.get("wins", 0)
            losses = result.get("losses", 0)
            total_pnl = result.get("total_pnl", 0.0)
            usdc_before = result.get("usdc_before", 0.0)
            usdc_after = result.get("usdc_after", 0.0)
            usdc_delta = usdc_after - usdc_before
            emoji = "✅" if failed == 0 else "⚠️"
            sign = "+" if total_pnl >= 0 else ""
            d_sign = "+" if usdc_delta >= 0 else ""

            lines = [
                f"{emoji} *Redemption Sweep*",
                f"Redeemed: `{redeemed}`  Failed: `{failed}`",
                f"Wins: `{wins}`  Losses cleared: `{losses}`",
                f"P&L: `{sign}${total_pnl:.2f}`  USDC: `${usdc_before:.2f}` → `${usdc_after:.2f}` (`{d_sign}${usdc_delta:.2f}`)",
            ]
            tx_hashes = result.get("tx_hashes", [])
            if tx_hashes:
                lines.append(f"Tx: `{tx_hashes[0]}`")
                if len(tx_hashes) > 1:
                    lines.append(f"_+{len(tx_hashes) - 1} more_")

            await self._send("\n".join(lines))
        except Exception as exc:
            self._log.warning("telegram.redeem_alert_failed", error=str(exc))

    async def send_daily_chart(self, windows: list[dict], date_str: str = "") -> None:
        """Send daily P&L curve chart."""
        try:
            from alerts.chart_generator import daily_pnl_curve
            chart = daily_pnl_curve(windows, date_str)
            if chart:
                await self._send_photo(chart, f"📊 Daily P&L — {date_str or _now_utc()}")
        except Exception as exc:
            self._log.debug("telegram.daily_chart_failed", error=str(exc))

    # ── Legacy coinglass format ────────────────────────────────────────────────

    def format_coinglass_block(self, snapshot) -> str:
        """Legacy helper — kept for compat with old orchestrator code."""
        try:
            if not snapshot or not snapshot.connected:
                return "🔬 CoinGlass: ❌"
            cg = snapshot
            oi_b = cg.oi_usd / 1e9
            liq_m = cg.liq_total_usd_1m / 1e6
            return (
                f"🔬 CoinGlass: OI `${oi_b:.1f}B` (Δ`{cg.oi_delta_pct_1m:+.2f}%`) "
                f"Liq `${liq_m:.1f}M` L/S `{cg.long_short_ratio:.2f}`"
            )
        except Exception:
            return "🔬 CoinGlass: ⚠️"

    # ── Internal send helpers ──────────────────────────────────────────────────

    async def _send_with_id(self, text: str) -> Optional[int]:
        """Send text and return Telegram message_id (for logging)."""
        if not self._bot_token or not self._chat_id:
            return None
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("result", {}).get("message_id")
                    body = await resp.text()
                    if "can't parse entities" in body:
                        plain = dict(payload)
                        del plain["parse_mode"]
                        async with session.post(self._url, json=plain,
                                                timeout=aiohttp.ClientTimeout(total=10)) as r2:
                            if r2.status == 200:
                                d2 = await r2.json()
                                return d2.get("result", {}).get("message_id")
        except Exception as exc:
            self._log.warning("telegram.send_error", error=str(exc))
        return None

    async def _send_photo_with_id(self, photo_bytes: bytes, caption: str = "") -> Optional[int]:
        """Send photo and return Telegram message_id."""
        if not self._bot_token or not self._chat_id or not photo_bytes:
            return None
        try:
            form = aiohttp.FormData()
            form.add_field("chat_id", str(self._chat_id))
            form.add_field("photo", photo_bytes, content_type="image/png", filename="chart.png")
            if caption:
                form.add_field("caption", caption[:1024])
                form.add_field("parse_mode", "Markdown")
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._photo_url, data=form,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("result", {}).get("message_id")
                    body = await resp.text()
                    self._log.warning("telegram.photo_failed", status=resp.status, body=body[:100])
        except Exception as exc:
            self._log.warning("telegram.photo_error", error=str(exc))
        return None

    async def _send(self, text: str) -> None:
        if not self._bot_token or not self._chat_id:
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
                    self._url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        if "can't parse entities" in body:
                            # Retry without markdown
                            plain = {**payload}
                            del plain["parse_mode"]
                            async with session.post(
                                self._url, json=plain,
                                timeout=aiohttp.ClientTimeout(total=10),
                            ) as r2:
                                if r2.status != 200:
                                    self._log.warning("telegram.send_failed", status=r2.status)
                        else:
                            self._log.warning("telegram.api_error", status=resp.status, body=body[:200])
        except Exception as exc:
            self._log.warning("telegram.send_error", error=str(exc))

    async def _send_photo(self, photo_bytes: bytes, caption: str = "") -> None:
        """Send a PNG chart via Telegram sendPhoto."""
        if not self._bot_token or not self._chat_id or not photo_bytes:
            return
        try:
            form = aiohttp.FormData()
            form.add_field("chat_id", str(self._chat_id))
            form.add_field("photo", photo_bytes, content_type="image/png", filename="chart.png")
            if caption:
                form.add_field("caption", caption[:1024])
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._photo_url, data=form,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        self._log.warning("telegram.photo_failed", status=resp.status, body=body[:200])
        except Exception as exc:
            self._log.warning("telegram.photo_error", error=str(exc))


# Helper (module-level to avoid closure issues)
def _agree_bar_fn(n: int, total: int = 3) -> str:
    return "🟢" * n + "⚫" * (total - n)
