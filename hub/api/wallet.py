"""
Wallet v2 — CLOB-first, trustworthy endpoints backing the /wallet page.

Spec: hub note #189 (§4.2–4.4, §5, §7.3).

Endpoints:
  GET /api/wallet/snapshot   — page-load payload (balance, unredeemed,
                                redeemer_health, deltas)
  GET /api/wallet/pending    — unredeemed positions with stuck_reason
                                classification
  GET /api/wallet/history    — settled trades with transport + initiator

Hard constraint: this module MUST NOT call `*.polymarket.com` directly.
All Polymarket-derived data is read from engine-written DB tables
(`poly_pending_wins`, `redeemer_state`, `wallet_snapshots`) only. The
engine perimeter owns Polymarket; the hub is read-only on Postgres.

Source-of-trust rank (spec §3, 1=best):
  1 → on-chain Polygon RPC 2-of-3 consensus   (NOT AVAILABLE in hub yet)
  2 → data-api.polymarket.com positions       (BANNED from hub)
  3 → data-api.polymarket.com activity        (BANNED from hub)
  4 → local Postgres trades                    (USED)
  5 → local Postgres wallet_snapshots / poly_pending_wins / redeemer_state
       (USED — "db-only" fallback)

Because rank 1–3 sources are not wired into the hub at this spec milestone,
every response pins `_meta.sot_rank = 5` ("db-only"). When the engine adds
authoritative on-chain writes into these tables (audit #252 fix), the
records gain a `source='onchain_2of3'` flag and `_sot_rank_for_row` picks
that up automatically — no endpoint change needed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/wallet", tags=["wallet"])


# ─── SOT rank helpers ─────────────────────────────────────────────────────────


def _sot_rank(*ranks: int) -> int:
    """Aggregate sub-source trust ranks → worst (max) rank.

    Spec §5 rule: "rank=1 iff ALL sub-values come from authoritative
    sources, else take the max (worst) rank across sub-sources."
    """
    clean = [r for r in ranks if r is not None]
    if not clean:
        return 5
    return max(clean)


def _is_missing_table_error(exc: Exception) -> bool:
    """Postgres 42P01 (undefined_table) via SQLAlchemy ProgrammingError.

    Same shape as api/positions.py::_is_missing_table_error — copied so
    the wallet module has no internal cross-file coupling (small cost,
    fewer cascading imports if positions.py is deleted later).
    """
    if not isinstance(exc, ProgrammingError):
        return False
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return sqlstate == "42P01"


# ─── stuck_reason classifier (pure — tested in isolation) ─────────────────────


STUCK_REASON_LEGEND = {
    "negrisk_unresolved": (
        "CTF payoutDenominator=0, NegRisk oracle has not reported"
    ),
    "relayer_quota_exhausted": (
        "Builder Relayer 100/day cap hit; use MATIC route"
    ),
    "engine_cooldown": (
        "Redeemer in backoff after recent failure"
    ),
}


def classify_stuck_reason(
    *,
    redeemable: Optional[bool],
    payout_denominator: Optional[int],
    cooldown_active: bool,
    quota_used_today: int,
    daily_quota_limit: int,
) -> Optional[str]:
    """Classify why a pending win is still manually-actionable.

    Priority order (spec §4.3):
      1. NegRisk oracle unresolved  → `negrisk_unresolved`
         payoutDenominator==0 on-chain means the oracle hasn't reported,
         so neither relayer nor MATIC-route can redeem yet.
      2. Engine cooldown active     → `engine_cooldown`
         Redeemer is in backoff — human click would race; prefer manual.
      3. Relayer quota exhausted    → `relayer_quota_exhausted`
         80+ daily-quota calls used → MATIC-route fallback required.
      4. Otherwise                  → None  (redeemer should handle it;
         absence of reason + redeemable=True means the engine is slow).
    """
    # Only classify rows that should-be-redeemable but aren't happening
    if redeemable is False:
        # Not redeemable yet — NegRisk oracle check takes precedence
        if payout_denominator is not None and int(payout_denominator) == 0:
            return "negrisk_unresolved"
        return "negrisk_unresolved"
    # redeemable in (True, None): engine _should_ be acting
    if cooldown_active:
        return "engine_cooldown"
    if daily_quota_limit and quota_used_today >= int(0.8 * daily_quota_limit):
        return "relayer_quota_exhausted"
    return None


# ─── Metadata JSONB helper (copied shape from api/trades.py) ──────────────────


def _meta(v: Any) -> dict:
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return {}
    return {}


def _f(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ─── /api/wallet/snapshot ─────────────────────────────────────────────────────


@router.get("/snapshot")
async def get_wallet_snapshot(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Spec §4.2 — page-load wallet snapshot.

    Sub-queries + their trust ranks:

      balance.cash_usdc_onchain
        rank 5 — `engine_state.wallet_balance_usdc` from system_state
        JSONB. Hub does NOT have a Polygon RPC helper yet, so this is
        DB-derived. Memory `reference_wallet_truth.md` warns this can be
        stale; FE should call this out via _meta.sot_rank.
        (When a hub-side 2-of-3 RPC helper lands, this lifts to rank 1.)

      unredeemed.*
        rank 5 — `trades WHERE outcome='WIN' AND redeemed!=true`,
        cross-referenced with poly_pending_wins. redeemer_state provides
        quota / cooldown.

      redeemer_health.wins_missed_by_engine_24h
        rank 4 — `trades WHERE transport='manual' AND
        initiator='polymarket_sweeper' AND resolved_at > NOW()-24h`.
        Will read 0 until the engine + manual_redeem.py write these
        columns (separate PR); migration in this PR adds the schema.

      deltas
        rank 5 — bankroll_24h_ago - bankroll_now derived from trades PnL
        over the same 24h window. Best-effort.
    """
    missing_tables: list[str] = []
    sub_ranks: list[int] = []

    # ── balance.cash_usdc_onchain ───────────────────────────────────────────
    cash_usdc = 0.0
    cash_source = "db-only"
    try:
        row = (
            await session.execute(
                text(
                    "SELECT state FROM system_state WHERE id = 1"
                )
            )
        ).mappings().first()
        if row:
            state = row.get("state") or {}
            if isinstance(state, str):
                try:
                    state = json.loads(state)
                except json.JSONDecodeError:
                    state = {}
            cash_usdc = float(
                state.get("wallet_balance_usdc")
                or state.get("current_balance")
                or 0.0
            )
    except Exception as exc:  # noqa: BLE001
        if _is_missing_table_error(exc):
            missing_tables.append("system_state")
        else:
            log.error("wallet.cash_query_failed", error=str(exc)[:200])
            raise
    sub_ranks.append(5)

    # ── balance.pending_wins_value + unredeemed counts ──────────────────────
    pending_total = 0.0
    pending_count = 0
    overdue_5min = 0
    overdue_1h = 0
    try:
        rows = (
            await session.execute(
                text(
                    "SELECT value, "
                    "  EXTRACT(EPOCH FROM (NOW() - window_end_utc))::int "
                    "    AS overdue_seconds "
                    "FROM poly_pending_wins"
                )
            )
        ).mappings().all()
        pending_count = len(rows)
        pending_total = round(sum(float(r["value"]) for r in rows), 2)
        overdue_5min = sum(1 for r in rows if int(r["overdue_seconds"] or 0) > 300)
        overdue_1h = sum(1 for r in rows if int(r["overdue_seconds"] or 0) > 3600)
    except Exception as exc:  # noqa: BLE001
        if _is_missing_table_error(exc):
            missing_tables.append("poly_pending_wins")
        else:
            log.error("wallet.pending_query_failed", error=str(exc)[:200])
            raise
    sub_ranks.append(5)

    # ── redeemer_health ─────────────────────────────────────────────────────
    rs: dict = {}
    try:
        rs_row = (
            await session.execute(
                text(
                    "SELECT cooldown_active, cooldown_remaining_seconds, "
                    "  cooldown_resets_at, cooldown_reason, "
                    "  daily_quota_limit, quota_used_today, observed_at "
                    "FROM redeemer_state "
                    "ORDER BY observed_at DESC LIMIT 1"
                )
            )
        ).mappings().first()
        if rs_row:
            rs = dict(rs_row)
    except Exception as exc:  # noqa: BLE001
        if _is_missing_table_error(exc):
            missing_tables.append("redeemer_state")
        else:
            log.error("wallet.redeemer_query_failed", error=str(exc)[:200])
            raise
    sub_ranks.append(5)

    cooldown_active = bool(rs.get("cooldown_active"))
    daily_quota_limit = int(rs.get("daily_quota_limit") or 100)
    quota_used_today = int(rs.get("quota_used_today") or 0)

    # wins_missed_by_engine_24h — depends on transport/initiator columns
    # added in this PR's migration. Safe: NULL matches nothing, value=0
    # until engine + manual_redeem.py start writing the columns.
    wins_missed = 0
    try:
        wm_row = (
            await session.execute(
                text(
                    "SELECT COUNT(*) AS n FROM trades "
                    "WHERE transport = 'manual' "
                    "  AND initiator = 'polymarket_sweeper' "
                    "  AND resolved_at > NOW() - INTERVAL '24 hours'"
                )
            )
        ).mappings().first()
        wins_missed = int(wm_row["n"] or 0) if wm_row else 0
    except Exception as exc:  # noqa: BLE001
        # Column missing → pre-migration DB. Not a hard error.
        if _is_missing_table_error(exc) or "transport" in str(exc).lower():
            wins_missed = 0
        else:
            log.warning("wallet.wins_missed_query_soft_fail", error=str(exc)[:200])
            wins_missed = 0
    sub_ranks.append(4)

    # ── deltas (rank 5 — derived from trades PnL 24h) ──────────────────────
    delta_24h = 0.0
    try:
        pnl_row = (
            await session.execute(
                text(
                    "SELECT COALESCE(SUM(pnl_usd), 0) AS pnl FROM trades "
                    "WHERE resolved_at > NOW() - INTERVAL '24 hours' "
                    "  AND pnl_usd IS NOT NULL"
                )
            )
        ).mappings().first()
        if pnl_row:
            delta_24h = round(float(pnl_row["pnl"] or 0.0), 2)
    except Exception as exc:  # noqa: BLE001
        log.warning("wallet.delta_query_soft_fail", error=str(exc)[:200])
    sub_ranks.append(5)

    effective_total = round(cash_usdc + pending_total, 2)

    last_sweep_iso = None
    observed_at = rs.get("observed_at")
    if hasattr(observed_at, "isoformat"):
        last_sweep_iso = observed_at.isoformat()

    return {
        "balance": {
            "cash_usdc_onchain": round(cash_usdc, 2),
            "cash_usdc_source": cash_source,
            "pending_wins_value": pending_total,
            "open_positions_mark": 0.0,  # placeholder — no open-positions feed
            "effective_total": effective_total,
            "deltas": {
                "vs_24h_ago": delta_24h,
                "vs_session_start": None,
            },
        },
        "unredeemed": {
            "redeemable_now": pending_count,
            "redeemable_value_usd": pending_total,
            "negrisk_lag_count": 0,  # classifier populates this on /pending
            "overdue_5min": overdue_5min,
            "overdue_1h": overdue_1h,
        },
        "redeemer_health": {
            "last_successful_sweep_utc": last_sweep_iso,
            "cooldown_active": cooldown_active,
            "cooldown_reason": rs.get("cooldown_reason") or None,
            "cooldown_resets_at": (
                rs["cooldown_resets_at"].isoformat()
                if hasattr(rs.get("cooldown_resets_at"), "isoformat")
                else None
            ),
            "wins_missed_by_engine_24h": wins_missed,
            "quota_used_today": quota_used_today,
            "quota_limit": daily_quota_limit,
        },
        "deltas": {
            "vs_24h_ago": delta_24h,
        },
        "_meta": {
            "sot_rank": _sot_rank(*sub_ranks),
            "sources_used": ["db"],
            "missing_tables": missing_tables,
            "data_stale": len(missing_tables) > 0,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    }


# ─── /api/wallet/pending ──────────────────────────────────────────────────────


@router.get("/pending")
async def get_wallet_pending(
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Spec §4.3 — rows of unredeemed positions with stuck_reason.

    Data source: `trades` LEFT JOIN `poly_pending_wins` on condition_id.
    We prefer trades (has strategy + cost context) and enrich with the
    pending-wins row for window_end_utc + overdue_seconds.

    `trades` lookup: WHERE outcome='WIN' AND (metadata->>'redemption_state'
    IS DISTINCT FROM 'redeemed'). Matches spec §5.1 rule — FE legacy query
    filtered client-side on `redeemed !== true`, which was a silent no-op.
    """
    # Pull redeemer_state first so we can classify stuck_reason per-row.
    cooldown_active = False
    quota_used_today = 0
    daily_quota_limit = 100
    meta_missing: list[str] = []

    try:
        rs_row = (
            await session.execute(
                text(
                    "SELECT cooldown_active, daily_quota_limit, quota_used_today "
                    "FROM redeemer_state ORDER BY observed_at DESC LIMIT 1"
                )
            )
        ).mappings().first()
        if rs_row:
            cooldown_active = bool(rs_row["cooldown_active"])
            daily_quota_limit = int(rs_row["daily_quota_limit"] or 100)
            quota_used_today = int(rs_row["quota_used_today"] or 0)
    except Exception as exc:  # noqa: BLE001
        if _is_missing_table_error(exc):
            meta_missing.append("redeemer_state")
        else:
            log.warning("wallet.pending_rs_query_failed", error=str(exc)[:200])

    rows_out: list[dict] = []
    try:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT
                        COALESCE(pw.condition_id, t.clob_order_id) AS condition_id,
                        t.market_slug AS title,
                        t.direction   AS side,
                        t.fill_size   AS size,
                        t.stake_usd   AS cost_usd,
                        pw.value      AS current_value_usd,
                        pw.window_end_utc,
                        EXTRACT(EPOCH FROM (NOW() - pw.window_end_utc))::int
                            AS overdue_seconds,
                        t.strategy    AS strategy,
                        t.metadata    AS metadata
                    FROM trades t
                    LEFT JOIN poly_pending_wins pw
                      ON pw.condition_id = t.clob_order_id
                    WHERE t.outcome = 'WIN'
                      AND (t.metadata->>'redemption_state' IS DISTINCT FROM 'redeemed')
                    ORDER BY pw.window_end_utc ASC NULLS LAST,
                             t.resolved_at DESC
                    LIMIT 500
                    """
                )
            )
        ).mappings().all()
        for r in rows:
            meta = _meta(r.get("metadata"))
            redeemable = meta.get("redeemable")
            payout_denom = meta.get("payout_denominator")
            reason = classify_stuck_reason(
                redeemable=redeemable,
                payout_denominator=payout_denom,
                cooldown_active=cooldown_active,
                quota_used_today=quota_used_today,
                daily_quota_limit=daily_quota_limit,
            )
            window_end = r.get("window_end_utc")
            rows_out.append(
                {
                    "condition_id": r.get("condition_id"),
                    "title": r.get("title"),
                    "side": r.get("side"),
                    "size": _f(r.get("size")),
                    "cost_usd": _f(r.get("cost_usd")),
                    "current_value_usd": _f(r.get("current_value_usd")),
                    "redeemable": redeemable,
                    "payout_denominator_onchain": payout_denom,
                    "window_end_utc": (
                        window_end.isoformat()
                        if hasattr(window_end, "isoformat")
                        else None
                    ),
                    "overdue_seconds": (
                        int(r["overdue_seconds"])
                        if r.get("overdue_seconds") is not None
                        else None
                    ),
                    "strategy": r.get("strategy"),
                    "stuck_reason": reason,
                }
            )
    except Exception as exc:  # noqa: BLE001
        if _is_missing_table_error(exc):
            # Either `trades` (shouldn't happen) or `poly_pending_wins`
            # missing → return empty with degraded _meta.
            meta_missing.append("poly_pending_wins_or_trades")
            log.warning("wallet.pending_query_missing_table", error=str(exc)[:200])
        else:
            log.error("wallet.pending_query_failed", error=str(exc)[:200])
            raise

    sot = 5 if rows_out or meta_missing else 5

    return {
        "rows": rows_out,
        "stuck_reason_legend": STUCK_REASON_LEGEND,
        "_meta": {
            "sot_rank": sot,
            "sources_used": ["db"],
            "missing_tables": meta_missing,
            "data_stale": len(meta_missing) > 0,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "reason": (
                "db-only, redeemer_state missing"
                if "redeemer_state" in meta_missing
                else None
            ),
        },
    }


