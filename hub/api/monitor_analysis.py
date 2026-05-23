"""Hub API: /api/monitor/analysis — single-call operator analysis page.

Read-only. One endpoint, six sections, ~10 s TTL cache. The frontend
makes ONE request and renders the whole "are we winning, are we wiped
out, is the reconciler running" answer in one screen.

Sections returned in the payload envelope:

  1. ``live_8h`` — last-8-hour LIVE strategy roll-up from ``trades``
     where ``is_live = TRUE``. Per-strategy fires / W / L / pending /
     wr_pct / net_pnl_usd. Sourced from the trades table (Gamma-
     verified by the reconciler from PR #575 — outcome is now
     trustworthy).
  2. ``ghost_24h`` — last-24-hour would-fire / would-WR for the
     featured GHOST strategies (scoped to a known ID list so the
     composite index on (strategy_id, action, evaluated_at) keeps
     the scan bounded — see "performance notes" below). Uses
     DISTINCT ON (strategy_id, asset, window_ts, timeframe) over
     action='TRADE' rows so re-evals collapse to one would-fire per
     window. Joins ``window_snapshots.oracle_outcome`` for the win
     check (the same oracle Polymarket resolves against).
  3. ``featured`` — the 3 new strategy families Billy is tracking
     (ETH v9.5, BTC v9.3 raw+tight, XRP v9.5 raw+tight) with their
     would-WR side-by-side with the training projection. Each row
     flags ``train_serve_skew_pp > 5`` so the FE can highlight the
     gap between training-claimed accuracy and live shadow accuracy.
  4. ``wallet`` — latest ``wallet_snapshots`` row with USDC + pUSD
     balances, the sum as ``effective_balance``, and a ``stale`` flag
     for when ``recorded_at`` is more than 5 min old (reconciler poll
     cadence is ~60s; >5 min means the Montreal sidecar is down).
     Both balances are written server-side by the engine reconciler;
     the hub never calls Polymarket / Polygon RPC directly (memory:
     feedback_polymarket_montreal_only).
  5. ``bankroll_24h`` — hourly min/max/avg of ``wallet_snapshots``
     for the last 24 hours. Frontend renders this as a sparkline
     so Billy can eyeball "are we trending toward zero".
  6. ``reconciler_health`` — counts + timestamps over ``trades`` so
     the operator can answer "did the redemption reconciler run in
     the last 5 minutes". Cheap query: just a few COUNT FILTER and
     a MAX(redeemed_at). No DB write — heartbeat is implicit from
     "did the most recent redeemed_at value advance".

Cache: module-level dict, 10 s TTL — matches monitor_scorecard.py.
The dashboard auto-refreshes; the cache absorbs that without
hammering RDS.

Failure mode: every section is fetched via ``asyncio.gather(...,
return_exceptions=True)``. Any single section that fails logs and
returns an empty default (e.g. ``[]`` or ``{}``). The endpoint
never 500s — a transient DB blip won't blank the dashboard.

Hard constraints:
  * READ-ONLY. No INSERT / UPDATE / DELETE anywhere.
  * NO Polymarket / Polygon RPC from the hub. pUSD comes from the
    Montreal reconciler via ``wallet_snapshots.balance_pusd``.
  * Every query is time-bounded. The blueprint specifically calls
    out avoiding the slow-scan pattern that broke
    /api/monitor/strategies/scorecard at 504.

Performance notes:
  ``strategy_decisions`` is under heavy concurrent INSERT load from
  the engine (~1 Hz across many strategies). A naive scan of
  WHERE action='TRADE' AND evaluated_at >= NOW() - INTERVAL '24h'
  (without strategy_id filter) takes 5+ minutes under load — this is
  exactly the slow-scan pattern that broke /monitor/strategies. We
  avoid it by scoping Q2 to an explicit strategy ID list (the GHOSTS
  Billy is tracking), which leverages the composite index
  ``idx_sd_strategy_action_evaluated (strategy_id, action,
  evaluated_at DESC)`` for fast index-only seeks.

  The trade-off: only the featured GHOST strategies show up in
  ``ghost_24h``. For a complete cross-strategy roll-up, see
  /api/strategy-comparison or /api/monitor/strategies/scorecard
  (which read from the pre-aggregated ``strategy_comparison`` table).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.middleware import get_current_user
from db.database import get_session

log = structlog.get_logger(__name__)
router = APIRouter()


# ─── Cache ───────────────────────────────────────────────────────────────────
# Module-level TTL dict. 10 s matches monitor_scorecard.py — single value
# absorbs auto-refresh bursts (Billy clicking around / multiple tabs) without
# changing the underlying RDS load shape.

_CACHE_TTL_S = 10.0
_cache: dict[str, Any] = {"data": None, "ts": 0.0}


def _cache_get() -> Optional[dict[str, Any]]:
    if _cache["data"] is None:
        return None
    if (time.monotonic() - _cache["ts"]) > _CACHE_TTL_S:
        return None
    return _cache["data"]


def _cache_put(payload: dict[str, Any]) -> None:
    _cache["data"] = payload
    _cache["ts"] = time.monotonic()


# ─── Featured-strategy registry ──────────────────────────────────────────────
# The 3 new GHOST families Billy is tracking. Training projection numbers come
# from the memo (notes #570, #573, #574). Each entry feeds the "featured"
# section so the FE can render the train-vs-serve gap as a single card.
#
# `train_projection_wr_pct` is the OOF backtest win rate the model author
# claimed in their PR description. We compare against the live ghost-fire
# WR and flag any gap > 5 percentage points as train-serve skew.

_FEATURED_STRATEGIES = [
    {"strategy_id": "v9_5_eth_raw_lgb", "asset": "ETH",
     "train_projection_wr_pct": 90.8, "family": "ETH v9.5"},
    {"strategy_id": "v9_3_btc_raw_lgb", "asset": "BTC",
     "train_projection_wr_pct": 90.2, "family": "BTC v9.3 (raw)"},
    {"strategy_id": "v9_3_btc_tight",   "asset": "BTC",
     "train_projection_wr_pct": 92.5, "family": "BTC v9.3 (tight)"},
    {"strategy_id": "v9_5_xrp_raw_lgb", "asset": "XRP",
     "train_projection_wr_pct": 90.5, "family": "XRP v9.5 (raw)"},
    {"strategy_id": "v9_5_xrp_tight",   "asset": "XRP",
     "train_projection_wr_pct": 92.0, "family": "XRP v9.5 (tight)"},
]
_FEATURED_IDS = tuple(s["strategy_id"] for s in _FEATURED_STRATEGIES)


# ─── Queries ─────────────────────────────────────────────────────────────────

# Q1: LIVE 8h roll-up from trades. is_live=TRUE filters paper noise; the
# reconciler from PR #575 backfilled wins/losses so `outcome` is trustworthy.
# 8h window scoped via created_at to use idx_trades_created_at.
_Q_LIVE_8H = """
SELECT
    COALESCE(strategy, '(unknown)') AS strategy,
    COUNT(*)                                                   AS fires,
    COUNT(*) FILTER (WHERE outcome = 'WIN')                    AS wins,
    COUNT(*) FILTER (WHERE outcome = 'LOSS')                   AS losses,
    COUNT(*) FILTER (WHERE outcome IS NULL)                    AS pending,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE outcome = 'WIN')::numeric
        / NULLIF(COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')), 0),
        1
    )                                                          AS wr_pct,
    ROUND(COALESCE(SUM(pnl_usd), 0)::numeric, 2)               AS net_pnl_usd,
    ROUND(COALESCE(SUM(stake_usd), 0)::numeric, 2)             AS stake_usd
