"""
v5.8 Monitor API

Endpoints for the v5.8 BTC trading strategy monitor dashboard.
Uses window_snapshots table (raw SQL — no ORM model yet).

GET  /api/v58/windows              — last 50 window snapshots with all v5.8 fields
GET  /api/v58/countdown/{ts}       — countdown evaluation stages for a specific window
GET  /api/v58/stats                — win/loss/skip stats + agreement accuracy
GET  /api/v58/price-history        — BTC price history for chart (last 1h from trades/signals)
GET  /api/v58/outcomes             — per-window outcome + what-if P&L analysis
GET  /api/v58/accuracy             — rolling accuracy stats
POST /api/v58/manual-trade         — place a paper or live manual trade
GET  /api/v58/manual-trades        — list all manual trades with outcomes
GET  /api/v58/window-detail/{ts}   — detailed window data for a specific timestamp
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session

router = APIRouter()


# ─── DB Migration helper (called from main.py lifespan) ─────────────────────

async def ensure_manual_trades_table(session: AsyncSession) -> None:
    """Create manual_trades table if it doesn't exist."""
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS manual_trades (
            id SERIAL PRIMARY KEY,
            trade_id VARCHAR(64) UNIQUE NOT NULL,
            window_ts BIGINT,
            asset VARCHAR(10) DEFAULT 'BTC',
            direction VARCHAR(4) NOT NULL,
            mode VARCHAR(10) NOT NULL,
            entry_price DOUBLE PRECISION NOT NULL,
            gamma_up_price DOUBLE PRECISION,
            gamma_down_price DOUBLE PRECISION,
            stake_usd DOUBLE PRECISION DEFAULT 4.0,
            status VARCHAR(20) DEFAULT 'open',
            outcome_direction VARCHAR(4),
            pnl_usd DOUBLE PRECISION,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            resolved_at TIMESTAMPTZ
        )
    """))
    await session.commit()


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _safe_float(v: Any) -> Optional[float]:
    """Convert a DB value to float, returning None on failure."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _row_to_window(row: Any) -> dict:
    """Map a window_snapshots row (RowMapping) to a serialisable dict."""
    # window_ts is BIGINT (unix epoch seconds), not a datetime
    wts = row["window_ts"]
    if wts is not None and isinstance(wts, (int, float)):
        wts_iso = datetime.fromtimestamp(int(wts), tz=timezone.utc).isoformat()
    elif wts is not None and hasattr(wts, 'isoformat'):
        wts_iso = wts.isoformat()
    else:
        wts_iso = str(wts) if wts else None
    return {
        "window_ts": wts_iso,
        "asset": row.get("asset"),
        "timeframe": row.get("timeframe"),
        "open_price": _safe_float(row.get("open_price")),
        "close_price": _safe_float(row.get("close_price")),
        "delta_pct": _safe_float(row.get("delta_pct")),
        "vpin": _safe_float(row.get("vpin")),
        "regime": row.get("regime"),
        "direction": row.get("direction"),
        "confidence": _safe_float(row.get("confidence")),
        "trade_placed": bool(row.get("trade_placed")) if row.get("trade_placed") is not None else None,
        "skip_reason": row.get("skip_reason"),
        # TWAP
        "twap_direction": row.get("twap_direction"),
        "twap_agreement_score": _safe_float(row.get("twap_agreement_score")),
        "twap_gamma_gate": bool(row.get("twap_gamma_gate")) if row.get("twap_gamma_gate") is not None else None,
        # TimesFM
        "timesfm_direction": row.get("timesfm_direction"),
        "timesfm_confidence": _safe_float(row.get("timesfm_confidence")),
        "timesfm_predicted_close": _safe_float(row.get("timesfm_predicted_close")),
        "timesfm_agreement": bool(row.get("timesfm_agreement")) if row.get("timesfm_agreement") is not None else None,
        # Gamma
        "gamma_up_price": _safe_float(row.get("gamma_up_price")),
        "gamma_down_price": _safe_float(row.get("gamma_down_price")),
        # Engine version
        "engine_version": row.get("engine_version"),
    }


# ─── Routes ──────────────────────────────────────────────────────────────────

