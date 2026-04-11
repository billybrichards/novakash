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

import json
import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session

log = structlog.get_logger(__name__)

# TIMESFM proxy base — same env var margin.py uses; never talks to Polymarket,
# only to our own TimesFM service hosting the v3/v4 decision surfaces.
TIMESFM_URL = os.environ.get("TIMESFM_URL", "http://localhost:8001")

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
    # Add order_type column if missing (migration-safe)
    await session.execute(text("""
        ALTER TABLE manual_trades ADD COLUMN IF NOT EXISTS order_type VARCHAR(5) DEFAULT 'FAK'
    """))
    await session.commit()


async def ensure_manual_trade_snapshots_table(session: AsyncSession) -> None:
    """
    LT-03 — Create manual_trade_snapshots table for operator-vs-engine
    ground-truth analysis.

    Every manual trade placed through /api/v58/manual-trade writes a companion
    row into this table capturing the full decision context at the moment the
    operator clicked: v4 fusion surface, v3 composite, last 5 resolved
    outcomes, macro bias, VPIN, and what the engine's gate pipeline would have
    decided for that same window. After resolution we know whether the
    operator was right, whether the engine was right, and where they disagree.

    JSONB columns let us capture the full surface without forcing a schema
    for every field the decision surface might add in future.
    """
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS manual_trade_snapshots (
            id SERIAL PRIMARY KEY,
            trade_id VARCHAR(64) NOT NULL,
            window_ts BIGINT NOT NULL,
            taken_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            -- Operator input
            operator_rationale TEXT,
            operator_direction CHAR(2) NOT NULL,

            -- Full v4 fusion surface at decision time (complete JSON)
            v4_snapshot JSONB,

            -- v3 composite signal surface (complete JSON)
            v3_snapshot JSONB,

            -- Last 5 resolved window outcomes preceding this decision
            last_5_window_outcomes JSONB,

            -- What the engine's gate pipeline decided for this window
            engine_would_have_done CHAR(5),
            engine_gate_reason VARCHAR(100),
            engine_direction CHAR(2),

            -- VPIN, macro bias snapshot
            vpin NUMERIC(6,4),
            macro_bias VARCHAR(16),
            macro_confidence INTEGER,

            -- Resolution (populated later when the trade resolves)
            resolved_at TIMESTAMPTZ,
            resolved_outcome CHAR(2),
            resolved_pnl_usd NUMERIC(10,4),
            operator_was_right BOOLEAN,
            engine_was_right BOOLEAN,

            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    await session.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_mts_trade_id ON manual_trade_snapshots(trade_id)"
    ))
    await session.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_mts_window_ts ON manual_trade_snapshots(window_ts DESC)"
    ))
    await session.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_mts_taken_at ON manual_trade_snapshots(taken_at DESC)"
    ))
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


# ─── v9.0 field derivation helpers ───────────────────────────────────────────

def _derive_source_agreement(row: Any) -> Optional[bool]:
    """Derive whether Chainlink + Tiingo agree on direction from their deltas."""
    dc = _safe_float(row.get("delta_chainlink"))
    dt = _safe_float(row.get("delta_tiingo"))
    if dc is None or dt is None:
        return None
    cl_dir = "UP" if dc > 0 else "DOWN"
    ti_dir = "UP" if dt > 0 else "DOWN"
    return cl_dir == ti_dir


def _derive_eval_tier(row: Any) -> Optional[str]:
    """Derive the v9.0 eval tier from VPIN + regime/confidence_tier.

    EARLY_CASCADE: VPIN >= 0.65 (high informed flow, early offsets)
    GOLDEN: VPIN >= 0.45 (T-130..T-60 golden zone)
    Returns None if data insufficient.
    """
    vpin = _safe_float(row.get("vpin"))
    regime = row.get("regime")
    tier = row.get("confidence_tier")
    if vpin is None:
        return None
    if vpin >= 0.65 and regime in ("CASCADE", "TRANSITION"):
        return "EARLY_CASCADE"
    if vpin >= 0.45:
        return "GOLDEN"
    return None


def _derive_v9_cap(row: Any) -> Optional[float]:
    """Derive the v9.0 dynamic entry cap used for this window."""
    tier = _derive_eval_tier(row)
    if tier == "EARLY_CASCADE":
        return 0.55
    if tier == "GOLDEN":
        return 0.65
    return None


def _derive_order_type(row: Any) -> Optional[str]:
    """Derive order type from execution_mode or engine_version."""
    exe = row.get("execution_mode")
    ev = row.get("engine_version") or ""
    if exe:
        exe_upper = str(exe).upper()
        if "FAK" in exe_upper:
            return "FAK"
        if "FOK" in exe_upper:
            return "FOK"
        if "GTC" in exe_upper:
            return "GTC"
    # v9.0+ uses FAK by default
    if "v9" in ev.lower():
        return "FAK"
    if row.get("fok_attempts") is not None:
        return "FOK"
    return None


def _derive_partial_fill(row: Any) -> Optional[bool]:
    """Detect FAK partial fill: fill_step < total attempts."""
    fok_attempts = row.get("fok_attempts")
    fok_fill_step = row.get("fok_fill_step")
    fill_price = _safe_float(row.get("clob_fill_price"))
    if fill_price is not None and fok_attempts is not None and fok_fill_step is not None:
        # If filled on first step of multiple, it might be partial
        return fok_fill_step < fok_attempts
    return None


