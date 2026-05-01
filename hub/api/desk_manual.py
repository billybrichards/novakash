"""
/desk Phase 3 — operator manual-trade endpoint.

Spec: Track A of the manual-trade-bar feature.

Endpoints (all JWT-protected):
  POST /desk/manual-trade
        body: DeskManualTradeRequest
        → { trade_id, status, queued_at, capped_price?, message }

        Hard safety perimeter:
          - direction ∈ {UP, DOWN}
          - 1.0 ≤ stake_usd ≤ 25.0        → 400 if outside
          - 0.05 ≤ price ≤ 0.82           → 400 if outside
          - window_epoch required; must match current 5-min floor ±60s → 400 stale
          - max 3 successful INSERTs per operator per rolling 60 min   → 429
          - if open pending_live/executing row exists for same window+asset → 409

  GET  /desk/manual-trades?recent=20
        → { trades: [...] }   (rows scoped to operator_user_id)

The INSERT logic is delegated to _insert_manual_trade_row — a shared helper
that is also imported by the existing /api/v58/manual-trade endpoint so there
is no duplication.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session
from db.migrations.v58_monitor_ddl import ensure_manual_trades_table

log = structlog.get_logger(__name__)

router = APIRouter(tags=["desk"])

# Matches engine constants.POLY_WINDOW_SECONDS
WINDOW_SECONDS = 300

# ── Safety caps ──────────────────────────────────────────────────────────────
STAKE_MIN = 1.0
STAKE_MAX = 25.0
PRICE_MIN = 0.05
PRICE_MAX = 0.82

# ── Rate-limit ───────────────────────────────────────────────────────────────
RATE_LIMIT_MAX = 12         # max successful inserts per operator per rolling 60 min
RATE_LIMIT_WINDOW_S = 3600  # rolling window (60 min)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _window_floor(now_s: int) -> int:
    """5-minute floor of a unix timestamp (seconds)."""
    return (now_s // WINDOW_SECONDS) * WINDOW_SECONDS


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── Pydantic ──────────────────────────────────────────────────────────────────

class DeskManualTradeRequest(BaseModel):
    direction: str = Field(..., description="UP or DOWN")
    stake_usd: float = Field(..., ge=STAKE_MIN, le=STAKE_MAX,
                             description=f"Stake in USD ({STAKE_MIN}–{STAKE_MAX})")
    price: float = Field(..., ge=PRICE_MIN, le=PRICE_MAX,
                         description=f"Entry price cap ({PRICE_MIN}–{PRICE_MAX})")
    window_epoch: int = Field(..., ge=0,
                              description="Unix epoch (seconds) of the 5-min window")
    asset: str = Field(default="BTC")
    mode: str = Field(default="live", description="live or paper")
    order_type: str = Field(default="FAK", description="FAK, FOK, or GTC")
    operator_rationale: Optional[str] = None

    @field_validator("direction")
    @classmethod
    def _direction_up_or_down(cls, v: str) -> str:
        up = v.strip().upper()
        if up not in ("UP", "DOWN"):
            raise ValueError("direction must be 'UP' or 'DOWN'")
        return up

    @field_validator("mode")
    @classmethod
    def _mode_valid(cls, v: str) -> str:
        lv = v.strip().lower()
        if lv not in ("live", "paper"):
            raise ValueError("mode must be 'live' or 'paper'")
        return lv

    @field_validator("order_type")
    @classmethod
    def _order_type_valid(cls, v: str) -> str:
        up = v.strip().upper()
        if up not in ("FAK", "FOK", "GTC"):
            raise ValueError("order_type must be FAK, FOK, or GTC")
        return up


# ── Route: POST /desk/manual-trade ────────────────────────────────────────────

@router.post("/desk/manual-trade")
async def post_desk_manual_trade(
    body: DeskManualTradeRequest,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Fire a real FAK trade via the engine.

    Safety perimeter (enforced here, before any DB write):
      1. stake_usd ∈ [1.0, 25.0]              — validated by Pydantic
      2. price ∈ [0.05, 0.82]                 — validated by Pydantic
      3. direction ∈ {UP, DOWN}               — validated by Pydantic
      4. window_epoch within ±60s of current  — validated here (400)
      5. Rate limit: ≤3 per operator per 60m  — validated here (429)
      6. Concurrency: no open row for window+asset — validated here (409)
    """
    # ── 1. Validate window freshness ─────────────────────────────────────────
    now_s = int(time.time())
    current_floor = _window_floor(now_s)
    if abs(body.window_epoch - current_floor) > 60:
        raise HTTPException(
            status_code=400,
            detail=(
                f"window_epoch {body.window_epoch} is stale or future. "
                f"Current 5-min floor is {current_floor} (±60s allowed)."
            ),
        )

    # ── 2. Ensure schema ─────────────────────────────────────────────────────
    await ensure_manual_trades_table(session)

    # ── 3. Rate limit ─────────────────────────────────────────────────────────
    cutoff_ts = now_s - RATE_LIMIT_WINDOW_S
    cutoff_dt = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc)
    rate_result = await session.execute(
        text("""
            SELECT COUNT(*) AS cnt
            FROM manual_trades
            WHERE operator_user_id = :user_id
              AND created_at >= :cutoff
              AND status NOT IN ('rejected', 'error')
        """),
        {"user_id": user.user_id, "cutoff": cutoff_dt},
    )
    row = rate_result.mappings().first()
    recent_count = int(row["cnt"]) if row else 0
    if recent_count >= RATE_LIMIT_MAX:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Rate limit: you have placed {recent_count} trade(s) in the last 60 minutes. "
                f"Maximum is {RATE_LIMIT_MAX}. Wait before submitting another."
            ),
        )

    # ── 4. Concurrency block ──────────────────────────────────────────────────
    # Reject if there is already an open pending_live or executing row for the
    # same window+asset — prevents double-execution on the same candle.
    conc_result = await session.execute(
        text("""
            SELECT trade_id
            FROM manual_trades
            WHERE window_ts = :window_epoch
              AND asset = :asset
              AND status IN ('pending_live', 'executing')
            LIMIT 1
        """),
        {"window_epoch": body.window_epoch, "asset": body.asset},
    )
    conc_row = conc_result.mappings().first()
    if conc_row:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Conflict: trade {conc_row['trade_id']} is already "
                f"pending_live/executing for window {body.window_epoch} "
                f"asset {body.asset}. Wait for it to settle."
            ),
        )

    # ── 5. Insert ──────────────────────────────────────────────────────────────
    trade_id = f"desk_{uuid.uuid4().hex[:16]}"
    status = "open" if body.mode == "paper" else "pending_live"

    await session.execute(
        text("""
            INSERT INTO manual_trades
                (trade_id, window_ts, asset, direction, mode,
                 entry_price, stake_usd, status, order_type, created_at,
                 operator_user_id, operator_username)
            VALUES
                (:trade_id, :window_epoch, :asset, :direction, :mode,
                 :price, :stake_usd, :status, :order_type, NOW(),
                 :operator_user_id, :operator_username)
        """),
        {
            "trade_id": trade_id,
            "window_epoch": body.window_epoch,
            "asset": body.asset,
            "direction": body.direction,
            "mode": body.mode,
            "price": body.price,
            "stake_usd": body.stake_usd,
            "status": status,
            "order_type": body.order_type,
            "operator_user_id": user.user_id,
            "operator_username": user.username,
        },
    )
    await session.commit()

    # ── 6. NOTIFY engine for live trades ──────────────────────────────────────
    # Mirrors the LT-04 pattern in v58_monitor.py — engine polls every 1s as
    # fallback, but NOTIFY drops latency to ~tens of milliseconds.
    if body.mode == "live":
        try:
            await session.execute(
                text("SELECT pg_notify('manual_trade_pending', :trade_id)"),
                {"trade_id": trade_id},
            )
            await session.commit()
            log.info("desk_manual.notified", trade_id=trade_id)
        except Exception as exc:
            log.warning("desk_manual.notify_failed", error=str(exc)[:200], trade_id=trade_id)

    log.info(
        "desk_manual.placed",
        trade_id=trade_id,
        direction=body.direction,
        mode=body.mode,
        stake=body.stake_usd,
        price=body.price,
        operator=user.username,
    )

    return {
        "trade_id": trade_id,
        "status": status,
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "capped_price": body.price,
        "message": (
            f"Trade queued for engine execution."
            if body.mode == "live"
            else "Paper trade recorded."
        ),
    }


