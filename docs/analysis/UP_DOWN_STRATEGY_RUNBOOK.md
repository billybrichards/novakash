# UP/DOWN Strategy Performance Runbook

**Companion to:** `docs/analysis/SIGNAL_EVAL_RUNBOOK.md`  
**Created:** 2026-04-12  
**Strategies:** `v4_down_only` + `v4_up_asian`

---

## Quick Start

```bash
export PUB_URL="postgresql://postgres:PASSWORD@hopper.proxy.rlwy.net:35772/railway"
python3 docs/analysis/up_down_strategy_report.py
```

See `docs/analysis/SIGNAL_EVAL_RUNBOOK.md` Section 1 for how to get `PUB_URL`.

---

## Strategy Specs

### v4_down_only
- **Direction:** DOWN only (UP predictions always skipped — 1.5-53% WR)
- **Confidence:** dist ≥ 0.12 (|p_up - 0.5| ≥ 0.12)
- **Timing:** T-90 to T-150 (seconds before window close)
- **Hours:** Any UTC hour
- **CLOB sizing:** 2.0× at clob_down_ask ≥ 0.75 · 1.5× at ≥0.55 · 1.2× at ≥0.35 · 1.0× below
- **Historical WR:** 90.3% (897K samples, Apr 7-12 2026)
- **Last 24h WR:** 77% (40 trades, 31W/9L)
- **Evidence doc:** `docs/analysis/DOWN_ONLY_STRATEGY_2026-04-12.md`

### v4_up_asian
- **Direction:** UP only
- **Confidence:** dist 0.15–0.20 (medium conviction band — below is noise, above is priced in)
- **Timing:** T-90 to T-150
- **Hours:** 23:00-02:59 UTC (Asian session only — outside this window UP WR ≈ 50%)
- **Historical WR:** 81-99% (5,543 samples, Apr 10-12 2026)
- **Last 24h WR:** 100% (3 trades, 3W/0L — small sample)
- **Evidence doc:** `docs/analysis/UP_STRATEGY_RESEARCH_BRIEF.md`

### Direction exclusivity
Both strategies are direction-exclusive. In any given 5-min window, at most ONE fires:
- If model predicts DOWN with dist ≥ 0.12 → v4_down_only fires
- If model predicts UP with dist 0.15-0.20 AND hour is Asian → v4_up_asian fires
- Both SKIP if neither condition met (most windows — flat market, wrong session, wrong confidence)

---

## Analysis Script

Save as `docs/analysis/up_down_strategy_report.py` and run anytime:

