"""Hub API: /api/monitor/strategies/scorecard — per-strategy operator scorecard.

Read-only. Joins three already-populated tables:

  1. ``strategy_configs`` (engine-seeded at startup) — YAML config blob
     + asset + timescale + base mode for every registered strategy.
  2. ``strategy_runtime_overrides`` (operator/audit writes) — runtime
     mode/param flips that may shadow the YAML default. The "effective"
     mode shown in the scorecard is COALESCE(override.mode, config.mode).
  3. ``strategy_comparison`` (engine scheduler, 5-min cadence) — per-
     strategy rollup with fires/wins/losses/wr/net_pnl across multiple
     window_periods. Both LIVE and GHOST strategies emit notional PnL
     here using identical ``real_pnl_win/loss`` fee math — so a GHOST
     strategy's ``real_net_pnl_usd`` is "what it would have made if
     it were LIVE", net of the same modelled fees a LIVE fill incurs.

The output is a single flat list of "scorecard rows" — one row per
registered strategy, sorted LIVE-first by today's PnL desc, then GHOST
by today's PnL desc. The FE renders LIVE and GHOST sections separately
using the ``mode`` field as the divider.

Cache: a 10-second module-level TTL dict. The underlying tables update
on a 5-minute cadence so 10s is generous; the cache exists to absorb
dashboard auto-refresh bursts (Billy clicking around / multiple tabs).

Failure mode: all three queries are wrapped in
``asyncio.gather(..., return_exceptions=True)``. Any individual query
failure is logged and that data source falls through to defaults
(empty config map, empty stats, no last-fire timestamps). The endpoint
always returns a well-formed envelope — never 500 — so a transient DB
hiccup doesn't blank the dashboard. The FE displays empty rows as
"— no data yet" rather than throwing.

Hard constraints (note #596, dashboard PR 1):
  * READ-ONLY. Never write to ``strategy_configs`` or
    ``strategy_runtime_overrides``. The PATCH override surface lives
    on ``/api/strategies/{id}/override`` (audit #291) and is mounted
    elsewhere.
  * NO auto-promote. Mode flips happen via the existing override CRUD
    only. This endpoint observes; it does not act.
  * NO compute on the hot path. Stats are pre-aggregated by the engine
    scheduler — we just SELECT them.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
import yaml
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.middleware import get_current_user
from db.database import get_session

log = structlog.get_logger(__name__)
router = APIRouter()


# ─── Cache ───────────────────────────────────────────────────────────────────
# Module-level dict — single-process FastAPI worker per pod. If we ever
# horizontal-scale this, swap for a tiny Redis or per-key TTL helper.
# 10 s TTL absorbs auto-refresh bursts; underlying data refreshes on 5 min.

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


# ─── YAML threshold parser ───────────────────────────────────────────────────

_UP_KEYS = ("gates.up_threshold", "gates.min_probability_up", "up_threshold", "threshold")
_DN_KEYS = ("gates.down_threshold", "gates.min_probability_down", "down_threshold", "threshold")


def _dot_get(d: Any, dotted: str) -> Any:
    """Walk a dotted-key path through a nested dict. Returns None on miss."""
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def _parse_thresholds(yaml_text: Optional[str]) -> tuple[Optional[float], Optional[float]]:
    """Pull (up_threshold, down_threshold) from a strategy YAML blob.

    Fallback chain per blueprint:
        gates.up_threshold -> gates.min_probability_up -> up_threshold -> threshold

    Returns ``(None, None)`` if YAML can't be parsed or no keys hit.
    """
    if not yaml_text:
        return (None, None)
    try:
        parsed = yaml.safe_load(yaml_text)
    except Exception:
        return (None, None)
    if not isinstance(parsed, dict):
        return (None, None)

    def _first(keys: tuple[str, ...]) -> Optional[float]:
        for k in keys:
            v = _dot_get(parsed, k)
            if v is None:
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        return None

    return (_first(_UP_KEYS), _first(_DN_KEYS))


# ─── Stat helpers ────────────────────────────────────────────────────────────


def _empty_stats() -> dict[str, Any]:
    return {
        "fires": 0,
        "wins": 0,
        "losses": 0,
        "pending": 0,
        "wr_pct": None,
        "net_pnl_usd": 0.0,
    }


def _stats_from_row(r: Any) -> dict[str, Any]:
    """Map a strategy_comparison row into the scorecard ``today`` / ``week`` shape."""
    return {
        "fires": int(r["n_fires"] or 0),
        "wins": int(r["n_wins"] or 0),
        "losses": int(r["n_losses"] or 0),
        "pending": int(r["n_pending"] or 0),
        "wr_pct": float(r["wr_pct"]) if r["wr_pct"] is not None else None,
        "net_pnl_usd": float(r["real_net_pnl_usd"] or 0.0),
    }


def _iso(dt: Any) -> Optional[str]:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


# ─── Queries ─────────────────────────────────────────────────────────────────

# Query 1 — strategy registry + effective mode.
# DISTINCT ON picks the most-recent (strategy_id, version) row from
# strategy_configs so multi-version registries collapse to one row per
# strategy_id. Effective mode = override.mode if set, else config.mode.
_Q_CONFIGS = """
SELECT
    sc.strategy_id,
    sc.mode          AS config_mode,
    sc.asset,
    sc.timescale,
    sc.config_yaml,
    sc.updated_at,
    ovr.mode         AS override_mode
