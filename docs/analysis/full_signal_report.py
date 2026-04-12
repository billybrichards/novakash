#!/usr/bin/env python3
"""
Full Signal Evaluation Report
Runs all standard analyses against Railway PostgreSQL and prints a formatted report.

Usage:
    # Preferred: get PUB_URL from Railway dashboard (DATABASE_PUBLIC_URL)
    export PUB_URL="postgresql://postgres:PASSWORD@hopper.proxy.rlwy.net:35772/railway"
    python3 docs/analysis/full_signal_report.py

    # Alternative: get via Montreal SSH (requires AWS access)
    # ssh-keygen -t ed25519 -f /tmp/k -N "" -q
    # aws ec2-instance-connect send-ssh-public-key --region ca-central-1 \
    #   --instance-id i-0785ed930423ae9fd --instance-os-user ubuntu \
    #   --ssh-public-key "$(cat /tmp/k.pub)"
    # ssh -i /tmp/k ubuntu@15.223.247.178 \
    #   "sudo grep '^DATABASE_URL=' /home/novakash/novakash/engine/.env | sed 's/postgresql+asyncpg/postgresql/'"

    # Hub API (no DB needed — for quick checks):
    # TOKEN=$(curl -s -X POST http://3.98.114.0:8091/auth/login \
    #   -H "Content-Type: application/json" \
    #   -d '{"username":"billy","password":"novakash2026"}' \
    #   | python3 -c "import json,sys; print(json.load(sys.stdin).get('access_token',''))")
    # curl -s "http://3.98.114.0:8091/api/v58/accuracy?limit=100" -H "Authorization: Bearer $TOKEN"

Options:
    --hours N     Look-back window for "recent" sections (default: 4)
    --asset X     Asset to analyse (default: BTC)
    --no-color    Disable ANSI colour output
"""

import asyncio
import asyncpg
import os
import sys
import argparse
from datetime import datetime, timezone

# ── Config ─────────────────────────────────────────────────────────────────────

DB = (
    os.environ.get("PUB_URL")
    or os.environ.get("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")
)

# Ground truth expression (requires se + ws in scope)
GROUND_TRUTH = """(
    (se.v2_direction = 'UP'   AND ws.close_price > ws.open_price)
    OR (se.v2_direction = 'DOWN' AND ws.close_price < ws.open_price)
)"""

# ── Helpers ────────────────────────────────────────────────────────────────────

USE_COLOR = True


def c(code: str, text: str) -> str:
    if not USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(t):   return c("1", t)
def green(t):  return c("32", t)
def red(t):    return c("31", t)
def yellow(t): return c("33", t)
def cyan(t):   return c("36", t)
def dim(t):    return c("2", t)


def section(title: str):
    width = 72
    print()
    print(bold(cyan("=" * width)))
    print(bold(cyan(f"  {title}")))
    print(bold(cyan("=" * width)))


def subsection(title: str):
    print()
    print(bold(f"-- {title} --"))


def bar(accuracy: float, width: int = 20) -> str:
    """ASCII bar showing distance from 50%."""
    if accuracy is None:
        return ""
    filled = max(0, int((float(accuracy) - 50) * width / 50))
    return green("█" * filled) if float(accuracy) >= 55 else (
        yellow("█" * filled) if float(accuracy) >= 50 else red("░" * max(0, int((50 - float(accuracy)) * width / 50)))
    )


def acc_color(accuracy: float) -> str:
    if accuracy is None:
        return dim("N/A")
    a = float(accuracy)
    txt = f"{a:5.1f}%"
    if a >= 65:
        return green(txt)
    if a >= 55:
        return yellow(txt)
    return red(txt)


async def safe_fetch(conn, query: str, *args):
    try:
        return await conn.fetch(query, *args)
    except Exception as e:
        print(red(f"  [query error] {e}"))
        return []


# ── Section 1: Data Coverage ───────────────────────────────────────────────────

