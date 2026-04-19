"""
30-minute activity digest for the Polymarket proxy wallet.

Queries Polymarket's data-api for the last N activity events and
classifies them into TRADE BUY / TRADE SELL / REDEEM (win) / REDEEM
(loser dust). Consumed by ``alerts.positions.render_snapshot_text``
to add a "what just happened" block to POSITION SNAPSHOT messages.

Data-api notes (verified live 2026-04-18):
  - Endpoint: https://data-api.polymarket.com/activity
  - REQUIRES ``User-Agent`` header — returns 403 otherwise.
  - ``?user=<proxy>&limit=500`` returns the proxy's most-recent
    events in reverse-chronological order.
  - ``type`` field is "TRADE" or "REDEEM"; ``side`` is "BUY" or "SELL"
    on TRADE rows. On REDEEM rows ``usdcSize`` = payout: non-zero
    means a winning redemption, exactly zero means dust (losing token
    set that paid zero on resolution).
  - ``timestamp`` is a unix-seconds integer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
import time

import aiohttp
import structlog

log = structlog.get_logger(__name__)

DATA_API_ACTIVITY_URL = "https://data-api.polymarket.com/activity"
# data-api 403s without a realistic UA; this is the shape of the
# header that works.
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) novakash-engine activity-digest"
_DEFAULT_LIMIT = 500
_DEFAULT_TIMEOUT_S = 10.0
_CUTOFF_SECONDS = 30 * 60  # 30-minute window


@dataclass(frozen=True)
class DigestRow:
    """One classified activity event."""

    kind: str  # "TRADE_BUY" | "TRADE_SELL" | "REDEEM_WIN" | "REDEEM_DUST"
    timestamp: int
    usdc_size: float
    shares: float
    price: Optional[float]
    condition_id: str


@dataclass(frozen=True)
class DigestPayload:
    """Aggregated 30-minute digest."""

    rows: list[DigestRow] = field(default_factory=list)
    now_ts: int = 0
    trade_buy_count: int = 0
    trade_sell_count: int = 0
    redeem_win_count: int = 0
    redeem_dust_count: int = 0
    trade_buy_usd: float = 0.0
    trade_sell_usd: float = 0.0
    redeem_win_usd: float = 0.0


def _classify(row: dict) -> Optional[DigestRow]:
    """Classify one raw data-api row. Returns None if unrecognised."""
    try:
        kind_raw = (row.get("type") or "").upper()
        ts = int(row.get("timestamp") or 0)
        usdc = float(row.get("usdcSize") or 0)
        shares = float(row.get("size") or 0)
        price = row.get("price")
        price_f: Optional[float] = float(price) if price is not None else None
        cid = str(row.get("conditionId") or "")
    except (TypeError, ValueError):
        return None

    if ts <= 0:
        return None

    if kind_raw == "TRADE":
        side = (row.get("side") or "").upper()
        if side == "BUY":
            kind = "TRADE_BUY"
        elif side == "SELL":
            kind = "TRADE_SELL"
        else:
            return None
    elif kind_raw == "REDEEM":
        # Non-zero payout = real win. Exactly-zero = losing-token dust
        # (Polymarket emits a REDEEM for every losing token set too).
        kind = "REDEEM_WIN" if usdc > 0 else "REDEEM_DUST"
    else:
        return None

    return DigestRow(
        kind=kind,
        timestamp=ts,
        usdc_size=usdc,
        shares=shares,
        price=price_f,
        condition_id=cid,
    )


def build_digest(
    rows: list[dict],
    *,
    now_ts: int,
    cutoff_seconds: int = _CUTOFF_SECONDS,
) -> DigestPayload:
    """Pure function: filter + classify raw data-api rows."""
    cutoff = now_ts - cutoff_seconds
    classified: list[DigestRow] = []
    for raw in rows or []:
        dr = _classify(raw)
        if dr is None:
            continue
        if dr.timestamp < cutoff:
            # data-api returns reverse-chrono so we could break early,
            # but some pagination edge cases return unsorted — iterate
            # fully for safety. 500-row cap keeps cost trivial.
            continue
        classified.append(dr)

    # Aggregate counts + totals.
    buy_n = sum(1 for r in classified if r.kind == "TRADE_BUY")
    sell_n = sum(1 for r in classified if r.kind == "TRADE_SELL")
    win_n = sum(1 for r in classified if r.kind == "REDEEM_WIN")
    dust_n = sum(1 for r in classified if r.kind == "REDEEM_DUST")
    buy_usd = sum(r.usdc_size for r in classified if r.kind == "TRADE_BUY")
    sell_usd = sum(r.usdc_size for r in classified if r.kind == "TRADE_SELL")
    win_usd = sum(r.usdc_size for r in classified if r.kind == "REDEEM_WIN")

    return DigestPayload(
        rows=classified,
        now_ts=now_ts,
        trade_buy_count=buy_n,
        trade_sell_count=sell_n,
        redeem_win_count=win_n,
        redeem_dust_count=dust_n,
        trade_buy_usd=round(buy_usd, 2),
        trade_sell_usd=round(sell_usd, 2),
        redeem_win_usd=round(win_usd, 2),
    )


class ActivityDigestFetcher:
    """Thin async wrapper over the data-api ``/activity`` endpoint.

    Runtime dependency injection point — tests substitute a fake via
    a subclass or a mock on ``fetch``. Never swallows exceptions
    silently; callers decide retry policy.
    """

    def __init__(
        self,
        proxy_address: str,
        *,
        base_url: str = DATA_API_ACTIVITY_URL,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self._proxy = proxy_address
        self._base_url = base_url
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session = session  # injected for tests; production path opens one per call
        self._log = log.bind(component="activity_digest")

    async def fetch_raw(self, limit: int = _DEFAULT_LIMIT) -> list[dict]:
        """GET /activity?user=<proxy>&limit=<limit>. Returns JSON list.

        Raises aiohttp.ClientError on transport failure so the caller
        sees the failure rather than silently getting an empty list
        (the digest renderer handles empty explicitly).
        """
        params = {"user": self._proxy, "limit": str(limit)}
        headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}

        if self._session is not None:
            return await self._get(self._session, params, headers)

        async with aiohttp.ClientSession(timeout=self._timeout) as sess:
            return await self._get(sess, params, headers)

    async def _get(
        self,
        sess: aiohttp.ClientSession,
        params: dict,
        headers: dict,
    ) -> list[dict]:
        async with sess.get(self._base_url, params=params, headers=headers) as resp:
            if resp.status != 200:
                body = await resp.text()
                self._log.bind(status=resp.status).warning(
                    "activity_digest.fetch_non_200",
                    body=body[:200],
                )
                return []
            data = await resp.json()
            if not isinstance(data, list):
                self._log.warning(
                    "activity_digest.unexpected_shape",
                    sample=str(data)[:200],
                )
                return []
            return data

    async def fetch(self, *, now_ts: Optional[int] = None) -> DigestPayload:
        """Fetch + build a 30-min digest. Returns empty payload on error."""
        now = int(now_ts if now_ts is not None else time.time())
        try:
            raw = await self.fetch_raw()
        except aiohttp.ClientError as exc:
            self._log.bind(error=str(exc)[:200]).warning(
                "activity_digest.fetch_failed"
            )
            return DigestPayload(now_ts=now)
        return build_digest(raw, now_ts=now)
