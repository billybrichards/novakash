"""On-chain authoritative per-window exposure cap.

Queries Polymarket data-api positions endpoint to enforce a per-(asset,
window_ts) cumulative-cost cap that is INDEPENDENT of the trades-table
view. Trades table can be unreliable when the sub-fill writer drops rows
(documented incidents 2026-05-20 and 2026-05-21 — see
`tasks/dedup_audit_2026-05-21.md`).

Authoritative source: Polymarket's data-api position aggregation for the
funder proxy. Whatever Polymarket sees on-chain IS the truth. If on-chain
cost + the proposed new stake would breach the cap, we SKIP — regardless
of what the DB thinks.

Opt-in via env `RISK_MAX_STAKE_PER_WINDOW_USD_PM` (default OFF). Fail-closed
on any error (timeout, HTTP 5xx, JSON parse) — block the trade. Better to
miss one legitimate fire than repeat a $69 multi-fill writer-bypass loss.

Cross-strategy race (Hub #582)
------------------------------

Multiple strategies firing in the same tick will all read on-chain
cost = 0 before any has settled (data-api lags ~30 s behind the on-chain
fill). The serialisation lock below closes the window so check + submit
runs sequentially per (funder, slug, direction). Trades on a DIFFERENT
window still parallelise (separate lock keys).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional

import structlog

log = structlog.get_logger(__name__)


_POSITIONS_URL = "https://data-api.polymarket.com/positions"
_CACHE_TTL_S = 5.0
# data-api positions endpoint observed latency from Montreal:
# typical 1.0-1.5s, p99 ~2.5s. Original 0.8s timeout failed every call
# (-3rd recurrence pattern). Bumped to 3.5s outer / 3.0s inner with the
# 5s in-process cache absorbing repeated lookups within the same tick burst.
_HTTP_TIMEOUT_S = 3.0

# {(funder, "ALL"): (timestamp, full_positions_list)}  — cache full
# response so multiple per-direction cap checks share one HTTP call.
_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}

# Hub #582: same-tick race serialisation. Per-(funder, slug, direction)
# asyncio.Lock keyed in a module-level dict so two strategies firing the
# same window are forced to take the lock sequentially. The lock spans
# read-cost → projected-check → return path so a strategy that PASSES
# the cap completes its place_order() write before the next strategy
# is allowed to read on-chain cost. Place-order itself happens outside
# the lock (the caller releases when its function returns — the in-flight
# stake is then counted by the next caller via the trades-table cap in
# Step 4.5, and by data-api once it catches up).
_lock_registry: dict[tuple[str, str, str], asyncio.Lock] = {}
_registry_lock: Optional[asyncio.Lock] = None


def _get_lock(funder: str, slug: str, direction: str) -> asyncio.Lock:
    """Return (creating if needed) the per-(funder, slug, dir) Lock.

    The registry itself is unprotected because Python's GIL serialises
    dict mutations and ``setdefault`` is atomic on CPython. Callers
    await the returned lock, not the registry lookup.
    """
    key = (funder.lower(), slug.lower(), direction.upper())
    lock = _lock_registry.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _lock_registry[key] = lock
    return lock


def get_window_cap_pm_usd() -> Optional[float]:
    """Read the optional opt-in cap from env. None = disabled."""
    raw = os.environ.get("RISK_MAX_STAKE_PER_WINDOW_USD_PM", "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except ValueError:
        return None


def _fetch_positions_sync(funder: str) -> list[dict]:
    """Single HTTP GET to data-api positions for the funder.

    NOTE: We fetch ALL positions (no market filter) because the engine
    populates `WindowMarket.condition_id` with a placeholder string like
    `"BTC-1779360000"` rather than the real 0x... hex hash (pre-existing
    bug in `runtime.py`'s WindowMarket construction). Filtering by
    market_slug client-side is reliable.

    Sync because urllib is sync; wrapped with run_in_executor at call site.
    """
    url = f"{_POSITIONS_URL}?user={funder}&sizeThreshold=0.001&limit=500"
    req = urllib.request.Request(
        url, headers={"User-Agent": "novakash-onchain-cap/1.0"}
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read())


async def get_onchain_cost_for_window(
    *,
    funder_address: str,
    market_slug: str,
    outcome: str,
) -> Optional[float]:
    """Return current on-chain cost (USDC) the funder has spent BUYING
    ``outcome`` ("Up"/"YES" or "Down"/"NO") on the market identified by
    ``market_slug`` (e.g. "btc-updown-5m-1779336900").

    Fetches ALL positions for the funder, then filters by market_slug
    client-side. We don't filter via the data-api `market=` query param
    because the engine populates `WindowMarket.condition_id` with a fake
    placeholder string rather than the real 0x... hex hash (pre-existing
    bug in runtime.py). Filtering on `slug` from the response is reliable.

    Cached for 5s per funder (NOT per market) — one fetch covers all
    markets the funder is in, amortising across parallel-strategy fires.

    Returns None on any error — caller fails-closed (block the trade).
    """
    if not funder_address or not market_slug:
        return None
    # Audit 2026-05-23: original validation accepted only ("Up", "Down",
    # "YES", "NO") but ``execute_trade`` passes the upper-cased trade
    # direction ("UP" / "DOWN") from ``decision.direction``. The
    # mismatch silently returned None for every call, which fail-closed
    # the entire cap to ``exposure_cap_window_pm_unavailable`` — engine
    # logs showed 120+ such blocks in the 40 min after the cap was
    # enabled in prod, before anyone noticed.
    normalised = (outcome or "").strip().upper()
    if normalised in ("UP", "YES"):
        pm_outcome = "Up"
    elif normalised in ("DOWN", "NO"):
        pm_outcome = "Down"
    else:
        return None

    key = (funder_address.lower(), "ALL")
    now = time.time()
    cached = _cache.get(key)
    positions: Optional[list[dict]] = None
    if cached is not None and (now - cached[0]) < _CACHE_TTL_S:
        positions = cached[1]  # full positions list stored

    if positions is None:
        loop = asyncio.get_event_loop()
        try:
            positions = await asyncio.wait_for(
                loop.run_in_executor(
                    None, _fetch_positions_sync, funder_address
                ),
                timeout=_HTTP_TIMEOUT_S + 0.2,
            )
        except (asyncio.TimeoutError, urllib.error.URLError, OSError) as exc:
            log.warning(
                "onchain_position_cap.fetch_timeout_or_neterr",
                funder=funder_address[:10],
                market_slug=market_slug[:40],
                err=str(exc)[:100],
            )
            return None
        except Exception as exc:
            log.warning(
                "onchain_position_cap.fetch_error",
                funder=funder_address[:10],
                market_slug=market_slug[:40],
                err=str(exc)[:100],
            )
            return None
        # Cache full position list.
        _cache[key] = (now, positions)

    # Filter to positions on THIS market slug
    cost = 0.0
    target = market_slug.lower()
    for p in positions:
        try:
            slug = (p.get("slug") or "").lower()
            if slug != target:
                continue
            if (p.get("outcome") or "") != pm_outcome:
                continue
            cost += float(p.get("initialValue") or 0)
        except (TypeError, ValueError):
            continue
    return cost


async def check_onchain_window_cap(
    *,
    funder_address: str,
    market_slug: str,
    direction: str,
    new_stake_usd: float,
    cap_usd: float,
) -> Optional[str]:
    """Return a skip_reason string if the proposed stake would breach the
    on-chain per-(asset, window) cap, else None.

    Fail-closed: returns ``"exposure_cap_window_pm_unavailable"`` if the
    data-api call errored. Better to skip ONE legitimate trade than
    permit another $69 multi-fill loss.

    Race protection (Hub #582)
    --------------------------

    Per-(funder, slug, direction) asyncio.Lock + in-memory pending-stake
    registry. When strategy A passes the cap and reserves $X, strategy B
    arriving in the same tick sees ``on_chain_cost + pending_stake[key]
    + new_stake_usd`` instead of ``on_chain_cost + new_stake_usd``. The
    reservation is held until ``release_pending_stake`` is called by
    the executor after the order completes (fill or cancel). If no
    release happens within ``_PENDING_STAKE_TTL_S`` the entry is
    garbage-collected to avoid permanent leak from a crashed task.
    """
    if cap_usd <= 0:
        return None
    if not funder_address or not market_slug:
        return None

    lock = _get_lock(funder_address, market_slug, direction)
    async with lock:
        return await _check_under_lock(
            funder_address=funder_address,
            market_slug=market_slug,
            direction=direction,
            new_stake_usd=new_stake_usd,
            cap_usd=cap_usd,
        )


async def _check_under_lock(
    *,
    funder_address: str,
    market_slug: str,
    direction: str,
    new_stake_usd: float,
    cap_usd: float,
) -> Optional[str]:
    on_chain_cost = await get_onchain_cost_for_window(
        funder_address=funder_address,
        market_slug=market_slug,
        outcome=direction,
    )
    if on_chain_cost is None:
        log.warning(
            "onchain_position_cap.fail_closed_block",
            market_slug=market_slug[:40],
            direction=direction,
            cap_usd=cap_usd,
        )
        return "exposure_cap_window_pm_unavailable"

    pending = _sum_pending_stake(funder_address, market_slug, direction)
    projected = on_chain_cost + pending + new_stake_usd
    if projected > cap_usd:
        log.warning(
            "onchain_position_cap.blocked",
            market_slug=market_slug[:40],
            direction=direction,
            on_chain_cost=round(on_chain_cost, 4),
            pending=round(pending, 4),
            new_stake=round(new_stake_usd, 4),
            projected=round(projected, 4),
            cap_usd=cap_usd,
        )
        return "exposure_cap_window_pm"

    # Reserve the proposed stake so a parallel-tick strategy sees it.
    # Caller MUST call ``release_pending_stake`` with the same key.
    _reserve_pending_stake(
        funder_address, market_slug, direction, new_stake_usd
    )
    return None


# ── pending-stake registry (Hub #582) ──────────────────────────────
#
# ``_pending_stakes`` holds reservations made under the lock so the
# next strategy on the same (funder, slug, direction) sees them before
# data-api catches up to the on-chain fill.
#
# Entry shape: {(funder, slug, dir): {reservation_id: (stake_usd, ts)}}.
# A reservation is released by ID so partial fills / cancels can
# release exactly what they reserved.

_PENDING_STAKE_TTL_S = 60.0
_pending_stakes: dict[tuple[str, str, str], dict[str, tuple[float, float]]] = {}
_pending_counter = 0


def _pending_key(funder: str, slug: str, direction: str) -> tuple[str, str, str]:
    return (funder.lower(), slug.lower(), direction.upper())


def _sum_pending_stake(funder: str, slug: str, direction: str) -> float:
    """Sum live reservations for the key after GC of expired ones."""
    key = _pending_key(funder, slug, direction)
    entries = _pending_stakes.get(key)
    if not entries:
        return 0.0
    now = time.time()
    total = 0.0
    expired_ids: list[str] = []
    for rid, (stake, ts) in entries.items():
        if (now - ts) > _PENDING_STAKE_TTL_S:
            expired_ids.append(rid)
            continue
        total += stake
    for rid in expired_ids:
        entries.pop(rid, None)
        log.warning(
            "onchain_position_cap.pending_stake_ttl_expired",
            slug=slug[:40],
            direction=direction,
            reservation_id=rid,
            note="reservation was not released — possible crashed task",
        )
    return total


def _reserve_pending_stake(
    funder: str, slug: str, direction: str, stake_usd: float
) -> str:
    """Add a reservation; return the reservation_id for release."""
    global _pending_counter
    _pending_counter += 1
    rid = f"r{_pending_counter}"
    key = _pending_key(funder, slug, direction)
    entries = _pending_stakes.setdefault(key, {})
    entries[rid] = (float(stake_usd), time.time())
    return rid


def release_pending_stake(
    *, funder_address: str, market_slug: str, direction: str, stake_usd: float
) -> None:
    """Release the most-recent reservation matching the stake amount.

    Called by the executor after the trade attempt completes (fill,
    rebound, cancel, error — any terminal state). Idempotent — if no
    matching reservation exists, this is a no-op.

    NOTE: We match by amount rather than by reservation_id because the
    caller does not currently thread the id through to the executor.
    With sub-fills the amount could mis-match (price improvement) — in
    that case we fall back to the closest reservation. The TTL GC is
    the ultimate backstop against permanent leaks.
    """
    key = _pending_key(funder_address, market_slug, direction)
    entries = _pending_stakes.get(key)
    if not entries:
        return
    target = float(stake_usd)
    # Find best match: prefer exact, fall back to closest.
    best_rid: Optional[str] = None
    best_delta = float("inf")
    for rid, (stake, _ts) in entries.items():
        delta = abs(stake - target)
        if delta < best_delta:
            best_delta = delta
            best_rid = rid
            if delta == 0:
                break
    if best_rid is not None:
        entries.pop(best_rid, None)
        if not entries:
            _pending_stakes.pop(key, None)


def clear_state_for_tests() -> None:
    """Test hook — drop locks, cache, and pending reservations."""
    _cache.clear()
    _lock_registry.clear()
    _pending_stakes.clear()