async def report_coverage(conn, asset: str):
    section("1. DATA COVERAGE")

    rows = await safe_fetch(conn, """
        SELECT
            COUNT(DISTINCT ws.window_ts) AS windows,
            MIN(ws.open_time) AS earliest,
            MAX(ws.close_time) AS latest
        FROM window_snapshots ws
        WHERE ws.asset = $1 AND ws.close_price > 0 AND ws.open_price > 0
    """, asset)
    if rows:
        r = rows[0]
        print(f"  Windows (with price data):  {bold(str(r['windows']))}")
        print(f"  Date range:                 {r['earliest']} → {r['latest']}")

    rows2 = await safe_fetch(conn, """
        SELECT COUNT(*) AS n, MIN(evaluated_at) AS earliest, MAX(evaluated_at) AS latest
        FROM signal_evaluations WHERE asset = $1
    """, asset)
    if rows2:
        r = rows2[0]
        print(f"  Signal evaluations total:   {bold(str(r['n']))}")
        print(f"  Eval date range:            {r['earliest']} → {r['latest']}")

    rows3 = await safe_fetch(conn, """
        SELECT strategy_id, action, COUNT(*) AS n
        FROM strategy_decisions
        WHERE asset = $1
        GROUP BY 1, 2
        ORDER BY 1, 2
    """, asset)
    if rows3:
        print(f"  Strategy decisions:")
        for r in rows3:
            print(f"    {r['strategy_id']:20s} {r['action']:6s}: {r['n']}")


# ── Section 2: Current Market Regime ──────────────────────────────────────────

async def report_regime(conn, asset: str, hours: int):
    section(f"2. CURRENT MARKET REGIME (last {hours}h)")

    rows = await safe_fetch(conn, """
        SELECT
            COUNT(*) AS windows,
            SUM(CASE WHEN close_price > open_price THEN 1 ELSE 0 END) AS up_windows,
            SUM(CASE WHEN close_price < open_price THEN 1 ELSE 0 END) AS down_windows,
            ROUND(AVG(close_price - open_price)::numeric, 2) AS avg_delta
        FROM window_snapshots
        WHERE asset = $1
          AND close_price > 0 AND open_price > 0
          AND close_time >= NOW() - ($2 || ' hours')::interval
    """, asset, str(hours))
    if rows and rows[0]['windows']:
        r = rows[0]
        up_pct = 100.0 * r['up_windows'] / r['windows']
        dn_pct = 100.0 * r['down_windows'] / r['windows']
        print(f"  Windows (last {hours}h):  {r['windows']}")
        print(f"  UP windows:         {up_pct:.1f}%  ({r['up_windows']})")
        print(f"  DOWN windows:       {dn_pct:.1f}%  ({r['down_windows']})")
        print(f"  Avg price delta:    ${r['avg_delta']}")
    else:
        print(red(f"  No window data in last {hours}h"))

    rows2 = await safe_fetch(conn, """
        SELECT
            ROUND(AVG(se.vpin)::numeric, 3) AS avg_vpin,
            regime,
            COUNT(*) AS n
        FROM signal_evaluations se
        WHERE se.asset = $1
          AND se.evaluated_at >= NOW() - ($2 || ' hours')::interval
          AND se.regime IS NOT NULL
        GROUP BY regime
        ORDER BY n DESC
        LIMIT 6
    """, asset, str(hours))
    if rows2:
        print(f"\n  HMM Regime distribution (last {hours}h):")
        for r in rows2:
            marker = bold(" <-- dominant") if r == rows2[0] else ""
            print(f"    {r['regime']:20s}: {r['n']}{marker}")
        # average VPIN
        rows_vpin = await safe_fetch(conn, """
            SELECT ROUND(AVG(vpin)::numeric, 3) AS avg_vpin
            FROM signal_evaluations
            WHERE asset = $1 AND evaluated_at >= NOW() - ($2 || ' hours')::interval
        """, asset, str(hours))
        if rows_vpin:
            print(f"\n  Avg VPIN (last {hours}h):  {rows_vpin[0]['avg_vpin']}")


# ── Section 3: Ungated Signal Performance ─────────────────────────────────────

