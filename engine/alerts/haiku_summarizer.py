"""LLM-powered window summary generator for Telegram alerts.

Uses OpenRouter to produce human-readable 2-3 sentence summaries of
BTC trading windows. Falls back to a template-based summary
if the API call fails (rate limit, key missing, timeout).
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Optional

import structlog

from llm.openrouter import chat_completion

log = structlog.get_logger(__name__)


class HaikuSummarizer:
    """Generate human-readable window summaries via OpenRouter.

    Two entry points:
    - summarize_evaluation(): called at T-62 with strategy decisions
    - summarize_resolution(): called when a window resolves with outcome
    """

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._model = model or os.environ.get(
            "OPENROUTER_MODEL", "qwen/qwen-2.5-7b-instruct"
        )

    # ── T-62 Window Evaluation Summary ─────────────────────────────────────

    async def summarize_evaluation(self, context: dict) -> str:
        """Generate a human-readable evaluation summary for a trading window.

        Called at T-62 (final evaluation offset) with the full data surface
        and strategy decision results.
        """
        if not self._api_key:
            return self._fallback_evaluation(context)

        prompt = self._build_evaluation_prompt(context)
        try:
            summary = await chat_completion(
                api_key=self._api_key,
                model=self._model,
                prompt=prompt,
                max_tokens=220,
                temperature=0.2,
                timeout_s=15,
            )
            if not summary:
                return self._fallback_evaluation(context)
            return self._format_evaluation_message(context, summary)
        except Exception as exc:
            log.warning("haiku_summarizer.eval_api_error", error=str(exc)[:200])
            return self._fallback_evaluation(context)

    def _build_evaluation_prompt(self, ctx: dict) -> str:
        # Build full signal surface block
        surface_lines = [
            f"  CLOB: up_ask={ctx.get('clob_up_ask', '?')} dn_ask={ctx.get('clob_dn_ask', '?')} mid={ctx.get('clob_mid', '?')}",
            f"  Trade advised: {ctx.get('trade_advised', '?')} | V4 consensus: {ctx.get('v4_consensus', '?')} | V2 dir: {ctx.get('v2_direction', '?')}",
            f"  Open price: ${ctx.get('open_price', '?')} | Macro bias: {ctx.get('macro_bias', '?')}",
        ]
        surface_block = "\n".join(surface_lines)

        return (
            f"You are a crypto trading analyst. Write a concise Telegram window summary.\n\n"
            f"=== BTC {ctx.get('timescale', '5m')} | {ctx.get('window_time', '?')} UTC ===\n"
            f"Delta: {ctx.get('delta_pct', '?')}% | VPIN: {ctx.get('vpin', '?')} | Regime: {ctx.get('regime', '?')}\n"
            f"Model: P(UP)={ctx.get('p_up', '?')} dist={ctx.get('dist', '?')} → {ctx.get('model_direction', '?')}\n"
            f"Chainlink: {ctx.get('chainlink_delta', '?')}% | Tiingo: {ctx.get('tiingo_delta', '?')}% | Sources agree: {ctx.get('sources_agree', '?')}\n"
            f"Full surface:\n{surface_block}\n\n"
            f"=== Strategy decisions (each includes its gate config) ===\n"
            f"{ctx.get('decisions_text', 'none')}\n\n"
            f"Write TWO parts separated by exactly '---':\n\n"
            f"PART 1 (1 sentence): Market read — what BTC is doing this window and why the model favors {ctx.get('model_direction', '?')}. Include the key signal values.\n\n"
            f"PART 2: For EACH strategy, write '• [name]: [one plain-English sentence]' explaining:\n"
            f"  - If SKIPPED: WHY it skipped in human terms based on its specific gates and the current signal values (e.g. 'confidence dist=0.07 needs 0.12+', not just 'low confidence')\n"
            f"  - If TRADED: what signal triggered it and at what values\n"
            f"  - If GHOST: note it's monitoring only\n"
            f"Be specific with actual numbers from the surface. Max 15 words per strategy."
        )

    def _format_evaluation_message(self, ctx: dict, ai_summary: str) -> str:
        """Build the final Telegram message. ai_summary has PART1 --- PART2 format."""
        delta_str = (
            f"{ctx.get('delta_pct', '?')}%" if ctx.get("delta_pct") is not None else "?"
        )
        vpin_str = f"VPIN {ctx['vpin']:.2f}" if ctx.get("vpin") is not None else ""
        header = f"\U0001f550 BTC {ctx.get('timescale', '5m')} | {ctx.get('window_time', '?')} UTC | \u0394{delta_str}"
        if vpin_str:
            header += f" | {vpin_str}"

        # Split AI response into market read + per-strategy analysis
        if "---" in ai_summary:
            parts_split = ai_summary.split("---", 1)
            market_read = parts_split[0].strip()
            strategy_analysis = parts_split[1].strip()
            return "\n".join(
                [header, "", f"\U0001f4ca {market_read}", "", strategy_analysis]
            )
        else:
            # Fallback: AI didn't use separator, show as-is + template strategies
            strategy_lines = ctx.get("decision_lines", [])
            parts = [header, "", f"\U0001f4ca {ai_summary}"]
            if strategy_lines:
                parts.append("")
                parts.extend(strategy_lines)
            return "\n".join(parts)

    def _fallback_evaluation(self, ctx: dict) -> str:
        """Template-based fallback when Haiku API is unavailable."""
        delta_str = (
            f"{ctx.get('delta_pct', '?')}%" if ctx.get("delta_pct") is not None else "?"
        )
        vpin_str = f"VPIN {ctx['vpin']:.2f}" if ctx.get("vpin") is not None else ""
        regime = ctx.get("regime", "?")

        header = f"\U0001f550 BTC {ctx.get('timescale', '5m')} | {ctx.get('window_time', '?')} UTC | \u0394{delta_str}"
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
        if not self._api_key:
            return self._fallback_resolution(context)

        prompt = self._build_resolution_prompt(context)
        try:
            summary = await chat_completion(
                api_key=self._api_key,
                model=self._model,
                prompt=prompt,
                max_tokens=220,
                temperature=0.2,
                timeout_s=15,
            )
            if not summary:
                return self._fallback_resolution(context)
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
        delta_str = (
            f"{ctx.get('delta_pct', '?')}%" if ctx.get("delta_pct") is not None else "?"
        )

        header = (
            f"\u2705 BTC {ctx.get('timescale', '5m')} | {ctx.get('window_time', '?')} UTC | "
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
        delta_str = (
            f"{ctx.get('delta_pct', '?')}%" if ctx.get("delta_pct") is not None else "?"
        )

        header = (
            f"\u2705 BTC {ctx.get('timescale', '5m')} | {ctx.get('window_time', '?')} UTC | "
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
