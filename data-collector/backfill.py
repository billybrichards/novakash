"""
Polymarket Historical Backfill — Fetches 30 days of 5m/15m market outcomes.

Runs through every 5-min and 15-min window for BTC, ETH, SOL, XRP going back
30 days. Fetches outcome (UP/DOWN won), volume, liquidity from Gamma API.

Rate-limit aware: 1.2s between requests, backs off on 429.
Estimated runtime: ~11 hours for all 4 assets × 2 timeframes.

Can be resumed — skips windows already in DB.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone

import aiohttp
import asyncpg
import structlog

log = structlog.get_logger()

ASSETS = ["BTC", "ETH", "SOL", "XRP"]
TIMEFRAMES = {"5m": 300, "15m": 900}
GAMMA_API = "https://gamma-api.polymarket.com"
MIN_REQUEST_INTERVAL = 1.2
BACKOFF_BASE = 5.0
BACKOFF_MAX = 120.0
DAYS_BACK = 30

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgresql+asyncpg://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")


class RateLimiter:
    def __init__(self):
        self._last = 0.0
        self._backoff = 0.0
    
    async def wait(self):
        now = time.time()
        wait = max(MIN_REQUEST_INTERVAL - (now - self._last), self._backoff)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last = time.time()
        self._backoff = 0.0
    
    def backoff(self):
        self._backoff = min(self._backoff * 2 + BACKOFF_BASE, BACKOFF_MAX)
        log.warning("backoff", seconds=self._backoff)


async def init_db(pool):
    """Ensure tables exist."""
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS market_data (
                id SERIAL PRIMARY KEY,
                window_ts BIGINT NOT NULL,
                asset VARCHAR(10) NOT NULL,
                timeframe VARCHAR(5) NOT NULL,
                market_slug VARCHAR(128),
                condition_id VARCHAR(128),
                question TEXT,
                up_price DOUBLE PRECISION,
                down_price DOUBLE PRECISION,
                best_bid DOUBLE PRECISION,
                best_ask DOUBLE PRECISION,
                spread DOUBLE PRECISION,
                volume DOUBLE PRECISION,
                liquidity DOUBLE PRECISION,
                up_token_id VARCHAR(128),
                down_token_id VARCHAR(128),
                open_price DOUBLE PRECISION,
                close_price DOUBLE PRECISION,
                resolved BOOLEAN DEFAULT FALSE,
                outcome VARCHAR(4),
                window_start TIMESTAMPTZ,
                window_end TIMESTAMPTZ,
                collected_at TIMESTAMPTZ DEFAULT NOW(),
                resolved_at TIMESTAMPTZ,
                snapshot_count INTEGER DEFAULT 1,
                last_snapshot_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(window_ts, asset, timeframe)
            );
            CREATE INDEX IF NOT EXISTS idx_md_ts ON market_data(window_ts);
            CREATE INDEX IF NOT EXISTS idx_md_asset ON market_data(asset);
            CREATE INDEX IF NOT EXISTS idx_md_resolved ON market_data(resolved);
        """)
    log.info("db.ready")


