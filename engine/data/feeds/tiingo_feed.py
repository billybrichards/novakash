"""
Tiingo Multi-Asset Crypto Feed

Polls Tiingo's top-of-book API every 2 seconds for BTC, ETH, SOL, XRP.
Shows best bid/ask exchange — important for oracle matching against Chainlink.

API endpoint:
    https://api.tiingo.com/tiingo/crypto/top?tickers=btcusd,ethusd,solusd,xrpusd&token=KEY

Data written to: ticks_tiingo table in Railway PostgreSQL.

Tiingo key: TIINGO_API_KEY from .env

Asset subset override: ``TIINGO_ASSETS`` env var (comma-separated, upper-case).
Defaults to ``BTC,ETH,SOL,XRP``. Used to roll the feed back to BTC-only at
runtime without code changes if the Tiingo plan does not support a ticker
(empirical rollback knob — PR #321 surfaced v7_15m_sniper_{eth,sol,xrp}
skips with ``feature_stale: tiingo missing at eval`` which would be either
(a) Tiingo not returning a ticker or (b) this feed failing to parse it;
this override lets Daisy bisect).
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

# Full asset ticker map: internal asset name → Tiingo ticker.
# Keep this dict stable — the composition reads it, tests assert on it.
# Runtime subset selection is via ``_resolve_tickers()`` below.
ALL_TICKERS = {
    "BTC": "btcusd",
    "ETH": "ethusd",
    "SOL": "solusd",
    "XRP": "xrpusd",
}

# Back-compat alias — callers (and tests) import ``TICKERS`` directly.
# It mirrors the full map so existing imports keep working even when a
# runtime subset is active on a given feed instance.
TICKERS = ALL_TICKERS

POLL_INTERVAL = 2  # seconds (Tiingo paid: 10K req/hr = 2.7/s, 2s is safe)
API_BASE = "https://api.tiingo.com/tiingo/crypto/top"
SOURCE = "tiingo"


def _resolve_tickers(
    explicit_assets: Optional[list[str]] = None,
) -> dict[str, str]:
    """Resolve the asset→ticker map for this feed instance.

    Precedence (highest first):
      1. ``explicit_assets`` constructor arg (composition-driven)
      2. ``TIINGO_ASSETS`` env var (ops-driven rollback knob)
      3. ``ALL_TICKERS`` (BTC+ETH+SOL+XRP default)

    Unknown asset names are dropped with a warning — fail-open rather than
    crashing the feed boot on a typo. An empty resolved set falls back to
    ``ALL_TICKERS`` so the feed always has something to poll.
    """
    selected: Optional[list[str]] = explicit_assets
    if selected is None:
        env_val = os.environ.get("TIINGO_ASSETS", "").strip()
        if env_val:
            selected = [s.strip().upper() for s in env_val.split(",") if s.strip()]
    if not selected:
        return dict(ALL_TICKERS)

    out: dict[str, str] = {}
    unknown: list[str] = []
    for asset in selected:
        asset_up = asset.upper()
        if asset_up in ALL_TICKERS:
            out[asset_up] = ALL_TICKERS[asset_up]
        else:
            unknown.append(asset_up)
    if unknown:
        log.warning("tiingo_feed.unknown_assets_skipped", unknown=unknown)
    return out if out else dict(ALL_TICKERS)


class TiingoFeed:
    """
    Polls Tiingo crypto top-of-book API every 2 seconds.

    Writes to ticks_tiingo table. Runs as an async background task.
    Logs which exchange has best bid/ask for oracle cross-referencing.

    Attributes:
        connected: True while polling is active and last poll succeeded.
        last_message_at: Timestamp of the most recent successful poll.
    """

    def __init__(
        self,
        api_key: str,
        pool,
        assets: Optional[list[str]] = None,
    ) -> None:
        """
        Args:
            api_key: Tiingo API key (TIINGO_API_KEY from .env)
            pool:    asyncpg.Pool from DBClient._pool for ticks_tiingo writes
            assets:  Optional subset of ``ALL_TICKERS.keys()`` to poll.
                     Defaults to the full set (BTC/ETH/SOL/XRP) or the
                     ``TIINGO_ASSETS`` env override. See
                     :func:`_resolve_tickers` for precedence.
        """
        self._api_key = api_key
        self._pool = pool
        self._running = False
        self._connected = False
        self._last_message_at: Optional[datetime] = None
        self._session = None
        # Per-instance ticker map — resolved once at init so every poll uses
        # the same stable set. Full map stays in ALL_TICKERS for callers.
        self._tickers: dict[str, str] = _resolve_tickers(assets)
        self._tickers_param = ",".join(self._tickers.values())
        # In-memory cache: updated on EVERY poll tick. Keyed by asset name.
        # Read by DataSurfaceManager for zero-I/O delta calculation.
        self.latest_prices: dict[str, float] = {}
        # Observability: log the asset set each poll returns. Flips to
        # True after the first successful poll so we only emit the INFO
        # "assets_received" summary on that first success + whenever the
        # received set changes (missing assets = Tiingo plan / ticker
        # dropped). Otherwise it's DEBUG to avoid log spam every 2s.
        self._logged_initial_assets = False
        self._last_received_assets: frozenset[str] = frozenset()

    # ─── Public Status ────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_message_at(self) -> Optional[datetime]:
        return self._last_message_at

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start HTTP session and polling loop."""
        try:
            import aiohttp
        except ImportError:
            log.error("tiingo_feed.aiohttp_not_installed")
            return

        import aiohttp

        log.info(
            "tiingo_feed.starting",
            assets=list(self._tickers.keys()),
            interval=POLL_INTERVAL,
        )

        self._running = True
        async with aiohttp.ClientSession(
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Token {self._api_key}",
            }
        ) as session:
            self._session = session
            while self._running:
                try:
                    await self._poll(session)
                    self._connected = True
                    self._last_message_at = datetime.now(timezone.utc)
                except Exception as exc:
                    log.error("tiingo_feed.poll_error", error=str(exc))
                    self._connected = False
                    if "429" in str(exc):
                        await asyncio.sleep(60)  # Back off 60s on rate limit
                        continue
                await asyncio.sleep(POLL_INTERVAL)

        self._session = None

    async def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
        self._connected = False
        log.info("tiingo_feed.stopped")

    # ─── Internal ─────────────────────────────────────────────────────────────

    async def _poll(self, session) -> None:
        """Fetch top-of-book data for all tickers and write to DB."""
        import aiohttp

        url = f"{API_BASE}?tickers={self._tickers_param}&token={self._api_key}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                raise Exception(f"HTTP {resp.status}")
            data = await resp.json()

        rows = []
        received_assets: list[str] = []
        for item in data:
            ticker = item.get("ticker", "").lower()
            # Map ticker back to asset — use per-instance map so a BTC-only
            # feed doesn't accidentally accept ETH/SOL/XRP rows.
            asset = next(
                (k for k, v in self._tickers.items() if v == ticker), None
            )
            if not asset:
                continue

            # Tiingo top-of-book structure: topOfBookData is a list, take [0]
            tob_list = item.get("topOfBookData", [])
            if not tob_list:
                continue
            tob = tob_list[0]

            last_price = _safe_float(tob.get("lastPrice") or tob.get("last"))
            bid_price = _safe_float(tob.get("bidPrice") or tob.get("bid"))
            ask_price = _safe_float(tob.get("askPrice") or tob.get("ask"))
            bid_exchange = _safe_str(tob.get("bidExchange") or tob.get("bidSizeExchange"))
            ask_exchange = _safe_str(tob.get("askExchange") or tob.get("askSizeExchange"))
            last_exchange = _safe_str(tob.get("lastExchange") or tob.get("exchange"))

            log.debug(
                "tiingo_feed.tick",
                asset=asset,
                last=last_price,
                bid=f"{bid_price} @ {bid_exchange}",
                ask=f"{ask_price} @ {ask_exchange}",
            )

            # Update in-memory cache on every poll tick
            if last_price is not None:
                self.latest_prices[asset] = last_price
                received_assets.append(asset)

            rows.append((
                asset,
                last_price,
                bid_price,
                ask_price,
                bid_exchange,
                ask_exchange,
                last_exchange,
            ))

        # Observability: emit the received-set once on startup and again
        # whenever it changes. Lets Montreal log-grep confirm ETH/SOL/XRP
        # ticks arrive post PR #321.
        received_set = frozenset(received_assets)
        requested_set = frozenset(self._tickers.keys())
        missing = requested_set - received_set
        if (not self._logged_initial_assets) or (received_set != self._last_received_assets):
            log.info(
                "tiingo_feed.assets_received",
                requested=sorted(requested_set),
                received=sorted(received_set),
                missing=sorted(missing),
            )
            self._logged_initial_assets = True
            self._last_received_assets = received_set

        if rows:
            await self._write_rows(rows)

    async def _write_rows(self, rows: list[tuple]) -> None:
        """Batch INSERT rows into ticks_tiingo."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.executemany(
                    """
                    INSERT INTO ticks_tiingo (
                        ts, asset,
                        last_price, bid_price, ask_price,
                        bid_exchange, ask_exchange, last_exchange,
                        source
                    ) VALUES (NOW(), $1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    [(*row, SOURCE) for row in rows],
                )
            log.debug("tiingo_feed.written", rows=len(rows))
        except Exception as exc:
            log.debug("tiingo_feed.write_error", error=str(exc))


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _safe_float(val) -> Optional[float]:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _safe_str(val) -> Optional[str]:
    try:
        return str(val)[:20] if val is not None else None
    except Exception:
        return None
