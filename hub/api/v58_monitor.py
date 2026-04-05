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
        # v7.1 Retroactive
        "v71_would_trade": bool(row.get("v71_would_trade")) if row.get("v71_would_trade") is not None else None,
        "v71_skip_reason": row.get("v71_skip_reason"),
        "v71_regime": row.get("v71_regime"),
        "v71_correct": bool(row.get("v71_correct")) if row.get("v71_correct") is not None else None,
        "v71_pnl": _safe_float(row.get("v71_pnl")),
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
                    gamma_up_price, gamma_down_price, engine_version,
                    v71_would_trade, v71_skip_reason, v71_regime, v71_correct, v71_pnl
                FROM window_snapshots
                WHERE asset = :asset AND timeframe = '5m'
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
                    gamma_up_price, gamma_down_price, engine_version,
                    v71_would_trade, v71_skip_reason, v71_regime, v71_correct, v71_pnl
                FROM window_snapshots
                WHERE timeframe = '5m'
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
            WHERE window_ts >= :since_epoch AND timeframe = '5m'
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
              AND timeframe = '5m'
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
                       stake: float = 10.0, fee: float = 0.02) -> Optional[float]:
    """
    Calculate what-if P&L for a $stake bet using Polymarket prices.

    entry_price = gamma_up_price if direction=="UP" else gamma_down_price
    correct: win = (1 - entry_price) * stake * (1 - fee)
    wrong:   loss = -entry_price * stake

    Returns None if entry price is 0, 1, or not in the real range (0.01–0.99).
    """
    if not direction or gamma_up is None or gamma_down is None:
        return None

    entry_price = gamma_up if direction == "UP" else gamma_down
    if entry_price is None:
        return None

    # Skip resolved prices ($0 or $1) — not a real entry price
    if entry_price <= 0.005 or entry_price >= 0.995:
        return None

    correct = direction == actual_direction
    if correct:
        return round((1.0 - entry_price) * stake * (1.0 - fee), 4)
    else:
        return round(-entry_price * stake, 4)


def _calc_v71_retroactive_decision(row: Any) -> dict:
    """
    Retroactively apply v7.1 thresholds to a historical window.
    
    v7.1 config:
    - vpin_gate: 0.45 (skip if VPIN < 0.45)
    - min_delta (NORMAL/TRANSITION): 0.02% (skip if |delta| < 0.02%)
    - min_delta (CASCADE): 0.01% (skip if |delta| < 0.01%)
    - cascade_threshold: 0.65
    - informed_threshold: 0.55
    
    Returns: {"v71_would_trade": bool, "v71_skip_reason": str, "v71_direction": str}
    """
    vpin = _safe_float(row.get("vpin"))
    delta_pct = _safe_float(row.get("delta_pct"))
    direction = row.get("direction")  # v5.7c direction as baseline
    timesfm_dir = row.get("timesfm_direction")
    
    # Constants for v7.1 (delta_pct from DB is already in percentage: -0.05 = -5%)
    VPIN_GATE = 0.45
    MIN_DELTA_NORMAL = 0.0002  # 0.02% (as decimal in DB: 0.02/100 = 0.0002)
    MIN_DELTA_CASCADE = 0.0001  # 0.01% (as decimal in DB: 0.01/100 = 0.0001)
    CASCADE_THRESHOLD = 0.65
    INFORMED_THRESHOLD = 0.55
    
    v71_would_trade = False
    v71_skip_reason = None
    v71_direction = direction  # default to v5.7c
    
    if not direction or vpin is None or delta_pct is None:
        v71_skip_reason = "Insufficient data for v7.1 retroactive"
        return {"v71_would_trade": False, "v71_skip_reason": v71_skip_reason, "v71_direction": None}
    
    # v7.1 Gate 1: VPIN gate
    if vpin < VPIN_GATE:
        v71_skip_reason = f"VPIN {vpin:.3f} < gate {VPIN_GATE} (TIMESFM_ONLY regime)"
        return {"v71_would_trade": False, "v71_skip_reason": v71_skip_reason, "v71_direction": None}
    
    # v7.1 Gate 2: Delta thresholds (regime-aware)
    abs_delta = abs(delta_pct)
    if vpin >= CASCADE_THRESHOLD:
        # CASCADE regime: min delta = 0.01%
        min_delta = MIN_DELTA_CASCADE
        regime = "CASCADE"
    elif vpin >= INFORMED_THRESHOLD:
        # TRANSITION regime: min delta = 0.02%
        min_delta = MIN_DELTA_NORMAL
        regime = "TRANSITION"
    else:
        # NORMAL regime: min delta = 0.02%
        min_delta = MIN_DELTA_NORMAL
        regime = "NORMAL"
    
    if abs_delta < min_delta:
        v71_skip_reason = f"Delta {abs_delta:.4f}% < v7.1 {regime} threshold {min_delta:.4f}%"
        return {"v71_would_trade": False, "v71_skip_reason": v71_skip_reason, "v71_direction": None}
    
    # v7.1 Would trade: VPIN passed, delta passed, direction from v5.7c
    v71_would_trade = True
    return {
        "v71_would_trade": True,
        "v71_skip_reason": None,
        "v71_direction": direction,
        "v71_regime": regime,
    }