async def get_existing_windows(pool, asset, timeframe):
    """Get set of window_ts already in DB."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT window_ts FROM market_data WHERE asset=$1 AND timeframe=$2",
            asset, timeframe
        )
        return {r["window_ts"] for r in rows}


async def fetch_window(session, limiter, asset, timeframe, window_ts):
    """Fetch a single resolved window from Gamma API."""
    await limiter.wait()
    
    slug = f"{asset.lower()}-updown-{timeframe}-{window_ts}"
    url = f"{GAMMA_API}/events?slug={slug}"
    
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 429:
                limiter.backoff()
                return None
            if resp.status != 200:
                return None
            
            events = await resp.json()
            if not events:
                return None
            
            event = events[0]
            market = event.get("markets", [{}])[0]
            
            # Parse outcome prices
            op_raw = market.get("outcomePrices", "[]")
            outcomes_raw = market.get("outcomes", "[]")
            
            if isinstance(op_raw, str):
                try: prices = json.loads(op_raw)
                except: prices = []
            else:
                prices = op_raw
            
            if isinstance(outcomes_raw, str):
                try: outcomes = json.loads(outcomes_raw)
                except: outcomes = []
            else:
                outcomes = outcomes_raw
            
            up_price = float(prices[0]) if len(prices) > 0 else None
            down_price = float(prices[1]) if len(prices) > 1 else None
            
            # Parse token IDs
            clob_raw = market.get("clobTokenIds", "[]")
            if isinstance(clob_raw, str):
                try: clob_tokens = json.loads(clob_raw)
                except: clob_tokens = []
            else:
                clob_tokens = clob_raw or []
            
            # Determine outcome from resolved prices
            closed = market.get("closed", False)
            outcome = None
            if closed and up_price is not None:
                outcome = "UP" if up_price > 0.5 else "DOWN"
            
            # Parse dates
            end_date = market.get("endDate")
            window_end = None
            if end_date:
                try:
                    window_end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                except:
                    pass
            
            return {
                "window_ts": window_ts,
                "asset": asset,
                "timeframe": timeframe,
                "slug": slug,
                "condition_id": market.get("conditionId"),
                "question": market.get("question", "")[:200],
                "up_price": up_price,
                "down_price": down_price,
                "best_bid": float(market.get("bestBid") or 0) or None,
                "best_ask": float(market.get("bestAsk") or 0) or None,
                "spread": abs(up_price - down_price) if up_price is not None and down_price is not None else None,
                "volume": float(market.get("volume") or 0) or None,
                "liquidity": float(market.get("liquidity") or 0) or None,
                "up_token_id": clob_tokens[0] if len(clob_tokens) > 0 else None,
                "down_token_id": clob_tokens[1] if len(clob_tokens) > 1 else None,
                "closed": closed,
                "outcome": outcome,
                "window_end": window_end,
            }
    
    except asyncio.TimeoutError:
        return None
    except Exception as exc:
        log.debug("fetch_error", slug=slug, error=str(exc)[:80])
        return None


async def store_window(pool, data):
    """Store a backfilled window."""
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO market_data (
                window_ts, asset, timeframe, market_slug, condition_id, question,
                up_price, down_price, best_bid, best_ask, spread,
                volume, liquidity, up_token_id, down_token_id,
                resolved, outcome, window_end, collected_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6,
                $7, $8, $9, $10, $11,
                $12, $13, $14, $15,
                $16, $17, $18, NOW()
            )
            ON CONFLICT (window_ts, asset, timeframe) DO NOTHING
        """,
            data["window_ts"], data["asset"], data["timeframe"],
            data.get("slug"), data.get("condition_id"), data.get("question"),
            data.get("up_price"), data.get("down_price"),
            data.get("best_bid"), data.get("best_ask"), data.get("spread"),
            data.get("volume"), data.get("liquidity"),
            data.get("up_token_id"), data.get("down_token_id"),
            data.get("closed", False), data.get("outcome"),
            data.get("window_end"),
        )


async def backfill_asset(session, pool, limiter, asset, timeframe, duration):
    """Backfill all windows for one asset+timeframe."""
    now = int(time.time())
    start = now - (DAYS_BACK * 86400)
    
    # Align to window boundary
    start = (start // duration) * duration
    
    # Get existing to skip
    existing = await get_existing_windows(pool, asset, timeframe)
    
    # Generate all window timestamps
    all_windows = []
    ts = start
    while ts < now - duration:  # Don't fetch still-active windows
        if ts not in existing:
            all_windows.append(ts)
        ts += duration
    
    total = len(all_windows)
    skipped = (now - start) // duration - total
    log.info("backfill.start", asset=asset, timeframe=timeframe,
             total=total, skipped=skipped, 
             est_minutes=round(total * MIN_REQUEST_INTERVAL / 60, 1))
    
    fetched = 0
    errors = 0
    
    for i, window_ts in enumerate(all_windows):
        data = await fetch_window(session, limiter, asset, timeframe, window_ts)
        
        if data:
            await store_window(pool, data)
            fetched += 1
        else:
            errors += 1
        
        if (i + 1) % 100 == 0:
            pct = (i + 1) / total * 100
            log.info("backfill.progress", asset=asset, timeframe=timeframe,
                     done=i+1, total=total, pct=f"{pct:.1f}%",
                     fetched=fetched, errors=errors)
    
    log.info("backfill.complete", asset=asset, timeframe=timeframe,
             fetched=fetched, errors=errors, total=total)
    return fetched


async def main():
    log.info("backfill.starting", days=DAYS_BACK, assets=ASSETS)
    
    if not DATABASE_URL:
        log.error("no DATABASE_URL")
        return
    
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)
    await init_db(pool)
    
    limiter = RateLimiter()
    headers = {"User-Agent": "Mozilla/5.0 (Novakash Backfill)"}
    
    grand_total = 0
    
    async with aiohttp.ClientSession(headers=headers) as session:
        for asset in ASSETS:
            for tf, duration in TIMEFRAMES.items():
                count = await backfill_asset(session, pool, limiter, asset, tf, duration)
                grand_total += count
    
    log.info("backfill.done", total_fetched=grand_total)
    
    # Print DB stats
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM market_data")
        resolved = await conn.fetchval("SELECT COUNT(*) FROM market_data WHERE resolved=TRUE")
        log.info("db.stats", total=total, resolved=resolved)
    
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
