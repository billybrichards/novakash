"""
DB-backed feature emitters for the Sequoia v5 push contract.

These helpers compute features the engine has the data for but didn't
previously emit into `ticks_v2_probability.features` JSONB. They are
called from `engine/strategies/five_min_vpin.py` immediately before the
`build_v5_feature_body(...)` call.

## Train/serve parity rules

Every formula here mirrors the training-side SQL in
`training/queries.py` (novakash-timesfm-repo) — see the linked line
numbers in each function. Drift between this module and that file is a
silent train/serve skew bug; bump both sides in the same PR or document
the divergence in the PR body.

## Why a separate module

We could have shoved the SQL inline into `five_min_vpin._evaluate_window`,
but the function is already 3000+ lines (audit #224 / #233 / #396 noted
this multiple times). Splitting these out into a thin async-helper
module lets us:

  1. unit-test each emitter against a stub pool without spinning up the
     full strategy stack;
  2. keep `five_min_vpin.py` readable when adding the next batch of
     features;
  3. fail soft (return None) at the per-emitter granularity rather than
     letting one bad SQL call wipe out the whole feature dict.

## Failure semantics

Every emitter returns `None` (or a dict-of-None for the multi-output
ones) on ANY error path:

  - DB pool is None / not started → None
  - Query raises (timeout, connection lost, etc.) → None
  - Query returns no rows → None
  - Query returns a NULL value → None  (already handled by asyncpg's
    NULL → None mapping)

`None` flows through `coerce_float` → JSON null → scorer NaN → LightGBM
missing-value path. DO NOT return `0.0` as a "fallback" — that's a real
signal and creates train/serve skew for any feature where 0.0 is
plausible (oi_delta_cumulative can legitimately sit at 0.0 in a quiet
market).
"""

from __future__ import annotations

import datetime as _dt
import math
from typing import Any, Optional

import structlog

log = structlog.get_logger(__name__)


# ────────────────────────────────────────────────────────────────────
#  CoinGlass OI delta cumulative
# ────────────────────────────────────────────────────────────────────


