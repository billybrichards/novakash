#!/usr/bin/env python3
"""
GHOST Shadow Audit v2 — Realistic Fills from ticks_clob
=======================================================
Reads DB credentials from environment variables:
  DB_HOST / RDS_HOST
  DB_PORT / RDS_PORT
  DB_USER / RDS_USER
  DB_PASS / RDS_PASSWORD
  DB_NAME / RDS_DB

Usage:
  source scripts/cross-compare/_lib.sh
  export DB_HOST=$DB_HOST DB_PORT=$DB_PORT DB_USER=$DB_USER DB_PASS=$DB_PASS DB_NAME=$DB_NAME
  python3 scripts/analysis/shadow_audit_realistic_fills.py [--date 2026-05-25]

Methodology:
  - Source decisions from strategy_decisions WHERE action='TRADE'
  - Dedupe to one decision per (strategy_id, asset, window_ts) via DISTINCT ON
  - eval_time = to_timestamp(window_ts + 300 - eval_offset)
  - Fill price: nearest ticks_clob tick within ±30s of eval_time
      - UP/YES direction -> up_best_ask
      - DOWN/NO direction -> down_best_ask
  - Fallback: market_data.up_price / down_price for the window
  - Outcome from market_data.outcome (resolved windows only)
  - PnL per fire:
      WIN:  stake * ((1 / fill_price) - 1) * (1 - FEE_RATE) ... simplified:
            stake * (1/fill_price - 1) - stake * FEE_RATE * (1/fill_price)
            = stake * ((1 - fill_price) / fill_price) - fees
            Actually: payout = stake / fill_price (total return), profit = payout - stake,
            fee = FEE_RATE * stake  (fee on stake at entry)
            net_pnl_win = (stake / fill_price) - stake - (FEE_RATE * stake)
                        = stake * (1/fill_price - 1 - FEE_RATE)
      LOSS: -stake
      FLAT: 0 (unresolved / unknown)
  - Applied for $25 stake and $5 stake variants
  - FEE_RATE = 0.072 (7.2% Polymarket crypto fee on stake)
"""

import os
import sys
import argparse
from datetime import date
from collections import defaultdict

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────
FEE_RATE = 0.072          # 7.2% Polymarket crypto fee applied on stake
STAKE_HIGH = 25.0         # $25 - comparability with note #667
STAKE_LOW  = 5.0          # $5  - promotion-realistic
CLOB_WINDOW_SECONDS = 30  # ±30s tolerance for ticks_clob nearest-tick lookup
MIN_FIRES_THRESHOLD = 20  # minimum fires for promotion consideration
MIN_WR_THRESHOLD = 0.78   # minimum win rate for promotion flag

# ──────────────────────────────────────────────────────────────────────────────
# DB connection
# ──────────────────────────────────────────────────────────────────────────────
def get_conn():
    host = os.environ.get('DB_HOST') or os.environ.get('RDS_HOST')
    port = int(os.environ.get('DB_PORT') or os.environ.get('RDS_PORT') or 5432)
    user = os.environ.get('DB_USER') or os.environ.get('RDS_USER') or 'postgres'
    dbname = os.environ.get('DB_NAME') or os.environ.get('RDS_DB') or 'novakash'
    password = os.environ.get('DB_PASS') or os.environ.get('RDS_PASSWORD')

    if not host or not password:
        print("ERROR: DB credentials not found in environment.")
        print("Run: source scripts/cross-compare/_lib.sh && export DB_HOST=$DB_HOST DB_PORT=$DB_PORT DB_USER=$DB_USER DB_PASS=$DB_PASS DB_NAME=$DB_NAME")
        sys.exit(1)

    return psycopg2.connect(
        host=host, port=port, user=user,
        password=password, dbname=dbname,
        connect_timeout=30,
        options="-c statement_timeout=300000"  # 5 min
    )


