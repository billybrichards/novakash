"""
Post-Resolution AI Evaluator — v8.0

After each 5-min window resolves on Polymarket, this evaluator runs a Sonnet
analysis of ALL evaluation ticks (the skip/trade records) to determine:

  1. Would we have won if we'd entered at each skip point?
  2. Are our caps too aggressive (blocking winning trades)?
  3. Are our gates too strict (skipping good signals)?

Called from the shadow_resolution_loop after oracle resolution.
Rate limited: max 1 analysis per 60 seconds to avoid API spam.
Only analyses windows from the last 15 minutes.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import structlog

from llm.openrouter import chat_completion

log = structlog.get_logger(__name__)

STAKE_USD = 8.0  # Assumed stake for P&L calculation
FEE_MULT = 0.98  # 2% fee on winnings
ANALYSIS_COOLDOWN = 60.0  # Seconds between analyses (rate limit)
MAX_WINDOW_AGE_SECS = 900  # Only analyse windows from last 15 minutes (900s)
SONNET_MODEL = "qwen/qwen-2.5-7b-instruct"
SONNET_MAX_TOKENS = 200
SYSTEM_PROMPT = (
    "You are a quantitative trading analyst reviewing 5-min Polymarket prediction market "
    "window evaluations. Be concise and specific."
)


class PostResolutionEvaluator:
    """
    Runs Sonnet analysis after each skipped window resolves.

    Usage:
        evaluator = PostResolutionEvaluator(api_key="...", db_client=db, alerter=alerter)
        await evaluator.analyse_window(window_ts, asset, timeframe, oracle_direction, eval_ticks)
    """

    def __init__(
        self,
        api_key: str,
        model: str = SONNET_MODEL,
        db_client=None,
        alerter=None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._db = db_client
        self._alerter = alerter
        self._last_analysis_ts: float = 0.0
        self._lock = asyncio.Lock()

    async def analyse_window(
        self,
        window_ts: int,
        asset: str,
        timeframe: str,
        oracle_direction: str,
        eval_ticks: Optional[list] = None,
    ) -> Optional[dict]:
        """
        Run post-resolution analysis for a single resolved window.

        Args:
            window_ts:         Window open timestamp (unix int)
            asset:             e.g. "BTC"
            timeframe:         e.g. "5m"
            oracle_direction:  "UP" or "DOWN"
            eval_ticks:        List of eval tick dicts (from window_eval_history or gate_audit)
                               Each tick: {offset, skip_reason, v2_probability, vpin, delta_pct,
                                           clob_ask, confidence, regime}

        Returns:
            dict with analysis results, or None if skipped (rate limit / no data)
        """
        async with self._lock:
            # ── Rate limit: max 1 analysis per 60 seconds ─────────────────────
            now = time.time()
            elapsed = now - self._last_analysis_ts
            if elapsed < ANALYSIS_COOLDOWN:
                log.debug(
                    "post_resolution.rate_limited",
                    wait_secs=f"{ANALYSIS_COOLDOWN - elapsed:.1f}",
                )
                return None

            # ── Age gate: only analyse windows from last 15 minutes ───────────
            window_age = now - window_ts
            if window_age > MAX_WINDOW_AGE_SECS:
                log.debug(
                    "post_resolution.window_too_old",
                    window_ts=window_ts,
                    age_secs=int(window_age),
                )
                return None

            # ── Fetch eval ticks from DB if not provided ──────────────────────
            if not eval_ticks:
                eval_ticks = await self._fetch_eval_ticks(window_ts, asset, timeframe)

            if not eval_ticks:
                log.debug("post_resolution.no_eval_ticks", window_ts=window_ts)
                return None

            self._last_analysis_ts = now

        # ── Compute per-tick P&L ──────────────────────────────────────────────
        enriched_ticks = _enrich_ticks(eval_ticks, oracle_direction)

        # Aggregate P&L stats
        missed_profit = sum(
            t["hypothetical_pnl"] for t in enriched_ticks if t.get("would_win") is True
        )
        blocked_loss = sum(
            abs(t["hypothetical_pnl"])
            for t in enriched_ticks
            if t.get("would_win") is False
        )
        cap_too_tight = _detect_cap_too_tight(enriched_ticks)

        # ── Build Sonnet prompt ────────────────────────────────────────────────
        prompt = _build_prompt(window_ts, oracle_direction, enriched_ticks)

        # ── Call Sonnet ────────────────────────────────────────────────────────
        ai_text = await _call_sonnet(
            api_key=self._api_key,
            model=self._model,
            prompt=prompt,
        )
        if not ai_text:
            log.warning("post_resolution.sonnet_failed", window_ts=window_ts)
            ai_text = "Analysis unavailable."

        # ── Extract gate recommendation from AI text ───────────────────────────
        gate_recommendation = _extract_gate_recommendation(ai_text)

        result = {
            "window_ts": window_ts,
            "asset": asset,
            "timeframe": timeframe,
            "oracle_direction": oracle_direction,
            "n_ticks": len(enriched_ticks),
            "missed_profit_usd": round(missed_profit, 2),
            "blocked_loss_usd": round(blocked_loss, 2),
            "cap_too_tight": cap_too_tight,
            "gate_recommendation": gate_recommendation,
            "ai_post_analysis": ai_text,
            "enriched_ticks": enriched_ticks,
        }

        # ── Persist to DB ──────────────────────────────────────────────────────
        if self._db:
            try:
                await self._db.store_post_resolution_analysis(result)
            except Exception as exc:
                log.warning("post_resolution.db_store_failed", error=str(exc)[:100])

        # ── Send Telegram alert ────────────────────────────────────────────────
        if self._alerter:
            try:
                await self._alerter.send_post_resolution_analysis(
                    window_id=f"{asset}-{window_ts}",
                    oracle_direction=oracle_direction,
                    eval_ticks=enriched_ticks,
                    ai_analysis=ai_text,
                    missed_profit=missed_profit,
                    blocked_loss=blocked_loss,
                    cap_too_tight=cap_too_tight,
                )
            except Exception as exc:
                log.warning("post_resolution.telegram_failed", error=str(exc)[:100])

        log.info(
            "post_resolution.complete",
            window_ts=window_ts,
            oracle=oracle_direction,
            n_ticks=len(enriched_ticks),
            missed_profit=f"+${missed_profit:.2f}",
            blocked_loss=f"-${blocked_loss:.2f}",
            cap_too_tight=cap_too_tight,
        )

        return result

    async def _fetch_eval_ticks(
        self,
        window_ts: int,
        asset: str,
        timeframe: str,
    ) -> list:
        """Fetch eval ticks from gate_audit table for a given window."""
        if not self._db or not self._db._pool:
            return []
        try:
            async with self._db._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        eval_offset   AS offset,
                        skip_reason,
                        vpin,
                        delta_pct,
                        regime,
                        gate_failed,
                        gate_passed
                    FROM gate_audit
                    WHERE window_ts = $1
                      AND asset     = $2
                      AND timeframe = $3
                    ORDER BY eval_offset DESC
                    """,
                    window_ts,
                    asset,
                    timeframe,
                )
                return [dict(r) for r in rows]
        except Exception as exc:
            log.debug("post_resolution.fetch_ticks_failed", error=str(exc)[:80])
            return []


