"""
Dashboard API Routes

GET /api/dashboard              — full dashboard data (legacy)
GET /api/dashboard/summary      — lightweight stat summary (legacy)
GET /api/dashboard/vpin-history — VPIN time series (last 300)
GET /api/dashboard/cascade-state — current cascade FSM state
GET /api/dashboard/arb-spreads — arb combined price history
GET /api/dashboard/equity       — cumulative equity curve
GET /api/dashboard/daily-pnl    — last 60 days of P&L bars
GET /api/dashboard/stats        — header bar stats
GET /api/dashboard/trades       — recent trades for heatmap/bucket analysis
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session
from db.models import DailyPnL, Signal, SystemState, Trade, window_snapshots
from services.dashboard_service import DashboardService

router = APIRouter()


# ─── Legacy endpoints ─────────────────────────────────────────────────────────

@router.get("/dashboard")
async def get_dashboard(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    svc = DashboardService(session)
    return await svc.get_dashboard_data()


@router.get("/dashboard/summary")
async def get_dashboard_summary(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    svc = DashboardService(session)
    return await svc.get_summary()


# ─── New chart endpoints ───────────────────────────────────────────────────────

@router.get("/dashboard/vpin-history")
async def get_vpin_history(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> list:
    """
    Last 300 VPIN signal snapshots.
    Returns [{t, vpin, btcPrice}]
    """
    try:
        result = await session.execute(
            select(Signal)
            .where(Signal.signal_type == "vpin")
            .order_by(desc(Signal.created_at))
            .limit(300)
        )
        signals = result.scalars().all()
        if not signals:
            return []

        # Reverse to chronological order
        signals = list(reversed(signals))
        return [
            {
                "t": idx,
                "vpin": float(s.payload.get("vpin", 0)),
                "btcPrice": float(s.payload.get("btc_price", 0)),
            }
            for idx, s in enumerate(signals)
        ]
    except Exception:
        return []


@router.get("/dashboard/cascade-state")
async def get_cascade_state(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Current cascade FSM state.
    Returns {state, direction, oi_delta}
    """
    try:
        # Try latest cascade signal
        result = await session.execute(
            select(Signal)
            .where(Signal.signal_type == "cascade")
            .order_by(desc(Signal.created_at))
            .limit(1)
        )
        sig = result.scalar_one_or_none()

        if sig:
            payload = sig.payload
            return {
                "state": payload.get("state", "IDLE"),
                "direction": payload.get("direction", "—"),
                "oi_delta": payload.get("oi_delta", 0.0),
            }

        # Fall back to system_state
        state_result = await session.execute(
            select(SystemState).where(SystemState.id == 1)
        )
        system = state_result.scalar_one_or_none()
        if system and system.state:
            s = system.state
            return {
                "state": s.get("last_cascade_state", "IDLE"),
                "direction": s.get("cascade_direction", "—"),
                "oi_delta": float(s.get("cascade_oi_delta", 0.0)),
            }
    except Exception:
        pass

    return {"state": "IDLE", "direction": "—", "oi_delta": 0.0}


