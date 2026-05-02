"""
/desk Phase 1 API — window clock + operator manual-pick journal.

Spec: hub note #218 (frontend /desk play-along HUD).

Endpoints (all JWT-protected except where noted):
  GET  /api/windows/current
        → { window_epoch, t_open_ts, t_close_ts, target_price_chainlink,
            seconds_remaining }
        Window is a 5-minute floor of current UTC seconds. The
        `target_price_chainlink` is the most recent tick from
        ticks_chainlink at-or-before `t_open_ts`, which is the value
        Polymarket resolves against at close (per the
        oracle-source-of-truth memory note).

  POST /api/desk/picks
        body: { window_epoch, pick, t_remaining_s, notes?, asset?, timeframe? }
        → { pick_id, created }
        UPSERT semantics — a second POST for the same
        (window_epoch, asset, timeframe) updates pick + notes + t_remaining_s
        instead of inserting a duplicate. Operator can flip their call up
        until the FE T-00:10 client lockout.

  GET  /api/desk/picks?limit=50
        → { rows: [...] }
        Recent picks joined against strategy_decisions_resolved so the
        last-20-windows table on /desk can colour each pick by outcome.

  GET  /api/desk/clob-book?window_epoch=<int>&asset=BTC
        → 1-deep reconstructed book from window_snapshots CLOB columns.
        Used as a fallback when /api/clob/book is unavailable (HUB_ALLOW_CLOB_FETCH
        not set on this deployment). The engine writes clob_up_bid, clob_up_ask,
        clob_down_bid, clob_down_ask, clob_implied_up to window_snapshots so this
        works without hitting polymarket.com from the hub.
        Shape is compatible with /api/clob/book (yes.asks[0].price, etc.) plus
        extra fields: source="window_snapshots", age_s=<int>.
        Degrades to empty book (200) if no snapshot row exists for the window.

Degraded shapes:
  - ticks_chainlink may be cold / empty for an asset
    → target_price_chainlink = null, no 500.
  - strategy_decisions_resolved view may be dropped during a rebuild
    → the GET /desk/picks join falls back to an outcome-less response.
  - window_snapshots missing / CLOB columns NULL
    → /desk/clob-book returns {yes:{asks:[],bids:[]},no:{asks:[],bids:[]}} 200.
"""

from __future__ import annotations

import time
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session

# Staleness threshold for /desk/clob-book — if the most-recent window_snapshots
# row is older than this many seconds we include a stale=True flag so the FE
# can show a warning badge. 60s = 1/5th of a window is a reasonable cutoff.
_CLOB_STALE_THRESHOLD_S = 60

log = structlog.get_logger(__name__)

router = APIRouter(tags=["desk"])

# Matches engine constants.POLY_WINDOW_SECONDS; intentionally hard-coded
# here rather than imported so the hub module doesn't pull in engine/.
WINDOW_SECONDS = 300


# ── Helpers ─────────────────────────────────────────────────────────────────

def _window_floor(now_s: int) -> int:
    """5-minute floor of a unix timestamp."""
    return (now_s // WINDOW_SECONDS) * WINDOW_SECONDS


def _is_missing_table(exc: Exception) -> bool:
    """asyncpg UndefinedTable (pgcode 42P01) wrapped by SQLAlchemy."""
    if not isinstance(exc, ProgrammingError):
        return False
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) == "42P01"


# ── Pydantic ────────────────────────────────────────────────────────────────

class PickIn(BaseModel):
    window_epoch: int = Field(..., ge=0)
    pick: str = Field(..., pattern="^(UP|DOWN|SKIP)$")
    t_remaining_s: int = Field(..., ge=0, le=WINDOW_SECONDS)
    notes: Optional[str] = Field(default=None, max_length=2000)
    asset: str = Field(default="BTC", max_length=16)
    timeframe: str = Field(default="5m", max_length=16)


# ── Endpoints ───────────────────────────────────────────────────────────────

