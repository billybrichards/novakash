"""TimesFM buffer seeder — cold-start fix for ML prediction service.

After engine restart, the TimesFM ML box needs ~900 ticks (~15 min of
1-second data) before its LGB predictions are accurate. This module
queries ``ticks_binance`` for the last N minutes of 1-second price
averages and POSTs them to ``{TIMESFM_URL}/v4/seed`` so the price
buffer is pre-filled and predictions are usable immediately.

The ``/v4/seed`` endpoint may not exist yet on the ML box. A 404 is
handled gracefully (warn + skip). All errors are non-fatal — the
engine starts normally regardless.
"""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)


async def seed_timesfm_buffer(
    db_pool,
    timesfm_base_url: str,
    lookback_minutes: int = 120,
    asset: str = "BTC",
) -> int:
    """Seed the TimesFM price buffer from recent ticks_binance data.

    Parameters
    ----------
    db_pool : asyncpg.Pool
        Database connection pool for querying ticks_binance.
    timesfm_base_url : str
        Base URL of the TimesFM service (e.g. ``http://16.52.14.182:8080``).
    lookback_minutes : int
        How many minutes of history to send (default 20 = 1200 ticks).
    asset : str
        Asset to seed (default ``BTC``).

    Returns
    -------
    int
        Number of price points sent. 0 if seeding was skipped or failed.
    """
    import aiohttp

    # 1. Query recent 1-second price averages from ticks_binance.
    query = """
        SELECT date_trunc('second', ts) AS second,
               AVG(price)::float8        AS avg_price
          FROM ticks_binance
         WHERE ts > NOW() - make_interval(mins := $1)
           AND asset = $2
         GROUP BY 1
         ORDER BY 1 ASC
    """
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(query, lookback_minutes, asset)
    except Exception as exc:
        log.warning(
            "timesfm_seeder.db_query_failed",
            error=str(exc)[:200],
            asset=asset,
        )
        return 0

    if not rows:
        log.info(
            "timesfm_seeder.no_ticks",
            asset=asset,
            lookback_minutes=lookback_minutes,
        )
        return 0

    prices = [float(row["avg_price"]) for row in rows]

    # 2. POST to /v4/seed on the TimesFM service.
    url = f"{timesfm_base_url.rstrip('/')}/v4/seed"
    payload = {"features": {"prices": prices}, "source": "ticks_binance"}

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status == 404:
                    log.warning(
                        "timesfm_seeder.endpoint_not_found",
                        url=url,
                        status=404,
                        reason="/v4/seed not deployed yet; skipping",
                    )
                    return 0
                if resp.status != 200:
                    log.warning(
                        "timesfm_seeder.seed_http_error",
                        url=url,
                        status=resp.status,
                    )
                    return 0
                log.info(
                    "timesfm_seeder.seed_success",
                    asset=asset,
                    prices_sent=len(prices),
                    url=url,
                )
                return len(prices)
    except Exception as exc:
        log.warning(
            "timesfm_seeder.seed_request_failed",
            url=url,
            error=str(exc)[:200],
            reason="non-fatal; ML box will fill buffer from live ticks",
        )
        return 0
