"""Telegram window-summary formatter (adapter layer).

Renders a :class:`WindowSummaryContext` VO to plain Telegram text.
Kept separate from the Haiku summarizer — the Haiku path consumes the
same VO for rich narrative enrichment; this formatter is the
deterministic fallback + structured body the Haiku call is anchored on.

PR: C (clean-arch extraction).
"""

from __future__ import annotations

from domain.value_objects import (
    SummaryDecisionLine,
    WindowSummaryContext,
)


def format_window_summary(ctx: WindowSummaryContext) -> str:
    """Render the VO to a single Telegram-ready string.

    Output shape (empty sections are omitted):

        Open $74,252.10 | Now $74,430.80 | T-62
        ↕ sources: YES (DOWN)

        Eligible now (tradable):
          v10_gate (LIVE): TRADE DOWN | conf=HIGH

        Blocked by signal:
          v4_up_basic (LIVE): confidence: dist=0.074 < 0.12

        Blocked by execution timing:
          v4_fusion (LIVE): polymarket: timing=optimal T-62 -- too late (<70s), skip

        Off-window this offset:
          v15m_fusion (LIVE) outside window [T-240..T-480]

        Already traded this window:
          v15m_gate (LIVE): traded at T-312

        Inactive (GHOST shadow):
          v10_gate, v4_up_asian
    """
    lines: list[str] = []

    # Window header
    header_bits: list[str] = []
    if ctx.open_price is not None:
        header_bits.append(f"Open ${ctx.open_price:,.2f}")
    if ctx.current_price is not None:
        header_bits.append(f"Now ${ctx.current_price:,.2f}")
    header_bits.append(f"T-{ctx.eval_offset}")
    lines.append(" | ".join(header_bits))

    if ctx.sources_agree:
        lines.append(f"↕ sources: {ctx.sources_agree}")

    def _section(title: str, rows: tuple[SummaryDecisionLine, ...]) -> None:
        if not rows:
            return
        if lines:
            lines.append("")
        lines.append(title)
        for r in rows:
            lines.append(f"  {r.strategy_id} ({r.mode}): {r.text}")

    _section("Eligible now (tradable):", ctx.eligible)
    _section("Blocked by signal:", ctx.blocked_signal)
    _section("Blocked by execution timing:", ctx.blocked_exec_timing)
    _section("Off-window this offset:", ctx.off_window)
    _section("Window expired without trade:", ctx.window_expired)
    _section("Already traded this window:", ctx.already_traded)

    if ctx.ghost_shadow:
        if lines:
            lines.append("")
        lines.append("Inactive (GHOST shadow):")
        # Collapsed single-line list — don't repeat mode tag per item.
        lines.append("  " + ", ".join(r.strategy_id for r in ctx.ghost_shadow))

    return "\n".join(lines)