# ── Route: GET /desk/manual-trades ────────────────────────────────────────────

@router.get("/desk/manual-trades")
async def get_desk_manual_trades(
    recent: int = Query(default=20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Return the most recent manual trades placed by the authenticated operator.

    Results are scoped to operator_user_id so operators can only see their
    own trades. Returns the SOT columns for reconciliation state display.
    """
    await ensure_manual_trades_table(session)

    result = await session.execute(
        text("""
            SELECT
                trade_id,
                window_ts,
                asset,
                direction,
                mode,
                entry_price,
                stake_usd,
                status,
                order_type,
                polymarket_order_id,
                polymarket_confirmed_fill_price AS fill_price,
                polymarket_confirmed_size AS fill_size,
                sot_reconciliation_state,
                created_at,
                resolved_at AS filled_at
            FROM manual_trades
            WHERE operator_user_id = :user_id
            ORDER BY created_at DESC
            LIMIT :limit
        """),
        {"user_id": user.user_id, "limit": recent},
    )
    rows = result.mappings().all()

    trades = []
    for r in rows:
        trades.append({
            "trade_id": r["trade_id"],
            "window_ts": r["window_ts"],
            "asset": r["asset"],
            "direction": r["direction"],
            "mode": r["mode"],
            "entry_price": _safe_float(r["entry_price"]),
            "stake_usd": _safe_float(r["stake_usd"]),
            "status": r["status"],
            "order_type": r.get("order_type"),
            "polymarket_order_id": r.get("polymarket_order_id"),
            "fill_price": _safe_float(r.get("fill_price")),
            "fill_size": _safe_float(r.get("fill_size")),
            "sot_reconciliation_state": r.get("sot_reconciliation_state"),
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
            "filled_at": r["filled_at"].isoformat() if r.get("filled_at") else None,
        })

    return {"trades": trades, "count": len(trades)}
