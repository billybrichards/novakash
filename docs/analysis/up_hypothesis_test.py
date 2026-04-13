#!/usr/bin/env python3
"""UP Strategy Research Analysis - Run all hypotheses from UP_STRATEGY_RESEARCH_BRIEF.md"""

import asyncio
import asyncpg
from typing import Optional

DB_URL = "postgresql://postgres:wKbsHjsWoWaUKkzSqgCUIijtnOKHIcQj@hopper.proxy.rlwy.net:35772/railway"


async def run_query(conn, name: str, sql: str):
    """Run a query and print results."""
    print(f"\n{'=' * 60}")
    print(f"HYPOTHESIS: {name}")
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
        # BASELINE: Confirm current UP/WR state
        baseline_sql = """
        SELECT
            v2_direction,
            COUNT(*) n,
            ROUND(100.0 * SUM(CASE WHEN
                (v2_direction='UP' AND ws.close_price > ws.open_price)
                THEN 1 ELSE 0 END)::numeric / COUNT(*), 1) AS wr
        FROM signal_evaluations se
        JOIN window_snapshots ws ON se.window_ts=ws.window_ts::bigint AND se.asset=ws.asset
        WHERE se.asset='BTC' AND se.eval_offset BETWEEN 90 AND 150
          AND ws.close_price > 0 AND ws.open_price > 0
          AND ABS(COALESCE(se.v2_probability_up,0.5)-0.5) >= 0.10
        GROUP BY 1;
        """
        await run_query(conn, "BASELINE: Current UP/WR", baseline_sql)

        # H1: Post-Cascade Bounce - large liquidation → UP bounce
        h1_sql = """
        WITH eval_with_cg AS (
            SELECT se.window_ts, se.v2_direction,
                   ws.open_price, ws.close_price,
                   cg.liq_long_usd, cg.liq_short_usd
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
            CASE WHEN liq_long_usd > 10000000 THEN 'huge_liq_long'
                 WHEN liq_long_usd > 5000000 THEN 'large_liq_long'
                 WHEN liq_long_usd > 1000000 THEN 'med_liq_long'
                 ELSE 'low_liq' END AS liq_band,
            COUNT(*) n,
            ROUND(100.0 * SUM(CASE WHEN close_price > open_price THEN 1 ELSE 0 END)::numeric / COUNT(*), 1) AS up_wr
        FROM eval_with_cg
        GROUP BY 1 ORDER BY up_wr DESC;
        """
        await run_query(conn, "H1: Post-Cascade Bounce (long liquidations)", h1_sql)

        # H2: Extreme Negative Funding = Short Squeeze
        h2_sql = """
        WITH eval_with_cg AS (
            SELECT se.window_ts, se.v2_direction,
                   ws.open_price, ws.close_price,
                   cg.funding_rate, cg.taker_buy_usd, cg.taker_sell_usd
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
            CASE WHEN funding_rate < -0.001 THEN 'very_negative'
                 WHEN funding_rate < 0 THEN 'negative'
                 WHEN funding_rate < 0.001 THEN 'neutral'
                 ELSE 'positive' END AS funding_band,
            COUNT(*) n,
            ROUND(100.0 * SUM(CASE WHEN close_price > open_price THEN 1 ELSE 0 END)::numeric / COUNT(*), 1) AS up_wr
        FROM eval_with_cg
        GROUP BY 1 ORDER BY up_wr DESC;
        """
        await run_query(conn, "H2: Extreme Negative Funding → Short Squeeze", h2_sql)

        # H3: Taker Buy Dominance
        h3_sql = """
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
            ROUND(100.0 * taker_buy_usd / NULLIF(taker_buy_usd + taker_sell_usd, 0), 1) AS taker_buy_pct,
            COUNT(*) n,
            ROUND(100.0 * SUM(CASE WHEN close_price > open_price THEN 1 ELSE 0 END)::numeric / COUNT(*), 1) AS up_wr
        FROM eval_with_cg
        WHERE taker_buy_usd > 0 AND taker_sell_usd > 0
        GROUP BY 1 ORDER BY 1;
        """
        await run_query(conn, "H3: Taker Buy Dominance", h3_sql)

        # H4: L/S Ratio Extreme (Mean Reversion)
        h4_sql = """
        WITH eval_with_cg AS (
            SELECT se.window_ts, se.v2_direction,
                   ws.open_price, ws.close_price,
                   cg.long_short_ratio
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
            CASE WHEN long_short_ratio < 0.90 THEN 'extreme_short'
                 WHEN long_short_ratio < 1.0 THEN 'mild_short'
                 WHEN long_short_ratio < 1.1 THEN 'balanced'
                 ELSE 'long_biased' END AS ls_band,
            COUNT(*) n,
            ROUND(100.0 * SUM(CASE WHEN close_price > open_price THEN 1 ELSE 0 END)::numeric / COUNT(*), 1) AS up_wr
        FROM eval_with_cg
        GROUP BY 1 ORDER BY up_wr DESC;
        """
        await run_query(conn, "H4: Long/Short Ratio Extreme", h4_sql)

        # H6: V3 Composite Score Positive
        h6_sql = """
        WITH v3_joined AS (
            SELECT se.window_ts, se.v2_direction,
                   ws.open_price, ws.close_price,
                   v3.composite_score, v3.cascade_signal, v3.vpin_signal
            FROM signal_evaluations se
            JOIN window_snapshots ws ON se.window_ts=ws.window_ts::bigint AND se.asset=ws.asset
            JOIN LATERAL (
                SELECT composite_score, cascade_signal, vpin_signal
                FROM ticks_v3_composite
                WHERE asset='BTC' AND timescale='5m'
                  AND ts BETWEEN TO_TIMESTAMP(se.window_ts - 300) AND TO_TIMESTAMP(se.window_ts + 30)
                ORDER BY ts DESC LIMIT 1
            ) v3 ON true
            WHERE se.asset='BTC' AND se.eval_offset BETWEEN 90 AND 150
              AND ws.close_price > 0 AND ws.open_price > 0
              AND se.v2_direction = 'UP'
        )
        SELECT
            CASE WHEN composite_score > 0.5 THEN 'strong_up'
                 WHEN composite_score > 0 THEN 'mild_up'
                 WHEN composite_score > -0.5 THEN 'mild_down'
                 ELSE 'strong_down' END AS v3_band,
            COUNT(*) n,
            ROUND(100.0 * SUM(CASE WHEN close_price > open_price THEN 1 ELSE 0 END)::numeric / COUNT(*), 1) AS up_wr
        FROM v3_joined
        GROUP BY 1 ORDER BY up_wr DESC;
        """
        await run_query(conn, "H6: V3 Composite Score Positive", h6_sql)

        # H11: High V4 Conviction UP (dist >= 0.20)
        h11_sql = """
        SELECT
            CASE WHEN ABS(COALESCE(se.v2_probability_up,0.5)-0.5) >= 0.20 THEN 'high_conv'
                 WHEN ABS(COALESCE(se.v2_probability_up,0.5)-0.5) >= 0.15 THEN 'med_conv'
                 ELSE 'low_conv' END AS conv_band,
            COUNT(*) n,
            ROUND(100.0 * SUM(CASE WHEN close_price > open_price THEN 1 ELSE 0 END)::numeric / COUNT(*), 1) AS up_wr
        FROM signal_evaluations se
        JOIN window_snapshots ws ON se.window_ts=ws.window_ts::bigint AND se.asset=ws.asset
        WHERE se.asset='BTC' AND se.eval_offset BETWEEN 90 AND 150
          AND ws.close_price > 0 AND ws.open_price > 0
          AND se.v2_direction = 'UP'
          AND ABS(COALESCE(se.v2_probability_up,0.5)-0.5) >= 0.10
        GROUP BY 1 ORDER BY up_wr DESC;
        """
        await run_query(conn, "H11: High V4 Conviction UP", h11_sql)

        # Time-of-day analysis
        tod_sql = """
        SELECT
            EXTRACT(HOUR FROM se.evaluated_at)::int AS hour_utc,
            se.v2_direction,
            COUNT(*) n,
            ROUND(100.0 * SUM(CASE WHEN
                (se.v2_direction='UP' AND ws.close_price > ws.open_price)
                THEN 1 ELSE 0 END)::numeric / COUNT(*), 1) AS wr
        FROM signal_evaluations se
        JOIN window_snapshots ws ON se.window_ts=ws.window_ts::bigint AND se.asset=ws.asset
        WHERE se.asset='BTC' AND se.eval_offset BETWEEN 90 AND 150
          AND ws.close_price > 0 AND ws.open_price > 0
          AND se.v2_direction = 'UP'
        GROUP BY 1, 2 ORDER BY 1;
        """
        await run_query(conn, "Time-of-Day Analysis (UP only)", tod_sql)

        print("\n" + "=" * 60)
        print("ANALYSIS COMPLETE - Review results above")
        print("=" * 60)

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