# ─── Module-level helpers ─────────────────────────────────────────────────────


def _enrich_ticks(ticks: list, oracle_direction: str) -> list:
    """
    For each tick, compute hypothetical P&L if we'd entered at the clob_ask.

    Tick structure expected (all fields optional with defaults):
        offset, skip_reason, vpin, delta_pct, v2_probability, clob_ask, confidence, regime
    """
    enriched = []
    for tick in ticks:
        t = dict(tick)
        clob_ask = t.get("clob_ask")
        skip_reason = t.get("skip_reason") or "unknown"
        offset = t.get("offset") or 0

        # Infer our signal direction from delta_pct or skip_reason
        delta_pct = t.get("delta_pct") or 0.0
        signal_dir = "UP" if delta_pct > 0 else "DOWN"
        # Fallback: if v2_probability available
        v2_prob = t.get("v2_probability") or t.get("v2_p")
        if v2_prob is not None:
            signal_dir = "UP" if v2_prob > 0.5 else "DOWN"

        t["signal_dir"] = signal_dir
        t["would_win"] = None
        t["hypothetical_pnl"] = 0.0
        t["pnl_label"] = ""

        if clob_ask is not None and 0.01 < clob_ask < 0.99:
            would_win = signal_dir == oracle_direction
            t["would_win"] = would_win
            if would_win:
                # Win: (1 - clob_ask) * STAKE * fee_mult
                t["hypothetical_pnl"] = (1.0 - clob_ask) * STAKE_USD * FEE_MULT
                t["pnl_label"] = f"would WIN +${t['hypothetical_pnl']:.2f}"
            else:
                # Loss: -clob_ask * STAKE
                t["hypothetical_pnl"] = -(clob_ask * STAKE_USD)
                t["pnl_label"] = f"would LOSE ${t['hypothetical_pnl']:.2f}"
        else:
            t["pnl_label"] = "no CLOB price"

        enriched.append(t)
    return enriched