FROM trades
WHERE is_live = TRUE
  AND created_at >= NOW() - INTERVAL '8 hours'
GROUP BY strategy
ORDER BY net_pnl_usd DESC NULLS LAST
"""

# Q2: GHOST 24h — scoped to the explicit featured-strategy ID list so the
# composite index idx_sd_strategy_action_evaluated (strategy_id, action,
# evaluated_at DESC) drives the scan. Without the strategy_id filter the
# planner falls back to a wide index walk that takes 5+ minutes under
# concurrent INSERT load (see "Performance notes" in the module docstring).
#
# Bind parameter `:strategy_ids` is a tuple of strategy_id strings — the
# query function expands _FEATURED_IDS to a sqlalchemy bindparam expansion.
#
# DISTINCT ON dedupes re-eval rows for the same (strategy, asset, window,
# timeframe) so each window contributes at most one would-fire. LEFT JOIN
# to window_snapshots picks up oracle_outcome to compute would-WR.
_Q_GHOST_24H = """
WITH fires AS (
    SELECT DISTINCT ON (strategy_id, asset, window_ts, timeframe)
        strategy_id, asset, window_ts, timeframe,
        direction, fill_price, confidence_score, evaluated_at
    FROM strategy_decisions
    WHERE strategy_id IN :strategy_ids
      AND action = 'TRADE'
      AND evaluated_at >= NOW() - INTERVAL '24 hours'
    ORDER BY strategy_id, asset, window_ts, timeframe, evaluated_at DESC
)
SELECT
    f.strategy_id,
    MIN(f.asset)                                                AS asset,
    COUNT(*)                                                    AS would_fires,
    COUNT(*) FILTER (WHERE ws.oracle_outcome IS NOT NULL)       AS resolved,
    COUNT(*) FILTER (WHERE ws.oracle_outcome = f.direction)     AS wins,
    COUNT(*) FILTER (WHERE ws.oracle_outcome IS NOT NULL
                       AND ws.oracle_outcome <> f.direction)    AS losses,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE ws.oracle_outcome = f.direction)::numeric
        / NULLIF(COUNT(*) FILTER (WHERE ws.oracle_outcome IS NOT NULL), 0),
        1
    )                                                           AS would_wr_pct,
    ROUND(AVG(f.fill_price)::numeric, 4)                        AS avg_fill_price,
    MAX(f.evaluated_at)                                         AS last_fire_at
