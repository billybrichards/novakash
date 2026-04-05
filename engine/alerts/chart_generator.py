"""
Chart Generator — Telegram-ready PNG charts for window reports.

Generates dark-theme matplotlib charts:
  - window_sparkline(): BTC price during a 5-min window, entry + outcome marked
  - daily_pnl_curve():  Cumulative P&L by window, win/loss markers
  - accuracy_bars():    Per-regime accuracy bar chart

All functions return bytes (PNG) suitable for Telegram sendPhoto.
No file I/O — everything in memory.
"""

from __future__ import annotations

import io
from typing import Optional

_DARK_BG = "#07070c"
_CARD = "#0f0f1a"
_BORDER = "#1a1a2e"
_TEXT = "#e2e8f0"
_MUTED = "#475569"
_GREEN = "#4ade80"
_RED = "#f87171"
_AMBER = "#fbbf24"
_CYAN = "#22d3ee"
_PURPLE = "#a855f7"
_WHITE = "#ffffff"


def window_sparkline(
    prices: list[float],          # BTC prices through the window (1 per second ideally)
    open_price: float,
    close_price: float,
    direction: str,               # "UP" or "DOWN"
    entry_price: float | None,    # Polymarket token price at entry (0-1)
    outcome: str | None,          # "WIN" or "LOSS" or None (pending)
    asset: str = "BTC",
    timeframe: str = "5m",
    window_ts: int | None = None,
    trade_placed: bool = False,
) -> bytes:
    """
    Generate a price sparkline for one 5-minute window.

    Returns PNG bytes.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import numpy as np

        fig, ax = plt.subplots(figsize=(8, 3), facecolor=_DARK_BG)
        ax.set_facecolor(_DARK_BG)

        x = list(range(len(prices)))

        # Price direction colour
        is_up = close_price >= open_price
        line_color = _GREEN if is_up else _RED
        outcome_color = _GREEN if outcome == "WIN" else (_RED if outcome == "LOSS" else _MUTED)

        # Outcome shading
        if outcome and trade_placed:
            ax.fill_between(x, min(prices) * 0.9999, max(prices) * 1.0001,
                            color=outcome_color, alpha=0.06, zorder=0)

        # Price line
        ax.plot(x, prices, color=line_color, linewidth=1.5, zorder=3)

        # Fill under line
        ax.fill_between(x, prices, min(prices) * 0.9999,
                        color=line_color, alpha=0.12, zorder=2)

        # Open price dashed line
        ax.axhline(open_price, color=_MUTED, linewidth=0.8, linestyle="--", zorder=1, alpha=0.6)
        ax.text(len(x) * 0.02, open_price, f"Open ${open_price:,.0f}",
                color=_MUTED, fontsize=7, va="bottom")

        # Close price marker
        close_color = _GREEN if is_up else _RED
        ax.scatter([len(x) - 1], [close_price], color=close_color, s=40, zorder=5)
        ax.text(len(x) * 0.98, close_price,
                f"${close_price:,.0f}", color=close_color, fontsize=7,
                va="bottom", ha="right")

        # Entry annotation
        if trade_placed and entry_price is not None:
            entry_text = f"{direction} @ ${entry_price:.3f}"
            ax.text(len(x) * 0.5, min(prices) * 0.99995,
                    entry_text, color=_AMBER, fontsize=8, ha="center",
                    style="italic")

        # Styling
        ax.set_xlim(0, len(x) - 1)
        y_range = max(prices) - min(prices)
        ax.set_ylim(min(prices) - y_range * 0.15, max(prices) + y_range * 0.15)

        for spine in ax.spines.values():
            spine.set_edgecolor(_BORDER)
        ax.tick_params(colors=_MUTED, labelsize=7)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
        ax.set_xticks([])

        # Title
        delta_pct = (close_price - open_price) / open_price * 100
        delta_sign = "+" if delta_pct >= 0 else ""
        title = f"{asset} {timeframe} — {delta_sign}{delta_pct:.3f}%"
        if outcome:
            title += f" — {'✓ WIN' if outcome == 'WIN' else '✗ LOSS'}"
        ax.set_title(title, color=_TEXT, fontsize=10, pad=6)

        fig.tight_layout(pad=0.5)

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=120, facecolor=_DARK_BG,
                    bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    except Exception:
        return b""


def daily_pnl_curve(
    windows: list[dict],   # list of {pnl, correct, ts, trade_placed}
    date_str: str = "",
) -> bytes:
    """
    Cumulative P&L curve for the day.

    Each item in windows: {pnl: float, correct: bool, ts: int, trade_placed: bool}
    Returns PNG bytes.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np

        fig, ax = plt.subplots(figsize=(10, 4), facecolor=_DARK_BG)
        ax.set_facecolor(_DARK_BG)

        traded = [w for w in windows if w.get("trade_placed")]
        if not traded:
            plt.close(fig)
            return b""

        xs = list(range(len(traded)))
        pnls = [w.get("pnl", 0) or 0 for w in traded]
        cumulative = []
        running = 0
        for p in pnls:
            running += p
            cumulative.append(running)

        # Line
        final = cumulative[-1] if cumulative else 0
        line_color = _GREEN if final >= 0 else _RED
        ax.plot(xs, cumulative, color=line_color, linewidth=2, zorder=3)
        ax.fill_between(xs, 0, cumulative,
                        where=[c >= 0 for c in cumulative],
                        color=_GREEN, alpha=0.12, zorder=2)
        ax.fill_between(xs, 0, cumulative,
                        where=[c < 0 for c in cumulative],
                        color=_RED, alpha=0.12, zorder=2)

        # Zero line
        ax.axhline(0, color=_MUTED, linewidth=0.8, linestyle="-", alpha=0.5, zorder=1)

        # Win/loss markers
        for i, w in enumerate(traded):
            if w.get("correct") is True:
                ax.scatter([i], [cumulative[i]], color=_GREEN, s=20, zorder=5, alpha=0.8)
            elif w.get("correct") is False:
                ax.scatter([i], [cumulative[i]], color=_RED, s=20, zorder=5, marker="x", alpha=0.8)

        # Styling
        ax.set_xlim(0, max(len(xs) - 1, 1))
        for spine in ax.spines.values():
            spine.set_edgecolor(_BORDER)
        ax.tick_params(colors=_MUTED, labelsize=8)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:+.2f}"))
        ax.set_xlabel("Trade #", color=_MUTED, fontsize=8)

        wins = sum(1 for w in traded if w.get("correct") is True)
        losses = sum(1 for w in traded if w.get("correct") is False)
        acc = wins / len(traded) * 100 if traded else 0
        sign = "+" if final >= 0 else ""
        title = f"Daily P&L — {sign}${final:.2f} | {wins}W/{losses}L ({acc:.0f}%)"
        if date_str:
            title = f"{date_str} — {title}"
        ax.set_title(title, color=_TEXT, fontsize=10, pad=6)

        fig.tight_layout(pad=0.5)

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=120, facecolor=_DARK_BG, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    except Exception:
        return b""