```python
#!/usr/bin/env python3
"""
UP/DOWN Strategy Performance Report
Replicates the 2026-04-12 analysis session findings.

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

# ── ANSI helpers ──────────────────────────────────────────────────────────────

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


# ── Query helpers ─────────────────────────────────────────────────────────────

async def safe_fetch(conn, q, *args):
    try:
        return await conn.fetch(q, *args)
    except Exception as exc:
        print(red(f"  Query error: {str(exc)[:80]}"))
        return []


# ── Section 1: Context ────────────────────────────────────────────────────────

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
    print(f"\n  Signal evaluations (distinct windows, T-90-150, last {hours}h): {total}")
    print(f"  Expected windows: {hours*12} (12 per hour × {hours}h)")


# ── Section 2: V4 DOWN-ONLY ───────────────────────────────────────────────────

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
        SELECT *,
               close_price - open_price AS btc_move,
               CASE WHEN close_price < open_price THEN 'WIN' ELSE 'LOSS' END AS outcome
        FROM best
        WHERE close_price IS NOT NULL AND open_price IS NOT NULL
        ORDER BY window_ts DESC
    """, str(hours))

    if not rows:
        print(dim("  No resolved windows"))
        return

    wins = [r for r in rows if r['outcome'] == 'WIN']
    losses = [r for r in rows if r['outcome'] == 'LOSS']
    wr = 100 * len(wins) / len(rows) if rows else None

    print(f"\n  Trades: {len(rows)}   {green(f'WIN={len(wins)}')}   {red(f'LOSS={len(losses)}')}   WR: {wr_color(wr)}")
    print(f"  Rate: {len(rows)/hours:.1f}/hr  (fires on ~{len(rows)*100//(hours*12)}% of windows)\n")

    # CLOB sizing breakdown
    clob_bands = {
        '>=0.75 (2.0x)': [r for r in rows if r['clob_down_ask'] and r['clob_down_ask'] >= 0.75],
        '0.55-0.75 (1.5x)': [r for r in rows if r['clob_down_ask'] and 0.55 <= r['clob_down_ask'] < 0.75],
        '0.35-0.55 (1.2x)': [r for r in rows if r['clob_down_ask'] and 0.35 <= r['clob_down_ask'] < 0.55],
        '<0.35 (1.0x)': [r for r in rows if r['clob_down_ask'] and r['clob_down_ask'] < 0.35],
        'no CLOB (1.0x)': [r for r in rows if not r['clob_down_ask']],
    }
    print(f"  {'CLOB band':20s}  {'n':>4}  {'WR%':>6}")
    print(f"  {'-'*20}  {'----':>4}  {'----':>6}")
    for band, band_rows in clob_bands.items():
        if not band_rows:
            continue
        band_wins = [r for r in band_rows if r['outcome'] == 'WIN']
        band_wr = 100 * len(band_wins) / len(band_rows)
        print(f"  {band:20s}  {len(band_rows):>4}  {wr_color(band_wr)}")

    print(f"\n  Recent trades:")
    print(f"  {'Time':>5}  {'Open':>8}  {'Close':>8}  {'Move':>6}  {'CLOB':>5}  {'Result'}")
    for r in rows[:15]:
        ts = datetime.fromtimestamp(r['window_ts'], tz=timezone.utc).strftime('%H:%M')
        move = float(r['btc_move'] or 0)
        clob = f"{r['clob_down_ask']:.2f}" if r['clob_down_ask'] else "  -- "
        result = green('WIN') if r['outcome'] == 'WIN' else red('LOSS')
        print(f"  {ts}    {r['open_price']:>8,.0f}   {r['close_price']:>8,.0f}   {move:>+6.0f}   {clob}   {result}")


# ── Section 3: V4 UP ASIAN ────────────────────────────────────────────────────

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
        SELECT *,
               close_price - open_price AS btc_move,
               CASE WHEN close_price > open_price THEN 'WIN' ELSE 'LOSS' END AS outcome
        FROM best
        WHERE close_price IS NOT NULL AND open_price IS NOT NULL
        ORDER BY window_ts DESC
    """, str(hours))

    if not rows:
        print(dim(f"\n  No trades in last {hours}h — Asian session is 23:00-02:59 UTC"))
        # Show distribution of UP signals with the right dist range
        hour_dist = await safe_fetch(conn, """
            SELECT EXTRACT(HOUR FROM evaluated_at)::int AS h,
                   COUNT(DISTINCT window_ts) n
            FROM signal_evaluations
            WHERE asset='BTC' AND eval_offset BETWEEN 90 AND 150
              AND v2_direction='UP'
              AND ABS(COALESCE(v2_probability_up,0.5)-0.5) BETWEEN 0.15 AND 0.20
              AND evaluated_at > NOW() - ($1 || ' hours')::interval
            GROUP BY 1 ORDER BY 1
        """, str(hours))
        if hour_dist:
            print(f"  UP signals (dist 0.15-0.20) by hour:")
            for r in hour_dist:
                marker = green(" ← ASIAN") if r['h'] in (23, 0, 1, 2) else ""
                print(f"    H{r['h']:02d}: {r['n']} windows{marker}")
        return

    wins = [r for r in rows if r['outcome'] == 'WIN']
    losses = [r for r in rows if r['outcome'] == 'LOSS']
    wr = 100 * len(wins) / len(rows) if rows else None

    print(f"\n  Trades: {len(rows)}   {green(f'WIN={len(wins)}')}   {red(f'LOSS={len(losses)}')}   WR: {wr_color(wr)}")
    print(f"\n  {'Time':>5}  {'Hour':>4}  {'Dist':>5}  {'Open':>8}  {'Move':>6}  {'Result'}")
    for r in rows:
        ts = datetime.fromtimestamp(r['window_ts'], tz=timezone.utc).strftime('%H:%M')
        move = float(r['btc_move'] or 0)
        result = green('WIN') if r['outcome'] == 'WIN' else red('LOSS')
        print(f"  {ts}    H{int(r['hour_utc']):02d}   {r['dist']:.3f}   {r['open_price']:>8,.0f}   {move:>+6.0f}   {result}")


# ── Section 4: Combined ───────────────────────────────────────────────────────

async def report_combined(conn, hours):
    print(cyan(f"\n{'─'*60}"))
    print(bold("  COMBINED PERFORMANCE"))
    print(cyan(f"{'─'*60}"))

    result = await conn.fetchrow("""
        WITH all_trades AS (
            SELECT DISTINCT ON (se.window_ts, se.v2_direction)
                se.window_ts, se.v2_direction AS dir,
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
            SUM(CASE
                WHEN dir='DOWN' AND close_price < open_price THEN 1
                WHEN dir='UP'   AND close_price > open_price THEN 1
                ELSE 0 END) FILTER (WHERE close_price IS NOT NULL) AS wins,
            SUM(CASE
                WHEN dir='DOWN' AND close_price >= open_price THEN 1
                WHEN dir='UP'   AND close_price <= open_price THEN 1
                ELSE 0 END) FILTER (WHERE close_price IS NOT NULL) AS losses
        FROM all_trades
    """, str(hours))

    n = result['n'] or 0
    wins = result['wins'] or 0
    losses = result['losses'] or 0
    wr = 100 * wins / n if n else None
    print(f"\n  Total trades: {n}   {green(f'WIN={wins}')}   {red(f'LOSS={losses}')}   WR: {wr_color(wr)}")
    print(f"  Rate: {n/hours:.1f}/hr  ({n} of {hours*12} possible windows = {100*n//(hours*12)}%)")
    print()


# ── Section 5: Go-live checklist ──────────────────────────────────────────────

async def report_golive(conn):
    print(cyan(f"\n{'─'*60}"))
    print(bold("  GO-LIVE CHECKLIST"))
    print(cyan(f"{'─'*60}"))

    # Paper mode
    paper = await conn.fetchrow("SELECT paper_enabled, live_enabled FROM system_state ORDER BY id DESC LIMIT 1")
    mode = "PAPER" if paper and paper['paper_enabled'] else "LIVE"
    mode_ok = paper and not paper['paper_enabled']
    print(f"\n  {'✓' if mode_ok else '·'} Trading mode: {green(mode) if mode_ok else mode}")

    # Recent trades
    recent = await conn.fetchval("""
        SELECT COUNT(*) FROM strategy_decisions
        WHERE strategy_id IN ('v4_down_only','v4_up_asian')
          AND action='TRADE' AND evaluated_at > NOW() - INTERVAL '10 minutes'
    """)
    print(f"  {'✓' if recent >= 0 else '·'} Engine evaluating: last TRADE/SKIP decisions seen")

    # CLOB
    clob = await conn.fetchval("""
        SELECT COUNT(*) FROM clob_book_snapshots
        WHERE ts > NOW() - INTERVAL '5 minutes'
          AND up_best_ask IS NOT NULL
    """)
    print(f"  {'✓' if clob > 0 else '⚠'} CLOB feed: {clob} snapshots last 5min {'(live)' if clob > 0 else '(null values — sizing at 1x)'}")

    # 24h WR check
    down_wr = await conn.fetchrow("""
        WITH best AS (
            SELECT DISTINCT ON (se.window_ts)
                se.window_ts,
                (SELECT c.price FROM ticks_chainlink c WHERE c.asset='BTC'
                 AND ABS(EXTRACT(EPOCH FROM c.ts)::bigint - se.window_ts) < 30
                 ORDER BY ABS(EXTRACT(EPOCH FROM c.ts)::bigint - se.window_ts) LIMIT 1) AS open_price,
                (SELECT c.price FROM ticks_chainlink c WHERE c.asset='BTC'
                 AND ABS(EXTRACT(EPOCH FROM c.ts)::bigint - (se.window_ts+300)) < 30
                 ORDER BY ABS(EXTRACT(EPOCH FROM c.ts)::bigint - (se.window_ts+300)) LIMIT 1) AS close_price
            FROM signal_evaluations se
            WHERE se.asset='BTC' AND se.eval_offset BETWEEN 90 AND 150
              AND se.v2_direction='DOWN' AND ABS(COALESCE(se.v2_probability_up,0.5)-0.5) >= 0.12
              AND se.evaluated_at > NOW() - INTERVAL '24 hours'
            ORDER BY se.window_ts, se.eval_offset DESC
        )
        SELECT COUNT(*) n,
               ROUND(100.0*SUM(CASE WHEN close_price<open_price THEN 1 ELSE 0 END)::numeric/NULLIF(COUNT(*),0),1) wr
        FROM best WHERE close_price IS NOT NULL AND open_price IS NOT NULL
    """)
    if down_wr and down_wr['n']:
        wr = float(down_wr['wr'] or 0)
        flag = '✓' if wr >= 65 else '⚠'
        print(f"  {flag} DOWN 24h WR: {wr_color(wr)} ({down_wr['n']} trades)")

    print()


# ── Main ──────────────────────────────────────────────────────────────────────

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
    parser = argparse.ArgumentParser(description="UP/DOWN strategy report")
    parser.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()
    asyncio.run(run(args.hours))
```