FROM fires f
LEFT JOIN window_snapshots ws
    ON ws.asset = f.asset
   AND ws.window_ts = f.window_ts
   AND ws.timeframe = f.timeframe
GROUP BY f.strategy_id
ORDER BY would_fires DESC
"""

# Q3: Wallet — latest USDC + pUSD balances. The Montreal reconciler writes
# both into wallet_snapshots every ~60s (PR 3 sidecar wiring landed
# 2026-05-23). On historical rows that pre-date the migration, balance_pusd
# is NULL — the shaping layer treats that as "stale / sidecar lag" so the
# UI can render a pill instead of pretending the balance is zero.
_Q_WALLET_LATEST = """
SELECT balance_usdc, balance_pusd, source, recorded_at
FROM wallet_snapshots
ORDER BY recorded_at DESC
LIMIT 1
"""

# Q4: Bankroll trajectory — hourly bucketed wallet_snapshots over 24h. The
# FE renders this as a sparkline. Bucketing keeps the payload small (~24
# points) regardless of the engine's per-minute snapshot cadence.
_Q_BANKROLL_24H = """
SELECT
    DATE_TRUNC('hour', recorded_at)                AS bucket_at,
    ROUND(MIN(balance_usdc)::numeric, 2)           AS min_usdc,
    ROUND(MAX(balance_usdc)::numeric, 2)           AS max_usdc,
    ROUND(AVG(balance_usdc)::numeric, 2)           AS avg_usdc
