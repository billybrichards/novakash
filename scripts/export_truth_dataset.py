#!/usr/bin/env python3
"""
Export a ground-truth trading dataset as CSV files.

Pulls every `poly_fills` row from the last N hours and joins the
correlated signal context across `signal_evaluations`, `gate_audit`,
`window_snapshots`, `trade_bible`, and `trades`. Writes multiple CSVs
to `docs/truth_dataset/` so you can feed them into pandas/duckdb/ML
pipelines without touching Railway directly.

CSVs produced (all in docs/truth_dataset/YYYYMMDD-HHMMSS/):

1. poly_fills.csv
   The authoritative fill record sourced from Polymarket data-api.
   Append-only, source-tagged, multi-fill-annotated. THIS IS THE GROUND
   TRUTH for what actually happened on-chain.

2. poly_fills_enriched.csv
   poly_fills LEFT JOIN trade_bible LEFT JOIN signal_evaluations.
   One row per fill with the engine's matching signal context.

3. trade_bible.csv
   Engine-side resolved trade records for the same period. Use this
   for P&L attribution; cross-reference against poly_fills for
   integrity checks.

4. signal_evaluations.csv
   Every 2s TRADE/SKIP decision with full gate context. Useful for
   counterfactual analysis (what would have won if we'd traded it?).

5. gate_audit.csv
   Per-window gate decision audit log.

6. summary.json
   Aggregate counts and integrity checks (single vs multi fill, gap
   between wallet spend and recorded stake, etc).

7. README.md
   Human-readable description of the CSVs, column definitions, and
   example queries.

Usage:
    # Last 36 hours (default)
    python3 scripts/export_truth_dataset.py

    # Custom range
    python3 scripts/export_truth_dataset.py --hours 72

    # Specific output dir
    python3 scripts/export_truth_dataset.py --out /tmp/my-export

    # Skip CSVs you don't need
    python3 scripts/export_truth_dataset.py --skip signal_evaluations gate_audit

Montreal rules: read-only against Railway DB. Safe to run on any host
with DATABASE_URL set.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    print("Install: pip3 install psycopg2-binary", file=sys.stderr)
    sys.exit(1)


DEFAULT_HOURS = 36


def _get_db_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url.replace("postgresql+asyncpg://", "postgresql://", 1)

    repo_root = Path(__file__).resolve().parent.parent
    for candidate in (repo_root / "engine" / ".env", repo_root / "engine" / ".env.local"):
        if not candidate.exists():
            continue
        for line in candidate.read_text().splitlines():
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip().replace("postgresql+asyncpg://", "postgresql://", 1)

    raise RuntimeError("DATABASE_URL not found")


def _dump_csv(rows: list[dict[str, Any]], path: Path) -> int:
    """Write rows to CSV. Handles empty result set cleanly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return 0

    # Union of keys across all rows, preserving first-row order
    seen_keys: dict[str, None] = {}
    for r in rows:
        for k in r.keys():
            if k not in seen_keys:
                seen_keys[k] = None
    fieldnames = list(seen_keys.keys())

    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            # Serialize jsonb/dict/list to JSON strings for CSV cells
            safe = {}
            for k, v in r.items():
                if isinstance(v, (dict, list)):
                    safe[k] = json.dumps(v, default=str)
                elif isinstance(v, datetime):
                    safe[k] = v.isoformat()
                else:
                    safe[k] = v
            writer.writerow(safe)
    return len(rows)


