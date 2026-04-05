"""
TickRecorder — Comprehensive tick-level data recorder.

Records ALL real-time data to Railway PostgreSQL for later analysis:
  - Binance aggTrades (buffered 1s, batch INSERT)
  - CoinGlass snapshots (every 10s)
  - Gamma/Polymarket prices (every window evaluation)
  - TimesFM forecasts (every forecast)
  - VPIN is included in the Binance ticks table

Architecture:
  - Passive observation ONLY — never blocks the trading loop
  - All writes are fire-and-forget (errors logged and swallowed)
  - Uses the existing asyncpg.Pool from DBClient
  - Binance ticks are buffered in memory and flushed every 1 second

Usage:
    recorder = TickRecorder(pool=db_client._pool)
    await recorder.ensure_tables()
    await recorder.start()
    # ... in trade callback:
    recorder.record_binance_tick(trade, vpin=0.42)
    # ... on stop:
    await recorder.stop()
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Optional, Any

import structlog

log = structlog.get_logger(__name__)


class TickRecorder:
    """
    Passive tick-level data recorder.

    Takes the existing asyncpg.Pool — does NOT create a new pool.
    All record_*() methods are non-blocking and never raise.
    """

    def __init__(self, pool) -> None:
        """
        Args:
            pool: asyncpg.Pool from DBClient._pool
        """
        self._pool = pool
        self._running = False
        self._flush_task: Optional[asyncio.Task] = None

        # Binance tick buffer: list of tuples ready for executemany
        # (ts, asset, price, quantity, is_buyer_maker, vpin)
        self._binance_buffer: list[tuple] = []
        self._buffer_lock = asyncio.Lock()

    # ─── Schema Creation ──────────────────────────────────────────────────────

    async def ensure_tables(self) -> None:
        """Create all tick tables if they don't exist. Non-fatal on failure."""
        if not self._pool:
            log.warning("tick_recorder.ensure_tables.no_pool")
            return
        try:
            async with self._pool.acquire() as conn:
                # ── ticks_binance ─────────────────────────────────────────
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS ticks_binance (
                        id          BIGSERIAL PRIMARY KEY,
                        ts          TIMESTAMPTZ NOT NULL,
                        asset       VARCHAR(10)  NOT NULL,
                        price       FLOAT8       NOT NULL,
                        quantity    FLOAT8       NOT NULL,
                        is_buyer_maker BOOLEAN   NOT NULL,
                        vpin        FLOAT8,
                        created_at  TIMESTAMPTZ  DEFAULT NOW()
                    );
                    CREATE INDEX IF NOT EXISTS idx_ticks_binance_ts
                        ON ticks_binance (ts DESC);
                    CREATE INDEX IF NOT EXISTS idx_ticks_binance_asset_ts
                        ON ticks_binance (asset, ts DESC);
                """)

                # ── ticks_coinglass ───────────────────────────────────────
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS ticks_coinglass (
                        id               BIGSERIAL PRIMARY KEY,
                        ts               TIMESTAMPTZ NOT NULL,
                        asset            VARCHAR(10)  NOT NULL,
                        oi_usd           FLOAT8,
                        oi_delta_pct     FLOAT8,
                        liq_long_usd     FLOAT8,
                        liq_short_usd    FLOAT8,
                        long_pct         FLOAT8,
                        short_pct        FLOAT8,
                        top_long_pct     FLOAT8,
                        top_short_pct    FLOAT8,
                        taker_buy_usd    FLOAT8,
                        taker_sell_usd   FLOAT8,
                        funding_rate     FLOAT8,
                        long_short_ratio FLOAT8,
                        top_position_ratio FLOAT8,
                        created_at       TIMESTAMPTZ DEFAULT NOW()
                    );
                    CREATE INDEX IF NOT EXISTS idx_ticks_coinglass_ts
                        ON ticks_coinglass (ts DESC);
                    CREATE INDEX IF NOT EXISTS idx_ticks_coinglass_asset_ts
                        ON ticks_coinglass (asset, ts DESC);
                """)

                # ── ticks_gamma ───────────────────────────────────────────
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS ticks_gamma (
                        id           BIGSERIAL PRIMARY KEY,
                        ts           TIMESTAMPTZ NOT NULL,
                        asset        VARCHAR(10)  NOT NULL,
                        timeframe    VARCHAR(5),
                        window_ts    BIGINT,
                        up_price     FLOAT8,
                        down_price   FLOAT8,
                        price_source VARCHAR(50),
                        up_token_id  VARCHAR(120),
                        down_token_id VARCHAR(120),
                        slug         VARCHAR(200),
                        created_at   TIMESTAMPTZ DEFAULT NOW()
                    );
                    CREATE INDEX IF NOT EXISTS idx_ticks_gamma_ts
                        ON ticks_gamma (ts DESC);
                    CREATE INDEX IF NOT EXISTS idx_ticks_gamma_asset_ts
                        ON ticks_gamma (asset, ts DESC);
                    CREATE INDEX IF NOT EXISTS idx_ticks_gamma_window_ts
                        ON ticks_gamma (window_ts);
                """)

                # ── ticks_timesfm ─────────────────────────────────────────
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS ticks_timesfm (
                        id               BIGSERIAL PRIMARY KEY,
                        ts               TIMESTAMPTZ NOT NULL,
                        asset            VARCHAR(10)  NOT NULL,
                        window_ts        BIGINT,
                        window_close_ts  BIGINT,
                        seconds_to_close INTEGER,
                        horizon          INTEGER,
                        direction        VARCHAR(4),
                        confidence       FLOAT8,
                        predicted_close  FLOAT8,
                        spread           FLOAT8,
                        p10              FLOAT8,
                        p50              FLOAT8,
                        p90              FLOAT8,
                        delta_vs_open    FLOAT8,
                        fetch_latency_ms FLOAT8,
                        is_stale         BOOLEAN,
                        created_at       TIMESTAMPTZ DEFAULT NOW()
                    );
                    CREATE INDEX IF NOT EXISTS idx_ticks_timesfm_ts
                        ON ticks_timesfm (ts DESC);
                    CREATE INDEX IF NOT EXISTS idx_ticks_timesfm_asset_ts
                        ON ticks_timesfm (asset, ts DESC);
                    CREATE INDEX IF NOT EXISTS idx_ticks_timesfm_window
                        ON ticks_timesfm (window_ts, seconds_to_close);
                """)

                # Safe migration: add new columns if table already exists
                for col, col_type in [
                    ("window_close_ts", "BIGINT"),
                    ("seconds_to_close", "INTEGER"),
                    ("horizon", "INTEGER"),
                ]:
                    try:
                        await conn.execute(f"ALTER TABLE ticks_timesfm ADD COLUMN IF NOT EXISTS {col} {col_type}")
                    except Exception:
                        pass

            log.info("tick_recorder.tables_ensured")
        except Exception as exc:
            log.error("tick_recorder.ensure_tables_failed", error=str(exc))

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background flush loop."""
        if self._running:
            return
        self._running = True
        self._flush_task = asyncio.create_task(
            self._flush_loop(), name="tick_recorder:flush"
        )
        log.info("tick_recorder.started")

    async def stop(self) -> None:
        """Flush remaining buffer and stop."""
        self._running = False
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        # Final flush of any remaining buffered ticks
        await self._flush_binance_buffer()
        log.info("tick_recorder.stopped")

    # ─── Record Methods ───────────────────────────────────────────────────────

    def record_binance_tick(self, trade: Any, vpin: float = 0.0) -> None:
        """
        Buffer a Binance aggTrade tick for batch insert.

        Non-blocking — just appends to in-memory buffer.
        The flush loop drains it every 1 second.

        Args:
            trade: AggTrade dataclass/model with price, quantity, is_buyer_maker, trade_time
            vpin:  Current VPIN value at time of tick
        """
        try:
            ts = trade.trade_time
            if not isinstance(ts, datetime):
                ts = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            elif ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            row = (
                ts,
                "BTC",  # Binance feed is BTC-only for now
                float(trade.price),
                float(trade.quantity),
                bool(trade.is_buyer_maker),
                float(vpin),
            )
            # Non-blocking append (no lock needed for simple list append in CPython)
            self._binance_buffer.append(row)
        except Exception as exc:
            log.debug("tick_recorder.record_binance_tick.error", error=str(exc))

    async def record_coinglass_snapshot(
        self, asset: str, snapshot: Any
    ) -> None:
        """
        Write a CoinGlass snapshot to ticks_coinglass.

        Fire-and-forget — never blocks or raises.

        Args:
            asset:    e.g. "BTC", "ETH", "SOL", "XRP"
            snapshot: CoinGlassSnapshot dataclass
        """
        if not self._pool:
            return
        try:
            ts = datetime.now(timezone.utc)
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO ticks_coinglass (
                        ts, asset,
                        oi_usd, oi_delta_pct,
                        liq_long_usd, liq_short_usd,
                        long_pct, short_pct,
                        top_long_pct, top_short_pct,
                        taker_buy_usd, taker_sell_usd,
                        funding_rate, long_short_ratio, top_position_ratio
                    ) VALUES (
                        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15
                    )
                    """,
                    ts,
                    asset,
                    _f(snapshot, "oi_usd"),
                    _f(snapshot, "oi_delta_pct_1m"),
                    _f(snapshot, "liq_long_usd_1m"),
                    _f(snapshot, "liq_short_usd_1m"),
                    _f(snapshot, "long_pct"),
                    _f(snapshot, "short_pct"),
                    _f(snapshot, "top_position_long_pct"),
                    _f(snapshot, "top_position_short_pct"),
                    _f(snapshot, "taker_buy_volume_1m"),
                    _f(snapshot, "taker_sell_volume_1m"),
                    _f(snapshot, "funding_rate"),
                    _f(snapshot, "long_short_ratio"),
                    _f(snapshot, "top_position_ratio"),
                )
            log.debug("tick_recorder.coinglass_written", asset=asset)
        except Exception as exc:
            log.debug("tick_recorder.record_coinglass.error", asset=asset, error=str(exc))

    async def record_gamma_price(self, window: Any) -> None:
        """
        Write a Gamma/Polymarket window price to ticks_gamma.

        Fire-and-forget — never blocks or raises.

        Args:
            window: WindowInfo dataclass from Polymarket5MinFeed
        """
        if not self._pool:
            return
        try:
            ts = datetime.now(timezone.utc)
            duration = getattr(window, "duration_secs", 300)
            timeframe = "15m" if duration == 900 else "5m"
            slug = (
                f"{window.asset.lower()}-updown-{timeframe}-{window.window_ts}"
            )
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO ticks_gamma (
                        ts, asset, timeframe, window_ts,
                        up_price, down_price, price_source,
                        up_token_id, down_token_id, slug
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                    """,
                    ts,
                    window.asset,
                    timeframe,
                    int(window.window_ts),
                    _fv(window, "up_price"),
                    _fv(window, "down_price"),
                    getattr(window, "price_source", "gamma_api"),
                    getattr(window, "up_token_id", None),
                    getattr(window, "down_token_id", None),
                    slug,
                )
            log.debug(
                "tick_recorder.gamma_written",
                asset=window.asset,
                window_ts=window.window_ts,
            )
        except Exception as exc:
            log.debug(
                "tick_recorder.record_gamma.error",
                error=str(exc),
            )

    async def record_timesfm_forecast(
        self,
        forecast: Any,
        asset: str = "BTC",
        window_ts: Optional[int] = None,
        window_close_ts: Optional[int] = None,
        seconds_to_close: Optional[int] = None,
    ) -> None:
        """
        Write a TimesFM forecast to ticks_timesfm.

        Fire-and-forget — never blocks or raises.

        Args:
            forecast:         TimesFMForecast dataclass
            asset:            e.g. "BTC"
            window_ts:        Window open Unix timestamp
            window_close_ts:  Window close Unix timestamp
            seconds_to_close: Seconds remaining until window close (= horizon used)
        """
        if not self._pool:
            return
        try:
            ts = datetime.now(timezone.utc)
            _horizon = getattr(forecast, "horizon", None) or seconds_to_close
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO ticks_timesfm (
                        ts, asset, window_ts, window_close_ts,
                        seconds_to_close, horizon,
                        direction, confidence, predicted_close,
                        spread, p10, p50, p90,
                        delta_vs_open, fetch_latency_ms, is_stale
                    ) VALUES (
                        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16
                    )
                    """,
                    ts,
                    asset,
                    window_ts,
                    window_close_ts,
                    seconds_to_close,
                    _horizon,
                    getattr(forecast, "direction", None),
                    _fv(forecast, "confidence"),
                    _fv(forecast, "predicted_close"),
                    _fv(forecast, "spread"),
                    _fv(forecast, "p10"),
                    _fv(forecast, "p50"),
                    _fv(forecast, "p90"),
                    _fv(forecast, "delta_vs_open_pct"),
                    _fv(forecast, "fetch_latency_ms"),
                    bool(getattr(forecast, "is_stale", False)),
                )
            log.debug(
                "tick_recorder.timesfm_written",
                asset=asset,
                direction=getattr(forecast, "direction", "?"),
                confidence=getattr(forecast, "confidence", 0),
            )
        except Exception as exc:
            log.debug(
                "tick_recorder.record_timesfm.error",
                error=str(exc),
            )

    # ─── Internal Flush Loop ──────────────────────────────────────────────────

    async def _flush_loop(self) -> None:
        """Background task: flush Binance buffer every 1 second."""
        while self._running:
            try:
                await asyncio.sleep(1.0)
                await self._flush_binance_buffer()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.debug("tick_recorder.flush_loop.error", error=str(exc))

    async def _flush_binance_buffer(self) -> None:
        """Drain the Binance buffer and batch INSERT all rows."""
        if not self._pool or not self._binance_buffer:
            return
        try:
            # Swap buffer atomically (CPython GIL makes this safe)
            batch, self._binance_buffer = self._binance_buffer, []
            if not batch:
                return
            async with self._pool.acquire() as conn:
                await conn.executemany(
                    """
                    INSERT INTO ticks_binance
                        (ts, asset, price, quantity, is_buyer_maker, vpin)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    batch,
                )
            log.debug("tick_recorder.binance_flushed", rows=len(batch))
        except Exception as exc:
            log.debug("tick_recorder.flush_binance.error", error=str(exc))


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _f(obj: Any, attr: str) -> Optional[float]:
    """Safely get a float attribute from an object."""
    try:
        v = getattr(obj, attr, None)
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _fv(obj: Any, attr: str) -> Optional[float]:
    """Alias for _f (values from dataclass fields)."""
    return _f(obj, attr)