def _derive_dune_cap(row: Any) -> Optional[float]:
    """Derive v10 DUNE dynamic cap: cap = DUNE_P - 5pp, bounded [0.30, 0.75].

    Uses v2_probability_up and source direction to calculate P(agreed direction),
    then cap = P - 0.05, clamped to floor/ceiling.
    Falls back to None if no DUNE data.
    """
    p_up = _safe_float(row.get("v2_probability_up"))
    if p_up is None:
        return None
    dc = _safe_float(row.get("delta_chainlink"))
    dt = _safe_float(row.get("delta_tiingo"))
    if dc is None or dt is None:
        return None
    cl_dir = "UP" if dc > 0 else "DOWN"
    ti_dir = "UP" if dt > 0 else "DOWN"
    if cl_dir != ti_dir:
        return None  # No agreed direction
    dune_p = p_up if cl_dir == "UP" else (1.0 - p_up)
    cap = round(min(max(dune_p - 0.05, 0.30), 0.75), 2)
    return cap


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
        # v8.0 execution metadata
        "delta_source": row.get("delta_source"),
        "execution_mode": row.get("execution_mode"),
        "fok_attempts": row.get("fok_attempts"),
        "fok_fill_step": row.get("fok_fill_step"),
        "clob_fill_price": _safe_float(row.get("clob_fill_price")),
        "gates_passed": row.get("gates_passed"),
        "gate_failed": row.get("gate_failed"),
        "confidence_tier": row.get("confidence_tier"),
        # Shadow resolution
        "shadow_trade_direction": row.get("shadow_trade_direction"),
        "shadow_trade_entry_price": _safe_float(row.get("shadow_trade_entry_price")),
        "oracle_outcome": row.get("oracle_outcome"),
        "shadow_pnl": _safe_float(row.get("shadow_pnl")),
        "shadow_would_win": bool(row.get("shadow_would_win")) if row.get("shadow_would_win") is not None else None,
        # Poly outcome from trades table (WIN/LOSS)
        "poly_outcome": row.get("poly_outcome"),
        # v9.0 fields — derived from existing columns
        "delta_chainlink": _safe_float(row.get("delta_chainlink")),
        "delta_tiingo": _safe_float(row.get("delta_tiingo")),
        "source_agreement": _derive_source_agreement(row),
        "eval_tier": _derive_eval_tier(row),
        "v9_cap": _derive_v9_cap(row),
        "order_type": _derive_order_type(row),
        "partial_fill": _derive_partial_fill(row),
        # v10 DUNE fields (from window_snapshots v2_probability_up column)
        "dune_probability_up": _safe_float(row.get("v2_probability_up")),
        "dune_direction": row.get("v2_direction"),
        "dune_agrees": bool(row.get("v2_agrees")) if row.get("v2_agrees") is not None else None,
        "dune_cap": _derive_dune_cap(row),
        "entry_reason": row.get("entry_reason"),
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
                    v71_would_trade, v71_skip_reason, v71_regime, v71_correct, v71_pnl,
                    delta_source, execution_mode, fok_attempts, fok_fill_step, clob_fill_price,
                    gates_passed, gate_failed, confidence_tier,
                    shadow_trade_direction, shadow_trade_entry_price,
                    oracle_outcome, shadow_pnl, shadow_would_win
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
                    v71_would_trade, v71_skip_reason, v71_regime, v71_correct, v71_pnl,
                    delta_source, execution_mode, fok_attempts, fok_fill_step, clob_fill_price,
                    gates_passed, gate_failed, confidence_tier,
                    shadow_trade_direction, shadow_trade_entry_price,
                    oracle_outcome, shadow_pnl, shadow_would_win
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
        # v8.0 fields — COALESCE window_snapshot with trades metadata fallback
        "delta_source": row.get("delta_source"),
        "execution_mode": row.get("execution_mode"),
        "fok_attempts": row.get("fok_attempts") or row.get("t_fok_attempts"),
        "fok_fill_step": row.get("fok_fill_step") or row.get("t_fok_fill_step"),
        "clob_fill_price": _safe_float(row.get("clob_fill_price")) or _safe_float(row.get("t_clob_fill_price")),
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
                ws.delta_source, ws.execution_mode,
                ws.fok_attempts, ws.fok_fill_step, ws.clob_fill_price,
                ws.shadow_trade_direction, ws.shadow_trade_entry_price,
                ws.oracle_outcome, ws.shadow_pnl, ws.shadow_would_win,
                t.outcome AS poly_outcome,
                t.direction AS trade_direction,
                (t.metadata::json->>'fok_attempts')::int AS t_fok_attempts,
                (t.metadata::json->>'fok_fill_step')::int AS t_fok_fill_step,
                (t.metadata::json->>'clob_fill_price')::float AS t_clob_fill_price
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
                SELECT outcome, direction, metadata
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
    order_type: str = "FAK"           # FAK, FOK, or GTC
    price_override: Optional[float] = None  # manual entry price override
    stake_usd: float = 4.0           # stake in USD
    # LT-03 — free-text "why did the operator click?" captured at trade time
    operator_rationale: Optional[str] = None


# ─── LT-03 decision-snapshot helper ───────────────────────────────────────────