def accuracy_bars(
    regimes: dict[str, dict],   # {regime: {windows: int, wins: int}}
    title: str = "Accuracy by Regime",
) -> bytes:
    """
    Horizontal bar chart showing accuracy per regime.
    regimes: {"NORMAL": {"windows": 142, "wins": 141}, ...}
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        if not regimes:
            return b""

        labels = list(regimes.keys())
        accuracies = []
        ns = []
        for r in labels:
            d = regimes[r]
            w = d.get("wins", 0)
            n = d.get("windows", 1)
            accuracies.append(w / n * 100 if n > 0 else 0)
            ns.append(n)

        fig, ax = plt.subplots(figsize=(8, max(3, len(labels) * 0.8)), facecolor=_DARK_BG)
        ax.set_facecolor(_DARK_BG)

        colors = []
        for a in accuracies:
            if a >= 95:
                colors.append(_GREEN)
            elif a >= 80:
                colors.append(_AMBER)
            else:
                colors.append(_RED)

        bars = ax.barh(labels, accuracies, color=colors, alpha=0.8, height=0.6)

        # Labels
        for bar, acc, n in zip(bars, accuracies, ns):
            ax.text(min(acc + 1, 99), bar.get_y() + bar.get_height() / 2,
                    f"{acc:.1f}% (n={n})", va="center", color=_TEXT, fontsize=9)

        ax.axvline(50, color=_MUTED, linewidth=0.8, linestyle="--", alpha=0.5)
        ax.set_xlim(0, 105)
        ax.set_xlabel("Accuracy %", color=_MUTED, fontsize=9)
        ax.tick_params(colors=_MUTED, labelsize=9)
        for spine in ax.spines.values():
            spine.set_edgecolor(_BORDER)
        ax.set_title(title, color=_TEXT, fontsize=11, pad=8)

        fig.tight_layout(pad=0.6)

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=120, facecolor=_DARK_BG, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    except Exception:
        return b""
