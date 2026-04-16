# Telegram Redemption & Position Visibility — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the Telegram alerts + `/telegram` Hub page so unredeemed wins, NegRisk overdue payouts, relayer cooldowns, and multi-fill (FAK split) events are immediately obvious without ever opening Polymarket. Effective balance (cash + pending wins) must be visible at all times.

**Architecture:** Three new/enhanced engine TG alert types feed `telegram_notifications`; a new Hub endpoint `GET /api/positions/snapshot` derives effective-balance + pending-wins + relayer-cooldown from existing tables (`poly_fills`, `trade_bible`, redeemer state); the Telegram frontend page (from `2026-04-14-telegram-dashboard.md`) gains a sticky top bar that renders that snapshot live.

**Tech Stack:** Python 3.12 (engine), FastAPI + SQLAlchemy `text()` (Hub), React 18 + Vite (frontend), PostgreSQL 16 (Railway), Polymarket data-api for ground-truth fills, builder-relayer for redemptions.

**Builds on:** `docs/superpowers/plans/2026-04-14-telegram-dashboard.md` (the base `/telegram` page must exist first; if not, deliver Tasks 1–4 of that plan before starting Task 6 here).

---

## Background — what the user actually needs

Live operational evidence (2026-04-16 morning, 09:31–10:46 UTC):

- **6 winning positions ($47.65)** were sitting "stuck" — `redeemable=True` on chain, but USDC had not landed in the wallet because NegRisk auto-redeem batches were running >45 min late.
- The Telegram feed had no surface for *pending wins*, so the wallet looked low ($135.57) when the effective balance was actually ~$183.22.
- The earlier agent miscounted held wins as losses because it had no way to see which positions were redeemed vs. waiting.
- A 09:58 trade was a **single FAK order with two partial fills at $0.750** (split across the layered ask book). The current `order_filled` alert renders this as one fill — the fact that there were two on-chain rows for the same `condition_id` was invisible.
- The redemption sweep alert (runtime.py:3185) reports `Redeemed: N | Failed: N` but does not say *which conditions* failed, *whether the relayer cooldown is active*, or *how many quota units remain*. With expected frequent 429 rate-limit hits, this needs a dedicated card.

**Symptom we are solving:** "wallet looks empty, did the engine break?" → answered at a glance from Telegram.

---

## Files Touched

**New:**
- `engine/alerts/positions.py` — pure builder for the position-snapshot text + dict (no IO).
- `hub/api/positions.py` — `GET /api/positions/snapshot`.
- `frontend/src/pages/telegram/PositionSnapshotBar.jsx` — sticky top bar consumed by `Telegram.jsx`.
- `engine/tests/alerts/test_positions_snapshot.py` — unit tests for the builder.
- `hub/tests/api/test_positions.py` — endpoint tests.

**Modified:**
- `engine/alerts/telegram.py` — add `send_position_snapshot()`, `send_relayer_cooldown()`; enhance `send_order_filled()` to detect + display multi-fills; enhance redemption-sweep text in runtime.py:3185 region.
- `engine/infrastructure/runtime.py` — wire periodic snapshot loop (15 min cadence), relayer-cooldown trip detection, multi-fill lookup before sending `order_filled`.
- `engine/execution/redeemer.py` — expose `pending_wins_summary()` returning the redeemable-but-not-yet-redeemed list with overdue ages.
- `hub/main.py` — register the `positions` router.
- `frontend/src/pages/Telegram.jsx` — mount `<PositionSnapshotBar />` at top, add new card colours for `position_snapshot` / `relayer_cooldown` / `redemption_sweep`.

**Read-only (reference, no edit):**
- `hub/db/migrations/versions/20260410_01_poly_fills.sql` — already has `is_multi_fill`, `multi_fill_index`, `multi_fill_total`, `condition_id`. No new columns needed.
- `engine/execution/redeemer.py:222` — existing `cooldown_status()` returns `{active, remaining_seconds, resets_at, reason}`.

---

## Task 1: Position-snapshot dict + Telegram text builder (pure)

**Why first:** Pure function with no IO. Locks the data shape so Hub endpoint, TG alert, and frontend bar all agree.

**Files:**
- Create: `engine/alerts/positions.py`
- Test: `engine/tests/alerts/test_positions_snapshot.py`

- [ ] **Step 1: Write the failing test**

```python
# engine/tests/alerts/test_positions_snapshot.py
from engine.alerts.positions import build_snapshot, render_snapshot_text


def test_build_snapshot_computes_effective_balance():
    snap = build_snapshot(
        wallet_usdc=135.57,
        pending_wins=[
            {"condition_id": "0xaaa", "value": 7.40, "window_end_utc": "2026-04-16T09:35:00Z", "overdue_seconds": 5700},
            {"condition_id": "0xbbb", "value": 7.18, "window_end_utc": "2026-04-16T09:45:00Z", "overdue_seconds": 5100},
        ],
        open_orders=[],
        cooldown={"active": False, "remaining_seconds": 0, "resets_at": None, "reason": ""},
        daily_quota_limit=100,
        quota_used_today=12,
        now_utc="2026-04-16T11:10:00Z",
    )
    assert snap["wallet_usdc"] == 135.57
    assert snap["pending_total_usd"] == 14.58
    assert snap["effective_balance"] == 150.15
    assert snap["pending_count"] == 2
    assert snap["overdue_count"] == 2  # both > 5min past window_end
    assert snap["cooldown"]["active"] is False
    assert snap["quota_remaining"] == 88


def test_render_snapshot_text_marks_overdue_wins():
    snap = build_snapshot(
        wallet_usdc=135.57,
        pending_wins=[
            {"condition_id": "0xaaa", "value": 7.40, "window_end_utc": "2026-04-16T09:35:00Z", "overdue_seconds": 5700},
        ],
        open_orders=[],
        cooldown={"active": False, "remaining_seconds": 0, "resets_at": None, "reason": ""},
        daily_quota_limit=100,
        quota_used_today=0,
        now_utc="2026-04-16T11:10:00Z",
    )
    text = render_snapshot_text(snap)
    assert "$135.57" in text
    assert "$142.97" in text  # effective
    assert "1 pending" in text
    assert "OVERDUE" in text  # >5min past window_end


def test_render_snapshot_text_shows_cooldown_when_active():
    snap = build_snapshot(
        wallet_usdc=200.0,
        pending_wins=[],
        open_orders=[],
        cooldown={"active": True, "remaining_seconds": 1800, "resets_at": "2026-04-16T11:40:00Z", "reason": "429 quota exceeded"},
        daily_quota_limit=100,
        quota_used_today=100,
        now_utc="2026-04-16T11:10:00Z",
    )
    text = render_snapshot_text(snap)
    assert "RELAYER COOLDOWN" in text
    assert "30m" in text  # 1800s formatted as 30m
    assert "0/100 quota left" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest engine/tests/alerts/test_positions_snapshot.py -v`