async def _capture_trade_snapshot(
    session: AsyncSession,
    trade_id: str,
    window_ts: Optional[int],
    asset: str,
    operator_direction: str,
    operator_rationale: Optional[str],
) -> None:
    """
    Capture the full decision context at the moment the operator clicked.

    Writes one row into manual_trade_snapshots joining:
      - v4 fusion surface from TIMESFM (macro bias, per-TS recommended_action)
      - v3 composite signal surface from TIMESFM
      - last 5 resolved window outcomes from market_data
      - engine's gate-pipeline decision from signal_evaluations (if present)
      - VPIN, macro bias, macro confidence

    All upstream calls are wrapped in individual try/except so partial data
    still produces a snapshot row — the outer caller wraps this whole function
    in another try/except so even a total failure can't block trade execution.
    """
    # Ensure the table exists (cheap no-op after first call)
    try:
        await ensure_manual_trade_snapshots_table(session)
    except Exception:
        # Table creation failed — bail, the outer try/except logs this
        return

    # Normalise operator direction to CHAR(2) column: UP / DN
    op_dir_2 = "UP" if operator_direction.upper() == "UP" else "DN"

    # --- Fetch v4 fusion surface (5m + 15m + 1h) ---
    v4_snap: Optional[dict] = None
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            v4_resp = await client.get(
                f"{TIMESFM_URL}/v4/snapshot",
                params={"asset": asset, "timescales": "5m,15m,1h"},
            )
            if v4_resp.status_code == 200:
                v4_snap = v4_resp.json()
    except Exception:
        v4_snap = None

    # --- Fetch v3 composite signal surface ---
    v3_snap: Optional[dict] = None
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            v3_resp = await client.get(
                f"{TIMESFM_URL}/v3/snapshot",
                params={"asset": asset},
            )
            if v3_resp.status_code == 200:
                v3_snap = v3_resp.json()
    except Exception:
        v3_snap = None

    # --- Last 5 resolved window outcomes preceding this decision ---
    last_5_outcomes: list[dict] = []
    try:
        q = text("""
            SELECT window_ts, outcome, close_price, open_price
            FROM market_data
            WHERE asset = :asset
              AND timeframe = '5m'
              AND resolved = true
              AND outcome IS NOT NULL
            ORDER BY window_ts DESC
            LIMIT 5
        """)
        res = await session.execute(q, {"asset": asset})
        for row in res.mappings():
            open_p = _safe_float(row.get("open_price"))
            close_p = _safe_float(row.get("close_price"))
            delta_pct = None
            if open_p and close_p and open_p != 0:
                delta_pct = (close_p - open_p) / open_p
            last_5_outcomes.append({
                "window_ts": int(row["window_ts"]) if row.get("window_ts") is not None else None,
                "outcome": row.get("outcome"),
                "open_price": open_p,
                "close_price": close_p,
                "outcome_price_delta_pct": delta_pct,
            })
    except Exception:
        last_5_outcomes = []

    # --- What would the engine's gate pipeline have decided for THIS window? ---
    engine_would: Optional[str] = None
    engine_dir: Optional[str] = None
    engine_reason: Optional[str] = None
    vpin_val: Optional[float] = None
    if window_ts is not None:
        try:
            # window_ts from the frontend may be ms or s; signal_evaluations
            # stores epoch seconds, so normalise both sides.
            ts_s = window_ts // 1000 if window_ts > 1e10 else window_ts
            q = text("""
                SELECT decision, gate_failed, v2_direction, v2_probability_up, vpin
                FROM signal_evaluations
                WHERE asset = :asset
                  AND window_ts = :ts_s
                ORDER BY evaluated_at DESC NULLS LAST, eval_offset DESC
                LIMIT 1
            """)
            res = await session.execute(q, {"asset": asset, "ts_s": int(ts_s)})
            row = res.mappings().first()
            if row:
                engine_would = (row.get("decision") or "SKIP")[:5]
                raw_dir = row.get("v2_direction")
                if raw_dir:
                    engine_dir = "UP" if str(raw_dir).upper() == "UP" else "DN"
                elif row.get("v2_probability_up") is not None:
                    p_up = _safe_float(row.get("v2_probability_up"))
                    if p_up is not None:
                        engine_dir = "UP" if p_up >= 0.5 else "DN"
                raw_reason = row.get("gate_failed")
                if raw_reason:
                    engine_reason = str(raw_reason)[:100]
                vpin_val = _safe_float(row.get("vpin"))
        except Exception:
            pass

    # --- Macro bias + confidence lifted from v4 snapshot if present ---
    macro_bias: Optional[str] = None
    macro_conf: Optional[int] = None
    if v4_snap:
        try:
            macro = v4_snap.get("macro") or {}
            raw_bias = macro.get("bias")
            if raw_bias:
                macro_bias = str(raw_bias)[:16]
            raw_conf = macro.get("confidence")
            if raw_conf is not None:
                try:
                    macro_conf = int(float(raw_conf))
                except (TypeError, ValueError):
                    macro_conf = None
        except Exception:
            pass

    # --- Insert the snapshot row ---
    await session.execute(text("""
        INSERT INTO manual_trade_snapshots (
            trade_id, window_ts, operator_rationale, operator_direction,
            v4_snapshot, v3_snapshot, last_5_window_outcomes,
            engine_would_have_done, engine_gate_reason, engine_direction,
            vpin, macro_bias, macro_confidence
        ) VALUES (
            :trade_id, :window_ts, :rationale, :op_dir,
            CAST(:v4 AS JSONB), CAST(:v3 AS JSONB), CAST(:outcomes AS JSONB),
            :eng_would, :eng_reason, :eng_dir,
            :vpin, :macro_bias, :macro_conf
        )
    """), {
        "trade_id": trade_id,
        "window_ts": int(window_ts) if window_ts is not None else 0,
        "rationale": operator_rationale,
        "op_dir": op_dir_2,
        "v4": json.dumps(v4_snap) if v4_snap else None,
        "v3": json.dumps(v3_snap) if v3_snap else None,
        "outcomes": json.dumps(last_5_outcomes),
        "eng_would": engine_would,
        "eng_reason": engine_reason,
        "eng_dir": engine_dir,
        "vpin": vpin_val,
        "macro_bias": macro_bias,
        "macro_conf": macro_conf,
    })
    await session.commit()


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

    # Determine entry price: use override if provided, else Gamma price
    if body.price_override is not None and body.price_override > 0:
        entry_price = body.price_override
    else:
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
    stake = body.stake_usd
    order_type = body.order_type.upper() if body.order_type else "FAK"
    if order_type not in ("FAK", "FOK", "GTC"):
        order_type = "FAK"
    status = "open" if mode == "paper" else "pending_live"

    # Store in DB
    await session.execute(text("""
        INSERT INTO manual_trades
            (trade_id, window_ts, asset, direction, mode,
             entry_price, gamma_up_price, gamma_down_price,
             stake_usd, status, order_type, created_at)
        VALUES
            (:trade_id, :window_ts, :asset, :direction, :mode,
             :entry_price, :gamma_up_price, :gamma_down_price,
             :stake_usd, :status, :order_type, NOW())
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
        "order_type": order_type,
    })
    await session.commit()

    # ── LT-03: capture decision snapshot for operator-vs-engine analysis ──
    # This block is wrapped in a top-level try so a snapshot capture failure
    # can NEVER break the trade execution path — the manual_trades row has
    # already been committed above by the time we reach here.
    try:
        await _capture_trade_snapshot(
            session=session,
            trade_id=trade_id,
            window_ts=body.window_ts,
            asset=body.asset,
            operator_direction=direction,
            operator_rationale=body.operator_rationale,
        )
    except Exception as snap_exc:
        log.warning(
            "lt03.snapshot_capture_failed",
            error=str(snap_exc)[:200],
            trade_id=trade_id,
        )

    return {
        "trade_id": trade_id,
        "direction": direction,
        "entry_price": entry_price,
        "gamma_up_price": up_price,
        "gamma_down_price": down_price,
        "stake": stake,
        "order_type": order_type,
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


@router.get("/v58/manual-trade-snapshots")
async def get_manual_trade_snapshots(
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    LT-03 — return recent manual-trade decision snapshots joined with the
    resolved outcome from the manual_trades row.

    This is the read endpoint a future `/decision-review` frontend page will
    hit to render the side-by-side operator-vs-engine comparison. For now
    it's read-only and paginated via `limit` only — no filtering, no
    pagination cursors. Populated incrementally by every manual-trade POST.
    """
    # Ensure the table exists so a fresh DB doesn't 500 the frontend
    try:
        await ensure_manual_trade_snapshots_table(session)
    except Exception as exc:
        return {"rows": [], "count": 0, "error": f"schema: {exc}"}

    try:
        q = text("""
            SELECT
                mts.id,
                mts.trade_id,
                mts.window_ts,
                mts.taken_at,
                mts.operator_rationale,
                mts.operator_direction,
                mts.v4_snapshot,
                mts.v3_snapshot,
                mts.last_5_window_outcomes,
                mts.engine_would_have_done,
                mts.engine_gate_reason,
                mts.engine_direction,
                mts.vpin,
                mts.macro_bias,
                mts.macro_confidence,
                mts.resolved_at       AS mts_resolved_at,
                mts.resolved_outcome,
                mts.resolved_pnl_usd,
                mts.operator_was_right,
                mts.engine_was_right,
                mt.pnl_usd            AS mt_pnl_usd,
                mt.outcome_direction  AS mt_outcome_direction,
                mt.resolved_at        AS mt_resolved_at,
                mt.status             AS mt_status,
                mt.mode               AS mt_mode,
                mt.stake_usd          AS mt_stake_usd
            FROM manual_trade_snapshots mts
            LEFT JOIN manual_trades mt ON mt.trade_id = mts.trade_id
            ORDER BY mts.taken_at DESC
            LIMIT :lim
        """)
        res = await session.execute(q, {"lim": limit})
        rows_out: list[dict] = []
        for r in res.mappings():
            rows_out.append({
                "id": r.get("id"),
                "trade_id": r.get("trade_id"),
                "window_ts": r.get("window_ts"),
                "taken_at": r["taken_at"].isoformat() if r.get("taken_at") else None,
                "operator_rationale": r.get("operator_rationale"),
                "operator_direction": r.get("operator_direction"),
                "v4_snapshot": r.get("v4_snapshot"),
                "v3_snapshot": r.get("v3_snapshot"),
                "last_5_window_outcomes": r.get("last_5_window_outcomes"),
                "engine_would_have_done": r.get("engine_would_have_done"),
                "engine_gate_reason": r.get("engine_gate_reason"),
                "engine_direction": r.get("engine_direction"),
                "vpin": _safe_float(r.get("vpin")),
                "macro_bias": r.get("macro_bias"),
                "macro_confidence": r.get("macro_confidence"),
                "resolved_at": (
                    r["mts_resolved_at"].isoformat()
                    if r.get("mts_resolved_at")
                    else (r["mt_resolved_at"].isoformat() if r.get("mt_resolved_at") else None)
                ),
                "resolved_outcome": r.get("resolved_outcome") or r.get("mt_outcome_direction"),
                "resolved_pnl_usd": _safe_float(r.get("resolved_pnl_usd"))
                                    if r.get("resolved_pnl_usd") is not None
                                    else _safe_float(r.get("mt_pnl_usd")),
                "operator_was_right": r.get("operator_was_right"),
                "engine_was_right": r.get("engine_was_right"),
                "mt_status": r.get("mt_status"),
                "mt_mode": r.get("mt_mode"),
                "mt_stake_usd": _safe_float(r.get("mt_stake_usd")),
            })
        return {"rows": rows_out, "count": len(rows_out)}
    except Exception as exc:
        return {"rows": [], "count": 0, "error": str(exc)}


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


# ─── Execution HQ endpoint ─────────────────────────────────────────────────

# UI-02: canonical asset / timeframe sets for the multi-market HQ monitors.
# Kept at module scope so they're easy to grow and easy to import from tests.
_HQ_ASSETS = {"btc", "eth", "sol", "xrp"}
_HQ_TIMEFRAMES = {"5m", "15m"}


@router.get("/v58/execution-hq")
async def get_execution_hq(
    limit: int = Query(200, ge=1, le=500),
    shadow_only: bool = Query(False),
    asset: str = Query("btc"),
    timeframe: str = Query("5m"),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Combined endpoint for the Execution HQ dashboard.

    Query params (UI-02):
    - ``asset``     — one of {btc, eth, sol, xrp}. Defaults to ``btc`` so the
                      unparameterised call ``/api/v58/execution-hq`` keeps the
                      legacy BTC-5m behavior for backward compatibility.
    - ``timeframe`` — one of {5m, 15m}. Defaults to ``5m``.

    Returns:
    - windows: recent window snapshots with all columns including shadow resolution
    - shadow_stats: aggregate stats on missed opportunities
    - recent_trades: last 20 trades for execution log
    - system: current engine state (bankroll, mode, status)
    - gate_heartbeat: last 50 signal_evaluations rows for the asset/timeframe
    - asset / timeframe: echoed back so the client can verify the request

    If the asset/timeframe combo isn't being written by the data-collector yet
    (e.g. ETH 15m on day 1), this returns empty arrays — **not** a 500 — so
    the UI can render a clean "no data yet" banner.
    """
    asset_norm = (asset or "").strip().lower()
    tf_norm = (timeframe or "").strip().lower()
    if asset_norm not in _HQ_ASSETS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown asset '{asset}'. Valid: {sorted(_HQ_ASSETS)}",
        )
    if tf_norm not in _HQ_TIMEFRAMES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown timeframe '{timeframe}'. Valid: {sorted(_HQ_TIMEFRAMES)}",
        )
    # Engine writes asset uppercased in window_snapshots/signal_evaluations
    # (see FiveMinSignal + db_client INSERTs), but we normalise incoming
    # params to lowercase so the route and caller don't have to care.
    asset_upper = asset_norm.upper()
    # trades.market_slug follows the pattern "<asset>-updown-<tf>-<ts>"
    # and the engine lowercases the asset part (see
    # engine/strategies/five_min_vpin.py ~line 2528).
    market_slug_prefix = f"{asset_norm}-updown-{tf_norm}-"

    try:
        # ── Windows with shadow data ──────────────────────────────────────
        # Build WHERE clauses safely using parameterised conditions
        conditions = [
            "ws.asset = :asset",
            "ws.timeframe = :timeframe",
        ]
        params: dict = {
            "limit": limit,
            "asset": asset_upper,
            "timeframe": tf_norm,
        }

        if shadow_only:
            conditions.append("ws.shadow_would_win = TRUE")
            conditions.append("ws.trade_placed = FALSE")

        where_clause = " AND ".join(conditions)

        q = text(f"""
            SELECT
                ws.window_ts, ws.asset, ws.timeframe,
                ws.open_price, ws.close_price, ws.delta_pct, ws.vpin,
                ws.regime, ws.direction, ws.confidence,
                ws.trade_placed, ws.skip_reason,
                ws.twap_direction, ws.twap_agreement_score, ws.twap_gamma_gate,
                ws.timesfm_direction, ws.timesfm_confidence, ws.timesfm_predicted_close, ws.timesfm_agreement,
                ws.gamma_up_price, ws.gamma_down_price, ws.engine_version,
                ws.v71_would_trade, ws.v71_skip_reason, ws.v71_regime, ws.v71_correct, ws.v71_pnl,
                ws.delta_source, ws.execution_mode, ws.fok_attempts, ws.fok_fill_step, ws.clob_fill_price,
                ws.gates_passed, ws.gate_failed, ws.confidence_tier,
                ws.shadow_trade_direction, ws.shadow_trade_entry_price,
                ws.oracle_outcome, ws.shadow_pnl, ws.shadow_would_win,
                ws.delta_chainlink, ws.delta_tiingo, ws.price_consensus,
                ws.v2_probability_up, ws.v2_direction, ws.v2_agrees,
                t.outcome AS poly_outcome,
                t.entry_reason AS entry_reason
            FROM window_snapshots ws
            LEFT JOIN LATERAL (
                SELECT outcome, metadata::json->>'entry_reason' AS entry_reason
                FROM trades
                WHERE strategy = 'five_min_vpin'
                  AND market_slug ILIKE :market_slug_like
                  AND (metadata::json->>'window_ts')::bigint = ws.window_ts
                  AND outcome IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 1
            ) t ON true
            WHERE {where_clause}
            ORDER BY ws.window_ts DESC
            LIMIT :limit
        """)
        params["market_slug_like"] = market_slug_prefix + "%"
        result = await session.execute(q, params)
        rows = result.mappings().all()
        windows = [_row_to_window(r) for r in rows]

        # ── Shadow stats (scoped to this asset/timeframe) ─────────────────
        stats_conditions = [
            "asset = :asset",
            "timeframe = :timeframe",
        ]
        stats_params: dict = {
            "asset": asset_upper,
            "timeframe": tf_norm,
        }

        stats_where = " AND ".join(stats_conditions)

        sq = text(f"""
            SELECT
                COUNT(*) FILTER (
                    WHERE trade_placed = FALSE AND shadow_trade_direction IS NOT NULL
                ) AS total_skipped_with_shadow,
                COUNT(*) FILTER (WHERE shadow_would_win = TRUE) AS shadow_wins,
                COUNT(*) FILTER (
                    WHERE shadow_would_win = FALSE AND oracle_outcome IS NOT NULL
                ) AS shadow_losses,
                COALESCE(SUM(shadow_pnl) FILTER (WHERE shadow_would_win = TRUE), 0) AS pnl_missed,
                COALESCE(SUM(shadow_pnl) FILTER (
                    WHERE shadow_would_win = FALSE AND oracle_outcome IS NOT NULL
                ), 0) AS pnl_avoided,
                COUNT(*) FILTER (WHERE trade_placed = TRUE) AS total_traded,
                COUNT(*) AS total_windows
            FROM window_snapshots
            WHERE {stats_where}
        """)
        sresult = await session.execute(sq, stats_params)
        srow = sresult.mappings().first()

        total_with_shadow = int(srow["total_skipped_with_shadow"] or 0)
        shadow_wins = int(srow["shadow_wins"] or 0)
        shadow_losses = int(srow["shadow_losses"] or 0)

        shadow_stats = {
            "total_skipped_with_shadow": total_with_shadow,
            "shadow_wins": shadow_wins,
            "shadow_losses": shadow_losses,
            "shadow_win_rate": round(shadow_wins / max(shadow_wins + shadow_losses, 1) * 100, 1),
            "pnl_missed": round(float(srow["pnl_missed"] or 0), 2),
            "pnl_avoided": round(float(srow["pnl_avoided"] or 0), 2),
            "total_traded": int(srow["total_traded"] or 0),
            "total_windows": int(srow["total_windows"] or 0),
        }

        # ── Recent trades (execution log) ─────────────────────────────────
        # Filter by market_slug prefix so each HQ only shows trades for its
        # asset × timeframe. Engine writes market_slug as
        # "<asset_lower>-updown-<tf>-<window_ts>" (see five_min_vpin.py ~2528).
        tq = text("""
            SELECT id, strategy, direction, entry_price, stake_usd,
                   outcome, pnl_usd, created_at, status
            FROM trades
            WHERE strategy = 'five_min_vpin'
              AND market_slug ILIKE :market_slug_like
            ORDER BY created_at DESC
            LIMIT 20
        """)
        tresult = await session.execute(tq, {"market_slug_like": market_slug_prefix + "%"})
        trows = tresult.mappings().all()
        recent_trades = [{
            "id": r["id"],
            "direction": r["direction"],
            "entry_price": _safe_float(r["entry_price"]),
            "stake_usd": _safe_float(r["stake_usd"]),
            "outcome": r["outcome"],
            "pnl_usd": _safe_float(r["pnl_usd"]),
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
            "status": r["status"],
        } for r in trows]

        # ── System state ──────────────────────────────────────────────────
        sys_q = text("SELECT state, paper_enabled, live_enabled FROM system_state WHERE id = 1")
        sys_result = await session.execute(sys_q)
        sys_row = sys_result.mappings().first()
        system_state = {}
        if sys_row:
            state_json = sys_row["state"] or {}
            system_state = {
                "bankroll": _safe_float(state_json.get("bankroll")),
                "paper_mode": bool(sys_row["paper_enabled"]),
                "live_enabled": bool(sys_row["live_enabled"]),
                "engine_status": state_json.get("status", "unknown"),
            }

        # ── v10 WR stats (DUNE-gated trades) ──────────────────────────
        v10_stats = {"wins": 0, "losses": 0, "wr_pct": 0.0, "total_trades": 0}
        try:
            v10q = text("""
                SELECT
                    COUNT(*) FILTER (WHERE t.outcome LIKE '%WIN%') AS wins,
                    COUNT(*) FILTER (WHERE t.outcome LIKE '%LOSS%') AS losses,
                    COUNT(*) AS total
                FROM trades t
                WHERE t.strategy = 'five_min_vpin'
                  AND t.market_slug ILIKE :market_slug_like
                  AND t.outcome IS NOT NULL
                  AND (t.engine_version LIKE 'v10%'
                       OR t.metadata::text LIKE '%v10_DUNE%')
            """)
            v10r = await session.execute(v10q, {"market_slug_like": market_slug_prefix + "%"})
            v10row = v10r.mappings().first()
            if v10row:
                w = int(v10row["wins"] or 0)
                l = int(v10row["losses"] or 0)
                v10_stats = {
                    "wins": w,
                    "losses": l,
                    "total_trades": int(v10row["total"] or 0),
                    "wr_pct": round(w / max(w + l, 1) * 100, 1),
                }
        except Exception:
            pass

        # ── Fallback: combined v9+v10 stats if no v10 trades yet ──────
        v9_stats = {"wins": 0, "losses": 0, "wr_pct": 0.0, "total_trades": 0}
        try:
            v9q = text("""
                SELECT
                    COUNT(*) FILTER (WHERE t.outcome LIKE '%WIN%') AS wins,
                    COUNT(*) FILTER (WHERE t.outcome LIKE '%LOSS%') AS losses,
                    COUNT(*) AS total
                FROM trades t
                WHERE t.strategy = 'five_min_vpin'
                  AND t.market_slug ILIKE :market_slug_like
                  AND t.outcome IS NOT NULL
                  AND (t.engine_version LIKE 'v9%' OR t.engine_version LIKE 'v10%')
            """)
            v9r = await session.execute(v9q, {"market_slug_like": market_slug_prefix + "%"})
            v9row = v9r.mappings().first()
            if v9row:
                w = int(v9row["wins"] or 0)
                l = int(v9row["losses"] or 0)
                v9_stats = {
                    "wins": w,
                    "losses": l,
                    "total_trades": int(v9row["total"] or 0),
                    "wr_pct": round(w / max(w + l, 1) * 100, 1),
                }
        except Exception:
            pass

        # ── v10 signal_evaluations gate data for recent windows ──────
        v9_gate_data = {}
        try:
            gq = text("""
                SELECT
                    window_ts, eval_offset,
                    gate_vpin_passed, gate_delta_passed, gate_cg_passed,
                    gate_passed, gate_failed, decision,
                    delta_chainlink, delta_tiingo, delta_source,
                    vpin, regime, v2_probability_up
                FROM signal_evaluations
                WHERE asset = :asset
                  AND timeframe = :timeframe
                  AND window_ts >= (
                    SELECT COALESCE(MAX(window_ts) - 1800, 0)
                    FROM signal_evaluations
                    WHERE asset = :asset AND timeframe = :timeframe
                  )
                ORDER BY window_ts DESC, eval_offset DESC
                LIMIT 500
            """)
            gresult = await session.execute(
                gq, {"asset": asset_upper, "timeframe": tf_norm}
            )
            grows = gresult.mappings().all()
            for gr in grows:
                wts = int(gr["window_ts"]) if gr["window_ts"] else 0
                offset = int(gr["eval_offset"]) if gr["eval_offset"] else 0
                if wts not in v9_gate_data:
                    v9_gate_data[wts] = {}
                p_up = _safe_float(gr.get("v2_probability_up"))
                # Derive P(agreed direction) for DUNE gate evaluation
                dune_p_dir = None
                if p_up is not None:
                    agree = _derive_source_agreement(gr)
                    dc = _safe_float(gr.get("delta_chainlink"))
                    if agree and dc is not None:
                        agreed_dir = "UP" if dc > 0 else "DOWN"
                        dune_p_dir = p_up if agreed_dir == "UP" else (1.0 - p_up)
                    else:
                        dune_p_dir = max(p_up, 1.0 - p_up)
                v9_gate_data[wts][offset] = {
                    "gate_agreement": "pass" if _derive_source_agreement(gr) else ("fail" if _derive_source_agreement(gr) is False else "unknown"),
                    "gate_dune": "pass" if (dune_p_dir is not None and dune_p_dir >= 0.65) else ("fail" if dune_p_dir is not None else "unknown"),
                    "gate_cg_veto": "pass" if gr.get("gate_cg_passed") else "fail",
                    "gate_cap": "pass" if gr.get("gate_passed") else "fail",
                    "gate_passed": bool(gr.get("gate_passed")),
                    "gate_failed": gr.get("gate_failed"),
                    "decision": gr.get("decision"),
                    "dune_p": dune_p_dir,
                    "vpin": _safe_float(gr.get("vpin")),
                    "regime": gr.get("regime"),
                }
        except Exception:
            pass

        # ── UI-01: Gate heartbeat — last 50 signal_evaluations rows ─────
        # Surfaces the V10.6 8-gate pipeline status for the operator so
        # blocking gates are visible in real time. Reads the same
        # signal_evaluations table as v9_gate_data above, but returns a
        # flat newest-first array (not a per-window nested dict) plus
        # per-gate pass/fail derived from `gate_failed` + `gate_passed`.
        #
        # The 8 V10.6 gates (in pipeline order, see
        # engine/strategies/five_min_vpin.py ~line 695):
        #   G0 eval_offset_bounds  (DS-01, v10.6 EvalOffsetBoundsGate)
        #   G1 source_agreement    (SourceAgreementGate)
        #   G2 delta_magnitude     (DeltaMagnitudeGate)
        #   G3 taker_flow          (TakerFlowGate)
        #   G4 cg_confirmation     (CGConfirmationGate)
        #   G5 dune_confidence     (DuneConfidenceGate)
        #   G6 spread_gate         (SpreadGate)
        #   G7 dynamic_cap         (DynamicCapGate)
        gate_heartbeat: list = []
        try:
            hbq = text("""
                SELECT
                    evaluated_at, window_ts, eval_offset, decision,
                    gate_failed, gate_passed,
                    gate_vpin_passed, gate_delta_passed, gate_cg_passed,
                    gate_twap_passed, gate_timesfm_passed,
                    v2_probability_up, delta_chainlink, delta_tiingo
                FROM signal_evaluations
                WHERE asset = :asset
                  AND timeframe = :timeframe
                ORDER BY evaluated_at DESC
                LIMIT 50
            """)
            hbresult = await session.execute(
                hbq, {"asset": asset_upper, "timeframe": tf_norm}
            )
            hbrows = hbresult.mappings().all()

            # Ordered list of 8 V10.6 gate pipeline keys (G0 .. G7)
            v106_pipeline_order = [
                "eval_offset_bounds",
                "source_agreement",
                "delta_magnitude",
                "taker_flow",
                "cg_confirmation",
                "dune_confidence",
                "spread_gate",
                "dynamic_cap",
            ]
            # Aliases the engine may write into gate_failed — some legacy
            # names (e.g. "cg") predate the V10.6 rename.
            gate_failed_aliases = {
                "eval_offset_bounds": "eval_offset_bounds",
                "source_agreement": "source_agreement",
                "source_disagree": "source_agreement",
                "delta_magnitude": "delta_magnitude",
                "delta": "delta_magnitude",
                "taker_flow": "taker_flow",
                "cg_confirmation": "cg_confirmation",
                "cg_confirm": "cg_confirmation",
                "cg": "cg_confirmation",
                "cg_veto": "cg_confirmation",
                "dune_confidence": "dune_confidence",
                "dune": "dune_confidence",
                "timesfm": "dune_confidence",
                "spread_gate": "spread_gate",
                "spread": "spread_gate",
                "dynamic_cap": "dynamic_cap",
                "cap": "dynamic_cap",
            }

            for hbr in hbrows:
                gate_failed_raw = hbr.get("gate_failed")
                gate_failed_canonical = None
                if gate_failed_raw:
                    gate_failed_canonical = gate_failed_aliases.get(
                        str(gate_failed_raw).strip().lower().replace(" ", "_")
                    ) or str(gate_failed_raw)

                overall_passed = bool(hbr.get("gate_passed"))
                gate_results: dict = {}
                if overall_passed:
                    # All gates in the pipeline passed
                    for gname in v106_pipeline_order:
                        gate_results[gname] = True
                elif gate_failed_canonical in v106_pipeline_order:
                    # Pipeline stops at the first failing gate; every
                    # gate before it passed, the failing gate is False,
                    # and every gate after it never ran (None).
                    idx = v106_pipeline_order.index(gate_failed_canonical)
                    for i, gname in enumerate(v106_pipeline_order):
                        if i < idx:
                            gate_results[gname] = True
                        elif i == idx:
                            gate_results[gname] = False
                        else:
                            gate_results[gname] = None
                else:
                    # Unknown failure — fall back to legacy column
                    # mappings where possible. This mainly triggers for
                    # pre-V10.6 rows.
                    legacy = {
                        "source_agreement": _derive_source_agreement(hbr),
                        "delta_magnitude": hbr.get("gate_delta_passed"),
                        "taker_flow": hbr.get("gate_cg_passed"),
                        "cg_confirmation": hbr.get("gate_cg_passed"),
                        "dune_confidence": hbr.get("gate_timesfm_passed"),
                        "spread_gate": None,
                        "dynamic_cap": None,
                        "eval_offset_bounds": None,
                    }
                    for gname in v106_pipeline_order:
                        v = legacy.get(gname)
                        gate_results[gname] = bool(v) if v is not None else None

                gate_heartbeat.append({
                    "evaluated_at": hbr["evaluated_at"].isoformat() if hbr.get("evaluated_at") else None,
                    "window_ts": int(hbr["window_ts"]) if hbr.get("window_ts") else None,
                    "eval_offset": int(hbr["eval_offset"]) if hbr.get("eval_offset") is not None else None,
                    "decision": hbr.get("decision") or ("TRADE" if overall_passed else "SKIP"),
                    "gate_failed": gate_failed_canonical,
                    "gate_failed_raw": gate_failed_raw,
                    "v2_probability_up": _safe_float(hbr.get("v2_probability_up")),
                    "gate_results": gate_results,
                })
        except Exception as exc:
            # Swallow — gate_heartbeat is purely cosmetic; the rest of
            # the payload stays backward compatible.
            gate_heartbeat = []
            try:
                log_ctx = getattr(text, "__name__", "")  # no-op
            except Exception:
                pass

        return {
            "asset": asset_norm,
            "timeframe": tf_norm,
            "windows": windows,
            "shadow_stats": shadow_stats,
            "recent_trades": recent_trades,
            "system": system_state,
            "v9_stats": v9_stats,
            "v10_stats": v10_stats,
            "v9_gate_data": v9_gate_data,
            "gate_heartbeat": gate_heartbeat,
        }
    except HTTPException:
        # Don't swallow the 400 we raised above for bad asset/timeframe.
        raise
    except Exception as exc:
        return {
            "asset": asset_norm,
            "timeframe": tf_norm,
            "windows": [],
            "shadow_stats": {},
            "recent_trades": [],
            "system": {},
            "v9_stats": {"wins": 0, "losses": 0, "wr_pct": 0.0, "total_trades": 0},
            "v10_stats": {"wins": 0, "losses": 0, "wr_pct": 0.0, "total_trades": 0},
            "v9_gate_data": {},
            "gate_heartbeat": [],
            "error": str(exc),
        }


@router.get("/wallet/live")
async def get_wallet_live(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Live Polymarket wallet view — reads from DB tables populated by Montreal engine."""
    try:
        # Wallet balance from wallet_snapshots (written by CLOB reconciler every 2s)
        wq = text("SELECT balance_usdc, recorded_at FROM wallet_snapshots ORDER BY recorded_at DESC LIMIT 1")
        wr = await session.execute(wq)
        wrow = wr.mappings().first()
        wallet = {
            "balance": _safe_float(wrow["balance_usdc"]) if wrow else None,
            "updated_at": wrow["recorded_at"].isoformat() if wrow and wrow.get("recorded_at") else None,
        }

        # Open positions (trades with status OPEN, not expired/resolved)
        oq = text("""
            SELECT direction, entry_price, stake_usd, status, created_at,
                   metadata->>'entry_reason' as entry_reason,
                   metadata->>'v81_entry_cap' as cap,
                   metadata->>'token_id' as token_id
            FROM trades
            WHERE status IN ('OPEN', 'FILLED') AND is_live = true
            ORDER BY created_at DESC LIMIT 20
        """)
        oresult = await session.execute(oq)
        open_positions = [{
            "direction": r["direction"],
            "entry_price": _safe_float(r["entry_price"]),
            "stake": _safe_float(r["stake_usd"]),
            "entry_reason": r["entry_reason"],
            "cap": _safe_float(r["cap"]),
            "placed_at": r["created_at"].isoformat() if r.get("created_at") else None,
            "status": r["status"],
        } for r in oresult.mappings().all()]

        # Recent resolved trades (from trade_bible for accurate attribution)
        rq = text("""
            SELECT trade_outcome, pnl_usd, entry_reason, config_version, eval_tier,
                   resolved_at, direction, entry_price
            FROM trade_bible
            WHERE trade_outcome IS NOT NULL AND is_live = true
            ORDER BY resolved_at DESC NULLS LAST
            LIMIT 10
        """)
        rresult = await session.execute(rq)
        resolved = [{
            "outcome": r["trade_outcome"],
            "pnl": _safe_float(r["pnl_usd"]),
            "entry_reason": r["entry_reason"],
            "config": r["config_version"],
            "tier": r["eval_tier"],
            "direction": r["direction"],
            "entry_price": _safe_float(r["entry_price"]),
            "resolved_at": r["resolved_at"].isoformat() if r.get("resolved_at") else None,
        } for r in rresult.mappings().all()]

        # Session stats
        sq = text("""
            SELECT
                count(*) FILTER (WHERE trade_outcome LIKE '%WIN%') as wins,
                count(*) FILTER (WHERE trade_outcome LIKE '%LOSS%') as losses,
                COALESCE(SUM(pnl_usd), 0) as total_pnl
            FROM trade_bible WHERE is_live = true
        """)
        sresult = await session.execute(sq)
        srow = sresult.mappings().first()
        session_stats = {
            "wins": int(srow["wins"] or 0) if srow else 0,
            "losses": int(srow["losses"] or 0) if srow else 0,
            "total_pnl": round(float(srow["total_pnl"] or 0), 2) if srow else 0,
        }

        total_exposure = sum(p["stake"] or 0 for p in open_positions)

        return {
            "wallet": wallet,
            "open_positions": open_positions,
            "resting_orders": len([p for p in open_positions if p["status"] == "OPEN"]),
            "total_exposure": round(total_exposure, 2),
            "recent_resolved": resolved,
            "session": session_stats,
        }
    except Exception as exc:
        return {"wallet": {}, "open_positions": [], "recent_resolved": [], "error": str(exc)}
