"""
Polymarket HTML Resolution Feed — canonical post-resolve outcome via HTML scrape.

Companion to ``polymarket_html_pricetobeat.py``. After a 5m / 15m window
resolves, the same Polymarket event page (``polymarket.com/event/<asset>-updown-
<tf>-<window_ts>``) updates ``__NEXT_DATA__`` so that:

  * ``eventMetadata.priceToBeat`` holds the canonical priceToBeat, AND
  * ``past-results`` includes the resolved window itself with both ``openPrice``
    and ``closePrice`` populated.

Comparing ``priceToBeat`` (the threshold) to the window's ``closePrice`` (the
final Chainlink-sampled price at resolution) gives the canonical UP/DOWN that
Polymarket's UI displays as the resolved outcome — the same source ``scripts/
ops/wallet_truth.py`` cross-checks against on-chain redemptions.

Why this is canonical
---------------------
* It's exactly what the public Polymarket UI shows for the resolved window.
* It uses the SAME Chainlink oracle sample that the on-chain CTF resolution
  used (Polymarket samples once per window boundary; that single price is
  closePrice for window N and priceToBeat for window N+1).
* Independent of data-api lag (curPrice can be stale or ratchet through 0.5
  briefly during settlement).
* Independent of synthetic placeholders or window-open Binance prices.

Returns ``ResolvedWindow`` (UP/DOWN + priceToBeat + closePrice) or ``None`` if
the page hasn't been published yet, the window isn't resolved, or any HTTP
failure. NEVER raises.

Defensive design mirrors the priceToBeat feed:
  * Single-flight per (asset, tf, window_ts).
  * Permanent cache of positive results — resolution is monotonic.
  * Short-TTL negative cache so we retry windows whose page hasn't published.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
import structlog

log = structlog.get_logger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:120.0) "
    "Gecko/20100101 Firefox/120.0"
)
DEFAULT_FETCH_TIMEOUT_SECS = 8.0
NEGATIVE_CACHE_TTL_SECS = 5.0
MAX_POSITIVE_CACHE_ENTRIES = 256

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
    re.DOTALL,
)


@dataclass(frozen=True)
class ResolvedWindow:
    """Canonical resolved-window record from Polymarket HTML.

    Attributes:
        outcome: Either ``"UP"`` (close > priceToBeat) or ``"DOWN"`` (close <=
            priceToBeat). Polymarket itself treats equality as the YES/UP side
            losing (no strict gain → DOWN) — this matches the on-chain oracle
            convention.
        price_to_beat: The threshold Polymarket displayed for the window (= the
            previous window's closePrice).
        close_price: The window's final Chainlink-sampled price (= the
            ``closePrice`` of the window's own past-results entry).
    """

    outcome: str
    price_to_beat: float
    close_price: float


def _epoch_to_iso_z(epoch_seconds: int) -> Optional[str]:
    try:
        dt = datetime.fromtimestamp(int(epoch_seconds), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class PolymarketHTMLResolutionFetcher:
    """Fetches the canonical resolved-window outcome by scraping the same
    Polymarket event page used by ``PolymarketHTMLPriceToBeatFeed``.

    Usage::

        fetcher = PolymarketHTMLResolutionFetcher()
        rw = await fetcher.fetch_resolution("BTC", "5m", 1777824300)
        if rw is not None:
            print(rw.outcome, rw.price_to_beat, rw.close_price)
    """

    def __init__(
        self,
        http_client: Optional[httpx.AsyncClient] = None,
        user_agent: str = DEFAULT_USER_AGENT,
        fetch_timeout_secs: float = DEFAULT_FETCH_TIMEOUT_SECS,
    ) -> None:
        self._http: Optional[httpx.AsyncClient] = http_client
        self._owns_http = http_client is None
        self._user_agent = user_agent
        self._fetch_timeout = fetch_timeout_secs

        self._cache: dict[tuple[str, str, int], ResolvedWindow] = {}
        self._cache_order: list[tuple[str, str, int]] = []
        self._neg_cache: dict[tuple[str, str, int], float] = {}
        self._inflight: dict[tuple[str, str, int], asyncio.Future] = {}

        self._log = log.bind(component="PolymarketHTMLResolutionFetcher")

    # ─── Public API ──────────────────────────────────────────────────────────

    async def fetch_resolution(
        self,
        asset: str,
        timeframe: str,
        window_ts: int,
    ) -> Optional[ResolvedWindow]:
        """Return the canonical UP/DOWN outcome for a resolved window, or None.

        Args:
            asset: ``"BTC"``, ``"ETH"``, etc.
            timeframe: ``"5m"`` or ``"15m"``.
            window_ts: Window-aligned epoch seconds.

        Returns:
            ``ResolvedWindow`` with outcome + both source prices, or ``None``
            if the window hasn't resolved yet / the page isn't published / any
            transient HTTP / parse failure.
        """
        asset_u = asset.upper()
        tf = timeframe.lower()
        try:
            window_ts = int(window_ts)
        except (TypeError, ValueError):
            return None

        key = (asset_u, tf, window_ts)

        cached = self._cache.get(key)
        if cached is not None:
            return cached

        neg_until = self._neg_cache.get(key)
        if neg_until is not None and time.monotonic() < neg_until:
            return None

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
            rw = await self._fetch_resolution(asset_u, tf, window_ts)
        except Exception as exc:  # noqa: BLE001 — paranoid; should not happen
            self._log.warning(
                "polymarket_html_resolution.unexpected_error",
                asset=asset_u, timeframe=tf, window_ts=window_ts,
                error=str(exc)[:200],
            )
            rw = None
        finally:
            if not fut.done():
                fut.set_result(rw)
            self._inflight.pop(key, None)

        if rw is not None:
            self._store_positive(key, rw)
        else:
            self._neg_cache[key] = time.monotonic() + NEGATIVE_CACHE_TTL_SECS
        return rw

    async def stop(self) -> None:
        """Close the owned http client (if any). Safe to call multiple times."""
        if self._owns_http and self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None

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

    async def _fetch_resolution(
        self,
        asset: str,
        timeframe: str,
        window_ts: int,
    ) -> Optional[ResolvedWindow]:
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
                    "polymarket_html_resolution.non_200",
                    asset=asset, timeframe=timeframe, window_ts=window_ts,
                    status=resp.status_code,
                )
                return None
            html = resp.text
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "polymarket_html_resolution.fetch_failed",
                asset=asset, timeframe=timeframe, window_ts=window_ts,
                url=url, error=str(exc)[:200],
            )
            return None

        rw = self._parse_resolution_from_html(
            html, slug=slug, window_ts=window_ts
        )
        if rw is None:
            self._log.debug(
                "polymarket_html_resolution.not_in_html",
                asset=asset, timeframe=timeframe, window_ts=window_ts,
                html_size=len(html),
            )
            return None

        self._log.info(
            "polymarket_html_resolution.fetched",
            asset=asset,
            timeframe=timeframe,
            window_ts=window_ts,
            outcome=rw.outcome,
            price_to_beat=rw.price_to_beat,
            close_price=rw.close_price,
            slug=slug,
        )
        return rw

    @staticmethod
    def _parse_resolution_from_html(
        html: str,
        slug: str,
        window_ts: int,
    ) -> Optional[ResolvedWindow]:
        """Extract priceToBeat + this window's closePrice from ``__NEXT_DATA__``.

        Two parts:
          1. ``priceToBeat``: prefer ``eventMetadata.priceToBeat`` (resolved
             windows only); fall back to the past-results entry whose ``endTime``
             == window_ts (the previous window's closePrice).
          2. ``closePrice``: the past-results entry whose ``startTime`` ==
             window_ts (this window's own resolved entry).

        Returns ``ResolvedWindow`` or ``None``.
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
            queries = data["props"]["pageProps"]["dehydratedState"]["queries"]
        except (KeyError, TypeError):
            return None
        if not isinstance(queries, list):
            return None

        # ── priceToBeat lookup (eventMetadata first) ──────────────────────
        price_to_beat: Optional[float] = None
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
                v = em.get("priceToBeat")
                if v is not None:
                    try:
                        f = float(v)
                        if f > 0:
                            price_to_beat = f
                    except (TypeError, ValueError):
                        pass
            break

        # ── past-results: simultaneously locate (a) priceToBeat fallback
        #    via prev window's closePrice (endTime == window_ts) and (b) the
        #    target window's own closePrice (startTime == window_ts) ───────
        target_iso = _epoch_to_iso_z(window_ts)
        if target_iso is None:
            return None

        close_price: Optional[float] = None
        ptb_fallback: Optional[float] = None

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
                start = r.get("startTime")
                end = r.get("endTime")
                cp_raw = r.get("closePrice")
                try:
                    cp = float(cp_raw) if cp_raw is not None else None
                except (TypeError, ValueError):
                    cp = None
                if cp is None or cp <= 0:
                    continue
                # Target window's own entry: closePrice = window's final price.
                if start == target_iso and close_price is None:
                    close_price = cp
                # Previous window's entry: closePrice = our priceToBeat fallback.
                if end == target_iso and ptb_fallback is None:
                    ptb_fallback = cp

        if price_to_beat is None:
            price_to_beat = ptb_fallback
        if price_to_beat is None or close_price is None:
            return None

        outcome = "UP" if close_price > price_to_beat else "DOWN"
        return ResolvedWindow(
            outcome=outcome,
            price_to_beat=price_to_beat,
            close_price=close_price,
        )

    def _store_positive(
        self,
        key: tuple[str, str, int],
        value: ResolvedWindow,
    ) -> None:
        if key in self._cache:
            return
        self._cache[key] = value
        self._cache_order.append(key)
        self._neg_cache.pop(key, None)
        if len(self._cache_order) > MAX_POSITIVE_CACHE_ENTRIES:
            old = self._cache_order.pop(0)
            self._cache.pop(old, None)
