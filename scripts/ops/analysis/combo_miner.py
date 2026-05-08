#!/usr/bin/env python3
"""Greedy signal-combo miner.

Approach: take a small ENUMERATED list of "atomic" gate predicates (well-known
single-signal alpha cells) and try every PAIR (and a handful of triples) as
AND-combined predicates. Report combos where:

- n >= ``--min-n`` (default 15)
- Wilson_low > ``--min-wr-low`` (default 0.60)
- Sorted by Wilson_low * sqrt(n) (favours both confidence and sample).

Atomic gate definitions live in ``ATOMS`` below — same predicates as
gate_gain_loss.py for consistency. Each atom commits to a DIRECTION (UP/DOWN)
because a direction-agnostic combo has no actionable interpretation.

Pairs only by default — enumerate is N*(N-1)/2 = ~150 pairs which is tractable.
Add ``--triples`` to test top-20-by-pair triples.

Usage
-----

    python3 scripts/ops/analysis/combo_miner.py --hours 87
    python3 scripts/ops/analysis/combo_miner.py --hours 87 --triples

Reads the same surface_json + window_snapshots source as gate_gain_loss.py.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import itertools
import os
import sys
from typing import List, Optional

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if THIS_DIR not in sys.path:
    sys.path.insert(0, THIS_DIR)

from _common import (  # noqa: E402
    DEFAULT_HOURS, DEFAULT_STAKE_USD, DEFAULT_ASSET, DEFAULT_TIMEFRAME,
    POLY_FEE_MULT,
    connect_pg, render_md_table, write_output, wilson_ci, cell_from_row,
)
from gate_gain_loss import build_cte  # noqa: E402


# ---------------------------------------------------------------------------
# Atomic gates — each commits to a direction
# ---------------------------------------------------------------------------

# Naming: a_<short>_<dir>
# Each entry: (name, direction, predicate)
ATOMS: List[tuple[str, str, str]] = [
    # Single LGB signals
    ("v9_up_loose",     "UP",   "dir_v9='UP'   AND dist_v9 >= 0.10"),
    ("v9_up_tight",     "UP",   "dir_v9='UP'   AND dist_v9 >= 0.20"),
    ("v9_down_loose",   "DOWN", "dir_v9='DOWN' AND dist_v9 >= 0.10"),
    ("v9_down_tight",   "DOWN", "dir_v9='DOWN' AND dist_v9 >= 0.20"),
    ("v10_up",          "UP",   "dir_v10='UP'  AND dist_v10 >= 0.10"),
    ("v10_down",        "DOWN", "dir_v10='DOWN' AND dist_v10 >= 0.10"),
    ("v12_up",          "UP",   "dir_v12='UP'  AND dist_v12 >= 0.10"),
    ("v12_down",        "DOWN", "dir_v12='DOWN' AND dist_v12 >= 0.10"),

    # Regime
    ("regime_calm",       "ANY", "regime='CALM'"),
    ("regime_normal",     "ANY", "regime='NORMAL'"),
    ("regime_transition", "ANY", "regime='TRANSITION'"),
    ("regime_cascade",    "ANY", "regime='CASCADE'"),

    # T-band
    ("tband_close",     "ANY", "tband IN ('T-30','T-60')"),
    ("tband_mid",       "ANY", "tband IN ('T-90','T-120')"),
    ("tband_far",       "ANY", "tband IN ('T-180','T-240')"),

    # VPIN
    ("vpin_high",       "ANY", "vpin >= 0.7"),
    ("vpin_low",        "ANY", "vpin < 0.5"),

    # CoinGlass
    ("cg_short_squeeze","UP",   "cg_liq_short_usd > 1e6"),
    ("cg_long_squeeze", "DOWN", "cg_liq_long_usd > 1e6"),
    ("cg_oi_falling",   "ANY", "cg_oi_delta_pct < -0.5"),
    ("cg_oi_rising",    "ANY", "cg_oi_delta_pct > 0.5"),
    ("cg_taker_buy_dom","UP",   "cg_taker_buy_usd > cg_taker_sell_usd * 1.3"),
    ("cg_taker_sell_dom","DOWN","cg_taker_sell_usd > cg_taker_buy_usd * 1.3"),
    ("cg_funding_neg",  "ANY", "cg_funding_rate < 0"),
    ("cg_funding_pos",  "ANY", "cg_funding_rate > 0"),

    # CLOB tier
    ("clob_cheap_up",   "UP",   "clob_up_ask < 0.75"),
    ("clob_cheap_down", "DOWN", "clob_down_ask < 0.75"),

    # TWAP
    ("twap_agree",      "ANY", "twap_gamma_agree IS TRUE"),
]


# ---------------------------------------------------------------------------
# Combo evaluation
# ---------------------------------------------------------------------------

async def eval_combo(
    conn, hours: int, asset: str, timeframe: str,
    cte: str, predicate: str, direction: str, stake: float,
) -> dict:
    if direction == "UP":
        dir_expr = "'UP'"
        fill_expr = "clob_up_ask"
    elif direction == "DOWN":
        dir_expr = "'DOWN'"
        fill_expr = "clob_down_ask"
    else:
        # ANY direction — fall back to whichever side wins the bet (paper-trade).
        # Conservative: skip ANY-direction combos unless forced by atom mix.
        return {}
    sql = f"""
        {cte}
        SELECT
            count(*) AS n,
            count(*) FILTER (WHERE outcome = {dir_expr}) AS wins,
            avg({fill_expr}) FILTER (WHERE {fill_expr} > 0 AND {fill_expr} < 1) AS avg_fill,
            sum(
                CASE
                    WHEN {fill_expr} IS NULL OR {fill_expr} <= 0 OR {fill_expr} >= 1 THEN 0
                    WHEN outcome = {dir_expr}
                        THEN ({stake} * (1.0 - {fill_expr}) / {fill_expr}) - {POLY_FEE_MULT * stake}
                    ELSE -{stake}
                END
            ) AS pnl
        FROM src
        WHERE {predicate};
    """
    row = await conn.fetchrow(sql)
    n = int(row["n"] or 0)
    wins = int(row["wins"] or 0)
    return {
        "n": n,
        "wins": wins,
        "wr": (wins / n) if n > 0 else 0,
        "avg_fill": float(row["avg_fill"] or 0.0),
        "pnl": float(row["pnl"] or 0.0),
    }


def resolve_direction(atoms: List[tuple[str, str, str]]) -> Optional[str]:
    """Pick a direction for a combo based on its component atoms.

    - If any atom is 'UP', combo is UP (unless conflicting 'DOWN').
    - If any atom is 'DOWN', combo is DOWN.
    - All-ANY combos return None (skipped).
    """
    dirs = {a[1] for a in atoms}
    if "UP" in dirs and "DOWN" in dirs:
        return None
    if "UP" in dirs:
        return "UP"
    if "DOWN" in dirs:
        return "DOWN"
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=int, default=DEFAULT_HOURS)
    ap.add_argument("--asset", default=DEFAULT_ASSET)
    ap.add_argument("--timeframe", default=DEFAULT_TIMEFRAME)
    ap.add_argument("--stake", type=float, default=DEFAULT_STAKE_USD)
    ap.add_argument("--min-n", type=int, default=15)
    ap.add_argument("--min-wr-low", type=float, default=0.60)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--triples", action="store_true",
                    help="also test top-20-pair triples (slower)")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    out_dir = args.out_dir or os.path.join(
        "docs", "analysis", "auto", dt.date.today().isoformat()
    )

    print(f"[combo_miner] hours={args.hours} atoms={len(ATOMS)} pairs={len(ATOMS)*(len(ATOMS)-1)//2}")

    cte = build_cte(args.hours, args.asset, args.timeframe)
    conn = await connect_pg()
    pair_results = []
    try:
        # First: each atom alone (baseline lift comparison)
        atom_results = {}
        for name, dirn, pred in ATOMS:
            d = dirn if dirn != "ANY" else None
            if d is None:
                continue
            res = await eval_combo(conn, args.hours, args.asset, args.timeframe, cte, pred, d, args.stake)
            if res:
                atom_results[name] = res

        # Pairs
        for (n1, d1, p1), (n2, d2, p2) in itertools.combinations(ATOMS, 2):
            d = resolve_direction([(n1, d1, p1), (n2, d2, p2)])
            if d is None:
                continue
            pred = f"({p1}) AND ({p2})"
            res = await eval_combo(conn, args.hours, args.asset, args.timeframe, cte, pred, d, args.stake)
            if not res or res["n"] < args.min_n:
                continue
            lo, hi = wilson_ci(res["wins"], res["n"])
            if lo < args.min_wr_low:
                continue
            pair_results.append({
                "name": f"{n1} & {n2}",
                "dir": d,
                "n": res["n"],
                "wins": res["wins"],
                "wr": res["wr"],
                "lo": lo, "hi": hi,
                "avg_fill": res["avg_fill"],
                "pnl": res["pnl"],
                "atoms": [n1, n2],
                "predicate": pred,
            })

        triple_results: list = []
        if args.triples and pair_results:
            top_pairs = sorted(pair_results, key=lambda r: -(r["lo"] * (r["n"] ** 0.5)))[:20]
            atom_dict = {a[0]: a for a in ATOMS}
            tested = set()
            for p in top_pairs:
                used = set(p["atoms"])
                for atom in ATOMS:
                    if atom[0] in used:
                        continue
                    triple_atoms = sorted(list(used) + [atom[0]])
                    key = tuple(triple_atoms)
                    if key in tested:
                        continue
                    tested.add(key)
                    triplet = [atom_dict[a] for a in triple_atoms]
                    d = resolve_direction(triplet)
                    if d is None:
                        continue
                    pred = " AND ".join(f"({a[2]})" for a in triplet)
                    res = await eval_combo(conn, args.hours, args.asset, args.timeframe, cte, pred, d, args.stake)
                    if not res or res["n"] < args.min_n:
                        continue
                    lo, hi = wilson_ci(res["wins"], res["n"])
                    if lo < args.min_wr_low:
                        continue
                    triple_results.append({
                        "name": " & ".join(triple_atoms),
                        "dir": d,
                        "n": res["n"], "wins": res["wins"], "wr": res["wr"],
                        "lo": lo, "hi": hi,
                        "avg_fill": res["avg_fill"], "pnl": res["pnl"],
                    })
    finally:
        await conn.close()

    md = [
        f"# Combo Miner — {args.hours}h",
        "",
        f"- asset: {args.asset}, timeframe: {args.timeframe}, stake: ${args.stake:.2f}",
        f"- generated: {dt.datetime.now(dt.timezone.utc).isoformat()}",
        f"- atoms: {len(ATOMS)}; min_n: {args.min_n}; min_wilson_low: {args.min_wr_low}",
        f"- pairs surviving filter: {len(pair_results)}",
        "",
        f"## Top {args.top} pair combos (Wilson_low * sqrt(n))",
        "",
        "| combo | dir | n | WR | Wilson 95% | avg_fill | real_pnl |",
        "|---|---|---:|---:|---|---:|---:|",
    ]
    sorted_pairs = sorted(pair_results, key=lambda r: -(r["lo"] * (r["n"] ** 0.5)))[:args.top]
    for r in sorted_pairs:
        md.append(
            f"| {r['name']} | {r['dir']} | {r['n']} | {r['wr']*100:.1f}% | "
            f"[{r['lo']*100:.1f}, {r['hi']*100:.1f}] | {r['avg_fill']:.3f} | ${r['pnl']:+.2f} |"
        )

    if triple_results:
        md.append("")
        md.append(f"## Top {args.top} triple combos (from triples-mode)")
        md.append("")
        md.append("| combo | dir | n | WR | Wilson 95% | avg_fill | real_pnl |")
        md.append("|---|---|---:|---:|---|---:|---:|")
        sorted_triples = sorted(triple_results, key=lambda r: -(r["lo"] * (r["n"] ** 0.5)))[:args.top]
        for r in sorted_triples:
            md.append(
                f"| {r['name']} | {r['dir']} | {r['n']} | {r['wr']*100:.1f}% | "
                f"[{r['lo']*100:.1f}, {r['hi']*100:.1f}] | {r['avg_fill']:.3f} | ${r['pnl']:+.2f} |"
            )

    body = "\n".join(md)
    md_path = write_output(out_dir, "combo_miner", body)
    print(f"[combo_miner] wrote {md_path}")
    print(body)


if __name__ == "__main__":
    asyncio.run(main())
