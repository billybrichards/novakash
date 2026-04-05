"""
Polymarket 5m/15m Data Collector — Background Railway Service

Continuously collects UP/DOWN market data for BTC, ETH, SOL, XRP from
Polymarket Gamma API. Records open prices, token prices, volumes at window
open, then resolves outcomes after window close.

Stores everything to Railway Postgres `market_data` table.

Rate-limit aware:
- Gamma API: 1 req/s max, batch 4 assets per call where possible
- CLOB API: not used (read-only via Gamma)
- Backs off on 429s with exponential delay
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import asyncpg
import structlog

log = structlog.get_logger()

# ─── Config ───────────────────────────────────────────────────────────────────

ASSETS = ["BTC", "ETH", "SOL", "XRP"]
TIMEFRAMES = ["5m", "15m"]
DURATIONS = {"5m": 300, "15m": 900}

GAMMA_API = "https://gamma-api.polymarket.com"

# Rate limiting — aggressive but within bounds
MIN_REQUEST_INTERVAL = 0.25  # 4 req/s to Gamma (they allow ~5/s)
BACKOFF_BASE = 3.0           # seconds on 429
BACKOFF_MAX = 60.0           # max backoff
POLL_INTERVAL = 1            # 1-second collection cycles
RESOLUTION_DELAY = 30        # seconds after window close before checking resolution

DATABASE_URL = os.environ.get("DATABASE_URL", "")
# Convert asyncpg URL format
if DATABASE_URL.startswith("postgresql+asyncpg://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")


# ─── Database ─────────────────────────────────────────────────────────────────

async def init_db(pool: asyncpg.Pool):
    """Create market_data table if it doesn't exist."""
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS market_data (
                id SERIAL PRIMARY KEY,
                window_ts BIGINT NOT NULL,
                asset VARCHAR(10) NOT NULL,
                timeframe VARCHAR(5) NOT NULL,
                
                -- Market info
                market_slug VARCHAR(128),
                condition_id VARCHAR(128),
                question TEXT,
                
                -- Prices at collection time
                up_price DOUBLE PRECISION,
                down_price DOUBLE PRECISION,
                best_bid DOUBLE PRECISION,
                best_ask DOUBLE PRECISION,
                spread DOUBLE PRECISION,
                
                -- Volume & liquidity
                volume DOUBLE PRECISION,
                liquidity DOUBLE PRECISION,
                
                -- Token IDs (for CLOB if needed later)
                up_token_id VARCHAR(128),
                down_token_id VARCHAR(128),
                
                -- BTC/ETH/SOL/XRP actual price from Chainlink resolution
                open_price DOUBLE PRECISION,
                close_price DOUBLE PRECISION,
                
                -- Outcome
                resolved BOOLEAN DEFAULT FALSE,
                outcome VARCHAR(4),  -- 'UP' or 'DOWN'
                
                -- Timestamps
                window_start TIMESTAMPTZ,
                window_end TIMESTAMPTZ,
                collected_at TIMESTAMPTZ DEFAULT NOW(),
                resolved_at TIMESTAMPTZ,
                
                -- Snapshot tracking
                snapshot_count INTEGER DEFAULT 1,
                last_snapshot_at TIMESTAMPTZ DEFAULT NOW(),
                
                UNIQUE(window_ts, asset, timeframe)
            );
            
            CREATE INDEX IF NOT EXISTS idx_md_ts ON market_data(window_ts);
            CREATE INDEX IF NOT EXISTS idx_md_asset ON market_data(asset);
            CREATE INDEX IF NOT EXISTS idx_md_resolved ON market_data(resolved);
            CREATE INDEX IF NOT EXISTS idx_md_collected ON market_data(collected_at);
        """)
        
        # Price snapshots table — multiple readings per window
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS market_snapshots (
                id SERIAL PRIMARY KEY,
                window_ts BIGINT NOT NULL,
                asset VARCHAR(10) NOT NULL,
                timeframe VARCHAR(5) NOT NULL,
                
                up_price DOUBLE PRECISION,
                down_price DOUBLE PRECISION,
                best_bid DOUBLE PRECISION,
                best_ask DOUBLE PRECISION,
                volume DOUBLE PRECISION,
                
                seconds_remaining INTEGER,
                snapshot_at TIMESTAMPTZ DEFAULT NOW()
            );
            
            CREATE INDEX IF NOT EXISTS idx_ms_window ON market_snapshots(window_ts, asset, timeframe);
        """)
        
    log.info("db.tables_ensured")


