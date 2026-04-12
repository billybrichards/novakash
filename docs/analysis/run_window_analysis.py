#!/usr/bin/env python3
"""
Trading window analysis script.
Uses price-derived ground truth (close>open=UP) for accuracy calculations.

Usage:
    export PUB_URL="postgresql://postgres:PASSWORD@hopper.proxy.rlwy.net:35772/railway"
    python3 docs/analysis/run_window_analysis.py
"""
import asyncio
import asyncpg
import os

DB = os.environ.get("PUB_URL") or os.environ.get("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")

GROUND_TRUTH = """(
    (se.v2_direction='UP' AND ws.close_price>ws.open_price)
    OR (se.v2_direction='DOWN' AND ws.close_price<ws.open_price)
)"""

BASE_WHERE = """
    se.eval_offset BETWEEN 90 AND 150
    AND se.asset='BTC'
    AND se.v2_direction IS NOT NULL
    AND ws.close_price>0 AND ws.open_price>0
    AND ABS(COALESCE(se.v2_probability_up,0.5)-0.5) >= 0.12
"""


async def run():
    c = await asyncpg.connect(DB, timeout=60)

    print("=== A1: Accuracy by eval_offset (15s buckets) ===")
    rows = await c.fetch(f"""
        SELECT FLOOR(se.eval_offset/15.0)*15 AS b, COUNT(*) n,
          ROUND(100.0*SUM(CASE WHEN {GROUND_TRUTH} THEN 1 ELSE 0 END)::numeric/COUNT(*),1) acc,
          ROUND(AVG(ABS(COALESCE(se.v2_probability_up,0.5)-0.5))::numeric,3) dist
        FROM signal_evaluations se
        JOIN window_snapshots ws ON se.window_ts=ws.window_ts::bigint AND se.asset=ws.asset
        WHERE se.eval_offset BETWEEN 30 AND 240 AND se.asset='BTC'
          AND se.v2_direction IS NOT NULL AND ws.close_price>0 AND ws.open_price>0
        GROUP BY 1 ORDER BY 1 DESC""")
    for r in rows:
        acc = float(r["acc"] or 0)
        bar = "█" * max(0, int((acc - 50) / 2))
        print(f"  T-{int(r['b']):3d}: {acc:5.1f}% dist={r['dist']} n={r['n']} {bar}")

    print()
    print("=== A2: offset x confidence band ===")
    rows2 = await c.fetch(f"""
        SELECT FLOOR(se.eval_offset/30.0)*30 AS b,
          CASE WHEN ABS(COALESCE(se.v2_probability_up,0.5)-0.5)<0.06 THEN 'weak'
               WHEN ABS(COALESCE(se.v2_probability_up,0.5)-0.5)<0.12 THEN 'mod'
               WHEN ABS(COALESCE(se.v2_probability_up,0.5)-0.5)<0.20 THEN 'strong'
               ELSE 'high' END band,
          COUNT(*) n,
          ROUND(100.0*SUM(CASE WHEN {GROUND_TRUTH} THEN 1 ELSE 0 END)::numeric/COUNT(*),1) acc
        FROM signal_evaluations se
        JOIN window_snapshots ws ON se.window_ts=ws.window_ts::bigint AND se.asset=ws.asset
        WHERE se.asset='BTC' AND se.v2_direction IS NOT NULL
          AND ws.close_price>0 AND ws.open_price>0
        GROUP BY 1,2 ORDER BY 1 DESC,2""")
    last_b = None
    for r in rows2:
        if last_b != r["b"]: print()
        last_b = r["b"]
        acc = float(r["acc"] or 0)
        bar = "█" * max(0, int((acc - 50) / 2))
        print(f"  T-{int(r['b']):3d} {r['band']:7s}: {acc:5.1f}% n={r['n']} {bar}")

    print()
    print("=== A3: CLOB ask vs accuracy ===")
    rows3 = await c.fetch(f"""
        SELECT FLOOR(se.eval_offset/30.0)*30 AS b,
          CASE WHEN ABS(COALESCE(se.v2_probability_up,0.5)-0.5)>=0.12 THEN 'hi' ELSE 'lo' END conf,
          ROUND(AVG(CASE WHEN se.v2_direction='UP' THEN se.clob_up_ask ELSE se.clob_down_ask END)::numeric,3) ask,
          ROUND(100.0*SUM(CASE WHEN {GROUND_TRUTH} THEN 1 ELSE 0 END)::numeric/COUNT(*),1) acc,
          COUNT(*) n
        FROM signal_evaluations se
        JOIN window_snapshots ws ON se.window_ts=ws.window_ts::bigint AND se.asset=ws.asset
        WHERE se.asset='BTC' AND se.v2_direction IS NOT NULL
          AND ws.close_price>0 AND ws.open_price>0
          AND (se.clob_up_ask IS NOT NULL OR se.clob_down_ask IS NOT NULL)
        GROUP BY 1,2 ORDER BY 1 DESC,2""")
    for r in rows3:
        acc = float(r["acc"] or 0)
        print(f"  T-{int(r['b']):3d} {r['conf']}: ask=${r['ask']} acc={acc:.1f}% n={r['n']}")

    await c.close()
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(run())
