#!/usr/bin/env python3
"""CASCADE-regime alpha specialist.

Prior 15h analysis flagged DOWN x CASCADE = +$135 with strong WR. This script
verifies/extends with the full 87h sample and cross-tabs CASCADE regime against:

1. Per-direction WR + real P&L (UP CASCADE worst, DOWN CASCADE best — confirm).
2. Per-T-band WR within CASCADE.
3. Per-signal quintile WR within CASCADE (which signals are most predictive
   *within* CASCADE).
4. CG liquidation magnitude correlation: does the alpha co-occur with big
   ``cg_liq_short_usd`` (UP-bias) or ``cg_liq_long_usd`` (DOWN-bias) spikes?

Uses ``signal_evaluations`` (49 cols) + ``window_snapshots`` join for outcome.

Usage
-----

    python3 scripts/ops/analysis/regime_cascade_specialist.py --hours 87
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if THIS_DIR not in sys.path:
    sys.path.insert(0, THIS_DIR)

from _common import (  # noqa: E402
    DEFAULT_HOURS, DEFAULT_STAKE_USD, DEFAULT_ASSET, DEFAULT_TIMEFRAME,
    POLY_FEE_MULT, TBAND_SQL, REGIMES, DIRECTIONS,
    connect_pg, write_output, wilson_ci,
)


CTE = """
WITH base AS (
    SELECT
        se.*,
        ({tband_sql}) AS tband
    FROM signal_evaluations se
    WHERE evaluated_at > NOW() - INTERVAL '{hours} hours'
      AND asset = '{asset}'
      AND timeframe = '{timeframe}'
      AND outcome IN ('UP','DOWN')
),
cascade AS (
    SELECT * FROM base WHERE regime = 'CASCADE'
)
""".format(tband_sql=TBAND_SQL, hours="{hours}", asset="{asset}", timeframe="{timeframe}")


# ----- per-direction -----

async def per_direction(conn, hours, asset, tf, stake) -> list:
    sql = CTE.format(hours=hours, asset=asset, timeframe=tf) + f"""
        SELECT
            outcome,
            count(*) AS n,
            avg(CASE WHEN outcome='UP' THEN clob_up_ask ELSE clob_down_ask END) AS avg_fill,
            sum(
                CASE
                    WHEN outcome='UP' AND clob_up_ask > 0 AND clob_up_ask < 1
                        THEN ({stake}*(1.0-clob_up_ask)/clob_up_ask) - {POLY_FEE_MULT*stake}
                    WHEN outcome='DOWN' AND clob_down_ask > 0 AND clob_down_ask < 1
                        THEN ({stake}*(1.0-clob_down_ask)/clob_down_ask) - {POLY_FEE_MULT*stake}
                    ELSE 0
                END
            ) AS pnl_if_perfect
        FROM cascade
        GROUP BY outcome
        ORDER BY outcome;
    """
    return await conn.fetch(sql)


async def per_tband(conn, hours, asset, tf):
    sql = CTE.format(hours=hours, asset=asset, timeframe=tf) + """
        SELECT tband, outcome, count(*) AS n
        FROM cascade
        GROUP BY tband, outcome
        ORDER BY tband, outcome;
    """
    return await conn.fetch(sql)


# ----- per-signal-quintile within CASCADE -----

NUMERIC_SCAN_COLS = [
    "v2_probability_up", "v3_composite", "v3_cascade_signal",
    "delta_pct", "vpin", "twap_delta",
    "cg_liq_short_usd", "cg_liq_long_usd", "cg_taker_buy_usd",
    "cg_taker_sell_usd", "cg_oi_delta_pct", "cg_funding_rate",
]


async def signal_quintiles_within_cascade(conn, hours, asset, tf, stake, min_n=20):
    out = []
    for col in NUMERIC_SCAN_COLS:
        sql = CTE.format(hours=hours, asset=asset, timeframe=tf) + f"""
            , binned AS (
                SELECT outcome, NTILE(5) OVER (ORDER BY {col}) AS q, {col} AS val
                FROM cascade
                WHERE {col} IS NOT NULL
            )
            SELECT q,
                   count(*) AS n,
                   count(*) FILTER (WHERE outcome='UP') AS up_w,
                   count(*) FILTER (WHERE outcome='DOWN') AS down_w,
                   avg(val) AS m
            FROM binned
            GROUP BY q
            HAVING count(*) >= {min_n}
            ORDER BY q;
        """
        try:
            rows = await conn.fetch(sql)
        except Exception as e:
            print(f"  [skip] {col}: {e}")
            continue
        for r in rows:
            n = r["n"]
            for direction, w in (("UP", r["up_w"]), ("DOWN", r["down_w"])):
                lo, hi = wilson_ci(w, n)
                if lo < 0.55:
                    continue  # only keep "winning" cells
                out.append({
                    "col": col, "q": r["q"], "mean": float(r["m"] or 0),
                    "dir": direction, "n": n, "wins": w,
                    "wr": w / n, "lo": lo, "hi": hi,
                })
    return out


# ----- CG liq magnitude vs alpha -----

async def cg_liq_magnitude_correlation(conn, hours, asset, tf, stake):
    sql = CTE.format(hours=hours, asset=asset, timeframe=tf) + """
        SELECT
            CASE
                WHEN cg_liq_short_usd >= 5e6 THEN 'short_huge'
                WHEN cg_liq_short_usd >= 1e6 THEN 'short_big'
                WHEN cg_liq_short_usd >= 100e3 THEN 'short_med'
                WHEN cg_liq_short_usd > 0 THEN 'short_small'
                ELSE 'short_zero'
            END AS short_bucket,
            count(*) AS n,
            count(*) FILTER (WHERE outcome='UP') AS up_n,
            count(*) FILTER (WHERE outcome='DOWN') AS down_n
        FROM cascade
        GROUP BY 1
        ORDER BY 1;
    """
    rows_short = await conn.fetch(sql)

    sql2 = CTE.format(hours=hours, asset=asset, timeframe=tf) + """
        SELECT
            CASE
                WHEN cg_liq_long_usd >= 5e6 THEN 'long_huge'
                WHEN cg_liq_long_usd >= 1e6 THEN 'long_big'
                WHEN cg_liq_long_usd >= 100e3 THEN 'long_med'
                WHEN cg_liq_long_usd > 0 THEN 'long_small'
                ELSE 'long_zero'
            END AS long_bucket,
            count(*) AS n,
            count(*) FILTER (WHERE outcome='UP') AS up_n,
            count(*) FILTER (WHERE outcome='DOWN') AS down_n
        FROM cascade
        GROUP BY 1
        ORDER BY 1;
    """
    rows_long = await conn.fetch(sql2)
    return rows_short, rows_long


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=int, default=DEFAULT_HOURS)
    ap.add_argument("--asset", default=DEFAULT_ASSET)
    ap.add_argument("--timeframe", default=DEFAULT_TIMEFRAME)
    ap.add_argument("--stake", type=float, default=DEFAULT_STAKE_USD)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    out_dir = args.out_dir or os.path.join("docs", "analysis", "auto", dt.date.today().isoformat())

    conn = await connect_pg()
    try:
        print("  per-direction within CASCADE...")
        pdir = await per_direction(conn, args.hours, args.asset, args.timeframe, args.stake)
        print("  per-T-band within CASCADE...")
        pband = await per_tband(conn, args.hours, args.asset, args.timeframe)
        print("  per-signal-quintile within CASCADE (winners only)...")
        sigq = await signal_quintiles_within_cascade(conn, args.hours, args.asset, args.timeframe, args.stake)
        print("  cg liq magnitude correlation...")
        cg_short, cg_long = await cg_liq_magnitude_correlation(conn, args.hours, args.asset, args.timeframe, args.stake)
    finally:
        await conn.close()

    md = [
        f"# CASCADE-Regime Specialist — {args.hours}h",
        "",
        f"- asset: {args.asset}, timeframe: {args.timeframe}, stake: ${args.stake:.2f}",
        f"- generated: {dt.datetime.now(dt.timezone.utc).isoformat()}",
        "",
        "## Per-direction within CASCADE",
        "",
        "| outcome | n | avg_fill | hypothetical_perfect_pnl |",
        "|---|---:|---:|---:|",
    ]
    for r in pdir:
        md.append(
            f"| {r['outcome']} | {r['n']} | "
            f"{(r['avg_fill'] or 0):.3f} | ${(r['pnl_if_perfect'] or 0):+.2f} |"
        )

    md += ["", "## Per-T-band within CASCADE", ""]
    md.append("| tband | UP | DOWN | total | UP_share |")
    md.append("|---|---:|---:|---:|---:|")
    by_band = {}
    for r in pband:
        by_band.setdefault(r["tband"], {"UP": 0, "DOWN": 0})
        by_band[r["tband"]][r["outcome"]] = r["n"]
    for band, d in sorted(by_band.items()):
        total = d["UP"] + d["DOWN"]
        if total == 0:
            continue
        md.append(f"| {band} | {d['UP']} | {d['DOWN']} | {total} | {d['UP']/total*100:.1f}% |")

    md += ["", "## Top winning signal-quintile cells WITHIN CASCADE", "",
           "| col | quintile | mean | dir | n | WR | Wilson 95% |",
           "|---|---:|---:|---|---:|---:|---|"]
    sigq_sorted = sorted(sigq, key=lambda r: -(r["lo"] * (r["n"] ** 0.5)))[:25]
    for r in sigq_sorted:
        md.append(
            f"| {r['col']} | q{r['q']} | {r['mean']:.4g} | {r['dir']} | "
            f"{r['n']} | {r['wr']*100:.1f}% | [{r['lo']*100:.1f}, {r['hi']*100:.1f}] |"
        )

    md += ["", "## CG short-liquidation buckets (CASCADE only)", "",
           "| bucket | n | UP_n | DOWN_n | UP_share |",
           "|---|---:|---:|---:|---:|"]
    for r in cg_short:
        n = r["n"]
        if n == 0:
            continue
        md.append(f"| {r['short_bucket']} | {n} | {r['up_n']} | {r['down_n']} | {r['up_n']/n*100:.1f}% |")

    md += ["", "## CG long-liquidation buckets (CASCADE only)", "",
           "| bucket | n | UP_n | DOWN_n | DOWN_share |",
           "|---|---:|---:|---:|---:|"]
    for r in cg_long:
        n = r["n"]
        if n == 0:
            continue
        md.append(f"| {r['long_bucket']} | {n} | {r['up_n']} | {r['down_n']} | {r['down_n']/n*100:.1f}% |")

    body = "\n".join(md)
    md_path = write_output(out_dir, "regime_cascade_specialist", body)
    print(f"[regime_cascade_specialist] wrote {md_path}")
    print(body)


if __name__ == "__main__":
    asyncio.run(main())