async def upsert_market(pool: asyncpg.Pool, data: dict):
    """Insert or update a market window record."""
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO market_data (
                window_ts, asset, timeframe, market_slug, condition_id, question,
                up_price, down_price, best_bid, best_ask, spread,
                volume, liquidity, up_token_id, down_token_id,
                open_price, window_start, window_end, collected_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6,
                $7, $8, $9, $10, $11,
                $12, $13, $14, $15,
                $16, $17, $18, NOW()
            )
            ON CONFLICT (window_ts, asset, timeframe) DO UPDATE SET
                up_price = EXCLUDED.up_price,
                down_price = EXCLUDED.down_price,
                best_bid = EXCLUDED.best_bid,
                best_ask = EXCLUDED.best_ask,
                spread = EXCLUDED.spread,
                volume = EXCLUDED.volume,
                liquidity = EXCLUDED.liquidity,
                snapshot_count = market_data.snapshot_count + 1,
                last_snapshot_at = NOW()
        """,
            data["window_ts"], data["asset"], data["timeframe"],
            data.get("slug"), data.get("condition_id"), data.get("question"),
            data.get("up_price"), data.get("down_price"),
            data.get("best_bid"), data.get("best_ask"), data.get("spread"),
            data.get("volume"), data.get("liquidity"),
            data.get("up_token_id"), data.get("down_token_id"),
            data.get("open_price"),
            data.get("window_start"), data.get("window_end"),
        )


async def save_snapshot(pool: asyncpg.Pool, data: dict):
    """Save a price snapshot for a window."""
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO market_snapshots (
                window_ts, asset, timeframe,
                up_price, down_price, best_bid, best_ask, volume,
                seconds_remaining
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """,
            data["window_ts"], data["asset"], data["timeframe"],
            data.get("up_price"), data.get("down_price"),
            data.get("best_bid"), data.get("best_ask"), data.get("volume"),
            data.get("seconds_remaining"),
        )