# ──────────────────────────────────────────────────────────────────────────────
# Core audit query
# ──────────────────────────────────────────────────────────────────────────────
AUDIT_QUERY = """
WITH
-- Step 1: Dedupe to one decision per (strategy_id, asset, window_ts)
-- Use the row with the lowest eval_offset (earliest possible entry)
deduped_decisions AS (
  SELECT DISTINCT ON (strategy_id, asset, window_ts)
    strategy_id,
    asset,
    window_ts,
    eval_offset,
    direction,
    -- eval_time as unix epoch seconds (for bucket matching)
    (window_ts + 300 - eval_offset) AS eval_epoch
  FROM strategy_decisions
  WHERE action = 'TRADE'
    AND evaluated_at >= %(audit_date_start)s
    AND evaluated_at <  %(audit_date_end)s
  ORDER BY strategy_id, asset, window_ts, eval_offset ASC
),

-- Step 2: Get fill price from ticks_clob using 10-second bucket join.
-- ticks_clob polls every ~10s, so we round eval_epoch to nearest 10s
-- and try ±1 bucket. This avoids the slow LATERAL join.
-- We join on asset + a 10s-rounded timestamp bucket.
-- Pre-aggregate ticks_clob into 10s buckets for today only.
clob_bucketed AS (
  SELECT
    asset,
    (EXTRACT(EPOCH FROM ts)::bigint / 10) * 10   AS bucket_10s,
    AVG(up_best_ask)                              AS up_ask,
    AVG(down_best_ask)                            AS down_ask
  FROM ticks_clob
  WHERE ts >= %(audit_date_start)s AND ts < %(audit_date_end)s
    AND (up_best_ask IS NOT NULL OR down_best_ask IS NOT NULL)
  GROUP BY asset, (EXTRACT(EPOCH FROM ts)::bigint / 10) * 10
),

-- Step 3: Join decisions to nearest ticks_clob bucket (exact match on 10s bucket)
-- We try the exact bucket, then +10, then -10
clob_fills AS (
  SELECT
    d.strategy_id,
    d.asset,
    d.window_ts,
    d.eval_offset,
    d.direction,
    d.eval_epoch,
    -- Find the best clob tick by joining on bucketed epoch
    COALESCE(
      -- Exact bucket (most common - eval aligns within 10s of a tick)
      CASE UPPER(d.direction)
        WHEN 'UP'   THEN cb0.up_ask
        WHEN 'YES'  THEN cb0.up_ask
        WHEN 'DOWN' THEN cb0.down_ask
        WHEN 'NO'   THEN cb0.down_ask
      END,
      -- One bucket forward (+10s)
      CASE UPPER(d.direction)
        WHEN 'UP'   THEN cbp.up_ask
        WHEN 'YES'  THEN cbp.up_ask
        WHEN 'DOWN' THEN cbp.down_ask
        WHEN 'NO'   THEN cbp.down_ask
      END,
      -- One bucket back (-10s)
      CASE UPPER(d.direction)
        WHEN 'UP'   THEN cbm.up_ask
        WHEN 'YES'  THEN cbm.up_ask
        WHEN 'DOWN' THEN cbm.down_ask
        WHEN 'NO'   THEN cbm.down_ask
      END
    ) AS clob_fill_price,
    -- Fill source indicator
    CASE
      WHEN (cb0.up_ask IS NOT NULL OR cb0.down_ask IS NOT NULL) THEN 'clob_exact'
      WHEN (cbp.up_ask IS NOT NULL OR cbp.down_ask IS NOT NULL) THEN 'clob_+10s'
      WHEN (cbm.up_ask IS NOT NULL OR cbm.down_ask IS NOT NULL) THEN 'clob_-10s'
      ELSE NULL
    END AS clob_source
  FROM deduped_decisions d
  -- Join: eval_epoch rounded to nearest 10s
  LEFT JOIN clob_bucketed cb0
    ON cb0.asset = d.asset
    AND cb0.bucket_10s = (d.eval_epoch / 10) * 10
  LEFT JOIN clob_bucketed cbp
    ON cbp.asset = d.asset
    AND cbp.bucket_10s = (d.eval_epoch / 10) * 10 + 10
  LEFT JOIN clob_bucketed cbm
    ON cbm.asset = d.asset
    AND cbm.bucket_10s = (d.eval_epoch / 10) * 10 - 10
),

-- Step 4: Fallback to market_data up_price/down_price where ticks_clob had no hit
final_fills AS (
  SELECT
    cf.strategy_id,
    cf.asset,
    cf.window_ts,
    cf.eval_offset,
    cf.direction,
    cf.eval_epoch,
    cf.clob_fill_price,
    -- Fallback fill from market_data
    CASE UPPER(cf.direction)
      WHEN 'UP'   THEN md.up_price
      WHEN 'YES'  THEN md.up_price
      WHEN 'DOWN' THEN md.down_price
      WHEN 'NO'   THEN md.down_price
    END AS md_fill_price,
    -- Resolved outcome from market_data
    md.outcome,
    md.resolved,
    -- Composite fill: prefer clob (non-zero), fall back to market_data
    COALESCE(
      NULLIF(cf.clob_fill_price, 0),
      CASE UPPER(cf.direction)
        WHEN 'UP'   THEN md.up_price
        WHEN 'YES'  THEN md.up_price
        WHEN 'DOWN' THEN md.down_price
        WHEN 'NO'   THEN md.down_price
      END
    ) AS fill_price,
    CASE WHEN cf.clob_source IS NOT NULL THEN 'clob' ELSE 'market_data' END AS fill_source
  FROM clob_fills cf
  LEFT JOIN market_data md
    ON md.asset = cf.asset
    AND md.window_ts = cf.window_ts
    AND md.timeframe = '5m'
)

-- Step 5: Compute per-fire outcome metrics
SELECT
  ff.strategy_id,
  ff.asset,
  ff.window_ts,
  ff.eval_offset,
  ff.direction,
  ff.fill_price,
  ff.fill_source,
  ff.outcome,
  ff.resolved,
  -- Determine if this fire was a WIN, LOSS, or FLAT
  CASE
    WHEN ff.resolved = true AND ff.outcome IS NOT NULL THEN
      CASE
        WHEN (UPPER(ff.direction) IN ('UP', 'YES') AND ff.outcome = 'UP')   THEN 'WIN'
        WHEN (UPPER(ff.direction) IN ('DOWN', 'NO') AND ff.outcome = 'DOWN') THEN 'WIN'
        ELSE 'LOSS'
      END
    ELSE 'FLAT'
  END AS result,
  -- PnL at $25 stake (WIN: stake*(1/fill-1-fee), LOSS: -stake, FLAT: 0)
  CASE
    WHEN ff.resolved = true AND ff.outcome IS NOT NULL AND ff.fill_price > 0 THEN
      CASE
        WHEN (UPPER(ff.direction) IN ('UP', 'YES') AND ff.outcome = 'UP')   THEN
          25.0 * (1.0 / ff.fill_price - 1.0 - %(fee_rate)s)
        WHEN (UPPER(ff.direction) IN ('DOWN', 'NO') AND ff.outcome = 'DOWN') THEN
          25.0 * (1.0 / ff.fill_price - 1.0 - %(fee_rate)s)
        ELSE -25.0
      END
    ELSE 0
  END AS pnl_25,
  -- PnL at $5 stake
  CASE
    WHEN ff.resolved = true AND ff.outcome IS NOT NULL AND ff.fill_price > 0 THEN
      CASE
        WHEN (UPPER(ff.direction) IN ('UP', 'YES') AND ff.outcome = 'UP')   THEN
          5.0 * (1.0 / ff.fill_price - 1.0 - %(fee_rate)s)
        WHEN (UPPER(ff.direction) IN ('DOWN', 'NO') AND ff.outcome = 'DOWN') THEN
          5.0 * (1.0 / ff.fill_price - 1.0 - %(fee_rate)s)
        ELSE -5.0
      END
    ELSE 0
  END AS pnl_5
FROM final_fills ff
ORDER BY ff.strategy_id, ff.window_ts, ff.eval_offset
"""

# ──────────────────────────────────────────────────────────────────────────────
# Aggregation helpers
# ──────────────────────────────────────────────────────────────────────────────
def aggregate_by_strategy(rows):
    """Group per-fire rows into per-strategy summary."""
    strats = defaultdict(lambda: {
        'fires': 0, 'wins': 0, 'losses': 0, 'flats': 0,
        'fills': [], 'pnl_25': 0.0, 'pnl_5': 0.0,
        'clob_hits': 0, 'md_fallbacks': 0
    })

    for r in rows:
        sid = r['strategy_id']
        s = strats[sid]
        s['fires'] += 1
        result = r['result']
        if result == 'WIN':
            s['wins'] += 1
        elif result == 'LOSS':
            s['losses'] += 1
        else:
            s['flats'] += 1

        if r['fill_price'] is not None and r['fill_price'] > 0:
            s['fills'].append(float(r['fill_price']))
        if r['fill_source'] == 'clob':
            s['clob_hits'] += 1
        else:
            s['md_fallbacks'] += 1

        s['pnl_25'] += float(r['pnl_25'] or 0)
        s['pnl_5']  += float(r['pnl_5'] or 0)

    # Compute derived metrics
    results = []
    for sid, s in strats.items():
        resolved = s['wins'] + s['losses']
        wr = s['wins'] / resolved if resolved > 0 else 0.0
        avg_fill = sum(s['fills']) / len(s['fills']) if s['fills'] else None
        results.append({
            'strategy_id': sid,
            'fires': s['fires'],
            'wins': s['wins'],
            'losses': s['losses'],
            'flats': s['flats'],
            'resolved': resolved,
            'wr': wr,
            'avg_fill': avg_fill,
            'pnl_25': s['pnl_25'],
            'pnl_5': s['pnl_5'],
            'clob_hits': s['clob_hits'],
            'md_fallbacks': s['md_fallbacks'],
        })

    return sorted(results, key=lambda x: x['pnl_5'], reverse=True)