def export_poly_fills(conn, since: datetime) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                id, transaction_hash, asset_token_id, condition_id, market_slug,
                side, outcome, price, size, cost_usd, fee_usd,
                match_timestamp, match_time_utc,
                trade_bible_id, clob_order_id, source, verified_at,
                is_multi_fill, multi_fill_index, multi_fill_total,
                created_at
            FROM poly_fills
            WHERE match_time_utc >= %s
            ORDER BY match_time_utc ASC
            """,
            (since,),
        )
        return list(cur.fetchall())


def export_poly_fills_enriched(conn, since: datetime) -> list[dict[str, Any]]:
    """poly_fills LEFT JOIN trade_bible LEFT JOIN signal_evaluations.

    One row per fill with the engine's correlated signal context.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                pf.id AS fill_id,
                pf.transaction_hash,
                pf.market_slug,
                pf.condition_id,
                pf.side,
                pf.outcome AS fill_outcome,
                pf.price AS fill_price,
                pf.size AS fill_size,
                pf.cost_usd,
                pf.match_time_utc,
                pf.is_multi_fill,
                pf.multi_fill_index,
                pf.multi_fill_total,
                pf.source,

                tb.id AS trade_bible_id,
                tb.trade_outcome,
                tb.direction AS tb_direction,
                tb.entry_price AS tb_entry_price,
                tb.stake_usd AS tb_stake_usd,
                tb.pnl_usd,
                tb.payout_usd,
                tb.regime AS tb_regime,
                tb.entry_reason,
                tb.eval_tier,
                tb.dynamic_cap,
                tb.order_type,
                tb.vpin_at_entry,
                tb.delta_chainlink,
                tb.delta_tiingo,
                tb.placed_at,
                tb.resolved_at,

                se.eval_offset AS se_eval_offset,
                se.regime AS se_regime,
                se.vpin AS se_vpin,
                se.delta_pct AS se_delta_pct,
                se.delta_binance AS se_delta_binance,
                se.delta_chainlink AS se_delta_chainlink,
                se.delta_tiingo AS se_delta_tiingo,
                se.v2_probability_up,
                se.v2_direction,
                se.clob_up_ask,
                se.clob_down_ask,
                se.clob_spread,
                se.decision AS se_decision,
                se.evaluated_at AS se_evaluated_at
            FROM poly_fills pf
            LEFT JOIN trade_bible tb ON tb.id = pf.trade_bible_id
            LEFT JOIN LATERAL (
                SELECT *
                FROM signal_evaluations
                WHERE asset = 'BTC'
                  AND decision = 'TRADE'
                  AND evaluated_at BETWEEN (pf.match_time_utc - interval '10 minutes')
                                       AND (pf.match_time_utc + interval '2 minutes')
                ORDER BY ABS(EXTRACT(EPOCH FROM (evaluated_at - pf.match_time_utc))) ASC
                LIMIT 1
            ) se ON TRUE
            WHERE pf.match_time_utc >= %s
            ORDER BY pf.match_time_utc ASC
            """,
            (since,),
        )
        return list(cur.fetchall())


def export_trade_bible(conn, since: datetime) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                id AS trade_id,
                window_ts, asset, market_slug, condition_id, clob_order_id,
                placed_at, resolved_at,
                direction, entry_price, stake_usd,
                entry_reason, config_version,
                source_agreement, delta_chainlink, delta_tiingo,
                vpin_at_entry, regime, eval_tier, dynamic_cap, order_type,
                oracle_outcome, trade_outcome, pnl_usd, payout_usd,
                resolution_source, is_live, notes, created_at
            FROM trade_bible
            WHERE placed_at >= %s
              AND is_live = true
            ORDER BY placed_at ASC
            """,
            (since,),
        )
        return list(cur.fetchall())