Expected: FAIL — `ModuleNotFoundError: engine.alerts.positions`

- [ ] **Step 3: Implement the builder**

```python
# engine/alerts/positions.py
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


def build_snapshot(
    wallet_usdc: float,
    pending_wins: list[PendingWin],
    open_orders: list[dict],
    cooldown: CooldownState,
    daily_quota_limit: int,
    quota_used_today: int,
    now_utc: str,
) -> dict:
    pending_total = round(sum(float(w["value"]) for w in pending_wins), 2)
    effective = round(wallet_usdc + pending_total, 2)
    overdue_count = sum(
        1 for w in pending_wins if int(w.get("overdue_seconds", 0)) > OVERDUE_THRESHOLD_SECONDS
    )
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
        f"| Pending: `${snap['pending_total_usd']:.2f}` ({snap['pending_count']} wins) "
        f"| *Effective: `${snap['effective_balance']:.2f}`*"
    )

    if snap["pending_wins"]:
        lines.append("")
        lines.append("*Unredeemed wins:*")
        for w in snap["pending_wins"][:8]:
            cid = w["condition_id"]
            short = (cid[:10] + "…") if len(cid) > 12 else cid
            age = _fmt_age(int(w.get("overdue_seconds", 0)))
            tag = "🚨 OVERDUE" if int(w.get("overdue_seconds", 0)) > OVERDUE_THRESHOLD_SECONDS else "⏳"
            lines.append(f"  {tag} `{short}` `${float(w['value']):.2f}` `{age}` since close")
        if len(snap["pending_wins"]) > 8:
            lines.append(f"  …+{len(snap['pending_wins']) - 8} more")
        if snap["overdue_count"]:
            lines.append(f"⚠️ `{snap['overdue_count']}` overdue >5min — NegRisk slow")

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

    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest engine/tests/alerts/test_positions_snapshot.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add engine/alerts/positions.py engine/tests/alerts/test_positions_snapshot.py
git commit -m "feat(alerts): pure position-snapshot builder with overdue + cooldown"
```

---

## Task 2: Redeemer exposes pending-wins summary

**Why:** Task 1's builder needs `pending_wins[]` from somewhere. The Redeemer already scans on-chain redeemable positions — surface that scan as a summary the runtime can pass to `build_snapshot()`.

**Files:**
- Modify: `engine/execution/redeemer.py` (add method `pending_wins_summary()`)
- Test: `engine/tests/execution/test_redeemer_pending_summary.py`

- [ ] **Step 1: Read existing scan code in redeemer**

Run: `grep -n "redeemable" engine/execution/redeemer.py | head -20`
Identify the scan that builds the redeemable position list (the same one feeding the sweep loop). That list — `[{condition_id, value, resolved_at}]` — is what the new method must return, with `overdue_seconds` derived from `resolved_at`.

- [ ] **Step 2: Write the failing test**

```python
# engine/tests/execution/test_redeemer_pending_summary.py
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from engine.execution.redeemer import Redeemer


async def test_pending_wins_summary_marks_overdue(monkeypatch):
    r = Redeemer(paper_mode=True)
    now = datetime(2026, 4, 16, 11, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        r, "_scan_redeemable_positions",
        AsyncMock(return_value=[
            {"condition_id": "0xaaa", "value": 7.40, "resolved_at": now - timedelta(minutes=95)},
            {"condition_id": "0xbbb", "value": 4.50, "resolved_at": now - timedelta(minutes=2)},
        ]),
    )
    summary = await r.pending_wins_summary(now=now)
    assert len(summary) == 2
    assert summary[0]["condition_id"] == "0xaaa"
    assert summary[0]["overdue_seconds"] == 5700  # 95min × 60
    assert summary[1]["overdue_seconds"] == 120
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest engine/tests/execution/test_redeemer_pending_summary.py -v`
Expected: FAIL — `AttributeError: pending_wins_summary` (or scan method missing).

- [ ] **Step 4: Implement `pending_wins_summary`**

Add to `engine/execution/redeemer.py` (near `cooldown_status` at line ~222):

```python
async def pending_wins_summary(
    self,
    now: Optional[datetime] = None,
) -> list[dict]:
    """
    Return the list of redeemable positions that have NOT yet been redeemed,
    each annotated with how long it has been waiting since market resolution.

    Used by the position-snapshot Telegram alert and the Hub
    /api/positions/snapshot endpoint. Read-only — does NOT trigger
    redemption and does NOT consume a relayer quota unit.
    """
    now = now or datetime.now(timezone.utc)
    try:
        positions = await self._scan_redeemable_positions()
    except Exception as exc:
        self._log.warning("redeemer.pending_summary_scan_failed", error=str(exc)[:120])
        return []

    out: list[dict] = []
    for p in positions:
        resolved_at = p.get("resolved_at")
        if resolved_at is None:
            overdue = 0
        else:
            overdue = max(0, int((now - resolved_at).total_seconds()))
        out.append({
            "condition_id": p["condition_id"],
            "value": float(p.get("value", 0.0)),
            "window_end_utc": resolved_at.isoformat() if resolved_at else None,
            "overdue_seconds": overdue,
        })
    # Newest-first → oldest first (worst overdue at the top of the list)
    out.sort(key=lambda x: x["overdue_seconds"], reverse=True)
    return out
```

If `_scan_redeemable_positions()` does not already exist as a method, extract the scan loop currently inside `redeem_wins()` into a helper that both `redeem_wins()` and `pending_wins_summary()` call.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest engine/tests/execution/test_redeemer_pending_summary.py -v`
Expected: 1 passed.

- [ ] **Step 6: Commit**

```bash
git add engine/execution/redeemer.py engine/tests/execution/test_redeemer_pending_summary.py
git commit -m "feat(redeemer): expose pending_wins_summary() for snapshot alerts"
```

---

## Task 3: New `send_position_snapshot()` TG alert

**Files:**
- Modify: `engine/alerts/telegram.py` (add method around line ~990, alongside `send_trade_result`)
- Test: `engine/tests/alerts/test_send_position_snapshot.py`

- [ ] **Step 1: Write the failing test**

```python
# engine/tests/alerts/test_send_position_snapshot.py
from unittest.mock import AsyncMock, MagicMock
from engine.alerts.telegram import TelegramAlerter