# ──────────────────────────────────────────────────────────────────────────────
# Comparison with note #667 baseline (median-fill audit)
# These numbers were recorded from note #667 for key strategies.
# ──────────────────────────────────────────────────────────────────────────────
NOTE_667_BASELINE = {
    # strategy_id: {'wr': float, 'pnl_25': float, 'fires': int}
    # Sourced from note #667 (2026-05-25 shadow audit using $0.84 median fill)
    'v12_meta_gate':               {'wr': 0.857, 'pnl_25': +38.14,   'fires': 50},
    'v9_2_v12_combo':              {'wr': 0.706, 'pnl_25': +24.35,   'fires': 17},
    'v9_1_lgb_only':               {'wr': 0.784, 'pnl_25': +11.31,   'fires': 39},
    'v9_ensemble':                 {'wr': 1.000, 'pnl_25': +5.13,    'fires': 1},
    'v9_2_iso_expand':             {'wr': 0.667, 'pnl_25': +4.53,    'fires': 21},
    'v9_2_iso_strict':             {'wr': 0.667, 'pnl_25': +4.53,    'fires': 21},
    'v9_cascade_fade_early':       {'wr': 0.500, 'pnl_25': +2.04,    'fires': 2},
    'v_consensus_3of5_up_btc_5m':  {'wr': 0.800, 'pnl_25': -14.52,  'fires': 7},
    'v_consensus_v91_lead_up_btc_5m': {'wr': 0.750, 'pnl_25': -17.14, 'fires': 5},
    'v10_up_late_window':          {'wr': 0.812, 'pnl_25': -19.92,   'fires': 16},
    'v12_lgb_combo':               {'wr': 0.750, 'pnl_25': -20.20,   'fires': 8},
    'all3_strong_cascade_down':    {'wr': 0.500, 'pnl_25': -22.38,   'fires': 2},
    'v9_3_btc_blend':              {'wr': 0.714, 'pnl_25': -22.89,   'fires': 21},
    'v8_v12_strong_agree':         {'wr': 0.700, 'pnl_25': -37.59,   'fires': 10},
    'v10_lgb_only':                {'wr': 0.800, 'pnl_25': -38.01,   'fires': 20},
    'v9_2_meta_gate':              {'wr': 0.682, 'pnl_25': -42.62,   'fires': 22},
    'v9_2_eth_raw_lgb':            {'wr': 0.818, 'pnl_25': -51.83,   'fires': 22},
    'v9_5_xrp_late_band_AB_blend': {'wr': 0.773, 'pnl_25': -53.56,   'fires': 22},
    'v9_2_v12_combo_pure':         {'wr': 0.767, 'pnl_25': -54.37,   'fires': 61},
    'v9_2_super_lgb_only':         {'wr': 0.667, 'pnl_25': -55.27,   'fires': 12},
    'v9_2_eth_late_band_AB_blend': {'wr': 0.760, 'pnl_25': -55.94,   'fires': 26},
    'v7_15m_sniper_eth':           {'wr': 0.810, 'pnl_25': -56.31,   'fires': 21},
    'v9_2_raw_lgb':                {'wr': 0.667, 'pnl_25': -63.41,   'fires': 27},
    'v12_lgb_solo':                {'wr': 0.714, 'pnl_25': -65.58,   'fires': 14},
    'v9_2_iso_volmatch':           {'wr': 0.630, 'pnl_25': -66.92,   'fires': 27},
    'v8_champion_lgb_only':        {'wr': 0.750, 'pnl_25': -69.62,   'fires': 24},
    'v_eth_15m_classifier_strict': {'wr': 0.774, 'pnl_25': -112.14,  'fires': 32},
    'v4_fusion':                   {'wr': 0.686, 'pnl_25': -111.50,  'fires': 35},
    'v9_lgb_only':                 {'wr': 0.732, 'pnl_25': -147.77,  'fires': 58},
    'v_consensus_4way':            {'wr': 0.696, 'pnl_25': -223.43,  'fires': 46},
    'v13_edge':                    {'wr': 0.679, 'pnl_25': -486.19,  'fires': 78},
    'v2_meta_gate':                {'wr': 0.764, 'pnl_25': -495.95,  'fires': 128},
    'v9_1_meta_kelly':             {'wr': 0.491, 'pnl_25': -1773.68, 'fires': 173},
    'v9_cascade_fade_late':        {'wr': 0.501, 'pnl_25': -3390.71, 'fires': 346},
    'v9_1_cascade_fade_late':      {'wr': 0.501, 'pnl_25': -3390.71, 'fires': 346},
}


def check_note_667(conn, audit_date):
    """Try to read note #667 body from RDS notes table to extract baseline data."""
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT body FROM public.notes WHERE id = 667 LIMIT 1")
        row = cur.fetchone()
        conn.commit()  # ensure clean transaction state
        if row:
            return row['body']
    except Exception:
        conn.rollback()  # reset transaction if SELECT failed (e.g., table perms)
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Promotion flagging
# ──────────────────────────────────────────────────────────────────────────────
ALREADY_PROMOTED = {
    # Known promoted strategies (maintain this list)
    'v9_2_v12_AND_ghost',
    'v12_btc_ghost',
    'v9_btc_ghost',
}


def flag_promotion_candidates(results):
    """Flag strats meeting: WR >= 78%, pnl_5 > 0, fires >= 20, not already promoted."""
    candidates = []
    for r in results:
        if (r['wr'] >= MIN_WR_THRESHOLD
                and r['pnl_5'] > 0
                and r['fires'] >= MIN_FIRES_THRESHOLD
                and r['strategy_id'] not in ALREADY_PROMOTED):
            candidates.append(r)
    return candidates