def export_signal_evaluations(conn, since: datetime) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                id, window_ts, asset, timeframe, eval_offset,
                clob_up_bid, clob_up_ask, clob_down_bid, clob_down_ask,
                clob_spread, clob_mid,
                binance_price, tiingo_open, tiingo_close, chainlink_price,
                delta_pct, delta_binance, delta_chainlink, delta_tiingo, delta_source,
                vpin, regime,
                v2_probability_up, v2_direction, v2_agrees, v2_high_conf, v2_model_version,
                gate_vpin_passed, gate_delta_passed, gate_cg_passed,
                gate_twap_passed, gate_timesfm_passed,
                gate_passed, gate_failed, decision,
                twap_delta, twap_direction, twap_gamma_agree,
                evaluated_at
            FROM signal_evaluations
            WHERE evaluated_at >= %s
              AND asset = 'BTC'
            ORDER BY evaluated_at ASC
            """,
            (since,),
        )
        return list(cur.fetchall())


def export_gate_audit(conn, since: datetime) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                id, window_ts, asset, timeframe, engine_version,
                direction, delta_source,
                tiingo_delta, binance_delta, chainlink_delta,
                vpin, regime,
                gate_vpin, gate_delta, gate_cg_veto, gate_macro,
                gate_divergence, gate_floor, gate_cap, gate_confidence,
                confidence_tier,
                trade_attempted, trade_filled, execution_mode,
                fok_attempts, fok_fill_step, entry_price, clob_fill_price,
                oracle_outcome, would_have_won, pnl_usd, skip_reason, gate_failed,
                macro_bias, macro_confidence,
                tiingo_price, chainlink_price, binance_price,
                clob_up_ask, clob_down_ask, exchange_spread,
                created_at, evaluated_at,
                delta_pct, gate_passed, gates_passed_list, decision,
                eval_offset, v2_probability_up, v2_direction, v2_agrees, v2_high_conf
            FROM gate_audit
            WHERE created_at >= %s
              AND asset = 'BTC'
            ORDER BY created_at ASC
            """,
            (since,),
        )
        return list(cur.fetchall())


def build_summary(
    poly_fills: list[dict[str, Any]],
    trade_bible: list[dict[str, Any]],
    signal_evals: list[dict[str, Any]],
    gate_audit: list[dict[str, Any]],
    since: datetime,
) -> dict[str, Any]:
    """Aggregate metrics + integrity checks across the export."""
    from collections import Counter

    # Multi-fill breakdown
    cond_counts = Counter()
    for pf in poly_fills:
        if pf.get("side") == "BUY":
            cond_counts[pf.get("condition_id", "")] += 1
    single_fill_windows = sum(1 for c in cond_counts.values() if c == 1)
    double_fill_windows = sum(1 for c in cond_counts.values() if c == 2)
    triple_fill_windows = sum(1 for c in cond_counts.values() if c >= 3)
    total_windows = len(cond_counts)
    total_buy_fills = sum(cond_counts.values())
    total_buy_gross = sum(
        float(pf["cost_usd"] or 0)
        for pf in poly_fills
        if pf.get("side") == "BUY"
    )

    # trade_bible aggregates
    tb_recorded_stake = sum(float(r.get("stake_usd") or 0) for r in trade_bible)
    tb_recorded_pnl = sum(float(r.get("pnl_usd") or 0) for r in trade_bible)
    tb_wins = sum(1 for r in trade_bible if r.get("trade_outcome") == "WIN")
    tb_losses = sum(1 for r in trade_bible if r.get("trade_outcome") == "LOSS")
    tb_total = len(trade_bible)

    # Integrity check: unrecorded spend gap
    unrecorded = round(total_buy_gross - tb_recorded_stake, 2)

    return {
        "export_since": since.isoformat(),
        "export_generated_at": datetime.now(timezone.utc).isoformat(),
        "poly_fills": {
            "total_rows": len(poly_fills),
            "total_buy_fills": total_buy_fills,
            "total_sell_fills": sum(1 for r in poly_fills if r.get("side") == "SELL"),
            "distinct_windows": total_windows,
            "single_fill_windows": single_fill_windows,
            "double_fill_windows": double_fill_windows,
            "triple_or_more_fill_windows": triple_fill_windows,
            "multi_fill_pct": round(
                100.0 * (double_fill_windows + triple_fill_windows) / max(total_windows, 1),
                1,
            ),
            "total_buy_gross_usd": round(total_buy_gross, 2),
        },
        "trade_bible": {
            "total_rows": tb_total,
            "wins": tb_wins,
            "losses": tb_losses,
            "wr_pct": round(100.0 * tb_wins / max(tb_wins + tb_losses, 1), 1),
            "total_recorded_stake_usd": round(tb_recorded_stake, 2),
            "total_recorded_pnl_usd": round(tb_recorded_pnl, 2),
        },
        "integrity_check": {
            "actual_buy_gross": round(total_buy_gross, 2),
            "recorded_stake": round(tb_recorded_stake, 2),
            "unrecorded_spend_usd": unrecorded,
            "verdict": (
                "GOOD — single-fill execution, tb stake matches wallet"
                if abs(unrecorded) < 10
                else "BAD — multi-fill bug active, spend drifting from recorded stake"
            ),
        },
        "signal_evaluations": {
            "total_rows": len(signal_evals),
            "trade_decisions": sum(1 for r in signal_evals if r.get("decision") == "TRADE"),
            "skip_decisions": sum(1 for r in signal_evals if r.get("decision") == "SKIP"),
        },
        "gate_audit": {
            "total_rows": len(gate_audit),
        },
    }