async def report_ungated(conn, asset: str, hours: int):
    section("3. UNGATED SIGNAL PERFORMANCE")

    for label, time_clause in [
        (f"Last {hours}h", f"AND se.evaluated_at >= NOW() - ('{hours} hours')::interval"),
        ("All-time",       ""),
    ]:
        subsection(f"3a. Overall — {label}")

        rows = await safe_fetch(conn, f"""
            SELECT
                COUNT(*) AS n,
                ROUND(
                    100.0 * SUM(CASE WHEN {GROUND_TRUTH} THEN 1 ELSE 0 END)::numeric / COUNT(*), 1
                ) AS accuracy,
                ROUND(AVG(ABS(COALESCE(se.v2_probability_up, 0.5) - 0.5))::numeric, 3) AS avg_dist,
                SUM(CASE WHEN se.v2_direction='UP'   THEN 1 ELSE 0 END) AS called_up,
                SUM(CASE WHEN se.v2_direction='DOWN' THEN 1 ELSE 0 END) AS called_down
            FROM signal_evaluations se
            JOIN window_snapshots ws
                ON se.window_ts = ws.window_ts::bigint AND se.asset = ws.asset
            WHERE se.asset = $1
              AND se.eval_offset BETWEEN 90 AND 150
              AND se.v2_direction IS NOT NULL
              AND ws.close_price > 0 AND ws.open_price > 0
              AND ABS(COALESCE(se.v2_probability_up, 0.5) - 0.5) >= 0.12
              {time_clause}
        """, asset)
        if rows and rows[0]['n']:
            r = rows[0]
            print(f"  Accuracy:       {acc_color(r['accuracy'])}  (n={r['n']})")
            print(f"  Avg dist:       {r['avg_dist']}")
            print(f"  Called UP:      {r['called_up']}  DOWN: {r['called_down']}")
        else:
            print(red(f"  No data ({label})"))

    subsection("3b. Accuracy by eval_offset bucket (15s) — All-time")
    rows = await safe_fetch(conn, f"""
        SELECT
            FLOOR(se.eval_offset / 15.0) * 15 AS b,
            COUNT(*) AS n,
            ROUND(
                100.0 * SUM(CASE WHEN {GROUND_TRUTH} THEN 1 ELSE 0 END)::numeric / COUNT(*), 1
            ) AS acc,
            ROUND(AVG(ABS(COALESCE(se.v2_probability_up, 0.5) - 0.5))::numeric, 3) AS dist
        FROM signal_evaluations se
        JOIN window_snapshots ws
            ON se.window_ts = ws.window_ts::bigint AND se.asset = ws.asset
        WHERE se.asset = $1
          AND se.eval_offset BETWEEN 30 AND 240
          AND se.v2_direction IS NOT NULL
          AND ws.close_price > 0 AND ws.open_price > 0
        GROUP BY 1
        ORDER BY 1 DESC
    """, asset)
    print(f"  {'T-':>5}  {'Acc':>7}  {'n':>6}  {'dist':>6}  {'bar'}")
    print(f"  {'----':>5}  {'---':>7}  {'----':>6}  {'----':>6}")
    for r in rows:
        acc = float(r['acc'] or 0)
        print(f"  T-{int(r['b']):3d}  {acc_color(r['acc'])}  {r['n']:6d}  {r['dist']:6.3f}  {bar(acc)}")

    subsection("3c. Accuracy by confidence band (T-90 to T-150) — All-time")
    rows = await safe_fetch(conn, f"""
        SELECT
            CASE
                WHEN ABS(COALESCE(se.v2_probability_up, 0.5) - 0.5) < 0.06 THEN '1_weak(<6%)'
                WHEN ABS(COALESCE(se.v2_probability_up, 0.5) - 0.5) < 0.12 THEN '2_mod(6-12%)'
                WHEN ABS(COALESCE(se.v2_probability_up, 0.5) - 0.5) < 0.20 THEN '3_strong(12-20%)'
                ELSE '4_high(>20%)'
            END AS band,
            COUNT(*) AS n,
            ROUND(
                100.0 * SUM(CASE WHEN {GROUND_TRUTH} THEN 1 ELSE 0 END)::numeric / COUNT(*), 1
            ) AS acc
        FROM signal_evaluations se
        JOIN window_snapshots ws
            ON se.window_ts = ws.window_ts::bigint AND se.asset = ws.asset
        WHERE se.asset = $1
          AND se.eval_offset BETWEEN 90 AND 150
          AND se.v2_direction IS NOT NULL
          AND ws.close_price > 0 AND ws.open_price > 0
        GROUP BY 1
        ORDER BY 1
    """, asset)
    for r in rows:
        band_label = r['band'].split('_', 1)[1]
        trade_flag = green(" [TRADE]") if float(r['acc'] or 0) >= 55 else red(" [SKIP]")
        print(f"  {band_label:20s}  {acc_color(r['acc'])}  n={r['n']:6d}  {bar(float(r['acc'] or 0))}{trade_flag}")


