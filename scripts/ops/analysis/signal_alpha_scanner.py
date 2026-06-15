#!/usr/bin/env python3
"""Single-signal alpha scanner.

For every analysable column on ``signal_evaluations`` (numeric, boolean,
categorical), compute WR + Wilson CI + real P&L within (T-band x regime x
direction) cells. Emits the strongest cells where Wilson_low > 0.55, n >= 20.

Numeric columns get split into 5 quintiles. For each quintile we ask: "if this
quintile's sign chooses UP, what's WR? If DOWN?" — picking the direction with
higher WR.

Boolean columns: WR conditional on TRUE vs FALSE.

Categorical: WR per category.

CoinGlass and v3_* columns also covered. v9 / v10 / v12 LGB probabilities pulled
from ``window_evaluation_traces.surface_json`` (regression #6 — the dedicated
``probability_lgb_v12`` column on signal_evaluations is 0% populated in 87h).

Usage
-----

    python3 scripts/ops/analysis/signal_alpha_scanner.py --hours 87

CLI
---

    --hours N        lookback in hours (default 87)
    --asset BTC      asset filter (default BTC)
    --timeframe 5m   timeframe filter (default 5m)
    --stake 7.5      $ stake assumption for real P&L (default 7.5)
    --top 25         top-K cells per section (default 25)
    --min-n 20       min sample size (default 20)
    --min-wr 0.55    Wilson_low minimum to be flagged "winning" (default 0.55)
    --out-dir docs/analysis/auto/<TODAY>/  (default)

Memory references: see ``_common.py`` docstring.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import sys
from typing import List

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if THIS_DIR not in sys.path:
    sys.path.insert(0, THIS_DIR)

from _common import (  # noqa: E402
    CellResult, REGIMES, DIRECTIONS, TBAND_SQL,
    DEFAULT_HOURS, DEFAULT_STAKE_USD, DEFAULT_ASSET, DEFAULT_TIMEFRAME,
    cell_from_row, connect_pg, render_md_table, render_csv, write_output,
    real_pnl_sql_expr,
)

# ---------------------------------------------------------------------------
# Column registry — what to scan and how
# ---------------------------------------------------------------------------

NUMERIC_COLUMNS = [
    # ML / probability surfaces
    "v2_probability_up", "v3_composite", "v3_cascade_signal",
    "v3_taker_signal", "v3_momentum_signal",
    # Price / delta
    "delta_pct", "delta_chainlink", "delta_tiingo", "delta_binance",
    # Microstructure
    "vpin", "clob_spread", "clob_mid",
    "clob_up_bid", "clob_up_ask", "clob_down_bid", "clob_down_ask",
    # TWAP
    "twap_delta",
    # CoinGlass — under-utilised, deserves quintile sweep
    "cg_oi_delta_pct", "cg_liq_long_usd", "cg_liq_short_usd",
    "cg_taker_buy_usd", "cg_taker_sell_usd", "cg_funding_rate",
]

BOOLEAN_COLUMNS = ["v2_high_conf", "twap_gamma_agree"]

CATEGORICAL_COLUMNS = {
    "regime": list(REGIMES),
    "twap_direction": ["UP", "DOWN", "FLAT", None],
    "delta_source": None,  # autodiscover
    "v2_direction": ["UP", "DOWN", None],
    "v2_agrees": ["UP", "DOWN", None],
}


# ---------------------------------------------------------------------------
# Core query
# ---------------------------------------------------------------------------

OUTCOME_COL = "outcome"
FILL_COL = "implied_fill"  # we synthesize from CLOB bid/ask; see CTE below


# We compute a synthesized fill from CLOB ask side for the predicted direction.
# UP: clob_up_ask  DOWN: clob_down_ask  (engine fills via FOK at ask + step).
# Many rows will have NULL fill — those bets are treated as "no fill" (real_pnl=0).
PNL_CTE = """
WITH base AS (
    SELECT
        se.*,
        ({tband_sql}) AS tband,
        CASE
            WHEN se.v2_direction = 'UP' THEN se.clob_up_ask
            WHEN se.v2_direction = 'DOWN' THEN se.clob_down_ask
            ELSE NULL
        END AS implied_fill
    FROM signal_evaluations se
    WHERE evaluated_at > NOW() - INTERVAL '{hours} hours'
      AND asset = '{asset}'
      AND timeframe = '{timeframe}'
      AND outcome IN ('UP', 'DOWN')
)
""".format(tband_sql=TBAND_SQL, hours="{hours}", asset="{asset}", timeframe="{timeframe}")


def _wrap(sql_body: str, hours: int, asset: str, timeframe: str) -> str:
    return PNL_CTE.format(hours=hours, asset=asset, timeframe=timeframe) + sql_body


# ---------------------------------------------------------------------------
# Scan sections
# ---------------------------------------------------------------------------

async def scan_numeric_quintile(
    conn, col: str, hours: int, asset: str, timeframe: str, stake: float,
    min_n: int,
) -> List[CellResult]:
    """For a numeric column: split into 5 quintiles, in each quintile pick whichever
    direction (UP or DOWN) has higher empirical WR — that's the "if-this-bucket-fires-this-way" alpha.
    """
    pnl_expr = real_pnl_sql_expr(OUTCOME_COL, "predicted_dir", FILL_COL, stake)
    body = f"""
        , binned AS (
            SELECT
                tband, regime, outcome,
                NTILE(5) OVER (
                    PARTITION BY tband, regime
                    ORDER BY {col}
                ) AS q,
                {col} AS val,
                implied_fill
            FROM base
            WHERE {col} IS NOT NULL
        ),
        directional AS (
            -- For each (tband, regime, q): compute UP-WR and DOWN-WR.
            SELECT
                tband, regime, q,
                count(*) AS n_total,
                count(*) FILTER (WHERE outcome='UP') AS n_up_wins,
                count(*) FILTER (WHERE outcome='DOWN') AS n_down_wins,
                avg(val) AS bucket_mean,
                avg(implied_fill) FILTER (WHERE implied_fill IS NOT NULL) AS avg_fill_any
            FROM binned
            GROUP BY tband, regime, q
        )
        SELECT
            tband, regime, q,
            n_total,
            n_up_wins, n_down_wins,
            bucket_mean,
            avg_fill_any
        FROM directional
        WHERE n_total >= {min_n}
          AND tband IS NOT NULL
        ORDER BY tband, regime, q;
    """
    rows = await conn.fetch(_wrap(body, hours, asset, timeframe))
    out: List[CellResult] = []
    for r in rows:
        n = r["n_total"]
        # UP cell
        up_wins = r["n_up_wins"]
        out.append(cell_from_row(
            label=f"{col} q{r['q']} | {r['tband']} | {r['regime']} | UP (mean={r['bucket_mean']:.4g})",
            n=n, wins=up_wins,
            avg_fill=float(r["avg_fill_any"] or 0),
            real_pnl_usd=_synth_pnl(up_wins, n - up_wins, float(r["avg_fill_any"] or 0), stake),
            extra=f"col={col};quintile={r['q']};dir=UP",
        ))
        # DOWN cell
        down_wins = r["n_down_wins"]
        out.append(cell_from_row(
            label=f"{col} q{r['q']} | {r['tband']} | {r['regime']} | DOWN (mean={r['bucket_mean']:.4g})",
            n=n, wins=down_wins,
            avg_fill=float(r["avg_fill_any"] or 0),
            real_pnl_usd=_synth_pnl(down_wins, n - down_wins, float(r["avg_fill_any"] or 0), stake),
            extra=f"col={col};quintile={r['q']};dir=DOWN",
        ))
    return out


async def scan_boolean(
    conn, col: str, hours: int, asset: str, timeframe: str, stake: float, min_n: int,
) -> List[CellResult]:
    """Boolean column: TRUE vs FALSE WR for UP and DOWN outcomes."""
    body = f"""
        SELECT
            COALESCE({col}::text, 'NULL') AS bval,
            tband, regime,
            count(*) AS n,
            count(*) FILTER (WHERE outcome='UP') AS up_wins,
            count(*) FILTER (WHERE outcome='DOWN') AS down_wins,
            avg(implied_fill) FILTER (WHERE implied_fill IS NOT NULL) AS avg_fill_any
        FROM base
        GROUP BY {col}, tband, regime
        HAVING count(*) >= {min_n}
        ORDER BY {col}, tband, regime;
    """
    rows = await conn.fetch(_wrap(body, hours, asset, timeframe))
    out: List[CellResult] = []
    for r in rows:
        n = r["n"]
        up_wins = r["up_wins"]
        down_wins = r["down_wins"]
        for direction, w in (("UP", up_wins), ("DOWN", down_wins)):
            out.append(cell_from_row(
                label=f"{col}={r['bval']} | {r['tband']} | {r['regime']} | {direction}",
                n=n, wins=w,
                avg_fill=float(r["avg_fill_any"] or 0),
                real_pnl_usd=_synth_pnl(w, n - w, float(r["avg_fill_any"] or 0), stake),
                extra=f"col={col};val={r['bval']};dir={direction}",
            ))
    return out


async def scan_categorical(
    conn, col: str, hours: int, asset: str, timeframe: str, stake: float, min_n: int,
) -> List[CellResult]:
    """Categorical column: per-value WR for UP and DOWN."""
    body = f"""
        SELECT
            COALESCE({col}, 'NULL') AS cat,
            tband,
            count(*) AS n,
            count(*) FILTER (WHERE outcome='UP') AS up_wins,
            count(*) FILTER (WHERE outcome='DOWN') AS down_wins,
            avg(implied_fill) FILTER (WHERE implied_fill IS NOT NULL) AS avg_fill_any
        FROM base
        GROUP BY {col}, tband
        HAVING count(*) >= {min_n}
        ORDER BY {col}, tband;
    """
    rows = await conn.fetch(_wrap(body, hours, asset, timeframe))
    out: List[CellResult] = []
    for r in rows:
        n = r["n"]
        up_wins = r["up_wins"]
        down_wins = r["down_wins"]
        for direction, w in (("UP", up_wins), ("DOWN", down_wins)):
            out.append(cell_from_row(
                label=f"{col}='{r['cat']}' | {r['tband']} | {direction}",
                n=n, wins=w,
                avg_fill=float(r["avg_fill_any"] or 0),
                real_pnl_usd=_synth_pnl(w, n - w, float(r["avg_fill_any"] or 0), stake),
                extra=f"col={col};val={r['cat']};dir={direction}",
            ))
    return out


# ---------------------------------------------------------------------------
# v9/v10/v12 LGB scan via window_evaluation_traces.surface_json
# ---------------------------------------------------------------------------

async def scan_lgb_models(
    conn, hours: int, asset: str, timeframe: str, stake: float, min_n: int,
) -> List[CellResult]:
    """v9/v10/v12 LGB probability quintile scans from surface_json + WS outcome."""
    # v9 = probability_lgb (when model='cedar' it's v9-flavour); v10/v12 named explicitly.
    bands_sql = TBAND_SQL.replace("eval_offset", "wet.eval_offset")
    body = f"""
        WITH lgb AS (
            SELECT
                wet.window_ts, wet.asset, wet.timeframe, wet.eval_offset,
                {bands_sql} AS tband,
                (wet.surface_json->>'regime') AS regime,
                (wet.surface_json->>'probability_lgb')::float AS p_v9,
                (wet.surface_json->>'probability_lgb_v10')::float AS p_v10,
                (wet.surface_json->>'probability_lgb_v12')::float AS p_v12,
                ws.outcome,
                CASE
                    WHEN (wet.surface_json->>'probability_lgb_v10')::float > 0.5 THEN ws.clob_up_ask
                    WHEN (wet.surface_json->>'probability_lgb_v10')::float < 0.5 THEN ws.clob_down_ask
                    ELSE NULL
                END AS fill_v10
            FROM window_evaluation_traces wet
            JOIN window_snapshots ws
              ON ws.window_ts = wet.window_ts
             AND ws.asset = wet.asset
             AND ws.timeframe = wet.timeframe
            WHERE wet.assembled_at > NOW() - INTERVAL '{hours} hours'
              AND wet.asset = '{asset}'
              AND wet.timeframe = '{timeframe}'
              AND ws.outcome IN ('UP','DOWN')
        ),
        binned AS (
            SELECT
                tband, regime, model, prob, outcome, fill,
                NTILE(5) OVER (
                    PARTITION BY model, tband, regime
                    ORDER BY prob
                ) AS q
            FROM (
                SELECT tband, regime, 'v9' AS model, p_v9 AS prob, outcome, NULL::float AS fill FROM lgb WHERE p_v9 IS NOT NULL
                UNION ALL
                SELECT tband, regime, 'v10' AS model, p_v10 AS prob, outcome, fill_v10 AS fill FROM lgb WHERE p_v10 IS NOT NULL
                UNION ALL
                SELECT tband, regime, 'v12' AS model, p_v12 AS prob, outcome, NULL::float AS fill FROM lgb WHERE p_v12 IS NOT NULL
            ) u
        )
        SELECT
            model, tband, regime, q,
            count(*) AS n,
            count(*) FILTER (WHERE outcome='UP') AS up_wins,
            count(*) FILTER (WHERE outcome='DOWN') AS down_wins,
            avg(prob) AS bucket_mean,
            avg(fill) FILTER (WHERE fill IS NOT NULL) AS avg_fill_v10
        FROM binned
        GROUP BY model, tband, regime, q
        HAVING count(*) >= {min_n}
        ORDER BY model, tband, regime, q;
    """
    try:
        rows = await conn.fetch(body)
    except Exception as e:
        print(f"  [warn] LGB surface_json scan skipped: {e}", file=sys.stderr)
        return []
    out: List[CellResult] = []
    for r in rows:
        n = r["n"]
        # use 0.78 as fallback fill assumption when implied fill not available
        avg_fill = float(r["avg_fill_v10"] or 0.78)
        up_wins = r["up_wins"]
        down_wins = r["down_wins"]
        out.append(cell_from_row(
            label=f"{r['model']} q{r['q']} | {r['tband']} | {r['regime']} | UP (mean={r['bucket_mean']:.3f})",
            n=n, wins=up_wins,
            avg_fill=avg_fill,
            real_pnl_usd=_synth_pnl(up_wins, n - up_wins, avg_fill, stake),
            extra=f"col=lgb_{r['model']};quintile={r['q']};dir=UP",
        ))
        out.append(cell_from_row(
            label=f"{r['model']} q{r['q']} | {r['tband']} | {r['regime']} | DOWN (mean={r['bucket_mean']:.3f})",
            n=n, wins=down_wins,
            avg_fill=avg_fill,
            real_pnl_usd=_synth_pnl(down_wins, n - down_wins, avg_fill, stake),
            extra=f"col=lgb_{r['model']};quintile={r['q']};dir=DOWN",
        ))
    return out


# ---------------------------------------------------------------------------
# P&L helpers
# ---------------------------------------------------------------------------

def _synth_pnl(wins: int, losses: int, avg_fill: float, stake: float) -> float:
    """Aggregate real P&L: wins * (stake*(1-f)/f - fee) - losses * stake."""
    from _common import POLY_FEE_MULT, real_pnl  # type: ignore
    if avg_fill is None or avg_fill <= 0 or avg_fill >= 1:
        avg_fill = 0.78  # conservative default fill
    win_pnl = wins * (stake * (1.0 - avg_fill) / avg_fill - POLY_FEE_MULT * stake)
    loss_pnl = -losses * stake
    return win_pnl + loss_pnl


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=int, default=DEFAULT_HOURS)
    ap.add_argument("--asset", default=DEFAULT_ASSET)
    ap.add_argument("--timeframe", default=DEFAULT_TIMEFRAME)
    ap.add_argument("--stake", type=float, default=DEFAULT_STAKE_USD)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--min-n", type=int, default=20)
    ap.add_argument("--min-wr", type=float, default=0.55)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    out_dir = args.out_dir or os.path.join(
        "docs", "analysis", "auto", dt.date.today().isoformat()
    )
    print(f"[signal_alpha_scanner] hours={args.hours} asset={args.asset} stake=${args.stake:.2f}")
    print(f"[signal_alpha_scanner] writing -> {out_dir}/")

    conn = await connect_pg()
    try:
        all_cells: List[CellResult] = []

        for col in NUMERIC_COLUMNS:
            print(f"  scan numeric: {col}")
            cells = await scan_numeric_quintile(conn, col, args.hours, args.asset, args.timeframe, args.stake, args.min_n)
            all_cells.extend(cells)

        for col in BOOLEAN_COLUMNS:
            print(f"  scan bool:    {col}")
            cells = await scan_boolean(conn, col, args.hours, args.asset, args.timeframe, args.stake, args.min_n)
            all_cells.extend(cells)

        for col in CATEGORICAL_COLUMNS:
            print(f"  scan cat:     {col}")
            cells = await scan_categorical(conn, col, args.hours, args.asset, args.timeframe, args.stake, args.min_n)
            all_cells.extend(cells)

        print("  scan LGB v9/v10/v12 (surface_json)...")
        lgb_cells = await scan_lgb_models(conn, args.hours, args.asset, args.timeframe, args.stake, args.min_n)
        all_cells.extend(lgb_cells)
    finally:
        await conn.close()

    # Filter to "alpha cells": Wilson_low > min_wr AND n >= min_n
    alpha = [c for c in all_cells if c.wilson_low >= args.min_wr and c.n >= args.min_n]

    md_lines = [
        f"# Signal Alpha Scanner — {args.hours}h",
        f"",
        f"- asset: {args.asset}, timeframe: {args.timeframe}, stake: ${args.stake:.2f}",
        f"- generated: {dt.datetime.now(dt.timezone.utc).isoformat()}",
        f"- total cells scanned: {len(all_cells):,}",
        f"- alpha cells (Wilson_low >= {args.min_wr*100:.0f}%, n >= {args.min_n}): {len(alpha):,}",
        f"",
        f"## Top {args.top} alpha cells (sorted by Wilson_low * sqrt(n))",
        f"",
        render_md_table(alpha, top=args.top, sort_key="quality"),
        f"",
        f"## Top {args.top} by raw real P&L",
        f"",
        render_md_table(alpha, top=args.top, sort_key="pnl"),
        f"",
        f"## Caveats",
        f"",
        f"- 'EXP' flag = exploratory (n<30 or 95% CI > 30pp wide) — treat as hint not signal.",
        f"- ~{len(NUMERIC_COLUMNS)*5*4*6} numeric cells x {len(BOOLEAN_COLUMNS)*4*2*6} bool cells = "
        f"thousands tested; expect 5% false-positives at p=0.05. Bonferroni: only trust Wilson_low > 0.65 with n > 50.",
        f"- avg_fill is synthesized from clob_up_ask / clob_down_ask at signal time, not realised fill. Real engine fills are FOK + step.",
        f"- Outcome ground truth = signal_evaluations.outcome (post PR #441, 98.9% populated).",
    ]
    md_body = "\n".join(md_lines)
    csv_body = render_csv(all_cells)

    md_path = write_output(out_dir, "signal_alpha_scanner", md_body, csv_body)
    print(f"[signal_alpha_scanner] wrote {md_path}")
    print(md_body)


if __name__ == "__main__":
    asyncio.run(main())