FROM wallet_snapshots
WHERE recorded_at >= NOW() - INTERVAL '24 hours'
GROUP BY bucket_at
ORDER BY bucket_at
"""

# Q5: Reconciler health. The blueprint suggested "count rows redeemed in
# last 5 min — if >0 we know it ran recently". That's the heartbeat
# signal. Today's payout total is the operator-visible "value of wins
# the reconciler stamped today".
_Q_RECONCILER_HEALTH = """
SELECT
    COUNT(*) FILTER (WHERE redeemed = TRUE AND redeemed_at::date = CURRENT_DATE) AS today_redeemed,
    COUNT(*) FILTER (WHERE redeemed = TRUE AND redeemed_at >= NOW() - INTERVAL '5 min') AS recent_redeemed_5m,
    ROUND(COALESCE(SUM(payout_usd) FILTER (WHERE redeemed = TRUE AND redeemed_at::date = CURRENT_DATE), 0)::numeric, 2) AS today_payout_usd,
    MAX(redeemed_at) FILTER (WHERE redeemed = TRUE) AS last_stamp_at
FROM trades
WHERE redeemed_at >= NOW() - INTERVAL '48 hours' OR redeemed_at IS NULL
"""


# ─── Fetchers (one per section, each isolated for gather() exception handling)

async def _fetch_live_8h(db: AsyncSession) -> list[dict[str, Any]]:
    res = await db.execute(text(_Q_LIVE_8H))
    return [dict(r) for r in res.mappings().all()]


async def _fetch_ghost_24h(db: AsyncSession) -> list[dict[str, Any]]:
    # Expand :strategy_ids via sqlalchemy bindparam(expanding=True) so the
    # IN clause becomes a proper parametric expansion (IN (:p1, :p2, ...)).
    # Avoids any string interpolation of the ID list.
    from sqlalchemy import bindparam
    stmt = text(_Q_GHOST_24H).bindparams(bindparam("strategy_ids", expanding=True))
    res = await db.execute(stmt, {"strategy_ids": list(_FEATURED_IDS)})
    return [dict(r) for r in res.mappings().all()]


async def _fetch_wallet_latest(db: AsyncSession) -> Optional[dict[str, Any]]:
    res = await db.execute(text(_Q_WALLET_LATEST))
    row = res.mappings().first()
    return dict(row) if row else None


async def _fetch_bankroll_24h(db: AsyncSession) -> list[dict[str, Any]]:
    res = await db.execute(text(_Q_BANKROLL_24H))
    return [dict(r) for r in res.mappings().all()]


async def _fetch_reconciler_health(db: AsyncSession) -> Optional[dict[str, Any]]:
    res = await db.execute(text(_Q_RECONCILER_HEALTH))
    row = res.mappings().first()
    return dict(row) if row else None


# ─── Shaping helpers ─────────────────────────────────────────────────────────


def _iso(dt: Any) -> Optional[str]:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def _f(v: Any) -> Optional[float]:
    """Coerce DB numerics → float for JSON, preserving None."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any, default: int = 0) -> int:
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _shape_live_8h(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "strategy": r["strategy"],
            "fires":      _i(r["fires"]),
            "wins":       _i(r["wins"]),
            "losses":     _i(r["losses"]),
            "pending":    _i(r["pending"]),
            "wr_pct":     _f(r["wr_pct"]),
            "net_pnl_usd": _f(r["net_pnl_usd"]) or 0.0,
            "stake_usd":  _f(r["stake_usd"]) or 0.0,
        }
        for r in rows
    ]


def _shape_ghost_24h(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "strategy_id": r["strategy_id"],
            "asset":       r["asset"],
            "would_fires": _i(r["would_fires"]),
            "resolved":    _i(r["resolved"]),
            "wins":        _i(r["wins"]),
            "losses":      _i(r["losses"]),
            "would_wr_pct": _f(r["would_wr_pct"]),
            "avg_fill_price": _f(r["avg_fill_price"]),
            "last_fire_at":  _iso(r.get("last_fire_at")),
        }
        for r in rows
    ]