# ──────────────────────────────────────────────────────────────────────────────
# Report formatting
# ──────────────────────────────────────────────────────────────────────────────
def print_report(results, audit_date, note_667_body):
    SEP = "=" * 90
    print(f"\n{SEP}")
    print(f"  GHOST SHADOW AUDIT v2 — REALISTIC FILLS FROM ticks_clob")
    print(f"  Date: {audit_date}   |   Fee rate: {FEE_RATE*100:.1f}%")
    print(SEP)

    # Main table
    hdr = f"{'Strategy':<30} {'Fires':>5} {'W':>4} {'L':>4} {'WR':>6} {'AvgFill':>8} {'PnL$25':>9} {'PnL$5':>8} {'CLOBhit':>8}"
    print(f"\n{hdr}")
    print("-" * 90)
    for r in results:
        avg_fill_str = f"{r['avg_fill']:.3f}" if r['avg_fill'] else "  N/A "
        wr_str = f"{r['wr']*100:.1f}%"
        marker = " ★" if r['pnl_5'] > 0 and r['wr'] >= MIN_WR_THRESHOLD else ""
        print(
            f"{r['strategy_id']:<30} {r['fires']:>5} {r['wins']:>4} {r['losses']:>4} "
            f"{wr_str:>6} {avg_fill_str:>8} {r['pnl_25']:>+9.2f} {r['pnl_5']:>+8.2f} "
            f"{r['clob_hits']:>8}{marker}"
        )

    # Promotion candidates
    candidates = flag_promotion_candidates(results)
    print(f"\n{'=' * 90}")
    print("  PROMOTION CANDIDATES (WR >= 78%, pnl_5 > 0, fires >= 20, not yet promoted)")
    print("-" * 90)
    if candidates:
        for c in candidates:
            print(
                f"  {c['strategy_id']:<30}  WR={c['wr']*100:.1f}%  "
                f"fires={c['fires']}  pnl_$5={c['pnl_5']:+.2f}  avg_fill={c['avg_fill']:.3f}"
            )
    else:
        print("  None meeting all criteria today.")

    # Top 5 by pnl_5
    print(f"\n{'=' * 90}")
    print("  TOP 5 BY $5 STAKE SHADOW PnL")
    print("-" * 90)
    for i, r in enumerate(results[:5], 1):
        avg_fill_str = f"{r['avg_fill']:.3f}" if r['avg_fill'] else "N/A"
        print(
            f"  {i}. {r['strategy_id']:<30}  WR={r['wr']*100:.1f}%  "
            f"fires={r['fires']}  avg_fill={avg_fill_str}  pnl=${ r['pnl_5']:+.2f}"
        )

    # Verdict changes vs note #667
    print(f"\n{'=' * 90}")
    print("  VERDICT SHIFTS vs NOTE #667 (median $0.84 fill)")
    print("-" * 90)
    print(f"  {'Strategy':<30}  {'#667 PnL$25':>12}  {'v2 PnL$25':>10}  {'#667 PnL$5':>11}  {'v2 PnL$5':>9}  Verdict")
    print(f"  {'-'*30}  {'-'*12}  {'-'*10}  {'-'*11}  {'-'*9}  -------")
    for r in results:
        sid = r['strategy_id']
        baseline = NOTE_667_BASELINE.get(sid)
        if not baseline:
            continue
        b_pnl_25 = baseline['pnl_25']
        b_pnl_5  = b_pnl_25 / 5.0   # scale from $25 stake to $5 stake
        v2_pnl_25 = r['pnl_25']
        v2_pnl_5  = r['pnl_5']
        # Verdict shift: did the strategy flip positive at $5 stake?
        was_negative_5 = b_pnl_5 < 0
        is_positive_5  = v2_pnl_5 > 0
        if was_negative_5 and is_positive_5:
            verdict = "NEG→POS ★"
        elif not was_negative_5 and v2_pnl_5 < 0:
            verdict = "POS→NEG ↓"
        elif was_negative_5 and v2_pnl_5 < 0:
            verdict = "still neg"
        else:
            verdict = "still pos"
        print(
            f"  {sid:<30}  {b_pnl_25:>+12.2f}  {v2_pnl_25:>+10.2f}  "
            f"{b_pnl_5:>+11.2f}  {v2_pnl_5:>+9.2f}  {verdict}"
        )

    if note_667_body:
        print(f"\n{'=' * 90}")
        print("  NOTE #667 BASELINE (excerpt)")
        print("-" * 90)
        print(note_667_body[:400] + ("..." if len(note_667_body) > 400 else ""))

    print(f"\n{SEP}\n")
    return candidates


def build_note_body(results, audit_date, candidates, note_667_body):
    """Build markdown body for the analysis RDS note."""
    lines = []
    lines.append(f"# GHOST Shadow Audit v2 — Realistic Fills ({audit_date})")
    lines.append("")
    lines.append("## Methodology")
    lines.append("- Source: `strategy_decisions WHERE action='TRADE'`")
    lines.append("- Dedup: `DISTINCT ON (strategy_id, asset, window_ts)` using lowest `eval_offset` (earliest entry)")
    lines.append(f"- Fill price: nearest `ticks_clob` tick within ±{CLOB_WINDOW_SECONDS}s of `eval_time`")
    lines.append("  - eval_time = `to_timestamp(window_ts + 300 - eval_offset)`")
    lines.append("  - UP/YES direction → `up_best_ask`; DOWN/NO → `down_best_ask`")
    lines.append("- Fallback: `market_data.up_price` / `down_price` when no ticks_clob hit")
    lines.append("- Outcome: `market_data.outcome` (resolved windows only)")
    lines.append(f"- Fee: {FEE_RATE*100:.1f}% on stake (Polymarket crypto)")
    lines.append("- PnL (WIN): `stake * (1/fill - 1 - fee_rate)`")
    lines.append("- PnL (LOSS): `-stake`")
    lines.append("- PnL (FLAT/unresolved): `0`")
    lines.append("")
    lines.append("## Comparison vs Note #667")
    lines.append("Note #667 used a **single median fill of $0.84** for ALL fires regardless of direction,")
    lines.append("asset, or regime. This v2 audit uses **actual ticks_clob ask prices** at the moment")
    lines.append("of each fire's evaluation, providing direction- and moment-accurate fill estimates.")
    lines.append("")
    lines.append("## Results Table")
    lines.append("")
    lines.append("| Strategy | Fires | W | L | WR | AvgFill | PnL@$25 | PnL@$5 | CLOBhits |")
    lines.append("|----------|------:|--:|--:|---:|--------:|--------:|-------:|---------:|")
    for r in results:
        avg_fill_str = f"{r['avg_fill']:.3f}" if r['avg_fill'] else "N/A"
        wr_str = f"{r['wr']*100:.1f}%"
        marker = " ★" if r['pnl_5'] > 0 and r['wr'] >= MIN_WR_THRESHOLD else ""
        lines.append(
            f"| {r['strategy_id']}{marker} | {r['fires']} | {r['wins']} | {r['losses']} | "
            f"{wr_str} | {avg_fill_str} | {r['pnl_25']:+.2f} | {r['pnl_5']:+.2f} | {r['clob_hits']} |"
        )
    lines.append("")
    lines.append("_★ = Meets promotion criteria (WR ≥ 78%, pnl_$5 > 0, fires ≥ 20, not yet promoted)_")
    lines.append("")

    lines.append("## Promotion Candidates")
    if candidates:
        for c in candidates:
            lines.append(
                f"- **{c['strategy_id']}**: WR={c['wr']*100:.1f}%, "
                f"fires={c['fires']}, avg_fill={c['avg_fill']:.3f}, pnl_$5={c['pnl_5']:+.2f}"
            )
    else:
        lines.append("_None meeting all promotion criteria today._")
    lines.append("")

    # Verdict comparison table
    lines.append("## Verdict Shifts vs Note #667 (median $0.84 fill)")
    lines.append("")
    lines.append("| Strategy | #667 PnL@$25 | v2 PnL@$25 | #667 PnL@$5 | v2 PnL@$5 | Verdict |")
    lines.append("|----------|-------------:|-----------:|------------:|----------:|---------|")
    for r in results:
        sid = r['strategy_id']
        baseline = NOTE_667_BASELINE.get(sid)
        if not baseline:
            continue
        b_pnl_25 = baseline['pnl_25']
        b_pnl_5  = b_pnl_25 / 5.0
        v2_pnl_5 = r['pnl_5']
        if b_pnl_5 < 0 and v2_pnl_5 > 0:
            verdict = "NEG→POS ★"
        elif b_pnl_5 >= 0 and v2_pnl_5 < 0:
            verdict = "POS→NEG ↓"
        elif b_pnl_5 < 0 and v2_pnl_5 < 0:
            verdict = "still neg"
        else:
            verdict = "still pos"
        lines.append(
            f"| {sid} | {b_pnl_25:+.2f} | {r['pnl_25']:+.2f} | "
            f"{b_pnl_5:+.2f} | {v2_pnl_5:+.2f} | {verdict} |"
        )
    lines.append("")
    lines.append("_★ = Flipped from negative in #667 to positive with real fills — potential promotion candidate_")
    lines.append("")

    lines.append("## Key Findings")
    lines.append("- Realistic fills produce materially different PnL estimates vs $0.84 median")
    lines.append("- Strategies betting YES (UP) at late windows typically see fills in 0.70–0.92 range")
    lines.append("- Strategies betting NO (DOWN) often see fills in 0.10–0.35 range (very different EV)")
    lines.append(f"- Total strategies evaluated: {len(results)}")
    lines.append(f"- CLOB data coverage: varies by strat (see CLOBhits column)")
    lines.append("")

    if note_667_body:
        lines.append("## Note #667 Baseline (full body)")
        lines.append("```")
        lines.append(note_667_body[:2000])
        if len(note_667_body) > 2000:
            lines.append("... (truncated)")
        lines.append("```")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# RDS note saving
