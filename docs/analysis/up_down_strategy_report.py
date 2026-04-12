#!/usr/bin/env python3
"""
UP/DOWN Strategy Performance Report
See docs/analysis/UP_DOWN_STRATEGY_RUNBOOK.md for full documentation.

Usage:
    export PUB_URL="postgresql://postgres:PASSWORD@hopper.proxy.rlwy.net:35772/railway"
    python3 docs/analysis/up_down_strategy_report.py
    python3 docs/analysis/up_down_strategy_report.py --hours 48
"""
import asyncio
import asyncpg
import os
import sys
import argparse
from datetime import datetime, timezone

DB = os.environ.get("PUB_URL") or os.environ.get("DATABASE_URL", "").replace(
    "postgresql+asyncpg://", "postgresql://"
)

def green(s): return f"\033[32m{s}\033[0m"
def red(s): return f"\033[31m{s}\033[0m"
def cyan(s): return f"\033[36m{s}\033[0m"
def dim(s): return f"\033[2m{s}\033[0m"
def bold(s): return f"\033[1m{s}\033[0m"

def wr_color(wr):
    if wr is None: return dim("  N/A")
    if wr >= 70: return green(f"{wr:.1f}%")
    if wr >= 55: return f"\033[33m{wr:.1f}%\033[0m"
    return red(f"{wr:.1f}%")

async def safe_fetch(conn, q, *args):
    try:
        return await conn.fetch(q, *args)
    except Exception as exc:
        print(red(f"  Query error: {str(exc)[:80]}"))
        return []


