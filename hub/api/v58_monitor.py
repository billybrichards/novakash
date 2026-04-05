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
