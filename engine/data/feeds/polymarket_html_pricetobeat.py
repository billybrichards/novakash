"""
Polymarket HTML priceToBeat Feed — anonymous HTML scrape.

Replaces ``polymarket_rtds_chainlink.PolymarketRTDSFeed`` (which connected +
subscribed but received zero messages and timed out every 30s in an endless
reconnect loop). The HTML approach is simpler, more robust, and gives us the
EXACT priceToBeat Polymarket displays in their UI.

How it works
------------
Polymarket's per-event pages embed a Next.js ``__NEXT_DATA__`` JSON blob that
contains the canonical ``eventMetadata.priceToBeat`` for the window. The blob
is server-rendered, so a single anonymous HTTP GET retrieves it without needing
JS execution.

URL pattern: ``https://polymarket.com/event/<asset>-updown-<tf>-<window_ts>``
e.g. ``polymarket.com/event/btc-updown-5m-1777824300``.

Inside the page::

    <script id="__NEXT_DATA__" type="application/json">{ ...
        "props":{"pageProps":{"dehydratedState":{"queries":[
            ...,
            { "queryKey": ["/api/event/slug", "btc-updown-5m-1777824300"],
              "state": { "data": { ...,
                "eventMetadata": { "priceToBeat": 78675.76203619942, ... }
              }}}, ...
        ]}}}
    }</script>

We extract the JSON, locate the query whose ``queryKey[0] == '/api/event/slug'``
and ``queryKey[1] == <slug>``, and read ``state.data.eventMetadata.priceToBeat``.

Public surface
--------------
* ``await get_price_to_beat(asset, timeframe, window_ts) -> Optional[float]``
* ``await get_window_open_price(asset, window_ts) -> Optional[float]``
  (compat shim — defaults to 5m, matches old RTDS feed signature)
* ``await start()`` / ``await stop()`` — no-ops (kept for runtime symmetry)

Defensive design
----------------
* Single-flight per (asset, tf, window_ts) — concurrent callers share the
  in-flight HTTP GET.
* Permanent cache once captured — priceToBeat doesn't change after window open.
* Negative cache for transient None results, with a short TTL so we retry
  windows whose page hasn't been published yet.
* Any exception → logged at WARN, returns None — never crashes engine.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Optional

import httpx
import structlog

log = structlog.get_logger(__name__)

# Default User-Agent — Polymarket serves SPA HTML to common browsers without
# bot challenges. Pin to a recent stable Firefox UA.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:120.0) "
    "Gecko/20100101 Firefox/120.0"
)

# Per-request HTTP timeout. The page is ~2 MB; 8 s is generous on a healthy link.
DEFAULT_FETCH_TIMEOUT_SECS = 8.0

# How long a "None" result stays cached before we retry (e.g. for a window whose
# page is published a few seconds late). Successful results cache permanently.
NEGATIVE_CACHE_TTL_SECS = 5.0

# Cap memory by retaining at most this many positive results (FIFO eviction).
MAX_POSITIVE_CACHE_ENTRIES = 256

# Pre-compiled __NEXT_DATA__ extractor.
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
    re.DOTALL,
)


class PolymarketHTMLPriceToBeatFeed:
    """Fetches Polymarket's canonical ``priceToBeat`` by scraping the public
    event HTML page.

    Polymarket displays priceToBeat in their UI as soon as a 5m / 15m window
    opens — typically within 1-3 s of the window boundary. The value is
    server-rendered into ``__NEXT_DATA__``, so a single HTTP GET retrieves it
    without needing JS execution.

    Usage::

        feed = PolymarketHTMLPriceToBeatFeed()
        ptb = await feed.get_price_to_beat("BTC", "5m", 1777824300)
        # → 78675.76203619942 (or None if not yet available)
    """

    def __init__(
        self,
        http_client: Optional[httpx.AsyncClient] = None,
        user_agent: str = DEFAULT_USER_AGENT,
        fetch_timeout_secs: float = DEFAULT_FETCH_TIMEOUT_SECS,
    ) -> None:
        """
        Args:
            http_client: Optional shared ``httpx.AsyncClient``. If None, the
                feed lazily creates and owns its own client (closed on stop).
            user_agent: UA header sent with every request.
            fetch_timeout_secs: Per-request total timeout.
        """
        self._http: Optional[httpx.AsyncClient] = http_client
        self._owns_http = http_client is None
        self._user_agent = user_agent
        self._fetch_timeout = fetch_timeout_secs

        # Permanent cache: (asset, tf, window_ts) -> priceToBeat
        self._cache: dict[tuple[str, str, int], float] = {}
        # FIFO of cache keys for bounded eviction.
        self._cache_order: list[tuple[str, str, int]] = []

        # Negative cache: key -> monotonic-time when the None expires.
        self._neg_cache: dict[tuple[str, str, int], float] = {}

        # Single-flight: key -> in-flight Future.
        self._inflight: dict[tuple[str, str, int], asyncio.Future] = {}

        self._log = log.bind(component="PolymarketHTMLPriceToBeatFeed")
        self._started = False

    # ─── Lifecycle (no-ops; kept for symmetry with RTDS feed) ────────────────

    async def start(self) -> None:
        """No-op start. Kept so runtime code that does ``rtds_feed.start()``
        still works after the swap-in. The first ``get_price_to_beat`` call
        lazily creates the http client if we don't have one."""
        self._started = True
        self._log.info("polymarket_html.started")
        # Block forever so the runtime task wrapper stays alive — without this
        # the wrapper task would complete immediately and could be flagged as
        # dead by health checks. We sleep on a never-resolving event.
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self._log.info("polymarket_html.start_cancelled")
            raise

    async def stop(self) -> None:
        """Close the owned http client (if any)."""
        self._started = False
        if self._owns_http and self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None
        self._log.info("polymarket_html.stopped")

    # ─── Public API ──────────────────────────────────────────────────────────

    async def get_price_to_beat(
        self,
        asset: str,
        timeframe: str,
        window_ts: int,
    ) -> Optional[float]:
        """Return Polymarket's canonical ``priceToBeat`` for the window, or
        ``None`` if not yet available.

        Args:
            asset: Engine asset code, e.g. ``"BTC"``.
            timeframe: ``"5m"`` or ``"15m"``.
            window_ts: Window-aligned epoch seconds (5m or 15m boundary).

        Behaviour:
            * Permanent cache hit → returned immediately.
            * Negative-cache hit (TTL active) → ``None`` immediately, no HTTP.
            * Otherwise: single-flight HTTP GET. Concurrent callers for the
              same key share the result.
        """
        asset_u = asset.upper()
        tf = timeframe.lower()
        try:
            window_ts = int(window_ts)
        except (TypeError, ValueError):
            return None

        key = (asset_u, tf, window_ts)

        # Permanent cache.
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        # Negative cache.
        neg_until = self._neg_cache.get(key)
        if neg_until is not None and time.monotonic() < neg_until:
            return None

        # Single-flight.
        existing = self._inflight.get(key)
        if existing is not None:
            try:
                return await existing
            except Exception:
                return None

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._inflight[key] = fut
        try:
            ptb = await self._fetch_price_to_beat(asset_u, tf, window_ts)
        except Exception as exc:  # noqa: BLE001 — paranoid; should not happen
            self._log.warning(
                "polymarket_html.fetch_unexpected_error",
                asset=asset_u, timeframe=tf, window_ts=window_ts,
                error=str(exc)[:200],
            )
            ptb = None
        finally:
            # Wake any waiters before clearing the inflight slot.
            if not fut.done():
                fut.set_result(ptb)
            self._inflight.pop(key, None)

        if ptb is not None:
            self._store_positive(key, ptb)
        else:
            self._neg_cache[key] = time.monotonic() + NEGATIVE_CACHE_TTL_SECS
        return ptb

    async def get_window_open_price(
        self, asset: str, window_ts: int, timeframe: str = "5m"
    ) -> Optional[float]:
        """Compatibility shim matching the old RTDS feed signature (sync there,
        async here). Defaults timeframe to ``5m``."""
        return await self.get_price_to_beat(asset, timeframe, window_ts)

    # ─── Internals ───────────────────────────────────────────────────────────

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self._fetch_timeout),
                follow_redirects=True,
                headers={"User-Agent": self._user_agent},
            )
            self._owns_http = True
        return self._http

    async def _fetch_price_to_beat(
        self,
        asset: str,
        timeframe: str,
        window_ts: int,
    ) -> Optional[float]:
        """Anonymous HTTP GET → parse priceToBeat from ``__NEXT_DATA__``.

        Returns ``None`` (never raises) on any failure.
        """
        slug = f"{asset.lower()}-updown-{timeframe}-{window_ts}"
        url = f"https://polymarket.com/event/{slug}"
        try:
            client = await self._ensure_client()
            resp = await client.get(
                url,
                headers={"User-Agent": self._user_agent},
                timeout=self._fetch_timeout,
            )
            if resp.status_code != 200:
                self._log.debug(
                    "polymarket_html.non_200",
                    asset=asset, timeframe=timeframe, window_ts=window_ts,
                    status=resp.status_code,
                )
                return None
            html = resp.text
        except Exception as exc:  # noqa: BLE001 — defensive
            self._log.warning(
                "polymarket_html.fetch_failed",
                asset=asset, timeframe=timeframe, window_ts=window_ts,
                url=url, error=str(exc)[:200],
            )
            return None

        ptb = self._parse_price_to_beat_from_html(html, slug=slug)
        if ptb is None:
            self._log.debug(
                "polymarket_html.priceToBeat_not_in_html",
                asset=asset, timeframe=timeframe, window_ts=window_ts, html_size=len(html),
            )
            return None

        self._log.info(
            "polymarket_html.priceToBeat_fetched",
            asset=asset,
            timeframe=timeframe,
            window_ts=window_ts,
            price_to_beat=ptb,
            slug=slug,
        )
        return ptb

    @staticmethod
    def _parse_price_to_beat_from_html(html: str, slug: str) -> Optional[float]:
        """Extract ``__NEXT_DATA__`` and locate the event query for ``slug``.

        Returns the float ``priceToBeat`` or ``None`` if not present.

        We deliberately scope the lookup to the query whose
        ``queryKey == ['/api/event/slug', '<slug>']`` so that we don't
        accidentally pick up a sibling event's priceToBeat (the page also
        embeds related upcoming/past events under ``/api/series``).
        """
        if not html:
            return None
        m = _NEXT_DATA_RE.search(html)
        if not m:
            return None
        try:
            data = json.loads(m.group(1))
        except (TypeError, ValueError):
            return None

        try:
            queries = (
                data["props"]["pageProps"]["dehydratedState"]["queries"]
            )
        except (KeyError, TypeError):
            return None
        if not isinstance(queries, list):
            return None

        for q in queries:
            if not isinstance(q, dict):
                continue
            qk = q.get("queryKey")
            if not isinstance(qk, list) or len(qk) < 2:
                continue
            if qk[0] != "/api/event/slug" or qk[1] != slug:
                continue
            try:
                em = q["state"]["data"]["eventMetadata"]
            except (KeyError, TypeError):
                return None
            ptb = em.get("priceToBeat") if isinstance(em, dict) else None
            if ptb is None:
                return None
            try:
                f = float(ptb)
            except (TypeError, ValueError):
                return None
            return f if f > 0 else None
        return None

    def _store_positive(self, key: tuple[str, str, int], value: float) -> None:
        """Insert into permanent cache with bounded FIFO eviction."""
        if key in self._cache:
            return
        self._cache[key] = value
        self._cache_order.append(key)
        # Drop negative-cache entry if any.
        self._neg_cache.pop(key, None)
        if len(self._cache_order) > MAX_POSITIVE_CACHE_ENTRIES:
            old = self._cache_order.pop(0)
            self._cache.pop(old, None)