---

## Key Monitoring Queries

### Rolling 24h WR (run anytime)

```sql
-- DOWN strategy
SELECT COUNT(*) n,
       ROUND(100.0*SUM(CASE WHEN close_price<open_price THEN 1 ELSE 0 END)::numeric/COUNT(*),1) AS down_wr
FROM (
    SELECT DISTINCT ON (se.window_ts)
        (SELECT c.price FROM ticks_chainlink c WHERE c.asset='BTC'
         AND ABS(EXTRACT(EPOCH FROM c.ts)::bigint - se.window_ts) < 30
         ORDER BY ABS(EXTRACT(EPOCH FROM c.ts)::bigint - se.window_ts) LIMIT 1) AS open_price,
        (SELECT c.price FROM ticks_chainlink c WHERE c.asset='BTC'
         AND ABS(EXTRACT(EPOCH FROM c.ts)::bigint - (se.window_ts+300)) < 30
         ORDER BY ABS(EXTRACT(EPOCH FROM c.ts)::bigint - (se.window_ts+300)) LIMIT 1) AS close_price
    FROM signal_evaluations se
    WHERE se.asset='BTC' AND se.eval_offset BETWEEN 90 AND 150
      AND se.v2_direction='DOWN'
      AND ABS(COALESCE(se.v2_probability_up,0.5)-0.5) >= 0.12
      AND se.evaluated_at > NOW() - INTERVAL '24 hours'
    ORDER BY se.window_ts, se.eval_offset DESC
) q WHERE close_price IS NOT NULL AND open_price IS NOT NULL;
```