# ─── /api/wallet/history ──────────────────────────────────────────────────────


@router.get("/history")
async def get_wallet_history(
    limit: int = Query(300, ge=1, le=1000),
    page: int = Query(1, ge=1),
    page_size: Optional[int] = Query(None, ge=1, le=1000),
    days: Optional[int] = Query(None, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Spec §4.4 — settled trades with transport + initiator.

    Paging: consistent with /api/trades (page + page_size). `limit` is
    an alias kept for the FE default: `?limit=300` yields most-recent 300,
    offset 0. `days` narrows to recent settlements (default: all).

    `transport` / `initiator` are NULL for pre-migration rows; FE renders
    as "—". No outcome filter — we return WIN + LOSS (settled trades).
    """
    effective_limit = page_size if page_size is not None else limit
    offset = (page - 1) * effective_limit if page_size is not None else 0

    where: list[str] = ["outcome IN ('WIN', 'LOSS')"]
    params: dict[str, Any] = {}

    if days is not None:
        where.append("resolved_at >= NOW() - make_interval(days => :days)")
        params["days"] = days

    where_sql = " AND ".join(where)

    rows = (
        await session.execute(
            text(
                f"""
                SELECT
                    id,
                    clob_order_id     AS condition_id,
                    resolved_at,
                    market_slug       AS title,
                    direction         AS side,
                    stake_usd         AS cost_usd,
                    payout_usd,
                    pnl_usd,
                    transport,
                    initiator,
                    strategy,
                    metadata
                FROM trades
                WHERE {where_sql}
                ORDER BY resolved_at DESC NULLS LAST, created_at DESC
                LIMIT :lim OFFSET :off
                """
            ),
            {**params, "lim": effective_limit, "off": offset},
        )
    ).mappings().all()

    rows_out: list[dict] = []
    for r in rows:
        meta = _meta(r.get("metadata"))
        resolved_at = r.get("resolved_at")
        rows_out.append(
            {
                "id": r.get("id"),
                "condition_id": r.get("condition_id"),
                "redeemed_at_utc": (
                    resolved_at.isoformat()
                    if hasattr(resolved_at, "isoformat")
                    else None
                ),
                "title": r.get("title"),
                "side": r.get("side"),
                "cost_usd": _f(r.get("cost_usd")),
                "payout_usd": _f(r.get("payout_usd")),
                "pnl_usd": _f(r.get("pnl_usd")),
                "transport": r.get("transport"),
                "initiator": r.get("initiator"),
                "strategy": r.get("strategy"),
                "redeem_tx": meta.get("redeem_tx"),
                "gas_matic": _f(meta.get("gas_matic")),
            }
        )

    return {
        "rows": rows_out,
        "_meta": {
            "sot_rank": 5,
            "sources_used": ["db"],
            "limit": effective_limit,
            "page": page if page_size is not None else 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    }