def _shape_featured(
    ghost_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Cross-reference the 5 featured strategies against ghost_24h roll-up.

    Each card carries the training projection from the registry above and
    the actual live shadow WR from strategy_decisions. The FE highlights
    rows with ``train_serve_skew_pp > 5`` so an operator can spot a
    drifted model at a glance.
    """
    by_id = {r["strategy_id"]: r for r in ghost_rows}
    out: list[dict[str, Any]] = []
    for spec in _FEATURED_STRATEGIES:
        sid = spec["strategy_id"]
        row = by_id.get(sid)
        live = row["would_wr_pct"] if row else None
        train = spec["train_projection_wr_pct"]
        skew = (train - live) if (live is not None and train is not None) else None
        out.append({
            "strategy_id": sid,
            "asset": spec["asset"],
            "family": spec["family"],
            "train_projection_wr_pct": train,
            "live_would_wr_pct": live,
            "train_serve_skew_pp": round(skew, 1) if skew is not None else None,
            "would_fires_24h": (row["would_fires"] if row else 0),
            "resolved_24h":    (row["resolved"]    if row else 0),
            "has_data": row is not None,
            "skew_warning": (skew is not None and abs(skew) > 5.0),
        })
    return out


def _shape_wallet(latest: Optional[dict[str, Any]]) -> dict[str, Any]:
    """USDC + pUSD from the Montreal reconciler's wallet_snapshots row.

    The hub box cannot reach Polymarket data-api (memory:
    feedback_polymarket_montreal_only), so both balances are written
    server-side by `engine/reconciliation/reconciler.py` and the hub
    just reads the freshest row. ``balance_pusd`` is NULL on rows
    written before the migration; in that case the shape returns it
    as ``None`` and the FE shows a "sidecar lag" pill.

    A row is considered ``stale`` when ``recorded_at`` is more than
    5 minutes old — the reconciler poll cadence is ~60s, so anything
    beyond 5 min is a real outage rather than jitter.
    """
    if latest is None:
        return {
            "balance_usdc": None,
            "balance_pusd": None,
            "effective_balance": None,
            "snapshot_at": None,
            "source": None,
            "stale": True,
            "pusd_note": "no wallet_snapshots rows — engine writer may be down",
        }
    usdc = _f(latest.get("balance_usdc"))
    pusd = _f(latest.get("balance_pusd"))
    effective = None
    if usdc is not None or pusd is not None:
        effective = float((usdc or 0.0) + (pusd or 0.0))
    recorded_at = latest.get("recorded_at")
    stale = True
    age_seconds: Optional[float] = None
    if recorded_at is not None:
        try:
            age_seconds = (datetime.now(timezone.utc) - recorded_at).total_seconds()
            stale = age_seconds > 300.0  # 5 min
        except Exception:
            stale = True
    pusd_note: Optional[str]
    if pusd is None:
        # Pre-migration row, or Montreal sidecar dropped the pUSD value
        # (RPC failure → reconciler treats as 0.0 in-memory, but writes
        # NULL — see _read_pusd_balance). Either way the FE should show
        # a pill, not silently render "pUSD $0.00".
        pusd_note = "pUSD not yet recorded — Montreal sidecar lag or pre-migration row"
    else:
        pusd_note = None
    return {
        "balance_usdc": usdc,
        "balance_pusd": pusd,
        "effective_balance": effective,
        "snapshot_at": _iso(recorded_at),
        "source": latest.get("source"),
        "stale": stale,
        "age_seconds": age_seconds,
        "pusd_note": pusd_note,
    }


def _shape_bankroll_24h(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "bucket_at": _iso(r["bucket_at"]),
            "min_usdc": _f(r["min_usdc"]),
            "max_usdc": _f(r["max_usdc"]),
            "avg_usdc": _f(r["avg_usdc"]),
        }
        for r in rows
    ]


def _shape_reconciler_health(row: Optional[dict[str, Any]]) -> dict[str, Any]:
    if row is None:
        return {
            "today_redeemed": 0,
            "recent_redeemed_5m": 0,
            "today_payout_usd": 0.0,
            "last_stamp_at": None,
            "healthy": False,
        }
    last_at = row.get("last_stamp_at")
    # "Healthy" = something was stamped in the last 30 minutes. The reconciler
    # cadence is sub-minute when there's at least one resolved window pending
    # redemption, so 30 min is a generous outage window before we flag a red.
    healthy = False
    if last_at is not None:
        try:
            age_s = (datetime.now(timezone.utc) - last_at).total_seconds()
            healthy = age_s < 1800.0  # 30 min
        except Exception:
            healthy = False
    return {
        "today_redeemed": _i(row.get("today_redeemed")),
        "recent_redeemed_5m": _i(row.get("recent_redeemed_5m")),
        "today_payout_usd": _f(row.get("today_payout_usd")) or 0.0,
        "last_stamp_at": _iso(last_at),
        "healthy": healthy,
    }


# ─── Route ───────────────────────────────────────────────────────────────────


@router.get("/monitor/analysis")
async def get_monitor_analysis(
    db: AsyncSession = Depends(get_session),
    _user=Depends(get_current_user),
) -> dict[str, Any]:
    """Single-call analysis page.

    Returns six sections in one envelope. Each section is fetched
    via asyncio.gather with per-section exception isolation — any
    one query failure logs and falls through to a defaulted shape
    so the dashboard never blanks.
    """
    cached = _cache_get()
    if cached is not None:
        return {**cached, "cache_hit": True}

    try:
        live_rows, ghost_rows, wallet_row, bank_rows, recon_row = await asyncio.gather(
            _fetch_live_8h(db),
            _fetch_ghost_24h(db),
            _fetch_wallet_latest(db),
            _fetch_bankroll_24h(db),
            _fetch_reconciler_health(db),
            return_exceptions=True,
        )
    except Exception as exc:  # pragma: no cover — gather rarely raises itself
        log.warning("monitor_analysis.gather_failed", error=str(exc)[:200])
        return {
            "live_8h": [], "ghost_24h": [], "featured": _shape_featured([]),
            "wallet": _shape_wallet(None),
            "bankroll_24h": [], "reconciler_health": _shape_reconciler_health(None),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "cache_hit": False,
            "error": str(exc)[:200],
        }

    # Defang per-section failures — log + fall through to empty default.
    if isinstance(live_rows, Exception):
        log.warning("monitor_analysis.live_8h_failed", error=str(live_rows)[:200])
        live_rows = []
    if isinstance(ghost_rows, Exception):
        log.warning("monitor_analysis.ghost_24h_failed", error=str(ghost_rows)[:200])
        ghost_rows = []
    if isinstance(wallet_row, Exception):
        log.warning("monitor_analysis.wallet_failed", error=str(wallet_row)[:200])
        wallet_row = None
    if isinstance(bank_rows, Exception):
        log.warning("monitor_analysis.bankroll_failed", error=str(bank_rows)[:200])
        bank_rows = []
    if isinstance(recon_row, Exception):
        log.warning("monitor_analysis.reconciler_failed", error=str(recon_row)[:200])
        recon_row = None

    ghost_shaped = _shape_ghost_24h(ghost_rows)

    payload = {
        "live_8h":           _shape_live_8h(live_rows),
        "ghost_24h":         ghost_shaped,
        "featured":          _shape_featured(ghost_shaped),
        "wallet":            _shape_wallet(wallet_row),
        "bankroll_24h":      _shape_bankroll_24h(bank_rows),
        "reconciler_health": _shape_reconciler_health(recon_row),
        "fetched_at":        datetime.now(timezone.utc).isoformat(),
        "cache_hit":         False,
    }
    _cache_put(payload)
    return payload
