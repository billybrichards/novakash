#!/usr/bin/env python3
"""CoinGlass direct-alpha probe.

Question: do CG-derived signals (liquidations, OI delta, funding rate, taker
flow) have RAW directional predictive power on 5-min BTC outcome — independent
of LGB models?

Tests:
1. ``cg_liq_short_usd`` quintiles -> UP-bias? (shorts squeezed = price up)
2. ``cg_liq_long_usd`` quintiles -> DOWN-bias?
3. ``cg_oi_delta_pct`` direction split (rising vs falling).
4. ``cg_funding_rate`` extremes (positive = longs paying = potential top;
   negative = shorts paying = potential bottom).
5. ``cg_taker_buy_usd / cg_taker_sell_usd`` ratio — taker flow imbalance.
6. Combined: short_liq_spike AND funding_pos -> UP? (squeeze hypothesis)

Each cell reports n / WR / Wilson 95% / hypothetical real P&L. Filtered to
winning cells (Wilson_low > 0.55).

Usage
-----

    python3 scripts/ops/analysis/coinglass_direct_alpha.py --hours 87
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
    POLY_FEE_MULT, TBAND_SQL,
    connect_pg, write_output, wilson_ci,
)


CTE_SE = """
WITH base AS (
    SELECT
        se.*,
        ({tband_sql}) AS tband
    FROM signal_evaluations se
    WHERE evaluated_at > NOW() - INTERVAL '{hours} hours'
      AND asset = '{asset}'
      AND timeframe = '{timeframe}'
      AND outcome IN ('UP','DOWN')
)
""".format(tband_sql=TBAND_SQL, hours="{hours}", asset="{asset}", timeframe="{timeframe}")


def _pnl_expr(direction: str, stake: float) -> str:
    if direction == "UP":
        f = "clob_up_ask"
    else:
        f = "clob_down_ask"
    return f"""
        sum(
            CASE
                WHEN {f} IS NULL OR {f} <= 0 OR {f} >= 1 THEN 0
                WHEN outcome = '{direction}' THEN ({stake}*(1.0-{f})/{f}) - {POLY_FEE_MULT*stake}
                ELSE -{stake}
            END
        )
    """


async def quintile_scan(conn, hours, asset, tf, col, direction, stake, min_n=20):
    pnl = _pnl_expr(direction, stake)
    sql = CTE_SE.format(hours=hours, asset=asset, timeframe=tf) + f"""
        , binned AS (
            SELECT outcome, NTILE(5) OVER (ORDER BY {col}) AS q,
                   {col} AS val,
                   clob_up_ask, clob_down_ask
            FROM base
            WHERE {col} IS NOT NULL
        )
        SELECT
            q,
            count(*) AS n,
            count(*) FILTER (WHERE outcome='{direction}') AS wins,
            avg(val) AS bucket_mean,
            {pnl} AS pnl
        FROM binned
        GROUP BY q
        HAVING count(*) >= {min_n}
        ORDER BY q;
    """
    return await conn.fetch(sql)


async def categorical_scan(conn, hours, asset, tf, predicate, label, direction, stake, min_n=20):
    pnl = _pnl_expr(direction, stake)
    sql = CTE_SE.format(hours=hours, asset=asset, timeframe=tf) + f"""
        SELECT
            count(*) AS n,
            count(*) FILTER (WHERE outcome='{direction}') AS wins,
            avg(clob_up_ask) AS avg_up_fill,
            avg(clob_down_ask) AS avg_down_fill,
            {pnl} AS pnl
        FROM base
        WHERE {predicate}
        HAVING count(*) >= {min_n};
    """
    return await conn.fetchrow(sql)


SCANS = [
    ("cg_liq_short_usd", "UP",   "shorts squeezed -> UP bias"),
    ("cg_liq_long_usd",  "DOWN", "longs blown out -> DOWN bias"),
    ("cg_oi_delta_pct",  "UP",   "OI rising -> UP bias hypothesis"),
    ("cg_oi_delta_pct",  "DOWN", "OI falling -> DOWN bias hypothesis"),
    ("cg_funding_rate",  "UP",   "low funding -> UP bias"),
    ("cg_funding_rate",  "DOWN", "high funding -> DOWN bias"),
    ("cg_taker_buy_usd", "UP",   "buy pressure -> UP"),
    ("cg_taker_sell_usd","DOWN", "sell pressure -> DOWN"),
]

CATEGORICAL_SCANS = [
    ("short_liq_huge_AND_funding_pos", "cg_liq_short_usd >= 1e6 AND cg_funding_rate > 0", "UP",
     "short squeeze with longs paying premium"),
    ("long_liq_huge_AND_funding_neg", "cg_liq_long_usd >= 1e6 AND cg_funding_rate < 0", "DOWN",
     "long blowout with shorts paying premium"),
    ("taker_buy_2x_sell", "cg_taker_buy_usd > 2 * cg_taker_sell_usd", "UP",
     "buy flow dominance 2x"),
    ("taker_sell_2x_buy", "cg_taker_sell_usd > 2 * cg_taker_buy_usd", "DOWN",
     "sell flow dominance 2x"),
    ("oi_falling_strong", "cg_oi_delta_pct < -1.0", "DOWN",
     "OI dropping >1% (deleveraging)"),
    ("oi_rising_strong", "cg_oi_delta_pct > 1.0", "UP",
     "OI building >1% (positioning)"),
]


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=int, default=DEFAULT_HOURS)
    ap.add_argument("--asset", default=DEFAULT_ASSET)
    ap.add_argument("--timeframe", default=DEFAULT_TIMEFRAME)
    ap.add_argument("--stake", type=float, default=DEFAULT_STAKE_USD)
    ap.add_argument("--min-n", type=int, default=20)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    out_dir = args.out_dir or os.path.join("docs", "analysis", "auto", dt.date.today().isoformat())

    md = [
        f"# CoinGlass Direct-Alpha — {args.hours}h",
        "",
        f"- asset: {args.asset}, timeframe: {args.timeframe}, stake: ${args.stake:.2f}",
        f"- generated: {dt.datetime.now(dt.timezone.utc).isoformat()}",
        "",
        "Hypothesis: CG signals carry directional information INDEPENDENT of LGB models.",
        "If true, we can build a v13 that uses raw CG features as a sanity check.",
        "",
    ]

    conn = await connect_pg()
    try:
        md += ["## Quintile sweeps", "",
               "| col | dir | quintile | bucket_mean | n | WR | Wilson 95% | real_pnl |",
               "|---|---|---:|---:|---:|---:|---|---:|"]
        for col, direction, note in SCANS:
            try:
                rows = await quintile_scan(conn, args.hours, args.asset, args.timeframe, col, direction, args.stake, args.min_n)
            except Exception as e:
                print(f"  [skip] {col} {direction}: {e}")
                continue
            for r in rows:
                n, wins = r["n"], r["wins"]
                wr = wins / n if n else 0
                lo, hi = wilson_ci(wins, n)
                md.append(
                    f"| {col} | {direction} | q{r['q']} | {(r['bucket_mean'] or 0):.4g} | "
                    f"{n} | {wr*100:.1f}% | [{lo*100:.1f}, {hi*100:.1f}] | "
                    f"${(r['pnl'] or 0):+.2f} |"
                )

        md += ["", "## Compound hypotheses", "",
               "| name | n | WR | Wilson 95% | real_pnl | hypothesis |",
               "|---|---:|---:|---|---:|---|"]
        for name, predicate, direction, note in CATEGORICAL_SCANS:
            try:
                row = await categorical_scan(conn, args.hours, args.asset, args.timeframe, predicate, name, direction, args.stake, args.min_n)
            except Exception as e:
                print(f"  [skip] {name}: {e}")
                continue
            if not row:
                md.append(f"| {name} | <{args.min_n} | - | - | - | {note} (skipped: too few) |")
                continue
            n = row["n"] or 0
            wins = row["wins"] or 0
            wr = wins / n if n else 0
            lo, hi = wilson_ci(wins, n)
            md.append(
                f"| {name} ({direction}) | {n} | {wr*100:.1f}% | "
                f"[{lo*100:.1f}, {hi*100:.1f}] | ${(row['pnl'] or 0):+.2f} | {note} |"
            )
    finally:
        await conn.close()

    body = "\n".join(md)
    md_path = write_output(out_dir, "coinglass_direct_alpha", body)
    print(f"[coinglass_direct_alpha] wrote {md_path}")
    print(body)


if __name__ == "__main__":
    asyncio.run(main())
