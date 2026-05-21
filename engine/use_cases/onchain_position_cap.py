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

# {(funder, condition_id): (timestamp, total_cost_usdc_for_outcome_yes, total_cost_usdc_for_outcome_no)}
_cache: dict[tuple[str, str], tuple[float, float, float]] = {}


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


def _fetch_positions_sync(funder: str, condition_id: str) -> list[dict]:
    """Single HTTP GET to data-api positions. Sync because urllib is sync;
    we'll wrap with run_in_executor at the call site for asyncio."""
    url = f"{_POSITIONS_URL}?user={funder}&market={condition_id}&sizeThreshold=0.001"
    req = urllib.request.Request(
        url, headers={"User-Agent": "novakash-onchain-cap/1.0"}
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read())


async def get_onchain_cost_for_window(
    *,
    funder_address: str,
    condition_id: str,
    outcome: str,
) -> Optional[float]:
    """Return current on-chain cost (USDC) the funder has spent BUYING
    ``outcome`` ("Up" or "Down") on the market identified by
    ``condition_id``. Cached for 5s per (funder, condition_id) to dedupe
    parallel fires.

    Returns None on any error — caller fails-closed (block the trade).
    """
    if not funder_address or not condition_id:
        return None
    if outcome not in ("Up", "Down", "YES", "NO"):
        return None
    # Normalise our internal direction to Polymarket outcome label
    pm_outcome = "Up" if outcome in ("Up", "YES") else "Down"

    key = (funder_address.lower(), condition_id.lower())
    now = time.time()
    cached = _cache.get(key)
    if cached is not None and (now - cached[0]) < _CACHE_TTL_S:
        return cached[1] if pm_outcome == "Up" else cached[2]

    loop = asyncio.get_event_loop()
    try:
        positions = await asyncio.wait_for(
            loop.run_in_executor(
                None, _fetch_positions_sync, funder_address, condition_id
            ),
            timeout=_HTTP_TIMEOUT_S + 0.2,
        )
    except (asyncio.TimeoutError, urllib.error.URLError, OSError) as exc:
        log.warning(
            "onchain_position_cap.fetch_timeout_or_neterr",
            funder=funder_address[:10],
            condition_id=condition_id[:10],
            err=str(exc)[:100],
        )
        return None
    except Exception as exc:
        log.warning(
            "onchain_position_cap.fetch_error",
            funder=funder_address[:10],
            condition_id=condition_id[:10],
            err=str(exc)[:100],
        )
        return None

    up_cost = 0.0
    dn_cost = 0.0
    for p in positions:
        try:
            cv_outcome = p.get("outcome", "")
            cost = float(p.get("initialValue") or 0)
            if cv_outcome == "Up":
                up_cost += cost
            elif cv_outcome == "Down":
                dn_cost += cost
        except (TypeError, ValueError):
            continue

    _cache[key] = (now, up_cost, dn_cost)
    return up_cost if pm_outcome == "Up" else dn_cost


async def check_onchain_window_cap(
    *,
    funder_address: str,
    condition_id: str,
    direction: str,
    new_stake_usd: float,
    cap_usd: float,
) -> Optional[str]:
    """Return a skip_reason string if the proposed stake would breach the
    on-chain per-(asset, window) cap, else None.

    Fail-closed: returns ``"exposure_cap_window_pm_unavailable"`` if the
    data-api call errored. Better to skip ONE legitimate trade than
    permit another $69 multi-fill loss.
    """
    if cap_usd <= 0:
        return None
    if not funder_address or not condition_id:
        return None

    on_chain_cost = await get_onchain_cost_for_window(
        funder_address=funder_address,
        condition_id=condition_id,
        outcome=direction,
    )
    if on_chain_cost is None:
        log.warning(
            "onchain_position_cap.fail_closed_block",
            condition_id=condition_id[:10],
            direction=direction,
            cap_usd=cap_usd,
        )
        return "exposure_cap_window_pm_unavailable"

    projected = on_chain_cost + new_stake_usd
    if projected > cap_usd:
        log.warning(
            "onchain_position_cap.blocked",
            condition_id=condition_id[:10],
            direction=direction,
            on_chain_cost=round(on_chain_cost, 4),
            new_stake=round(new_stake_usd, 4),
            projected=round(projected, 4),
            cap_usd=cap_usd,
        )
        return "exposure_cap_window_pm"

    return None
