#!/usr/bin/env python3
"""Gate gain-of-function / loss-of-function analysis.

Takes a baseline GATE SET (one per strategy in the live ledger) and tests:

- **Gain-of-function**: ADD each candidate gate (single-column threshold) on top
  of the baseline. Does WR + real P&L improve?
- **Loss-of-function**: REMOVE each existing gate. Does WR + real P&L stay the
  same / improve? If it improves, the gate is hurting us.

Output: Top 10 ADDs and Top 10 REMOVEs by P&L delta vs baseline.

Baselines (definable, see ``BASELINES`` dict below):

- ``v9_lgb_only``         — fire if dir_v9 != NULL and dist_v9 >= 0.10
- ``v10_lgb_only``        — fire if dir_v10 != NULL and dist_v10 >= 0.10
- ``v12_lgb_combo``       — v9 & v12 agree, dist_v9 >= 0.10, dist_v12 >= 0.10
- ``v8_champion_lgb_only`` — v9 LGB, dist >= 0.15  (champion subset)

These are *evaluative* gate sets reconstructed from strategy_ledger.md, not
direct mirrors of YAML — actual engine YAML may diverge. The intent is to
find INCREMENTAL improvements over a known baseline.

Methodology
-----------

1. For each baseline, build a SQL predicate from window_evaluation_traces +
   window_snapshots (since v9/v10/v12 lgb probs live in surface_json).
2. Compute baseline n / wr / Wilson / real P&L.
3. For each CANDIDATE gate:
   - Build augmented predicate (baseline AND new_gate).
   - Compute n / wr / pnl. Delta vs baseline.
4. For each EXISTING gate in the baseline:
   - Build relaxed predicate (baseline minus that gate).
   - Compute delta. Negative WR-delta = the gate was helping.

Caveats
-------

- LIVE engine fills via FOK at clob_*_ask + escalation step; we synthesize fill
  from clob_up_ask / clob_down_ask snapshot — sufficient for relative deltas.
- Multiple comparison: with ~25 candidate gates vs each of 4 baselines, ~100
  tests run; a Wilson-low improvement of 5pp at n=30 is comfortably significant.

Usage
-----

    python3 scripts/ops/analysis/gate_gain_loss.py --hours 87
    python3 scripts/ops/analysis/gate_gain_loss.py --baseline v12_lgb_combo
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import sys
from typing import List, Optional

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if THIS_DIR not in sys.path:
    sys.path.insert(0, THIS_DIR)

from _common import (  # noqa: E402
    CellResult, TBAND_SQL,
    DEFAULT_HOURS, DEFAULT_STAKE_USD, DEFAULT_ASSET, DEFAULT_TIMEFRAME,
    POLY_FEE_MULT,
    cell_from_row, connect_pg, render_md_table, write_output,
    wilson_ci,
)


# ---------------------------------------------------------------------------
# Baseline gate sets (SQL predicates over the LGB CTE below)
# ---------------------------------------------------------------------------

BASELINES = {
    "v9_lgb_only": {
        "predicate": "dir_v9 IS NOT NULL AND dist_v9 >= 0.10",
        "gates": {
            "v9_dir_known": "dir_v9 IS NOT NULL",
            "v9_dist_min": "dist_v9 >= 0.10",
        },
        "direction_expr": "dir_v9",
        "fill_expr": "CASE WHEN dir_v9='UP' THEN clob_up_ask WHEN dir_v9='DOWN' THEN clob_down_ask END",
    },
    "v10_lgb_only": {
        "predicate": "dir_v10 IS NOT NULL AND dist_v10 >= 0.10",
        "gates": {
            "v10_dir_known": "dir_v10 IS NOT NULL",
            "v10_dist_min": "dist_v10 >= 0.10",
        },
        "direction_expr": "dir_v10",
        "fill_expr": "CASE WHEN dir_v10='UP' THEN clob_up_ask WHEN dir_v10='DOWN' THEN clob_down_ask END",
    },
    "v12_lgb_combo": {
        "predicate": "dir_v9 IS NOT NULL AND dir_v12 IS NOT NULL "
                     "AND dir_v9 = dir_v12 AND dist_v9 >= 0.10 AND dist_v12 >= 0.10",
        "gates": {
            "v9_v12_agree": "dir_v9 = dir_v12",
            "v9_dist_min": "dist_v9 >= 0.10",
            "v12_dist_min": "dist_v12 >= 0.10",
            "v9_dir_known": "dir_v9 IS NOT NULL",
            "v12_dir_known": "dir_v12 IS NOT NULL",
        },
        "direction_expr": "dir_v9",
        "fill_expr": "CASE WHEN dir_v9='UP' THEN clob_up_ask WHEN dir_v9='DOWN' THEN clob_down_ask END",
    },
    "v8_champion_lgb_only": {
        "predicate": "dir_v9 IS NOT NULL AND dist_v9 >= 0.15",
        "gates": {
            "v9_dir_known": "dir_v9 IS NOT NULL",
            "v9_dist_min_15": "dist_v9 >= 0.15",
        },
        "direction_expr": "dir_v9",
        "fill_expr": "CASE WHEN dir_v9='UP' THEN clob_up_ask WHEN dir_v9='DOWN' THEN clob_down_ask END",
    },
}


# ---------------------------------------------------------------------------
# Candidate gates to consider ADDing
# ---------------------------------------------------------------------------

CANDIDATE_GATES = [
    # Regime
    ("regime_calm", "regime = 'CALM'"),
    ("regime_normal", "regime = 'NORMAL'"),
    ("regime_transition", "regime = 'TRANSITION'"),
    ("regime_cascade", "regime = 'CASCADE'"),
    ("regime_not_cascade", "regime <> 'CASCADE'"),
    # T-band (seconds-to-close)
    ("tband_le_60", "tband IN ('T-30','T-60')"),
    ("tband_60_to_120", "tband IN ('T-60','T-90','T-120')"),
    ("tband_ge_120", "tband IN ('T-120','T-180','T-240')"),
    # VPIN
    ("vpin_high", "vpin >= 0.7"),
    ("vpin_low", "vpin < 0.5"),
    # Delta consensus
    ("delta_strong", "ABS(delta_pct) >= 0.0005"),
    ("delta_aligned_chainlink_binance", "SIGN(delta_chainlink) = SIGN(delta_binance)"),
    # CoinGlass
    ("cg_oi_falling", "cg_oi_delta_pct < 0"),
    ("cg_oi_rising", "cg_oi_delta_pct > 0"),
    ("cg_short_liq_spike", "cg_liq_short_usd > 1e6"),
    ("cg_long_liq_spike", "cg_liq_long_usd > 1e6"),
    ("cg_taker_buy_dominant", "cg_taker_buy_usd > cg_taker_sell_usd * 1.2"),
    ("cg_taker_sell_dominant", "cg_taker_sell_usd > cg_taker_buy_usd * 1.2"),
    ("cg_funding_neg", "cg_funding_rate < 0"),
    ("cg_funding_pos", "cg_funding_rate > 0"),
    # TWAP
    ("twap_agree", "twap_gamma_agree IS TRUE"),
    ("twap_disagree", "twap_gamma_agree IS FALSE"),
    # v3 composites
    ("v3_strong_up", "v3_composite > 0.2"),
    ("v3_strong_down", "v3_composite < -0.2"),
    # CLOB price tier (avoid expensive entries)
    ("clob_cheap_up", "clob_up_ask < 0.75"),
    ("clob_cheap_down", "clob_down_ask < 0.75"),
]


# ---------------------------------------------------------------------------
# Source CTE — joins surface_json + window_snapshots
# ---------------------------------------------------------------------------

def build_cte(hours: int, asset: str, timeframe: str) -> str:
    """Build the source CTE.

    NOTE on writer regressions discovered 2026-05-01 in this analysis:
    - ``window_snapshots.clob_up_ask`` / ``clob_down_ask`` are 100% NULL in
      87h. Live values live on ``signal_evaluations`` instead.
    - ``window_snapshots.cg_*`` and ``signal_evaluations.cg_*`` are 99%+ NULL
      in 87h (CG feed regression — separate issue, flagged).

    To compensate we COALESCE clob_*_ask from se when ws is null. CG fields
    we leave as ws (so candidate gates relying on cg_* will mostly skip — that
    is a real-data limitation, not a script bug).
    """
    bands_sql = TBAND_SQL.replace("eval_offset", "wet.eval_offset")
    return f"""
    WITH src AS (
        SELECT
            wet.window_ts, wet.asset, wet.timeframe, wet.eval_offset,
            {bands_sql} AS tband,
            COALESCE(wet.surface_json->>'regime', ws.regime, se.regime) AS regime,
            (wet.surface_json->>'probability_lgb')::float AS p_v9,
            (wet.surface_json->>'probability_lgb_v10')::float AS p_v10,
            (wet.surface_json->>'probability_lgb_v12')::float AS p_v12,
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
            ABS((wet.surface_json->>'probability_lgb')::float - 0.5) AS dist_v9,
            ABS((wet.surface_json->>'probability_lgb_v10')::float - 0.5) AS dist_v10,
            ABS((wet.surface_json->>'probability_lgb_v12')::float - 0.5) AS dist_v12,
            ws.outcome,
            COALESCE(ws.clob_up_ask, se.clob_up_ask) AS clob_up_ask,
            COALESCE(ws.clob_down_ask, se.clob_down_ask) AS clob_down_ask,
            COALESCE(ws.vpin, se.vpin) AS vpin,
            ws.cg_oi_delta_pct, ws.cg_liq_long_usd, ws.cg_liq_short_usd,
            ws.cg_taker_buy_usd, ws.cg_taker_sell_usd, ws.cg_funding_rate,
            COALESCE(ws.delta_pct, se.delta_pct) AS delta_pct,
            COALESCE(ws.delta_chainlink, se.delta_chainlink) AS delta_chainlink,
            COALESCE(ws.delta_binance, se.delta_binance) AS delta_binance,
            COALESCE(ws.twap_gamma_agree, se.twap_gamma_agree) AS twap_gamma_agree
        FROM window_evaluation_traces wet
        JOIN window_snapshots ws
          ON ws.window_ts = wet.window_ts
         AND ws.asset = wet.asset
         AND ws.timeframe = wet.timeframe
         AND ws.eval_offset = wet.eval_offset
        LEFT JOIN signal_evaluations se
          ON se.window_ts = wet.window_ts
         AND se.asset = wet.asset
         AND se.timeframe = wet.timeframe
         AND se.eval_offset = wet.eval_offset
        WHERE wet.assembled_at > NOW() - INTERVAL '{hours} hours'
          AND wet.asset = '{asset}'
          AND wet.timeframe = '{timeframe}'
          AND ws.outcome IN ('UP','DOWN')
    )"""


# v3 composite isn't on window_snapshots — skip for now (already noted as candidate).
_CANDIDATES_NEED_V3 = {"v3_strong_up", "v3_strong_down"}


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

async def eval_predicate(
    conn, hours: int, asset: str, timeframe: str,
    cte: str, predicate: str, dir_expr: str, fill_expr: str,
    stake: float,
) -> tuple[int, int, float, float]:
    """Return (n, wins, avg_fill, real_pnl) for rows passing predicate."""
    sql = f"""
        {cte}
        SELECT
            count(*) AS n,
            count(*) FILTER (WHERE {dir_expr} = outcome) AS wins,
            avg(({fill_expr})) FILTER (WHERE ({fill_expr}) > 0 AND ({fill_expr}) < 1) AS avg_fill,
            sum(
                CASE
                    WHEN ({fill_expr}) IS NULL OR ({fill_expr}) <= 0 OR ({fill_expr}) >= 1 THEN 0
                    WHEN {dir_expr} = outcome
                        THEN ({stake} * (1.0 - ({fill_expr})) / ({fill_expr})) - {POLY_FEE_MULT * stake}
                    ELSE -{stake}
                END
            ) AS real_pnl
        FROM src
        WHERE {predicate};
    """
    row = await conn.fetchrow(sql)
    n = int(row["n"] or 0)
    wins = int(row["wins"] or 0)
    avg_fill = float(row["avg_fill"] or 0.0)
    pnl = float(row["real_pnl"] or 0.0)
    return n, wins, avg_fill, pnl


async def analyse_baseline(
    conn, baseline_name: str, hours: int, asset: str, timeframe: str, stake: float,
) -> dict:
    spec = BASELINES[baseline_name]
    cte = build_cte(hours, asset, timeframe)
    base_pred = spec["predicate"]
    dir_expr = spec["direction_expr"]
    fill_expr = spec["fill_expr"]

    print(f"  baseline={baseline_name}: {base_pred}")
    bn, bw, bf, bp = await eval_predicate(conn, hours, asset, timeframe, cte, base_pred, dir_expr, fill_expr, stake)
    bwr = (bw / bn) if bn > 0 else 0
    blo, bhi = wilson_ci(bw, bn)
    print(f"    n={bn} wins={bw} wr={bwr*100:.1f}% [{blo*100:.1f}, {bhi*100:.1f}] fill={bf:.3f} pnl=${bp:+.2f}")

    # Gain-of-function
    gain_results = []
    for cname, cgate in CANDIDATE_GATES:
        if cname in _CANDIDATES_NEED_V3:
            continue
        try:
            n, w, f, p = await eval_predicate(
                conn, hours, asset, timeframe, cte, f"({base_pred}) AND ({cgate})",
                dir_expr, fill_expr, stake,
            )
        except Exception as e:
            print(f"    [skip] {cname}: {e}")
            continue
        if n < 10:
            continue
        wr = w / n
        lo, hi = wilson_ci(w, n)
        gain_results.append({
            "gate": cname, "n": n, "wins": w, "wr": wr,
            "lo": lo, "hi": hi, "avg_fill": f, "pnl": p,
            "delta_wr": wr - bwr,
            "delta_pnl": p - bp,
            "n_drop_pct": (bn - n) / bn * 100 if bn > 0 else 0,
        })

    # Loss-of-function: drop each existing gate
    loss_results = []
    for gname, gpred in spec["gates"].items():
        # Build relaxed predicate by removing this clause from the AND chain.
        gates_list = list(spec["gates"].values())
        relaxed = " AND ".join(g for g in gates_list if g != gpred)
        if not relaxed:
            relaxed = "TRUE"
        try:
            n, w, f, p = await eval_predicate(
                conn, hours, asset, timeframe, cte, relaxed, dir_expr, fill_expr, stake,
            )
        except Exception as e:
            print(f"    [skip] remove {gname}: {e}")
            continue
        wr = w / n if n > 0 else 0
        lo, hi = wilson_ci(w, n)
        loss_results.append({
            "gate": gname, "n": n, "wins": w, "wr": wr,
            "lo": lo, "hi": hi, "avg_fill": f, "pnl": p,
            "delta_wr": wr - bwr,
            "delta_pnl": p - bp,
            "n_gain_pct": (n - bn) / bn * 100 if bn > 0 else 0,
        })

    return {
        "baseline": {
            "name": baseline_name, "predicate": base_pred,
            "n": bn, "wins": bw, "wr": bwr, "lo": blo, "hi": bhi,
            "avg_fill": bf, "pnl": bp,
        },
        "gain": gain_results,
        "loss": loss_results,
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def render_results(res: dict, top: int = 10) -> str:
    base = res["baseline"]
    out = []
    out.append(f"### Baseline: `{base['name']}`")
    out.append("")
    out.append(f"- predicate: `{base['predicate']}`")
    out.append(f"- n={base['n']:,} wins={base['wins']:,} WR={base['wr']*100:.1f}% "
               f"[Wilson 95%: {base['lo']*100:.1f}, {base['hi']*100:.1f}] "
               f"avg_fill={base['avg_fill']:.3f} real_pnl=${base['pnl']:+.2f}")
    out.append("")

    out.append(f"#### Top {top} GAIN-of-function (ADD this gate -> better than baseline)")
    out.append("")
    out.append("| gate | n | WR | delta_WR | delta_pnl | Wilson 95% | n_drop |")
    out.append("|---|---:|---:|---:|---:|---|---:|")
    gain_sorted = sorted(res["gain"], key=lambda r: -r["delta_pnl"])[:top]
    for r in gain_sorted:
        out.append(
            f"| {r['gate']} | {r['n']} | {r['wr']*100:.1f}% | "
            f"{(r['delta_wr']*100):+.1f}pp | ${r['delta_pnl']:+.2f} | "
            f"[{r['lo']*100:.1f}, {r['hi']*100:.1f}] | -{r['n_drop_pct']:.0f}% |"
        )
    out.append("")

    out.append(f"#### Top {top} LOSS-of-function (REMOVE this gate -> better than baseline)")
    out.append("")
    out.append("| gate removed | n | WR | delta_WR | delta_pnl | Wilson 95% | n_gain |")
    out.append("|---|---:|---:|---:|---:|---|---:|")
    loss_sorted = sorted(res["loss"], key=lambda r: -r["delta_pnl"])[:top]
    for r in loss_sorted:
        out.append(
            f"| {r['gate']} | {r['n']} | {r['wr']*100:.1f}% | "
            f"{(r['delta_wr']*100):+.1f}pp | ${r['delta_pnl']:+.2f} | "
            f"[{r['lo']*100:.1f}, {r['hi']*100:.1f}] | +{r['n_gain_pct']:.0f}% |"
        )
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=int, default=DEFAULT_HOURS)
    ap.add_argument("--asset", default=DEFAULT_ASSET)
    ap.add_argument("--timeframe", default=DEFAULT_TIMEFRAME)
    ap.add_argument("--stake", type=float, default=DEFAULT_STAKE_USD)
    ap.add_argument("--baseline", default="all", choices=["all"] + list(BASELINES.keys()))
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    out_dir = args.out_dir or os.path.join(
        "docs", "analysis", "auto", dt.date.today().isoformat()
    )
    print(f"[gate_gain_loss] hours={args.hours} baseline={args.baseline} stake=${args.stake:.2f}")

    baselines_to_run = list(BASELINES.keys()) if args.baseline == "all" else [args.baseline]

    md_lines = [
        f"# Gate Gain-of-Function / Loss-of-Function Analysis — {args.hours}h",
        "",
        f"- asset: {args.asset}, timeframe: {args.timeframe}, stake: ${args.stake:.2f}",
        f"- generated: {dt.datetime.now(dt.timezone.utc).isoformat()}",
        f"- candidate gates tested: {len(CANDIDATE_GATES)}",
        "",
        "Methodology: for each baseline gate-set, ADD each candidate gate (does WR/PNL improve?)",
        "or REMOVE each existing gate (does the engine actually need it?). Real P&L using",
        "synthesized fill from CLOB ask side; treat magnitudes as relative not absolute.",
        "",
    ]

    conn = await connect_pg()
    try:
        for bn in baselines_to_run:
            res = await analyse_baseline(conn, bn, args.hours, args.asset, args.timeframe, args.stake)
            md_lines.append(render_results(res, top=args.top))
    finally:
        await conn.close()

    body = "\n".join(md_lines)
    md_path = write_output(out_dir, "gate_gain_loss", body)
    print(f"[gate_gain_loss] wrote {md_path}")
    print(body)


if __name__ == "__main__":
    asyncio.run(main())
