"""
Pure builder for the position snapshot — no IO, fully unit-testable.

The same dict shape is consumed by:
  - engine/alerts/telegram.py        → send_position_snapshot()
  - hub/api/positions.py             → GET /api/positions/snapshot
  - frontend PositionSnapshotBar.jsx → live top-bar render

Inputs come from callers (runtime / Hub service layer) which know how to
fetch wallet USDC, redeemer state, and pending-wins from poly_fills + trades.
"""
from __future__ import annotations

from typing import TypedDict


OVERDUE_THRESHOLD_SECONDS = 5 * 60  # NegRisk auto-redeem typical SLA = 1–5 min


class PendingWin(TypedDict):
    condition_id: str
    value: float
    window_end_utc: str
    overdue_seconds: int


class CooldownState(TypedDict):
    active: bool
    remaining_seconds: int
    resets_at: str | None
    reason: str


def _is_overdue(win: dict) -> bool:
    """Single source of truth for the overdue predicate.

    Used by both the `overdue_count` aggregation in build_snapshot AND
    the per-row 🚨 OVERDUE tag in render_snapshot_text — extracting this
    helper prevents the two call sites from drifting apart.
    """
    return int(win.get("overdue_seconds", 0)) > OVERDUE_THRESHOLD_SECONDS


def build_snapshot(
    wallet_usdc: float,
    pending_wins: list[PendingWin],
    open_orders: list[dict],
    cooldown: CooldownState,
    daily_quota_limit: int,
    quota_used_today: int,
    now_utc: str,
) -> dict:
    # Sum at full float precision and round ONCE at the end, so that
    # round(round(a, 2) + round(b, 2), 2) drift on cent-precision floats
    # never creeps into the user-visible effective balance.
    pending_total_raw = sum(float(w["value"]) for w in pending_wins)
    pending_total = round(pending_total_raw, 2)
    effective = round(wallet_usdc + pending_total_raw, 2)  # sum raw, round once
    overdue_count = sum(1 for w in pending_wins if _is_overdue(w))
    quota_remaining = max(0, int(daily_quota_limit) - int(quota_used_today))
    return {
        "now_utc": now_utc,
        "wallet_usdc": round(wallet_usdc, 2),
        "pending_wins": pending_wins,
        "pending_count": len(pending_wins),
        "pending_total_usd": pending_total,
        "overdue_count": overdue_count,
        "effective_balance": effective,
        "open_orders": open_orders,
        "open_orders_count": len(open_orders),
        "cooldown": cooldown,
        "daily_quota_limit": daily_quota_limit,
        "quota_used_today": quota_used_today,
        "quota_remaining": quota_remaining,
    }


def _fmt_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60}m"


def render_snapshot_text(snap: dict) -> str:
    """
    Render a Telegram-ready Markdown block. Stays under the Telegram
    4096-char limit — pending_wins is capped at 8 rows; overflow is
    summarised in a footer line.
    """
    lines: list[str] = []
    lines.append(f"📊 *POSITION SNAPSHOT* | {snap['now_utc'][:16]}Z")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(
        f"Wallet: `${snap['wallet_usdc']:.2f}` "
        f"| Pending: `${snap['pending_total_usd']:.2f}` ({snap['pending_count']} pending) "
        f"| *Effective: `${snap['effective_balance']:.2f}`*"
    )

    if snap["pending_wins"]:
        lines.append("")
        lines.append("*Unredeemed wins:*")
        for w in snap["pending_wins"][:8]:
            cid = w["condition_id"]
            short = (cid[:10] + "…") if len(cid) > 12 else cid
            age = _fmt_age(int(w.get("overdue_seconds", 0)))
            tag = "🚨 OVERDUE" if _is_overdue(w) else "⏳"
            lines.append(
                f"  {tag} `{short}` `${float(w['value']):.2f}` `{age}` since close"
            )
        if len(snap["pending_wins"]) > 8:
            lines.append(f"  …+{len(snap['pending_wins']) - 8} more")
        if snap["overdue_count"]:
            lines.append(
                f"⚠️ `{snap['overdue_count']}` overdue >5min — NegRisk slow"
            )

    lines.append("")
    cd = snap["cooldown"]
    if cd["active"]:
        lines.append(
            f"🔴 *RELAYER COOLDOWN* — {_fmt_age(int(cd['remaining_seconds']))} left "
            f"| `{snap['quota_remaining']}/{snap['daily_quota_limit']} quota left`"
        )
        if cd.get("reason"):
            lines.append(f"  reason: `{cd['reason'][:60]}`")
    else:
        lines.append(
            f"🟢 Relayer OK | `{snap['quota_remaining']}/{snap['daily_quota_limit']} quota left`"
        )

    if snap["open_orders_count"]:
        lines.append(f"📋 Open orders: `{snap['open_orders_count']}`")

    # 30-min activity digest (optional — only render when caller passed
    # it in via the "activity_digest" key). Keeps positions.py free of
    # IO: the caller fetches from data-api and injects the payload dict.
    digest = snap.get("activity_digest")
    if digest:
        lines.append("")
        lines.append("*Last 30m:*")
        buy_n = digest.get("trade_buy_count", 0)
        sell_n = digest.get("trade_sell_count", 0)
        win_n = digest.get("redeem_win_count", 0)
        dust_n = digest.get("redeem_dust_count", 0)
        if buy_n == 0 and sell_n == 0 and win_n == 0 and dust_n == 0:
            lines.append("  _no activity_")
        else:
            if buy_n or sell_n:
                lines.append(
                    f"  🟢 BUY `{buy_n}` (`${digest.get('trade_buy_usd', 0):.2f}`)  "
                    f"🔴 SELL `{sell_n}` (`${digest.get('trade_sell_usd', 0):.2f}`)"
                )
            if win_n or dust_n:
                lines.append(
                    f"  🏆 WIN redeem `{win_n}` (`${digest.get('redeem_win_usd', 0):.2f}`)  "
                    f"🗑 dust `{dust_n}`"
                )

    return "\n".join(lines)
