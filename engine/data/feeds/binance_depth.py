"""
Binance Depth Feed — top-of-book / depth20 snapshots for multi-asset.

PR follow-up to #582 (gap 3 of 3). The v9.3 BTC + v9.5 ETH+XRP
boosters were trained on 4 depth features the engine did not emit:

  * `binance_depth_imbalance_inner` — top-of-book (best-bid vs best-ask)
    size imbalance.
  * `binance_depth_imbalance_1pct`  — bid vs ask depth within ±1% of mid.
  * `binance_depth_imbalance_5pct`  — bid vs ask depth within ±5% of mid.
  * `binance_spread_pct`            — (best_ask - best_bid) / mid * 100.

PR #582 added the schema slots + builder kwargs as stubs (all-None) and
documented that no `ticks_binance_book` table existed and the existing
`BinanceWebSocketFeed` had `on_book=None`. This module closes that gap.

## Architecture

- One `BinanceDepthFeed` instance per asset (BTC/ETH/XRP/…). Each owns
  ONE `/ws/<symbol>@depth20@100ms` connection. Mirrors the existing
  `BinanceWebSocketFeed` reconnect pattern.
- Throttled writer: depth20@100ms is a HIGH-rate stream (~10 msg/sec).
  Writing every tick to RDS would saturate the pool. The feed keeps the
  latest in-memory snapshot per asset and flushes to `ticks_binance_book`
  on a configurable interval (default 1s — 10× compression).
- The in-memory `latest_book_by_asset` dict is the fast-path source for
  the `compute_binance_depth_imbalance` feature emitter — no DB query
  on the scoring critical path.

## Failure semantics

- Connection drops → exponential backoff reconnect (matching
  `BinanceWebSocketFeed`).
- Parse errors / unexpected payloads → logged at debug, message dropped.
- DB write errors → logged + swallowed; next flush retries with the
  current snapshot.
- All errors fail soft — never raise into callers.

## Schema (created via `TickRecorder.ensure_tables`)

    CREATE TABLE ticks_binance_book (
        id            BIGSERIAL PRIMARY KEY,
        ts            TIMESTAMPTZ  NOT NULL,
        asset         VARCHAR(10)  NOT NULL,
        last_update_id BIGINT,
        best_bid      FLOAT8,
        best_ask      FLOAT8,
        best_bid_qty  FLOAT8,
        best_ask_qty  FLOAT8,
        mid           FLOAT8,
        spread_pct    FLOAT8,
        bid_depth_1pct FLOAT8,
        ask_depth_1pct FLOAT8,
        bid_depth_5pct FLOAT8,
        ask_depth_5pct FLOAT8,
        bids_top20    JSONB,
        asks_top20    JSONB,
        created_at    TIMESTAMPTZ DEFAULT NOW()
    );

Indexed on `(asset, ts DESC)` for ASOF lookups by the emitter.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
import websockets

log = structlog.get_logger(__name__)


# Default Binance symbol map. Kept in sync with `polymarket_5min.py:795-799`.
DEFAULT_BINANCE_SYMBOL_MAP: dict[str, str] = {
    "BTC": "btcusdt",
    "ETH": "ethusdt",
    "XRP": "xrpusdt",
    "SOL": "solusdt",
    "DOGE": "dogeusdt",
    "BNB": "bnbusdt",
}


DEFAULT_FLUSH_INTERVAL_S: float = float(
    os.environ.get("BINANCE_DEPTH_FLUSH_INTERVAL_S", "1.0")
)
RECONNECT_DELAY_MAX: int = 60  # seconds


def _asset_to_symbol(asset: str) -> str:
    """Resolve asset code to Binance symbol via env override or default map."""
    asset = asset.upper()
    # Env override format: `BINANCE_SYMBOL_BTC=btcusdt` (lower case symbol).
    env_key = f"BINANCE_SYMBOL_{asset}"
    env_val = os.environ.get(env_key)
    if env_val:
        return env_val.lower()
    sym = DEFAULT_BINANCE_SYMBOL_MAP.get(asset)
    if sym:
        return sym
    # Last-resort guess: lowercase + USDT suffix.
    return f"{asset.lower()}usdt"


def _resolve_assets() -> list[str]:
    """Read FIVE_MIN_ASSETS at construction time. Default BTC."""
    raw = os.environ.get("FIVE_MIN_ASSETS", "BTC")
    assets = [a.strip().upper() for a in raw.split(",") if a.strip()]
    return assets or ["BTC"]


# ──────────────────────────────────────────────────────────────────────
#  Top-level snapshot dataclass
# ──────────────────────────────────────────────────────────────────────


def parse_depth_message(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Parse a Binance depth20@100ms message into a normalized snapshot.

    Binance's depth20 payload (futures):
        {
            "e": "depthUpdate",
            "E": 1729345678901,
            "T": 1729345678900,
            "s": "BTCUSDT",
            "U": 1234567,        # first update id
            "u": 1234589,        # last update id
            "pu": 1234566,
            "b": [["67890.10", "1.234"], ...],   # bids (price, qty)
            "a": [["67890.20", "0.567"], ...]    # asks
        }

    Spot depth20 partial book has no `e`/`E` and uses `lastUpdateId`:
        {
            "lastUpdateId": 1234589,
            "bids": [["67890.10", "1.234"], ...],
            "asks": [["67890.20", "0.567"], ...]
        }

    Returns None on any unparseable payload.
    """
    if not isinstance(payload, dict):
        return None

    bids_raw = payload.get("b") or payload.get("bids")
    asks_raw = payload.get("a") or payload.get("asks")
    if not bids_raw or not asks_raw:
        return None

    try:
        bids: list[tuple[float, float]] = [
            (float(b[0]), float(b[1])) for b in bids_raw if len(b) >= 2
        ]
        asks: list[tuple[float, float]] = [
            (float(a[0]), float(a[1])) for a in asks_raw if len(a) >= 2
        ]
    except (TypeError, ValueError, IndexError):
        return None

    if not bids or not asks:
        return None

    # Sort defensively — Binance docs say descending bids / ascending asks,
    # but better to enforce here than to trust the wire format.
    bids.sort(key=lambda x: x[0], reverse=True)
    asks.sort(key=lambda x: x[0])

    best_bid, best_bid_qty = bids[0]
    best_ask, best_ask_qty = asks[0]
    if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
        # Crossed / zero book — drop.
        return None

    mid = (best_bid + best_ask) / 2.0
    spread_pct = (best_ask - best_bid) / mid * 100.0

    # Depth within ±1% and ±5% of mid.
    lo_1pct, hi_1pct = mid * 0.99, mid * 1.01
    lo_5pct, hi_5pct = mid * 0.95, mid * 1.05

    bid_depth_1pct = sum(q for p, q in bids if p >= lo_1pct)
    ask_depth_1pct = sum(q for p, q in asks if p <= hi_1pct)
    bid_depth_5pct = sum(q for p, q in bids if p >= lo_5pct)
    ask_depth_5pct = sum(q for p, q in asks if p <= hi_5pct)

    last_update_id = (
        payload.get("u") or payload.get("lastUpdateId") or 0
    )

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "best_bid_qty": best_bid_qty,
        "best_ask_qty": best_ask_qty,
        "mid": mid,
        "spread_pct": spread_pct,
        "bid_depth_1pct": bid_depth_1pct,
        "ask_depth_1pct": ask_depth_1pct,
        "bid_depth_5pct": bid_depth_5pct,
        "ask_depth_5pct": ask_depth_5pct,
        "bids": bids,
        "asks": asks,
        "last_update_id": int(last_update_id) if last_update_id else 0,
        "ts": time.time(),
    }