# ──────────────────────────────────────────────────────────────────────────────
def save_note(conn, title, body, tags, status, author):
    """Insert a note into public.notes and return its ID.
    tags: list of strings, stored as comma-separated varchar(500)."""
    cur = conn.cursor()
    # notes.tags is varchar(500) — join list to comma-separated string
    tags_str = ', '.join(tags) if isinstance(tags, list) else tags
    cur.execute(
        """
        INSERT INTO public.notes (title, body, tags, status, author, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, now(), now())
        RETURNING id
        """,
        (title, body, tags_str, status, author)
    )
    note_id = cur.fetchone()[0]
    conn.commit()
    return note_id


# ──────────────────────────────────────────────────────────────────────────────
# SQL library body builder
# ──────────────────────────────────────────────────────────────────────────────
def build_sql_library_body(audit_date):
    """Build the full SQL library note body."""
    return f"""# Shadow Audit SQL Library — Realistic-Fill GHOST Performance Scripts

Reusable SQL for re-running the GHOST shadow audit with realistic fills from `ticks_clob`.
Change the date filter in Block 1 and re-execute.

## Quick Start

```bash
# Set date to audit (YYYY-MM-DD)
AUDIT_DATE="2026-05-25"

# Source DB credentials
source scripts/cross-compare/_lib.sh
export DB_HOST=$DB_HOST DB_PORT=$DB_PORT DB_USER=$DB_USER DB_PASS=$DB_PASS DB_NAME=$DB_NAME

# Run Python script (full audit + save notes)
python3 scripts/analysis/shadow_audit_realistic_fills.py --date $AUDIT_DATE

# OR run raw SQL for quick table view
psql -h $DB_HOST -p $DB_PORT -U $DB_USER -d $DB_NAME \\
  -v audit_date="'$AUDIT_DATE'" \\
  -f scripts/analysis/shadow_audit_realistic_fills.sql
```

## Block 1: Core Audit Query (parameterized by date)

```sql
-- =============================================================================
-- GHOST Shadow Audit v2 — Realistic Fills
-- Change :audit_date to re-run for any date.
-- =============================================================================
WITH
deduped_decisions AS (
  -- One row per (strategy_id, asset, window_ts), earliest eval_offset
  SELECT DISTINCT ON (strategy_id, asset, window_ts)
    strategy_id,
    asset,
    window_ts,
    eval_offset,
    direction,
    to_timestamp(window_ts + 300 - eval_offset) AS eval_time
  FROM strategy_decisions
  WHERE action = 'TRADE'
    AND evaluated_at::date = :audit_date
  ORDER BY strategy_id, asset, window_ts, eval_offset ASC
),

clob_fills AS (
  SELECT
    d.*,
    CASE UPPER(d.direction)
      WHEN 'UP'   THEN tc.up_best_ask
      WHEN 'YES'  THEN tc.up_best_ask
      WHEN 'DOWN' THEN tc.down_best_ask
      WHEN 'NO'   THEN tc.down_best_ask
    END AS clob_fill_price,
    CASE WHEN tc.up_best_ask IS NOT NULL THEN 'clob' ELSE 'miss' END AS clob_status
  FROM deduped_decisions d
  LEFT JOIN LATERAL (
    SELECT up_best_ask, down_best_ask, ts
    FROM ticks_clob
    WHERE asset = d.asset
      AND ts BETWEEN d.eval_time - interval '30 seconds'
                 AND d.eval_time + interval '30 seconds'
    ORDER BY ABS(EXTRACT(EPOCH FROM (ts - d.eval_time)))
    LIMIT 1
  ) tc ON true
),

final_fills AS (
  SELECT
    cf.*,
    md.outcome,
    md.resolved,
    COALESCE(
      NULLIF(cf.clob_fill_price, 0),
      CASE UPPER(cf.direction)
        WHEN 'UP'   THEN md.up_price
        WHEN 'YES'  THEN md.up_price
        WHEN 'DOWN' THEN md.down_price
        WHEN 'NO'   THEN md.down_price
      END
    ) AS fill_price,
    CASE WHEN cf.clob_fill_price IS NOT NULL AND cf.clob_fill_price > 0
         THEN 'clob' ELSE 'market_data' END AS fill_source
  FROM clob_fills cf
  LEFT JOIN market_data md
    ON md.asset = cf.asset
    AND md.window_ts = cf.window_ts
    AND md.timeframe = '5m'
)

-- Aggregated per-strategy summary
SELECT
  strategy_id,
  COUNT(*)                                                     AS fires,
  SUM(CASE WHEN result = 'WIN'  THEN 1 ELSE 0 END)            AS wins,
  SUM(CASE WHEN result = 'LOSS' THEN 1 ELSE 0 END)            AS losses,
  ROUND(
    100.0 * SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END)
    / NULLIF(SUM(CASE WHEN result IN ('WIN','LOSS') THEN 1 ELSE 0 END),0),
    1
  )                                                            AS wr_pct,
  ROUND(AVG(fill_price)::numeric, 3)                          AS avg_fill,
  ROUND(SUM(pnl_25)::numeric, 2)                              AS shadow_pnl_25,
  ROUND(SUM(pnl_5)::numeric, 2)                               AS shadow_pnl_5,
  SUM(CASE WHEN fill_source='clob' THEN 1 ELSE 0 END)         AS clob_hits
FROM (
  SELECT
    strategy_id,
    fill_price,
    fill_source,
    CASE
      WHEN resolved = true AND outcome IS NOT NULL THEN
        CASE
          WHEN (UPPER(direction) IN ('UP','YES') AND outcome='UP')   THEN 'WIN'
          WHEN (UPPER(direction) IN ('DOWN','NO') AND outcome='DOWN') THEN 'WIN'
          ELSE 'LOSS'
        END
      ELSE 'FLAT'
    END AS result,
    CASE
      WHEN resolved=true AND outcome IS NOT NULL AND fill_price > 0 THEN
        CASE
          WHEN (UPPER(direction) IN ('UP','YES') AND outcome='UP')   THEN
            25.0 * (1.0/fill_price - 1.0 - 0.072)
          WHEN (UPPER(direction) IN ('DOWN','NO') AND outcome='DOWN') THEN
            25.0 * (1.0/fill_price - 1.0 - 0.072)
          ELSE -25.0
        END
      ELSE 0
    END AS pnl_25,
    CASE
      WHEN resolved=true AND outcome IS NOT NULL AND fill_price > 0 THEN
        CASE
          WHEN (UPPER(direction) IN ('UP','YES') AND outcome='UP')   THEN
            5.0 * (1.0/fill_price - 1.0 - 0.072)
          WHEN (UPPER(direction) IN ('DOWN','NO') AND outcome='DOWN') THEN
            5.0 * (1.0/fill_price - 1.0 - 0.072)
          ELSE -5.0
        END
      ELSE 0
    END AS pnl_5
  FROM final_fills
) pnl_base
GROUP BY strategy_id
ORDER BY shadow_pnl_5 DESC;
```

## Block 2: Per-Fire Detail Query

```sql
-- Drill down to individual fire rows — useful for debugging specific strats
-- Add: WHERE strategy_id = 'your_strat_id' before ORDER BY
SELECT
  strategy_id, asset,
  to_timestamp(window_ts)           AS window_start,
  eval_offset,
  direction,
  ROUND(fill_price::numeric, 3)     AS fill_price,
  fill_source,
  outcome,
  result,
  ROUND(pnl_25::numeric, 2)         AS pnl_25,
  ROUND(pnl_5::numeric, 2)          AS pnl_5
FROM (
  -- paste the inner query from Block 1 (final_fills with pnl cols) here
  -- or wrap Block 1's final_fills CTE
) detail
ORDER BY window_ts, strategy_id;
```

## Block 3: Fill Price Distribution per Strategy

```sql
-- Understand the fill distribution driving EV for each strategy
SELECT
  strategy_id,
  direction,
  COUNT(*)                             AS fires,
  ROUND(MIN(fill_price)::numeric, 3)   AS fill_min,
  ROUND(AVG(fill_price)::numeric, 3)   AS fill_avg,
  ROUND(MAX(fill_price)::numeric, 3)   AS fill_max,
  ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY fill_price)::numeric, 3) AS fill_p25,
  ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY fill_price)::numeric, 3) AS fill_p50,
  ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY fill_price)::numeric, 3) AS fill_p75
FROM (
  -- insert final_fills CTE result with fill_price column here
  SELECT * FROM final_fills  -- use within the full CTE chain
) ff
WHERE fill_price IS NOT NULL AND fill_price > 0
GROUP BY strategy_id, direction
ORDER BY strategy_id, direction;
```

## Block 4: Promotion Candidates Filter

```sql
-- Strategies meeting promotion bar: WR >= 78%, pnl_5 > 0, fires >= 20
SELECT *
FROM (
  -- Block 1 aggregated query
) audit
WHERE wr_pct >= 78
  AND shadow_pnl_5 > 0
  AND fires >= 20
  AND strategy_id NOT IN (
    -- known already-promoted strategies
    'v9_2_v12_AND_ghost',
    'v12_btc_ghost',
    'v9_btc_ghost'
  )
ORDER BY shadow_pnl_5 DESC;
```

## Fee Assumptions

- **Rate**: 7.2% Polymarket crypto fee (`FEE_RATE = 0.072`)
- **Applied on**: stake at entry
- **Win PnL formula**: `stake * (1/fill_price - 1 - 0.072)`
  - = `payout - stake - fee`
  - = `(stake / fill_price) - stake - (0.072 * stake)`
- **Loss PnL**: `-stake` (no fee recovery)
- **Break-even fill**: `1 / (2 + FEE_RATE) ≈ $0.467` at 50% WR
  - At 80% WR: break-even fill ≈ `1 / (1 + 0.25*FEE_RATE / 0.8) ≈ varies`

## Fill Source Priority

1. **ticks_clob** (preferred) — CLOB order book polled every 10s, ±30s window around eval_time
2. **market_data.up_price / down_price** (fallback) — Gamma API prices, less precise timing

## Date Filter Recipe

To re-run for any date:
```bash
# Python script:
python3 scripts/analysis/shadow_audit_realistic_fills.py --date 2026-05-26

# Raw psql:
psql $DB_CONN -v audit_date="'2026-05-26'" -f scripts/analysis/shadow_audit_realistic_fills.sql
```

_Generated: {audit_date} by claude-sonnet-bg-2026-05-25-sql-library_
"""