@router.get("/windows/current")
async def current_window(
    asset: str = Query("BTC", max_length=16),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Current 5m window clock + canonical Polymarket open price.

    Resolution lookup, in preference order:
      1. window_snapshots.open_price — written by the engine post-PR #464.
         This is Polymarket's eventMetadata.priceToBeat (Chainlink Streams
         off-chain) when available, else chainlink_polygon, else binance.
         Matches the number Polymarket resolves against to the cent.
      2. ticks_chainlink — legacy fallback while engine is cold-starting.
         Diverges from priceToBeat by $3-32 per window per audit #464.

    Both are exposed: `target_price` is the canonical (preferred) value,
    `target_price_chainlink` stays for back-compat with older FE builds,
    and `target_price_source` tags which path served it so the FE can
    label the badge accordingly.
    """
    now_s = int(time.time())
    t_open = _window_floor(now_s)
    t_close = t_open + WINDOW_SECONDS
    seconds_remaining = max(0, t_close - now_s)

    target_price: Optional[float] = None
    target_price_source: str = "unknown"

    # PRIMARY — engine-written canonical price (post-PR #464). Reading the
    # most-recent eval_offset row for this window so a cold window with no
    # snapshot yet falls through to ticks_chainlink below.
    try:
        q = text(
            """
            SELECT open_price
            FROM window_snapshots
            WHERE window_ts = :window_ts
              AND asset = :asset
              AND open_price IS NOT NULL
            ORDER BY eval_offset DESC NULLS LAST, created_at DESC
            LIMIT 1
            """
        )
        res = await session.execute(q, {"asset": asset, "window_ts": t_open})
        row = res.first()
        if row and row[0] is not None:
            target_price = float(row[0])
            target_price_source = "polymarket_canonical"
    except Exception as exc:
        if not _is_missing_table(exc):
            log.warning("desk.current_window.window_snapshots_err", error=str(exc)[:200])

    # FALLBACK — legacy chainlink_polygon-only path. Always read this so
    # target_price_chainlink stays populated for old FE builds.
    chainlink_price: Optional[float] = None
    try:
        q = text(
            """
            SELECT price
            FROM ticks_chainlink
            WHERE asset = :asset AND ts <= to_timestamp(:t_open)
            ORDER BY ts DESC
            LIMIT 1
            """
        )
        res = await session.execute(q, {"asset": asset, "t_open": t_open})
        row = res.first()
        if row and row[0] is not None:
            chainlink_price = float(row[0])
    except Exception as exc:
        if not _is_missing_table(exc):
            log.warning("desk.current_window.chainlink_err", error=str(exc)[:200])

    if target_price is None and chainlink_price is not None:
        target_price = chainlink_price
        target_price_source = "chainlink_polygon_fallback"

    return {
        "window_epoch": t_open,
        "t_open_ts": t_open,
        "t_close_ts": t_close,
        "target_price": target_price,
        "target_price_source": target_price_source,
        "target_price_chainlink": chainlink_price,
        "seconds_remaining": seconds_remaining,
        "asset": asset,
        "timeframe": "5m",
    }


@router.post("/desk/picks")
async def upsert_pick(
    req: PickIn,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Record/update the operator's pick for a window. Idempotent UPSERT."""
    q = text(
        """
        INSERT INTO desk_picks
            (window_epoch, asset, timeframe, pick, t_remaining_s, notes, updated_at)
        VALUES
            (:window_epoch, :asset, :timeframe, :pick, :t_remaining_s, :notes, NOW())
        ON CONFLICT (window_epoch, asset, timeframe)
        DO UPDATE SET
            pick = EXCLUDED.pick,
            t_remaining_s = EXCLUDED.t_remaining_s,
            notes = EXCLUDED.notes,
            updated_at = NOW()
        RETURNING id,
                  (xmax = 0) AS created
        """
    )
    try:
        res = await session.execute(
            q,
            {
                "window_epoch": req.window_epoch,
                "asset": req.asset,
                "timeframe": req.timeframe,
                "pick": req.pick,
                "t_remaining_s": req.t_remaining_s,
                "notes": req.notes,
            },
        )
        row = res.mappings().first()
        await session.commit()
    except ProgrammingError as exc:
        if _is_missing_table(exc):
            raise HTTPException(
                status_code=503,
                detail="desk_picks table not ready — migration pending",
            )
        raise

    log.info(
        "desk.pick_upserted",
        window_epoch=req.window_epoch,
        pick=req.pick,
        created=bool(row["created"]) if row else None,
    )
    return {
        "pick_id": int(row["id"]) if row else None,
        "created": bool(row["created"]) if row else False,
    }


