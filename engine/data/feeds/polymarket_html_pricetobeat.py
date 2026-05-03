"""
Polymarket HTML priceToBeat Feed — anonymous HTML scrape.

Replaces ``polymarket_rtds_chainlink.PolymarketRTDSFeed`` (which connected +
subscribed but received zero messages and timed out every 30s in an endless
reconnect loop). The HTML approach is simpler, more robust, and gives us the
EXACT priceToBeat Polymarket displays in their UI.

How it works
------------
Polymarket's per-event pages embed a Next.js ``__NEXT_DATA__`` JSON blob that
contains two sources for the canonical priceToBeat:

1. ``eventMetadata.priceToBeat`` — populated for RESOLVED windows only (older
   windows whose 5m period has already ended and Polymarket's backend has
   cached the metadata). For active or just-closed windows this is ``None``.

2. ``past-results.data.results[]`` — an array of recently-resolved windows
   under the queryKey ``['past-results', '<ASSET>', 'fiveminute' | 'fifteenminute',
   '<cutoff_iso>']``. Each entry has ``startTime``, ``endTime``, ``openPrice``,
   ``closePrice``. The entry whose ``endTime`` equals the target window's
   start has ``closePrice`` = priceToBeat for that target window (Polymarket
   samples the same Chainlink point for both window N's close and window N+1's
   priceToBeat).

   This is the source we need for ACTIVE windows: fetch the page for the
   ACTIVE window itself, and the page's past-results array includes the
   immediately-prior window whose closePrice is our priceToBeat.

URL pattern: ``https://polymarket.com/event/<asset>-updown-<tf>-<window_ts>``
e.g. ``polymarket.com/event/btc-updown-5m-1777824300``.

Lookup strategy (in order):

* Parse ``__NEXT_DATA__`` JSON.
* If the slug's ``eventMetadata.priceToBeat`` is set → return it.
* Else scan ``past-results`` queries on the same page; find the entry whose
  ``endTime`` ISO timestamp == ``window_ts`` epoch seconds; return its
  ``closePrice``.

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
from datetime import datetime, timezone
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


def _epoch_to_iso_z(epoch_seconds: int) -> Optional[str]:
    """Convert epoch seconds to Polymarket's ISO format ``YYYY-MM-DDTHH:MM:SS.000Z``.

    Polymarket past-results entries use this exact format with millisecond
    precision (always ``.000``) and a trailing ``Z`` for UTC.
    """
    try:
        dt = datetime.fromtimestamp(int(epoch_seconds), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None
    # Format: 2026-05-03T16:30:00.000Z
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


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

        ptb = self._parse_price_to_beat_from_html(html, slug=slug, window_ts=window_ts)
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
    def _parse_price_to_beat_from_html(
        html: str, slug: str, window_ts: Optional[int] = None
    ) -> Optional[float]:
        """Extract ``__NEXT_DATA__`` and locate the priceToBeat for ``slug``.

        Two-stage lookup:

        1. Find the query with ``queryKey == ['/api/event/slug', '<slug>']``
           and read ``state.data.eventMetadata.priceToBeat``. This is set
           for resolved windows only.
        2. Fallback: scan ``past-results`` queries on the same page; find
           the entry whose ``endTime`` ISO timestamp equals ``window_ts``
           epoch seconds; return its ``closePrice`` (Polymarket samples the
           same Chainlink point for window N close and window N+1 priceToBeat).

        Returns the float ``priceToBeat`` or ``None`` if not present.
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

        # ── Stage 1: eventMetadata.priceToBeat (resolved windows) ────────
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
                em = None
            if isinstance(em, dict):
                ptb = em.get("priceToBeat")
                if ptb is not None:
                    try:
                        f = float(ptb)
                        if f > 0:
                            return f
                    except (TypeError, ValueError):
                        pass
            # Found the slug entry but no usable priceToBeat — fall through to
            # past-results lookup below.
            break

        # ── Stage 2: past-results closePrice for prev window ─────────────
        if window_ts is None:
            return None
        target_iso = _epoch_to_iso_z(window_ts)
        if target_iso is None:
            return None
        for q in queries:
            if not isinstance(q, dict):
                continue
            qk = q.get("queryKey")
            if not isinstance(qk, list) or len(qk) < 1:
                continue
            if qk[0] != "past-results":
                continue
            try:
                results = q["state"]["data"]["data"]["results"]
            except (KeyError, TypeError):
                continue
            if not isinstance(results, list):
                continue
            for r in results:
                if not isinstance(r, dict):
                    continue
                # endTime of window W-1 == startTime of window W == target.
                if r.get("endTime") != target_iso:
                    continue
                cp = r.get("closePrice")
                try:
                    f = float(cp) if cp is not None else None
                except (TypeError, ValueError):
                    f = None
                if f is not None and f > 0:
                    return f
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