async def compute_oi_delta_cumulative(
    pool: Any,
    asset: str,
    window_ts: Optional[int],
    now: Optional[_dt.datetime] = None,
) -> Optional[float]:
    """
    Sum of `oi_delta_pct` from `ticks_coinglass` within the current 5m window.

    Mirrors `training/queries.py:434-440` (novakash-timesfm-repo):

        SELECT SUM(oi_delta_pct)::double precision AS nested_5m_oi_delta_cumulative
        FROM ticks_coinglass
        WHERE asset = $1
          AND ts >= to_timestamp($2)
          AND ts <  $3

    The training-side query closes at `t.window_end` which is
    `to_timestamp(window_ts + window_seconds)`. Here we close at "now"
    because the engine doesn't yet know the window-end until the window
    closes — and we want the cumulative over what's been observed SO
    FAR (the same semantics the scorer would compute if it had
    real-time data). At training time the window has fully closed by
    the time the SQL runs, so the two definitions are identical for
    closed windows but differ for in-flight windows.

    Args:
        pool:      asyncpg pool (or None if RDS not connected yet).
        asset:     'BTC' | 'ETH' | 'XRP' | … (case-sensitive, matches
                   the value the engine writes to ticks_coinglass.asset).
        window_ts: epoch seconds of the window OPEN.
        now:       wall-clock UTC. Pass None to use datetime.now(UTC).
                   Hook for deterministic tests.

    Returns:
        float (Σ oi_delta_pct), or None on any error path.
    """
    if pool is None or window_ts is None or not asset:
        return None
    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT SUM(oi_delta_pct)::double precision AS s
                FROM ticks_coinglass
                WHERE asset = $1
                  AND ts >= to_timestamp($2::bigint)
                  AND ts <  $3
                """,
                asset,
                int(window_ts),
                now,
            )
    except Exception as exc:  # asyncpg.PostgresError, asyncio.TimeoutError, …
        log.debug(
            "feature_emitters.oi_delta_cumulative.error",
            asset=asset,
            error=str(exc)[:120],
        )
        return None
    if row is None:
        return None
    s = row["s"]
    if s is None:
        return None
    try:
        f = float(s)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


# ────────────────────────────────────────────────────────────────────
#  Polymarket CLOB pre-event aggregates
# ────────────────────────────────────────────────────────────────────


# Default pre-event window: 60s before window_open (training-side default).
# Override via the `pre_event_seconds` kwarg if a future booster trains on
# a different band; keep the default aligned with the training query in
# novakash-timesfm-repo (when that query is added — at time of writing
# the v9.5 booster uses 60s per the loader comment in
# `app/v5_feature_loader.py:495`).
DEFAULT_PRE_EVENT_SECONDS: int = 60


def _empty_clob_pre() -> dict[str, Optional[float]]:
    return {
        "clob_pre_imbalance": None,
        "clob_pre_vig": None,
        "clob_up_pre_stdev": None,
        "clob_dn_pre_stdev": None,
        "clob_up_pre_n": None,
    }


async def compute_clob_pre_aggregates(
    pool: Any,
    asset: str,
    window_ts: Optional[int],
    pre_event_seconds: int = DEFAULT_PRE_EVENT_SECONDS,
    *,
    up_token_id: Optional[str] = None,
    down_token_id: Optional[str] = None,
) -> dict[str, Optional[float]]:
    """
    Aggregate Polymarket CLOB book state over the pre-event window.

    Reads `clob_book_snapshots` (richer schema with bid/ask depth) and
    returns 5 features capturing the book's behaviour in the
    `pre_event_seconds` seconds BEFORE `window_ts` opens.

    Returned dict keys (always present, values float or None):

      clob_pre_imbalance   — mean of (up_bid_depth - down_bid_depth)
                             / (up_ask_depth + down_ask_depth)
                             where the denominator > 0.
      clob_pre_vig         — mean of (up_best_ask + down_best_ask - 1.0).
                             Non-negative for a "tight" book; near 0 when
                             the market sums to 1.
      clob_up_pre_stdev    — population stddev of (up_best_bid+up_best_ask)/2
                             over the pre-event ticks.
      clob_dn_pre_stdev    — population stddev of (down_best_bid+down_best_ask)/2.
      clob_up_pre_n        — number of CLOB snapshots in the pre-event window.

    The training-side aggregator (when wired) should source from the
    SAME pre-event window and use SAMPLE stddev — we use PostgreSQL's
    STDDEV_POP for stability when n is small (PG returns NULL for
    STDDEV (sample) with n<2). If the training query later pins SAMPLE
    stddev, update this function to match and bump the test.

    Args:
        pool:               asyncpg pool.
        asset:              'BTC' | 'ETH' | 'XRP' | … . NB: the current
                            engine clob_feed writes asset='BTC' only —
                            for ETH/XRP this returns the all-None dict
                            until the CLOB feed becomes multi-asset.
        window_ts:          epoch seconds of the upcoming 5m window's open.
        pre_event_seconds:  how many seconds before window_ts to aggregate
                            over. Default 60s (training-side default).
        up_token_id:        Optional filter — if both token_ids are
                            supplied, the query narrows to exactly that
                            market. None → match any market for the asset.
        down_token_id:      see up_token_id.

    Returns:
        dict with 5 keys, every value float or None. Returns the
        all-None dict on any error path (pool unavailable, query fails,
        no pre-event ticks observed yet).
    """
    if pool is None or window_ts is None or not asset:
        return _empty_clob_pre()
    if pre_event_seconds <= 0:
        return _empty_clob_pre()

    try:
        async with pool.acquire() as conn:
            # We use the wider `clob_book_snapshots` table because it
            # captures `up_bid_depth` / `down_ask_depth` (the inputs the
            # imbalance ratio needs). `ticks_clob` only has best bid/ask
            # and would silently produce all-NULL imbalance.
            if up_token_id and down_token_id:
                row = await conn.fetchrow(
                    """
                    SELECT
                        AVG(
                            CASE
                                WHEN (COALESCE(up_ask_depth,0) + COALESCE(down_ask_depth,0)) > 0
                                THEN (COALESCE(up_bid_depth,0) - COALESCE(down_bid_depth,0))
                                     / (COALESCE(up_ask_depth,0) + COALESCE(down_ask_depth,0))
                            END
                        )::double precision AS imb,
                        AVG(
                            CASE
                                WHEN up_best_ask IS NOT NULL AND down_best_ask IS NOT NULL
                                THEN (up_best_ask + down_best_ask - 1.0)
                            END
                        )::double precision AS vig,
                        STDDEV_POP(
                            CASE
                                WHEN up_best_bid IS NOT NULL AND up_best_ask IS NOT NULL
                                THEN (up_best_bid + up_best_ask) / 2.0
                            END
                        )::double precision AS up_std,
                        STDDEV_POP(
                            CASE
                                WHEN down_best_bid IS NOT NULL AND down_best_ask IS NOT NULL
                                THEN (down_best_bid + down_best_ask) / 2.0
                            END
                        )::double precision AS dn_std,
                        COUNT(*)::double precision AS n
                    FROM clob_book_snapshots
                    WHERE asset = $1
                      AND up_token_id = $4
                      AND down_token_id = $5
                      AND ts >= to_timestamp($2::bigint - $3::bigint)
                      AND ts <  to_timestamp($2::bigint)
                    """,
                    asset,
                    int(window_ts),
                    int(pre_event_seconds),
                    up_token_id,
                    down_token_id,
                )
            else:
                row = await conn.fetchrow(
                    """
                    SELECT
                        AVG(
                            CASE
                                WHEN (COALESCE(up_ask_depth,0) + COALESCE(down_ask_depth,0)) > 0
                                THEN (COALESCE(up_bid_depth,0) - COALESCE(down_bid_depth,0))
                                     / (COALESCE(up_ask_depth,0) + COALESCE(down_ask_depth,0))
                            END
                        )::double precision AS imb,
                        AVG(
                            CASE
                                WHEN up_best_ask IS NOT NULL AND down_best_ask IS NOT NULL
                                THEN (up_best_ask + down_best_ask - 1.0)
                            END
                        )::double precision AS vig,
                        STDDEV_POP(
                            CASE
                                WHEN up_best_bid IS NOT NULL AND up_best_ask IS NOT NULL
                                THEN (up_best_bid + up_best_ask) / 2.0
                            END
                        )::double precision AS up_std,
                        STDDEV_POP(
                            CASE
                                WHEN down_best_bid IS NOT NULL AND down_best_ask IS NOT NULL
                                THEN (down_best_bid + down_best_ask) / 2.0
                            END
                        )::double precision AS dn_std,
                        COUNT(*)::double precision AS n
                    FROM clob_book_snapshots
                    WHERE asset = $1
                      AND ts >= to_timestamp($2::bigint - $3::bigint)
                      AND ts <  to_timestamp($2::bigint)
                    """,
                    asset,
                    int(window_ts),
                    int(pre_event_seconds),
                )
    except Exception as exc:
        log.debug(
            "feature_emitters.clob_pre_aggregates.error",
            asset=asset,
            error=str(exc)[:120],
        )
        return _empty_clob_pre()

    if row is None:
        return _empty_clob_pre()

    def _safe_float(v: Any) -> Optional[float]:
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if math.isnan(f) or math.isinf(f):
            return None
        return f

    return {
        "clob_pre_imbalance": _safe_float(row["imb"]),
        "clob_pre_vig": _safe_float(row["vig"]),
        "clob_up_pre_stdev": _safe_float(row["up_std"]),
        "clob_dn_pre_stdev": _safe_float(row["dn_std"]),
        # clob_up_pre_n is a count — we never want a NaN here; 0 is a
        # valid signal ("no pre-event ticks observed"). The training-
        # side query produces 0 in that case too, so we mirror it.
        "clob_up_pre_n": _safe_float(row["n"]) if row["n"] is not None else 0.0,
    }


# ────────────────────────────────────────────────────────────────────
#  Binance depth (top-of-book + ±1% / ±5% imbalance + spread)
# ────────────────────────────────────────────────────────────────────


def _empty_binance_depth() -> dict[str, Optional[float]]:
    return {
        "binance_depth_imbalance_inner": None,
        "binance_depth_imbalance_1pct": None,
        "binance_depth_imbalance_5pct": None,
        "binance_spread_pct": None,
    }


def _depth_imbalance(
    bid: Optional[float], ask: Optional[float]
) -> Optional[float]:
    if bid is None or ask is None:
        return None
    try:
        bf, af = float(bid), float(ask)
    except (TypeError, ValueError):
        return None
    s = bf + af
    if s <= 0:
        return None
    return (bf - af) / s


def compute_binance_depth_from_snapshot(
    snap: Optional[dict[str, Any]],
) -> dict[str, Optional[float]]:
    """Pure-function: 4-feature dict from a parsed depth snapshot.

    Extracted from `compute_binance_depth_imbalance` so the fast in-
    memory path (skips DB) can reuse it. Returns all-None dict if
    `snap` is None / malformed.
    """
    if snap is None:
        return _empty_binance_depth()
    spread = snap.get("spread_pct")
    inner = _depth_imbalance(snap.get("best_bid_qty"), snap.get("best_ask_qty"))
    one_pct = _depth_imbalance(
        snap.get("bid_depth_1pct"), snap.get("ask_depth_1pct")
    )
    five_pct = _depth_imbalance(
        snap.get("bid_depth_5pct"), snap.get("ask_depth_5pct")
    )
    try:
        spread_f = float(spread) if spread is not None else None
    except (TypeError, ValueError):
        spread_f = None
    return {
        "binance_depth_imbalance_inner": inner,
        "binance_depth_imbalance_1pct": one_pct,
        "binance_depth_imbalance_5pct": five_pct,
        "binance_spread_pct": spread_f,
    }


async def compute_binance_depth_imbalance(
    pool: Any,
    asset: str,
    *,
    max_age_seconds: int = 30,
    depth_feed: Any = None,
) -> dict[str, Optional[float]]:
    """4-feature dict for ``binance_depth_imbalance_{inner,1pct,5pct}`` + ``binance_spread_pct``.

    Two fast paths in priority order:

    1.  **In-memory snapshot via depth_feed**. If a
        ``BinanceDepthMultiFeed`` is wired and has a recent snapshot
        for the asset, derive features from it — zero DB latency on
        the scoring critical path. This is the steady-state path.

    2.  **ASOF query on ``ticks_binance_book``**. Fallback if the live
        feed hasn't observed the asset yet (cold start) or the
        in-memory snapshot is older than `max_age_seconds`. Slower but
        survives engine restarts.

    Returns all-None dict on any error path so the LightGBM missing-
    value branch is used (DO NOT return 0.0 — that's a real signal).

    Args:
        pool:            asyncpg pool (or None for in-memory-only).
        asset:           'BTC' | 'ETH' | 'XRP' | …
        max_age_seconds: Snapshot/row older than this is treated as
                         stale → None. Default 30s (depth feed flushes
                         every 1s, so 30s catches a stale connection).
        depth_feed:      Optional `BinanceDepthMultiFeed`-shaped object
                         exposing `latest_snapshot(asset) -> dict|None`.

    Returns:
        Dict with 4 keys, every value float or None.
    """
    if not asset:
        return _empty_binance_depth()

    asset_u = asset.upper()
    now_ts = _dt.datetime.now(_dt.timezone.utc).timestamp()

    # Fast path: in-memory snapshot.
    if depth_feed is not None:
        try:
            snap = depth_feed.latest_snapshot(asset_u)
        except Exception:
            snap = None
        if snap is not None:
            ts = snap.get("ts")
            try:
                age = now_ts - float(ts) if ts is not None else None
            except (TypeError, ValueError):
                age = None
            if age is None or age <= max_age_seconds:
                return compute_binance_depth_from_snapshot(snap)
        # Snapshot stale or unavailable — fall through to DB.

    if pool is None:
        return _empty_binance_depth()

    # ASOF query against ticks_binance_book.
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    best_bid_qty, best_ask_qty,
                    bid_depth_1pct, ask_depth_1pct,
                    bid_depth_5pct, ask_depth_5pct,
                    spread_pct, ts
                FROM ticks_binance_book
                WHERE asset = $1
                  AND ts >= NOW() - ($2::int * INTERVAL '1 second')
                ORDER BY ts DESC
                LIMIT 1
                """,
                asset_u,
                int(max_age_seconds),
            )
    except Exception as exc:
        log.debug(
            "feature_emitters.binance_depth.error",
            asset=asset_u,
            error=str(exc)[:120],
        )
        return _empty_binance_depth()

    if row is None:
        return _empty_binance_depth()

    # Re-use the pure helper on a normalized dict.
    snap_from_db = {
        "best_bid_qty": row["best_bid_qty"],
        "best_ask_qty": row["best_ask_qty"],
        "bid_depth_1pct": row["bid_depth_1pct"],
        "ask_depth_1pct": row["ask_depth_1pct"],
        "bid_depth_5pct": row["bid_depth_5pct"],
        "ask_depth_5pct": row["ask_depth_5pct"],
        "spread_pct": row["spread_pct"],
    }
    return compute_binance_depth_from_snapshot(snap_from_db)