def write_readme(out_dir: Path, summary: dict[str, Any], since: datetime, hours: float) -> None:
    lines = [
        "# Truth Dataset Export\n",
        f"Generated: `{summary['export_generated_at']}`  \n",
        f"Lookback: `{hours}h` (since `{since.isoformat()}`)  \n",
        "",
        "## Files in this directory\n",
        "| File | Description | Rows |",
        "|------|-------------|------|",
        f"| `poly_fills.csv` | Ground-truth CLOB fills from Polymarket data-api (append-only) | {summary['poly_fills']['total_rows']} |",
        f"| `poly_fills_enriched.csv` | poly_fills LEFT JOINed with trade_bible + signal_evaluations (one row per fill with engine context) | {summary['poly_fills']['total_rows']} |",
        f"| `trade_bible.csv` | Engine-side resolved trade records | {summary['trade_bible']['total_rows']} |",
        f"| `signal_evaluations.csv` | Every 2s TRADE/SKIP decision with gate context | {summary['signal_evaluations']['total_rows']} |",
        f"| `gate_audit.csv` | Per-window gate decision audit | {summary['gate_audit']['total_rows']} |",
        f"| `summary.json` | Aggregates + integrity check | — |",
        "",
        "## Integrity check\n",
        f"- Actual BUY gross (on-chain): **${summary['integrity_check']['actual_buy_gross']}**",
        f"- Recorded stake (trade_bible): **${summary['integrity_check']['recorded_stake']}**",
        f"- **Unrecorded spend**: **${summary['integrity_check']['unrecorded_spend_usd']}**",
        f"- Verdict: **{summary['integrity_check']['verdict']}**",
        "",
        "## Multi-fill breakdown\n",
        f"- Single-fill windows: {summary['poly_fills']['single_fill_windows']}",
        f"- Double-fill windows: {summary['poly_fills']['double_fill_windows']}",
        f"- Triple+ fill windows: {summary['poly_fills']['triple_or_more_fill_windows']}",
        f"- **Multi-fill %**: {summary['poly_fills']['multi_fill_pct']}%",
        f"- Total BUY gross: ${summary['poly_fills']['total_buy_gross_usd']}",
        "",
        "## Trade performance\n",
        f"- Total trades: {summary['trade_bible']['total_rows']}",
        f"- Wins: {summary['trade_bible']['wins']}",
        f"- Losses: {summary['trade_bible']['losses']}",
        f"- WR: **{summary['trade_bible']['wr_pct']}%**",
        f"- Recorded P&L: **${summary['trade_bible']['total_recorded_pnl_usd']}**",
        "",
        "## Loading into pandas\n",
        "```python",
        "import pandas as pd",
        "from pathlib import Path",
        "",
        "d = Path('docs/truth_dataset/<THIS_DIR>')  # replace with actual timestamp",
        "fills = pd.read_csv(d / 'poly_fills.csv', parse_dates=['match_time_utc', 'verified_at', 'created_at'])",
        "enriched = pd.read_csv(d / 'poly_fills_enriched.csv', parse_dates=['match_time_utc', 'placed_at', 'resolved_at', 'se_evaluated_at'])",
        "tb = pd.read_csv(d / 'trade_bible.csv', parse_dates=['placed_at', 'resolved_at', 'created_at'])",
        "se = pd.read_csv(d / 'signal_evaluations.csv', parse_dates=['evaluated_at'])",
        "",
        "# Example: multi-fill windows joined with the engine's decision context",
        "multi = enriched[enriched['is_multi_fill'] == True]",
        "print(multi.groupby('multi_fill_total')['cost_usd'].agg(['count', 'sum', 'mean']))",
        "",
        "# Example: WR by regime",
        "tb_wr = tb.groupby('regime')['trade_outcome'].value_counts().unstack().fillna(0)",
        "tb_wr['wr'] = tb_wr.get('WIN', 0) / (tb_wr.get('WIN', 0) + tb_wr.get('LOSS', 0))",
        "print(tb_wr)",
        "```",
        "",
        "## Refreshing this export\n",
        "```bash",
        "cd /Users/.../brave-archimedes",
        "DATABASE_URL='postgresql://...@hopper.proxy.rlwy.net:35772/railway' \\",
        f"  python3 scripts/export_truth_dataset.py --hours {int(hours)}",
        "```",
        "",
        "The script is read-only against Railway — safe to run anytime.",
    ]
    (out_dir / "README.md").write_text("\n".join(lines))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hours", type=float, default=DEFAULT_HOURS, help=f"Lookback window (default {DEFAULT_HOURS}h)")
    p.add_argument("--out", help="Output directory (default docs/truth_dataset/YYYYMMDD-HHMMSS)")
    p.add_argument("--skip", nargs="+", default=[], help="Table names to skip")
    args = p.parse_args()

    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)

    repo_root = Path(__file__).resolve().parent.parent
    if args.out:
        out_dir = Path(args.out)
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out_dir = repo_root / "docs" / "truth_dataset" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output dir: {out_dir}")
    print(f"Lookback:   {args.hours}h (since {since.isoformat()})")
    print()

    conn = psycopg2.connect(_get_db_url())
    try:
        poly_fills_rows: list[dict[str, Any]] = []
        tb_rows: list[dict[str, Any]] = []
        se_rows: list[dict[str, Any]] = []
        ga_rows: list[dict[str, Any]] = []

        if "poly_fills" not in args.skip:
            poly_fills_rows = export_poly_fills(conn, since)
            n = _dump_csv(poly_fills_rows, out_dir / "poly_fills.csv")
            print(f"  poly_fills.csv:            {n:>6} rows")

        if "poly_fills_enriched" not in args.skip:
            enriched = export_poly_fills_enriched(conn, since)
            n = _dump_csv(enriched, out_dir / "poly_fills_enriched.csv")
            print(f"  poly_fills_enriched.csv:   {n:>6} rows")

        if "trade_bible" not in args.skip:
            tb_rows = export_trade_bible(conn, since)
            n = _dump_csv(tb_rows, out_dir / "trade_bible.csv")
            print(f"  trade_bible.csv:           {n:>6} rows")

        if "signal_evaluations" not in args.skip:
            se_rows = export_signal_evaluations(conn, since)
            n = _dump_csv(se_rows, out_dir / "signal_evaluations.csv")
            print(f"  signal_evaluations.csv:    {n:>6} rows")

        if "gate_audit" not in args.skip:
            ga_rows = export_gate_audit(conn, since)
            n = _dump_csv(ga_rows, out_dir / "gate_audit.csv")
            print(f"  gate_audit.csv:            {n:>6} rows")

        summary = build_summary(poly_fills_rows, tb_rows, se_rows, ga_rows, since)
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
        print(f"  summary.json:              (aggregates + integrity check)")

        write_readme(out_dir, summary, since, args.hours)
        print(f"  README.md:                 (human-readable description)")

        print()
        print("=== INTEGRITY CHECK ===")
        print(f"  Actual spent:     ${summary['integrity_check']['actual_buy_gross']:>10.2f}")
        print(f"  Recorded stake:   ${summary['integrity_check']['recorded_stake']:>10.2f}")
        print(f"  Unrecorded gap:   ${summary['integrity_check']['unrecorded_spend_usd']:>10.2f}")
        print(f"  Verdict: {summary['integrity_check']['verdict']}")
        print()
        print(f"Open the export:")
        print(f"  open {out_dir}")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