def _detect_cap_too_tight(enriched_ticks: list) -> bool:
    """
    Return True if any tick was blocked by a CLOB CAP skip reason
    AND would have been a winning trade.
    """
    for t in enriched_ticks:
        skip_reason = (t.get("skip_reason") or "").upper()
        if ("CLOB CAP" in skip_reason or "CAP:" in skip_reason) and t.get(
            "would_win"
        ) is True:
            return True
    return False


def _build_prompt(window_ts: int, oracle_direction: str, enriched_ticks: list) -> str:
    """Build the Sonnet evaluation prompt."""
    from datetime import datetime, timezone

    window_time = datetime.fromtimestamp(window_ts, tz=timezone.utc).strftime(
        "%H:%M UTC"
    )
    window_id = f"window_{window_ts}"

    lines = [
        f"Window {window_id} ({window_time}) resolved {oracle_direction}.",
        f"",
        f"Here are all {len(enriched_ticks)} evaluation ticks:",
    ]

    for t in enriched_ticks:
        offset = t.get("offset", "?")
        skip_reason = (t.get("skip_reason") or "unknown")[:60]
        vpin = t.get("vpin")
        delta = t.get("delta_pct")
        v2_prob = t.get("v2_probability") or t.get("v2_p")
        clob_ask = t.get("clob_ask")
        confidence = t.get("confidence", "?")
        pnl_label = t.get("pnl_label", "no CLOB price")

        parts = [f"T-{offset}:"]
        if skip_reason:
            parts.append(f"skip={skip_reason}")
        if vpin is not None:
            parts.append(f"vpin={vpin:.3f}")
        if delta is not None:
            parts.append(f"delta={delta:+.4f}%")
        if v2_prob is not None:
            parts.append(f"v2p={v2_prob:.3f}")
        if clob_ask is not None:
            parts.append(f"ask=${clob_ask:.3f}")
        if confidence:
            parts.append(f"conf={confidence}")
        parts.append(f"→ {pnl_label}")
        lines.append("  " + " | ".join(parts))

    # Count outcomes
    n_wins = sum(1 for t in enriched_ticks if t.get("would_win") is True)
    n_losses = sum(1 for t in enriched_ticks if t.get("would_win") is False)
    missed = sum(
        t["hypothetical_pnl"] for t in enriched_ticks if t.get("would_win") is True
    )
    avoided = sum(
        abs(t["hypothetical_pnl"])
        for t in enriched_ticks
        if t.get("would_win") is False
    )

    lines += [
        f"",
        f"Summary: {n_wins} would-win skips (+${missed:.2f} missed), "
        f"{n_losses} would-lose skips (-${avoided:.2f} avoided).",
        f"",
        f"In 2-3 sentences answer:",
        f"1. Were the skips correct? How many were good skips vs missed opportunities?",
        f"2. Should any cap be adjusted? Which specific cap and by how much?",
        f"3. Should any gate threshold change?",
    ]

    return "\n".join(lines)


def _extract_gate_recommendation(ai_text: str) -> Optional[str]:
    """Extract a short gate recommendation from the AI text."""
    if not ai_text:
        return None
    # Look for key action phrases
    lower = ai_text.lower()
    for keyword in [
        "raise",
        "lower",
        "increase",
        "decrease",
        "adjust",
        "tighten",
        "relax",
    ]:
        if keyword in lower:
            # Return first 150 chars of analysis as recommendation
            return ai_text[:150].strip()
    return None


async def _call_sonnet(api_key: str, model: str, prompt: str) -> Optional[str]:
    """Call OpenRouter and return the response text."""
    if not api_key:
        return None
    try:
        return await chat_completion(
            api_key=api_key,
            model=model,
            prompt=prompt,
            system_prompt=SYSTEM_PROMPT,
            max_tokens=SONNET_MAX_TOKENS,
            temperature=0.2,
            timeout_s=20,
        )
    except asyncio.TimeoutError:
        log.warning("sonnet.timeout")
        return None
    except Exception as exc:
        log.warning("sonnet.error", error=str(exc)[:100])
        return None
