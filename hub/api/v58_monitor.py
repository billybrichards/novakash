"""
v5.8 Monitor API

Endpoints for the v5.8 BTC trading strategy monitor dashboard.
Uses window_snapshots table (raw SQL — no ORM model yet).

GET /api/v58/windows         — last 50 window snapshots with all v5.8 fields
GET /api/v58/countdown/{ts}  — countdown evaluation stages for a specific window
GET /api/v58/stats           — win/loss/skip stats + agreement accuracy
GET /api/v58/price-history   — BTC price history for chart (last 1h from trades/signals)
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session

router = APIRouter()


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
    return {
        "window_ts": row["window_ts"].isoformat() if row["window_ts"] else None,
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
        q = text("""
            SELECT
                window_ts, asset, timeframe,
                open_price, close_price, delta_pct, vpin,
                regime, direction, confidence,
                trade_placed, skip_reason,
                twap_direction, twap_agreement_score, twap_gamma_gate,
                timesfm_direction, timesfm_confidence, timesfm_predicted_close, timesfm_agreement,
                gamma_up_price, gamma_down_price
            FROM window_snapshots
            WHERE (:asset IS NULL OR asset = :asset)
            ORDER BY window_ts DESC
            LIMIT :limit
        """)
        result = await session.execute(q, {"limit": limit, "asset": asset})
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
                COUNT(*) FILTER (WHERE twap_gamma_gate = TRUE)              AS twap_gate_passed,
                AVG(twap_agreement_score)                                   AS avg_twap_agreement
            FROM window_snapshots
            WHERE window_ts >= :since
        """)
        result = await session.execute(q, {"since": since})
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
            WHERE window_ts >= :since
              AND open_price IS NOT NULL
            ORDER BY window_ts ASC
            LIMIT 500
        """)
        result = await session.execute(q, {"since": since})
        rows = result.mappings().all()

        if rows:
            candles = []
            for r in rows:
                o = _safe_float(r["open"]) or 0.0
                c = _safe_float(r["close"]) or o
                candles.append({
                    "time": int(r["time"].timestamp()),
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

    # v5.8 decision: would trade if timesfm_agreement=True
    timesfm_agreement = row.get("timesfm_agreement")
    trade_placed = bool(row.get("trade_placed")) if row.get("trade_placed") is not None else False
    v58_would_trade = bool(timesfm_agreement) and trade_placed
    v58_pnl: Optional[float] = None
    v58_correct: Optional[bool] = None

    if v58_would_trade and actual_direction:
        v58_correct = v57c_correct  # v5.8 follows v5.7c direction when agreed
        v58_pnl = _calc_what_if_pnl(direction, actual_direction, gamma_up, gamma_down)

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
        "v58_would_trade": v58_would_trade,
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
                window_ts, asset, timeframe,
                open_price, close_price, delta_pct,
                direction, trade_placed, skip_reason,
                timesfm_direction, timesfm_confidence, timesfm_predicted_close, timesfm_agreement,
                twap_direction, twap_agreement_score, twap_gamma_gate,
                gamma_up_price, gamma_down_price,
                vpin, regime, confidence
            FROM window_snapshots
            WHERE (:asset IS NULL OR asset = :asset)
              AND close_price IS NOT NULL
            ORDER BY window_ts DESC
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
                gamma_up_price, gamma_down_price,
                vpin, regime, confidence
            FROM window_snapshots
            WHERE (:asset IS NULL OR asset = :asset)
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

        # Cumulative P&L (v5.8 trades only, newest-first → reverse for running total)
        pnls = [o["v58_pnl"] for o in reversed(outcomes) if o["v58_pnl"] is not None]
        cumulative_pnl = round(sum(pnls), 4)

        # Current win streak (from most recent backwards)
        streak = 0
        for o in outcomes:
            if not o["v58_would_trade"]:
                continue
            if o["v58_correct"] is True:
                streak += 1
            elif o["v58_correct"] is False:
                break

        # Cumulative P&L timeline for charting (newest-first from API, reversed for chart)
        pnl_timeline = []
        running = 0.0
        for o in reversed(outcomes):
            if o["v58_pnl"] is not None:
                running += o["v58_pnl"]
                pnl_timeline.append({
                    "window_ts": o["window_ts"],
                    "pnl": round(o["v58_pnl"], 4),
                    "cumulative": round(running, 4),
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
        "current_streak": 0,
        "pnl_timeline": [],
    }
