"""
/desk Phase 2 — CLOB order book + condition-id resolver proxy.

Spec: hub note #218 §3 (S-tier: CLOB Book 5-level, condition_id
resolver), feedback_no_local_polymarket memory.

Hard constraint: `*.polymarket.com` calls originate from the MONTREAL
engine box, not from AWS Hub. To keep the hub's Polymarket perimeter
clean, outbound fetches here are gated behind env flag
`HUB_ALLOW_CLOB_FETCH=true`. The flag is ONLY set on deployments that
are permitted to talk to Polymarket. On the default AWS Hub the flag is
absent — endpoints return 503 `{error: "clob_unavailable"}` and the FE
hides the ladder gracefully.

Endpoints (all JWT-protected):
  GET  /api/clob/book?condition_id=<id>&token_yes=<id>&token_no=<id>
      → 5 levels each side × YES + NO, plus spread/imbalance/implied_p_up.
      Server-side cache: 8s TTL per (condition_id) key.
      On timeout/upstream error → 503 with short body.

  GET  /api/windows/condition?window_epoch=<epoch>&asset=BTC
      → { condition_id, yes_token_id, no_token_id, slug, window_epoch }
      Resolves the current 5-minute BTC UP/DOWN market via Gamma events.
      Same HUB_ALLOW_CLOB_FETCH gate applies — Gamma is technically a
      public read-only catalogue but sits on the same polymarket.com
      boundary we keep server-side.

The 8s cache deliberately sits just above the FE polling cadence (10s)
so the upstream CLOB is hit at most once per unique condition-id per
10s tick from the dashboard, even with multiple FE clients.

Degraded shapes (explicit, not 500):
  - Flag off         → 503 {error: "clob_unavailable", reason: "feature_flag_off"}
  - Upstream timeout → 503 {error: "clob_unavailable", reason: "upstream_timeout"}
  - Upstream 4xx/5xx → 503 {error: "clob_unavailable", reason: "upstream_<code>"}
  - Empty book       → 200 with {yes: {bids:[], asks:[]}, ...} (caller decides)
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Optional

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query

from auth.jwt import TokenData
from auth.middleware import get_current_user

log = structlog.get_logger(__name__)

router = APIRouter(tags=["clob"])

# ── Config ──────────────────────────────────────────────────────────────────
#
# The feature flag is read at import-time AND per-request so tests can flip
# it at runtime via monkeypatch without reloading the module.

CLOB_HOST = os.environ.get("CLOB_HOST", "https://clob.polymarket.com")
GAMMA_HOST = os.environ.get("GAMMA_HOST", "https://gamma-api.polymarket.com")

# Timeouts tuned for a single transatlantic round-trip. CLOB is reliably
# fast (<300 ms from Montreal); anything over 4s is a real failure.
_CLOB_TIMEOUT_S = 4.0
_GAMMA_TIMEOUT_S = 6.0

# Cache TTL — 8s lets the 10s FE poll land on mostly-fresh data while
# absorbing a burst of concurrent clients.
_BOOK_CACHE_TTL_S = 8.0
_CONDITION_CACHE_TTL_S = 30.0  # condition_id for a window doesn't change


def _allow_clob_fetch() -> bool:
    """True if this hub deployment is permitted to hit polymarket.com."""
    return os.environ.get("HUB_ALLOW_CLOB_FETCH", "").lower() in ("1", "true", "yes")


# ── In-memory TTL cache ─────────────────────────────────────────────────────
#
# Plain dict keyed by a tuple — good enough for a few hundred QPS from the
# dashboard. Not shared across hub workers but the FE cadence is low enough
# that per-worker caches still hit >70% of the time.

_book_cache: dict[str, tuple[float, dict]] = {}
_condition_cache: dict[tuple[int, str], tuple[float, dict]] = {}
_cache_lock = asyncio.Lock()


async def _cache_get(store: dict, key: Any, ttl_s: float) -> Optional[dict]:
    async with _cache_lock:
        entry = store.get(key)
        if not entry:
            return None
        ts, value = entry
        if (time.monotonic() - ts) > ttl_s:
            store.pop(key, None)
            return None
        return value


async def _cache_set(store: dict, key: Any, value: dict) -> None:
    async with _cache_lock:
        store[key] = (time.monotonic(), value)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _503(reason: str) -> HTTPException:
    """Standardised graceful-unavailable shape — FE hides on this."""
    return HTTPException(
        status_code=503,
        detail={"error": "clob_unavailable", "reason": reason},
    )


def _parse_level(raw: Any) -> Optional[dict]:
    """
    Normalise one CLOB book level into `{price: float, size: float}`.

    CLOB returns levels as either {"price":"0.54","size":"60"} (strings) or
    [price, size] pairs depending on the endpoint version. Handle both.
    """
    try:
        if isinstance(raw, dict):
            price = float(raw.get("price"))
            size = float(raw.get("size"))
        elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
            price = float(raw[0])
            size = float(raw[1])
        else:
            return None
        if not (0.0 <= price <= 1.0) or size < 0:
            return None
        return {"price": price, "size": size}
    except (TypeError, ValueError):
        return None


def _reduce_side(levels: list, *, descending: bool, keep: int = 5) -> list[dict]:
    """
    Keep top `keep` levels sorted by price. Bids are best-first (high → low),
    asks are best-first (low → high).
    """
    parsed = [p for p in (_parse_level(r) for r in levels or []) if p is not None]
    parsed.sort(key=lambda x: x["price"], reverse=descending)
    return parsed[:keep]


def _mid(bids: list[dict], asks: list[dict]) -> Optional[float]:
    if not bids or not asks:
        return None
    return (bids[0]["price"] + asks[0]["price"]) / 2.0


def _compute_metrics(yes: dict, no: dict) -> dict:
    """
    Derive spread (YES), imbalance (YES, top-of-book), implied_p_up (YES mid).
    """
    y_bid = yes["bids"][0]["price"] if yes["bids"] else None
    y_ask = yes["asks"][0]["price"] if yes["asks"] else None

    spread = (y_ask - y_bid) if (y_bid is not None and y_ask is not None) else None

    # Top-of-book depth imbalance: (bid_size - ask_size) / (bid + ask).
    bid_sz = yes["bids"][0]["size"] if yes["bids"] else 0.0
    ask_sz = yes["asks"][0]["size"] if yes["asks"] else 0.0
    tot = bid_sz + ask_sz
    imbalance = ((bid_sz - ask_sz) / tot) if tot > 0 else None

    implied_p_up = _mid(yes["bids"], yes["asks"])

    return {
        "spread": spread,
        "imbalance": imbalance,
        "implied_p_up": implied_p_up,
    }


async def _fetch_clob_book(client: httpx.AsyncClient, token_id: str) -> dict:
    """
    GET /book?token_id=... — CLOB returns {"bids": [...], "asks": [...]}.
    Raises on any non-2xx or timeout; caller wraps into 503.
    """
    resp = await client.get(
        f"{CLOB_HOST}/book",
        params={"token_id": token_id},
        timeout=_CLOB_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()


# ── Endpoints ───────────────────────────────────────────────────────────────

@router.get("/clob/book")
async def clob_book(
    condition_id: str = Query(..., min_length=4, max_length=128),
    token_yes: str = Query(..., min_length=4, max_length=128),
    token_no: str = Query(..., min_length=4, max_length=128),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    CLOB top-5 book for YES and NO on one market.

    Shape per hub note #218 §3:
      { condition_id, yes: {bids, asks}, no: {bids, asks},
        spread, imbalance, implied_p_up, cached_at }
    """
    if not _allow_clob_fetch():
        raise _503("feature_flag_off")

    cache_key = f"{condition_id}:{token_yes}:{token_no}"
    cached = await _cache_get(_book_cache, cache_key, _BOOK_CACHE_TTL_S)
    if cached is not None:
        return cached

    try:
        async with httpx.AsyncClient() as client:
            yes_raw, no_raw = await asyncio.gather(
                _fetch_clob_book(client, token_yes),
                _fetch_clob_book(client, token_no),
            )
    except httpx.TimeoutException:
        log.warning("clob.book.timeout", condition_id=condition_id)
        raise _503("upstream_timeout")
    except httpx.HTTPStatusError as exc:
        log.warning(
            "clob.book.upstream_status",
            condition_id=condition_id,
            status=exc.response.status_code,
        )
        raise _503(f"upstream_{exc.response.status_code}")
    except httpx.RequestError as exc:
        log.warning(
            "clob.book.network_err", condition_id=condition_id, err=str(exc)[:200]
        )
        raise _503("upstream_network_error")

    yes = {
        "bids": _reduce_side(yes_raw.get("bids") or [], descending=True),
        "asks": _reduce_side(yes_raw.get("asks") or [], descending=False),
    }
    no = {
        "bids": _reduce_side(no_raw.get("bids") or [], descending=True),
        "asks": _reduce_side(no_raw.get("asks") or [], descending=False),
    }

    metrics = _compute_metrics(yes, no)

    payload = {
        "condition_id": condition_id,
        "yes": yes,
        "no": no,
        **metrics,
        "cached_at": time.time(),
    }
    await _cache_set(_book_cache, cache_key, payload)
    return payload