async def test_send_position_snapshot_logs_notification():
    a = TelegramAlerter(bot_token="x", chat_id="y", engine_version="test")
    a._send_with_id = AsyncMock(return_value=999)
    a._log_notification = AsyncMock()
    snap = {
        "now_utc": "2026-04-16T11:10:00Z",
        "wallet_usdc": 135.57,
        "pending_wins": [],
        "pending_count": 0,
        "pending_total_usd": 0.0,
        "overdue_count": 0,
        "effective_balance": 135.57,
        "open_orders": [],
        "open_orders_count": 0,
        "cooldown": {"active": False, "remaining_seconds": 0, "resets_at": None, "reason": ""},
        "daily_quota_limit": 100,
        "quota_used_today": 0,
        "quota_remaining": 100,
    }
    msg_id = await a.send_position_snapshot(snap)
    assert msg_id == 999
    args, kwargs = a._log_notification.call_args
    assert args[0] == "position_snapshot"
    assert "POSITION SNAPSHOT" in args[1]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest engine/tests/alerts/test_send_position_snapshot.py -v`
Expected: FAIL — `AttributeError: send_position_snapshot`.

- [ ] **Step 3: Implement `send_position_snapshot`**

Add to `engine/alerts/telegram.py` after `send_order_filled`:

```python
async def send_position_snapshot(self, snap: dict) -> Optional[int]:
    """
    📊 POSITION SNAPSHOT — periodic + on-demand visibility on wallet,
    pending wins, relayer cooldown, and open orders. The dict comes
    from engine.alerts.positions.build_snapshot().
    """
    try:
        from engine.alerts.positions import render_snapshot_text
        text = render_snapshot_text(snap)
        msg_id = await self._send_with_id(text)
        await self._log_notification(
            "position_snapshot", text, telegram_message_id=msg_id
        )
        return msg_id
    except Exception as exc:
        self._log.warning("telegram.send_position_snapshot_failed", error=str(exc)[:100])
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest engine/tests/alerts/test_send_position_snapshot.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add engine/alerts/telegram.py engine/tests/alerts/test_send_position_snapshot.py
git commit -m "feat(alerts): send_position_snapshot for TG visibility"
```

---

## Task 4: Periodic snapshot loop in runtime + on-demand trigger

**Files:**
- Modify: `engine/infrastructure/runtime.py` (new `_position_snapshot_loop`, register in `start()`)

- [ ] **Step 1: Add the loop**

In `engine/infrastructure/runtime.py`, near `_redeemer_loop` (around line 3030), add:

```python
async def _position_snapshot_loop(self) -> None:
    """
    Periodic position snapshot — every 15 min plus immediately after
    every redemption sweep. Cheap (no relayer hit) since pending_wins
    just reads positions already cached by the redeemer scan.
    """
    SNAPSHOT_INTERVAL = 900  # 15 min
    while not self._shutdown_event.is_set():
        try:
            await self._send_position_snapshot()
        except Exception as e:
            log.error("orchestrator.snapshot_loop.error", error=str(e))

        try:
            await asyncio.wait_for(
                asyncio.shield(self._shutdown_event.wait()),
                timeout=SNAPSHOT_INTERVAL,
            )
            break
        except asyncio.TimeoutError:
            pass


async def _send_position_snapshot(self) -> None:
    """
    Build the snapshot from current state + push to Telegram.
    Also called on-demand from Hub via _check_snapshot_requested().
    """
    if not (self._alerter and self._redeemer):
        return
    from engine.alerts.positions import build_snapshot
    from datetime import datetime, timezone

    wallet_usdc = 0.0
    if self._poly_client and hasattr(self._poly_client, "get_usdc_balance"):
        try:
            wallet_usdc = float(await self._poly_client.get_usdc_balance() or 0)
        except Exception:
            pass

    pending = await self._redeemer.pending_wins_summary()
    cooldown = self._redeemer.cooldown_status()
    quota_used = await self._db.count_redeems_today() if hasattr(self._db, "count_redeems_today") else 0
    open_orders = await self._poly_client.list_open_orders() if hasattr(self._poly_client, "list_open_orders") else []

    snap = build_snapshot(
        wallet_usdc=wallet_usdc,
        pending_wins=pending,
        open_orders=open_orders,
        cooldown=cooldown,
        daily_quota_limit=self._redeemer.daily_quota_limit,
        quota_used_today=quota_used,
        now_utc=datetime.now(timezone.utc).isoformat(),
    )
    await self._alerter.send_position_snapshot(snap)
```

- [ ] **Step 2: Register the loop in `start()`**

Find the existing `asyncio.create_task(self._redeemer_loop())` call in the orchestrator's `start()`. Immediately after it add:

```python
self._tasks.append(asyncio.create_task(self._position_snapshot_loop()))
```

- [ ] **Step 3: Trigger snapshot after every sweep**

In the redeemer-loop block at `runtime.py:3170` (right after the existing `if redeemed > 0 or failed > 0:` Telegram send), append:

```python
# After every sweep — send a fresh snapshot so the user sees the
# pending-wins list shrink in real time.
try:
    await self._send_position_snapshot()
except Exception:
    pass
```

- [ ] **Step 4: Smoke test the loop**

Run: `pytest engine/tests/infrastructure/ -k snapshot -v` (if a runtime test harness exists; otherwise rely on Task 1+3 unit coverage and validate live in Task 10).

- [ ] **Step 5: Commit**

```bash
git add engine/infrastructure/runtime.py
git commit -m "feat(runtime): periodic position-snapshot loop + post-sweep trigger"
```

---

## Task 5: Multi-fill detection in `send_order_filled`

**Why:** A FAK order that splits into N partial fills currently shows as one TG message. We want the message to say "2 fills @ $0.750 (FAK split — same condition_id)" with the per-row breakdown.

**Files:**
- Modify: `engine/alerts/telegram.py` (`send_order_filled` at line 935)
- Modify: `engine/infrastructure/runtime.py` (caller — fetch `condition_id` fills before the alert)
- Test: `engine/tests/alerts/test_order_filled_multi.py`

- [ ] **Step 1: Write the failing test**

```python
# engine/tests/alerts/test_order_filled_multi.py
from unittest.mock import AsyncMock, MagicMock
from engine.alerts.telegram import TelegramAlerter


