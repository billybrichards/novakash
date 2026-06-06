#!/usr/bin/env python3
"""
All-probs comprehensive sweep + v4 gating audit.
2026-05-30

Methodology: window-dedup per #759 canonical (DISTINCT ON window_ts),
Wilson95 one-sided LCL (z=1.6449), 0.02-granularity threshold sweep.

Coverage: all 21 probability columns × UP + DOWN directions.
Analysis window: 2026-05-25 00:00 UTC → now (~5.5d).
Band: eval_offset 0-240 (broad), with a secondary [60,180] check for top results.
"""
import asyncio
import json
import math
import subprocess
import asyncpg
from datetime import datetime, timezone

START = datetime(2026, 5, 25, 0, 0, 0, tzinfo=timezone.utc)
ANALYSIS_DAYS = 5.5

Z = 1.6449  # Wilson 95% one-sided

def wilson95_lcl(wins: int, n: int, z: float = Z) -> float:
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return (centre - margin) / denom

# (prob_col, asset, short_name)
# v9_2_xrp has 0 rows on XRP; use BTC instead (it's a BTC model, mislabelled)
PROB_COLS = [
    ("probability_lgb_v9_1",         "BTC", "v9_1"),
    ("probability_lgb_v9_2",         "BTC", "v9_2"),
    ("probability_lgb_v9_2_eth",     "ETH", "v9_2_eth"),
    ("probability_lgb_v9_2_pure",    "BTC", "v9_2_pure"),
    ("probability_lgb_v9_2_xrp",     "BTC", "v9_2_xrp*"),   # 0 rows on XRP; BTC fallback
    ("probability_lgb_v9_2_post_iso","BTC", "v9_2_post_iso"),
    ("probability_lgb_v9_3_btc",     "BTC", "v9_3_btc"),
    ("probability_lgb_v9_3_btc_pure","BTC", "v9_3_btc_pure"),
    ("probability_lgb_v9_5_eth",     "ETH", "v9_5_eth"),
    ("probability_lgb_v9_5_eth_pure","ETH", "v9_5_eth_pure"),
    ("probability_lgb_v9_5_xrp",     "XRP", "v9_5_xrp"),
    ("probability_lgb_v9_5_xrp_pure","XRP", "v9_5_xrp_pure"),
    ("probability_lgb_v12",          "BTC", "v12"),
    ("probability_lgb_v12_pure",     "BTC", "v12_pure"),
    ("probability_tickformer_v16",   "BTC", "tf_v16"),
    ("probability_tickformer_v17",   "BTC", "tf_v17"),
    ("probability_tickformer_v18",   "BTC", "tf_v18"),
    ("probability_tickformer_v20",   "BTC", "tf_v20"),
    ("probability_v9_2_meta_gate",   "BTC", "v9_2_mg"),
    ("probability_v12_meta_gate",    "BTC", "v12_mg"),
    ("probability_v2_meta_gate",     "BTC", "v2_mg"),
]

# Threshold sweep ranges
UP_THRESHOLDS   = [round(x * 0.02, 2) for x in range(30, 51)]  # 0.60 → 1.00
DOWN_THRESHOLDS = [round(x * 0.02, 2) for x in range(20, 1, -1)]  # 0.40 → 0.02

BAND_MIN = 0
BAND_MAX = 240


async def get_password():
    return json.loads(subprocess.check_output(
        ["aws", "secretsmanager", "get-secret-value",
         "--secret-id", "novakash/rds/postgres-master",
         "--region", "ca-central-1",
         "--query", "SecretString", "--output", "text"],
        text=True
    ))["password"]