FROM (
    SELECT DISTINCT ON (strategy_id)
        strategy_id, version, mode, asset, timescale, config_yaml, updated_at
    FROM strategy_configs
    ORDER BY strategy_id, updated_at DESC, version DESC
) sc
LEFT JOIN strategy_runtime_overrides ovr
    ON ovr.strategy_id = sc.strategy_id
"""

# Query 2 — latest stats snapshot per strategy across 24h + 7d windows.
# Pulls only the 'all/all/all' slice (no t_band / direction / regime
# filter) so the scorecard is the operator's "total" view. The engine
# scheduler writes a new snapshot every 5 minutes; we pick the latest.
_Q_STATS = """
SELECT
    sc.strategy_id,
    sc.window_period,
    sc.n_fires,
    sc.n_wins,
    sc.n_losses,
    sc.n_pending,
    sc.wr_pct,
    sc.real_net_pnl_usd
FROM strategy_comparison sc
WHERE sc.t_band           = 'all'
  AND sc.direction_filter = 'all'
  AND sc.regime_filter    = 'all'
  AND sc.window_period    IN ('24h', '7d')
  AND sc.snapshot_at      = (
      SELECT MAX(snapshot_at) FROM strategy_comparison
  )
"""

# Query 3 — last_fire_at per strategy (most-recent decision overall).
# Cheap MAX(...) GROUP BY is fine — strategy_decisions is indexed on
# (strategy_id, evaluated_at).
_Q_LAST_FIRE = """
SELECT strategy_id, MAX(evaluated_at) AS last_fire_at
FROM strategy_decisions
GROUP BY strategy_id
"""


async def _fetch_configs(db: AsyncSession) -> list[dict[str, Any]]:
    res = await db.execute(text(_Q_CONFIGS))
    return [dict(r) for r in res.mappings().all()]


async def _fetch_stats(db: AsyncSession) -> list[dict[str, Any]]:
    res = await db.execute(text(_Q_STATS))
    return [dict(r) for r in res.mappings().all()]


async def _fetch_last_fires(db: AsyncSession) -> list[dict[str, Any]]:
    res = await db.execute(text(_Q_LAST_FIRE))
    return [dict(r) for r in res.mappings().all()]


# ─── Route ───────────────────────────────────────────────────────────────────


@router.get("/monitor/strategies/scorecard")
async def get_strategies_scorecard(
    db: AsyncSession = Depends(get_session),
    _user=Depends(get_current_user),
) -> dict[str, Any]:
    """Per-strategy scorecard for the operator monitor page.

    Returns one row per registered strategy with today + 7d stats and
    last-fire timestamp. LIVE rows sort to the top by today's PnL desc;
    GHOST rows follow in the same order. The FE renders the two groups
    as distinct sections.
    """
    cached = _cache_get()
    if cached is not None:
        # Re-stamp cache_hit so the FE knows we served from cache without
        # mutating the cached payload itself (preserves fetched_at).
        return {**cached, "cache_hit": True}

    try:
        configs, stats, last_fires = await asyncio.gather(
            _fetch_configs(db),
            _fetch_stats(db),
            _fetch_last_fires(db),
            return_exceptions=True,
        )
    except Exception as exc:  # pragma: no cover - asyncio.gather rarely raises
        log.warning("monitor_scorecard.gather_failed", error=str(exc)[:200])
        return {
            "scorecards": [],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "cache_hit": False,
            "error": str(exc)[:200],
        }

    # Each individual query may have failed — log + fall through to defaults.
    if isinstance(configs, Exception):
        log.warning("monitor_scorecard.configs_failed", error=str(configs)[:200])
        configs = []
    if isinstance(stats, Exception):
        log.warning("monitor_scorecard.stats_failed", error=str(stats)[:200])
        stats = []
    if isinstance(last_fires, Exception):
        log.warning("monitor_scorecard.last_fires_failed", error=str(last_fires)[:200])
        last_fires = []

    # Index stats by (strategy_id, window_period).
    stats_idx: dict[tuple[str, str], dict[str, Any]] = {}
    for r in stats:
        key = (r["strategy_id"], r["window_period"])
        stats_idx[key] = r

    # Index last_fire_at by strategy_id.
    fire_idx: dict[str, Any] = {r["strategy_id"]: r["last_fire_at"] for r in last_fires}

    # If configs is empty (engine hasn't seeded strategy_configs yet — fresh
    # cluster), fall through to deriving strategy_ids from strategy_comparison.
    # This degrades gracefully: no asset/timescale/threshold info, but the
    # operator still sees the stats they care about.
    if not configs:
        seen: set[str] = set()
        for r in stats:
            sid = r["strategy_id"]
            if sid in seen:
                continue
            seen.add(sid)
            configs.append({
                "strategy_id": sid,
                "config_mode": "LIVE",  # safest default when unknown
                "asset": None,
                "timescale": None,
                "config_yaml": None,
                "updated_at": None,
                "override_mode": None,
            })

    # Build the per-strategy scorecard rows.
    scorecards: list[dict[str, Any]] = []
    for cfg in configs:
        sid = cfg["strategy_id"]
        # Effective mode: runtime override wins over YAML baseline.
        effective_mode = cfg.get("override_mode") or cfg.get("config_mode") or "LIVE"
        effective_mode = str(effective_mode).upper()
        up_th, dn_th = _parse_thresholds(cfg.get("config_yaml"))

        today_row = stats_idx.get((sid, "24h"))
        week_row = stats_idx.get((sid, "7d"))

        scorecards.append({
            "strategy_id": sid,
            "mode": effective_mode,
            "asset": cfg.get("asset"),
            "timescale": cfg.get("timescale"),
            "is_ghost": effective_mode == "GHOST",
            "today": _stats_from_row(today_row) if today_row else _empty_stats(),
            "week": _stats_from_row(week_row) if week_row else _empty_stats(),
            "last_fire_at": _iso(fire_idx.get(sid)),
            "up_threshold": up_th,
            "down_threshold": dn_th,
        })

    # Sort: LIVE block (today_pnl desc), then GHOST block (today_pnl desc).
    # DISABLED rows fall to the bottom. None pnl ordered as 0 to keep stable.
    def _sort_key(row: dict[str, Any]) -> tuple:
        mode = row["mode"]
        mode_rank = 0 if mode == "LIVE" else (1 if mode == "GHOST" else 2)
        today_pnl = row["today"]["net_pnl_usd"] or 0.0
        return (mode_rank, -today_pnl)

    scorecards.sort(key=_sort_key)

    payload = {
        "scorecards": scorecards,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "cache_hit": False,
    }
    _cache_put(payload)
    return payload
