#!/usr/bin/env python3
"""Time-decay analyzer.

For each signal, plot accuracy as a function of seconds-to-close (T-band).
Some signals work near close (microstructure / TWAP), some work far back
(macro / OI). This finds which.

For each numeric signal, we compute "directional WR" per T-band:
- val > median -> bet UP, else bet DOWN (ad hoc but consistent baseline)
- WR per T-band, plotted as ASCII line.

For each LGB model: WR conditional on dir match per T-band, sourced from
window_evaluation_traces.surface_json.

Usage
-----

    python3 scripts/ops/analysis/time_decay_analyzer.py --hours 87
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
    TBAND_SQL,
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

NUMERIC_COLS = [
    "v2_probability_up", "v3_composite", "v3_cascade_signal",
    "v3_taker_signal", "v3_momentum_signal",
    "delta_pct", "vpin", "twap_delta",
    "cg_oi_delta_pct", "cg_liq_short_usd", "cg_liq_long_usd",
    "cg_funding_rate",
]


T_BAND_ORDER = ["T-30", "T-60", "T-90", "T-120", "T-180", "T-240"]


async def signal_decay(conn, hours, asset, tf, col, min_n=20):
    """Per T-band: compute median, then WR if 'val>median -> UP else DOWN'."""
    sql = CTE_SE.format(hours=hours, asset=asset, timeframe=tf) + f"""
        , medians AS (
            SELECT tband, percentile_cont(0.5) WITHIN GROUP (ORDER BY {col}) AS m
            FROM base
            WHERE {col} IS NOT NULL
            GROUP BY tband
        )
        SELECT b.tband,
               count(*) AS n,
               count(*) FILTER (
                   WHERE (b.{col} > m.m AND b.outcome='UP')
                      OR (b.{col} <= m.m AND b.outcome='DOWN')
               ) AS wins
        FROM base b
        JOIN medians m ON m.tband = b.tband
        WHERE b.{col} IS NOT NULL
        GROUP BY b.tband
        HAVING count(*) >= {min_n}
        ORDER BY b.tband;
    """
    return await conn.fetch(sql)


async def lgb_decay(conn, hours, asset, timeframe):
    """Per T-band: dir-match WR for v9 / v10 / v12 from surface_json."""
    bands_sql = TBAND_SQL.replace("eval_offset", "wet.eval_offset")
    sql = f"""
        WITH lgb AS (
            SELECT
                {bands_sql} AS tband,
                CASE
                    WHEN (wet.surface_json->>'probability_lgb')::float > 0.5 THEN 'UP'
                    WHEN (wet.surface_json->>'probability_lgb')::float < 0.5 THEN 'DOWN'
                END AS dir_v9,
                CASE
                    WHEN (wet.surface_json->>'probability_lgb_v10')::float > 0.5 THEN 'UP'
                    WHEN (wet.surface_json->>'probability_lgb_v10')::float < 0.5 THEN 'DOWN'
                END AS dir_v10,
                CASE
                    WHEN (wet.surface_json->>'probability_lgb_v12')::float > 0.5 THEN 'UP'
                    WHEN (wet.surface_json->>'probability_lgb_v12')::float < 0.5 THEN 'DOWN'
                END AS dir_v12,
                ws.outcome
            FROM window_evaluation_traces wet
            JOIN window_snapshots ws
              ON ws.window_ts = wet.window_ts
             AND ws.asset = wet.asset
             AND ws.timeframe = wet.timeframe
             AND ws.eval_offset = wet.eval_offset
            WHERE wet.assembled_at > NOW() - INTERVAL '{hours} hours'
              AND wet.asset = '{asset}'
              AND wet.timeframe = '{timeframe}'
              AND ws.outcome IN ('UP','DOWN')
        )
        SELECT
            'v9' AS model, tband,
            count(*) FILTER (WHERE dir_v9 IS NOT NULL) AS n,
            count(*) FILTER (WHERE dir_v9 = outcome) AS wins
        FROM lgb GROUP BY tband
        UNION ALL
        SELECT
            'v10' AS model, tband,
            count(*) FILTER (WHERE dir_v10 IS NOT NULL) AS n,
            count(*) FILTER (WHERE dir_v10 = outcome) AS wins
        FROM lgb GROUP BY tband
        UNION ALL
        SELECT
            'v12' AS model, tband,
            count(*) FILTER (WHERE dir_v12 IS NOT NULL) AS n,
            count(*) FILTER (WHERE dir_v12 = outcome) AS wins
        FROM lgb GROUP BY tband
        ORDER BY 1, 2;
    """
    return await conn.fetch(sql)


def _ascii_chart(name: str, by_band: dict, scale_min=0.45, scale_max=0.85) -> str:
    """Render a 1-line ascii bar per band: WR encoded with characters."""
    width = 30
    lines = [f"### {name}"]
    for band in T_BAND_ORDER:
        d = by_band.get(band)
        if not d:
            lines.append(f"  {band:<6} : (no data)")
            continue
        wr = d["wr"]
        n = d["n"]
        lo, hi = d["lo"], d["hi"]
        # bar position
        clamped = max(scale_min, min(scale_max, wr))
        pos = int((clamped - scale_min) / (scale_max - scale_min) * width)
        bar = "." * pos + "#" + "." * (width - pos - 1)
        flag = "EXP" if n < 30 else "   "
        lines.append(f"  {band:<6} : [{bar}] WR={wr*100:5.1f}%  n={n:5d}  CI=[{lo*100:.1f}, {hi*100:.1f}]  {flag}")
    return "\n".join(lines)


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=int, default=DEFAULT_HOURS)
    ap.add_argument("--asset", default=DEFAULT_ASSET)
    ap.add_argument("--timeframe", default=DEFAULT_TIMEFRAME)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    out_dir = args.out_dir or os.path.join("docs", "analysis", "auto", dt.date.today().isoformat())

    md = [
        f"# Time-Decay Analyzer — {args.hours}h",
        "",
        f"- asset: {args.asset}, timeframe: {args.timeframe}",
        f"- generated: {dt.datetime.now(dt.timezone.utc).isoformat()}",
        "",
        "Methodology: per signal, compute median per T-band, then WR if 'above-median predicts UP'.",
        "This is an ad-hoc directional rule but lets us compare decay shape across signals.",
        "LGB models use their own dir signal directly. ASCII chart x-axis: WR scaled 45%-85%.",
        "",
        "```",
    ]

    conn = await connect_pg()
    try:
        # numeric signals
        for col in NUMERIC_COLS:
            try:
                rows = await signal_decay(conn, args.hours, args.asset, args.timeframe, col)
            except Exception as e:
                md.append(f"### {col}\n  (error: {e})\n")
                continue
            by_band = {}
            for r in rows:
                n, wins = r["n"], r["wins"]
                wr = wins / n if n else 0
                lo, hi = wilson_ci(wins, n)
                by_band[r["tband"]] = {"wr": wr, "n": n, "lo": lo, "hi": hi}
            md.append(_ascii_chart(f"{col} (above-median->UP)", by_band))
            md.append("")

        # LGB
        try:
            lgb_rows = await lgb_decay(conn, args.hours, args.asset, args.timeframe)
        except Exception as e:
            md.append(f"### LGB models\n  (error: {e})\n")
            lgb_rows = []
        for model in ("v9", "v10", "v12"):
            by_band = {}
            for r in lgb_rows:
                if r["model"] != model:
                    continue
                n, wins = r["n"], r["wins"]
                if not n:
                    continue
                wr = wins / n
                lo, hi = wilson_ci(wins, n)
                by_band[r["tband"]] = {"wr": wr, "n": n, "lo": lo, "hi": hi}
            md.append(_ascii_chart(f"lgb_{model}", by_band))
            md.append("")
    finally:
        await conn.close()

    md.append("```")
    body = "\n".join(md)
    md_path = write_output(out_dir, "time_decay_analyzer", body)
    print(f"[time_decay_analyzer] wrote {md_path}")
    print(body)


if __name__ == "__main__":
    asyncio.run(main())