async def test_send_order_filled_renders_multi_fill_breakdown():
    a = TelegramAlerter(bot_token="x", chat_id="y", engine_version="test")
    a._send_with_id = AsyncMock(return_value=42)
    a._log_notification = AsyncMock()
    order = MagicMock(direction="NO", stake_usd=4.98, order_id="abc")
    fills = [
        {"price": 0.750, "size": 0.74, "tx": "0x111"},
        {"price": 0.750, "size": 5.90, "tx": "0x222"},
    ]
    await a.send_order_filled(
        order, fill_price=0.750, shares=6.64, fills=fills,
    )
    text = a._send_with_id.call_args.args[0]
    assert "FAK split" in text
    assert "2 fills" in text
    assert "0.74" in text
    assert "5.90" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest engine/tests/alerts/test_order_filled_multi.py -v`
Expected: FAIL — current signature has no `fills` param.

- [ ] **Step 3: Update `send_order_filled` signature + body**

In `engine/alerts/telegram.py` replace the `send_order_filled` body (line 935) — add the optional `fills` arg and the multi-fill block. Keep the existing single-fill output unchanged when `fills` is None or len ≤ 1:

```python
async def send_order_filled(
    self,
    order,
    fill_price: float,
    shares: float,
    gamma_at_fill: Optional[dict] = None,
    gamma_at_decision: Optional[dict] = None,
    ai_text: Optional[str] = None,
    fills: Optional[list[dict]] = None,
) -> Optional[int]:
    try:
        direction = "DOWN" if getattr(order, "direction", "") == "NO" else "UP"
        cost = fill_price * shares
        rr = round((1 - fill_price) / fill_price, 1) if fill_price > 0 else 0
        profit_if_win = round((1 - fill_price) * shares * 0.98, 2)

        fok_step = getattr(order, "fok_fill_step", None)
        fok_attempts = getattr(order, "fok_attempts", None)
        delta_source = getattr(order, "delta_source", "?")
        src_short = (
            (delta_source or "?").replace("_rest_candle", "").replace("_db_tick", "(db)")
        )

        fok_line = ""
        if fok_step is not None and fok_attempts is not None:
            fok_line = f"⚡ FOK step `{fok_step}/{fok_attempts}`\n"

        # Multi-fill block — only when the engine passes a fills list of len > 1.
        multi_block = ""
        if fills and len(fills) > 1:
            rows = "\n".join(
                f"  • `${float(f['price']):.4f}` × `{float(f['size']):.2f}`"
                for f in fills[:6]
            )
            extra = f"\n  …+{len(fills) - 6} more" if len(fills) > 6 else ""
            multi_block = (
                f"🧩 *FAK split* — {len(fills)} fills, same condition_id\n"
                f"{rows}{extra}\n"
            )

        text = (
            f"💰 *FILLED* — BTC 5m {direction} | {self._engine_version}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Fill: `${fill_price:.4f}` × `{shares:.2f}` shares\n"
            f"Cost: `${cost:.2f}` | R/R `1:{rr}`\n"
            f"If WIN: `+${profit_if_win:.2f}`\n"
            f"{fok_line}"
            f"{multi_block}"
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
```

- [ ] **Step 4: Wire the fills lookup at the caller**

Find the single existing call site of `send_order_filled` in the engine (likely in `engine/infrastructure/runtime.py` or `engine/strategies/*orchestrator*.py`). Just before the call, fetch the rows from `poly_fills` for the same `condition_id` matched within the last 60 s:

```python
fills = []
try:
    fills = await self._db.fetch_recent_fills_for_condition(
        condition_id=getattr(order, "condition_id", None),
        within_seconds=60,
    )
except Exception:
    pass
await self._alerter.send_order_filled(order, fill_price, shares, fills=fills)
```

If `fetch_recent_fills_for_condition` is missing on the DB client, add it as a thin wrapper:

```sql
SELECT price, size, transaction_hash AS tx
FROM poly_fills
WHERE condition_id = $1
  AND match_time_utc >= NOW() - make_interval(secs => $2)
ORDER BY match_time_utc ASC
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest engine/tests/alerts/test_order_filled_multi.py -v`
Expected: 1 passed. Also re-run any pre-existing `send_order_filled` test to confirm the single-fill branch is unchanged.

- [ ] **Step 6: Commit**

```bash
git add engine/alerts/telegram.py engine/infrastructure/runtime.py engine/persistence/db_client.py engine/tests/alerts/test_order_filled_multi.py
git commit -m "feat(alerts): show FAK split-fill breakdown on order_filled"
```

---

## Task 6: Relayer-cooldown TG alert (one-shot on trip + clear on resume)

**Why:** User explicitly expects to hit the 429 quota frequently. A separate, loud, one-shot card is more useful than a buried line in the snapshot.

**Files:**
- Modify: `engine/alerts/telegram.py` (new `send_relayer_cooldown` + `send_relayer_resumed`)
- Modify: `engine/infrastructure/runtime.py` (state-edge detection in `_redeemer_loop`)
- Test: `engine/tests/alerts/test_relayer_cooldown.py`

- [ ] **Step 1: Write the failing test**

```python
# engine/tests/alerts/test_relayer_cooldown.py
from unittest.mock import AsyncMock
from engine.alerts.telegram import TelegramAlerter


async def test_send_relayer_cooldown_message():
    a = TelegramAlerter(bot_token="x", chat_id="y", engine_version="test")
    a._send_with_id = AsyncMock(return_value=11)
    a._log_notification = AsyncMock()
    await a.send_relayer_cooldown({
        "active": True,
        "remaining_seconds": 9906,
        "resets_at": "2026-04-16T13:55:00Z",
        "reason": "quota exceeded: 0 units remaining",
    }, quota_remaining=0, daily_quota_limit=100)
    text = a._send_with_id.call_args.args[0]
    assert "RELAYER COOLDOWN" in text
    assert "2h45m" in text
    assert "0/100" in text


async def test_send_relayer_resumed_message():
    a = TelegramAlerter(bot_token="x", chat_id="y", engine_version="test")
    a._send_with_id = AsyncMock(return_value=12)
    a._log_notification = AsyncMock()
    await a.send_relayer_resumed(quota_remaining=100, daily_quota_limit=100)
    text = a._send_with_id.call_args.args[0]
    assert "RELAYER RESUMED" in text
    assert "100/100" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest engine/tests/alerts/test_relayer_cooldown.py -v`
Expected: FAIL — `AttributeError: send_relayer_cooldown`.

- [ ] **Step 3: Implement both alerts**

Add to `engine/alerts/telegram.py` near `send_position_snapshot`:

```python
async def send_relayer_cooldown(
    self,
    cooldown: dict,
    quota_remaining: int,
    daily_quota_limit: int,
) -> Optional[int]:
    """🚫 Builder-relayer 429 quota tripped — fired once on the leading edge."""
    try:
        from engine.alerts.positions import _fmt_age
        text = (
            f"🚫 *RELAYER COOLDOWN* — paused\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Resets in: `{_fmt_age(int(cooldown.get('remaining_seconds', 0)))}`\n"
            f"Resets at: `{cooldown.get('resets_at') or '?'}`\n"
            f"Quota: `{quota_remaining}/{daily_quota_limit}`\n"
            f"Reason: `{(cooldown.get('reason') or '?')[:80]}`\n"
            f"Pending wins will redeem when cooldown clears."
        )
        msg_id = await self._send_with_id(text)
        await self._log_notification("relayer_cooldown", text, telegram_message_id=msg_id)
        return msg_id
    except Exception as exc:
        self._log.warning("telegram.send_relayer_cooldown_failed", error=str(exc)[:100])
        return None


async def send_relayer_resumed(
    self,
    quota_remaining: int,
    daily_quota_limit: int,
) -> Optional[int]:
    """✅ Fired once on the trailing edge when cooldown clears."""
    try:
        text = (
            f"✅ *RELAYER RESUMED*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Quota: `{quota_remaining}/{daily_quota_limit}`\n"
            f"Catching up pending wins on next sweep."
        )
        msg_id = await self._send_with_id(text)
        await self._log_notification("relayer_resumed", text, telegram_message_id=msg_id)
        return msg_id
    except Exception as exc:
        self._log.warning("telegram.send_relayer_resumed_failed", error=str(exc)[:100])
        return None
```

- [ ] **Step 4: Edge-detect cooldown in runtime**

In `engine/infrastructure/runtime.py` near the orchestrator constructor add `self._relayer_cooldown_active = False`. Then in `_redeemer_loop` (around the existing log of `redeemer.cooldown_active`), insert before the loop body:

```python
cd = self._redeemer.cooldown_status()
was_active, now_active = self._relayer_cooldown_active, bool(cd.get("active"))
if now_active and not was_active and self._alerter:
    quota_used = await self._db.count_redeems_today() if hasattr(self._db, "count_redeems_today") else 0
    quota_left = max(0, self._redeemer.daily_quota_limit - quota_used)
    await self._alerter.send_relayer_cooldown(cd, quota_left, self._redeemer.daily_quota_limit)
elif was_active and not now_active and self._alerter:
    quota_used = await self._db.count_redeems_today() if hasattr(self._db, "count_redeems_today") else 0
    quota_left = max(0, self._redeemer.daily_quota_limit - quota_used)
    await self._alerter.send_relayer_resumed(quota_left, self._redeemer.daily_quota_limit)
self._relayer_cooldown_active = now_active
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest engine/tests/alerts/test_relayer_cooldown.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add engine/alerts/telegram.py engine/infrastructure/runtime.py engine/tests/alerts/test_relayer_cooldown.py
git commit -m "feat(alerts): one-shot relayer cooldown + resume cards"
```

---

## Task 7: Enhance the redemption-sweep TG card with overdue list + cooldown line

**Why:** The current sweep card at `runtime.py:3185` reports counters but not *which* conditions failed and not whether we are now in cooldown. With expected frequent 429 hits this is the diagnostic the user wants.

**Files:**
- Modify: `engine/infrastructure/runtime.py` (the existing `🔄 *REDEMPTION SWEEP*` block at line 3183-3189)

- [ ] **Step 1: Replace the sweep alert text**

Inside the `if redeemed > 0 or failed > 0:` block, replace the existing `_send_with_id(...)` call with:

```python
cd = self._redeemer.cooldown_status() if self._redeemer else {"active": False}
failed_details = result.get("failed_details", []) or []
failed_lines = ""
if failed_details:
    rows = "\n".join(
        f"  • `{(d.get('condition_id') or '?')[:12]}…` `{(d.get('error') or '?')[:40]}`"
        for d in failed_details[:5]
    )
    extra = f"\n  …+{len(failed_details) - 5} more" if len(failed_details) > 5 else ""
    failed_lines = f"\n*Failures:*\n{rows}{extra}"

cooldown_line = ""
if cd.get("active"):
    from engine.alerts.positions import _fmt_age
    cooldown_line = (
        f"\n🚫 RELAYER COOLDOWN — resets in "
        f"`{_fmt_age(int(cd.get('remaining_seconds', 0)))}`"
    )

await self._alerter._send_with_id(
    f"🔄 *REDEMPTION SWEEP* 🔴 LIVE\n"
    f"━━━━━━━━━━━━━━━━━━━━━━\n"
    f"Type: `{result.get('redeem_type', 'all')}` | Scanned: `{total}`\n"
    f"Redeemed: `{redeemed}` | Failed: `{failed}`\n"
    f"Wins: `{wins}` | Losses: `{losses}`\n"
    f"P&L: `${pnl:+.2f}` | USDC change: `${usdc:+.2f}`"
    f"{failed_lines}"
    f"{cooldown_line}"
)
```

- [ ] **Step 2: Ensure `failed_details` is populated**

Confirm the redeemer's sweep result dict already includes a `failed_details: [{condition_id, error}]` key. If not, add it where the sweep aggregates per-condition errors. Search:

```bash
rg -n "failed_details|\"failed\":" engine/execution/redeemer.py
```

If absent, append `{"condition_id": cid, "error": str(exc)[:80]}` to a `failed_details` list inside the per-condition try/except in `redeem_wins()`.

- [ ] **Step 3: Manual sanity test**

Trigger a sweep with at least one expected failure (e.g., a non-existent condition_id) and confirm Telegram receives the new format with the failure list and cooldown line if applicable.

- [ ] **Step 4: Commit**

```bash
git add engine/infrastructure/runtime.py engine/execution/redeemer.py
git commit -m "feat(runtime): sweep TG card now lists failures + cooldown state"
```

---

## Task 7.5: STRATEGY MISSED WINDOW critical alert (NEW)

**Why:** A LIVE strategy with zero in-window evaluations is a critical engine-health signal — today it's silently buried in the window summary as one line. We need a dedicated loud alert plus diagnostic context (was the engine alive? did sibling strategies evaluate?).

**Files:**
- Modify: `engine/alerts/telegram.py` — new `send_strategy_missed_window()` alert
- Modify: `engine/use_cases/build_window_summary.py` — emit the alert via the alerter when `window_expired` contains LIVE strategies that were never evaluated in-window
- Test: `engine/tests/alerts/test_strategy_missed_window.py`

**Decision data the alert must carry:**
- Strategy id + mode (LIVE/PAPER)
- Eligible window bounds (e.g. `T-180..T-70`)
- Window timestamp
- Did sibling LIVE strategies evaluate in this window? (yes count / no count) — answers "was the engine alive?"
- Did THIS strategy's evals happen at unexpected offsets? (e.g. only T-60 = post-window)

### Step 1: Write the failing test

```python
# engine/tests/alerts/test_strategy_missed_window.py
import pytest
from unittest.mock import AsyncMock
from alerts.telegram import TelegramAlerter


@pytest.mark.asyncio
async def test_send_strategy_missed_window_loud_alert():
    a = TelegramAlerter(bot_token="x", chat_id="y")
    a._send_with_id = AsyncMock(return_value=77)
    a._log_notification = AsyncMock()
    await a.send_strategy_missed_window(
        strategy_id="v4_fusion",
        mode="LIVE",
        window_ts=1745842200,
        bounds_str="T-180..T-70",
        siblings_evaluated=3,
        siblings_total=5,
        first_eval_offset=60,
    )
    text = a._send_with_id.call_args.args[0]
    assert "STRATEGY MISSED WINDOW" in text
    assert "v4_fusion" in text
    assert "LIVE" in text
    assert "T-180..T-70" in text
    assert "3/5" in text  # sibling evaluation count
    args, kwargs = a._log_notification.call_args
    assert args[0] == "strategy_missed_window"
```

### Step 2: Run test → FAIL

`pytest engine/tests/alerts/test_strategy_missed_window.py -v` → `AttributeError: send_strategy_missed_window`.

### Step 3: Implement the alert

Add to `engine/alerts/telegram.py` near `send_relayer_resumed`:

```python
async def send_strategy_missed_window(
    self,
    strategy_id: str,
    mode: str,
    window_ts: int,
    bounds_str: str,
    siblings_evaluated: int,
    siblings_total: int,
    first_eval_offset: int | None = None,
) -> Optional[int]:
    """🚨 LIVE strategy was never evaluated inside its eligible window.

    Diagnostic carries enough to triage in 5 seconds: bounds, sibling
    eval rate (engine-alive proxy), and the offset of the first eval
    we DID see (usually post-window 'too late').
    """
    try:
        from datetime import datetime, timezone
        ts_str = datetime.fromtimestamp(window_ts, tz=timezone.utc).strftime("%H:%M UTC")
        siblings_line = (
            f"Sibling LIVE strategies evaluated this window: `{siblings_evaluated}/{siblings_total}`"
        )
        cause_hint = ""
        if siblings_evaluated == 0 and siblings_total > 0:
            cause_hint = "\n⚠️ NO siblings evaluated either — engine likely paused/restarting."
        elif siblings_evaluated == siblings_total and siblings_total > 0:
            cause_hint = "\n⚠️ All siblings evaluated — issue is strategy-specific (config/registry)."
        first_line = (
            f"\nFirst eval seen: `T-{first_eval_offset}` (post-window)"
            if first_eval_offset is not None else ""
        )
        text = (
            f"🚨 *STRATEGY MISSED WINDOW* — LIVE\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Strategy: `{strategy_id}` ({mode})\n"
            f"Window: `{ts_str}` | Eligible: `{bounds_str}`\n"
            f"{siblings_line}"
            f"{cause_hint}"
            f"{first_line}"
        )
        msg_id = await self._send_with_id(text)
        await self._log_notification(
            "strategy_missed_window", text, telegram_message_id=msg_id
        )
        return msg_id
    except Exception as exc:
        self._log.warning("telegram.send_strategy_missed_window_failed", error=str(exc)[:100])
        return None
```

### Step 4: Wire into the window-summary path

In `engine/use_cases/build_window_summary.py`, after the existing summary is built and dispatched (find the surrounding caller — likely `engine/infrastructure/runtime.py` where the summary is rendered to Telegram), iterate `ctx.window_expired` and for each entry whose mode is LIVE and whose body contains "never evaluated in-window", call `alerter.send_strategy_missed_window(...)`. Compute `siblings_evaluated` by counting unique LIVE strategy_ids in `eligible + already_traded + blocked_signal + blocked_exec_timing + off_window` (i.e. anything that was evaluated). `siblings_total` = total LIVE strategies in this window context (sum of all live decision groups).

The new logic must NOT fire for PAPER strategies — those legitimately can have eval gaps.

### Step 5: Run tests

```bash
pytest engine/tests/alerts/test_strategy_missed_window.py -v   # 1 passed
pytest engine/tests/alerts/ -v                                 # full suite green
pytest engine/tests/unit/use_cases/test_build_window_summary.py -v  # no regressions
```

### Step 6: Commit

```bash
git add engine/alerts/telegram.py engine/use_cases/build_window_summary.py engine/tests/alerts/test_strategy_missed_window.py
git commit -m "feat(alerts): loud STRATEGY MISSED WINDOW alert with sibling-eval diagnostic"
```

---

## Task 8: Hub endpoint `GET /api/positions/snapshot`

**Files:**
- Create: `hub/api/positions.py`
- Modify: `hub/main.py` (register router)
- Test: `hub/tests/api/test_positions.py`

- [ ] **Step 1: Write the failing test**

```python
# hub/tests/api/test_positions.py
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_positions_snapshot_returns_expected_shape(authed_client: AsyncClient):
    r = await authed_client.get("/api/positions/snapshot")
    assert r.status_code == 200
    body = r.json()
    for k in (
        "wallet_usdc", "pending_wins", "pending_count", "pending_total_usd",
        "overdue_count", "effective_balance", "open_orders", "open_orders_count",
        "cooldown", "daily_quota_limit", "quota_used_today", "quota_remaining",
    ):
        assert k in body, f"missing {k}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest hub/tests/api/test_positions.py -v`
Expected: FAIL — 404 / route not registered.

- [ ] **Step 3: Implement the endpoint**

```python
# hub/api/positions.py
"""
Positions snapshot — read-only view consumed by the Telegram page top bar.
Mirrors the dict shape of engine.alerts.positions.build_snapshot().

Source-of-truth tables:
  - poly_wallet_balance (latest USDC reading written by the engine)
  - poly_pending_wins   (engine writes this every sweep, see Task 9 below)
  - redeemer_state      (engine writes cooldown + quota_used_today every loop)
"""
from __future__ import annotations
from datetime import datetime, timezone
import structlog
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/positions", tags=["positions"])


@router.get("/snapshot")
async def get_snapshot(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    # Wallet
    wallet_row = (await session.execute(text(
        "SELECT usdc_balance, observed_at FROM poly_wallet_balance "
        "ORDER BY observed_at DESC LIMIT 1"
    ))).mappings().first()
    wallet_usdc = float(wallet_row["usdc_balance"]) if wallet_row else 0.0

    # Pending wins (written by engine — see Task 9)
    pending_rows = (await session.execute(text(
        "SELECT condition_id, value, window_end_utc, "
        "  EXTRACT(EPOCH FROM (NOW() - window_end_utc))::int AS overdue_seconds "
        "FROM poly_pending_wins "
        "ORDER BY window_end_utc ASC"
    ))).mappings().all()
    pending = [dict(r) for r in pending_rows]
    pending_total = round(sum(float(r["value"]) for r in pending), 2)
    overdue_count = sum(1 for r in pending if int(r["overdue_seconds"]) > 300)

    # Cooldown + quota (engine writes this row every redeemer loop)
    rs = (await session.execute(text(
        "SELECT cooldown_active, cooldown_remaining_seconds, cooldown_resets_at, "
        "  cooldown_reason, daily_quota_limit, quota_used_today "
        "FROM redeemer_state ORDER BY observed_at DESC LIMIT 1"
    ))).mappings().first() or {}

    cooldown = {
        "active": bool(rs.get("cooldown_active")),
        "remaining_seconds": int(rs.get("cooldown_remaining_seconds") or 0),
        "resets_at": rs.get("cooldown_resets_at").isoformat() if rs.get("cooldown_resets_at") else None,
        "reason": rs.get("cooldown_reason") or "",
    }
    daily_quota_limit = int(rs.get("daily_quota_limit") or 100)
    quota_used_today = int(rs.get("quota_used_today") or 0)
    quota_remaining = max(0, daily_quota_limit - quota_used_today)

    return {
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "wallet_usdc": round(wallet_usdc, 2),
        "pending_wins": pending,
        "pending_count": len(pending),
        "pending_total_usd": pending_total,
        "overdue_count": overdue_count,
        "effective_balance": round(wallet_usdc + pending_total, 2),
        "open_orders": [],         # TODO Task 11 (out of scope)
        "open_orders_count": 0,
        "cooldown": cooldown,
        "daily_quota_limit": daily_quota_limit,
        "quota_used_today": quota_used_today,
        "quota_remaining": quota_remaining,
    }
```

- [ ] **Step 4: Register the router in `hub/main.py`**

Add near other `app.include_router(...)` lines:

```python
from api.positions import router as positions_router
app.include_router(positions_router, prefix="/api", tags=["positions"])
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest hub/tests/api/test_positions.py -v`
Expected: 1 passed (with empty pending list when no rows exist).

- [ ] **Step 6: Commit**

```bash
git add hub/api/positions.py hub/main.py hub/tests/api/test_positions.py
git commit -m "feat(hub): GET /api/positions/snapshot for TG page top bar"
```

---

## Task 9: DB tables `poly_pending_wins` + `redeemer_state` (engine-writer)

**Why:** Hub Task 8 reads from two tables that don't exist yet. Engine snapshots write to them every loop.

**Files:**
- Create: `hub/db/migrations/versions/20260416_01_pending_wins.sql`
- Modify: `engine/infrastructure/runtime.py` (write to both tables in `_send_position_snapshot`)
- Modify: `engine/persistence/db_client.py` (add `upsert_pending_wins`, `upsert_redeemer_state`)

- [ ] **Step 1: Write the migration**

```sql
-- hub/db/migrations/versions/20260416_01_pending_wins.sql
-- Engine-managed snapshot tables read by Hub /api/positions/snapshot.
-- Written from engine.infrastructure.runtime._send_position_snapshot()
-- on every 15-min cadence + post-sweep.

CREATE TABLE IF NOT EXISTS poly_pending_wins (
    condition_id     TEXT PRIMARY KEY,
    value            DOUBLE PRECISION NOT NULL,
    window_end_utc   TIMESTAMPTZ NOT NULL,
    observed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pending_wins_window_end
    ON poly_pending_wins (window_end_utc);

CREATE TABLE IF NOT EXISTS redeemer_state (
    id                              BIGSERIAL PRIMARY KEY,
    cooldown_active                 BOOLEAN NOT NULL,
    cooldown_remaining_seconds      INTEGER NOT NULL DEFAULT 0,
    cooldown_resets_at              TIMESTAMPTZ,
    cooldown_reason                 TEXT,
    daily_quota_limit               INTEGER NOT NULL,
    quota_used_today                INTEGER NOT NULL,
    observed_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_redeemer_state_observed
    ON redeemer_state (observed_at DESC);
```

- [ ] **Step 2: Add the writer methods**

In `engine/persistence/db_client.py` add:

```python
async def upsert_pending_wins(self, wins: list[dict]) -> None:
    """Replace the pending_wins set with the supplied list (atomic)."""
    async with self._pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM poly_pending_wins")
            if wins:
                await conn.executemany(
                    "INSERT INTO poly_pending_wins (condition_id, value, window_end_utc) "
                    "VALUES ($1, $2, $3)",
                    [(w["condition_id"], float(w["value"]), w["window_end_utc"]) for w in wins],
                )

async def upsert_redeemer_state(
    self,
    cooldown: dict,
    daily_quota_limit: int,
    quota_used_today: int,
) -> None:
    async with self._pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO redeemer_state "
            "(cooldown_active, cooldown_remaining_seconds, cooldown_resets_at, "
            " cooldown_reason, daily_quota_limit, quota_used_today) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            bool(cooldown.get("active")),
            int(cooldown.get("remaining_seconds") or 0),
            cooldown.get("resets_at"),
            cooldown.get("reason") or "",
            int(daily_quota_limit),
            int(quota_used_today),
        )

async def count_redeems_today(self) -> int:
    async with self._pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS c FROM poly_redeem_attempts "
            "WHERE attempted_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')"
        )
        return int(row["c"] or 0) if row else 0
```

- [ ] **Step 3: Wire writes into `_send_position_snapshot`**

In Task 4's `_send_position_snapshot` body, just before `await self._alerter.send_position_snapshot(snap)`:

```python
try:
    await self._db.upsert_pending_wins(pending)
    await self._db.upsert_redeemer_state(cooldown, self._redeemer.daily_quota_limit, quota_used)
except Exception as exc:
    log.warning("orchestrator.snapshot_persist_failed", error=str(exc)[:120])
```

- [ ] **Step 4: Apply migration locally**

Run: `cd hub && alembic upgrade head` (or apply the SQL via `psql` against the local dev DB).

- [ ] **Step 5: Smoke test**

Run engine in paper mode locally for 20 min, then `psql -c "SELECT * FROM poly_pending_wins; SELECT * FROM redeemer_state ORDER BY observed_at DESC LIMIT 3"`. Confirm rows appear after the first sweep + first 15-min snapshot.

- [ ] **Step 6: Commit**

```bash
git add hub/db/migrations/versions/20260416_01_pending_wins.sql engine/persistence/db_client.py engine/infrastructure/runtime.py
git commit -m "feat(db): poly_pending_wins + redeemer_state for snapshot endpoint"
```

---

## Task 10: Frontend `<PositionSnapshotBar />` sticky top bar

**Files:**
- Create: `frontend/src/pages/telegram/PositionSnapshotBar.jsx`
- Modify: `frontend/src/pages/Telegram.jsx` (mount the bar above `<NotificationFeed />`, add card colours for the new types)

- [ ] **Step 1: Build the component**

```jsx
// frontend/src/pages/telegram/PositionSnapshotBar.jsx
import React, { useEffect, useState } from 'react';
import { useApi } from '../../hooks/useApi.js';
import { T } from '../polymarket/components/theme.js';

function fmtAge(s) {
  if (s == null) return '—';
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`;
}

export default function PositionSnapshotBar() {
  const api = useApi();
  const [snap, setSnap] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let live = true;
    const tick = async () => {
      try {
        const res = await api.get('/positions/snapshot');
        if (live) { setSnap(res); setError(null); }
      } catch (e) {
        if (live) setError(String(e));
      }
    };
    tick();
    const id = setInterval(tick, 5000);
    return () => { live = false; clearInterval(id); };
  }, [api]);

  if (error && !snap) {
    return <div style={{ padding: 12, color: T.red, fontFamily: T.mono }}>snapshot error: {error}</div>;
  }
  if (!snap) {
    return <div style={{ padding: 12, color: T.textMuted, fontFamily: T.mono }}>loading snapshot…</div>;
  }

  const cd = snap.cooldown || {};
  const pendingTone = snap.overdue_count > 0 ? T.amber : T.cyan;
  const cooldownTone = cd.active ? T.red : T.green;

  return (
    <div style={{
      position: 'sticky', top: 0, zIndex: 10,
      background: T.headerBg, borderBottom: `1px solid ${T.border}`,
      padding: '10px 14px', fontFamily: T.mono, color: T.text,
      display: 'flex', flexWrap: 'wrap', gap: 16, alignItems: 'center',
    }}>
      <div>
        <span style={{ color: T.textMuted }}>Wallet </span>
        <span style={{ color: T.text }}>${snap.wallet_usdc.toFixed(2)}</span>
      </div>
      <div>
        <span style={{ color: T.textMuted }}>Pending </span>
        <span style={{ color: pendingTone }}>
          ${snap.pending_total_usd.toFixed(2)} ({snap.pending_count})
          {snap.overdue_count > 0 && (
            <span style={{ color: T.amber, marginLeft: 6 }}>· {snap.overdue_count} OVERDUE</span>
          )}
        </span>
      </div>
      <div>
        <span style={{ color: T.textMuted }}>Effective </span>
        <span style={{ color: T.green, fontWeight: 600 }}>${snap.effective_balance.toFixed(2)}</span>
      </div>
      <div style={{ marginLeft: 'auto' }}>
        <span style={{ color: cooldownTone }}>
          {cd.active
            ? `🚫 cooldown ${fmtAge(cd.remaining_seconds)}`
            : '🟢 relayer ok'}
        </span>
        <span style={{ color: T.textMuted, marginLeft: 8 }}>
          {snap.quota_remaining}/{snap.daily_quota_limit} quota
        </span>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Mount the bar inside `Telegram.jsx`**

Insert at the top of the page render tree, above the existing filter bar:

```jsx
import PositionSnapshotBar from './telegram/PositionSnapshotBar.jsx';
// ...
<PositionSnapshotBar />
<FilterBar ... />
<NotificationFeed ... />
```

- [ ] **Step 3: Add new colours to the card colour map**

In the `cardColor` function inside `Telegram.jsx`:

```js
if (t === 'position_snapshot') return T.cyan;
if (t === 'relayer_cooldown')  return T.red;
if (t === 'relayer_resumed')   return T.green;
```

- [ ] **Step 4: Manual smoke test**

Run frontend dev (`cd frontend && npm run dev`), log in, open `/telegram`. Verify:
- The bar renders within 5 s of page load.
- `Effective` matches `Wallet + Pending`.
- During an active cooldown the right-hand chip is red and shows the countdown.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/telegram/PositionSnapshotBar.jsx frontend/src/pages/Telegram.jsx
git commit -m "feat(frontend): sticky PositionSnapshotBar + new card colours"
```

---

## Task 11: End-to-end live verification (Montreal)

**Why:** Per CLAUDE.md, Polymarket calls only run on Montreal. Verifying live is the only way to confirm pending_wins is populated by the real `_scan_redeemable_positions()`.

- [ ] **Step 1: Deploy to develop**

Push branch, open PR to `develop`, merge after review (per memory: never push direct to develop). Railway auto-deploys Hub + frontend.

- [ ] **Step 2: rsync engine to Montreal + restart**

Per `reference_montreal_deploy.md`: `rsync` engine, then SSH and `systemctl restart novakash-engine`.

- [ ] **Step 3: Verify within 20 min**

In Telegram, expect:
- A `📊 POSITION SNAPSHOT` message within 15 min of restart.
- After the next sweep, a fresh snapshot showing the pending list shrinking.
- If the engine hits 429: a `🚫 RELAYER COOLDOWN` card, then on cooldown clear a `✅ RELAYER RESUMED` card.

In the Hub frontend:
- `/telegram` top bar shows live `Effective` balance.
- Cards for the three new types render with the new colours.

- [ ] **Step 4: Update memory**

Run: append a project memory entry:

```markdown
- [Telegram redemption visibility shipped](project_telegram_redemption_visibility.md) — pending_wins, effective balance, relayer cooldown alerts live
```

- [ ] **Step 5: Final commit**

If any tweaks were needed during live verification:

```bash
git add -A && git commit -m "chore(telegram): live-verification tweaks"
```

---

## Out of Scope (intentional)

- **Open-orders enrichment in the snapshot** — `open_orders` field is reserved (`[]`) but not yet populated. Add in a follow-up plan when an `engine.poly_client.list_open_orders()` accessor exists.
- **Force-redeem button on the frontend** — Hub already exposes `redeem_requested`; wiring a UI button is a separate UX task.
- **Chart attachments on snapshots** — text-only for now. Daily P&L charts already exist on the PnL page.

---

## Self-Review Checklist (run before handoff)

1. **Spec coverage:** Every user requirement maps to a task —
   unredeemed wins (Tasks 1, 2, 3, 4, 8, 9, 10), relayer limit (Tasks 6, 7, 8, 10), positions/wins/losses (Tasks 1, 4, 7), multi-fill (Task 5), effective balance (Tasks 1, 8, 10).
2. **Placeholder scan:** No "TBD" / "appropriate error handling" / "similar to Task N". One TODO comment in Task 8 explicitly defers `open_orders` to Task 11 with an explanation.
3. **Type consistency:** `build_snapshot` dict shape is identical across `positions.py`, `hub/api/positions.py`, and `PositionSnapshotBar.jsx` (all 12 keys). `pending_wins` row shape is `{condition_id, value, window_end_utc, overdue_seconds}` everywhere. `cooldown` shape is `{active, remaining_seconds, resets_at, reason}` everywhere.