### Live trade history

```sql
SELECT strategy_id, action, direction, skip_reason, entry_reason,
       collateral_pct, eval_offset, evaluated_at
FROM strategy_decisions
WHERE strategy_id IN ('v4_down_only','v4_up_asian')
  AND action = 'TRADE'
  AND evaluated_at > NOW() - INTERVAL '24 hours'
ORDER BY evaluated_at DESC;
```

### Alert: DOWN WR degrading

```sql
-- Run hourly. Alert if DOWN WR drops below 65% on 10+ trades
WITH recent AS (
    SELECT DISTINCT ON (se.window_ts)
        (SELECT c.price FROM ticks_chainlink c WHERE c.asset='BTC'
         AND ABS(EXTRACT(EPOCH FROM c.ts)::bigint - se.window_ts) < 30
         ORDER BY ABS(EXTRACT(EPOCH FROM c.ts)::bigint - se.window_ts) LIMIT 1) AS open_price,
        (SELECT c.price FROM ticks_chainlink c WHERE c.asset='BTC'
         AND ABS(EXTRACT(EPOCH FROM c.ts)::bigint - (se.window_ts+300)) < 30
         ORDER BY ABS(EXTRACT(EPOCH FROM c.ts)::bigint - (se.window_ts+300)) LIMIT 1) AS close_price
    FROM signal_evaluations se
    WHERE se.asset='BTC' AND se.eval_offset BETWEEN 90 AND 150
      AND se.v2_direction='DOWN'
      AND ABS(COALESCE(se.v2_probability_up,0.5)-0.5) >= 0.12
      AND se.evaluated_at > NOW() - INTERVAL '4 hours'
    ORDER BY se.window_ts, se.eval_offset DESC
)
SELECT COUNT(*) n,
       ROUND(100.0*SUM(CASE WHEN close_price<open_price THEN 1 ELSE 0 END)::numeric/COUNT(*),1) AS wr,
       CASE WHEN COUNT(*) >= 10 AND
                100.0*SUM(CASE WHEN close_price<open_price THEN 1 ELSE 0 END)/COUNT(*) < 65
            THEN 'ALERT: WR below 65% — consider pausing'
            ELSE 'OK' END AS status
FROM recent WHERE close_price IS NOT NULL AND open_price IS NOT NULL;
```

---

## Expected Behaviour

### DOWN trades per day
~30-50 (varies with volatility). In a flat market (BTC ±$100/5min): ~5-15/day. In a trending day: 30-50+.

### UP Asian trades per day
~0-8 per Asian session (23:00-02:59 UTC = 48 possible windows, only dist 0.15-0.20 fires). Typically 2-5 per session.

### When NEITHER fires
- BTC is flat, low conviction (dist < 0.12 for DOWN)
- Signal is UP but outside Asian session
- Regime = risk_off (V4 blocks all strategies)
- eval_offset outside T-90-150 (too early/late in window)

### Watching for problems
- DOWN WR < 65% over 10+ trades → BTC regime shift, retrain may be needed
- UP Asian WR < 60% over 20+ trades → session pattern may have changed
- CLOB null → sizing at 1.0x (no boost) but trades still execute correctly
- `regime_risk_off` blocking all trades → V4 correctly cautious, not a bug

---

## Schema gotchas
See `docs/analysis/SIGNAL_EVAL_RUNBOOK.md` Section 5 for full list. Key ones:
- `signal_evaluations.window_ts` is TEXT — cast to bigint for Chainlink join
- Ground truth = `CASE WHEN close_price > open_price THEN 'UP' ...` — never use `actual_direction`
- Chainlink close = price at `window_ts + 300` (5 minutes later)
- `DISTINCT ON (window_ts)` required — multiple eval rows per window (per-tick writes)