@router.get("/v58/windows")
async def get_windows(
    limit: int = Query(50, ge=1, le=200),
    asset: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Return the most recent window snapshots with all v5.8 signal fields.

    Ordered newest-first. Falls back gracefully when the table doesn't exist.
    """
    try:
        if asset:
            q = text("""
                SELECT
                    window_ts, asset, timeframe,
                    open_price, close_price, delta_pct, vpin,
                    regime, direction, confidence,
                    trade_placed, skip_reason,
                    twap_direction, twap_agreement_score, twap_gamma_gate,
                    timesfm_direction, timesfm_confidence, timesfm_predicted_close, timesfm_agreement,
                    gamma_up_price, gamma_down_price, engine_version
                FROM window_snapshots
                WHERE asset = :asset
                ORDER BY window_ts DESC
                LIMIT :limit
            """)
            result = await session.execute(q, {"limit": limit, "asset": asset})
        else:
            q = text("""
                SELECT
                    window_ts, asset, timeframe,
                    open_price, close_price, delta_pct, vpin,
                    regime, direction, confidence,
                    trade_placed, skip_reason,
                    twap_direction, twap_agreement_score, twap_gamma_gate,
                    timesfm_direction, timesfm_confidence, timesfm_predicted_close, timesfm_agreement,
                    gamma_up_price, gamma_down_price, engine_version
                FROM window_snapshots
                ORDER BY window_ts DESC
                LIMIT :limit
            """)
            result = await session.execute(q, {"limit": limit})
        rows = result.mappings().all()
        return {"windows": [_row_to_window(r) for r in rows]}
    except Exception as exc:
        # Table may not exist yet — return empty rather than 500
        return {"windows": [], "error": str(exc)}


@router.get("/v58/countdown/{window_ts}")
async def get_countdown(
    window_ts: str,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Return countdown evaluation stages for a specific window timestamp.

    Looks for signals with payload.window_ts matching the provided value
    (stored by the engine when it evaluates T-180/T-120/T-90/T-60).
    """
    try:
        # Parse and validate the timestamp
        ts = datetime.fromisoformat(window_ts.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid window_ts format — use ISO 8601")

    try:
        # Try dedicated countdown_evaluations table first
        q = text("""
            SELECT
                window_ts, stage, evaluated_at,
                direction, confidence, agreement,
                action, notes
            FROM countdown_evaluations
            WHERE window_ts = :ts
            ORDER BY evaluated_at ASC
        """)
        result = await session.execute(q, {"ts": ts})
        rows = result.mappings().all()
        if rows:
            return {
                "window_ts": window_ts,
                "evaluations": [
                    {
                        "stage": r["stage"],
                        "evaluated_at": r["evaluated_at"].isoformat() if r.get("evaluated_at") else None,
                        "direction": r.get("direction"),
                        "confidence": _safe_float(r.get("confidence")),
                        "agreement": bool(r.get("agreement")) if r.get("agreement") is not None else None,
                        "action": r.get("action"),
                        "notes": r.get("notes"),
                    }
                    for r in rows
                ],
            }
    except Exception:
        pass

    # Fallback: look in the signals table for countdown payloads
    try:
        q2 = text("""
            SELECT signal_type, payload, created_at
            FROM signals
            WHERE signal_type LIKE 'countdown%'
              AND payload->>'window_ts' = :ts_str
            ORDER BY created_at ASC
            LIMIT 20
        """)
        result2 = await session.execute(q2, {"ts_str": ts.isoformat()})
        rows2 = result2.mappings().all()
        return {
            "window_ts": window_ts,
            "evaluations": [
                {
                    "stage": r["signal_type"],
                    "evaluated_at": r["created_at"].isoformat() if r.get("created_at") else None,
                    **r["payload"],
                }
                for r in rows2
            ],
        }
    except Exception as exc:
        return {"window_ts": window_ts, "evaluations": [], "error": str(exc)}


@router.get("/v58/stats")
async def get_stats(
    days: int = Query(7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Aggregate win/loss/skip stats and TimesFM agreement accuracy.

    Covers the last `days` days of window snapshots.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_epoch = int(since.timestamp())

    try:
        q = text("""
            SELECT
                COUNT(*)                                                    AS total_windows,
                COUNT(*) FILTER (WHERE trade_placed = TRUE)                 AS trades_placed,
                COUNT(*) FILTER (WHERE trade_placed = FALSE OR trade_placed IS NULL) AS windows_skipped,
                COUNT(*) FILTER (WHERE skip_reason IS NOT NULL)             AS explicit_skips,

                -- TimesFM agreement stats
                COUNT(*) FILTER (WHERE timesfm_agreement IS NOT NULL)       AS timesfm_evaluated,
                COUNT(*) FILTER (WHERE timesfm_agreement = TRUE)            AS timesfm_agreed,
                COUNT(*) FILTER (WHERE timesfm_agreement = FALSE)           AS timesfm_disagreed,

                -- Direction breakdown
                COUNT(*) FILTER (WHERE direction = 'UP')                    AS direction_up,
                COUNT(*) FILTER (WHERE direction = 'DOWN')                  AS direction_down,

                -- Confidence stats
                AVG(confidence)                                             AS avg_confidence,
                MIN(confidence)                                             AS min_confidence,
                MAX(confidence)                                             AS max_confidence,

                -- TWAP stats
                COUNT(*) FILTER (WHERE twap_gamma_gate = 'OK')               AS twap_gate_passed,
                AVG(twap_agreement_score)                                   AS avg_twap_agreement
            FROM window_snapshots
            WHERE window_ts >= :since_epoch
        """)
        result = await session.execute(q, {"since_epoch": since_epoch})
        row = result.mappings().first()

        if not row:
            return _empty_stats(days)

        total = int(row["total_windows"] or 0)
        evaluated = int(row["timesfm_evaluated"] or 0)
        agreed = int(row["timesfm_agreed"] or 0)

        return {
            "period_days": days,
            "since": since.isoformat(),
            "total_windows": total,
            "trades_placed": int(row["trades_placed"] or 0),
            "windows_skipped": int(row["windows_skipped"] or 0),
            "explicit_skips": int(row["explicit_skips"] or 0),
            "trade_rate_pct": round((int(row["trades_placed"] or 0) / total * 100) if total > 0 else 0, 1),
            "timesfm": {
                "evaluated": evaluated,
                "agreed": agreed,
                "disagreed": int(row["timesfm_disagreed"] or 0),
                "agreement_rate_pct": round((agreed / evaluated * 100) if evaluated > 0 else 0, 1),
            },
            "direction": {
                "up": int(row["direction_up"] or 0),
                "down": int(row["direction_down"] or 0),
            },
            "confidence": {
                "avg": _safe_float(row["avg_confidence"]),
                "min": _safe_float(row["min_confidence"]),
                "max": _safe_float(row["max_confidence"]),
            },
            "twap": {
                "gate_passed": int(row["twap_gate_passed"] or 0),
                "avg_agreement_score": _safe_float(row["avg_twap_agreement"]),
            },
        }
    except Exception as exc:
        return {**_empty_stats(days), "error": str(exc)}


def _empty_stats(days: int) -> dict:
    return {
        "period_days": days,
        "total_windows": 0,
        "trades_placed": 0,
        "windows_skipped": 0,
        "explicit_skips": 0,
        "trade_rate_pct": 0.0,
        "timesfm": {"evaluated": 0, "agreed": 0, "disagreed": 0, "agreement_rate_pct": 0.0},
        "direction": {"up": 0, "down": 0},
        "confidence": {"avg": None, "min": None, "max": None},
        "twap": {"gate_passed": 0, "avg_agreement_score": None},
    }


@router.get("/v58/price-history")
async def get_price_history(
    minutes: int = Query(60, ge=5, le=1440),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Return recent BTC price points for the live chart.

    Sources in priority order:
    1. window_snapshots open/close prices (most reliable for OHLC)
    2. signals table (tick payloads with btc_price)
    3. trades table (entry prices)

    Returns list of {time, open, high, low, close} OHLC candles.
    """
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)

    # ── Source 1: window snapshots ──────────────────────────────────────────
    try:
        q = text("""
            SELECT
                window_ts   AS time,
                open_price  AS open,
                open_price  AS high,
                open_price  AS low,
                close_price AS close,
                delta_pct,
                vpin,
                direction,
                trade_placed
            FROM window_snapshots
            WHERE window_ts >= :since_epoch
              AND open_price IS NOT NULL
            ORDER BY window_ts ASC
            LIMIT 500
        """)
        since_epoch = int(since.timestamp())
        result = await session.execute(q, {"since_epoch": since_epoch})
        rows = result.mappings().all()

        if rows:
            candles = []
            for r in rows:
                o = _safe_float(r["open"]) or 0.0
                c = _safe_float(r["close"]) or o
                candles.append({
                    "time": int(r["time"]) if isinstance(r["time"], (int, float)) else int(r["time"].timestamp()),
                    "open": o,
                    "high": max(o, c),
                    "low": min(o, c),
                    "close": c,
                    "delta_pct": _safe_float(r["delta_pct"]),
                    "vpin": _safe_float(r["vpin"]),
                    "direction": r.get("direction"),
                    "trade_placed": bool(r.get("trade_placed")),
                })
            return {"candles": candles, "source": "window_snapshots", "count": len(candles)}
    except Exception:
        pass

    # ── Source 2: signals table (tick events) ───────────────────────────────
    try:
        q2 = text("""
            SELECT
                created_at,
                payload->>'btc_price' AS price
            FROM signals
            WHERE signal_type = 'tick'
              AND created_at >= :since
              AND payload->>'btc_price' IS NOT NULL
            ORDER BY created_at ASC
            LIMIT 1000
        """)
        result2 = await session.execute(q2, {"since": since})
        rows2 = result2.mappings().all()

        if rows2:
            # Aggregate into 1-min candles
            candles = _aggregate_ticks_to_candles(rows2)
            return {"candles": candles, "source": "signals_tick", "count": len(candles)}
    except Exception:
        pass

    # ── Source 3: trades table ───────────────────────────────────────────────
    try:
        q3 = text("""
            SELECT
                created_at,
                entry_price AS price
            FROM trades
            WHERE created_at >= :since
              AND entry_price IS NOT NULL
            ORDER BY created_at ASC
            LIMIT 200
        """)
        result3 = await session.execute(q3, {"since": since})
        rows3 = result3.mappings().all()

        if rows3:
            candles = _aggregate_ticks_to_candles(rows3)
            return {"candles": candles, "source": "trades", "count": len(candles)}
    except Exception:
        pass

    return {"candles": [], "source": "none", "count": 0}


def _aggregate_ticks_to_candles(rows: list, interval_seconds: int = 60) -> list:
    """Group tick rows into OHLC candles of `interval_seconds` width."""
    buckets: dict[int, dict] = {}
    for r in rows:
        ts = r["created_at"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        bucket_ts = int(ts.timestamp() // interval_seconds) * interval_seconds
        price = _safe_float(r.get("price"))
        if price is None:
            continue
        if bucket_ts not in buckets:
            buckets[bucket_ts] = {"time": bucket_ts, "open": price, "high": price, "low": price, "close": price}
        else:
            b = buckets[bucket_ts]
            b["high"] = max(b["high"], price)
            b["low"] = min(b["low"], price)
            b["close"] = price

    return sorted(buckets.values(), key=lambda x: x["time"])


# ─── Outcome calculation helpers ─────────────────────────────────────────────

def _calc_what_if_pnl(direction: Optional[str], actual_direction: str,
                       gamma_up: Optional[float], gamma_down: Optional[float],
                       stake: float = 4.0, fee: float = 0.02) -> Optional[float]:
    """
    Calculate what-if P&L for a $stake bet using Polymarket prices.

    entry_price = gamma_up_price if direction=="UP" else gamma_down_price
    correct: win = (1 - entry_price) * stake * (1 - fee)
    wrong:   loss = -entry_price * stake
    """
    if not direction or gamma_up is None or gamma_down is None:
        return None

    entry_price = gamma_up if direction == "UP" else gamma_down
    if entry_price is None:
        return None

    correct = direction == actual_direction
    if correct:
        return round((1.0 - entry_price) * stake * (1.0 - fee), 4)
    else:
        return round(-entry_price * stake, 4)


def _calc_outcome_row(row: Any) -> dict:
    """Calculate outcome metrics for a single window_snapshots row."""
    open_p = _safe_float(row.get("open_price"))
    close_p = _safe_float(row.get("close_price"))

    # Actual direction from price movement
    actual_direction: Optional[str] = None
    if open_p is not None and close_p is not None:
        actual_direction = "UP" if close_p > open_p else "DOWN"

    direction = row.get("direction")  # v5.7c final call
    timesfm_dir = row.get("timesfm_direction")
    twap_dir = row.get("twap_direction")
    gamma_up = _safe_float(row.get("gamma_up_price"))
    gamma_down = _safe_float(row.get("gamma_down_price"))

    # Gamma implied direction: UP if gamma_up > gamma_down (more expensive UP = market favours UP)
    gamma_implied: Optional[str] = None
    if gamma_up is not None and gamma_down is not None:
        gamma_implied = "UP" if gamma_up > gamma_down else "DOWN"

    # Correctness flags
    timesfm_correct = (timesfm_dir == actual_direction) if (timesfm_dir and actual_direction) else None
    v57c_correct = (direction == actual_direction) if (direction and actual_direction) else None
    twap_correct = (twap_dir == actual_direction) if (twap_dir and actual_direction) else None
    gamma_correct = (gamma_implied == actual_direction) if (gamma_implied and actual_direction) else None

    # What-if P&L for each source
    timesfm_pnl = _calc_what_if_pnl(timesfm_dir, actual_direction, gamma_up, gamma_down) if actual_direction else None
    v57c_pnl = _calc_what_if_pnl(direction, actual_direction, gamma_up, gamma_down) if actual_direction else None
    twap_pnl = _calc_what_if_pnl(twap_dir, actual_direction, gamma_up, gamma_down) if actual_direction else None

    # v5.8 decision: would trade if TimesFM agrees with v5.7c direction
    # Compute agreement from actual columns (timesfm_agreement column is not populated)
    trade_placed = bool(row.get("trade_placed")) if row.get("trade_placed") is not None else False
    skip_reason = row.get("skip_reason")
    tfm_v57c_agree = (timesfm_dir == direction) if (timesfm_dir and direction) else None
    
    # v5.8 would trade if: TimesFM agrees with v5.7c AND v5.7c didn't skip on thresholds
    v58_would_trade = bool(tfm_v57c_agree) and not skip_reason
    v58_pnl: Optional[float] = None
    v58_correct: Optional[bool] = None
    v58_skip_reason: Optional[str] = None

    if not tfm_v57c_agree and timesfm_dir and direction:
        v58_skip_reason = f"DISAGREE: TimesFM={timesfm_dir} vs v5.7c={direction}"
    elif skip_reason:
        v58_skip_reason = skip_reason
    elif not timesfm_dir:
        v58_skip_reason = "No TimesFM forecast"
    elif not direction:
        v58_skip_reason = "No v5.7c signal"

    if v58_would_trade and actual_direction:
        v58_correct = v57c_correct
        v58_pnl = _calc_what_if_pnl(direction, actual_direction, gamma_up, gamma_down)

    # Always compute "ungated" P&L — what if we followed v5.7c regardless of gate?
    ungated_pnl = v57c_pnl  # already computed above for all windows with direction + prices
    ungated_correct = v57c_correct

    # Gate value: positive = gate saved us, negative = gate cost us profit
    gate_value: Optional[float] = None
    if ungated_pnl is not None and not v58_would_trade:
        gate_value = round(-ungated_pnl, 4)  # saved us from loss (positive) or blocked profit (negative)

    base = _row_to_window(row)
    base.update({
        "actual_direction": actual_direction,
        "gamma_implied_direction": gamma_implied,
        "timesfm_correct": timesfm_correct,
        "v57c_correct": v57c_correct,
        "twap_correct": twap_correct,
        "gamma_correct": gamma_correct,
        "timesfm_pnl": timesfm_pnl,
        "v57c_pnl": v57c_pnl,
        "twap_pnl": twap_pnl,
        "ungated_pnl": ungated_pnl,
        "ungated_correct": ungated_correct,
        "gate_value": gate_value,
        "v58_would_trade": v58_would_trade,
        "v58_skip_reason": v58_skip_reason,
        "tfm_v57c_agree": tfm_v57c_agree,
        "v58_correct": v58_correct,
        "v58_pnl": v58_pnl,
    })
    return base


# ─── New outcome + accuracy endpoints ────────────────────────────────────────

@router.get("/v58/outcomes")
async def get_outcomes(
    limit: int = Query(100, ge=1, le=500),
    asset: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Return outcome analysis for recent windows.

    For each window calculates:
    - actual_direction (UP/DOWN from open→close prices)
    - correctness flags for each signal source (TimesFM, v5.7c, TWAP, Gamma)
    - what-if P&L for $4 bets using Polymarket gamma prices
    - v5.8 decision (would_trade when timesfm_agreement=True)
    """
    try:
        q = text("""
            SELECT
                ws.window_ts, ws.asset, ws.timeframe,
                ws.open_price, ws.close_price, ws.delta_pct,
                ws.direction, ws.trade_placed, ws.skip_reason,
                ws.timesfm_direction, ws.timesfm_confidence, ws.timesfm_predicted_close, ws.timesfm_agreement,
                ws.twap_direction, ws.twap_agreement_score, ws.twap_gamma_gate,
                COALESCE(ws.gamma_up_price, ms.up_price) as gamma_up_price,
                COALESCE(ws.gamma_down_price, ms.down_price) as gamma_down_price,
                ws.engine_version,
                ws.vpin, ws.regime, ws.confidence
            FROM window_snapshots ws
            LEFT JOIN LATERAL (
                SELECT up_price, down_price
                FROM market_snapshots
                WHERE window_ts = ws.window_ts AND asset = ws.asset AND timeframe = ws.timeframe
                  AND up_price > 0.01 AND up_price < 0.99
                ORDER BY ABS(seconds_remaining - 60) NULLS LAST
                LIMIT 1
            ) ms ON true
            WHERE (CAST(:asset AS VARCHAR) IS NULL OR ws.asset = :asset)
              AND ws.close_price IS NOT NULL
            ORDER BY ws.window_ts DESC
            LIMIT :limit
        """)
        result = await session.execute(q, {"limit": limit, "asset": asset})
        rows = result.mappings().all()
        outcomes = [_calc_outcome_row(r) for r in rows]
        return {"outcomes": outcomes, "count": len(outcomes)}
    except Exception as exc:
        return {"outcomes": [], "count": 0, "error": str(exc)}


@router.get("/v58/accuracy")
async def get_accuracy(
    limit: int = Query(100, ge=10, le=500),
    asset: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Rolling accuracy statistics across recent windows.

    Returns accuracy percentages for each signal source,
    agreement rate, cumulative P&L, and current win streak.
    """
    try:
        q = text("""
            SELECT
                window_ts, asset, timeframe,
                open_price, close_price, delta_pct,
                direction, trade_placed, skip_reason,
                timesfm_direction, timesfm_confidence, timesfm_predicted_close, timesfm_agreement,
                twap_direction, twap_agreement_score, twap_gamma_gate,
                gamma_up_price, gamma_down_price, engine_version,
                vpin, regime, confidence
            FROM window_snapshots
            WHERE (CAST(:asset AS VARCHAR) IS NULL OR asset = :asset)
              AND close_price IS NOT NULL
              AND open_price IS NOT NULL
            ORDER BY window_ts DESC
            LIMIT :limit
        """)
        result = await session.execute(q, {"limit": limit, "asset": asset})
        rows = result.mappings().all()
        outcomes = [_calc_outcome_row(r) for r in rows]

        if not outcomes:
            return _empty_accuracy()

        # Accuracy calculations
        def _accuracy(items: list) -> float:
            filtered = [x for x in items if x is not None]
            if not filtered:
                return 0.0
            return round(sum(1 for x in filtered if x) / len(filtered) * 100, 1)

        timesfm_corrects = [o["timesfm_correct"] for o in outcomes]
        v57c_corrects = [o["v57c_correct"] for o in outcomes]
        twap_corrects = [o["twap_correct"] for o in outcomes]
        gamma_corrects = [o["gamma_correct"] for o in outcomes]

        # v5.8 accuracy: only when it would trade
        v58_trades = [o for o in outcomes if o["v58_would_trade"]]
        v58_corrects = [o["v58_correct"] for o in v58_trades]

        # Agreement rate: % of windows where TimesFM and v5.7c agreed
        agreement_windows = [
            o for o in outcomes
            if o.get("timesfm_direction") and o.get("direction")
        ]
        agreed_count = sum(
            1 for o in agreement_windows
            if o["timesfm_direction"] == o["direction"]
        )
        agreement_rate = round(
            (agreed_count / len(agreement_windows) * 100) if agreement_windows else 0.0, 1
        )

        # Cumulative P&L — gated (v5.8 only) vs ungated (every v5.7c signal)
        gated_pnls = [o["v58_pnl"] for o in reversed(outcomes) if o["v58_pnl"] is not None]
        cumulative_pnl = round(sum(gated_pnls), 4)

        ungated_pnls = [o["ungated_pnl"] for o in reversed(outcomes) if o["ungated_pnl"] is not None]
        ungated_cumulative = round(sum(ungated_pnls), 4)
        ungated_wins = sum(1 for o in outcomes if o.get("ungated_correct") is True)
        ungated_losses = sum(1 for o in outcomes if o.get("ungated_correct") is False)
        ungated_accuracy = round(ungated_wins / (ungated_wins + ungated_losses) * 100, 1) if (ungated_wins + ungated_losses) > 0 else 0.0

        # Gate value: how much did gating save/cost?
        gate_total = round(ungated_cumulative - cumulative_pnl, 4)

        # Current win streak (from most recent backwards)
        streak = 0
        for o in outcomes:
            if not o["v58_would_trade"]:
                continue
            if o["v58_correct"] is True:
                streak += 1
            elif o["v58_correct"] is False:
                break

        # Cumulative P&L timeline — both gated and ungated
        pnl_timeline = []
        running_gated = 0.0
        running_ungated = 0.0
        for o in reversed(outcomes):
            ungated_p = o.get("ungated_pnl")
            gated_p = o.get("v58_pnl")
            if ungated_p is not None:
                running_ungated += ungated_p
            if gated_p is not None:
                running_gated += gated_p
            if ungated_p is not None or gated_p is not None:
                pnl_timeline.append({
                    "window_ts": o["window_ts"],
                    "gated_pnl": round(gated_p, 4) if gated_p is not None else None,
                    "ungated_pnl": round(ungated_p, 4) if ungated_p is not None else None,
                    "gated_cumulative": round(running_gated, 4),
                    "ungated_cumulative": round(running_ungated, 4),
                })

        return {
            "windows_analysed": len(outcomes),
            "timesfm_accuracy": _accuracy(timesfm_corrects),
            "v57c_accuracy": _accuracy(v57c_corrects),
            "twap_accuracy": _accuracy(twap_corrects),
            "gamma_accuracy": _accuracy(gamma_corrects),
            "v58_accuracy": _accuracy(v58_corrects),
            "v58_trades_count": len(v58_trades),
            "agreement_rate": agreement_rate,
            "cumulative_pnl": cumulative_pnl,
            "ungated_pnl": ungated_cumulative,
            "ungated_accuracy": ungated_accuracy,
            "ungated_wins": ungated_wins,
            "ungated_losses": ungated_losses,
            "gate_value": gate_total,
            "current_streak": streak,
            "pnl_timeline": pnl_timeline,
        }
    except Exception as exc:
        return {**_empty_accuracy(), "error": str(exc)}


def _empty_accuracy() -> dict:
    return {
        "windows_analysed": 0,
        "timesfm_accuracy": 0.0,
        "v57c_accuracy": 0.0,
        "twap_accuracy": 0.0,
        "gamma_accuracy": 0.0,
        "v58_accuracy": 0.0,
        "v58_trades_count": 0,
        "agreement_rate": 0.0,
        "cumulative_pnl": 0.0,
        "ungated_pnl": 0.0,
        "ungated_accuracy": 0.0,
        "ungated_wins": 0,
        "ungated_losses": 0,
        "gate_value": 0.0,
        "current_streak": 0,
        "pnl_timeline": [],
    }


# ─── Manual Trade Schemas ─────────────────────────────────────────────────────

class ManualTradeRequest(BaseModel):
    asset: str = "BTC"
    direction: str          # "UP" or "DOWN"
    mode: str               # "paper" or "live"
    window_ts: Optional[int] = None   # unix timestamp (ms or s)


# ─── Gamma price helper ───────────────────────────────────────────────────────

async def _fetch_gamma_prices(window_ts: Optional[int]) -> dict:
    """
    Fetch current UP/DOWN prices from Polymarket Gamma API.
    Returns {"up_price": float|None, "down_price": float|None, "raw": dict}
    """
    try:
        # Build the slug — window_ts might be seconds or ms
        if window_ts:
            ts_s = window_ts // 1000 if window_ts > 1e10 else window_ts
            # Format: btc-updown-5m-{ts} — try standard slug
            slug = f"btc-updown-5m-{ts_s}"
        else:
            slug = "btc-updown-5m"

        url = f"https://gamma-api.polymarket.com/events?slug={slug}"
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            events = resp.json()

        if not events:
            return {"up_price": None, "down_price": None, "raw": {}}

        event = events[0] if isinstance(events, list) else events
        markets = event.get("markets", [])

        up_price = None
        down_price = None

        for market in markets:
            outcome_prices_raw = market.get("outcomePrices", "[]")
            outcomes = market.get("outcomes", "[]")

            # outcomePrices can be a JSON string or list
            if isinstance(outcome_prices_raw, str):
                import json as _json
                try:
                    prices = _json.loads(outcome_prices_raw)
                except Exception:
                    prices = []
            else:
                prices = outcome_prices_raw

            if isinstance(outcomes, str):
                import json as _json
                try:
                    outcomes = _json.loads(outcomes)
                except Exception:
                    outcomes = []

            for i, outcome_name in enumerate(outcomes):
                try:
                    price = float(prices[i])
                except (IndexError, TypeError, ValueError):
                    continue
                name_upper = str(outcome_name).upper()
                if "UP" in name_upper or "YES" in name_upper:
                    up_price = price
                elif "DOWN" in name_upper or "NO" in name_upper:
                    down_price = price

        return {"up_price": up_price, "down_price": down_price, "raw": event}

    except Exception as exc:
        return {"up_price": None, "down_price": None, "raw": {}, "error": str(exc)}


# ─── Manual Trade Endpoints ───────────────────────────────────────────────────

@router.get("/v58/live-prices")
async def get_live_prices(
    window_ts: Optional[int] = None,
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Fetch real-time Gamma prices for the trade preview.
    Returns UP/DOWN prices, spread, and what-if P&L at $4 stake.
    Called by the frontend every 2s to keep the preview fresh.
    """
    # Auto-detect current window if not provided
    if not window_ts:
        import time as _time
        now = int(_time.time())
        window_ts = (now // 300) * 300  # Current 5-min window
    
    gamma = await _fetch_gamma_prices(window_ts)
    up = gamma.get("up_price")
    down = gamma.get("down_price")

    stake = 4.0
    fee_mult = 0.98  # 2% Polymarket fee

    result = {
        "up_price": up,
        "down_price": down,
        "spread": round(abs(up - down), 4) if up and down else None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # What-if for UP bet
    if up and up > 0:
        result["up_bet"] = {
            "entry": round(up, 4),
            "stake": stake,
            "shares": round(stake / up, 2),
            "win_pnl": round((1.0 - up) * stake * fee_mult, 2),
            "loss_pnl": round(-up * stake, 2),
            "breakeven_pct": round(up * 100, 1),
        }

    # What-if for DOWN bet
    if down and down > 0:
        result["down_bet"] = {
            "entry": round(down, 4),
            "stake": stake,
            "shares": round(stake / down, 2),
            "win_pnl": round((1.0 - down) * stake * fee_mult, 2),
            "loss_pnl": round(-down * stake, 2),
            "breakeven_pct": round(down * 100, 1),
        }

    return result


@router.post("/v58/manual-trade")
async def post_manual_trade(
    body: ManualTradeRequest,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Place a manual paper or live trade for the current window.

    - Fetches real Gamma prices from Polymarket
    - Calculates entry_price based on direction
    - Records in manual_trades table
    - For live mode: records with status='pending_live' for engine pickup
    """
    # Validate direction
    direction = body.direction.upper()
    if direction not in ("UP", "DOWN"):
        raise HTTPException(status_code=422, detail="direction must be 'UP' or 'DOWN'")

    mode = body.mode.lower()
    if mode not in ("paper", "live"):
        raise HTTPException(status_code=422, detail="mode must be 'paper' or 'live'")

    # Ensure table exists
    await ensure_manual_trades_table(session)

    # Fetch Gamma prices
    gamma = await _fetch_gamma_prices(body.window_ts)
    up_price = gamma.get("up_price")
    down_price = gamma.get("down_price")

    # Determine entry price for chosen direction
    entry_price = up_price if direction == "UP" else down_price

    # Fallback: try to get from window_snapshots if Gamma API failed
    if entry_price is None and body.window_ts:
        try:
            ts_s = body.window_ts // 1000 if body.window_ts > 1e10 else body.window_ts
            ts_dt = datetime.fromtimestamp(ts_s, tz=timezone.utc)
            q = text("""
                SELECT gamma_up_price, gamma_down_price, engine_version
                FROM window_snapshots
                WHERE window_ts >= :ts_epoch - 600
                  AND window_ts <= :ts_epoch + 600
                ORDER BY ABS(window_ts - :ts_epoch)
                LIMIT 1
            """)
            result = await session.execute(q, {"ts": ts_dt, "ts_epoch": int(ts_dt.timestamp())})
            row = result.mappings().first()
            if row:
                up_price = _safe_float(row.get("gamma_up_price")) or up_price
                down_price = _safe_float(row.get("gamma_down_price")) or down_price
                entry_price = up_price if direction == "UP" else down_price
        except Exception:
            pass

    if entry_price is None:
        raise HTTPException(
            status_code=503,
            detail="Could not fetch Gamma prices — Polymarket API unavailable and no cached prices found"
        )

    # Generate trade ID
    trade_id = f"manual_{uuid.uuid4().hex[:16]}"
    stake = 4.0
    status = "open" if mode == "paper" else "pending_live"

    # Store in DB
    await session.execute(text("""
        INSERT INTO manual_trades
            (trade_id, window_ts, asset, direction, mode,
             entry_price, gamma_up_price, gamma_down_price,
             stake_usd, status, created_at)
        VALUES
            (:trade_id, :window_ts, :asset, :direction, :mode,
             :entry_price, :gamma_up_price, :gamma_down_price,
             :stake_usd, :status, NOW())
    """), {
        "trade_id": trade_id,
        "window_ts": body.window_ts,
        "asset": body.asset,
        "direction": direction,
        "mode": mode,
        "entry_price": entry_price,
        "gamma_up_price": up_price,
        "gamma_down_price": down_price,
        "stake_usd": stake,
        "status": status,
    })
    await session.commit()

    return {
        "trade_id": trade_id,
        "direction": direction,
        "entry_price": entry_price,
        "gamma_up_price": up_price,
        "gamma_down_price": down_price,
        "stake": stake,
        "mode": mode,
        "status": status,
        "asset": body.asset,
        "window_ts": body.window_ts,
    }


@router.get("/v58/manual-trades")
async def get_manual_trades(
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Return all manual trades with their current outcomes.

    Also attempts to resolve open trades against window_snapshots data.
    """
    await ensure_manual_trades_table(session)

    try:
        # Resolve any open trades that now have window outcome data
        await _resolve_open_trades(session)

        q = text("""
            SELECT
                mt.trade_id, mt.window_ts, mt.asset, mt.direction, mt.mode,
                mt.entry_price, mt.gamma_up_price, mt.gamma_down_price,
                mt.stake_usd, mt.status, mt.outcome_direction, mt.pnl_usd,
                mt.created_at, mt.resolved_at,
                ws.open_price, ws.close_price, ws.delta_pct, ws.direction AS signal_direction
            FROM manual_trades mt
            LEFT JOIN window_snapshots ws ON (
                mt.window_ts IS NOT NULL
                AND ws.window_ts >= (
                    CASE WHEN mt.window_ts > 1000000000000
                         THEN mt.window_ts / 1000
                         ELSE mt.window_ts
                    END
                ) - 300
                AND ws.window_ts <= (
                    CASE WHEN mt.window_ts > 1000000000000
                         THEN mt.window_ts / 1000
                         ELSE mt.window_ts
                    END
                ) + 300
            )
            ORDER BY mt.created_at DESC
            LIMIT :limit
        """)
        result = await session.execute(q, {"limit": limit})
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
                "gamma_up_price": _safe_float(r["gamma_up_price"]),
                "gamma_down_price": _safe_float(r["gamma_down_price"]),
                "stake_usd": _safe_float(r["stake_usd"]) or 4.0,
                "status": r["status"],
                "outcome_direction": r["outcome_direction"],
                "pnl_usd": _safe_float(r["pnl_usd"]),
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                "resolved_at": r["resolved_at"].isoformat() if r.get("resolved_at") else None,
                # Window context from join
                "open_price": _safe_float(r.get("open_price")),
                "close_price": _safe_float(r.get("close_price")),
                "delta_pct": _safe_float(r.get("delta_pct")),
                "signal_direction": r.get("signal_direction"),
            })

        # Compute running total
        resolved = [t for t in trades if t["pnl_usd"] is not None]
        total_pnl = round(sum(t["pnl_usd"] for t in resolved), 4)

        return {
            "trades": trades,
            "count": len(trades),
            "total_pnl": total_pnl,
            "resolved_count": len(resolved),
        }

    except Exception as exc:
        return {"trades": [], "count": 0, "total_pnl": 0.0, "resolved_count": 0, "error": str(exc)}


async def _resolve_open_trades(session: AsyncSession) -> None:
    """
    Attempt to resolve open manual trades that now have window outcome data.
    Looks up the matching window_snapshot and calculates P&L.
    """
    try:
        open_trades_q = text("""
            SELECT trade_id, window_ts, direction, entry_price, gamma_up_price, gamma_down_price, stake_usd
            FROM manual_trades
            WHERE status = 'open'
            LIMIT 50
        """)
        result = await session.execute(open_trades_q)
        open_trades = result.mappings().all()

        for t in open_trades:
            window_ts = t["window_ts"]
            if not window_ts:
                continue

            ts_s = window_ts // 1000 if window_ts > 1e12 else window_ts
            ts_dt = datetime.fromtimestamp(ts_s, tz=timezone.utc)

            # Window must have been closed — add 6min buffer
            if datetime.now(timezone.utc) < ts_dt + timedelta(minutes=6):
                continue

            # Look for matching snapshot with close price
            ws_q = text("""
                SELECT open_price, close_price, direction
                FROM window_snapshots
                WHERE window_ts >= :ts_epoch - 300
                  AND window_ts <= :ts_epoch + 300
                  AND close_price IS NOT NULL
                ORDER BY ABS(window_ts - :ts_epoch)
                LIMIT 1
            """)
            ws_result = await session.execute(ws_q, {"ts": ts_dt, "ts_epoch": int(ts_dt.timestamp())})
            ws_row = ws_result.mappings().first()

            if not ws_row:
                continue

            open_p = _safe_float(ws_row.get("open_price"))
            close_p = _safe_float(ws_row.get("close_price"))
            if open_p is None or close_p is None:
                continue

            actual_dir = "UP" if close_p > open_p else "DOWN"
            trade_dir = t["direction"]
            gamma_up = _safe_float(t.get("gamma_up_price"))
            gamma_down = _safe_float(t.get("gamma_down_price"))
            stake = _safe_float(t.get("stake_usd")) or 4.0

            pnl = _calc_what_if_pnl(trade_dir, actual_dir, gamma_up, gamma_down, stake)
            status = "won" if (pnl is not None and pnl > 0) else "lost"

            await session.execute(text("""
                UPDATE manual_trades
                SET status = :status,
                    outcome_direction = :outcome_dir,
                    pnl_usd = :pnl,
                    resolved_at = NOW()
                WHERE trade_id = :trade_id
            """), {
                "status": status,
                "outcome_dir": actual_dir,
                "pnl": pnl,
                "trade_id": t["trade_id"],
            })

        await session.commit()
    except Exception:
        pass  # Non-critical — don't fail the main request


@router.get("/v58/window-detail/{window_ts}")
async def get_window_detail(
    window_ts: str,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Return detailed window data for a specific timestamp.

    Includes:
    - Full snapshot row with all signals
    - Signal values at T-180/T-120/T-90/T-60 (from countdown_evaluations or signals table)
    - Price ticks through the window
    - What-if P&L calculation regardless of gate status
    - Resolution data (actual direction, actual P&L)
    """
    # Parse the timestamp — accept ISO string or unix seconds/ms
    ts_dt: Optional[datetime] = None
    try:
        # Try ISO first
        ts_dt = datetime.fromisoformat(window_ts.replace("Z", "+00:00"))
    except ValueError:
        # Try as unix timestamp
        try:
            ts_i = int(window_ts)
            ts_s = ts_i // 1000 if ts_i > 1e10 else ts_i
            ts_dt = datetime.fromtimestamp(ts_s, tz=timezone.utc)
        except (ValueError, OverflowError):
            raise HTTPException(status_code=422, detail="Invalid window_ts — use ISO 8601 or unix timestamp")

    # ── 1. Main snapshot row ─────────────────────────────────────────────────
    snapshot = None
    try:
        q = text("""
            SELECT *
            FROM window_snapshots
            WHERE window_ts >= :ts_epoch - 120
              AND window_ts <= :ts_epoch + 120
            ORDER BY ABS(window_ts - :ts_epoch)
            LIMIT 1
        """)
        result = await session.execute(q, {"ts": ts_dt, "ts_epoch": int(ts_dt.timestamp())})
        row = result.mappings().first()
        if row:
            snapshot = _calc_outcome_row(row)
    except Exception as exc:
        snapshot = {"error": str(exc)}

    # ── 2. Countdown evaluations (T-180/T-120/T-90/T-60) ───────────────────
    evaluations = []
    try:
        # Try countdown_evaluations table first
        ce_q = text("""
            SELECT stage, evaluated_at, direction, confidence, agreement, action, notes
            FROM countdown_evaluations
            WHERE window_ts >= :ts_epoch - 120
              AND window_ts <= :ts_epoch + 120
            ORDER BY evaluated_at ASC
        """)
        ce_result = await session.execute(ce_q, {"ts": ts_dt, "ts_epoch": int(ts_dt.timestamp())})
        ce_rows = ce_result.mappings().all()
        evaluations = [
            {
                "stage": r["stage"],
                "evaluated_at": r["evaluated_at"].isoformat() if r.get("evaluated_at") else None,
                "direction": r.get("direction"),
                "confidence": _safe_float(r.get("confidence")),
                "agreement": bool(r.get("agreement")) if r.get("agreement") is not None else None,
                "action": r.get("action"),
                "notes": r.get("notes"),
            }
            for r in ce_rows
        ]
    except Exception:
        pass

    if not evaluations:
        # Fallback: signals table
        try:
            ts_str = ts_dt.isoformat()
            sig_q = text("""
                SELECT signal_type, payload, created_at
                FROM signals
                WHERE signal_type LIKE 'countdown%'
                  AND (
                      payload->>'window_ts' = :ts_str
                      OR created_at BETWEEN :ts - INTERVAL '6 minutes' AND :ts + INTERVAL '1 minute'
                  )
                ORDER BY created_at ASC
                LIMIT 10
            """)
            sig_result = await session.execute(sig_q, {"ts_str": ts_str, "ts": ts_dt})
            sig_rows = sig_result.mappings().all()
            evaluations = [
                {
                    "stage": r["signal_type"],
                    "evaluated_at": r["created_at"].isoformat() if r.get("created_at") else None,
                    **(r["payload"] if isinstance(r.get("payload"), dict) else {}),
                }
                for r in sig_rows
            ]
        except Exception:
            pass

    # ── 3. Price ticks through the window ───────────────────────────────────
    price_ticks = []
    try:
        tick_q = text("""
            SELECT created_at, payload->>'btc_price' AS price
            FROM signals
            WHERE signal_type = 'tick'
              AND created_at BETWEEN :ts - INTERVAL '5 minutes' AND :ts + INTERVAL '5 minutes'
              AND payload->>'btc_price' IS NOT NULL
            ORDER BY created_at ASC
            LIMIT 200
        """)
        tick_result = await session.execute(tick_q, {"ts": ts_dt, "ts_epoch": int(ts_dt.timestamp())})
        tick_rows = tick_result.mappings().all()
        price_ticks = [
            {
                "time": int(r["created_at"].timestamp()) if r.get("created_at") else None,
                "price": _safe_float(r.get("price")),
            }
            for r in tick_rows
        ]
    except Exception:
        pass

    # ── 4. Entry timing from market_snapshots (v5.8.1) ──────────────────────
    # For each countdown stage (T-240, T-180, T-120, T-90, T-60), find the
    # Gamma market prices closest to that seconds_remaining value.
    # Also join with ticks_timesfm for TimesFM forecast at each stage.
    _ENTRY_STAGES = [
        {"stage": "T-240", "seconds": 240},
        {"stage": "T-180", "seconds": 180},
        {"stage": "T-120", "seconds": 120},
        {"stage": "T-90",  "seconds": 90},
        {"stage": "T-60",  "seconds": 60},
    ]
    entry_timing: list[dict] = []
    ts_epoch = int(ts_dt.timestamp())

    try:
        # Fetch all market_snapshots for this window (±5 min of window_ts)
        ms_q = text("""
            SELECT
                seconds_remaining,
                up_price,
                down_price,
                snapshot_at
            FROM market_snapshots
            WHERE window_ts >= :ts_epoch - 120
              AND window_ts <= :ts_epoch + 120
              AND up_price IS NOT NULL
              AND down_price IS NOT NULL
            ORDER BY seconds_remaining DESC
        """)
        ms_result = await session.execute(ms_q, {"ts_epoch": ts_epoch})
        ms_rows = ms_result.mappings().all()

        # Fetch TimesFM ticks for this window
        tfm_q = text("""
            SELECT
                seconds_to_close,
                direction,
                confidence
            FROM ticks_timesfm
            WHERE window_ts >= :ts_epoch - 120
              AND window_ts <= :ts_epoch + 120
            ORDER BY seconds_to_close DESC
        """)
        tfm_result = await session.execute(tfm_q, {"ts_epoch": ts_epoch})
        tfm_rows = tfm_result.mappings().all()

        def _closest_ms(target_secs: int) -> Optional[dict]:
            """Return market_snapshot row closest to target seconds_remaining."""
            if not ms_rows:
                return None
            best = min(ms_rows, key=lambda r: abs((r.get("seconds_remaining") or 0) - target_secs))
            gap = abs((best.get("seconds_remaining") or 0) - target_secs)
            if gap > 60:  # More than 60s off — don't report stale data
                return None
            return best

        def _closest_tfm(target_secs: int) -> Optional[dict]:
            """Return ticks_timesfm row closest to target seconds_to_close."""
            if not tfm_rows:
                return None
            best = min(tfm_rows, key=lambda r: abs((r.get("seconds_to_close") or 0) - target_secs))
            gap = abs((best.get("seconds_to_close") or 0) - target_secs)
            if gap > 90:
                return None
            return best

        for stage_def in _ENTRY_STAGES:
            stage_name = stage_def["stage"]
            stage_secs = stage_def["seconds"]
            ms = _closest_ms(stage_secs)
            tfm = _closest_tfm(stage_secs)

            entry_timing.append({
                "stage": stage_name,
                "seconds": stage_secs,
                "gamma_up": _safe_float(ms["up_price"]) if ms else None,
                "gamma_down": _safe_float(ms["down_price"]) if ms else None,
                "actual_seconds_remaining": ms.get("seconds_remaining") if ms else None,
                "timesfm_dir": tfm.get("direction") if tfm else None,
                "timesfm_conf": _safe_float(tfm.get("confidence")) if tfm else None,
            })

    except Exception as exc:
        entry_timing = [{"error": str(exc)}]

    # ── 5. What-if P&L for all signal sources + entry timing ────────────────
    # Calculate what-if P&L for each countdown stage AND for signal sources.
    # Always computed regardless of gate/skip status.
    what_if = None
    if snapshot and "actual_direction" in snapshot:
        actual_dir = snapshot.get("actual_direction")
        gamma_up = snapshot.get("gamma_up_price")
        gamma_down = snapshot.get("gamma_down_price")
        stake = 4.0

        # Per-source scenarios (v57c, timesfm, twap)
        scenarios = {}
        for src_name, dir_key in [
            ("v57c", "direction"),
            ("timesfm", "timesfm_direction"),
            ("twap", "twap_direction"),
        ]:
            src_dir = snapshot.get(dir_key)
            if src_dir and actual_dir:
                pnl = _calc_what_if_pnl(src_dir, actual_dir, gamma_up, gamma_down, stake)
                entry = gamma_up if src_dir == "UP" else gamma_down
                scenarios[src_name] = {
                    "direction": src_dir,
                    "entry_price": entry,
                    "stake": stake,
                    "actual_direction": actual_dir,
                    "correct": src_dir == actual_dir,
                    "pnl_usd": pnl,
                }

        # Per-entry-stage what-if (using the shadow direction from the snapshot)
        shadow_dir = snapshot.get("shadow_trade_direction") or snapshot.get("direction")
        entry_what_if = []
        if shadow_dir and actual_dir and entry_timing:
            best_stage = None
            best_pnl = None
            for et in entry_timing:
                if "error" in et:
                    continue
                et_gamma_up = et.get("gamma_up")
                et_gamma_down = et.get("gamma_down")
                if et_gamma_up is None or et_gamma_down is None:
                    entry_what_if.append({
                        "stage": et["stage"],
                        "entry": None,
                        "pnl": None,
                        "correct": None,
                    })
                    continue
                entry_price = et_gamma_up if shadow_dir == "UP" else et_gamma_down
                pnl = _calc_what_if_pnl(shadow_dir, actual_dir, et_gamma_up, et_gamma_down, stake)
                correct = shadow_dir == actual_dir
                entry_what_if.append({
                    "stage": et["stage"],
                    "entry": entry_price,
                    "pnl": pnl,
                    "correct": correct,
                })
                # Track best entry (highest P&L when correct, least loss when wrong)
                if best_pnl is None or (pnl is not None and pnl > best_pnl):
                    best_pnl = pnl
                    best_stage = et["stage"]

            # Mark best entry stage
            for ewi in entry_what_if:
                ewi["is_best"] = ewi["stage"] == best_stage

        gate_status = "BLOCKED" if snapshot.get("skip_reason") else "PASSED"
        if not snapshot.get("trade_placed") and not snapshot.get("skip_reason"):
            gate_status = "SKIPPED"

        what_if = {
            "gate_status": gate_status,
            "skip_reason": snapshot.get("skip_reason"),
            "trade_placed": snapshot.get("trade_placed"),
            "scenarios": scenarios,
            "entry_timing": entry_what_if,
            "best_entry_stage": best_stage if "best_stage" in dir() else None,
            "note": "P&L calculated regardless of gate/skip status",
        }

    return {
        "window_ts": window_ts,
        "snapshot": snapshot,
        "evaluations": evaluations,
        "price_ticks": price_ticks,
        "entry_timing": entry_timing,
        "what_if": what_if,
    }
