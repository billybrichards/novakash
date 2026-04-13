#!/usr/bin/env python3
"""UP Strategy Research - Deep dive on medium conviction + time-of-day"""

import asyncio
import asyncpg

DB_URL = "postgresql://postgres:wKbsHjsWoWaUKkzSqgCUIijtnOKHIcQj@hopper.proxy.rlwy.net:35772/railway"


async def run_query(conn, name: str, sql: str):
    print(f"\n{'=' * 60}")
    print(f"{name}")
    print(f"{'=' * 60}")
    try:
        rows = await conn.fetch(sql)
        if not rows:
            print("  No data returned")
            return None
        for row in rows:
            print(f"  {dict(row)}")
        return rows
    except Exception as e:
        print(f"  ERROR: {e}")
        return None


async def main():
    conn = await asyncpg.connect(DB_URL)

    try:
        # H11 refined: Medium conviction by hour
        h11_hourly = """
        SELECT
            EXTRACT(HOUR FROM se.evaluated_at)::int AS hour_utc,
            COUNT(*) n,
            ROUND(100.0 * SUM(CASE WHEN close_price > open_price THEN 1 ELSE 0 END)::numeric / COUNT(*), 1) AS up_wr
        FROM signal_evaluations se
        JOIN window_snapshots ws ON se.window_ts=ws.window_ts::bigint AND se.asset=ws.asset
        WHERE se.asset='BTC' AND se.eval_offset BETWEEN 90 AND 150
          AND ws.close_price > 0 AND ws.open_price > 0
          AND se.v2_direction = 'UP'
          AND ABS(COALESCE(se.v2_probability_up,0.5)-0.5) BETWEEN 0.15 AND 0.20
        GROUP BY 1 ORDER BY up_wr DESC;
        """
        await run_query(conn, "H11: Medium Conv UP (0.15-0.20) by Hour", h11_hourly)

        # Combined: Medium conviction + best hours (13-17, 19-21 UTC)
        combined = """
        SELECT
            COUNT(*) n,
            ROUND(100.0 * SUM(CASE WHEN close_price > open_price THEN 1 ELSE 0 END)::numeric / COUNT(*), 1) AS up_wr
        FROM signal_evaluations se
        JOIN window_snapshots ws ON se.window_ts=ws.window_ts::bigint AND se.asset=ws.asset
        WHERE se.asset='BTC' AND se.eval_offset BETWEEN 90 AND 150
          AND ws.close_price > 0 AND ws.open_price > 0
          AND se.v2_direction = 'UP'
          AND ABS(COALESCE(se.v2_probability_up,0.5)-0.5) BETWEEN 0.15 AND 0.20
          AND EXTRACT(HOUR FROM se.evaluated_at) IN (13,14,15,16,17,19,20,21)
        """
        await run_query(conn, "Combined: Medium Conv + Best Hours", combined)

        # H3 fixed: Taker Buy Dominance
        h3_fixed = """
        WITH eval_with_cg AS (
            SELECT se.window_ts, se.v2_direction,
                   ws.open_price, ws.close_price,
                   cg.taker_buy_usd, cg.taker_sell_usd
            FROM signal_evaluations se
            JOIN window_snapshots ws ON se.window_ts=ws.window_ts::bigint AND se.asset=ws.asset
            JOIN LATERAL (
                SELECT * FROM ticks_coinglass WHERE asset='BTC'
                  AND ABS(EXTRACT(EPOCH FROM ts) - se.window_ts) < 120
                ORDER BY ABS(EXTRACT(EPOCH FROM ts) - se.window_ts) LIMIT 1
            ) cg ON true
            WHERE se.asset='BTC' AND se.eval_offset BETWEEN 90 AND 150
              AND ws.close_price > 0 AND ws.open_price > 0
              AND se.v2_direction = 'UP'
        )
        SELECT
            CASE WHEN taker_buy_usd / NULLIF(taker_buy_usd + taker_sell_usd, 0) > 0.60 THEN 'strong_buy'
                 WHEN taker_buy_usd / NULLIF(taker_buy_usd + taker_sell_usd, 0) > 0.55 THEN 'mild_buy'
                 WHEN taker_buy_usd / NULLIF(taker_buy_usd + taker_sell_usd, 0) > 0.50 THEN 'balanced'
                 ELSE 'sell_dom' END AS taker_band,
            COUNT(*) n,
            ROUND(100.0 * SUM(CASE WHEN close_price > open_price THEN 1 ELSE 0 END)::numeric / COUNT(*), 1) AS up_wr
        FROM eval_with_cg
        WHERE taker_buy_usd > 0 AND taker_sell_usd > 0
        GROUP BY 1 ORDER BY up_wr DESC;
        """
        await run_query(conn, "H3 Fixed: Taker Buy Dominance", h3_fixed)

        # H11 + Taker: Medium conviction + taker buy > 55%
        h11_taker = """
        WITH eval_with_cg AS (
            SELECT se.window_ts, se.v2_direction,
                   ws.open_price, ws.close_price,
                   cg.taker_buy_usd, cg.taker_sell_usd
            FROM signal_evaluations se
            JOIN window_snapshots ws ON se.window_ts=ws.window_ts::bigint AND se.asset=ws.asset
            JOIN LATERAL (
                SELECT * FROM ticks_coinglass WHERE asset='BTC'
                  AND ABS(EXTRACT(EPOCH FROM ts) - se.window_ts) < 120
                ORDER BY ABS(EXTRACT(EPOCH FROM ts) - se.window_ts) LIMIT 1
            ) cg ON true
            WHERE se.asset='BTC' AND se.eval_offset BETWEEN 90 AND 150
              AND ws.close_price > 0 AND ws.open_price > 0
              AND se.v2_direction = 'UP'
              AND ABS(COALESCE(se.v2_probability_up,0.5)-0.5) BETWEEN 0.15 AND 0.20
        )
        SELECT
            COUNT(*) n,
            ROUND(100.0 * SUM(CASE WHEN close_price > open_price THEN 1 ELSE 0 END)::numeric / COUNT(*), 1) AS up_wr
        FROM eval_with_cg
        WHERE taker_buy_usd / NULLIF(taker_buy_usd + taker_sell_usd, 0) > 0.55
        """
        await run_query(conn, "H11+Taker: Medium Conv + Taker Buy >55%", h11_taker)

        # H8: Consecutive DOWN windows → UP mean reversion
        h8 = """
        WITH window_outcomes AS (
            SELECT window_ts,
                   CASE WHEN close_price > open_price THEN 'UP' ELSE 'DOWN' END AS outcome,
                   LAG(CASE WHEN close_price > open_price THEN 'UP' ELSE 'DOWN' END, 1)
                       OVER (ORDER BY window_ts) AS prev1,
                   LAG(CASE WHEN close_price > open_price THEN 'UP' ELSE 'DOWN' END, 2)
                       OVER (ORDER BY window_ts) AS prev2
            FROM window_snapshots WHERE asset='BTC' AND close_price > 0 AND open_price > 0
            ORDER BY window_ts
        )
        SELECT prev1, prev2, COUNT(*) n,
               SUM(CASE WHEN outcome='UP' THEN 1 ELSE 0 END) AS next_up,
               ROUND(100.0 * SUM(CASE WHEN outcome='UP' THEN 1 ELSE 0 END)::numeric / COUNT(*), 1) AS up_pct
        FROM window_outcomes WHERE prev1 IS NOT NULL AND prev2 IS NOT NULL
        GROUP BY 1, 2 ORDER BY up_pct DESC;
        """
        await run_query(conn, "H8: Consecutive DOWN → UP Mean Reversion", h8)

        # H11 + Consecutive DOWN
        h11_h8 = """
        WITH window_outcomes AS (
            SELECT window_ts,
                   CASE WHEN close_price > open_price THEN 'UP' ELSE 'DOWN' END AS outcome,
                   LAG(CASE WHEN close_price > open_price THEN 'UP' ELSE 'DOWN' END, 1)
                       OVER (ORDER BY window_ts) AS prev1,
                   LAG(CASE WHEN close_price > open_price THEN 'UP' ELSE 'DOWN' END, 2)
                       OVER (ORDER BY window_ts) AS prev2
            FROM window_snapshots WHERE asset='BTC' AND close_price > 0 AND open_price > 0
            ORDER BY window_ts
        )
        SELECT COUNT(*) n,
               ROUND(100.0 * SUM(CASE WHEN outcome='UP' THEN 1 ELSE 0 END)::numeric / COUNT(*), 1) AS up_pct
        FROM window_outcomes WHERE prev1='DOWN' AND prev2='DOWN'
        """
        await run_query(conn, "H11+H8: Medium Conv + 2x DOWN Prev", h11_h8)

        print("\n" + "=" * 60)
        print("DEEP DIVE COMPLETE")
        print("=" * 60)

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