# ── Section 4: V4 Paper Trade Performance ─────────────────────────────────────

async def report_v4(conn, asset: str, hours: int):
    section(f"4. V4 PAPER TRADE PERFORMANCE (last {hours}h)")

    # TRADE decisions with outcomes
    rows = await safe_fetch(conn, f"""
        SELECT
            sd.direction,
            sd.eval_offset,
            sd.evaluated_at,
            ws.close_price,
            ws.open_price,
            CASE
                WHEN ws.close_price = 0 OR ws.open_price = 0 THEN 'UNRESOLVED'
                WHEN (sd.direction = 'UP'   AND ws.close_price > ws.open_price)
                  OR (sd.direction = 'DOWN' AND ws.close_price < ws.open_price) THEN 'WIN'
                ELSE 'LOSS'
            END AS outcome
        FROM strategy_decisions sd
        JOIN window_snapshots ws
            ON sd.window_ts = ws.window_ts::bigint AND sd.asset = ws.asset
        WHERE sd.strategy_id = 'v4_fusion'
          AND sd.action = 'TRADE'
          AND sd.asset = $1
          AND sd.evaluated_at >= NOW() - ('{hours} hours')::interval
        ORDER BY sd.evaluated_at DESC
    """, asset)

    wins = sum(1 for r in rows if r['outcome'] == 'WIN')
    losses = sum(1 for r in rows if r['outcome'] == 'LOSS')
    unres = sum(1 for r in rows if r['outcome'] == 'UNRESOLVED')
    total = len(rows)

    if total == 0:
        print(yellow(f"  No V4 TRADE decisions in last {hours}h"))
    else:
        wr = 100.0 * wins / (wins + losses) if (wins + losses) > 0 else None
        print(f"  TRADE decisions:   {bold(str(total))}")
        print(f"    WIN:             {green(str(wins))}")
        print(f"    LOSS:            {red(str(losses))}")
        print(f"    Unresolved:      {dim(str(unres))}")
        print(f"  Win rate:          {acc_color(wr)}")
        print()
        print(f"  Recent trades:")
        for r in rows[:10]:
            outcome_str = green("WIN") if r['outcome'] == 'WIN' else (
                red("LOSS") if r['outcome'] == 'LOSS' else dim("?")
            )
            print(f"    {str(r['evaluated_at'])[:19]}  {r['direction']:4s}  T-{r['eval_offset']:3.0f}  {outcome_str}")

    # Skip reason distribution
    rows2 = await safe_fetch(conn, f"""
        SELECT skip_reason, COUNT(*) AS n
        FROM strategy_decisions
        WHERE strategy_id = 'v4_fusion'
          AND action = 'SKIP'
          AND asset = $1
          AND evaluated_at >= NOW() - ('{hours} hours')::interval
        GROUP BY 1
        ORDER BY 2 DESC
    """, asset)
    if rows2:
        total_skips = sum(r['n'] for r in rows2)
        print(f"\n  SKIP decisions:    {total_skips}")
        for r in rows2:
            pct = 100.0 * r['n'] / total_skips
            print(f"    {(r['skip_reason'] or 'unknown'):35s}: {r['n']:4d} ({pct:4.1f}%)")
    else:
        print(dim(f"\n  No V4 SKIP decisions in last {hours}h"))


# ── Section 5: V10 Ghost Performance ──────────────────────────────────────────