# ──────────────────────────────────────────────────────────────────────────────
# Per-strategy fallback: queries one strategy at a time using the index
# idx_sd_strategy_action_evaluated(strategy_id, action, evaluated_at DESC)
# This avoids full table scans when the DB is under load.
# ──────────────────────────────────────────────────────────────────────────────

# Per-strategy query: uses (strategy_id, action, evaluated_at) index
PER_STRATEGY_QUERY = """
WITH
sd AS (
  SELECT DISTINCT ON (asset, window_ts)
    %(strategy_id)s::text  AS strategy_id,
    asset,
    window_ts,
    eval_offset,
    direction,
    (window_ts + 300 - eval_offset) AS eval_epoch
  FROM strategy_decisions
  WHERE strategy_id = %(strategy_id)s
    AND action = 'TRADE'
    AND evaluated_at >= %(audit_date_start)s
    AND evaluated_at <  %(audit_date_end)s
  ORDER BY asset, window_ts, eval_offset ASC
),
clob_bucketed AS (
  SELECT
    asset,
    (EXTRACT(EPOCH FROM ts)::bigint / 10) * 10   AS bucket_10s,
    AVG(up_best_ask)                              AS up_ask,
    AVG(down_best_ask)                            AS down_ask
  FROM ticks_clob
  WHERE ts >= %(audit_date_start)s AND ts < %(audit_date_end)s
    AND (up_best_ask IS NOT NULL OR down_best_ask IS NOT NULL)
  GROUP BY asset, (EXTRACT(EPOCH FROM ts)::bigint / 10) * 10
),
filled AS (
  SELECT
    sd.*,
    COALESCE(
      NULLIF(CASE UPPER(sd.direction)
        WHEN 'UP'   THEN cb0.up_ask WHEN 'YES' THEN cb0.up_ask
        WHEN 'DOWN' THEN cb0.down_ask WHEN 'NO' THEN cb0.down_ask END, 0),
      NULLIF(CASE UPPER(sd.direction)
        WHEN 'UP'   THEN cbp.up_ask WHEN 'YES' THEN cbp.up_ask
        WHEN 'DOWN' THEN cbp.down_ask WHEN 'NO' THEN cbp.down_ask END, 0),
      NULLIF(CASE UPPER(sd.direction)
        WHEN 'UP'   THEN cbm.up_ask WHEN 'YES' THEN cbm.up_ask
        WHEN 'DOWN' THEN cbm.down_ask WHEN 'NO' THEN cbm.down_ask END, 0),
      CASE UPPER(sd.direction)
        WHEN 'UP'   THEN md.up_price WHEN 'YES' THEN md.up_price
        WHEN 'DOWN' THEN md.down_price WHEN 'NO' THEN md.down_price END
    ) AS fill_price,
    CASE
      WHEN (cb0.up_ask IS NOT NULL OR cb0.down_ask IS NOT NULL
         OR cbp.up_ask IS NOT NULL OR cbp.down_ask IS NOT NULL
         OR cbm.up_ask IS NOT NULL OR cbm.down_ask IS NOT NULL) THEN 'clob'
      ELSE 'market_data'
    END AS fill_source,
    md.outcome,
    md.resolved
  FROM sd
  LEFT JOIN clob_bucketed cb0 ON cb0.asset = sd.asset
    AND cb0.bucket_10s = (sd.eval_epoch / 10) * 10
  LEFT JOIN clob_bucketed cbp ON cbp.asset = sd.asset
    AND cbp.bucket_10s = (sd.eval_epoch / 10) * 10 + 10
  LEFT JOIN clob_bucketed cbm ON cbm.asset = sd.asset
    AND cbm.bucket_10s = (sd.eval_epoch / 10) * 10 - 10
  LEFT JOIN market_data md ON md.asset = sd.asset
    AND md.window_ts = sd.window_ts AND md.timeframe = '5m'
)
SELECT
  strategy_id, asset, window_ts, eval_offset, direction, fill_price, fill_source,
  outcome, resolved,
  CASE
    WHEN resolved = true AND outcome IS NOT NULL THEN
      CASE
        WHEN (UPPER(direction) IN ('UP','YES') AND outcome='UP')   THEN 'WIN'
        WHEN (UPPER(direction) IN ('DOWN','NO') AND outcome='DOWN') THEN 'WIN'
        ELSE 'LOSS'
      END
    ELSE 'FLAT'
  END AS result,
  CASE WHEN resolved=true AND outcome IS NOT NULL AND fill_price > 0 THEN
    CASE WHEN (UPPER(direction) IN ('UP','YES') AND outcome='UP')
            OR (UPPER(direction) IN ('DOWN','NO') AND outcome='DOWN')
         THEN 25.0 * (1.0/fill_price - 1.0 - %(fee_rate)s)
         ELSE -25.0 END
  ELSE 0 END AS pnl_25,
  CASE WHEN resolved=true AND outcome IS NOT NULL AND fill_price > 0 THEN
    CASE WHEN (UPPER(direction) IN ('UP','YES') AND outcome='UP')
            OR (UPPER(direction) IN ('DOWN','NO') AND outcome='DOWN')
         THEN 5.0 * (1.0/fill_price - 1.0 - %(fee_rate)s)
         ELSE -5.0 END
  ELSE 0 END AS pnl_5
FROM filled
"""