def _calc_outcome_row(row: Any) -> dict:
    """Calculate outcome metrics for a single window_snapshots row.
    
    v7.1: Uses Polymarket resolution as source of truth when available.
    Falls back to Binance open→close if no trade/resolution exists.
    """
    open_p = _safe_float(row.get("open_price"))
    close_p = _safe_float(row.get("close_price"))

    # v7.1: Prefer Polymarket resolution (the actual payout truth)
    poly_outcome = row.get("poly_outcome")  # "WIN" or "LOSS" from trades table
    trade_direction = row.get("trade_direction")  # "YES" or "NO" from trades table
    
    actual_direction: Optional[str] = None
    if poly_outcome and trade_direction:
        # Polymarket resolved this window — use that as truth
        # If trade was YES (UP) and WON → actual was UP
        # If trade was YES (UP) and LOST → actual was DOWN
        # If trade was NO (DOWN) and WON → actual was DOWN
        # If trade was NO (DOWN) and LOST → actual was UP
        if trade_direction == "YES":
            actual_direction = "UP" if poly_outcome == "WIN" else "DOWN"
        else:
            actual_direction = "DOWN" if poly_outcome == "WIN" else "UP"
    elif open_p is not None and close_p is not None:
        # Fallback: Binance T-60s price (less accurate)
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
    # v7.1 Retroactive decision (how current config would have performed on old windows)
    v71_ret = _calc_v71_retroactive_decision(row)
    v71_would_trade = v71_ret.get("v71_would_trade", False)
    # v71_correct should only be set from actual Polymarket outcomes (DB),
    # NOT from directional match (which is misleading — 99%+ accuracy but only 76% WR)
    v71_correct_fallback: Optional[bool] = None
    v71_pnl_fallback: Optional[float] = None
    # Only use directional fallback if no DB value AND there's a trade outcome  
    if v71_would_trade and actual_direction and row.get("poly_outcome"):
        v71_direction = v71_ret.get("v71_direction")
        poly_outcome_str = str(row.get("poly_outcome") or "")
        v71_correct_fallback = poly_outcome_str == "WIN" if poly_outcome_str in ("WIN", "LOSS") else None
    elif v71_would_trade and actual_direction:
        # Shadow: use what-if P&L for windows that weren't traded
        v71_direction = v71_ret.get("v71_direction")
        v71_pnl_fallback = _calc_what_if_pnl(v71_direction, actual_direction, gamma_up, gamma_down)
    
    # v7.1: Use DB columns if backfilled, else fall back to calculation
    db_v71_would_trade = row.get("v71_would_trade")
    db_v71_skip_reason = row.get("v71_skip_reason")
    db_v71_regime = row.get("v71_regime")
    db_v71_correct = row.get("v71_correct")
    db_v71_pnl = _safe_float(row.get("v71_pnl"))
    
    # Use DB values if available, else use calculated
    final_v71_would_trade = db_v71_would_trade if db_v71_would_trade is not None else v71_would_trade
    final_v71_skip_reason = db_v71_skip_reason if db_v71_skip_reason is not None else v71_ret.get("v71_skip_reason")
    final_v71_regime = db_v71_regime if db_v71_regime is not None else v71_ret.get("v71_regime")
    final_v71_correct = db_v71_correct if db_v71_correct is not None else v71_correct_fallback
    final_v71_pnl = db_v71_pnl if db_v71_pnl is not None else v71_pnl_fallback
    
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
        "v71_would_trade": final_v71_would_trade,
        "v71_skip_reason": final_v71_skip_reason,
        "v71_regime": final_v71_regime,
        "v71_correct": final_v71_correct,
        "v71_pnl": final_v71_pnl,
        "poly_outcome": poly_outcome,
        "resolution_source": "polymarket" if poly_outcome else "binance_t60",
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
                ws.vpin, ws.regime, ws.confidence,
                ws.v71_would_trade, ws.v71_skip_reason, ws.v71_regime, ws.v71_correct, ws.v71_pnl,
                t.outcome AS poly_outcome,
                t.direction AS trade_direction
            FROM window_snapshots ws
            LEFT JOIN LATERAL (
                SELECT up_price, down_price
                FROM market_snapshots
                WHERE window_ts = ws.window_ts AND asset = ws.asset AND timeframe = ws.timeframe
                  AND up_price > 0.01 AND up_price < 0.99
                ORDER BY ABS(seconds_remaining - 60) NULLS LAST
                LIMIT 1
            ) ms ON true
            LEFT JOIN LATERAL (
                SELECT outcome, direction
                FROM trades
                WHERE strategy = 'five_min_vpin'
                  AND (metadata::json->>'window_ts')::bigint = ws.window_ts
                  AND outcome IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 1
            ) t ON true
            WHERE (CAST(:asset AS VARCHAR) IS NULL OR ws.asset = :asset)
              AND ws.close_price IS NOT NULL
              AND ws.timeframe = '5m'
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
                ws.window_ts, ws.asset, ws.timeframe,
                ws.open_price, ws.close_price, ws.delta_pct,
                ws.direction, ws.trade_placed, ws.skip_reason,
                ws.timesfm_direction, ws.timesfm_confidence, ws.timesfm_predicted_close, ws.timesfm_agreement,
                ws.twap_direction, ws.twap_agreement_score, ws.twap_gamma_gate,
                COALESCE(ws.gamma_up_price, ms.up_price)    AS gamma_up_price,
                COALESCE(ws.gamma_down_price, ms.down_price) AS gamma_down_price,
                ws.engine_version,
                ws.vpin, ws.regime, ws.confidence,
                t.outcome AS poly_outcome,
                t.direction AS trade_direction
            FROM window_snapshots ws
            LEFT JOIN LATERAL (
                SELECT up_price, down_price
                FROM market_snapshots
                WHERE window_ts = ws.window_ts
                  AND asset = ws.asset
                  AND timeframe = ws.timeframe
                ORDER BY ABS(seconds_remaining - 60)
                LIMIT 1
            ) ms ON true
            LEFT JOIN LATERAL (
                SELECT outcome, direction
                FROM trades
                WHERE strategy = 'five_min_vpin'
                  AND (metadata::json->>'window_ts')::bigint = ws.window_ts
                  AND outcome IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 1
            ) t ON true
            WHERE (CAST(:asset AS VARCHAR) IS NULL OR ws.asset = :asset)
              AND ws.close_price IS NOT NULL
              AND ws.open_price IS NOT NULL
              AND ws.timeframe = '5m'
            ORDER BY ws.window_ts DESC
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

        # v7.1: Count resolution sources
        poly_resolved = sum(1 for o in outcomes if o.get("poly_outcome"))
        binance_resolved = sum(1 for o in outcomes if o.get("resolution_source") == "binance_t60" and o.get("actual_direction"))

        # v7.1 stats: accuracy, P&L, streak, trade count
        v71_trades = [o for o in outcomes if o.get("v71_would_trade")]
        v71_corrects = [o.get("v71_correct") for o in v71_trades if o.get("v71_correct") is not None]
        v71_wins = sum(1 for c in v71_corrects if c is True)
        v71_losses = sum(1 for c in v71_corrects if c is False)
        v71_accuracy = round(v71_wins / (v71_wins + v71_losses) * 100, 1) if (v71_wins + v71_losses) > 0 else 0.0
        v71_pnl_total = round(sum(o.get("v71_pnl", 0) or 0 for o in v71_trades if o.get("v71_pnl") is not None), 2)
        
        # v7.1 streak (from most recent backwards)
        v71_streak = 0
        for o in outcomes:
            if not o.get("v71_would_trade"):
                continue
            if o.get("v71_correct") is True:
                v71_streak += 1
            elif o.get("v71_correct") is False:
                break

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
            "resolution_sources": {
                "polymarket": poly_resolved,
                "binance_t60": binance_resolved,
            },
            # v7.1 stats
            "v71_accuracy": v71_accuracy,
            "v71_trades_count": len(v71_trades),
            "v71_resolved_count": v71_wins + v71_losses,
            "v71_wins": v71_wins,
            "v71_losses": v71_losses,
            "v71_pnl": v71_pnl_total,
            "v71_streak": v71_streak,
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
            SELECT ws.*,
              t.outcome AS poly_outcome,
              t.direction AS trade_direction
            FROM window_snapshots ws
            LEFT JOIN LATERAL (
                SELECT outcome, direction
                FROM trades
                WHERE strategy = 'five_min_vpin'
                  AND (metadata::json->>'window_ts')::bigint = ws.window_ts
                  AND outcome IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 1
            ) t ON true
            WHERE ws.window_ts >= :ts_epoch - 120
              AND ws.window_ts <= :ts_epoch + 120
            ORDER BY ABS(ws.window_ts - :ts_epoch)
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


