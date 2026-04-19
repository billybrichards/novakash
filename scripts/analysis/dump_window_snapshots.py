#!/usr/bin/env python3
"""
Dump last N hours of window_snapshots (+ signal_evaluations + strategy_decisions)
to a local parquet/csv file so offline config analysis can iterate without
re-querying Railway every time.

Outputs:
  scripts/analysis/data/window_snapshots_<hours>h.parquet
  scripts/analysis/data/strategy_decisions_<hours>h.parquet

Dedup:
  window_snapshots → one row per (window_ts, asset) — DISTINCT ON, ORDER BY
  eval_offset DESC NULLS LAST (prefers the final settled snapshot). Mirrors the
  pattern used in config_honest_wr.py.

  signal_evaluations → one row per (window_ts, asset) via DISTINCT ON with
  v2_direction IS NOT NULL, latest eval_offset.

  strategy_decisions → one row per (strategy_id, window_ts, asset, timeframe)
  via DISTINCT ON, latest eval_offset.

Safe to run from Mac — Railway is the DB host, distinct from polymarket.com
(which is banned from local machines). Requires DATABASE_URL env var.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

try:
    import asyncpg
    import pandas as pd
except ImportError as e:
    print(f"ERROR: missing deps — {e}. pip install asyncpg pandas pyarrow", file=sys.stderr)
    sys.exit(2)


WINDOW_SNAPSHOT_COLS = [
    # Core
    "window_ts", "asset", "timeframe", "eval_offset",
    "open_price", "close_price", "actual_direction",
    # Signals / direction
    "vpin", "delta_pct", "regime", "direction", "confidence",
    "twap_direction", "timesfm_direction", "signal_direction",
    "signal_confidence",
    # v7.1 retroactive
    "v71_would_trade", "v71_skip_reason", "v71_regime",
    "v71_correct", "v71_pnl",
    # CLOB / gamma entry prices
    "clob_up_ask", "clob_up_bid", "clob_down_ask", "clob_down_bid",
    "gamma_up_price", "gamma_down_price", "gamma_mid_price", "gamma_spread",
    # Polymarket resolution (ground truth)
    "poly_winner", "poly_resolved_outcome", "oracle_outcome",
    # Extras that help downstream sim
    "skip_reason", "trade_placed", "pnl_usd",
    "engine_version", "is_live",
    "delta_source", "delta_chainlink", "delta_tiingo", "delta_binance",
    "cg_oi_delta_pct", "cg_liq_total_usd", "cg_taker_buy_usd", "cg_taker_sell_usd",
    "v2_probability_up", "v2_direction", "v2_agrees", "v2_model_version",
    "created_at",
]


async def dump(hours: int, asset: str, timeframe: str, outdir: Path) -> dict:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL env var required", file=sys.stderr)
        sys.exit(2)

    # Drop asyncpg-incompatible scheme + query args if present
    if db_url.startswith("postgresql+asyncpg://"):
        db_url = "postgresql://" + db_url[len("postgresql+asyncpg://"):]

    outdir.mkdir(parents=True, exist_ok=True)
    cutoff = int(time.time()) - hours * 3600

    # window_snapshots column availability check — schema drifts over time
    conn = await asyncpg.connect(db_url)
    try:
        have_cols = {r["column_name"] for r in await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name='window_snapshots'"
        )}
        ws_select = [c for c in WINDOW_SNAPSHOT_COLS if c in have_cols]
        missing = [c for c in WINDOW_SNAPSHOT_COLS if c not in have_cols]
        if missing:
            print(f"[info] window_snapshots missing cols (skipped): {missing}", file=sys.stderr)

        # signal_evaluations: join v2_direction
        se_cols = {r["column_name"] for r in await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name='signal_evaluations'"
        )}
        has_v2 = "v2_direction" in se_cols

        select_ws = ", ".join(f"ws.{c} AS {c}" for c in ws_select)
        if has_v2:
            select_ws += ", se.v2_direction AS se_v2_direction, se.v2_probability_up AS se_v2_prob_up"

        query = f"""
            WITH se AS (
                SELECT DISTINCT ON (window_ts, asset)
                    window_ts, asset, v2_direction, v2_probability_up
                FROM signal_evaluations
                WHERE asset = $1 AND timeframe = $2 AND window_ts >= $3
                  AND v2_direction IS NOT NULL
                ORDER BY window_ts, asset, eval_offset DESC
            )
            SELECT DISTINCT ON (ws.window_ts, ws.asset)
                {select_ws}
            FROM window_snapshots ws
            LEFT JOIN se ON se.window_ts = ws.window_ts AND se.asset = ws.asset
            WHERE ws.asset = $1
              AND ws.timeframe = $2
              AND ws.window_ts >= $3
            ORDER BY ws.window_ts ASC, ws.asset, ws.eval_offset DESC NULLS LAST
        """
        ws_rows = await conn.fetch(query, asset, timeframe, cutoff)

        # strategy_decisions — per (strategy_id, window_ts), capture whether the
        # strategy EVER decided TRADE on any eval_offset, and if so which
        # direction / entry_cap / skip_reason at that offset.
        #
        # Semantics: gate pipeline runs every 2s from T-300 → T-0. A strategy
        # may flip SKIP → TRADE → SKIP as the offset enters/exits its timing
        # window. For "would this config have traded this window?" we care
        # whether ANY eval in its allowed range was TRADE. When multiple
        # TRADE evals exist, pick the EARLIEST one (closest to entry) as the
        # representative row — this matches engine behaviour where the first
        # TRADE decision would have fired the order.
        sd_query = """
            WITH traded AS (
                SELECT DISTINCT ON (strategy_id, window_ts, asset, timeframe)
                    strategy_id, strategy_version, asset, window_ts, timeframe,
                    eval_offset, mode, action, direction, confidence,
                    confidence_score, entry_cap, collateral_pct,
                    entry_reason, skip_reason, executed,
                    fill_price, fill_size, evaluated_at
                FROM strategy_decisions
                WHERE asset IN ($1, LOWER($1))
                  AND timeframe = $2
                  AND window_ts >= $3
                  AND action = 'TRADE'
                ORDER BY strategy_id, window_ts, asset, timeframe, eval_offset ASC
            ),
            latest_skip AS (
                SELECT DISTINCT ON (strategy_id, window_ts, asset, timeframe)
                    strategy_id, strategy_version, asset, window_ts, timeframe,
                    eval_offset, mode, action, direction, confidence,
                    confidence_score, entry_cap, collateral_pct,
                    entry_reason, skip_reason, executed,
                    fill_price, fill_size, evaluated_at
                FROM strategy_decisions
                WHERE asset IN ($1, LOWER($1))
                  AND timeframe = $2
                  AND window_ts >= $3
                ORDER BY strategy_id, window_ts, asset, timeframe, eval_offset DESC
            )
            SELECT * FROM traded
            UNION ALL
            SELECT ls.* FROM latest_skip ls
            LEFT JOIN traded t USING (strategy_id, window_ts, asset, timeframe)
            WHERE t.strategy_id IS NULL
        """
        sd_rows = await conn.fetch(sd_query, asset, timeframe, cutoff)
    finally:
        await conn.close()

    ws_df = pd.DataFrame([dict(r) for r in ws_rows])
    sd_df = pd.DataFrame([dict(r) for r in sd_rows])

    ws_path = outdir / f"window_snapshots_{hours}h.parquet"
    sd_path = outdir / f"strategy_decisions_{hours}h.parquet"
    try:
        ws_df.to_parquet(ws_path, index=False)
        sd_df.to_parquet(sd_path, index=False)
        fmt = "parquet"
    except Exception as e:
        print(f"[warn] parquet failed ({e}), falling back to csv.gz", file=sys.stderr)
        ws_path = outdir / f"window_snapshots_{hours}h.csv.gz"
        sd_path = outdir / f"strategy_decisions_{hours}h.csv.gz"
        ws_df.to_csv(ws_path, index=False, compression="gzip")
        sd_df.to_csv(sd_path, index=False, compression="gzip")
        fmt = "csv.gz"

    return {
        "format": fmt,
        "window_snapshots_path": str(ws_path),
        "window_snapshots_rows": len(ws_df),
        "window_snapshots_size": ws_path.stat().st_size,
        "strategy_decisions_path": str(sd_path),
        "strategy_decisions_rows": len(sd_df),
        "strategy_decisions_size": sd_path.stat().st_size,
        "hours": hours,
        "asset": asset,
        "timeframe": timeframe,
        "cutoff_ts": cutoff,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=72)
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--timeframe", default="5m")
    ap.add_argument("--outdir", default=None,
                    help="Output dir (default: scripts/analysis/data)")
    args = ap.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    outdir = Path(args.outdir) if args.outdir else (repo_root / "scripts" / "analysis" / "data")

    info = asyncio.run(dump(args.hours, args.asset, args.timeframe, outdir))
    print(f"""
Dumped {info['hours']}h of {info['asset']}/{info['timeframe']} data:
  window_snapshots   → {info['window_snapshots_path']}
     rows: {info['window_snapshots_rows']}   size: {info['window_snapshots_size']:,} B
  strategy_decisions → {info['strategy_decisions_path']}
     rows: {info['strategy_decisions_rows']}   size: {info['strategy_decisions_size']:,} B
  format: {info['format']}
""")


if __name__ == "__main__":
    main()