async def report_v10(conn, asset: str, hours: int):
    section(f"5. V10 GHOST PERFORMANCE (last {hours}h)")

    rows = await safe_fetch(conn, f"""
        SELECT COUNT(*) AS n FROM strategy_decisions
        WHERE strategy_id = 'v10_gate' AND asset = $1
          AND evaluated_at >= NOW() - ('{hours} hours')::interval
    """, asset)
    total = rows[0]['n'] if rows else 0
    print(f"  V10 eval count (last {hours}h):  {total}")

    rows2 = await safe_fetch(conn, f"""
        SELECT
            COALESCE(sd.gate_failed, sd.skip_reason, 'passed') AS gate,
            sd.action,
            COUNT(*) AS n
        FROM strategy_decisions sd
        WHERE sd.strategy_id = 'v10_gate'
          AND sd.asset = $1
          AND sd.evaluated_at >= NOW() - ('{hours} hours')::interval
        GROUP BY 1, 2
        ORDER BY 3 DESC
        LIMIT 15
    """, asset)
    if rows2:
        print(f"\n  Gate failure distribution:")
        for r in rows2:
            print(f"    {r['action']:5s}  {(r['gate'] or 'none'):35s}: {r['n']}")

    # Would-have outcomes for TRADE decisions
    rows3 = await safe_fetch(conn, f"""
        SELECT
            sd.direction,
            CASE
                WHEN ws.close_price = 0 OR ws.open_price = 0 THEN 'UNRESOLVED'
                WHEN (sd.direction = 'UP'   AND ws.close_price > ws.open_price)
                  OR (sd.direction = 'DOWN' AND ws.close_price < ws.open_price) THEN 'WIN'
                ELSE 'LOSS'
            END AS outcome,
            COUNT(*) AS n
        FROM strategy_decisions sd
        JOIN window_snapshots ws
            ON sd.window_ts = ws.window_ts::bigint AND sd.asset = ws.asset
        WHERE sd.strategy_id = 'v10_gate'
          AND sd.action = 'TRADE'
          AND sd.asset = $1
          AND sd.evaluated_at >= NOW() - ('{hours} hours')::interval
        GROUP BY 1, 2
        ORDER BY 1, 2
    """, asset)
    if rows3:
        wins = sum(r['n'] for r in rows3 if r['outcome'] == 'WIN')
        losses = sum(r['n'] for r in rows3 if r['outcome'] == 'LOSS')
        wr = 100.0 * wins / (wins + losses) if (wins + losses) > 0 else None
        print(f"\n  Would-have TRADE outcomes:")
        for r in rows3:
            print(f"    {r['direction']:4s}  {r['outcome']:10s}: {r['n']}")
        print(f"  Would-have WR:  {acc_color(wr)}")
    else:
        print(dim(f"\n  No V10 TRADE decisions in last {hours}h"))


# ── Section 6: CLOB Divergence ────────────────────────────────────────────────

async def report_clob(conn, asset: str, hours: int):
    section(f"6. CLOB DIVERGENCE CHECK (last {hours}h)")

    rows = await safe_fetch(conn, f"""
        SELECT
            FLOOR(se.eval_offset / 30.0) * 30 AS b,
            ROUND(AVG(
                CASE WHEN se.v2_direction = 'UP'   THEN se.clob_up_ask
                     WHEN se.v2_direction = 'DOWN' THEN se.clob_down_ask
                END
            )::numeric, 3) AS avg_ask,
            ROUND(AVG(ABS(COALESCE(se.v2_probability_up, 0.5) - 0.5))::numeric, 3) AS avg_dist,
            ROUND(
                100.0 * SUM(CASE WHEN {GROUND_TRUTH} THEN 1 ELSE 0 END)::numeric / COUNT(*), 1
            ) AS accuracy,
            COUNT(*) AS n
        FROM signal_evaluations se
        JOIN window_snapshots ws
            ON se.window_ts = ws.window_ts::bigint AND se.asset = ws.asset
        WHERE se.asset = $1
          AND se.v2_direction IS NOT NULL
          AND ws.close_price > 0 AND ws.open_price > 0
          AND (se.clob_up_ask IS NOT NULL OR se.clob_down_ask IS NOT NULL)
          AND se.evaluated_at >= NOW() - ('{hours} hours')::interval
          AND ABS(COALESCE(se.v2_probability_up, 0.5) - 0.5) >= 0.12
        GROUP BY 1
        ORDER BY 1 DESC
    """, asset)

    if not rows:
        print(yellow(f"  No CLOB data in last {hours}h"))
        return

    print(f"  {'T-':>5}  {'CLOB ask':>9}  {'Acc':>7}  {'avg_dist':>8}  {'n':>6}")
    print(f"  {'----':>5}  {'---------':>9}  {'---':>7}  {'--------':>8}  {'----':>6}")
    for r in rows:
        ask = r['avg_ask']
        ask_str = f"${float(ask):.3f}" if ask else "  N/A "
        edge_note = ""
        if ask and float(ask) <= 0.58:
            edge_note = green("  <= $0.58 [trade zone]")
        elif ask and float(ask) > 0.62:
            edge_note = red("  > $0.62 [skip]")
        print(f"  T-{int(r['b']):3d}  {ask_str:9s}  {acc_color(r['accuracy'])}  {str(r['avg_dist']):8s}  {r['n']:6d}{edge_note}")