# ─── Gate Analysis endpoint ──────────────────────────────────────────────────

@router.get("/v58/gate-analysis")
async def get_gate_analysis(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Win rate at different VPIN gate levels + AI suggestion.
    Shows the tradeoff between gate strictness and win rate.
    """
    try:
        q = text("""
            SELECT 
                vpin_bucket,
                COUNT(*) as eligible,
                COUNT(*) FILTER (WHERE v71_correct = true) as wins,
                COUNT(*) FILTER (WHERE v71_correct = false) as losses,
                ROUND(AVG(CASE WHEN v71_correct IS NOT NULL THEN CASE WHEN v71_correct THEN 1.0 ELSE 0.0 END END) * 100, 1) as wr_pct,
                ROUND(SUM(COALESCE(v71_pnl, 0))::numeric, 2) as total_pnl
            FROM (
                SELECT 
                    CASE 
                        WHEN ws.vpin >= 0.65 THEN '0.65+'
                        WHEN ws.vpin >= 0.55 THEN '0.55-0.65'
                        WHEN ws.vpin >= 0.45 THEN '0.45-0.55'
                        WHEN ws.vpin >= 0.35 THEN '0.35-0.45'
                        ELSE '<0.35'
                    END as vpin_bucket,
                    CASE WHEN t.outcome = 'WIN' THEN TRUE ELSE FALSE END as v71_correct,
                    t.pnl_usd as v71_pnl
                FROM window_snapshots ws
                JOIN trades t ON (t.metadata::json->>'window_ts')::bigint = ws.window_ts
                    AND t.strategy = 'five_min_vpin' AND t.outcome IS NOT NULL
                WHERE ws.timeframe = '5m' AND ws.v71_would_trade = true
            ) x
            GROUP BY vpin_bucket ORDER BY vpin_bucket
        """)
        result = await session.execute(q)
        rows = result.mappings().all()
        
        buckets = []
        total_wins = 0
        total_losses = 0
        total_pnl = 0.0
        for r in rows:
            wins = int(r["wins"] or 0)
            losses = int(r["losses"] or 0)
            total_wins += wins
            total_losses += losses
            pnl = float(r["total_pnl"] or 0)
            total_pnl += pnl
            buckets.append({
                "vpin_range": r["vpin_bucket"],
                "eligible": int(r["eligible"] or 0),
                "wins": wins,
                "losses": losses,
                "wr_pct": float(r["wr_pct"] or 0),
                "pnl": pnl,
            })
        
        overall_wr = round(total_wins / (total_wins + total_losses) * 100, 1) if (total_wins + total_losses) > 0 else 0.0
        
        # Cumulative WR at each gate level (from strictest to loosest)
        cumulative = []
        cum_wins = 0
        cum_losses = 0
        cum_pnl = 0.0
        for b in reversed(buckets):
            cum_wins += b["wins"]
            cum_losses += b["losses"]
            cum_pnl += b["pnl"]
            cum_total = cum_wins + cum_losses
            cumulative.append({
                "gate_at": b["vpin_range"],
                "total_trades": cum_total,
                "wins": cum_wins,
                "losses": cum_losses,
                "wr_pct": round(cum_wins / cum_total * 100, 1) if cum_total > 0 else 0.0,
                "pnl": round(cum_pnl, 2),
            })
        cumulative.reverse()
        
        # Find the optimal gate (best WR with >= 20 trades)
        best_gate = None
        for c in cumulative:
            if c["total_trades"] >= 20 and (best_gate is None or c["wr_pct"] > best_gate["wr_pct"]):
                best_gate = c
        
        # AI suggestion
        current_gate = 0.45
        suggestion = ""
        if best_gate:
            if best_gate["wr_pct"] > overall_wr + 3:
                suggestion = f"Tighten gate to {best_gate['gate_at']} — WR improves to {best_gate['wr_pct']}% ({best_gate['total_trades']} trades) vs current {overall_wr}%"
            elif overall_wr >= 70:
                suggestion = f"Current gate is performing well at {overall_wr}% WR. No change recommended."
            else:
                suggestion = f"WR is {overall_wr}%. Consider tightening VPIN gate above 0.55 if WR drops below 65%."
        
        return {
            "buckets": buckets,
            "cumulative": cumulative,
            "overall_wr": overall_wr,
            "total_wins": total_wins,
            "total_losses": total_losses,
            "total_pnl": round(total_pnl, 2),
            "current_gate": current_gate,
            "best_gate": best_gate,
            "suggestion": suggestion,
        }
    except Exception as exc:
        return {"buckets": [], "cumulative": [], "error": str(exc)}


# ─── Strategy Analysis endpoint ──────────────────────────────────────────────

@router.get("/v58/strategy-analysis")
async def get_strategy_analysis(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """30-day backtest of v7.1 strategy against real Polymarket outcomes."""
    try:
        # 1. Base rate: UP vs DOWN over 30 days
        base_q = text("""
            SELECT 
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE outcome = 'UP') as up_count,
                COUNT(*) FILTER (WHERE outcome = 'DOWN') as down_count
            FROM market_data
            WHERE asset = 'BTC' AND timeframe = '5m' AND resolved AND outcome IS NOT NULL
        """)
        base = (await session.execute(base_q)).mappings().first()
        
        # 2. Daily breakdown
        daily_q = text("""
            SELECT 
                DATE(to_timestamp(window_ts) AT TIME ZONE 'UTC') as day,
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE outcome = 'UP') as up,
                COUNT(*) FILTER (WHERE outcome = 'DOWN') as down
            FROM market_data
            WHERE asset = 'BTC' AND timeframe = '5m' AND resolved AND outcome IS NOT NULL
            GROUP BY 1 ORDER BY 1
        """)
        daily_rows = (await session.execute(daily_q)).mappings().all()
        
        # 3. Hourly pattern
        hourly_q = text("""
            SELECT 
                EXTRACT(HOUR FROM to_timestamp(window_ts) AT TIME ZONE 'UTC')::int as hour,
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE outcome = 'UP') as up,
                COUNT(*) FILTER (WHERE outcome = 'DOWN') as down,
                ROUND(COUNT(*) FILTER (WHERE outcome = 'DOWN')::numeric / COUNT(*) * 100, 1) as down_pct
            FROM market_data
            WHERE asset = 'BTC' AND timeframe = '5m' AND resolved AND outcome IS NOT NULL
            GROUP BY 1 ORDER BY 1
        """)
        hourly_rows = (await session.execute(hourly_q)).mappings().all()
        
        # 4. Real trade performance (from trades table)
        real_q = text("""
            SELECT 
                COUNT(*) as trades,
                COUNT(*) FILTER (WHERE outcome = 'WIN') as wins,
                COUNT(*) FILTER (WHERE outcome = 'LOSS') as losses,
                ROUND(COUNT(*) FILTER (WHERE outcome = 'WIN')::numeric / 
                    NULLIF(COUNT(*) FILTER (WHERE outcome IS NOT NULL), 0) * 100, 1) as wr,
                ROUND(SUM(pnl_usd)::numeric, 2) as pnl,
                ROUND(AVG(entry_price)::numeric, 4) as avg_entry,
                MIN(created_at AT TIME ZONE 'UTC') as first_trade,
                MAX(created_at AT TIME ZONE 'UTC') as last_trade
            FROM trades WHERE strategy = 'five_min_vpin' AND outcome IS NOT NULL
        """)
        real = (await session.execute(real_q)).mappings().first()
        
        # 5. v7.1 performance from window_snapshots
        v71_q = text("""
            SELECT 
                COUNT(*) FILTER (WHERE v71_would_trade) as eligible,
                COUNT(*) FILTER (WHERE v71_correct = true) as wins,
                COUNT(*) FILTER (WHERE v71_correct = false) as losses,
                COUNT(*) FILTER (WHERE v71_correct IS NOT NULL) as resolved,
                ROUND(COUNT(*) FILTER (WHERE v71_correct = true)::numeric / 
                    NULLIF(COUNT(*) FILTER (WHERE v71_correct IS NOT NULL), 0) * 100, 1) as wr,
                ROUND(SUM(CASE WHEN v71_pnl IS NOT NULL THEN v71_pnl ELSE 0 END)::numeric, 2) as pnl
            FROM window_snapshots WHERE timeframe = '5m'
        """)
        v71 = (await session.execute(v71_q)).mappings().first()
        
        # 6. v7.1 by regime
        regime_q = text("""
            SELECT v71_regime,
                COUNT(*) as eligible,
                COUNT(*) FILTER (WHERE v71_correct = true) as wins,
                COUNT(*) FILTER (WHERE v71_correct = false) as losses,
                ROUND(COUNT(*) FILTER (WHERE v71_correct = true)::numeric / 
                    NULLIF(COUNT(*) FILTER (WHERE v71_correct IS NOT NULL), 0) * 100, 1) as wr
            FROM window_snapshots 
            WHERE timeframe = '5m' AND v71_would_trade = true AND v71_correct IS NOT NULL
            GROUP BY v71_regime ORDER BY v71_regime
        """)
        regime_rows = (await session.execute(regime_q)).mappings().all()
        
        # 7. Multi-asset comparison
        multi_q = text("""
            SELECT asset,
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE outcome = 'UP') as up,
                COUNT(*) FILTER (WHERE outcome = 'DOWN') as down,
                ROUND(COUNT(*) FILTER (WHERE outcome = 'DOWN')::numeric / COUNT(*) * 100, 1) as down_pct
            FROM market_data
            WHERE timeframe = '5m' AND resolved AND outcome IS NOT NULL
            GROUP BY asset ORDER BY asset
        """)
        multi_rows = (await session.execute(multi_q)).mappings().all()
        
        return {
            "base_rate": {
                "total": int(base["total"]),
                "up": int(base["up_count"]),
                "down": int(base["down_count"]),
                "up_pct": round(int(base["up_count"]) / int(base["total"]) * 100, 1) if base["total"] else 0,
                "down_pct": round(int(base["down_count"]) / int(base["total"]) * 100, 1) if base["total"] else 0,
            },
            "daily": [{"day": str(r["day"]), "total": int(r["total"]), "up": int(r["up"]), "down": int(r["down"])} for r in daily_rows],
            "hourly": [{"hour": int(r["hour"]), "total": int(r["total"]), "up": int(r["up"]), "down": int(r["down"]), "down_pct": float(r["down_pct"] or 0)} for r in hourly_rows],
            "real_trades": {
                "trades": int(real["trades"] or 0),
                "wins": int(real["wins"] or 0),
                "losses": int(real["losses"] or 0),
                "wr": float(real["wr"] or 0),
                "pnl": float(real["pnl"] or 0),
                "avg_entry": float(real["avg_entry"] or 0),
            },
            "v71": {
                "eligible": int(v71["eligible"] or 0),
                "resolved": int(v71["resolved"] or 0),
                "wins": int(v71["wins"] or 0),
                "losses": int(v71["losses"] or 0),
                "wr": float(v71["wr"] or 0),
                "pnl": float(v71["pnl"] or 0),
            },
            "v71_by_regime": [{"regime": r["v71_regime"], "eligible": int(r["eligible"]), "wins": int(r["wins"]), "losses": int(r["losses"]), "wr": float(r["wr"] or 0)} for r in regime_rows],
            "multi_asset": [{"asset": r["asset"], "total": int(r["total"]), "up": int(r["up"]), "down": int(r["down"]), "down_pct": float(r["down_pct"] or 0)} for r in multi_rows],
        }
    except Exception as exc:
        return {"error": str(exc)}


# ─── Live Wallet & Position Status ────────────────────────────────────────────

@router.get("/v58/wallet-status")
async def get_wallet_status(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Real-time Polymarket wallet status: balance, positions, pending redemptions."""
    try:
        # 1. Current system state (balance, mode)
        state_q = text("""
            SELECT current_balance, peak_balance, current_drawdown_pct,
                   paper_enabled, live_enabled, engine_status, last_heartbeat,
                   config::json->>'wallet_balance_usdc' as wallet_usdc,
                   config::json->>'daily_pnl' as daily_pnl,
                   config::json->>'paper_mode' as paper_mode
            FROM system_state WHERE id = 1
        """)
        state = (await session.execute(state_q)).mappings().first()
        
        # 2. Live trades summary
        live_q = text("""
            SELECT 
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE outcome = 'WIN') as wins,
                COUNT(*) FILTER (WHERE outcome = 'LOSS') as losses,
                COUNT(*) FILTER (WHERE outcome IS NULL) as pending,
                ROUND(SUM(CASE WHEN outcome IS NOT NULL THEN COALESCE(pnl_usd, 0) ELSE 0 END)::numeric, 2) as realized_pnl,
                ROUND(SUM(CASE WHEN outcome IS NULL THEN stake_usd ELSE 0 END)::numeric, 2) as open_exposure,
                COUNT(*) FILTER (WHERE is_live = true) as live_count,
                COUNT(*) FILTER (WHERE is_live = false OR is_live IS NULL) as paper_count,
                COUNT(*) FILTER (WHERE redeemed = true AND is_live = true) as redeemed_count,
                COUNT(*) FILTER (WHERE outcome IS NOT NULL AND is_live = true AND (redeemed = false OR redeemed IS NULL)) as pending_redemption
            FROM trades WHERE strategy = 'five_min_vpin'
        """)
        trades = (await session.execute(live_q)).mappings().first()
        
        # 3. Recent trades with live flag
        recent_q = text("""
            SELECT 
                created_at AT TIME ZONE 'UTC' as time,
                direction, entry_price, outcome, pnl_usd, stake_usd,
                is_live, redeemed, clob_order_id,
                metadata::json->>'window_ts' as window_ts,
                metadata::json->>'asset' as asset
            FROM trades 
            WHERE strategy = 'five_min_vpin'
            ORDER BY created_at DESC LIMIT 20
        """)
        recent = (await session.execute(recent_q)).mappings().all()
        
        # 4. Today's P&L breakdown
        today_q = text("""
            SELECT 
                CASE WHEN is_live THEN 'live' ELSE 'paper' END as mode,
                COUNT(*) as trades,
                COUNT(*) FILTER (WHERE outcome = 'WIN') as wins,
                COUNT(*) FILTER (WHERE outcome = 'LOSS') as losses,
                ROUND(SUM(COALESCE(pnl_usd, 0))::numeric, 2) as pnl
            FROM trades 
            WHERE strategy = 'five_min_vpin' AND created_at > CURRENT_DATE
            GROUP BY 1
        """)
        today = (await session.execute(today_q)).mappings().all()
        
        return {
            "engine": {
                "status": state["engine_status"] if state else "unknown",
                "paper_enabled": state["paper_enabled"] if state else True,
                "live_enabled": state["live_enabled"] if state else False,
                "paper_mode": str(state["paper_mode"]).lower() == "true" if state and state["paper_mode"] else True,
                "balance": float(state["current_balance"] or 0) if state else 0,
                "peak": float(state["peak_balance"] or 0) if state else 0,
                "drawdown_pct": float(state["current_drawdown_pct"] or 0) if state else 0,
                "wallet_usdc": float(state["wallet_usdc"] or 0) if state and state["wallet_usdc"] else None,
                "daily_pnl": float(state["daily_pnl"] or 0) if state and state["daily_pnl"] else 0,
                "last_heartbeat": state["last_heartbeat"].isoformat() if state and state["last_heartbeat"] else None,
            },
            "trades": {
                "total": int(trades["total"] or 0),
                "wins": int(trades["wins"] or 0),
                "losses": int(trades["losses"] or 0),
                "pending": int(trades["pending"] or 0),
                "realized_pnl": float(trades["realized_pnl"] or 0),
                "open_exposure": float(trades["open_exposure"] or 0),
                "live_count": int(trades["live_count"] or 0),
                "paper_count": int(trades["paper_count"] or 0),
                "pending_redemption": int(trades["pending_redemption"] or 0),
                "redeemed": int(trades["redeemed_count"] or 0),
            },
            "today": [{"mode": r["mode"], "trades": int(r["trades"]), "wins": int(r["wins"]), "losses": int(r["losses"]), "pnl": float(r["pnl"] or 0)} for r in today],
            "recent": [{
                "time": str(r["time"]),
                "direction": r["direction"],
                "entry_price": float(r["entry_price"]) if r["entry_price"] else None,
                "outcome": r["outcome"],
                "pnl": float(r["pnl_usd"]) if r["pnl_usd"] else None,
                "stake": float(r["stake_usd"]) if r["stake_usd"] else None,
                "is_live": bool(r["is_live"]) if r["is_live"] is not None else False,
                "redeemed": bool(r["redeemed"]) if r["redeemed"] is not None else False,
                "clob_id": r["clob_order_id"],
                "asset": r["asset"] or "BTC",
            } for r in recent],
        }
    except Exception as exc:
        return {"error": str(exc)}