@router.get("/dashboard/arb-spreads")
async def get_arb_spreads(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> list:
    """
    Last 200 arb opportunity signals.
    Returns array of combined YES+NO prices.
    """
    try:
        result = await session.execute(
            select(Signal)
            .where(Signal.signal_type == "arb_opportunity")
            .order_by(desc(Signal.created_at))
            .limit(200)
        )
        signals = result.scalars().all()
        if not signals:
            return []

        signals = list(reversed(signals))
        out = []
        for s in signals:
            p = s.payload
            combined = p.get("combined_price", p.get("yes_price", 0) + p.get("no_price", 0))
            out.append(float(combined))
        return out
    except Exception:
        return []


@router.get("/dashboard/equity")
async def get_equity(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> list:
    """
    Cumulative equity curve from daily_pnl table.
    Returns [{day, balance}]
    """
    try:
        result = await session.execute(
            select(DailyPnL)
            .order_by(DailyPnL.date)
            .limit(90)
        )
        rows = result.scalars().all()
        if not rows:
            return []

        # Use bankroll_end if available, else cumulate from total_pnl
        out = []
        cumulative = 1000.0  # default starting balance
        for row in rows:
            if row.bankroll_end is not None:
                balance = float(row.bankroll_end)
            else:
                cumulative += float(row.total_pnl or 0)
                balance = cumulative
            out.append({
                "day": row.date.date().isoformat() if row.date else "",
                "balance": balance,
            })
        return out
    except Exception:
        return []


@router.get("/dashboard/daily-pnl")
async def get_daily_pnl(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> list:
    """
    Last 60 days of daily P&L values.
    Returns array of floats.
    """
    try:
        result = await session.execute(
            select(DailyPnL)
            .order_by(desc(DailyPnL.date))
            .limit(60)
        )
        rows = result.scalars().all()
        if not rows:
            return []

        # Return chronological
        rows = list(reversed(rows))
        return [float(r.total_pnl or 0) for r in rows]
    except Exception:
        return []


@router.get("/dashboard/stats")
async def get_stats(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Header bar stats: balance, today_pnl, win_rate, engine_status, total_trades.
    """
    try:
        # System state
        state_result = await session.execute(
            select(SystemState).where(SystemState.id == 1)
        )
        state = state_result.scalar_one_or_none()
        engine_state: dict = state.state if state and state.state else {}

        # Total trades + wins
        stats_result = await session.execute(
            select(
                func.count(Trade.id).label("total"),
                func.sum(Trade.pnl_usd).label("total_pnl"),
            ).where(Trade.pnl_usd.isnot(None))
        )
        row = stats_result.one()
        total = int(row.total or 0)

        wins_result = await session.execute(
            select(func.count()).where(Trade.outcome == "WIN")
        )
        wins = int(wins_result.scalar_one() or 0)

        # Today P&L
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        today_result = await session.execute(
            select(func.coalesce(func.sum(Trade.pnl_usd), 0)).where(
                Trade.resolved_at >= today_start
            )
        )
        today_pnl = float(today_result.scalar_one() or 0)

        # Engine status
        engine_running = state is not None and state.updated_at is not None
        if engine_running:
            age_sec = (datetime.now(timezone.utc) - state.updated_at).total_seconds()
            engine_status = "LIVE" if age_sec < 120 else "STALE"
        else:
            engine_status = "OFFLINE"

        # Extract config snapshot from heartbeat (includes wallet balance)
        config_data = engine_state.get("config", {})

        return {
            "balance": engine_state.get("current_bankroll"),
            "today_pnl": today_pnl,
            "win_rate": wins / total if total > 0 else 0.0,
            "engine_status": engine_status,
            "total_trades": total,
            "wallet_balance_usdc": config_data.get("wallet_balance_usdc"),
            "paper_mode": config_data.get("paper_mode", True),
            "daily_pnl_engine": config_data.get("daily_pnl", 0),
            "drawdown_pct": engine_state.get("current_drawdown_pct"),
        }
    except Exception:
        return {
            "balance": None,
            "today_pnl": 0.0,
            "win_rate": 0.0,
            "engine_status": "OFFLINE",
            "total_trades": 0,
            "wallet_balance_usdc": None,
            "paper_mode": True,
        }


@router.get("/dashboard/trades")
async def get_trades_for_analysis(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> list:
    """
    Recent trades with metadata for heatmap and VPIN bucket analysis.
    Returns array of trade objects with vpin, hour, dayOfWeek.
    """
    try:
        result = await session.execute(
            select(Trade)
            .order_by(desc(Trade.created_at))
            .limit(500)
        )
        trades = result.scalars().all()
        if not trades:
            return []

        out = []
        for t in trades:
            meta = t.metadata_json or {}
            created = t.created_at
            out.append({
                "id": t.id,
                "outcome": t.outcome,
                "pnl_usd": float(t.pnl_usd) if t.pnl_usd else None,
                "vpin": float(meta.get("vpin_at_entry", 0)),
                "hour": created.hour if created else 0,
                "dayOfWeek": created.weekday() if created else 0,
                "strategy": t.strategy,
                "stake_usd": float(t.stake_usd) if t.stake_usd else None,
            })
        return out
    except Exception:
        return []


# ─── New monitoring endpoints ──────────────────────────────────────────────────

@router.get("/dashboard/entry-timing")
async def get_entry_timing(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> list:
    """
    Entry timing analysis: when in the window did we fire?
    Returns [{seconds_to_close, confidence, tier, outcome, pnl_usd, delta_pct, token_price}]
    Last 200 trades.
    """
    try:
        result = await session.execute(
            select(Trade)
            .order_by(desc(Trade.created_at))
            .limit(200)
        )
        trades = result.scalars().all()
        if not trades:
            return []

        out = []
        for t in trades:
            meta = t.metadata_json or {}
            out.append({
                "seconds_to_close": meta.get("seconds_to_close"),
                "confidence": meta.get("confidence"),
                "tier": meta.get("tier"),
                "outcome": t.outcome,
                "pnl_usd": float(t.pnl_usd) if t.pnl_usd is not None else None,
                "delta_pct": meta.get("delta_pct"),
                "token_price": float(t.entry_price) if t.entry_price is not None else None,
                "stake_usd": float(t.stake_usd) if t.stake_usd is not None else None,
            })
        return out
    except Exception:
        return []


@router.get("/dashboard/signal-breakdown")
async def get_signal_breakdown(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> list:
    """
    Signal component weights for recent trades.
    Returns [{delta_weight, vpin_weight, liq_surge_weight, ls_imbalance_weight,
              funding_weight, oi_delta_weight, score, confidence, outcome}]
    Last 200 trades.
    """
    try:
        result = await session.execute(
            select(Trade)
            .order_by(desc(Trade.created_at))
            .limit(200)
        )
        trades = result.scalars().all()
        if not trades:
            return []

        out = []
        for t in trades:
            meta = t.metadata_json or {}
            out.append({
                "delta_weight": meta.get("delta_weight"),
                "vpin_weight": meta.get("vpin_weight"),
                "liq_surge_weight": meta.get("liq_surge_weight"),
                "ls_imbalance_weight": meta.get("ls_imbalance_weight"),
                "funding_weight": meta.get("funding_weight"),
                "oi_delta_weight": meta.get("oi_delta_weight"),
                "score": meta.get("score"),
                "confidence": meta.get("confidence"),
                "outcome": t.outcome,
            })
        return out
    except Exception:
        return []


@router.get("/dashboard/confidence-histogram")
async def get_confidence_histogram(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> list:
    """
    Confidence vs win rate histogram.
    Buckets confidence into 0.1 ranges.
    Returns [{range, total, wins, losses, win_rate, avg_pnl}]
    """
    try:
        result = await session.execute(
            select(Trade)
            .order_by(desc(Trade.created_at))
            .limit(200)
        )
        trades = result.scalars().all()
        if not trades:
            return []

        # Build buckets: 0.0–0.1, 0.1–0.2, ..., 0.9–1.0
        buckets: dict[str, Any] = {}
        for i in range(10):
            lo = i / 10
            hi = (i + 1) / 10
            key = f"{int(lo * 100)}-{int(hi * 100)}%"
            buckets[key] = {"lo": lo, "hi": hi, "total": 0, "wins": 0, "pnl_sum": 0.0}

        for t in trades:
            meta = t.metadata_json or {}
            conf = meta.get("confidence")
            if conf is None:
                continue
            try:
                conf = float(conf)
            except (TypeError, ValueError):
                continue
            bucket_idx = min(int(conf * 10), 9)
            lo = bucket_idx / 10
            hi = (bucket_idx + 1) / 10
            key = f"{int(lo * 100)}-{int(hi * 100)}%"
            if key in buckets:
                buckets[key]["total"] += 1
                if t.outcome == "WIN":
                    buckets[key]["wins"] += 1
                pnl = float(t.pnl_usd) if t.pnl_usd is not None else 0.0
                buckets[key]["pnl_sum"] += pnl

        out = []
        for key, b in buckets.items():
            total = b["total"]
            wins = b["wins"]
            losses = total - wins
            out.append({
                "range": key,
                "total": total,
                "wins": wins,
                "losses": losses,
                "win_rate": wins / total if total > 0 else 0.0,
                "avg_pnl": b["pnl_sum"] / total if total > 0 else 0.0,
            })
        return out
    except Exception:
        return []


@router.get("/dashboard/tier-stats")
async def get_tier_stats(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> list:
    """
    Win rate and P&L by entry tier.
    Groups by tier (DECISIVE/HIGH/MODERATE/DEADLINE/SPIKE).
    Returns [{tier, count, wins, losses, win_rate, total_pnl, avg_entry_seconds}]
    """
    try:
        result = await session.execute(
            select(Trade)
            .order_by(desc(Trade.created_at))
            .limit(500)
        )
        trades = result.scalars().all()
        if not trades:
            return []

        tier_order = ["DECISIVE", "HIGH", "MODERATE", "DEADLINE", "SPIKE"]
        tiers: dict[str, Any] = {
            t: {"count": 0, "wins": 0, "pnl_sum": 0.0, "seconds_sum": 0.0, "seconds_count": 0}
            for t in tier_order
        }

        for trade in trades:
            meta = trade.metadata_json or {}
            tier = meta.get("tier")
            if not tier or tier not in tiers:
                continue
            tiers[tier]["count"] += 1
            if trade.outcome == "WIN":
                tiers[tier]["wins"] += 1
            pnl = float(trade.pnl_usd) if trade.pnl_usd is not None else 0.0
            tiers[tier]["pnl_sum"] += pnl
            stc = meta.get("seconds_to_close")
            if stc is not None:
                try:
                    tiers[tier]["seconds_sum"] += float(stc)
                    tiers[tier]["seconds_count"] += 1
                except (TypeError, ValueError):
                    pass

        out = []
        for tier in tier_order:
            b = tiers[tier]
            count = b["count"]
            wins = b["wins"]
            losses = count - wins
            out.append({
                "tier": tier,
                "count": count,
                "wins": wins,
                "losses": losses,
                "win_rate": wins / count if count > 0 else 0.0,
                "total_pnl": b["pnl_sum"],
                "avg_entry_seconds": b["seconds_sum"] / b["seconds_count"] if b["seconds_count"] > 0 else None,
            })
        return out
    except Exception:
        return []


# ─── OAK (v2.2) monitoring endpoints ───────────────────────────────────────────

@router.get("/dashboard/oak-latest")
async def get_oak_latest(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Latest OAK (v2.2) prediction from window_snapshots.
    Returns {probability_up, probability_down, direction, model_version, timestamp}
    """
    try:
        from sqlalchemy import desc
        
        # Get latest window_snapshot with v2.2 data
        result = await session.execute(
            select(window_snapshots.c.v2_probability_up,
                   window_snapshots.c.v2_direction,
                   window_snapshots.c.v2_model_version,
                   window_snapshots.c.window_ts)
            .where(window_snapshots.c.v2_probability_up.isnot(None))
            .order_by(desc(window_snapshots.c.window_ts))
            .limit(1)
        )
        row = result.first()
        
        if not row:
            return {"status": "no_data"}
        
        p_up = float(row.v2_probability_up) if row.v2_probability_up is not None else None
        p_down = 1.0 - p_up if p_up is not None else None
        
        return {
            "probability_up": p_up,
            "probability_down": p_down,
            "direction": row.v2_direction,
            "model_version": row.v2_model_version,
            "timestamp": float(row.window_ts) if row.window_ts else None,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.get("/dashboard/oak-history")
async def get_oak_history(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
    limit: int = 50,
) -> list:
    """
    OAK (v2.2) prediction history from window_snapshots.
    Returns [{window_ts, probability_up, direction, v2_agrees, outcome}]
    """
    try:
        from sqlalchemy import desc
        
        result = await session.execute(
            select(window_snapshots.c.window_ts,
                   window_snapshots.c.v2_probability_up,
                   window_snapshots.c.v2_direction,
                   window_snapshots.c.v2_agrees,
                   window_snapshots.c.outcome)
            .where(window_snapshots.c.v2_probability_up.isnot(None))
            .order_by(desc(window_snapshots.c.window_ts))
            .limit(limit)
        )
        rows = result.fetchall()
        
        return [
            {
                "window_ts": float(r.window_ts) if r.window_ts else None,
                "probability_up": float(r.v2_probability_up) if r.v2_probability_up is not None else None,
                "direction": r.v2_direction,
                "v2_agrees": r.v2_agrees,
                "outcome": r.outcome,
                "is_correct": (r.v2_direction == r.outcome) if r.v2_direction and r.outcome else None,
            }
            for r in rows
        ]
    except Exception as e:
        return []