# ── Section 7: Config Recommendations ─────────────────────────────────────────

async def report_recommendations(conn, asset: str, hours: int):
    section("7. CONFIG RECOMMENDATIONS")

    # Fetch recent accuracy
    rows = await safe_fetch(conn, f"""
        SELECT
            ROUND(
                100.0 * SUM(CASE WHEN {GROUND_TRUTH} THEN 1 ELSE 0 END)::numeric / COUNT(*), 1
            ) AS accuracy,
            COUNT(*) AS n
        FROM signal_evaluations se
        JOIN window_snapshots ws
            ON se.window_ts = ws.window_ts::bigint AND se.asset = ws.asset
        WHERE se.asset = $1
          AND se.eval_offset BETWEEN 90 AND 150
          AND se.v2_direction IS NOT NULL
          AND ws.close_price > 0 AND ws.open_price > 0
          AND ABS(COALESCE(se.v2_probability_up, 0.5) - 0.5) >= 0.12
          AND se.evaluated_at >= NOW() - ('{hours} hours')::interval
    """, asset)

    if not rows or not rows[0]['n']:
        print(yellow(f"  Insufficient data in last {hours}h for recommendations"))
        return

    acc = float(rows[0]['accuracy'] or 0)
    n = rows[0]['n']

    print(f"  Recent accuracy ({hours}h, dist>=0.12, T-90-150):  {acc_color(acc)}  (n={n})")
    print()

    if acc > 65:
        print(green("  RECOMMENDATION: Signal strong. Keep config. Monitor for regime change."))
        print(green("  Consider increasing position size if sample >= 50 decisions."))
    elif acc >= 55:
        print(yellow("  RECOMMENDATION: Signal adequate. Keep config. Maintain position size."))
    elif acc >= 45:
        print(yellow("  RECOMMENDATION: Signal weakening. Consider tightening confidence to 0.15."))
        print(yellow("  Do not increase position size. Review regime."))
    else:
        print(red("  RECOMMENDATION: Signal below random. PAUSE TRADING."))
        print(red("  Investigate: regime change? Feed issue? VPIN=0 (WebSocket dead)?"))

    # Check V4 trade count
    v4_rows = await safe_fetch(conn, f"""
        SELECT COUNT(*) AS n FROM strategy_decisions
        WHERE strategy_id = 'v4_fusion' AND action = 'TRADE' AND asset = $1
          AND evaluated_at >= NOW() - ('{hours} hours')::interval
    """, asset)
    v4_trades = v4_rows[0]['n'] if v4_rows else 0

    print()
    if v4_trades == 0:
        print(yellow(f"  V4 has 0 TRADE decisions in last {hours}h. Checking gates..."))
        skip_rows = await safe_fetch(conn, f"""
            SELECT skip_reason, COUNT(*) AS n
            FROM strategy_decisions
            WHERE strategy_id = 'v4_fusion' AND action = 'SKIP' AND asset = $1
              AND evaluated_at >= NOW() - ('{hours} hours')::interval
            GROUP BY 1 ORDER BY 2 DESC LIMIT 5
        """, asset)
        if skip_rows:
            print(f"  Top skip reasons:")
            for r in skip_rows:
                print(f"    {(r['skip_reason'] or 'unknown'):35s}: {r['n']}")
            top = skip_rows[0]['skip_reason'] or ''
            if 'confidence' in top.lower():
                print(yellow("\n  ACTION: Signal is flat (low confidence). Market may be choppy."))
            elif 'regime' in top.lower() or 'risk_off' in top.lower():
                print(yellow("\n  ACTION: Regime gate blocking. Check if risk_off regime is justified."))
            elif 'timing' in top.lower():
                print(yellow("\n  ACTION: Timing gate blocking. Verify eval_offset range matches T-90-120."))
        else:
            print(yellow("  No SKIP decisions either — V4 may not be receiving evals."))
    else:
        print(green(f"  V4 placed {v4_trades} trade(s) in last {hours}h."))

    # Confidence threshold comparison
    print()
    subsection("Confidence threshold comparison (T-90-150, all-time)")
    for thresh_label, thresh in [("0.10", 0.10), ("0.12", 0.12), ("0.15", 0.15), ("0.20", 0.20)]:
        rows = await safe_fetch(conn, f"""
            SELECT
                COUNT(*) AS n,
                ROUND(
                    100.0 * SUM(CASE WHEN {GROUND_TRUTH} THEN 1 ELSE 0 END)::numeric / COUNT(*), 1
                ) AS acc
            FROM signal_evaluations se
            JOIN window_snapshots ws
                ON se.window_ts = ws.window_ts::bigint AND se.asset = ws.asset
            WHERE se.asset = $1
              AND se.eval_offset BETWEEN 90 AND 150
              AND se.v2_direction IS NOT NULL
              AND ws.close_price > 0 AND ws.open_price > 0
              AND ABS(COALESCE(se.v2_probability_up, 0.5) - 0.5) >= $2
        """, asset, thresh)
        if rows and rows[0]['n']:
            current = bold(" <-- CURRENT") if thresh_label == "0.12" else ""
            print(f"  dist >= {thresh_label}:  {acc_color(rows[0]['acc'])}  n={rows[0]['n']}{current}")


