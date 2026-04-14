"""Haiku-powered window summary generator for Telegram alerts.

Uses Claude Haiku to produce human-readable 2-3 sentence summaries of
5-minute BTC trading windows. Falls back to a template-based summary
if the API call fails (rate limit, key missing, timeout).

Never blocks the asyncio event loop -- runs the synchronous Anthropic
SDK call in a thread executor.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

log = structlog.get_logger(__name__)

# Lazy-loaded to avoid import errors when anthropic is not installed
_anthropic_module: Any = None


def _get_anthropic():
    global _anthropic_module
    if _anthropic_module is None:
        try:
            import anthropic
            _anthropic_module = anthropic
        except ImportError:
            _anthropic_module = None
    return _anthropic_module


class HaikuSummarizer:
    """Generate human-readable window summaries via Claude Haiku.

    Two entry points:
    - summarize_evaluation(): called at T-62 with strategy decisions
    - summarize_resolution(): called when a window resolves with outcome
    """

    def __init__(self, api_key: Optional[str] = None):
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._client: Any = None

    def _ensure_client(self) -> bool:
        """Lazily create the Anthropic client. Returns True if available."""
        if self._client is not None:
            return True
        if not self._api_key:
            return False
        anthropic = _get_anthropic()
        if anthropic is None:
            log.warning("haiku_summarizer.no_anthropic_sdk")
            return False
        try:
            self._client = anthropic.Anthropic(api_key=self._api_key)
            return True
        except Exception as exc:
            log.warning("haiku_summarizer.client_init_error", error=str(exc)[:200])
            return False

    # ── T-62 Window Evaluation Summary ─────────────────────────────────────

    async def summarize_evaluation(self, context: dict) -> str:
        """Generate a human-readable evaluation summary for a trading window.

        Called at T-62 (final evaluation offset) with the full data surface
        and strategy decision results.
        """
        if not self._ensure_client():
            return self._fallback_evaluation(context)

        prompt = self._build_evaluation_prompt(context)
        try:
            summary = await asyncio.get_event_loop().run_in_executor(
                None, self._call_haiku, prompt
            )
            return self._format_evaluation_message(context, summary)
        except Exception as exc:
            log.warning("haiku_summarizer.eval_api_error", error=str(exc)[:200])
            return self._fallback_evaluation(context)

    def _build_evaluation_prompt(self, ctx: dict) -> str:
        return (
            "Summarize this 5-minute BTC trading window in 2-3 concise sentences "
            "for a trader monitoring Telegram. Focus on market read and why "
            "strategies acted as they did. Be specific with numbers. No fluff.\n\n"
            f"Window: {ctx.get('window_time', '?')} UTC | "
            f"Delta: {ctx.get('delta_pct', '?')}% | "
            f"VPIN: {ctx.get('vpin', '?')} | "
            f"Regime: {ctx.get('regime', '?')}\n"
            f"Model: P(UP)={ctx.get('p_up', '?')}, "
            f"direction={ctx.get('model_direction', '?')}, "
            f"confidence dist={ctx.get('dist', '?')}\n"
            f"Chainlink delta: {ctx.get('chainlink_delta', '?')}% | "
            f"Tiingo delta: {ctx.get('tiingo_delta', '?')}% | "
            f"Sources agree: {ctx.get('sources_agree', '?')}\n\n"
            f"Strategy decisions:\n{ctx.get('decisions_text', 'none')}"
        )

    def _format_evaluation_message(self, ctx: dict, ai_summary: str) -> str:
        """Build the final Telegram message with header + AI summary + decisions."""
        # Header line
        delta_str = f"{ctx.get('delta_pct', '?')}%" if ctx.get('delta_pct') is not None else "?"
        vpin_str = f"VPIN {ctx['vpin']:.2f}" if ctx.get('vpin') is not None else ""
        header = f"\U0001f550 BTC {ctx.get('timescale','5m')} | {ctx.get('window_time', '?')} UTC | \u0394{delta_str}"
        if vpin_str:
            header += f" | {vpin_str}"

        # AI market summary
        market_line = f"\U0001f4ca {ai_summary}"

        # Per-strategy lines
        strategy_lines = ctx.get("decision_lines", [])

        parts = [header, "", market_line]
        if strategy_lines:
            parts.append("")
            parts.extend(strategy_lines)

        return "\n".join(parts)

    def _fallback_evaluation(self, ctx: dict) -> str:
        """Template-based fallback when Haiku API is unavailable."""
        delta_str = f"{ctx.get('delta_pct', '?')}%" if ctx.get('delta_pct') is not None else "?"
        vpin_str = f"VPIN {ctx['vpin']:.2f}" if ctx.get('vpin') is not None else ""
        regime = ctx.get("regime", "?")

        header = f"\U0001f550 BTC {ctx.get('timescale','5m')} | {ctx.get('window_time', '?')} UTC | \u0394{delta_str}"
        if vpin_str:
            header += f" | {vpin_str}"

        # Simple template summary
        model_dir = ctx.get("model_direction", "?")
        p_up = ctx.get("p_up")
        dist = ctx.get("dist")
        sources_agree = ctx.get("sources_agree", "?")
        chain_d = ctx.get("chainlink_delta", "?")
        tiingo_d = ctx.get("tiingo_delta", "?")

        summary_parts = []
        if p_up is not None:
            summary_parts.append(
                f"Model favors {model_dir} (P(UP)={p_up:.2f}, dist={dist:.2f})"
                if dist is not None
                else f"Model favors {model_dir} (P(UP)={p_up:.2f})"
            )
        summary_parts.append(f"Chainlink: {chain_d}% | Tiingo: {tiingo_d}%")
        if sources_agree is not None:
            summary_parts.append(f"Sources agree: {sources_agree}")

        market_line = f"\U0001f4ca {'. '.join(summary_parts)}."

        strategy_lines = ctx.get("decision_lines", [])

        parts = [header, "", market_line]
        if strategy_lines:
            parts.append("")
            parts.extend(strategy_lines)

        return "\n".join(parts)

    # ── Window Resolution Summary ──────────────────────────────────────────

    async def summarize_resolution(self, context: dict) -> str:
        """Generate a human-readable resolution summary for a completed window.

        Called when the reconciler resolves a window's actual direction.
        """
        if not self._ensure_client():
            return self._fallback_resolution(context)

        prompt = self._build_resolution_prompt(context)
        try:
            summary = await asyncio.get_event_loop().run_in_executor(
                None, self._call_haiku, prompt
            )
            return self._format_resolution_message(context, summary)
        except Exception as exc:
            log.warning("haiku_summarizer.resolution_api_error", error=str(exc)[:200])
            return self._fallback_resolution(context)

    def _build_resolution_prompt(self, ctx: dict) -> str:
        return (
            "Write a 1-2 sentence resolution summary for a BTC 5-minute trading "
            "window. Be specific and concise. Include what happened and any "
            "trade results.\n\n"
            f"Window: {ctx.get('window_time', '?')} UTC\n"
            f"Open: ${ctx.get('open_price', '?')} -> Close: ${ctx.get('close_price', '?')}\n"
            f"Delta: {ctx.get('delta_pct', '?')}%\n"
            f"Resolved: {ctx.get('actual_direction', '?')}\n"
            f"Oracle: {ctx.get('oracle_source', 'Chainlink')}\n\n"
            f"Trades this window:\n{ctx.get('trades_text', 'No trades placed.')}\n\n"
            f"Strategies that would have traded:\n"
            f"{ctx.get('ghost_text', 'None.')}"
        )

    def _format_resolution_message(self, ctx: dict, ai_summary: str) -> str:
        """Build the final resolution Telegram message."""
        actual = ctx.get("actual_direction", "?")
        arrow = "\u2193" if actual == "DOWN" else "\u2191"
        delta_str = f"{ctx.get('delta_pct', '?')}%" if ctx.get('delta_pct') is not None else "?"

        header = (
            f"\u2705 BTC {ctx.get('timescale','5m')} | {ctx.get('window_time', '?')} UTC | "
            f"RESOLVED {actual} {arrow}"
        )

        price_line = (
            f"Open: ${ctx.get('open_price', '?')} \u2192 "
            f"Close: ${ctx.get('close_price', '?')} | \u0394{delta_str}"
        )

        # Trade result lines (if any)
        trade_lines = ctx.get("trade_result_lines", [])

        parts = [header, price_line, "", ai_summary]
        if trade_lines:
            parts.append("")
            parts.extend(trade_lines)

        return "\n".join(parts)

    def _fallback_resolution(self, ctx: dict) -> str:
        """Template-based fallback for resolution messages."""
        actual = ctx.get("actual_direction", "?")
        arrow = "\u2193" if actual == "DOWN" else "\u2191"
        delta_str = f"{ctx.get('delta_pct', '?')}%" if ctx.get('delta_pct') is not None else "?"

        header = (
            f"\u2705 BTC {ctx.get('timescale','5m')} | {ctx.get('window_time', '?')} UTC | "
            f"RESOLVED {actual} {arrow}"
        )

        price_line = (
            f"Open: ${ctx.get('open_price', '?')} \u2192 "
            f"Close: ${ctx.get('close_price', '?')} | \u0394{delta_str}"
        )

        oracle = ctx.get("oracle_source", "Chainlink")
        oracle_line = f"Oracle: {oracle} confirmed {actual}"

        trade_lines = ctx.get("trade_result_lines", [])

        parts = [header, price_line, oracle_line]
        if trade_lines:
            parts.append("")
            parts.extend(trade_lines)
        else:
            parts.append("No trades placed this window.")

        return "\n".join(parts)

    # ── Shared Haiku API Call ──────────────────────────────────────────────

    def _call_haiku(self, prompt: str) -> str:
        """Synchronous Haiku API call -- run in executor to avoid blocking."""
        response = self._client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