# ──────────────────────────────────────────────────────────────────────
#  Imbalance computation (used by feature emitters too)
# ──────────────────────────────────────────────────────────────────────


def compute_inner_imbalance(
    best_bid_qty: Optional[float], best_ask_qty: Optional[float]
) -> Optional[float]:
    """Top-of-book imbalance: (bid_qty - ask_qty) / (bid_qty + ask_qty).

    Returns None if either input is None or the sum is 0.
    Range: [-1.0, +1.0]. Positive → more bid depth at top of book.
    """
    if best_bid_qty is None or best_ask_qty is None:
        return None
    s = float(best_bid_qty) + float(best_ask_qty)
    if s <= 0:
        return None
    return (float(best_bid_qty) - float(best_ask_qty)) / s


def compute_pct_imbalance(
    bid_depth: Optional[float], ask_depth: Optional[float]
) -> Optional[float]:
    """Depth-band imbalance: (bid - ask) / (bid + ask).

    Used for both ±1% and ±5% bands.
    """
    return compute_inner_imbalance(bid_depth, ask_depth)


# ──────────────────────────────────────────────────────────────────────
#  Per-asset depth feed
# ──────────────────────────────────────────────────────────────────────


class BinanceDepthFeed:
    """Subscribes to `<symbol>@depth20@100ms` for a single asset.

    Maintains the latest book snapshot in-memory and (if a db_pool is
    provided) flushes a throttled write to `ticks_binance_book` every
    `flush_interval_s` seconds.
    """

    def __init__(
        self,
        asset: str,
        db_pool: Any = None,
        venue: str = "futures",
        flush_interval_s: Optional[float] = None,
    ) -> None:
        self.asset = asset.upper()
        self.symbol = _asset_to_symbol(self.asset)
        self.venue = venue
        self._pool = db_pool
        self._flush_interval_s = (
            float(flush_interval_s)
            if flush_interval_s is not None
            else DEFAULT_FLUSH_INTERVAL_S
        )
        self._running = False
        self._connected = False
        self._reconnect_delay = 1.0
        self._last_message_at: Optional[datetime] = None
        self._latest_snapshot: Optional[dict[str, Any]] = None
        self._writer_task: Optional[asyncio.Task] = None
        self._reader_task: Optional[asyncio.Task] = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_message_at(self) -> Optional[datetime]:
        return self._last_message_at

    @property
    def latest_snapshot(self) -> Optional[dict[str, Any]]:
        return self._latest_snapshot

    @property
    def _stream_url(self) -> str:
        stream_name = f"{self.symbol}@depth20@100ms"
        if self.venue == "spot":
            return f"wss://stream.binance.com:9443/ws/{stream_name}"
        return f"wss://fstream.binance.com/ws/{stream_name}"

    async def start(self) -> None:
        """Begin reader + writer loops."""
        self._running = True
        self._writer_task = asyncio.create_task(
            self._writer_loop(), name=f"binance_depth_writer_{self.asset}"
        )
        self._reader_task = asyncio.create_task(
            self._reader_loop(), name=f"binance_depth_reader_{self.asset}"
        )
        log.info(
            "binance_depth.starting",
            asset=self.asset,
            symbol=self.symbol,
            venue=self.venue,
            flush_interval_s=self._flush_interval_s,
        )

    async def stop(self) -> None:
        """Stop reader + writer. Best-effort final flush."""
        self._running = False
        for task in (self._reader_task, self._writer_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        # Final flush of any pending snapshot.
        await self._flush_snapshot()
        log.info("binance_depth.stopped", asset=self.asset)

    # ── Reader: maintain WS connection + parse depth20 messages ──────────

    async def _reader_loop(self) -> None:
        while self._running:
            try:
                await self._connect()
                self._reconnect_delay = 1.0
            except Exception as exc:
                self._connected = False
                log.warning(
                    "binance_depth.disconnected",
                    asset=self.asset,
                    venue=self.venue,
                    error=str(exc)[:120],
                    retry_in=self._reconnect_delay,
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, RECONNECT_DELAY_MAX
                )

    async def _connect(self) -> None:
        log.info("binance_depth.connecting", asset=self.asset, url=self._stream_url)
        # compression=None: matches BinanceWebSocketFeed fix from 2026-04-23
        # (websockets 15.x permessage-deflate stalls).
        async with websockets.connect(self._stream_url, compression=None) as ws:
            self._connected = True
            log.info("binance_depth.connected", asset=self.asset, symbol=self.symbol)
            async for raw in ws:
                if not self._running:
                    break
                try:
                    envelope = json.loads(raw)
                    payload = envelope.get("data") if "data" in envelope else envelope
                    snap = parse_depth_message(payload)
                    if snap is not None:
                        self._latest_snapshot = snap
                        self._last_message_at = datetime.now(timezone.utc)
                except Exception as exc:
                    log.debug(
                        "binance_depth.parse_error",
                        asset=self.asset,
                        error=str(exc)[:120],
                    )
        self._connected = False

    # ── Writer: throttled flush of latest snapshot to RDS ────────────────

    async def _writer_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._flush_interval_s)
            await self._flush_snapshot()

    async def _flush_snapshot(self) -> None:
        if self._pool is None:
            return
        snap = self._latest_snapshot
        if snap is None:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO ticks_binance_book (
                        ts, asset, last_update_id,
                        best_bid, best_ask, best_bid_qty, best_ask_qty,
                        mid, spread_pct,
                        bid_depth_1pct, ask_depth_1pct,
                        bid_depth_5pct, ask_depth_5pct,
                        bids_top20, asks_top20
                    ) VALUES (
                        NOW(), $1, $2, $3, $4, $5, $6, $7, $8,
                        $9, $10, $11, $12, $13::jsonb, $14::jsonb
                    )
                    """,
                    self.asset,
                    snap.get("last_update_id", 0),
                    snap.get("best_bid"),
                    snap.get("best_ask"),
                    snap.get("best_bid_qty"),
                    snap.get("best_ask_qty"),
                    snap.get("mid"),
                    snap.get("spread_pct"),
                    snap.get("bid_depth_1pct"),
                    snap.get("ask_depth_1pct"),
                    snap.get("bid_depth_5pct"),
                    snap.get("ask_depth_5pct"),
                    json.dumps(snap.get("bids", [])[:20]),
                    json.dumps(snap.get("asks", [])[:20]),
                )
        except Exception as exc:
            log.debug(
                "binance_depth.write_error",
                asset=self.asset,
                error=str(exc)[:120],
            )


# ──────────────────────────────────────────────────────────────────────
#  Multi-asset orchestrator
# ──────────────────────────────────────────────────────────────────────


class BinanceDepthMultiFeed:
    """Owns one `BinanceDepthFeed` per asset in `FIVE_MIN_ASSETS`.

    Construct once at composition time; `start()` spawns N reader+writer
    pairs. Failures on one asset don't affect others.

    `latest_snapshot(asset)` is the fast-path source for the feature
    emitter — no DB query on the scoring critical path.
    """

    def __init__(
        self,
        db_pool: Any = None,
        assets: Optional[list[str]] = None,
        venue: str = "futures",
        flush_interval_s: Optional[float] = None,
    ) -> None:
        self._assets = [a.upper() for a in (assets or _resolve_assets())]
        self._feeds: dict[str, BinanceDepthFeed] = {
            asset: BinanceDepthFeed(
                asset=asset,
                db_pool=db_pool,
                venue=venue,
                flush_interval_s=flush_interval_s,
            )
            for asset in self._assets
        }
        self._pool = db_pool

    @property
    def assets(self) -> list[str]:
        return list(self._assets)

    def latest_snapshot(self, asset: str) -> Optional[dict[str, Any]]:
        feed = self._feeds.get(asset.upper())
        if feed is None:
            return None
        return feed.latest_snapshot

    async def start(self) -> None:
        log.info("binance_depth_multi.starting", assets=self._assets)
        for asset, feed in self._feeds.items():
            try:
                await feed.start()
            except Exception as exc:
                log.warning(
                    "binance_depth_multi.start_failed",
                    asset=asset,
                    error=str(exc)[:120],
                )

    async def stop(self) -> None:
        for feed in self._feeds.values():
            try:
                await feed.stop()
            except Exception:
                pass
        log.info("binance_depth_multi.stopped", assets=self._assets)