@router.get("/desk/picks")
async def list_picks(
    asset: str = Query("BTC", max_length=16),
    timeframe: str = Query("5m", max_length=16),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Recent operator picks joined against any resolved outcome.

    The outcome join uses strategy_decisions_resolved (audit #222) and is
    bucketed by window_epoch → most recent window_ts within that window.
    If the view is missing, we return picks with outcome=null.
    """
    # Base pick fetch — always works once the table exists.
    try:
        pick_rows = (
            await session.execute(
                text(
                    """
                    SELECT id, window_epoch, asset, timeframe, pick, t_remaining_s,
                           notes, created_at, updated_at
                    FROM desk_picks
                    WHERE asset = :asset AND timeframe = :tf
                    ORDER BY window_epoch DESC
                    LIMIT :limit
                    """
                ),
                {"asset": asset, "tf": timeframe, "limit": limit},
            )
        ).mappings().all()
    except ProgrammingError as exc:
        if _is_missing_table(exc):
            return {"rows": [], "_meta": {"missing": ["desk_picks"]}}
        raise

    if not pick_rows:
        return {"rows": []}

    # Build window_epoch list for the outcome LEFT JOIN.
    window_epochs = [int(r["window_epoch"]) for r in pick_rows]

    outcomes_by_window: dict[int, dict] = {}
    try:
        # strategy_decisions_resolved stores window_ts as TIMESTAMPTZ; convert
        # pick window_epoch -> ts via to_timestamp(). We aggregate across
        # strategies to pull the representative direction/outcome for that
        # window (operator-intent is window-level, not strategy-level).
        out_q = text(
            """
            SELECT
                EXTRACT(EPOCH FROM window_ts)::bigint AS window_epoch,
                -- Prefer a LIVE-mode row if any; fall back to any row.
                (ARRAY_AGG(direction ORDER BY (mode = 'live') DESC,
                                          evaluated_at DESC))[1] AS direction,
                (ARRAY_AGG(outcome ORDER BY (outcome IS NOT NULL) DESC,
                                       resolved_at DESC NULLS LAST))[1] AS outcome,
                MAX(pnl_usd) AS pnl_usd
            FROM strategy_decisions_resolved
            WHERE asset = :asset
              AND timeframe = :tf
              AND EXTRACT(EPOCH FROM window_ts)::bigint = ANY(:epochs)
            GROUP BY window_ts
            """
        )
        out_res = await session.execute(
            out_q,
            {"asset": asset, "tf": timeframe, "epochs": window_epochs},
        )
        for r in out_res.mappings().all():
            outcomes_by_window[int(r["window_epoch"])] = {
                "direction": r["direction"],
                "outcome": r["outcome"],
                "pnl_usd": float(r["pnl_usd"]) if r["pnl_usd"] is not None else None,
            }
    except ProgrammingError as exc:
        # View dropped / not yet created → degrade silently.
        if not _is_missing_table(exc):
            log.warning("desk.list_picks.view_err", error=str(exc)[:200])

    rows = []
    for r in pick_rows:
        d = dict(r)
        for k in ("created_at", "updated_at"):
            v = d.get(k)
            if v is not None and hasattr(v, "isoformat"):
                d[k] = v.isoformat()
        outcome = outcomes_by_window.get(int(d["window_epoch"]))
        if outcome:
            d["actual_direction"] = outcome["direction"]
            d["outcome"] = outcome["outcome"]
            d["pnl_usd"] = outcome["pnl_usd"]
        else:
            d["actual_direction"] = None
            d["outcome"] = None
            d["pnl_usd"] = None
        rows.append(d)

    return {"rows": rows}


# ── /desk/clob-book — window_snapshots fallback ───────────────────────────────

def _make_empty_book() -> dict:
    """Empty book shape compatible with /api/clob/book."""
    return {
        "yes": {"bids": [], "asks": []},
        "no": {"bids": [], "asks": []},
        "implied_p_up": None,
        "spread": None,
        "imbalance": None,
        "source": "window_snapshots",
        "age_s": None,
        "stale": False,
    }


def _one_level(price: Optional[float], size_placeholder: float = 0.0) -> list:
    """Build a 1-deep level list from a scalar price, or [] if price is None."""
    if price is None or not (0.0 <= price <= 1.0):
        return []
    return [{"price": float(price), "size": size_placeholder}]


@router.get("/desk/clob-book")
async def desk_clob_book(
    window_epoch: int = Query(..., ge=0, description="5-minute window start timestamp"),
    asset: str = Query("BTC", max_length=16),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    1-deep CLOB book reconstructed from window_snapshots CLOB columns.

    This is the fallback path for /desk when HUB_ALLOW_CLOB_FETCH is not set
    on this hub deployment (default on AWS hub). The engine writes
    clob_up_bid, clob_up_ask, clob_down_bid, clob_down_ask, clob_implied_up
    to window_snapshots every eval cycle so this endpoint works without any
    outbound call to polymarket.com.

    Shape is intentionally compatible with /api/clob/book so callers can
    use the same `book.yes.asks[0].price` access pattern. Extra fields:
      - source: "window_snapshots"
      - age_s:  seconds since the snapshot row was written (int or null)
      - stale:  true if age_s > 60 (1/5th of a window)

    Degrades to empty book (200) if window_snapshots is missing or has no
    CLOB data for this window — never returns 500 for a missing row.
    """
    empty = _make_empty_book()

    try:
        q = text(
            """
            SELECT
                clob_up_bid,
                clob_up_ask,
                clob_down_bid,
                clob_down_ask,
                clob_implied_up,
                created_at
            FROM window_snapshots
            WHERE window_ts = :window_ts
              AND asset     = :asset
              AND (clob_up_ask IS NOT NULL OR clob_down_ask IS NOT NULL)
            ORDER BY eval_offset DESC NULLS LAST, created_at DESC
            LIMIT 1
            """
        )
        res = await session.execute(q, {"window_ts": window_epoch, "asset": asset.upper()})
        row = res.mappings().first()
    except ProgrammingError as exc:
        if _is_missing_table(exc):
            log.info("desk.clob_book.window_snapshots_missing")
            return empty
        log.warning("desk.clob_book.query_err", error=str(exc)[:200])
        return empty
    except Exception as exc:
        log.warning("desk.clob_book.unexpected_err", error=str(exc)[:200])
        return empty

    if not row:
        return empty

    # Age calculation
    age_s: Optional[int] = None
    stale = False
    created_at = row.get("created_at")
    if created_at is not None:
        try:
            import datetime as _dt
            if hasattr(created_at, "timestamp"):
                age_s = max(0, int(time.time() - created_at.timestamp()))
            else:
                age_s = None
        except Exception:
            age_s = None
        if age_s is not None and age_s > _CLOB_STALE_THRESHOLD_S:
            stale = True

    up_bid = row.get("clob_up_bid")
    up_ask = row.get("clob_up_ask")
    down_bid = row.get("clob_down_bid")
    down_ask = row.get("clob_down_ask")
    implied_p_up = row.get("clob_implied_up")

    # Compute spread from YES side if both sides present
    spread: Optional[float] = None
    if up_bid is not None and up_ask is not None:
        try:
            spread = round(float(up_ask) - float(up_bid), 6)
        except (TypeError, ValueError):
            pass

    # Convert implied_p_up to float safely
    implied_p_up_f: Optional[float] = None
    if implied_p_up is not None:
        try:
            implied_p_up_f = float(implied_p_up)
        except (TypeError, ValueError):
            pass

    return {
        "yes": {
            "bids": _one_level(up_bid),
            "asks": _one_level(up_ask),
        },
        "no": {
            "bids": _one_level(down_bid),
            "asks": _one_level(down_ask),
        },
        "implied_p_up": implied_p_up_f,
        "spread": spread,
        "imbalance": None,   # not stored in window_snapshots
        "source": "window_snapshots",
        "age_s": age_s,
        "stale": stale,
    }