async def sweep_col(conn, prob_col: str, asset: str, short: str):
    """Window-dedup sweep for a single probability column."""
    q = f"""
      WITH wp AS (
        SELECT se.window_ts,
          MIN(se.{prob_col}::float) FILTER (
            WHERE (se.window_ts + 300 - extract(epoch from se.evaluated_at)::bigint)
                   BETWEEN $2 AND $3
          ) AS pmin,
          MAX(se.{prob_col}::float) FILTER (
            WHERE (se.window_ts + 300 - extract(epoch from se.evaluated_at)::bigint)
                   BETWEEN $2 AND $3
          ) AS pmax
        FROM signal_evaluations se
        WHERE se.evaluated_at >= $1::timestamptz
          AND se.{prob_col} IS NOT NULL
          AND se.asset = $4
          AND se.timeframe = '5m'
        GROUP BY se.window_ts
      ),
      outc AS (
        SELECT DISTINCT ON (window_ts) window_ts, outcome
          FROM signal_evaluations
         WHERE asset = $4
           AND timeframe = '5m'
           AND outcome IN ('UP', 'DOWN')
         ORDER BY window_ts, evaluated_at DESC
      )
      SELECT wp.pmin, wp.pmax, outc.outcome
        FROM wp
        JOIN outc USING (window_ts)
       WHERE wp.pmin IS NOT NULL AND wp.pmax IS NOT NULL
    """
    rows = await conn.fetch(q, START, BAND_MIN, BAND_MAX, asset)
    total_windows = len(rows)
    if total_windows == 0:
        return short, asset, total_windows, [], []

    fires_per_day_base = total_windows / ANALYSIS_DAYS

    # Sweep UP: fire when pmax >= thr; win when outcome=UP
    up_results = []
    for thr in UP_THRESHOLDS:
        qualified = [(r["pmin"], r["pmax"], r["outcome"]) for r in rows if r["pmax"] is not None and r["pmax"] >= thr]
        n = len(qualified)
        wins = sum(1 for _, _, o in qualified if o == "UP")
        wr = wins / n * 100 if n > 0 else 0.0
        lcl = wilson95_lcl(wins, n) * 100
        fires_24h = n / ANALYSIS_DAYS
        up_results.append((thr, n, wins, wr, lcl, fires_24h))

    # Sweep DOWN: fire when pmin <= thr; win when outcome=DOWN
    down_results = []
    for thr in DOWN_THRESHOLDS:
        qualified = [(r["pmin"], r["pmax"], r["outcome"]) for r in rows if r["pmin"] is not None and r["pmin"] <= thr]
        n = len(qualified)
        wins = sum(1 for _, _, o in qualified if o == "DOWN")
        wr = wins / n * 100 if n > 0 else 0.0
        lcl = wilson95_lcl(wins, n) * 100
        fires_24h = n / ANALYSIS_DAYS
        down_results.append((thr, n, wins, wr, lcl, fires_24h))

    return short, asset, total_windows, up_results, down_results


def best_up(results):
    """Find best UP result by LCL with n>=30."""
    valid = [(thr, n, wins, wr, lcl, f24) for thr, n, wins, wr, lcl, f24 in results if n >= 30]
    if not valid:
        return None
    return max(valid, key=lambda x: x[4])  # max LCL


def best_down(results):
    """Find best DOWN result by LCL with n>=30."""
    valid = [(thr, n, wins, wr, lcl, f24) for thr, n, wins, wr, lcl, f24 in results if n >= 30]
    if not valid:
        return None
    return max(valid, key=lambda x: x[4])  # max LCL


def format_sweep_table(all_col_results):
    """Format the summary table for all columns."""
    lines = []
    lines.append(f"\n{'col':<20} {'asset':<5} {'dir':<5} {'thr':>5} {'n':>5} {'wins':>5} {'WR%':>6} {'LCL%':>6} {'f/24h':>6}")
    lines.append("-" * 72)
    for short, asset, total, up_res, down_res in all_col_results:
        bu = best_up(up_res)
        bd = best_down(down_res)
        if bu:
            thr, n, wins, wr, lcl, f24 = bu
            lines.append(f"{short:<20} {asset:<5} {'UP':<5} {thr:>5.2f} {n:>5} {wins:>5} {wr:>6.1f} {lcl:>6.1f} {f24:>6.1f}")
        else:
            lines.append(f"{short:<20} {asset:<5} {'UP':<5} {'--':>5} {'--':>5} {'--':>5} {'--':>6} {'--':>6} {'--':>6}")
        if bd:
            thr, n, wins, wr, lcl, f24 = bd
            lines.append(f"{short:<20} {asset:<5} {'DN':<5} {thr:>5.2f} {n:>5} {wins:>5} {wr:>6.1f} {lcl:>6.1f} {f24:>6.1f}")
        else:
            lines.append(f"{short:<20} {asset:<5} {'DN':<5} {'--':>5} {'--':>5} {'--':>5} {'--':>6} {'--':>6} {'--':>6}")
    return "\n".join(lines)


