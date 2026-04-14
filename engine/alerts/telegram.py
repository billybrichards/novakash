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
_REGIME_EMOJI = {
    "CASCADE": "🌊",
    "TRANSITION": "🔄",
    "NORMAL": "📊",
    "CALM": "😴",
    "TIMESFM_ONLY": "⚫",
}
_GATE_EMOJI = {
    "BLOCK": "🚫",
    "SKIP": "⚠️",
    "REDUCE": "🔻",
    "OK": "✅",
    "PRICED_IN": "💸",
}


def _format_v103_block(signal: dict) -> str:
    """Build v10.3 gate/CG/threshold lines for Telegram alerts.

    Returns empty string when v10.3 data is absent (backward compatible).
    Designed for mobile readability — short lines, emoji-driven scanning.
    """
    lines = []

    # ── DUNE threshold breakdown ──
    threshold = signal.get("v103_threshold")
    dune_p = signal.get("v103_dune_p")
    if threshold is not None and dune_p is not None:
        passed = dune_p >= threshold
        icon = "✅" if passed else "❌"
        direction = signal.get("direction", "?")
        lines.append(
            f"🔮 DUNE: P({direction})=`{dune_p:.3f}` {'≥' if passed else '<'} `{threshold:.3f}` {icon}"
        )

        # Threshold components
        parts = []
        base = signal.get("v103_threshold_base")
        if base is not None:
            parts.append(f"{base:.2f}")
        offset_p = signal.get("v103_offset_penalty", 0)
        if offset_p > 0:
            parts.append(f"+{offset_p:.3f}off")
        down_p = signal.get("v103_down_penalty", 0)
        if down_p > 0:
            parts.append(f"+{down_p:.2f}dwn")
        cg_mod = signal.get("v103_cg_modifier", 0)
        if cg_mod > 0:
            parts.append(f"+{cg_mod:.2f}cg")
        elif cg_mod < 0:
            parts.append(f"{cg_mod:.2f}cg")
        cg_bonus = signal.get("v103_cg_bonus", 0)
        if cg_bonus > 0:
            parts.append(f"-{cg_bonus:.2f}cgb")
        elif cg_bonus < 0:
            parts.append(f"+{abs(cg_bonus):.2f}cgp")  # penalty (negative bonus)
        if parts:
            lines.append(f"📐 {' '.join(parts)} = {threshold:.3f}")

    # ── Taker flow alignment ──
    taker_status = signal.get("v103_taker_status")
    if taker_status:
        buy_pct = signal.get("v103_taker_buy_pct", 0)
        sell_pct = signal.get("v103_taker_sell_pct", 0)
        icon_map = {
            "aligned": "✅",
            "opposing": "⚠️",
            "both_opposing": "🚫",
            "neutral": "➖",
        }
        icon = icon_map.get(taker_status, "?")
        lines.append(
            f"🌊 Taker: `{taker_status.upper()}` {icon} buy {buy_pct:.0f}% sell {sell_pct:.0f}%"
        )

    # ── CG confirmation dots ──
    confirms = signal.get("v103_cg_confirms")
    if confirms is not None:
        dots = "🟢" * confirms + "⚫" * (3 - confirms)
        details = signal.get("v103_cg_details", [])
        detail_str = ", ".join(details[:3]) if details else "none"
        bonus = signal.get("v103_cg_bonus", 0)
        if bonus > 0:
            bonus_str = f" → -{bonus:.2f}"
        elif bonus < 0:
            bonus_str = f" → +{abs(bonus):.2f} penalty"
        else:
            bonus_str = ""
        lines.append(f"{dots} CG {confirms}/3 ({detail_str}){bonus_str}")

    # ── Spread ──
    spread = signal.get("v103_spread_pct")
    if spread is not None:
        icon = "✅" if spread <= 8 else "⚠️"
        lines.append(f"📏 Spread: `{spread:.1f}%` {icon}")

    return "\n".join(lines)


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
            anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "") or ""
        )

        # Try to get from pydantic settings if not set
        if not self._anthropic_api_key:
            try:
                from config.settings import settings

                self._anthropic_api_key = settings.anthropic_api_key or ""
            except Exception:
                pass

        self._log = log.bind(component="TelegramAlerter")

        # Dual AI system: Claude primary, Qwen122b fallback
        self._ai = DualAIAssessment(
            anthropic_key=self._anthropic_api_key,
            qwen_host=os.environ.get("QWEN_HOST", "ollama-ssh1"),
            qwen_port=int(os.environ.get("QWEN_PORT", "11434")),
            log=self._log,
        )

        if not bot_token or not chat_id:
            self._log.warning("telegram.not_configured")
        else:
            self._log.info(
                "telegram.configured",
                paper_mode=paper_mode,
                has_claude=bool(self._anthropic_api_key),
            )

    # ── Trade Decision + AI Analysis (Dual-AI) ────────────────────────────────
    async def send_trade_decision_detailed(
        self,
        window_id: str,
        signal: dict,
        decision: str,
        reason: str = "",
        gamma_up: float = None,
        gamma_down: float = None,
        consensus_live: Optional[int] = None,
        consensus_total: Optional[int] = None,
        macro_status: Optional[str] = None,
        sequoia_ok: Optional[bool] = None,
        strategy_decisions: Optional[list] = None,
    ) -> tuple:
        """v11.2 Window Evaluation Card — TRADE or SKIP with full source attribution.

        Optional data health params (added v11.2):
            consensus_live: Number of live consensus sources
            consensus_total: Total configured consensus sources
            macro_status: "OK" or "FALLBACK"
            sequoia_ok: Whether Sequoia model is responding
            strategy_decisions: List of StrategyDecision from StrategyPort evaluation
        """
        from datetime import datetime, timezone

        window_time = "?"
        try:
            ts = int(window_id.split("-")[1])
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            window_time = dt.strftime("%H:%M UTC")
        except Exception:
            pass

        direction = signal.get("direction", "?")
        delta = signal.get("delta_pct", 0)
        vpin = signal.get("vpin", 0)
        regime = signal.get("regime", "?")
        mode = self._mode_tag()

        # v8.0 multi-source data
        delta_source = signal.get("delta_source", "?")
        delta_tiingo = signal.get("delta_tiingo")
        delta_binance = signal.get("delta_binance")
        delta_chainlink = signal.get("delta_chainlink")
        tiingo_close = signal.get("tiingo_close")
        chainlink_price = signal.get("chainlink_price")
        binance_price = signal.get("binance_price")
        gates_passed = signal.get("gates_passed", "")
        gate_failed = signal.get("gate_failed")
        confidence_tier = signal.get("confidence_tier", "?")
        macro_bias = signal.get("macro_bias", "N/A")
        macro_confidence = signal.get("macro_confidence", "")

        # Build source prices line
        prices = []
        if tiingo_close:
            prices.append(f"TI=${tiingo_close:,.0f}")
        if chainlink_price:
            prices.append(f"CL=${chainlink_price:,.0f}")
        if binance_price:
            prices.append(f"BN=${binance_price:,.0f}")
        prices_line = " | ".join(prices) if prices else "N/A"

        # Build gates line — only show signal gates (VPIN, DELTA, CG)
        # Floor/Cap from Gamma are indicative only, not real CLOB book
        gate_icons = ""
        for g in ["vpin", "delta", "cg"]:
            if gate_failed and g == gate_failed:
                gate_icons += f"❌{g.upper()} "
            elif g in (gates_passed or ""):
                gate_icons += f"✅{g.upper()} "
        if not gate_icons:
            gate_icons = "N/A"

        # Real CLOB book price + Gamma indicative
        entry_line = ""
        clob_up = signal.get("clob_up_ask")
        clob_dn = signal.get("clob_down_ask")
        if clob_up or clob_dn:
            _clob_entry = clob_dn if direction in ("DOWN", "NO") else clob_up
            if _clob_entry and _clob_entry < 10:  # sanity check
                _clob_rr = (1 - _clob_entry) / _clob_entry if _clob_entry > 0 else 0
                entry_line = f"📊 CLOB: ↑`${clob_up:.3f}` ↓`${clob_dn:.3f}`"
                if 0.30 <= _clob_entry <= 0.73:
                    entry_line += f" | R/R `1:{_clob_rr:.1f}`\n"
                elif _clob_entry > 0.73:
                    entry_line += f" ⛔ >`$0.73`\n"
                else:
                    entry_line += f" ⛔ <`$0.30`\n"
            else:
                entry_line = f"📊 CLOB: no book\n"
        elif gamma_up is not None and gamma_down is not None:
            entry = gamma_down if direction in ("DOWN", "NO") else gamma_up
            rr = (1 - entry) / entry if entry > 0 else 0
            entry_line = f"💱 Gamma: `${entry:.3f}` R/R `1:{rr:.1f}` _(indicative)_\n"

        # Delta display with source
        delta_str = f"{delta:+.4f}%" if delta else "?"
        src_short = (
            delta_source.replace("_rest_candle", "")
            .replace("_db_tick", "(db)")
            .replace("_fallback", "(fb)")
        )

        emoji = "🎯" if decision == "TRADE" else "⏭"

        # v8.1 early entry info
        _eval_offset = signal.get("eval_offset")
        _entry_reason = signal.get("entry_reason", "v8_standard")
        _v2_p = signal.get("v2_probability_up")
        _v2_dir = signal.get("v2_direction")
        _v2_agrees = signal.get("v2_agrees")
        _v81_cap = signal.get("v81_entry_cap")

        v81_line = ""
        if _v2_p is not None:
            _v2_conf = "HIGH" if (_v2_p > 0.65 or _v2_p < 0.35) else "LOW"
            _agree_icon = "✅" if _v2_agrees else "❌"
            v81_line = (
                f"🔮 v2.2: P(UP)=`{_v2_p:.3f}` → `{_v2_dir}` {_agree_icon} "
                f"({_v2_conf})\n"
            )

        offset_line = ""
        if _eval_offset and _eval_offset != 60:
            offset_line = (
                f"⏱ Entry: `T-{_eval_offset}s` | cap `${_v81_cap:.2f}`\n"
                if _v81_cap
                else f"⏱ Entry: `T-{_eval_offset}s`\n"
            )

        # v10.3: build CG/DUNE/threshold block if available
        v103_block = _format_v103_block(signal)
        v103_section = f"\n{v103_block}\n" if v103_block else ""

        # v10.3 pipeline gate status (replaces old vpin/delta/cg icons)
        _gate_results = signal.get("v103_gate_results")
        if _gate_results:
            gate_icons = " ".join(
                f"{'✅' if g.get('passed') else '❌'}{g.get('name', '?')[:6]}"
                for g in _gate_results
            )

        _version_tag = "v10.3" if v103_block else self._engine_version

        # v11.2: data health line
        health_line = ""
        if consensus_live is not None and consensus_total is not None:
            _macro_tag = f"Macro: `{macro_status or 'OK'}`"
            _seq_tag = (
                f"Sequoia: {'✓' if sequoia_ok else '✗'}"
                if sequoia_ok is not None
                else ""
            )
            _health_parts = [
                f"Consensus `{consensus_live}/{consensus_total}`",
                _macro_tag,
            ]
            if _seq_tag:
                _health_parts.append(_seq_tag)
            health_line = f"📊 Health: {' | '.join(_health_parts)}\n"

        decision_text = (
            f"{emoji} *{decision}* — BTC 5m | {window_time} | {_version_tag}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Signal: `{direction}` | {src_short} Δ `{delta_str}`\n"
            f"📈 VPIN: `{vpin:.3f}` | `{regime}`\n"
            f"🔗 {prices_line}\n"
            f"{entry_line}"
            f"{v103_section}"
            f"{v81_line}"
            f"{offset_line}"
            f"{health_line}"
            f"\n⚡ Gates: {gate_icons}\n"
            f"🎖 Confidence: `{confidence_tier}`\n"
        )

        # v12: Strategy Port comparison (V10 LIVE vs V4 GHOST)
        if strategy_decisions:
            decision_text += "\n"
            for sd in strategy_decisions:
                _mode = (
                    getattr(sd, "mode", None) or sd.get("mode", "?")
                    if isinstance(sd, dict)
                    else "?"
                )
                _sid = getattr(sd, "strategy_id", None) or (
                    sd.get("strategy_id", "?") if isinstance(sd, dict) else "?"
                )
                _action = getattr(sd, "action", None) or (
                    sd.get("action", "?") if isinstance(sd, dict) else "?"
                )
                _skip = getattr(sd, "skip_reason", None) or (
                    sd.get("skip_reason") if isinstance(sd, dict) else None
                )
                _dir = getattr(sd, "direction", None) or (
                    sd.get("direction") if isinstance(sd, dict) else None
                )
                _conf = getattr(sd, "confidence", None) or (
                    sd.get("confidence") if isinstance(sd, dict) else None
                )
                _icon = "🎯" if _mode == "LIVE" else "👻"
                _label = _sid.upper().replace("_", " ")
                if _action == "TRADE":
                    _detail = f"`{_dir}` ({_conf or '?'})"
                elif _action == "SKIP":
                    _detail = f"SKIP — _{(_skip or 'no reason')[:60]}_"
                else:
                    _detail = f"`{_action}` — _{(_skip or '')[:60]}_"
                decision_text += f"{_icon} {_label} ({_mode}): {_detail}\n"

        if decision == "SKIP" and reason:
            decision_text += f"\n❌ _{reason[:200]}_\n"

        if not self._paper_mode and decision == "TRADE":
            decision_text += f"\n🟢 *ORDER SENT → awaiting fill*  {mode}\n"

        decision_msg_id = await self._send_with_id(decision_text)
        await self._log_notification(
            "trade_decision_v8",
            decision_text,
            f"{window_id}",
            telegram_message_id=decision_msg_id,
        )

        # AI analysis (shorter in v8.0 — 1 sentence max)
        analysis_msg_id = None
        if decision == "TRADE":
            try:
                prompt = (
                    f"BTC 5m {direction} trade. Tiingo Δ{delta_str}, VPIN {vpin:.2f} ({regime}). "
                    f"Source: {delta_source}. 1 sentence: win probability + key risk."
                )
                ai_text, ai_source = await self._ai.assess(prompt, timeout_s=6)
                analysis_card = f"🤖 `{ai_source.upper()}` — _{ai_text[:300]}_"
                analysis_msg_id = await self._send_with_id(analysis_card)
            except Exception as exc:
                self._log.warning("ai.decision_analysis_failed", error=str(exc)[:100])

        return decision_msg_id, analysis_msg_id

    # ── Consolidated Window Summary (replaces 19 individual skip alerts) ────────

    async def send_window_summary(
        self,
        window_id: str,
        eval_history: list,
        traded: bool = False,
        trade_offset: int = None,
        strategy_decisions: Optional[list] = None,
    ) -> Optional[int]:
        """
        Send one consolidated summary card per window instead of one alert per eval tick.

        If traded=False: shows ALL SKIPPED with grouped skip reasons.
        If traded=True:  shows TRADE at trade_offset with condensed skip summary.

        Optional (added v12):
            strategy_decisions: List of StrategyDecision objects from StrategyPort.
        """
        if not eval_history:
            return None

        # Parse window time from window_id (format: "BTC-1712345678")
        window_time = "?"
        asset = "BTC"
        try:
            parts = window_id.split("-", 1)
            asset = parts[0]
            ts = int(parts[1])
            window_time = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                "%H:%M UTC"
            )
        except Exception:
            pass

        n_evals = len(eval_history)

        # Use data from the most recent (lowest offset) entry for headline metrics
        latest = sorted(eval_history, key=lambda x: x.get("offset") or 9999)[0]
        vpin = latest.get("vpin") or 0.0
        delta_pct = latest.get("delta_pct") or 0.0
        regime = latest.get("regime") or "?"
        v2_p = latest.get("v2_p")
        v2_dir = latest.get("v2_dir") or "?"
        v2_agrees = latest.get("v2_agrees")
        confidence = latest.get("confidence") or "?"

        # v9.0 source agreement line (replaces v2.2 line)
        _cl_dir = latest.get("cl_dir")
        _ti_dir = latest.get("ti_dir")
        _delta_cl = latest.get("delta_chainlink")
        _delta_ti = latest.get("delta_tiingo")

        if _cl_dir and _ti_dir:
            _agree = _cl_dir == _ti_dir
            _agree_icon = "✅ AGREE" if _agree else "❌ DISAGREE"
            _cl_delta_s = f"Δ{_delta_cl:+.3f}%" if _delta_cl else ""
            _ti_delta_s = f"Δ{_delta_ti:+.3f}%" if _delta_ti else ""
            source_line = f"🔗 CL: `{_cl_dir}` {_cl_delta_s} | TI: `{_ti_dir}` {_ti_delta_s} — {_agree_icon}\n"
        elif v2_p is not None:
            # Fallback to v2.2 if no CL/TI data
            _v2_pct = int(round(v2_p * 100))
            _agree_icon = "✅ AGREE" if v2_agrees else "❌ DISAGREE"
            source_line = f"🔮 v2.2: `{v2_dir}` `{_v2_pct}%` | v8: `{('UP' if delta_pct > 0 else 'DOWN')}` — {_agree_icon}\n"
        else:
            source_line = ""

        regime_emoji = {
            "CASCADE": "🌊",
            "TRANSITION": "🔄",
            "NORMAL": "📊",
            "CALM": "😴",
        }.get(regime, "📊")
        delta_str = f"{delta_pct:+.4f}%" if delta_pct else "?"

        # v9 tier info
        _v9_tier = latest.get("v9_tier")
        _v9_cap = latest.get("v9_cap")
        tier_line = ""
        if _v9_tier:
            _cap_s = f"${_v9_cap:.2f}" if _v9_cap else "—"
            tier_line = f"📐 Tier: `{_v9_tier}` | Cap: `{_cap_s}`\n"

        # ── Group consecutive skip reasons ────────────────────────────────────
        def _group_reasons(history: list) -> list[str]:
            """Collapse consecutive same-reason entries into T-X..T-Y ranges."""
            if not history:
                return []
            # Sort by offset descending (T-240 first)
            sorted_h = sorted(history, key=lambda x: x.get("offset") or 0, reverse=True)
            groups = []
            current_reason = None
            current_start = None
            current_end = None
            for entry in sorted_h:
                offset = entry.get("offset")
                raw_reason = entry.get("skip_reason") or "unknown"
                # Normalise reason for grouping: strip offset-specific details
                # e.g. "v8.1: not CASCADE (VPIN 0.612 < 0.65) at T-240" → "not CASCADE"
                reason_key = raw_reason
                if " at T-" in reason_key:
                    reason_key = reason_key[: reason_key.rfind(" at T-")].strip()
                # Also normalise CLOB values: "CLOB CAP: UP ask $0.57 > $0.55" → "CLOB cap"
                if "CLOB CAP" in reason_key.upper():
                    # Extract the ask price for display
                    try:
                        _ask_part = reason_key.split("ask $")[1].split(" >")[0]
                        _cap_part = (
                            reason_key.split("> $")[1].split()[0]
                            if "> $" in reason_key
                            else "?"
                        )
                        reason_key = f"CLOB cap (${_ask_part} > ${_cap_part})"
                    except Exception:
                        reason_key = "CLOB cap"
                elif "CLOB FLOOR" in reason_key.upper():
                    reason_key = "CLOB floor"
                # Shorten common patterns
                elif "v8.1: not CASCADE" in reason_key:
                    _vpin_val = None
                    try:
                        _vpin_val = reason_key.split("VPIN ")[1].split(")")[0]
                    except Exception:
                        pass
                    reason_key = (
                        f"not CASCADE (VPIN {_vpin_val})"
                        if _vpin_val
                        else "not CASCADE"
                    )
                elif "v8.1: delta too weak" in reason_key:
                    reason_key = "delta too weak"
                elif "v2.2 DISAGREES" in reason_key:
                    reason_key = "v2.2 disagrees"
                elif "v2.2 LOW conf" in reason_key:
                    reason_key = "v2.2 low conf"
                elif "VPIN" in reason_key and "< gate" in reason_key:
                    reason_key = "VPIN below gate"
                elif "TWAP GATE" in reason_key:
                    reason_key = "TWAP gate"
                elif "CG VETO" in reason_key.upper():
                    reason_key = "CG veto"
                # v9.0 skip reasons
                elif "v9: CL=" in reason_key and "DISAGREE" in reason_key:
                    # Extract directions: "v9: CL=DOWN TI=UP DISAGREE"
                    try:
                        _cl = reason_key.split("CL=")[1].split(" ")[0]
                        _ti = reason_key.split("TI=")[1].split(" ")[0]
                        reason_key = f"CL={_cl} TI={_ti} DISAGREE"
                    except Exception:
                        reason_key = "source disagree"
                elif "v9: early offset" in reason_key:
                    try:
                        _vp = reason_key.split("VPIN ")[1].split(" ")[0]
                        reason_key = f"v9: early VPIN {_vp} < 0.65"
                    except Exception:
                        reason_key = "v9: early VPIN too low"
                elif "v9: golden zone" in reason_key:
                    try:
                        _vp = reason_key.split("VPIN ")[1].split(" ")[0]
                        reason_key = f"v9: VPIN {_vp} < 0.45"
                    except Exception:
                        reason_key = "v9: golden VPIN too low"
                elif "Gates passed but signal None" in reason_key:
                    reason_key = "signal None"

                if reason_key != current_reason:
                    if current_reason is not None:
                        if current_start == current_end:
                            groups.append(f"T-{current_start}: {current_reason}")
                        else:
                            groups.append(
                                f"T-{current_start}..T-{current_end}: {current_reason}"
                            )
                    current_reason = reason_key
                    current_start = offset
                    current_end = offset
                else:
                    current_end = offset

            if current_reason is not None:
                if current_start == current_end:
                    groups.append(f"T-{current_start}: {current_reason}")
                else:
                    groups.append(
                        f"T-{current_start}..T-{current_end}: {current_reason}"
                    )
            return groups

        reason_groups = _group_reasons(eval_history)

        # ── Format the card ───────────────────────────────────────────────────
        # v10.3: extract CG data from latest eval entry
        _v103_taker = latest.get("v103_taker_status")
        _v103_confirms = latest.get("v103_cg_confirms")
        v103_line = ""
        if _v103_taker:
            _icon_map = {
                "aligned": "✅",
                "opposing": "⚠️",
                "both_opposing": "🚫",
                "neutral": "➖",
            }
            _taker_icon = _icon_map.get(_v103_taker, "?")
            _buy_pct = latest.get("v103_taker_buy_pct", 0)
            _sell_pct = latest.get("v103_taker_sell_pct", 0)
            v103_line = f"🌊 Taker: `{_v103_taker.upper()}` {_taker_icon} buy {_buy_pct:.0f}% sell {_sell_pct:.0f}%\n"
        if _v103_confirms is not None:
            _dots = "🟢" * _v103_confirms + "⚫" * (3 - _v103_confirms)
            _details = latest.get("v103_cg_details", [])
            _detail_s = ", ".join(_details[:3]) if _details else "none"
            v103_line += f"{_dots} CG {_v103_confirms}/3 ({_detail_s})\n"

        _version_tag = "v10.3" if _v103_taker else "v9.0"

        if not traded:
            # ALL SKIPPED card
            skip_reasons_text = (
                "\n".join(f"  {r}" for r in reason_groups)
                if reason_groups
                else "  (unknown)"
            )
            msg = (
                f"📋 *{asset} 5m* | {window_time} | {_version_tag}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⏭ `{n_evals}` evals — *ALL SKIPPED*\n"
                f"{source_line}"
                f"{v103_line}"
                f"{tier_line}"
                f"📈 VPIN: `{vpin:.3f}` {regime_emoji} `{regime}` | Δ `{delta_str}`\n"
                f"\n*Skip reasons:*\n{skip_reasons_text}\n"
            )
        else:
            # TRADED card with condensed skip summary
            n_skipped = n_evals
            skip_reasons_compact = ", ".join(reason_groups) if reason_groups else "none"
            trade_line = f"🎯 FAK at T-{trade_offset}" if trade_offset else "🎯 TRADE"
            msg = (
                f"📋 *{asset} 5m* | {window_time} | {_version_tag}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{trade_line} (after `{n_skipped}` skips)\n"
                f"{source_line}"
                f"{v103_line}"
                f"{tier_line}"
                f"📈 VPIN: `{vpin:.3f}` {regime_emoji} `{regime}` | Δ `{delta_str}`\n"
                f"\n_Prior skips: {skip_reasons_compact}_\n"
            )

        # v12: Strategy Port comparison block (V10 LIVE vs V4 GHOST)
        if strategy_decisions:
            msg += "\n"
            for sd in strategy_decisions:
                _mode = getattr(sd, "mode", None) or (
                    sd.get("mode", "?") if isinstance(sd, dict) else "?"
                )
                _sid = getattr(sd, "strategy_id", None) or (
                    sd.get("strategy_id", "?") if isinstance(sd, dict) else "?"
                )
                _action = getattr(sd, "action", None) or (
                    sd.get("action", "?") if isinstance(sd, dict) else "?"
                )
                _skip = getattr(sd, "skip_reason", None) or (
                    sd.get("skip_reason") if isinstance(sd, dict) else None
                )
                _dir = getattr(sd, "direction", None) or (
                    sd.get("direction") if isinstance(sd, dict) else None
                )
                _conf = getattr(sd, "confidence", None) or (
                    sd.get("confidence") if isinstance(sd, dict) else None
                )
                _icon = "🎯" if _mode == "LIVE" else "👻"
                _label = _sid.upper().replace("_", " ")
                if _action == "TRADE":
                    _detail = f"`{_dir}` ({_conf or '?'})"
                elif _action == "SKIP":
                    _detail = f"SKIP — _{(_skip or 'no reason')[:60]}_"
                else:
                    _detail = f"`{_action}` — _{(_skip or '')[:60]}_"
                msg += f"{_icon} {_label} ({_mode}): {_detail}\n"

        msg_id = await self._send_with_id(msg)
        await self._log_notification(
            "window_summary", msg, window_id, telegram_message_id=msg_id
        )
        return msg_id

    # ── Clean 5-Stage Lifecycle Notifications ─────────────────────────────────

    async def send_signal_snapshot(
        self,
        window_id: str,
        data: dict,
        ai_text: Optional[str] = None,
        sequoia_p_up: Optional[float] = None,
        regime_hmm: Optional[str] = None,
        regime_hmm_confidence: Optional[float] = None,
    ) -> Optional[int]:
        """
        ① 📊 SIGNAL (T-90) — Combined market snapshot + AI analysis.

        data keys: vpin, delta_pct, regime, gamma_up, gamma_down,
                   timesfm_direction, timesfm_confidence,
                   twap_direction, twap_agreement, btc_price, window_time

        Optional V4 surface params (added v11.2):
            sequoia_p_up: Sequoia v5.2 P(UP) probability
            regime_hmm: HMM regime state (calm_trend/volatile_trend/chop/risk_off)
            regime_hmm_confidence: HMM regime confidence [0-1]
        """
        try:
            window_time = data.get("window_time", "?")
            if not window_time or window_time == "?":
                try:
                    ts = int(window_id.split("-")[1])
                    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                    window_time = dt.strftime("%H:%M")
                except Exception:
                    pass

            vpin = data.get("vpin", 0)
            delta = data.get("delta_pct", 0)
            regime = data.get("regime", "?")
            gamma_up = data.get("gamma_up", 0.50)
            gamma_down = data.get("gamma_down", 0.50)
            tsf_dir = data.get("timesfm_direction", "?")
            tsf_conf = data.get("timesfm_confidence", 0)
            twap_dir = data.get("twap_direction", "?")
            twap_agree = data.get("twap_agreement", 0)
            btc = data.get("btc_price", 0)
            mode = self._mode_tag()

            regime_emoji = _REGIME_EMOJI.get(regime, "📊")
            dir_arrow = "▲" if delta > 0 else "▼"
            implied_dir = "UP" if delta > 0 else "DOWN"

            lines = [
                f"📊 *SIGNAL — BTC window {window_time} UTC*  {mode}",
                f"`{window_id}`",
                f"",
                f"{dir_arrow} `{delta:+.4f}%`  |  VPIN `{vpin:.3f}` {regime_emoji} `{regime}`",
                f"Gamma ↑`${gamma_up:.3f}` ↓`${gamma_down:.3f}`",
                f"TimesFM `{tsf_dir}` `{tsf_conf:.0%}` conf  |  TWAP `{twap_dir}` `{twap_agree}/3`",
            ]
            if btc:
                lines.append(f"BTC: `${btc:,.2f}`")
            # v11.2: Sequoia + HMM regime
            if sequoia_p_up is not None:
                _seq_dir = "UP" if sequoia_p_up >= 0.5 else "DOWN"
                lines.append(
                    f"🔮 Sequoia v5.2: P(UP)=`{sequoia_p_up:.3f}` → `{_seq_dir}`"
                )
            if regime_hmm:
                _hmm_display = regime_hmm.replace("_", " ").title()
                _hmm_conf = (
                    f" `{regime_hmm_confidence:.0%}`"
                    if regime_hmm_confidence is not None
                    else ""
                )
                lines.append(f"🧠 HMM: `{_hmm_display}`{_hmm_conf}")
            if ai_text:
                lines += ["", f"🤖 _{ai_text}_"]
            lines += ["", self._footer(window_id)]

            text = "\n".join(lines)
            msg_id = await self._send_with_id(text)
            await self._log_notification(
                "signal_snapshot", text, window_id, telegram_message_id=msg_id
            )
            return msg_id
        except Exception as exc:
            self._log.warning(
                "telegram.send_signal_snapshot_failed", error=str(exc)[:100]
            )
            return None

    async def send_trade_decision(
        self,
        window_id: str,
        signal: dict,
        gamma: dict,
        ai_text: Optional[str] = None,
        prev_gamma_up: Optional[float] = None,
        prev_gamma_down: Optional[float] = None,
    ) -> Optional[int]:
        """
        ② 🎯 DECISION (T-70) — TRADE or SKIP with reason, gamma, R/R, AI analysis.

        signal keys: direction, delta_pct, vpin, regime, decision, reason
        gamma keys: gamma_up, gamma_down
        prev_gamma_up/down: Gamma prices recorded at T-90 for comparison
        """
        try:
            window_time = "?"
            try:
                ts = int(window_id.split("-")[1])
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                window_time = dt.strftime("%H:%M UTC")
            except Exception:
                pass

            direction = signal.get("direction", "?")
            delta = signal.get("delta_pct", 0)
            vpin = signal.get("vpin", 0)
            regime = signal.get("regime", "?")
            decision = signal.get("decision", "SKIP")
            reason = signal.get("reason", "")
            gamma_up = gamma.get("gamma_up", 0.50)
            gamma_down = gamma.get("gamma_down", 0.50)
            mode = self._mode_tag()

            entry = gamma_down if direction in ("DOWN", "NO") else gamma_up
            rr = round((1 - entry) / entry, 1) if entry > 0 else 0
            d_emoji = "🎯" if decision == "TRADE" else "⏭"

            lines = [
                f"{d_emoji} *{decision} — {window_time}*  {mode}",
                f"`{window_id}`",
                f"",
                f"Direction: `{direction}` | δ `{delta:+.4f}%` | VPIN `{vpin:.3f}` `{regime}`",
                f"Gamma ↑`${gamma_up:.3f}` ↓`${gamma_down:.3f}`",
            ]

            # Show gamma movement vs T-90 if available
            if prev_gamma_up is not None and prev_gamma_down is not None:
                prev_entry = (
                    prev_gamma_down if direction in ("DOWN", "NO") else prev_gamma_up
                )
                arrow = "↑" if entry > prev_entry else "↓"
                lines.append(
                    f"Gamma moved `${prev_entry:.3f}`→`${entry:.3f}` since T-90 {arrow}"
                )

            if decision == "TRADE":
                lines.append(
                    f"R/R `1:{rr}` | Entry `${entry:.3f}` | Will place at `${entry + 0.02:.3f}`"
                )

            if reason:
                lines.append(f"_{reason}_")

            if ai_text:
                lines += ["", f"🤖 _{ai_text}_"]

            if decision == "TRADE" and not self._paper_mode:
                lines.insert(2, "⚠️ *REAL MONEY ORDER WILL BE PLACED*")

            lines += ["", self._footer(window_id)]

            text = "\n".join(lines)
            msg_id = await self._send_with_id(text)
            await self._log_notification(
                "trade_decision", text, window_id, telegram_message_id=msg_id
            )
            return msg_id
        except Exception as exc:
            self._log.warning(
                "telegram.send_trade_decision_failed", error=str(exc)[:100]
            )
            return None

    async def send_order_filled(
        self,
        order,
        fill_price: float,
        shares: float,
        gamma_at_fill: Optional[dict] = None,
        gamma_at_decision: Optional[dict] = None,
        ai_text: Optional[str] = None,
    ) -> Optional[int]:
        """
        ③ 💰 FILLED — On CLOB MATCHED: shares, fill price, cost, gamma comparison, AI.

        order: Order object with order_id, direction, stake_usd
        gamma_at_fill: {"gamma_up": x, "gamma_down": y}
        gamma_at_decision: Gamma at T-70 for comparison
        """
        try:
            direction = "DOWN" if getattr(order, "direction", "") == "NO" else "UP"
            stake = getattr(order, "stake_usd", 0)
            cost = fill_price * shares
            rr = round((1 - fill_price) / fill_price, 1) if fill_price > 0 else 0
            profit_if_win = round((1 - fill_price) * shares * 0.98, 2)

            # v8.0 FOK metadata
            fok_step = getattr(order, "fok_fill_step", None)
            fok_attempts = getattr(order, "fok_attempts", None)
            delta_source = getattr(order, "delta_source", "?")
            src_short = (
                delta_source.replace("_rest_candle", "").replace("_db_tick", "(db)")
                if delta_source
                else "?"
            )

            fok_line = ""
            if fok_step is not None and fok_attempts is not None:
                fok_line = f"⚡ FOK step `{fok_step}/{fok_attempts}`\n"

            text = (
                f"💰 *FILLED* — BTC 5m {direction} | {self._engine_version}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Fill: `${fill_price:.4f}` × `{shares:.2f}` shares\n"
                f"Cost: `${cost:.2f}` | R/R `1:{rr}`\n"
                f"If WIN: `+${profit_if_win:.2f}`\n"
                f"{fok_line}"
                f"Source: `{src_short}` | Mode: `{'gtc' if not fok_step else 'fok'}`\n"
            )
            msg_id = await self._send_with_id(text)
            await self._log_notification(
                "order_filled", text, telegram_message_id=msg_id
            )
            return msg_id
        except Exception as exc:
            self._log.warning("telegram.send_order_filled_failed", error=str(exc)[:100])
            return None

    async def send_trade_result(
        self,
        order,
        outcome: str,
        pnl: float,
        ai_text: Optional[str] = None,
    ) -> Optional[int]:
        """
        ④ ✅❌ RESULT — WIN/LOSS from Polymarket oracle, P&L, AI post-trade analysis.

        NEVER uses Binance for resolution — outcome must come from Polymarket.
        """
        try:
            mode = self._mode_tag()
            meta = getattr(order, "metadata", {}) or {}
            direction = "DOWN" if getattr(order, "direction", "") == "NO" else "UP"
            entry_price = float(getattr(order, "price", 0) or 0)
            window_id = f"{meta.get('asset', 'BTC')}-{int(meta.get('window_ts', 0))}"
            stake = getattr(order, "stake_usd", 0)

            result_emoji = "✅" if outcome == "WIN" else "❌"
            pnl_sign = "+" if pnl >= 0 else ""

            lines = [
                f"{result_emoji} *{outcome} — Polymarket Resolution*  {mode}",
                f"`{window_id}`",
                f"",
                f"Direction: `{direction}` @ `${entry_price:.3f}`",
                f"P&L: `{pnl_sign}${pnl:.2f}` | Stake: `${stake:.2f}`",
            ]

            # Portfolio summary
            pf = self._portfolio_line()
            if pf:
                lines += ["", pf]

            if ai_text:
                lines += ["", f"🤖 _{ai_text}_"]

            lines += ["", self._footer(window_id)]

            text = "\n".join(lines)
            msg_id = await self._send_with_id(text)
            await self._log_notification(
                "trade_result", text, window_id, telegram_message_id=msg_id
            )
            return msg_id
        except Exception as exc:
            self._log.warning("telegram.send_trade_result_failed", error=str(exc)[:100])
            return None

    async def send_post_resolution_analysis(
        self,
        window_id: str,
        oracle_direction: str,
        eval_ticks: list,
        ai_analysis: str,
        missed_profit: float = 0.0,
        blocked_loss: float = 0.0,
        cap_too_tight: bool = False,
    ) -> Optional[int]:
        """
        🔬 POST-RESOLUTION — Full AI analysis of all skipped window ticks.

        Format:
            🔬 POST-RESOLUTION — BTC 5m | 07:15 UTC | v8.0
            ━━━━━━━━━━━━━━━━━━━━━━
            Oracle: DOWN ✅
            Evaluated: 19 ticks | Traded: 0 | Skipped: 19

            Missed profits:
            T-240 skip (CLOB $0.52 > cap $0.55): would WIN +$4.41
            T-210 skip (not CASCADE): would WIN +$3.20
            T-110 skip (v2.2 disagrees): would LOSE -$8.50

            🤖 Sonnet: "Cap at T-240 was $0.03 too tight..."
        """
        try:
            # Parse window time
            window_time = "?"
            asset = "BTC"
            try:
                parts = window_id.split("-", 1)
                asset = parts[0]
                ts = int(parts[1])
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                window_time = dt.strftime("%H:%M UTC")
            except Exception:
                pass

            n_ticks = len(eval_ticks)
            n_traded = sum(1 for t in eval_ticks if t.get("decision") == "TRADE")
            n_skipped = n_ticks - n_traded

            # Oracle emoji: would our signal have been correct?
            # We show ✅ if ANY tick would have won (missed opportunity)
            # and ❌ if all were correctly blocked
            n_would_win = sum(1 for t in eval_ticks if t.get("would_win") is True)
            oracle_emoji = "✅" if n_would_win > 0 else "✓"

            # Build tick breakdown lines (only show meaningful ones)
            tick_lines = []
            for t in eval_ticks:
                if t.get("pnl_label") and "no CLOB price" not in t.get("pnl_label", ""):
                    offset = t.get("offset", "?")
                    skip_reason = (t.get("skip_reason") or "unknown")[:40]
                    clob = t.get("clob_ask")
                    pnl_label = t.get("pnl_label", "")
                    clob_str = f" CLOB ${clob:.2f}" if clob is not None else ""
                    tick_lines.append(
                        f"T-{offset} ({skip_reason}{clob_str}): {pnl_label}"
                    )

            # Limit to 6 most informative lines to keep message readable
            tick_lines = tick_lines[:6]

            cap_warning = " ⚠️ CAP TOO TIGHT" if cap_too_tight else ""

            pnl_summary = ""
            if missed_profit > 0:
                pnl_summary += f"Missed: `+${missed_profit:.2f}`"
            if blocked_loss > 0:
                sep = " | " if pnl_summary else ""
                pnl_summary += f"{sep}Avoided: `-${blocked_loss:.2f}`"

            text = (
                f"🔬 *POST-RESOLUTION — {asset} 5m | {window_time} | {self._engine_version}*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Oracle: `{oracle_direction}` {oracle_emoji}{cap_warning}\n"
                f"Evaluated: `{n_ticks}` ticks | Traded: `{n_traded}` | Skipped: `{n_skipped}`\n"
            )

            if pnl_summary:
                text += f"{pnl_summary}\n"

            if tick_lines:
                text += f"\n*Tick breakdown:*\n"
                text += "\n".join(f"  _{line}_" for line in tick_lines) + "\n"

            if ai_analysis:
                # Truncate to ~250 chars for Telegram readability
                ai_snippet = ai_analysis[:250].strip()
                if len(ai_analysis) > 250:
                    ai_snippet += "…"
                text += f'\n🤖 _Sonnet: "{ai_snippet}"_\n'

            msg_id = await self._send_with_id(text)
            await self._log_notification(
                "post_resolution_analysis", text, window_id, telegram_message_id=msg_id
            )
            return msg_id

        except Exception as exc:
            self._log.warning(
                "telegram.send_post_resolution_failed", error=str(exc)[:100]
            )
            return None

    async def send_shadow_resolution(
        self,
        window_id: str,
        direction: str,
        entry_price: float,
        oracle_direction: str,
        shadow_pnl: float,
        skip_reason: str,
        confidence_tier: str,
        sequoia_p_up: Optional[float] = None,
        regime_hmm: Optional[str] = None,
    ) -> Optional[int]:
        """
        👻 SHADOW — notify when a skipped window's oracle outcome is resolved.

        Shows whether the skipped trade would have won or lost, helping Billy
        evaluate if our gates are too aggressive.

        Format (correct signal):
            👻 SHADOW — BTC 5m | 21:55 UTC | v8.0
            ━━━━━━━━━━━━━━━━━━━━━━━━
            Signal was: UP @ $0.32 (HIGH)
            Oracle resolved: UP ✅
            Shadow P&L: +$3.33 (missed profit)
            Skip reason: delta 0.004% < 0.020%

        Format (wrong signal):
            👻 SHADOW — BTC 5m | 21:55 UTC | v8.0
            ━━━━━━━━━━━━━━━━━━━━━━━━
            Signal was: DOWN @ $0.67 (MODERATE)
            Oracle resolved: UP ❌
            Shadow P&L: -$3.35 (avoided loss)
            Skip reason: VPIN 0.43 < gate 0.45
        """
        try:
            window_time = "?"
            try:
                ts = int(window_id.split("-")[1])
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                window_time = dt.strftime("%H:%M UTC")
            except Exception:
                pass

            signal_correct = direction == oracle_direction
            oracle_emoji = "✅" if signal_correct else "❌"
            pnl_sign = "+" if shadow_pnl >= 0 else ""
            pnl_label = "missed profit" if shadow_pnl > 0 else "avoided loss"

            # v11.2: Sequoia + HMM context at skip time
            _shadow_ctx = ""
            if sequoia_p_up is not None:
                _seq_dir = "UP" if sequoia_p_up >= 0.5 else "DOWN"
                _shadow_ctx += (
                    f"🔮 Sequoia v5.2: P(UP)=`{sequoia_p_up:.3f}` → `{_seq_dir}`\n"
                )
            if regime_hmm:
                _hmm_display = regime_hmm.replace("_", " ").title()
                _shadow_ctx += f"🧠 HMM: `{_hmm_display}`\n"

            text = (
                f"👻 *SHADOW — BTC 5m | {window_time} | {self._engine_version}*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Signal was: `{direction}` @ `${entry_price:.2f}` (`{confidence_tier}`)\n"
                f"Oracle resolved: `{oracle_direction}` {oracle_emoji}\n"
                f"Shadow P&L: `{pnl_sign}${shadow_pnl:.2f}` ({pnl_label})\n"
                f"{_shadow_ctx}"
                f"Skip reason: _{skip_reason[:100]}_\n"
            )

            msg_id = await self._send_with_id(text)
            await self._log_notification(
                "shadow_resolution", text, window_id, telegram_message_id=msg_id
            )
            return msg_id
        except Exception as exc:
            self._log.warning(
                "telegram.send_shadow_resolution_failed", error=str(exc)[:100]
            )
            return None

    async def send_redemption(
        self,
        amount: float,
        new_balance: float,
    ) -> Optional[int]:
        """
        ⑤ 🔄 REDEEMED — Amount returned to wallet, new wallet balance.
        """
        try:
            mode = self._mode_tag()
            sign = "+" if amount >= 0 else ""
            text = (
                f"🔄 *REDEEMED*  {mode}\n"
                f"\n"
                f"Amount returned: `{sign}${amount:.2f}` USDC\n"
                f"New wallet balance: `${new_balance:.2f}` USDC\n"
                f"\n"
                f"{self._footer()}"
            )
            msg_id = await self._send_with_id(text)
            await self._log_notification("redemption", text, telegram_message_id=msg_id)
            return msg_id
        except Exception as exc:
            self._log.warning("telegram.send_redemption_failed", error=str(exc)[:100])
            return None

    async def send_outcome_with_analysis(
        self,
        window_id: str,
        decision: str,
        entry_price: float,
        outcome: str,
        pnl_usd: float,
        window_data: dict = None,
        regime_hmm: Optional[str] = None,
    ) -> tuple:
        """Send MANDATORY outcome + comprehensive AI analysis (separated).

        window_data can include: vpin, delta_pct, regime, timesfm_direction,
        timesfm_confidence, twap_direction, twap_agreement, gamma_up, gamma_down,
        cg_snapshot, open_price, close_price, skip_reason, actual_direction

        Optional (added v11.2):
            regime_hmm: HMM regime active at trade time (calm_trend/volatile_trend/chop/risk_off)
        """
        from datetime import datetime, timezone

        window_time = "?"
        try:
            ts = int(window_id.split("-")[1])
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            window_time = dt.strftime("%H:%M UTC")
        except Exception:
            pass

        wd = window_data or {}
        emoji = "✅" if outcome == "WIN" else "❌"
        pnl_sign = "+" if pnl_usd >= 0 else ""
        mode = self._mode_tag()

        # v8.0 session tracking — reload from DB on first call
        if not hasattr(self, "_session_wins"):
            self._session_wins = 0
            self._session_losses = 0
            self._session_pnl = 0.0
            # Backfill from DB so restarts don't reset counters (synchronous)
            if self._db_client:
                try:
                    async with self._db_client._pool.acquire() as conn:
                        row = await conn.fetchrow(
                            "SELECT "
                            "  SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as w, "
                            "  SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) as l, "
                            "  COALESCE(SUM(pnl_usd), 0) as pnl "
                            "FROM trades WHERE outcome IS NOT NULL "
                            "AND created_at > DATE_TRUNC('day', NOW())"
                        )
                        if row:
                            self._session_wins = int(row["w"] or 0)
                            self._session_losses = int(row["l"] or 0)
                            self._session_pnl = float(row["pnl"] or 0)
                except Exception:
                    pass  # Fall back to zero counters
        if outcome == "WIN":
            self._session_wins += 1
        else:
            self._session_losses += 1
        self._session_pnl += pnl_usd
        total = self._session_wins + self._session_losses
        wr = (self._session_wins / total * 100) if total > 0 else 0

        # v8.0 source attribution
        delta_source = wd.get("delta_source", "?")
        delta_val = wd.get("delta_pct", 0)
        src_short = (
            delta_source.replace("_rest_candle", "").replace("_db_tick", "(db)")
            if delta_source
            else "?"
        )

        oracle_note = ""
        if outcome == "LOSS" and wd.get("actual_direction"):
            oracle_note = (
                f"\nOracle: `{wd['actual_direction']}` ← {src_short} was wrong"
            )

        # v8.1: Show actual fill price (may differ from submitted entry)
        _fill_price = wd.get("actual_fill_price")
        _entry_reason = wd.get("entry_reason", "")
        _fill_line = f"Entry: `${entry_price:.3f}`"
        if _fill_price and abs(float(_fill_price) - entry_price) > 0.001:
            _fill_line = (
                f"Submitted: `${entry_price:.3f}` → Fill: `${float(_fill_price):.4f}`"
            )
        elif _fill_price:
            _fill_line = f"Fill: `${float(_fill_price):.4f}`"

        _reason_tag = f" | `{_entry_reason}`" if _entry_reason else ""

        # v10.3 retrospective block (especially useful for LOSS to show what gates said)
        v103_retro = ""
        _v103_taker = wd.get("v103_taker_status")
        _v103_confirms = wd.get("v103_cg_confirms")
        _v103_threshold = wd.get("v103_threshold")
        _v103_dune_p = wd.get("v103_dune_p")
        if _v103_taker or _v103_confirms is not None:
            retro_lines = []
            retro_label = (
                "v10.3 at entry:" if outcome == "WIN" else "v10.3 retrospective:"
            )
            retro_lines.append(f"\n*{retro_label}*")
            if _v103_taker:
                _icon_map = {
                    "aligned": "✅",
                    "opposing": "⚠️",
                    "both_opposing": "🚫",
                    "neutral": "➖",
                }
                _bp = wd.get("v103_taker_buy_pct", 0)
                _sp = wd.get("v103_taker_sell_pct", 0)
                retro_lines.append(
                    f"🌊 Taker was: `{_v103_taker.upper()}` {_icon_map.get(_v103_taker, '?')} buy {_bp:.0f}% sell {_sp:.0f}%"
                )
            if _v103_confirms is not None:
                _dots = "🟢" * _v103_confirms + "⚫" * (3 - _v103_confirms)
                _dets = wd.get("v103_cg_details", [])
                _det_s = ", ".join(_dets[:3]) if _dets else "none"
                retro_lines.append(f"{_dots} CG: {_v103_confirms}/3 ({_det_s})")
            if _v103_threshold is not None and _v103_dune_p is not None:
                retro_lines.append(
                    f"📐 Was: P={_v103_dune_p:.3f} vs threshold={_v103_threshold:.3f}"
                )
            if outcome == "LOSS" and _v103_taker in ("opposing", "both_opposing"):
                retro_lines.append(
                    f"⚠️ v10.3 taker gate would have: {'BLOCKED' if _v103_taker == 'both_opposing' else 'raised threshold +0.05'}"
                )
            v103_retro = "\n".join(retro_lines) + "\n"

        _version_tag = "v10.3" if _v103_taker else self._engine_version

        # v11.2: HMM regime at trade time
        _hmm_line = ""
        if regime_hmm:
            _hmm_display = regime_hmm.replace("_", " ").title()
            _hmm_line = f"🧠 HMM at trade: `{_hmm_display}`\n"

        outcome_text = (
            f"{emoji} *{outcome}* — BTC 5m | {window_time} | {_version_tag}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Direction: `{decision}` ({src_short} Δ `{delta_val:+.4f}%`)\n"
            f"{_fill_line} | P&L: `{pnl_sign}${pnl_usd:.2f}`{_reason_tag}"
            f"{oracle_note}\n"
            f"📊 Session: `{self._session_wins}W/{self._session_losses}L ({wr:.1f}%)` | `{'+' if self._session_pnl >= 0 else ''}${self._session_pnl:.2f}`\n"
            f"{_hmm_line}"
            f"{v103_retro}"
        )

        outcome_msg_id = await self._send_with_id(outcome_text)
        await self._log_notification(
            "outcome_v8", outcome_text, window_id, telegram_message_id=outcome_msg_id
        )

        # AI analysis — shorter in v8.0 (1-2 sentences)
        analysis_msg_id = None
        try:
            _ti = wd.get("delta_tiingo")
            _bn = wd.get("delta_binance")
            _src_detail = ""
            if _ti is not None and _bn is not None:
                _src_detail = f" Tiingo Δ{_ti:+.4f}%, Binance Δ{_bn:+.4f}%."
            prompt = (
                f"BTC 5m {decision} @ ${entry_price:.3f} → {outcome} ({pnl_sign}${pnl_usd:.2f}). "
                f"Source: {delta_source}. VPIN: {wd.get('vpin', '?')}.{_src_detail} "
                f"Oracle: {wd.get('actual_direction', '?')}. "
                f"1 sentence: why did this {'win' if outcome == 'WIN' else 'lose'}?"
            )
            ai_text, ai_source = await self._ai.assess(prompt, timeout_s=6)
            analysis_card = f"🤖 `{ai_source.upper()}` — _{ai_text[:300]}_"
            analysis_msg_id = await self._send_with_id(analysis_card)
        except Exception as exc:
            self._log.warning("ai.outcome_analysis_failed", error=str(exc)[:100])

        return outcome_msg_id, analysis_msg_id

    def set_risk_manager(self, rm) -> None:
        self._risk_manager = rm

    def set_poly_client(self, pc) -> None:
        self._poly_client = pc

    @property
    def trade_alerts_enabled(self) -> bool:
        return self._alerts_paper if self._paper_mode else self._alerts_live

    # ── Location / identity ────────────────────────────────────────────────────

    _location: str = "MTL"
    _engine_version: str = "v11.2"
    _db_client = None  # injected after construction

    async def send_session_summary(
        self,
        delta_source: Optional[str] = None,
        features: Optional[dict] = None,
    ) -> Optional[int]:
        """v11.2 Session Summary Card — call periodically or on demand.

        Args:
            delta_source: Primary delta source name (e.g. "tiingo", "chainlink").
                          Falls back to "consensus" if not provided.
            features: Dict of feature flags, e.g. {"FOK": True, "TWAP": False, "Sequoia": True}.
                      Falls back to sensible defaults if not provided.
        """
        try:
            w = getattr(self, "_session_wins", 0)
            l = getattr(self, "_session_losses", 0)
            pnl = getattr(self, "_session_pnl", 0.0)
            total = w + l
            wr = (w / total * 100) if total > 0 else 0
            pnl_sign = "+" if pnl >= 0 else ""

            _src = delta_source or "consensus"
            _feats = features or {}
            feat_parts = []
            for k, v in _feats.items():
                feat_parts.append(f"{k}: `{'on' if v else 'off'}`")
            feat_line = " | ".join(feat_parts) if feat_parts else ""

            text = (
                f"📋 *Session Summary* | {self._engine_version}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Trades: `{total}` | `{w}W/{l}L` (`{wr:.1f}%`)\n"
                f"P&L: `{pnl_sign}${pnl:.2f}`\n"
                f"Delta source: `{_src}`\n"
            )
            if feat_line:
                text += f"{feat_line}\n"
            return await self._send_with_id(text)
        except Exception as exc:
            self._log.warning("telegram.session_summary_failed", error=str(exc)[:100])
            return None

    async def send_fok_exhausted(
        self,
        window_id: str,
        attempts: int,
        prices: list,
        abort_reason: str = "",
        dynamic_cap: float = 0.73,
    ) -> Optional[int]:
        """v8.1 FOK Ladder — no fill, falling back to GTC with dynamic cap."""
        try:
            from datetime import datetime, timezone

            window_time = "?"
            try:
                ts = int(window_id.split("-")[1])
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                window_time = dt.strftime("%H:%M UTC")
            except Exception:
                pass
            import os as _os

            _ot = _os.environ.get("ORDER_TYPE", "FAK").upper()
            if attempts == 0:
                reason_clean = abort_reason or "no book liquidity"
                reason_clean = (
                    reason_clean.split("for token")[0].strip()
                    if "for token" in reason_clean
                    else reason_clean
                )
                text = (
                    f"🔄 *{_ot} → GTC* — BTC 5m | {window_time} | v9.0\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"CLOB book: `{reason_clean[:60]}`\n"
                    f"→ GTC limit at cap (`${dynamic_cap:.2f}`)\n"
                )
            else:
                price_str = " → ".join([f"${p:.3f}" for p in prices[:5]])
                text = (
                    f"🔄 *{_ot} → GTC* — BTC 5m | {window_time} | v9.0\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"{_ot}: `{attempts}` killed | Prices: `{price_str}`\n"
                    f"→ GTC limit at cap (`${dynamic_cap:.2f}`)\n"
                )
            return await self._send_with_id(text)
        except Exception as exc:
            self._log.warning("telegram.fok_exhausted_failed", error=str(exc)[:100])
            return None

    def set_location(self, location: str, version: str = "v11.2") -> None:
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
            async with self._db_client._pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO telegram_notifications
                       (bot_id, location, window_id, notification_type,
                        message_text, has_chart, engine_version, telegram_message_id)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                    "novakash",
                    self._location,
                    window_id,
                    notification_type,
                    message_text[:4000],
                    has_chart,
                    self._engine_version,
                    telegram_message_id,
                )
        except Exception as exc:
            self._log.warning("telegram.log_notification_failed", error=str(exc)[:80])

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
        """v8.0 Window Open — compact card."""
        from datetime import datetime, timezone

        window_time = "?"
        try:
            ts = int(window_id.split("-")[1])
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            window_time = dt.strftime("%H:%M UTC")
        except:
            pass

        skew = (
            "BALANCED"
            if abs(gamma_up - gamma_down) < 0.03
            else ("↑ UP" if gamma_up > gamma_down else "↓ DOWN")
        )
        text = (
            f"🪟 *{asset} {timeframe}* | {window_time} | {self._engine_version}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Open: `${open_price:,.2f}` | Gamma: ↑`${gamma_up:.3f}` ↓`${gamma_down:.3f}` `{skew}`\n"
        )
        msg_id = await self._send_with_id(text)
        await self._log_notification(
            "window_open", text, window_id, telegram_message_id=msg_id
        )

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
        from datetime import datetime, timezone

        delta_sign = "+" if delta_pct >= 0 else ""
        dirs = [
            d
            for d in [
                twap_direction,
                timesfm_direction,
                "UP" if delta_pct > 0 else "DOWN",
            ]
            if d
        ]
        conflict = len(set(dirs)) > 1

        # Extract window time from ID
        window_time = "?"
        try:
            ts = int(window_id.split("-")[1])
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            window_time = dt.strftime("%H:%M UTC")
        except:
            pass

        # v8.0 compact snapshot card
        _dir_emoji = "▲" if delta_pct > 0 else "▼"
        _dir = "UP" if delta_pct > 0 else "DOWN"
        caption_lines = [
            f"⏱ *{t_label}* — BTC {window_time} | {self._engine_version}",
            f"━━━━━━━━━━━━━━━━━━━━━━",
            f"Tiingo: Δ `{delta_sign}{delta_pct:.4f}%` {_dir_emoji} `{_dir}`",
            f"VPIN: `{vpin:.3f}` `{vpin_regime}` | Gamma: ↑`${gamma_up:.3f}` ↓`${gamma_down:.3f}`",
        ]
        # No more SIGNAL CONFLICT — TWAP/TimesFM disabled in v8.0
        if ai_commentary:
            caption_lines.append(f"🤖 _{ai_commentary[:200]}_")

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
            f"snapshot_{t_label}",
            caption,
            window_id,
            has_chart=bool(chart_bytes),
            telegram_message_id=msg_id,
        )

    async def send_window_resolution(
        self,
        window_id: str,
        asset: str,
        timeframe: str,
        outcome: str,  # "WIN" or "LOSS"
        direction: str,  # "UP" or "DOWN" (our bet)
        actual_direction: str,  # "UP" or "DOWN" (what happened)
        entry_price: float,  # actual token entry price
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
        entry_reason: Optional[str] = None,
    ) -> None:
        """Full resolution report with what-if P&L table at each T-point."""
        result_e = "✅" if outcome == "WIN" else "❌"
        correct = actual_direction == direction
        confirm = "✓" if correct else "✗"
        pnl_sign = "+" if pnl_usd >= 0 else ""
        streak_str = (
            f" | `{win_streak}W streak`"
            if win_streak > 0
            else f" | `{loss_streak}L streak`"
            if loss_streak > 0
            else ""
        )

        reason_line = f"Entry: `{entry_reason}`\n" if entry_reason else ""

        lines = [
            f"{result_e} *{outcome}* — {asset} {timeframe} | {self._engine_version}",
            f"━━━━━━━━━━━━━━━━━━━━━━",
            f"Bet: `{direction}` @ `${entry_price:.3f}` → Oracle: `{actual_direction}` {confirm}",
            f"P&L: `{pnl_sign}${pnl_usd:.2f}`{streak_str}",
            f"BTC: `${open_price:,.2f}` → `${close_price:,.2f}` (Δ `{delta_pct:+.4f}%`)",
            f"VPIN: `{vpin:.3f}` `{regime}`",
        ]
        if reason_line:
            lines.append(reason_line.rstrip())
        lines += [
            f"",
            f"*Entry prices by checkpoint:*",
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
            lines.append(
                f"`{label}`  `${ep_price:.3f}`  →  `{actual_sign}${actual_pnl:.2f}` {pnl_e}{marker}"
            )

        if ai_commentary:
            lines += ["", f"🤖 _{ai_commentary}_"]

        pf = self._portfolio_line()
        if pf:
            lines += ["", pf]
        lines += ["", self._footer(window_id)]

        text = "\n".join(lines)
        msg_id = await self._send_with_id(text)
        await self._log_notification(
            "resolution",
            text,
            window_id,
            has_chart=False,
            telegram_message_id=msg_id,
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
            _twap_dir = (
                getattr(twap_result, "twap_direction", None) if twap_result else None
            )
            _gamma_dir = (
                getattr(twap_result, "gamma_direction", None) if twap_result else None
            )
            _tfm_dir = (
                getattr(timesfm_forecast, "direction", None)
                if timesfm_forecast
                else None
            )
            _tfm_conf = (
                getattr(timesfm_forecast, "confidence", 0) if timesfm_forecast else 0
            )
            _tfm_close = (
                getattr(timesfm_forecast, "predicted_close", 0)
                if timesfm_forecast
                else 0
            )
            _tfm_err = (
                getattr(timesfm_forecast, "error", "") if timesfm_forecast else ""
            )

            # Agreement score (Point + TWAP + Gamma)
            _agree_count = 0
            _agree_parts = []
            for src_name, src_dir in [
                ("Point", _pt_dir),
                ("TWAP", _twap_dir),
                ("Gamma", _gamma_dir),
            ]:
                if src_dir:
                    arrow = "▲" if src_dir == "UP" else "▼"
                    _agree_parts.append(f"{src_name} {arrow}")
                    if src_dir == _dir:
                        _agree_count += 1
                else:
                    _agree_parts.append(f"{src_name} ?")
            _agree_bar = _agree_bar_fn(_agree_count)

            # TWAP agreement score (internal)
            _twap_agree_score = (
                getattr(twap_result, "agreement_score", 0) if twap_result else 0
            )
            _gamma_gate = (
                getattr(twap_result, "gamma_gate", "OK") if twap_result else "OK"
            )
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
            lines.append(
                f"Gamma UP `${_g_up:.3f}` / DOWN `${_g_down:.3f}`  {_gate_e} `{_gamma_gate}`"
            )

            lines.append(f"")

            # Trade / skip
            if trade_placed:
                _v81_cap = signal.get("v81_entry_cap") if signal else None
                _price = (
                    _v81_cap or token_price or (_g_down if _dir == "DOWN" else _g_up)
                )
                _stake = stake_usd or 4.0
                _token = "NO" if _dir == "DOWN" else "YES"
                _win = (1.0 - _price) * _stake * 0.98
                _reason = (signal.get("entry_reason", "") if signal else "") or ""
                lines.append(
                    f"⚡ *PLACED {_token} limit `${_price:.2f}`* | `{_reason}`"
                )
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
            streak_str = (
                f"{win_streak}W"
                if win_streak > 0
                else (f"{loss_streak}L" if loss_streak > 0 else "")
            )

            # Did actual close confirm direction?
            _actual_dir = "UP" if close_price > open_price else "DOWN"
            _correct = _actual_dir == _dir
            _confirm = "✓" if _correct else "✗"

            lines = [
                f"{result_emoji} *{outcome} — {asset} {timeframe}*  {mode}",
                f"",
                f"{_dir_arrow} {_dir} @ `${tp:.3f}` → resolved {_actual_dir} {_confirm}",
                f"P&L: `{pnl_sign}${pnl:.2f}`  |  Streak: `{streak_str}`"
                if streak_str
                else f"P&L: `{pnl_sign}${pnl:.2f}`",
                f"",
            ]

            # AI assessment
            try:
                assessment = await self._generate_assessment(
                    order=order,
                    asset=asset,
                    timeframe=timeframe,
                    open_price=open_price,
                    close_price=close_price,
                    delta_pct=delta_pct,
                    vpin=vpin,
                    regime=regime,
                    twap_result=twap_result,
                    timesfm_forecast=timesfm_forecast,
                    win_streak=win_streak,
                    loss_streak=loss_streak,
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
                        caption = (
                            f"{result_emoji} {asset} {timeframe} — {pnl_sign}${pnl:.2f}"
                        )
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
        """Send GTC fill confirmation when CLOB matches our order."""
        try:
            meta = order.metadata or {}
            fill_price = meta.get("actual_fill_price", "?")
            cap = meta.get("v81_entry_cap", "?")
            reason = meta.get("entry_reason", "?")
            wait_s = meta.get("fill_wait_seconds", "?")
            direction = "⬇️ DOWN" if order.direction == "NO" else "⬆️ UP"
            size = meta.get("size_matched", meta.get("shares_filled", "?"))

            # Format numeric values safely — fill_price/cap may be strings or "?"
            try:
                fp_str = f"${float(fill_price):.4f}"
            except (TypeError, ValueError):
                fp_str = f"`{fill_price}`"
            try:
                cap_str = f"${float(cap):.2f}"
            except (TypeError, ValueError):
                cap_str = f"`{cap}`"

            msg = (
                f"✅ *GTC FILLED*\n"
                f"─────────────\n"
                f"{direction} | `{reason}`\n"
                f"💰 Fill: `{fp_str}` (cap `{cap_str}`)\n"
                f"📦 Size: `{size}` shares | Stake: `${order.stake_usd:.2f}`\n"
                f"⏱ Filled in `{wait_s}s`\n"
            )
            # v11 fix: was calling non-existent self._send_telegram() — use self._send()
            await self._send(msg)
            await self._log_notification(
                notification_type="gtc_fill",
                message_text=msg,
                window_id=meta.get("market_slug"),
            )
            self._log.info(
                "telegram.entry_alert_sent", order_id=str(order.order_id)[:20]
            )
        except Exception as exc:
            self._log.warning("telegram.entry_alert_failed", error=str(exc)[:200])

    # Backwards compat for v6.0 window reports
    async def send_timesfm_window_report(self, **kwargs) -> None:
        """Legacy alias → send_window_report."""
        await self.send_window_report(
            **{
                k: v
                for k, v in kwargs.items()
                if k
                in (
                    "window_ts",
                    "asset",
                    "timeframe",
                    "open_price",
                    "close_price",
                    "delta_pct",
                    "vpin",
                    "regime",
                    "direction",
                    "trade_placed",
                    "skip_reason",
                    "twap_result",
                    "timesfm_forecast",
                    "gamma_up_price",
                    "gamma_down_price",
                    "stake_usd",
                    "token_price",
                )
            }
        )

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
            entry_reason = meta.get("entry_reason_detail") or meta.get(
                "entry_label", "—"
            )

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
                tfm_ctx = (
                    f"TimesFM predicted {tfm_dir} with {tfm_conf:.0%} confidence. "
                )

            # Streak context
            streak_ctx = ""
            if win_streak >= 3:
                streak_ctx = f"Currently on a {win_streak}-win streak. "
            elif loss_streak >= 2:
                streak_ctx = f"Coming off a {loss_streak}-loss streak. "

            # v8.0: include delta source and multi-source data
            delta_source = meta.get("delta_source", "?")
            delta_tiingo = meta.get("delta_tiingo")
            delta_binance = meta.get("delta_binance")
            delta_chainlink = meta.get("delta_chainlink")
            confidence_tier = meta.get("confidence_tier", "?")
            gates = meta.get("gates_passed", "")
            gate_failed = meta.get("gate_failed", "")

            source_ctx = f"Delta source: {delta_source}. "
            if delta_tiingo is not None and delta_binance is not None:
                source_ctx += (
                    f"Tiingo Δ{delta_tiingo:+.4f}%, Binance Δ{delta_binance:+.4f}%"
                )
                if delta_chainlink is not None:
                    source_ctx += f", Chainlink Δ{delta_chainlink:+.4f}%"
                source_ctx += ". "

            prompt = (
                f"{asset} {timeframe} v8.0 trade. "
                f"Regime: {regime}, VPIN: {vpin:.3f}, Delta: {delta_pct:+.4f}%\n"
                f"Direction: {direction}, Entry: ${tp:.3f}, Confidence: {confidence_tier}\n"
                f"BTC: ${open_price:,.2f}→${close_price:,.2f}. Outcome: {outcome}, PnL: ${pnl:+.2f}\n"
                f"{source_ctx}{streak_ctx}"
                f"1 sentence: was this a good entry? Key factor in the {outcome.lower()}."
            )

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.anthropic.com/v1/messages",
                    json={
                        "model": "claude-sonnet-4-6",
                        "max_tokens": 100,
                        "system": "You are a crypto trading analyst for Polymarket 5-min prediction markets. The engine uses Tiingo as primary delta source (oracle-aligned). Be concise.",
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
            dir_str = (
                f"{_DIR_EMOJI.get(signal.direction or '', '')} `{signal.direction}`"
                if signal.direction
                else "`none`"
            )

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
        emoji = {"info": "🟢", "warning": "🟡", "error": "🔴", "critical": "🔴"}.get(
            level, "🟢"
        )
        try:
            text = f"{emoji} *System*\n{message}"
            await self._send(text)
            await self._log_notification(f"system_{level}", text[:2000])
            self._log.info(
                "telegram.system_alert_sent", level=level, preview=message[:80]
            )
        except Exception as exc:
            # v11 fix: was silently swallowing all errors — now logs for diagnosability
            self._log.warning(
                "telegram.system_alert_failed", level=level, error=str(exc)[:200]
            )

    async def send_strategy_trade_alert(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        direction: str,
        confidence: str = "?",
        confidence_score: float = 0.0,
        entry_reason: str = "",
        gate_results: list = None,
        sizing_modifier: float = 1.0,
        sizing_label: str = "default",
        fill_price: float = 0.0,
        fill_size: float = 0.0,
        stake_usd: float = 0.0,
        order_type: str = "PAPER",
        btc_price: float = 0.0,
        vpin: float = 0.0,
        regime: str = "?",
        clob_up_ask: float = None,
        clob_down_ask: float = None,
        eval_offset: int = None,
        paper_mode: bool = True,
        success: bool = True,
        failure_reason: str = "",
        elapsed_s: float = 0.0,
    ) -> None:
        """Rich Telegram alert for Strategy Engine v2 trades.

        Shows strategy name, gate pipeline, sizing, fill details.
        """
        try:
            dir_emoji = "⬇" if direction == "DOWN" else "⬆"
            mode_tag = "📝 PAPER" if paper_mode else "💰 LIVE"

            # Header
            lines = [
                f"🎯 *TRADE — BTC 5m | {strategy_id} {strategy_version}*",
                f"━━━━━━━━━━━━━━━━━━━━━━━━━",
            ]

            # Signal
            lines.append(
                f"{dir_emoji} Signal: *{direction}* | conf={confidence} ({confidence_score:.2f})"
            )
            if vpin > 0:
                lines.append(f"📈 VPIN: {vpin:.3f} | {regime}")
            if clob_up_ask or clob_down_ask:
                up_str = f"${clob_up_ask:.3f}" if clob_up_ask else "?"
                dn_str = f"${clob_down_ask:.3f}" if clob_down_ask else "?"
                lines.append(f"📊 CLOB: ↑{up_str} ↓{dn_str}")
            if eval_offset:
                lines.append(f"⏱ Entry: T-{eval_offset}s")

            # Gates
            if gate_results:
                checks = []
                for g in gate_results or []:
                    icon = "✅" if g.get("passed") else "❌"
                    checks.append(f"{icon}{g.get('gate', '?')}")
                lines.append(f"\n⚡ Gates: {' '.join(checks)}")

            # Sizing
            if sizing_modifier != 1.0:
                lines.append(f"💰 Sizing: {sizing_modifier:.1f}× ({sizing_label})")

            lines.append("")

            # Fill result
            if success and fill_price > 0:
                lines.append(f"✅ {order_type} FILLED")
                lines.append(
                    f"💵 Fill: ${fill_price:.3f} | "
                    f"Size: {fill_size:.1f} shares | "
                    f"Stake: ${stake_usd:.2f}"
                )
                if elapsed_s > 0:
                    lines.append(f"⏱ Filled in {elapsed_s:.1f}s")
            elif not success:
                lines.append(f"❌ {order_type} FAILED: {failure_reason}")

            if btc_price > 0:
                lines.append(f"\nBTC: ${btc_price:,.2f}")

            lines.append(f"\n{mode_tag}")

            text = "\n".join(lines)
            await self._send(text)
            await self._log_notification(f"strategy_trade_{strategy_id}", text[:2000])
            self._log.info(
                "telegram.strategy_trade_sent",
                strategy=strategy_id,
                direction=direction,
                success=success,
            )
        except Exception as exc:
            self._log.warning(
                "telegram.strategy_trade_alert_failed",
                strategy=strategy_id,
                error=str(exc)[:200],
            )

    async def send_kill_switch_alert(self) -> None:
        try:
            await self._send(
                "🛑 *KILL SWITCH*\nAll trading halted. Manual restart required."
            )
        except Exception:
            pass

    async def send_raw_message(self, text: str) -> None:
        """Send a raw markdown message — always sends (bypasses trade_alerts_enabled gate).
        Used for sitreps and system notifications that should always go through."""
        try:
            await self._send(text)
            await self._log_notification("raw_message", text[:2000])
        except Exception as exc:
            self._log.warning("telegram.raw_message_failed", error=str(exc)[:200])

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
                await self._send_photo(
                    chart, f"📊 Daily P&L — {date_str or _now_utc()}"
                )
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
                    self._url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("result", {}).get("message_id")
                    body = await resp.text()
                    if "can't parse entities" in body:
                        plain = dict(payload)
                        del plain["parse_mode"]
                        async with session.post(
                            self._url,
                            json=plain,
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as r2:
                            if r2.status == 200:
                                d2 = await r2.json()
                                return d2.get("result", {}).get("message_id")
        except Exception as exc:
            self._log.warning("telegram.send_error", error=str(exc))
        return None

    async def _send_photo_with_id(
        self, photo_bytes: bytes, caption: str = ""
    ) -> Optional[int]:
        """Send photo and return Telegram message_id."""
        if not self._bot_token or not self._chat_id or not photo_bytes:
            return None
        try:
            form = aiohttp.FormData()
            form.add_field("chat_id", str(self._chat_id))
            form.add_field(
                "photo", photo_bytes, content_type="image/png", filename="chart.png"
            )
            if caption:
                form.add_field("caption", caption[:1024])
                form.add_field("parse_mode", "Markdown")
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._photo_url,
                    data=form,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("result", {}).get("message_id")
                    body = await resp.text()
                    self._log.warning(
                        "telegram.photo_failed", status=resp.status, body=body[:100]
                    )
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
                    self._url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        if "can't parse entities" in body:
                            # Retry without markdown
                            plain = {**payload}
                            del plain["parse_mode"]
                            async with session.post(
                                self._url,
                                json=plain,
                                timeout=aiohttp.ClientTimeout(total=10),
                            ) as r2:
                                if r2.status != 200:
                                    self._log.warning(
                                        "telegram.send_failed", status=r2.status
                                    )
                        else:
                            self._log.warning(
                                "telegram.api_error",
                                status=resp.status,
                                body=body[:200],
                            )
        except Exception as exc:
            self._log.warning("telegram.send_error", error=str(exc))

    async def _send_photo(self, photo_bytes: bytes, caption: str = "") -> None:
        """Send a PNG chart via Telegram sendPhoto."""
        if not self._bot_token or not self._chat_id or not photo_bytes:
            return
        try:
            form = aiohttp.FormData()
            form.add_field("chat_id", str(self._chat_id))
            form.add_field(
                "photo", photo_bytes, content_type="image/png", filename="chart.png"
            )
            if caption:
                form.add_field("caption", caption[:1024])
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._photo_url,
                    data=form,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        self._log.warning(
                            "telegram.photo_failed", status=resp.status, body=body[:200]
                        )
        except Exception as exc:
            self._log.warning("telegram.photo_error", error=str(exc))


# Helper (module-level to avoid closure issues)
def _agree_bar_fn(n: int, total: int = 3) -> str:
    return "🟢" * n + "⚫" * (total - n)


# ─── Dual AI Fallback System ────────────────────────────────────────────────
class DualAIAssessment:
    """Claude primary, Qwen122b fallback for redundancy."""

    def __init__(
        self,
        anthropic_key: str,
        qwen_host: str = "ollama-ssh1",
        qwen_port: int = 11434,
        log=None,
    ):
        self.anthropic_key = anthropic_key
        self.qwen_host = qwen_host
        self.qwen_port = qwen_port
        self.log = log

    async def assess(self, prompt: str, timeout_s: int = 8) -> tuple[str, str]:
        """
        Get AI assessment with fallback.
        Returns (text, source) where source = "claude" | "qwen" | "timeout" | "error"
        """
        # Try Claude first (if key available)
        if self.anthropic_key:
            try:
                text = await self._claude(prompt, timeout_s=timeout_s)
                return text, "claude"
            except asyncio.TimeoutError:
                if self.log:
                    self.log.warning("ai.claude_timeout")
            except Exception as e:
                if self.log:
                    self.log.warning("ai.claude_error", error=str(e)[:100])

        # Fallback: Qwen122b
        try:
            text = await self._qwen(prompt, timeout_s=timeout_s)
            return text, "qwen"
        except asyncio.TimeoutError:
            if self.log:
                self.log.warning("ai.qwen_timeout")
            return "[AI analysis timeout - see trade decision above]", "timeout"
        except Exception as e:
            if self.log:
                self.log.warning("ai.qwen_error", error=str(e)[:100])
            return "[AI analysis unavailable - see trade decision above]", "error"

    async def _claude(self, prompt: str, timeout_s: int = 8) -> str:
        """Call Claude via Anthropic API."""
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": self.anthropic_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 200,
            "messages": [{"role": "user", "content": prompt}],
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["content"][0]["text"]
                raise Exception(f"Status {resp.status}")

    async def _qwen(self, prompt: str, timeout_s: int = 45) -> str:
        """Call Qwen122b — supports both Ollama (/api/generate) and OpenAI (/v1) endpoints.

        Vast.ai / OpenAI-compatible: set QWEN_HOST=194.228.55.129 QWEN_PORT=39633
        Reasoning model needs up to 45s timeout.
        """
        host = self.qwen_host
        port = self.qwen_port

        # Detect OpenAI-compatible endpoint (not Ollama)
        api_key = os.environ.get("QWEN_API_KEY", "")
        if api_key:
            # OpenAI-compatible (Vast.ai / vLLM)
            url = f"http://{host}:{port}/v1/chat/completions"
            payload = {
                "model": os.environ.get("QWEN_MODEL", "qwen35-122b-abliterated"),
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 250,
                "temperature": 0.3,
            }
            headers = {"Authorization": f"Bearer {api_key}"}
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout_s),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"][:300]
                    raise Exception(f"Status {resp.status}")
        else:
            # Legacy Ollama endpoint
            url = f"http://{host}:{port}/api/generate"
            payload = {
                "model": "qwen35-122b-abliterated:latest",
                "prompt": prompt,
                "stream": False,
                "temperature": 0.3,
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout_s)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["response"][:300]
                    raise Exception(f"Status {resp.status}")
