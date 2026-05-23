"""Polymarket on-chain auto-redeem feed.

Wraps the data-api ``activity?type=REDEEM&user=<funder>`` endpoint and
returns parsed :class:`RedemptionEvent` records for consumption by
:class:`engine.use_cases.reconcile_redemptions.ReconcileRedemptionsUseCase`.

Auth
----

The activity endpoint is **public** (no API key required) — only the
funder address goes in the query string. We still pin the User-Agent to
``novakash-redeem-feed/1.0`` so server-side rate limits can identify the
client.

Failure modes
-------------

- Network error / non-200 / unparseable JSON ⇒ return ``[]`` and log a
  warning. The reconciler treats an empty list as "nothing to do this
  pass" — strictly safer than fail-closed because the alternative
  (block stamping) would just keep the trades stuck NULL.
- Malformed individual row ⇒ skipped with a log warning, other rows
  proceed.

Latency budget
--------------

Observed from Montreal: 200-600 ms typical, p99 ~1.5 s. Total wait
3.0 s with a sane 5 s in-process cache (matches the on-chain cap
caching pattern in ``engine/use_cases/onchain_position_cap.py``).
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from typing import Optional

import structlog

from domain.value_objects import RedemptionEvent

log = structlog.get_logger(__name__)


_ACTIVITY_URL = "https://data-api.polymarket.com/activity"
_HTTP_TIMEOUT_S = 3.0
_DEFAULT_LIMIT = 200  # data-api default page; max 500 per call
_CACHE_TTL_S = 30.0  # reconciler runs every 2 min so 30 s amortises duplicate calls cheaply

# Module-level cache: {(funder, limit): (timestamp, events)}
_cache: dict[tuple[str, int], tuple[float, list[RedemptionEvent]]] = {}


class PolymarketRedemptionFeed:
    """Async wrapper over the data-api activity endpoint, REDEEM-filtered.

    Stateless — safe to instantiate per call. The internal cache is
    process-wide and keyed on funder address so multiple instances do
    not double-fetch.
    """

    def __init__(
        self,
        funder_address: str,
        *,
        http_timeout_s: float = _HTTP_TIMEOUT_S,
        cache_ttl_s: float = _CACHE_TTL_S,
    ) -> None:
        if not funder_address:
            raise ValueError("funder_address is required")
        self._funder = funder_address
        self._timeout = http_timeout_s
        self._cache_ttl = cache_ttl_s

    async def fetch_recent(
        self,
        *,
        limit: int = _DEFAULT_LIMIT,
        use_cache: bool = True,
    ) -> list[RedemptionEvent]:
        """Return the most recent REDEEM events for the funder.

        ``limit`` caps the page size (data-api hard max 500). The
        endpoint returns newest-first, which matches the typical
        reconciler use case (process the recent window first, then
        catch up).
        """
        key = (self._funder.lower(), limit)
        now = time.time()
        if use_cache:
            cached = _cache.get(key)
            if cached is not None and (now - cached[0]) < self._cache_ttl:
                return cached[1]

        raw = await self._fetch_async(limit=limit)
        events = self._parse_rows(raw)
        _cache[key] = (now, events)
        return events

    async def fetch_since(
        self,
        *,
        since_timestamp: int,
        max_pages: int = 10,
        page_size: int = 500,
    ) -> list[RedemptionEvent]:
        """Paginate backwards through REDEEMs until ``since_timestamp``.

        Used by the backfill script. Walks pages of ``page_size``
        results (data-api default 500) until either:
          - the oldest row on the current page is older than
            ``since_timestamp``, or
          - ``max_pages`` has been reached (safety bound — at 500
            rows/page that's 5 000 redemptions, ~weeks of trading).

        Returns events in newest-first order.
        """
        collected: list[RedemptionEvent] = []
        offset = 0
        for _ in range(max_pages):
            raw = await self._fetch_async(limit=page_size, offset=offset)
            page = self._parse_rows(raw)
            if not page:
                break
            collected.extend(page)
            oldest = min(e.timestamp for e in page)
            if oldest < since_timestamp:
                break
            offset += page_size
        # De-dup by tx + filter by timestamp
        seen: set[str] = set()
        out: list[RedemptionEvent] = []
        for e in collected:
            if e.transaction_hash in seen:
                continue
            seen.add(e.transaction_hash)
            if e.timestamp >= since_timestamp:
                out.append(e)
        return out

    # ── internal ──────────────────────────────────────────────────────

    async def _fetch_async(
        self, *, limit: int, offset: int = 0
    ) -> list[dict]:
        loop = asyncio.get_event_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(
                    None, self._fetch_sync, limit, offset
                ),
                timeout=self._timeout + 0.5,
            )
        except (asyncio.TimeoutError, urllib.error.URLError, OSError) as exc:
            log.warning(
                "redeem_feed.fetch_timeout_or_neterr",
                funder=self._funder[:10],
                limit=limit,
                offset=offset,
                err=str(exc)[:120],
            )
            return []
        except Exception as exc:
            log.warning(
                "redeem_feed.fetch_error",
                funder=self._funder[:10],
                limit=limit,
                offset=offset,
                err=str(exc)[:120],
            )
            return []

    def _fetch_sync(self, limit: int, offset: int) -> list[dict]:
        url = (
            f"{_ACTIVITY_URL}?user={self._funder}&type=REDEEM"
            f"&limit={limit}&offset={offset}"
        )
        req = urllib.request.Request(
            url, headers={"User-Agent": "novakash-redeem-feed/1.0"}
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            data = json.loads(resp.read())
            if not isinstance(data, list):
                return []
            return data

    def _parse_rows(self, rows: list[dict]) -> list[RedemptionEvent]:
        out: list[RedemptionEvent] = []
        for row in rows:
            try:
                tx = (row.get("transactionHash") or "").strip()
                if not tx:
                    continue
                slug = (row.get("slug") or row.get("eventSlug") or "").strip()
                cid = (row.get("conditionId") or "").strip()
                if not slug or not cid:
                    continue
                ts = int(row.get("timestamp") or 0)
                usdc_size = float(row.get("usdcSize") or 0)
                size = float(row.get("size") or 0)
                out.append(
                    RedemptionEvent(
                        transaction_hash=tx,
                        condition_id=cid,
                        market_slug=slug,
                        usdc_size=usdc_size,
                        size=size,
                        timestamp=ts,
                        funder_address=self._funder,
                    )
                )
            except (TypeError, ValueError) as exc:
                log.debug(
                    "redeem_feed.row_parse_failed",
                    err=str(exc)[:120],
                    raw_keys=list(row.keys())[:10],
                )
                continue
        return out


def clear_cache() -> None:
    """Test hook — drop the module-level cache so each call hits the API."""
    _cache.clear()