def format_full_sweep(short, asset, up_res, down_res):
    """Format full threshold sweep for one column."""
    lines = [f"\n### {short} ({asset})"]
    lines.append(f"  UP sweep  (fire when pmax >= thr; win when outcome=UP)")
    lines.append(f"  {'thr':>5} {'n':>5} {'wins':>5} {'WR%':>6} {'LCL%':>6} {'f/24h':>6}")
    for thr, n, wins, wr, lcl, f24 in up_res:
        marker = " <--" if n >= 30 and lcl >= 85 else ""
        lines.append(f"  {thr:>5.2f} {n:>5} {wins:>5} {wr:>6.1f} {lcl:>6.1f} {f24:>6.1f}{marker}")
    lines.append(f"  DN sweep  (fire when pmin <= thr; win when outcome=DOWN)")
    lines.append(f"  {'thr':>5} {'n':>5} {'wins':>5} {'WR%':>6} {'LCL%':>6} {'f/24h':>6}")
    for thr, n, wins, wr, lcl, f24 in down_res:
        marker = " <--" if n >= 30 and lcl >= 85 else ""
        lines.append(f"  {thr:>5.2f} {n:>5} {wins:>5} {wr:>6.1f} {lcl:>6.1f} {f24:>6.1f}{marker}")
    return "\n".join(lines)


async def main():
    pw = await get_password()
    conn = await asyncpg.connect(
        host="localhost", port=15432, user="postgres", password=pw, database="novakash"
    )

    print(f"=== ALL-PROBS SWEEP — band {BAND_MIN}-{BAND_MAX} — {START.date()} to now ===\n")

    all_results = []
    for prob_col, asset, short in PROB_COLS:
        print(f"  sweeping {short} ({asset})...")
        result = await sweep_col(conn, prob_col, asset, short)
        all_results.append(result)

    # Summary table
    print(format_sweep_table(all_results))

    # Top-10 best signal cells (highest LCL, n>=30)
    all_cells = []
    for short, asset, total, up_res, down_res in all_results:
        for thr, n, wins, wr, lcl, f24 in up_res:
            if n >= 30:
                all_cells.append((lcl, short, asset, "UP", thr, n, wins, wr, f24))
        for thr, n, wins, wr, lcl, f24 in down_res:
            if n >= 30:
                all_cells.append((lcl, short, asset, "DN", thr, n, wins, wr, f24))

    all_cells.sort(reverse=True)
    print(f"\n\n=== TOP 20 SIGNAL CELLS (n>=30, sorted by LCL) ===")
    print(f"{'rank':<5} {'col':<22} {'asset':<5} {'dir':<4} {'thr':>5} {'n':>5} {'WR%':>6} {'LCL%':>6} {'f/24h':>6}")
    print("-" * 72)
    for rank, (lcl, short, asset, direction, thr, n, wins, wr, f24) in enumerate(all_cells[:20], 1):
        print(f"{rank:<5} {short:<22} {asset:<5} {direction:<4} {thr:>5.2f} {n:>5} {wr:>6.1f} {lcl:>6.1f} {f24:>6.1f}")

    # Full sweep output per column
    print(f"\n\n=== FULL SWEEP OUTPUT PER COLUMN ===")
    for short, asset, total, up_res, down_res in all_results:
        print(format_full_sweep(short, asset, up_res, down_res))

    await conn.close()
    return all_results, all_cells


if __name__ == "__main__":
    results, cells = asyncio.run(main())