async def report_context(conn, hours):
    print(cyan(f"\n{'='*60}"))
    print(bold(f"  UP/DOWN Strategy Report — last {hours}h"))
    print(dim(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"))
    print(cyan(f"{'='*60}"))
    total = await conn.fetchval("""
        SELECT COUNT(DISTINCT window_ts) FROM signal_evaluations
        WHERE asset='BTC' AND eval_offset BETWEEN 90 AND 150
          AND evaluated_at > NOW() - ($1 || ' hours')::interval
    """, str(hours))
    print(f"\n  Distinct windows with signal (T-90-150, last {hours}h): {total} of ~{hours*12} possible")


async def report_down_only(conn, hours):
    print(cyan(f"\n{'─'*60}"))
    print(bold("  V4 DOWN-ONLY  (dist≥0.12, DOWN, T-90-150, any hour)"))
    print(cyan(f"{'─'*60}"))

    rows = await safe_fetch(conn, """
        WITH best AS (
            SELECT DISTINCT ON (se.window_ts)
                se.window_ts,
                ABS(COALESCE(se.v2_probability_up,0.5)-0.5) AS dist,
                se.clob_down_ask,
                (SELECT c.price FROM ticks_chainlink c WHERE c.asset='BTC'
                 AND ABS(EXTRACT(EPOCH FROM c.ts)::bigint - se.window_ts) < 30
                 ORDER BY ABS(EXTRACT(EPOCH FROM c.ts)::bigint - se.window_ts) LIMIT 1
                ) AS open_price,
                (SELECT c.price FROM ticks_chainlink c WHERE c.asset='BTC'
                 AND ABS(EXTRACT(EPOCH FROM c.ts)::bigint - (se.window_ts+300)) < 30
                 ORDER BY ABS(EXTRACT(EPOCH FROM c.ts)::bigint - (se.window_ts+300)) LIMIT 1
                ) AS close_price
            FROM signal_evaluations se
            WHERE se.asset='BTC'
              AND se.eval_offset BETWEEN 90 AND 150
              AND se.v2_direction = 'DOWN'
              AND ABS(COALESCE(se.v2_probability_up,0.5)-0.5) >= 0.12
              AND se.evaluated_at > NOW() - ($1 || ' hours')::interval
            ORDER BY se.window_ts, se.eval_offset DESC
        )
        SELECT *, close_price - open_price AS btc_move,
               CASE WHEN close_price < open_price THEN 'WIN' ELSE 'LOSS' END AS outcome
        FROM best WHERE close_price IS NOT NULL AND open_price IS NOT NULL
        ORDER BY window_ts DESC
    """, str(hours))

    if not rows:
        print(dim("  No resolved windows"))
        return

    wins = [r for r in rows if r['outcome'] == 'WIN']
    losses = [r for r in rows if r['outcome'] == 'LOSS']
    wr = 100 * len(wins) / len(rows)

    print(f"\n  Trades: {len(rows)}   {green(f'WIN={len(wins)}')}   {red(f'LOSS={len(losses)}')}   WR: {wr_color(wr)}")
    print(f"  Rate: {len(rows)/hours:.1f}/hr\n")

    clob_bands = [
        ('>=0.75 (2.0x)', lambda r: r['clob_down_ask'] and r['clob_down_ask'] >= 0.75),
        ('0.55-0.75 (1.5x)', lambda r: r['clob_down_ask'] and 0.55 <= r['clob_down_ask'] < 0.75),
        ('0.35-0.55 (1.2x)', lambda r: r['clob_down_ask'] and 0.35 <= r['clob_down_ask'] < 0.55),
        ('<0.35 (1.0x)', lambda r: r['clob_down_ask'] and r['clob_down_ask'] < 0.35),
        ('no CLOB (1.0x)', lambda r: not r['clob_down_ask']),
    ]
    print(f"  {'CLOB band':22s}  {'n':>4}  {'WR%':>6}")
    for label, fn in clob_bands:
        br = [r for r in rows if fn(r)]
        if not br: continue
        bw = [r for r in br if r['outcome'] == 'WIN']
        print(f"  {label:22s}  {len(br):>4}  {wr_color(100*len(bw)/len(br))}")

    print(f"\n  {'Time':>5}  {'Open':>8}  {'Close':>8}  {'Move':>6}  {'CLOB':>5}  Result")
    for r in rows[:15]:
        ts = datetime.fromtimestamp(r['window_ts'], tz=timezone.utc).strftime('%H:%M')
        move = float(r['btc_move'] or 0)
        clob = f"{r['clob_down_ask']:.2f}" if r['clob_down_ask'] else "  --"
        result = green('WIN') if r['outcome'] == 'WIN' else red('LOSS')
        print(f"  {ts}    {r['open_price']:>8,.0f}   {r['close_price']:>8,.0f}   {move:>+6.0f}   {clob}   {result}")


async def report_up_asian(conn, hours):
    print(cyan(f"\n{'─'*60}"))
    print(bold("  V4 UP ASIAN  (dist 0.15-0.20, UP, T-90-150, hours 23/0/1/2 UTC)"))
    print(cyan(f"{'─'*60}"))

    rows = await safe_fetch(conn, """
        WITH best AS (
            SELECT DISTINCT ON (se.window_ts)
                se.window_ts,
                ABS(COALESCE(se.v2_probability_up,0.5)-0.5) AS dist,
                EXTRACT(HOUR FROM se.evaluated_at) AS hour_utc,
                (SELECT c.price FROM ticks_chainlink c WHERE c.asset='BTC'
                 AND ABS(EXTRACT(EPOCH FROM c.ts)::bigint - se.window_ts) < 30
                 ORDER BY ABS(EXTRACT(EPOCH FROM c.ts)::bigint - se.window_ts) LIMIT 1
                ) AS open_price,
                (SELECT c.price FROM ticks_chainlink c WHERE c.asset='BTC'
                 AND ABS(EXTRACT(EPOCH FROM c.ts)::bigint - (se.window_ts+300)) < 30
                 ORDER BY ABS(EXTRACT(EPOCH FROM c.ts)::bigint - (se.window_ts+300)) LIMIT 1
                ) AS close_price
            FROM signal_evaluations se
            WHERE se.asset='BTC'
              AND se.eval_offset BETWEEN 90 AND 150
              AND se.v2_direction = 'UP'
              AND ABS(COALESCE(se.v2_probability_up,0.5)-0.5) BETWEEN 0.15 AND 0.20
              AND EXTRACT(HOUR FROM se.evaluated_at) IN (23, 0, 1, 2)
              AND se.evaluated_at > NOW() - ($1 || ' hours')::interval
            ORDER BY se.window_ts, se.eval_offset DESC
        )
        SELECT *, close_price - open_price AS btc_move,
               CASE WHEN close_price > open_price THEN 'WIN' ELSE 'LOSS' END AS outcome
        FROM best WHERE close_price IS NOT NULL AND open_price IS NOT NULL
        ORDER BY window_ts DESC
    """, str(hours))

    if not rows:
        print(dim(f"\n  No trades — Asian session is 23:00-02:59 UTC (check if in last {hours}h)"))
        hour_dist = await safe_fetch(conn, """
            SELECT EXTRACT(HOUR FROM evaluated_at)::int AS h, COUNT(DISTINCT window_ts) n
            FROM signal_evaluations
            WHERE asset='BTC' AND eval_offset BETWEEN 90 AND 150
              AND v2_direction='UP'
              AND ABS(COALESCE(v2_probability_up,0.5)-0.5) BETWEEN 0.15 AND 0.20
              AND evaluated_at > NOW() - ($1 || ' hours')::interval
            GROUP BY 1 ORDER BY 1
        """, str(hours))
        if hour_dist:
            print("  UP signals (dist 0.15-0.20) by hour:")
            for r in hour_dist:
                marker = green(" ← ASIAN") if r['h'] in (23, 0, 1, 2) else ""
                print(f"    H{r['h']:02d}: {r['n']} windows{marker}")
        return

    wins = [r for r in rows if r['outcome'] == 'WIN']
    losses = [r for r in rows if r['outcome'] == 'LOSS']
    wr = 100 * len(wins) / len(rows)

    print(f"\n  Trades: {len(rows)}   {green(f'WIN={len(wins)}')}   {red(f'LOSS={len(losses)}')}   WR: {wr_color(wr)}")
    print(f"\n  {'Time':>5}  {'Hour':>4}  {'Dist':>5}  {'Open':>8}  {'Move':>6}  Result")
    for r in rows:
        ts = datetime.fromtimestamp(r['window_ts'], tz=timezone.utc).strftime('%H:%M')
        move = float(r['btc_move'] or 0)
        result = green('WIN') if r['outcome'] == 'WIN' else red('LOSS')
        print(f"  {ts}    H{int(r['hour_utc']):02d}   {r['dist']:.3f}   {r['open_price']:>8,.0f}   {move:>+6.0f}   {result}")


async def report_combined(conn, hours):
    print(cyan(f"\n{'─'*60}"))
    print(bold("  COMBINED"))
    print(cyan(f"{'─'*60}"))

    result = await conn.fetchrow("""
        WITH all_trades AS (
            SELECT DISTINCT ON (se.window_ts, se.v2_direction)
                se.v2_direction AS dir,
                (SELECT c.price FROM ticks_chainlink c WHERE c.asset='BTC'
                 AND ABS(EXTRACT(EPOCH FROM c.ts)::bigint - se.window_ts) < 30
                 ORDER BY ABS(EXTRACT(EPOCH FROM c.ts)::bigint - se.window_ts) LIMIT 1) AS open_price,
                (SELECT c.price FROM ticks_chainlink c WHERE c.asset='BTC'
                 AND ABS(EXTRACT(EPOCH FROM c.ts)::bigint - (se.window_ts+300)) < 30
                 ORDER BY ABS(EXTRACT(EPOCH FROM c.ts)::bigint - (se.window_ts+300)) LIMIT 1) AS close_price
            FROM signal_evaluations se
            WHERE se.asset='BTC' AND se.eval_offset BETWEEN 90 AND 150
              AND se.evaluated_at > NOW() - ($1 || ' hours')::interval
              AND (
                (se.v2_direction='DOWN' AND ABS(COALESCE(se.v2_probability_up,0.5)-0.5) >= 0.12)
                OR
                (se.v2_direction='UP'
                 AND ABS(COALESCE(se.v2_probability_up,0.5)-0.5) BETWEEN 0.15 AND 0.20
                 AND EXTRACT(HOUR FROM se.evaluated_at) IN (23,0,1,2))
              )
            ORDER BY se.window_ts, se.v2_direction, se.eval_offset DESC
        )
        SELECT
            COUNT(*) FILTER (WHERE close_price IS NOT NULL) AS n,
            SUM(CASE WHEN dir='DOWN' AND close_price < open_price THEN 1
                     WHEN dir='UP'   AND close_price > open_price THEN 1
                     ELSE 0 END) FILTER (WHERE close_price IS NOT NULL) AS wins,
            SUM(CASE WHEN dir='DOWN' AND close_price >= open_price THEN 1
                     WHEN dir='UP'   AND close_price <= open_price THEN 1
                     ELSE 0 END) FILTER (WHERE close_price IS NOT NULL) AS losses
        FROM all_trades
    """, str(hours))

    n = result['n'] or 0
    wins = result['wins'] or 0
    losses = result['losses'] or 0
    wr = 100 * wins / n if n else None
    print(f"\n  Total trades: {n}   {green(f'WIN={wins}')}   {red(f'LOSS={losses}')}   WR: {wr_color(wr)}")
    print(f"  Rate: {n/hours:.1f}/hr  ({n} of {hours*12} windows = {100*n//(hours*12) if hours*12 else 0}% hit rate)")


async def report_golive(conn):
    print(cyan(f"\n{'─'*60}"))
    print(bold("  LIVE CHECKLIST"))
    print(cyan(f"{'─'*60}\n"))

    paper = await conn.fetchrow("SELECT paper_enabled, live_enabled FROM system_state ORDER BY id DESC LIMIT 1")
    if paper:
        mode = "PAPER" if paper['paper_enabled'] else "LIVE"
        print(f"  Mode: {green(mode) if not paper['paper_enabled'] else mode}")

    last_eval = await conn.fetchrow("""
        SELECT strategy_id, action, evaluated_at FROM strategy_decisions
        WHERE strategy_id IN ('v4_down_only','v4_up_asian')
        ORDER BY evaluated_at DESC LIMIT 1
    """)
    if last_eval:
        age_s = (datetime.now(timezone.utc) - last_eval['evaluated_at'].replace(tzinfo=timezone.utc)).seconds
        print(f"  Engine: last eval {age_s}s ago ({last_eval['strategy_id']} {last_eval['action']})")

    clob = await conn.fetchval("""
        SELECT COUNT(*) FROM clob_book_snapshots
        WHERE ts > NOW() - INTERVAL '5 minutes' AND up_best_ask IS NOT NULL
    """)
    print(f"  CLOB: {green(f'{clob} rows/5min') if clob > 0 else 'null values — sizing at 1.0x base'}")
    print()


async def run(hours: int):
    if not DB:
        print("ERROR: Set PUB_URL environment variable")
        sys.exit(1)
    conn = await asyncpg.connect(DB, timeout=60)
    try:
        await report_context(conn, hours)
        await report_down_only(conn, hours)
        await report_up_asian(conn, hours)
        await report_combined(conn, hours)
        await report_golive(conn)
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="UP/DOWN strategy performance report")
    parser.add_argument("--hours", type=int, default=24, help="Look-back window in hours (default: 24)")
    args = parser.parse_args()
    asyncio.run(run(args.hours))