@router.get("/windows/condition")
async def window_condition(
    window_epoch: int = Query(..., ge=0),
    asset: str = Query("BTC", max_length=16),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Resolve a 5-minute window to its Polymarket condition_id via Gamma.

    Polymarket issues a distinct market per window (slug
    `{asset}-updown-5m-{epoch}`). We fetch the event, return the first
    market's condition_id + token ids, cached 30s (markets never flip).
    """
    if not _allow_clob_fetch():
        raise _503("feature_flag_off")

    cache_key = (window_epoch, asset.upper())
    cached = await _cache_get(_condition_cache, cache_key, _CONDITION_CACHE_TTL_S)
    if cached is not None:
        return cached

    # Common slug pattern matches existing v58_monitor._fetch_gamma_prices.
    slug = f"{asset.lower()}-updown-5m-{window_epoch}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GAMMA_HOST}/events",
                params={"slug": slug},
                timeout=_GAMMA_TIMEOUT_S,
            )
            resp.raise_for_status()
            events = resp.json()
    except httpx.TimeoutException:
        raise _503("gamma_timeout")
    except httpx.HTTPStatusError as exc:
        raise _503(f"gamma_{exc.response.status_code}")
    except httpx.RequestError as exc:
        log.warning("clob.condition.network_err", slug=slug, err=str(exc)[:200])
        raise _503("gamma_network_error")

    if not events:
        raise HTTPException(
            status_code=404,
            detail={"error": "market_not_found", "slug": slug},
        )

    event = events[0] if isinstance(events, list) else events
    markets = event.get("markets") or []
    if not markets:
        raise HTTPException(
            status_code=404,
            detail={"error": "market_not_found", "slug": slug, "reason": "empty_markets"},
        )

    market = markets[0]
    condition_id = market.get("conditionId") or market.get("condition_id")
    token_ids_raw = market.get("clobTokenIds") or market.get("clob_token_ids") or "[]"

    # clobTokenIds is a JSON-encoded string per Gamma quirks.
    if isinstance(token_ids_raw, str):
        import json as _json
        try:
            token_ids = _json.loads(token_ids_raw)
        except Exception:
            token_ids = []
    else:
        token_ids = token_ids_raw or []

    # Map outcomes → token id via the outcomes list (same ordering).
    outcomes_raw = market.get("outcomes") or "[]"
    if isinstance(outcomes_raw, str):
        import json as _json
        try:
            outcomes = _json.loads(outcomes_raw)
        except Exception:
            outcomes = []
    else:
        outcomes = outcomes_raw or []

    yes_token_id = None
    no_token_id = None
    for i, name in enumerate(outcomes):
        if i >= len(token_ids):
            break
        upper = str(name).upper()
        if "UP" in upper or "YES" in upper:
            yes_token_id = str(token_ids[i])
        elif "DOWN" in upper or "NO" in upper:
            no_token_id = str(token_ids[i])

    if not condition_id or not yes_token_id or not no_token_id:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "market_not_found",
                "slug": slug,
                "reason": "incomplete_mapping",
            },
        )

    payload = {
        "condition_id": condition_id,
        "yes_token_id": yes_token_id,
        "no_token_id": no_token_id,
        "slug": slug,
        "window_epoch": window_epoch,
        "asset": asset.upper(),
    }
    await _cache_set(_condition_cache, cache_key, payload)
    return payload