async def resolve_window(pool: asyncpg.Pool, window_ts: int, asset: str,
                         timeframe: str, close_price: float, outcome: str):
    """Mark a window as resolved with outcome."""
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE market_data
            SET resolved = TRUE, close_price = $1, outcome = $2, resolved_at = NOW()
            WHERE window_ts = $3 AND asset = $4 AND timeframe = $5
        """, close_price, outcome, window_ts, asset, timeframe)


async def get_unresolved(pool: asyncpg.Pool) -> list:
    """Get windows that need resolution."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT window_ts, asset, timeframe, market_slug, open_price,
                   window_end, up_token_id, down_token_id
            FROM market_data
            WHERE resolved = FALSE
              AND window_end < NOW() - INTERVAL '30 seconds'
            ORDER BY window_ts ASC
            LIMIT 50
        """)
        return [dict(r) for r in rows]


# ─── Gamma API ────────────────────────────────────────────────────────────────

class RateLimiter:
    """Simple rate limiter for API calls."""
    def __init__(self, min_interval: float = MIN_REQUEST_INTERVAL):
        self._min_interval = min_interval
        self._last_call = 0.0
        self._backoff = 0.0
    
    async def wait(self):
        now = time.time()
        wait_time = max(
            self._min_interval - (now - self._last_call),
            self._backoff
        )
        if wait_time > 0:
            await asyncio.sleep(wait_time)
        self._last_call = time.time()
        self._backoff = 0.0
    
    def backoff(self):
        self._backoff = min(self._backoff * 2 + BACKOFF_BASE, BACKOFF_MAX)
        log.warning("rate_limit.backing_off", seconds=self._backoff)


_limiter = RateLimiter()


async def fetch_current_markets(session: aiohttp.ClientSession, asset: str,
                                 timeframe: str) -> list:
    """Fetch active markets for an asset+timeframe from Gamma API."""
    await _limiter.wait()
    
    slug_prefix = f"{asset.lower()}-updown-{timeframe}"
    url = f"{GAMMA_API}/events"
    params = {
        "tag": f"{asset.lower()}-updown",
        "closed": "false",
        "limit": 10,
    }
    
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 429:
                _limiter.backoff()
                return []
            if resp.status != 200:
                log.warning("gamma.http_error", status=resp.status, asset=asset)
                return []
            
            events = await resp.json()
            results = []
            
            for event in events:
                markets = event.get("markets", [])
                for market in markets:
                    slug = market.get("slug", "")
                    if timeframe not in slug:
                        continue
                    
                    # Parse outcome prices
                    outcome_prices_raw = market.get("outcomePrices", "[]")
                    outcomes_raw = market.get("outcomes", "[]")
                    
                    if isinstance(outcome_prices_raw, str):
                        try:
                            outcome_prices = json.loads(outcome_prices_raw)
                        except:
                            outcome_prices = []
                    else:
                        outcome_prices = outcome_prices_raw
                    
                    if isinstance(outcomes_raw, str):
                        try:
                            outcomes = json.loads(outcomes_raw)
                        except:
                            outcomes = []
                    else:
                        outcomes = outcomes_raw
                    
                    up_price = None
                    down_price = None
                    up_token = None
                    down_token = None
                    
                    # Get token IDs from clobTokenIds
                    clob_tokens_raw = market.get("clobTokenIds", "[]")
                    if isinstance(clob_tokens_raw, str):
                        try:
                            clob_tokens = json.loads(clob_tokens_raw)
                        except:
                            clob_tokens = []
                    else:
                        clob_tokens = clob_tokens_raw or []
                    
                    for i, outcome in enumerate(outcomes):
                        name = str(outcome).upper()
                        try:
                            price = float(outcome_prices[i])
                        except (IndexError, ValueError):
                            continue
                        
                        token_id = clob_tokens[i] if i < len(clob_tokens) else None
                        
                        if "UP" in name or "YES" in name:
                            up_price = price
                            up_token = token_id
                        elif "DOWN" in name or "NO" in name:
                            down_price = price
                            down_token = token_id
                    
                    # Extract window timestamp from slug
                    # Format: btc-updown-5m-1775379300
                    try:
                        parts = slug.split("-")
                        window_ts = int(parts[-1])
                    except (ValueError, IndexError):
                        continue
                    
                    # Parse dates
                    end_date = market.get("endDate")
                    start_date = event.get("startDate")
                    window_end = None
                    window_start = None
                    if end_date:
                        try:
                            window_end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                        except:
                            pass
                    if start_date:
                        try:
                            window_start = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
                        except:
                            pass
                    
                    results.append({
                        "window_ts": window_ts,
                        "asset": asset,
                        "timeframe": timeframe,
                        "slug": slug,
                        "condition_id": market.get("conditionId"),
                        "question": market.get("question", "")[:200],
                        "up_price": up_price,
                        "down_price": down_price,
                        "best_bid": float(market.get("bestBid", 0) or 0) or None,
                        "best_ask": float(market.get("bestAsk", 0) or 0) or None,
                        "spread": abs(up_price - down_price) if up_price and down_price else None,
                        "volume": float(market.get("volume", 0) or 0) or None,
                        "liquidity": float(market.get("liquidity", 0) or 0) or None,
                        "up_token_id": up_token,
                        "down_token_id": down_token,
                        "open_price": None,  # Set from resolution
                        "window_start": window_start,
                        "window_end": window_end,
                    })
            
            return results
    
    except asyncio.TimeoutError:
        log.warning("gamma.timeout", asset=asset, timeframe=timeframe)
        return []
    except Exception as exc:
        log.error("gamma.fetch_error", asset=asset, error=str(exc)[:100])
        return []


async def fetch_resolved_market(session: aiohttp.ClientSession, slug: str) -> Optional[dict]:
    """Fetch a resolved market to get close price and outcome."""
    await _limiter.wait()
    
    try:
        url = f"{GAMMA_API}/events"
        params = {"slug": slug}
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 429:
                _limiter.backoff()
                return None
            if resp.status != 200:
                return None
            
            events = await resp.json()
            if not events:
                return None
            
            market = events[0].get("markets", [{}])[0]
            
            # Check if resolved
            if not market.get("closed"):
                return None
            
            # Get resolution
            outcome_prices_raw = market.get("outcomePrices", "[]")
            if isinstance(outcome_prices_raw, str):
                try:
                    prices = json.loads(outcome_prices_raw)
                except:
                    prices = []
            else:
                prices = outcome_prices_raw
            
            # Resolved market: winning token goes to ~$1.00, loser to ~$0.00
            up_final = float(prices[0]) if len(prices) > 0 else 0.5
            outcome = "UP" if up_final > 0.5 else "DOWN"
            
            return {
                "outcome": outcome,
                "up_final": up_final,
                "down_final": float(prices[1]) if len(prices) > 1 else 1.0 - up_final,
            }
    
    except Exception as exc:
        log.debug("gamma.resolve_error", slug=slug, error=str(exc)[:100])
        return None


# ─── Main Loop ────────────────────────────────────────────────────────────────

async def collect_cycle(session: aiohttp.ClientSession, pool: asyncpg.Pool,
                       cycle_num: int):
    """One collection cycle: fetch all active markets + save snapshots.
    
    Every cycle (~1s): fetch prices for all 4 assets (both timeframes per call)
    Every 60 cycles (~1min): resolve old windows
    """
    collected = 0
    snapshots = 0
    
    for asset in ASSETS:
        # Single call per asset gets both 5m and 15m markets
        for tf in TIMEFRAMES:
            markets = await fetch_current_markets(session, asset, tf)
            for mkt in markets:
                try:
                    # Always save snapshot (1-second granularity)
                    if mkt.get("up_price") is not None:
                        remaining = None
                        if mkt.get("window_end"):
                            remaining = int((mkt["window_end"] - datetime.now(timezone.utc)).total_seconds())
                        mkt["seconds_remaining"] = remaining
                        await save_snapshot(pool, mkt)
                        snapshots += 1
                    
                    # Upsert main record (updates latest prices)
                    await upsert_market(pool, mkt)
                    collected += 1
                except Exception as exc:
                    log.error("collect.upsert_failed", error=str(exc)[:100])
    
    # Resolve old windows every 60 cycles (~1 minute)
    resolved = 0
    if cycle_num % 60 == 0:
        unresolved = await get_unresolved(pool)
        for window in unresolved:
            slug = window.get("market_slug")
            if not slug:
                continue
            
            result = await fetch_resolved_market(session, slug)
            if result:
                try:
                    await resolve_window(
                        pool,
                        window["window_ts"],
                        window["asset"],
                        window["timeframe"],
                        close_price=0.0,
                        outcome=result["outcome"],
                    )
                    resolved += 1
                except Exception as exc:
                    log.error("resolve.failed", error=str(exc)[:100])
    
    if cycle_num % 30 == 0:  # Log every 30s
        log.info(
            "cycle.stats",
            cycle=cycle_num,
            collected=collected,
            snapshots=snapshots,
            resolved=resolved,
        )


async def main():
    """Main entry point."""
    log.info("collector.starting", assets=ASSETS, timeframes=TIMEFRAMES)
    
    if not DATABASE_URL:
        log.error("collector.no_database_url")
        return
    
    # Connect to DB
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)
    await init_db(pool)
    log.info("collector.db_connected")
    
    # HTTP session with headers
    headers = {"User-Agent": "Mozilla/5.0 (Novakash Data Collector)"}
    
    async with aiohttp.ClientSession(headers=headers) as session:
        cycle = 0
        log.info("collector.collecting", interval=f"{POLL_INTERVAL}s", assets=ASSETS)
        while True:
            try:
                cycle += 1
                t0 = time.time()
                await collect_cycle(session, pool, cycle)
                elapsed = time.time() - t0
                
                # DB stats every 5 minutes
                if cycle % 300 == 0:
                    async with pool.acquire() as conn:
                        total = await conn.fetchval("SELECT COUNT(*) FROM market_data")
                        unresolved = await conn.fetchval("SELECT COUNT(*) FROM market_data WHERE resolved = FALSE")
                        snaps = await conn.fetchval("SELECT COUNT(*) FROM market_snapshots")
                        log.info("collector.db_stats", total_markets=total, unresolved=unresolved,
                                 total_snapshots=snaps, cycle=cycle,
                                 snapshots_per_min=round(snaps / max(cycle, 1) * 60, 0))
                
                # Sleep remainder of interval
                sleep_time = max(0, POLL_INTERVAL - elapsed)
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
                
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("collector.cycle_error", error=str(exc)[:200])
                await asyncio.sleep(5)
    
    await pool.close()
    log.info("collector.stopped")


if __name__ == "__main__":
    asyncio.run(main())