# ── Main ───────────────────────────────────────────────────────────────────────

async def run(asset: str, hours: int):
    if not DB:
        print(red("ERROR: Set PUB_URL environment variable."))
        print("  export PUB_URL=\"postgresql://postgres:PASSWORD@hopper.proxy.rlwy.net:35772/railway\"")
        sys.exit(1)

    print(bold(f"\nSignal Evaluation Report — {asset} — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"))
    print(dim(f"DB: {DB[:60]}..."))

    conn = None
    try:
        conn = await asyncpg.connect(DB, timeout=60)
        await report_coverage(conn, asset)
        await report_regime(conn, asset, hours)
        await report_ungated(conn, asset, hours)
        await report_v4(conn, asset, hours)
        await report_v10(conn, asset, hours)
        await report_clob(conn, asset, hours)
        await report_recommendations(conn, asset, hours)
    except asyncpg.exceptions.InvalidPasswordError:
        print(red("ERROR: Invalid database password. Check PUB_URL."))
        sys.exit(1)
    except OSError as e:
        print(red(f"ERROR: Cannot connect to database: {e}"))
        sys.exit(1)
    finally:
        if conn:
            await conn.close()

    print()
    print(dim("=" * 72))
    print(dim("Report complete."))
    print()


def main():
    global USE_COLOR

    parser = argparse.ArgumentParser(description="Full signal evaluation report")
    parser.add_argument("--hours", type=int, default=4, help="Look-back window in hours (default: 4)")
    parser.add_argument("--asset", type=str, default="BTC", help="Asset to analyse (default: BTC)")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colour output")
    args = parser.parse_args()

    if args.no_color:
        USE_COLOR = False

    asyncio.run(run(asset=args.asset, hours=args.hours))


if __name__ == "__main__":
    main()