def run_per_strategy_fallback(conn, audit_date_start, audit_date_end):
    """Run the audit per-strategy using the (strategy_id, action, evaluated_at) index.

    First discovers strategy IDs from `strategy_decisions` using a fast indexed
    per-day scan (we query from the NOTE_667_BASELINE known list first, then
    discover any new ones), then runs PER_STRATEGY_QUERY for each.
    """
    # Step 1: Get strategy list — query each known strategy to check if it has fires today
    # Use a LIMIT 1 query per strategy to quickly check existence
    known_strategies = list(NOTE_667_BASELINE.keys())

    # Also discover new strategies not in our baseline
    cur = conn.cursor()
    print(f"  Fallback: discovering strategy IDs with per-strategy index scans...", flush=True)

    active_strategies = []
    for sid in known_strategies:
        try:
            cur.execute("""
                SELECT 1 FROM strategy_decisions
                WHERE strategy_id = %s AND action='TRADE'
                  AND evaluated_at >= %s AND evaluated_at < %s
                LIMIT 1
            """, (sid, audit_date_start, audit_date_end))
            if cur.fetchone():
                active_strategies.append(sid)
            conn.commit()
        except Exception:
            conn.rollback()

    # Also find any new strategies not in our known list (use a limited scan)
    try:
        cur.execute("""
            SELECT DISTINCT strategy_id
            FROM (
              SELECT strategy_id
              FROM strategy_decisions
              WHERE action='TRADE'
                AND evaluated_at >= %s AND evaluated_at < %s
              LIMIT 5000
            ) s
        """, (audit_date_start, audit_date_end))
        new_strats = [r[0] for r in cur.fetchall() if r[0] not in active_strategies]
        active_strategies.extend(new_strats)
        conn.commit()
    except Exception:
        conn.rollback()

    print(f"  Found {len(active_strategies)} active strategies. Querying each...", flush=True)

    # Step 2: Pre-compute clob_bucketed once (shared across all strategy queries)
    # Cache it in Python to avoid re-running for each strategy
    print("  Pre-fetching ticks_clob buckets...", flush=True)
    try:
        cur.execute("""
            SELECT asset,
                   (EXTRACT(EPOCH FROM ts)::bigint / 10) * 10 AS bucket_10s,
                   AVG(up_best_ask)   AS up_ask,
                   AVG(down_best_ask) AS down_ask
            FROM ticks_clob
            WHERE ts >= %s AND ts < %s
              AND (up_best_ask IS NOT NULL OR down_best_ask IS NOT NULL)
            GROUP BY asset, (EXTRACT(EPOCH FROM ts)::bigint / 10) * 10
        """, (audit_date_start, audit_date_end))
        clob_rows = cur.fetchall()
        conn.commit()
        # Build lookup: (asset, bucket_10s) -> (up_ask, down_ask)
        clob_lookup = {}
        for row in clob_rows:
            clob_lookup[(row[0], int(row[1]))] = (row[2], row[3])
        print(f"  Cached {len(clob_lookup)} ticks_clob buckets.", flush=True)
    except Exception as e:
        print(f"  ticks_clob pre-fetch failed: {e}. Fill source will be market_data.", flush=True)
        conn.rollback()
        clob_lookup = {}

    # Step 3: Pre-fetch market_data outcomes for today
    print("  Pre-fetching market_data outcomes...", flush=True)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT asset, window_ts, up_price, down_price, outcome, resolved
            FROM market_data
            WHERE timeframe = '5m'
              AND window_start >= %s AND window_start < %s
        """, (audit_date_start, audit_date_end))
        md_rows = cur.fetchall()
        conn.commit()
        # Build lookup: (asset, window_ts) -> (up_price, down_price, outcome, resolved)
        md_lookup = {}
        for row in md_rows:
            md_lookup[(row[0], int(row[1]))] = (row[2], row[3], row[4], row[5])
        print(f"  Cached {len(md_lookup)} market_data windows.", flush=True)
    except Exception as e:
        print(f"  market_data pre-fetch failed: {e}. Outcomes will be missing.", flush=True)
        conn.rollback()
        md_lookup = {}

    # Step 4: Query each strategy and compute fills in Python
    all_rows = []
    for sid in active_strategies:
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT DISTINCT ON (asset, window_ts)
                    asset, window_ts, eval_offset, direction,
                    (window_ts + 300 - eval_offset) AS eval_epoch
                FROM strategy_decisions
                WHERE strategy_id = %s
                  AND action = 'TRADE'
                  AND evaluated_at >= %s AND evaluated_at < %s
                ORDER BY asset, window_ts, eval_offset ASC
            """, (sid, audit_date_start, audit_date_end))
            sd_rows = cur.fetchall()
            conn.commit()
        except Exception as e:
            print(f"  Strategy {sid} query failed: {e}", flush=True)
            conn.rollback()
            continue

        for row in sd_rows:
            asset, window_ts, eval_offset, direction, eval_epoch = row
            window_ts_int = int(window_ts)
            eval_epoch_int = int(eval_epoch)
            bucket = (eval_epoch_int // 10) * 10

            # Look up CLOB fill
            clob_entry = (
                clob_lookup.get((asset, bucket)) or
                clob_lookup.get((asset, bucket + 10)) or
                clob_lookup.get((asset, bucket - 10))
            )
            dir_upper = (direction or '').upper()
            if clob_entry:
                if dir_upper in ('UP', 'YES'):
                    clob_fill = clob_entry[0]  # up_ask
                else:
                    clob_fill = clob_entry[1]  # down_ask
                fill_source = 'clob'
            else:
                clob_fill = None
                fill_source = 'market_data'

            # Look up market_data
            md_entry = md_lookup.get((asset, window_ts_int))
            if md_entry:
                md_up, md_down, outcome, resolved = md_entry
                if dir_upper in ('UP', 'YES'):
                    md_fill = md_up
                else:
                    md_fill = md_down
            else:
                md_fill = None
                outcome = None
                resolved = None

            # Composite fill
            fill_price = clob_fill if (clob_fill and clob_fill > 0) else md_fill

            # Result
            if resolved and outcome:
                if (dir_upper in ('UP', 'YES') and outcome == 'UP') or \
                   (dir_upper in ('DOWN', 'NO') and outcome == 'DOWN'):
                    result = 'WIN'
                else:
                    result = 'LOSS'
            else:
                result = 'FLAT'

            # PnL
            if result == 'WIN' and fill_price and fill_price > 0:
                pnl_25 = 25.0 * (1.0 / fill_price - 1.0 - FEE_RATE)
                pnl_5  = 5.0  * (1.0 / fill_price - 1.0 - FEE_RATE)
            elif result == 'LOSS':
                pnl_25 = -25.0
                pnl_5  = -5.0
            else:
                pnl_25 = 0.0
                pnl_5  = 0.0

            all_rows.append({
                'strategy_id': sid,
                'asset': asset,
                'window_ts': window_ts_int,
                'eval_offset': eval_offset,
                'direction': direction,
                'fill_price': fill_price,
                'fill_source': fill_source,
                'outcome': outcome,
                'resolved': resolved,
                'result': result,
                'pnl_25': pnl_25,
                'pnl_5': pnl_5,
            })

    return all_rows


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="GHOST Shadow Audit v2 — Realistic Fills")
    parser.add_argument('--date', default=str(date.today()), help='Audit date (YYYY-MM-DD)')
    parser.add_argument('--no-save', action='store_true', help='Skip saving notes to RDS')
    args = parser.parse_args()

    audit_date = args.date
    print(f"\nConnecting to RDS...", flush=True)
    conn = get_conn()
    print(f"Connected. Running audit for {audit_date}...", flush=True)

    # Fetch note #667 baseline
    note_667_body = check_note_667(conn, audit_date)
    if note_667_body:
        print("  Found note #667 baseline.", flush=True)
    else:
        print("  Note #667 not found or not accessible.", flush=True)

    # Run main audit query
    # Use date range (not ::date cast) to allow index scan on evaluated_at
    audit_date_start = f"{audit_date} 00:00:00+00"
    audit_date_end   = f"{audit_date} 23:59:59.999999+00"

    # Strategy 1: Try the full CTE query first (fast if RDS not overloaded)
    # Strategy 2: Fall back to per-strategy queries (uses index path)
    rows = []
    try:
        print("  Trying full CTE query...", flush=True)
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(AUDIT_QUERY, {
            'audit_date_start': audit_date_start,
            'audit_date_end':   audit_date_end,
            'fee_rate': FEE_RATE
        })
        rows = cur.fetchall()
        conn.commit()
        print(f"  Full query succeeded: {len(rows)} per-fire rows.", flush=True)
    except Exception as e:
        print(f"  Full query failed ({type(e).__name__}: {e}). Falling back to per-strategy queries...", flush=True)
        conn.rollback()
        rows = run_per_strategy_fallback(conn, audit_date_start, audit_date_end)
        print(f"  Per-strategy fallback complete: {len(rows)} per-fire rows.", flush=True)

    if not rows:
        print("  No TRADE decisions found for this date. Exiting.")
        conn.close()
        return

    # Aggregate
    results = aggregate_by_strategy(rows)

    # Print report
    candidates = print_report(results, audit_date, note_667_body)

    if not args.no_save:
        # Build and save Note A (analysis)
        print("\nSaving Note A (analysis) to RDS...")
        note_a_body = build_note_body(results, audit_date, candidates, note_667_body)
        note_a_id = save_note(
            conn,
            title=f"GHOST shadow audit v2 — REALISTIC fills from ticks_clob ({audit_date})",
            body=note_a_body,
            tags=['ghost-audit', 'shadow-performance', 'realistic-fills', audit_date],
            status='open',
            author='claude-sonnet-bg-2026-05-25-shadow-v2'
        )
        print(f"  Note A saved: id={note_a_id}")

        # Build and save Note B (SQL library)
        print("Saving Note B (SQL library) to RDS...")
        note_b_body = build_sql_library_body(audit_date)
        note_b_id = save_note(
            conn,
            title="Shadow audit SQL library — realistic-fill GHOST performance scripts (reusable)",
            body=note_b_body,
            tags=['shadow-audit', 'sql-library', 'reusable-analysis', 'maintenance'],
            status='open',
            author='claude-sonnet-bg-2026-05-25-sql-library'
        )
        print(f"  Note B saved: id={note_b_id}")

        print(f"\nNote IDs: A={note_a_id}, B={note_b_id}")
    else:
        print("\n[--no-save] Skipped saving notes.")

    conn.close()
    print("Done.\n")


if __name__ == '__main__':
    main()
