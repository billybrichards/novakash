"""Shadow / ghost strategy performance analysis — uses the NEW v3/v4 columns
populated by PR #216.

Unlocks the kind of per-strategy, per-regime, per-gate analysis that used to
require JSONB extraction from ``window_evaluation_traces.surface_json``. Now it's
plain SQL against ``window_snapshots`` + ``strategy_decisions`` +
``window_states.actual_direction``.

Use this for questions like:
  - "How would v15m_fusion have done if we'd flipped it LIVE for the last 24h?"
  - "What's the WR of v4_fusion trades when regime_confidence >= 0.9?"
  - "Does the consensus gate save or cost us net when all 3 price sources agree?"
  - "Which gate fires the most false-positives on UP-resolving windows?"

Usage
-----

On Montreal:

    ssh novakash@15.223.247.178 'python3 /home/novakash/novakash/scripts/ops/shadow_analysis.py'

With custom lookback:

    ... '--hours 6'         # default 24
    ... '--strategy v15m_fusion'
    ... '--regime risk_off'

Or dump ALL per-strategy summaries (no filters):

    ... '--all'

The queries are READ-ONLY. Zero relayer quota consumed. All uses Railway PG
credentials from engine .env (``DATABASE_URL``).

Related
-------

- ``scripts/ops/wallet_truth.py`` — canonical wallet P&L (same design pattern)
- ``docs/analysis/TRACE_DATA_GUIDE.md`` — which table for what
- Memory note ``reference_clob_audit.md`` — why DB `trades` alone is unreliable
- Hub note #48 (session 2026-04-16) — PR #216 surface persistence shipped the
  denormalised columns this script depends on
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv

# Same env-discovery as wallet_truth.py
for env_path in (
    "/home/novakash/novakash/engine/.env",
    os.path.expanduser("~/Code/novakash/engine/.env"),
    ".env",
):
    if os.path.exists(env_path):
        load_dotenv(env_path)
        break


async def _fetch_shadow_summary(
    conn, hours: int, strategy_filter: Optional[str] = None
) -> list:
    """Per-strategy summary: TRADE intent count, simulated WR, simulated P&L.

    Uses ``strategy_decisions`` (every strategy's decision per window) joined
    with ``window_states.actual_direction`` (oracle truth after PR #213).

    A strategy "wins" a window in shadow if its decided ``direction`` matches
    ``actual_direction``. Simulated P&L uses canonical $5 stake + $4.86 avg
    fill price + $8.70 avg win payout (empirical from wallet_truth.py run).
    """
    # Strategy-filter clause (applied to the deduplicated subquery)
    sf_clause = f"AND strategy_id = '{strategy_filter}'" if strategy_filter else ""

    # One row per (strategy, window) via DISTINCT ON — strategy_decisions
    # has ~12 rows per window per strategy (re-evaluated every few seconds),
    # so without dedup the counts are 10-50x inflated.
    sql = f"""
        SELECT
            sd.strategy_id,
            COUNT(*) FILTER (
                WHERE sd.action = 'TRADE'
                AND COALESCE(snap.actual_direction, UPPER(snap.poly_winner)) IS NOT NULL
            ) AS n_resolved,
            COUNT(*) FILTER (
                WHERE sd.action = 'TRADE'
                AND sd.direction = COALESCE(snap.actual_direction, UPPER(snap.poly_winner))
            ) AS n_wins,
            COUNT(*) FILTER (
                WHERE sd.action = 'TRADE'
                AND COALESCE(snap.actual_direction, UPPER(snap.poly_winner)) IS NOT NULL
                AND sd.direction <> COALESCE(snap.actual_direction, UPPER(snap.poly_winner))
            ) AS n_losses,
            COUNT(*) FILTER (
                WHERE sd.action = 'TRADE'
                AND COALESCE(snap.actual_direction, UPPER(snap.poly_winner)) IS NULL
            ) AS n_pending,
            ROUND(100.0 * COUNT(*) FILTER (
                WHERE sd.action = 'TRADE'
                AND sd.direction = COALESCE(snap.actual_direction, UPPER(snap.poly_winner))
            ) / NULLIF(COUNT(*) FILTER (
                WHERE sd.action = 'TRADE'
                AND COALESCE(snap.actual_direction, UPPER(snap.poly_winner)) IS NOT NULL
            ), 0), 1) AS wr_pct
        FROM (
            SELECT DISTINCT ON (strategy_id, asset, window_ts, timeframe)
                strategy_id, asset, window_ts, timeframe, action, direction, skip_reason, evaluated_at
            FROM strategy_decisions
            WHERE evaluated_at > NOW() - INTERVAL '{hours}hours'
              {sf_clause}
            ORDER BY strategy_id, asset, window_ts, timeframe, evaluated_at DESC
        ) sd
        LEFT JOIN window_snapshots snap
            ON snap.asset = sd.asset AND snap.window_ts = sd.window_ts
           AND snap.timeframe = sd.timeframe
        GROUP BY sd.strategy_id
        ORDER BY n_resolved DESC
    """
    return await conn.fetch(sql)


async def _fetch_skip_reasons(
    conn, hours: int, strategy: str, top_n: int = 10
) -> list:
    """Top skip reasons for a strategy, with would-have-won/lost counts.

    Uses the oracle-resolved direction to bucket what each skip cost us vs
    saved us.
    """
    sql = f"""
        WITH skip_windows AS (
            SELECT
                sd.skip_reason,
                sd.direction AS signal_dir,
                COALESCE(snap.actual_direction, UPPER(snap.poly_winner)) AS actual_direction
            FROM (
                SELECT DISTINCT ON (strategy_id, asset, window_ts, timeframe)
                    strategy_id, asset, window_ts, timeframe, action, direction, skip_reason
                FROM strategy_decisions
                WHERE evaluated_at > NOW() - INTERVAL '{hours}hours'
                  AND strategy_id = '{strategy}'
                ORDER BY strategy_id, asset, window_ts, timeframe, evaluated_at DESC
            ) sd
            LEFT JOIN window_snapshots snap
                ON snap.asset = sd.asset AND snap.window_ts = sd.window_ts
               AND snap.timeframe = sd.timeframe
            WHERE sd.action = 'SKIP'
        )
        SELECT
            skip_reason,
            COUNT(*) AS n_skips,
            COUNT(*) FILTER (WHERE signal_dir = actual_direction) AS would_have_won,
            COUNT(*) FILTER (WHERE signal_dir <> actual_direction
                             AND actual_direction IS NOT NULL) AS would_have_lost,
            COUNT(*) FILTER (WHERE actual_direction IS NULL) AS unresolved
        FROM skip_windows
        WHERE skip_reason IS NOT NULL
        GROUP BY skip_reason
        ORDER BY n_skips DESC
        LIMIT {top_n}
    """
    return await conn.fetch(sql)


async def _fetch_regime_performance(conn, hours: int) -> list:
    """WR by (strategy_id, v4_regime) — uses the new v4_regime column from
    window_snapshots (PR #216 + #213 pipeline populates it alongside
    actual_direction).
    """
    sql = f"""
        SELECT
            sd.strategy_id,
            snap.regime AS v4_regime,
            COUNT(*) AS n_windows,
            COUNT(*) FILTER (
                WHERE sd.direction = COALESCE(snap.actual_direction, UPPER(snap.poly_winner))
            ) AS n_wins,
            ROUND(100.0 * COUNT(*) FILTER (
                WHERE sd.direction = COALESCE(snap.actual_direction, UPPER(snap.poly_winner))
            ) / NULLIF(COUNT(*) FILTER (
                WHERE COALESCE(snap.actual_direction, UPPER(snap.poly_winner)) IS NOT NULL
            ), 0), 1) AS wr_pct
        FROM (
            SELECT DISTINCT ON (strategy_id, asset, window_ts, timeframe)
                strategy_id, asset, window_ts, timeframe, action, direction
            FROM strategy_decisions
            WHERE evaluated_at > NOW() - INTERVAL '{hours}hours'
            ORDER BY strategy_id, asset, window_ts, timeframe, evaluated_at DESC
        ) sd
        LEFT JOIN window_snapshots snap
            ON snap.asset = sd.asset AND snap.window_ts = sd.window_ts
           AND snap.timeframe = sd.timeframe
        WHERE sd.action = 'TRADE'
          AND snap.regime IS NOT NULL
          AND COALESCE(snap.actual_direction, UPPER(snap.poly_winner)) IS NOT NULL
        GROUP BY sd.strategy_id, snap.regime
        HAVING COUNT(*) >= 3
        ORDER BY sd.strategy_id, n_windows DESC
    """
    return await conn.fetch(sql)


async def _fetch_conviction_performance(conn, hours: int) -> list:
    """WR by strategy_conviction tier — uses PR #216 denormalised column."""
    sql = f"""
        SELECT
            sd.strategy_id,
            snap.strategy_conviction,
            COUNT(*) AS n,
            COUNT(*) FILTER (
                WHERE sd.direction = COALESCE(snap.actual_direction, UPPER(snap.poly_winner))
            ) AS n_wins,
            ROUND(100.0 * COUNT(*) FILTER (
                WHERE sd.direction = COALESCE(snap.actual_direction, UPPER(snap.poly_winner))
            ) / NULLIF(COUNT(*) FILTER (
                WHERE COALESCE(snap.actual_direction, UPPER(snap.poly_winner)) IS NOT NULL
            ), 0), 1) AS wr_pct
        FROM (
            SELECT DISTINCT ON (strategy_id, asset, window_ts, timeframe)
                strategy_id, asset, window_ts, timeframe, action, direction
            FROM strategy_decisions
            WHERE evaluated_at > NOW() - INTERVAL '{hours}hours'
            ORDER BY strategy_id, asset, window_ts, timeframe, evaluated_at DESC
        ) sd
        LEFT JOIN window_snapshots snap
            ON snap.asset = sd.asset AND snap.window_ts = sd.window_ts
           AND snap.timeframe = sd.timeframe
        WHERE sd.action = 'TRADE'
          AND snap.strategy_conviction IS NOT NULL
          AND COALESCE(snap.actual_direction, UPPER(snap.poly_winner)) IS NOT NULL
        GROUP BY sd.strategy_id, snap.strategy_conviction
        HAVING COUNT(*) >= 3
        ORDER BY sd.strategy_id, n
    """
    return await conn.fetch(sql)


async def _fetch_consensus_override_simulation(conn, hours: int) -> dict:
    """Simulate the 2026-04-16 consensus-override rule (Hub note #45 spec).

    Unblock ``consensus not safe_to_trade`` skips when:
      - sign(delta_binance) == sign(delta_chainlink)
      - sign(delta_tiingo)   == sign(delta_chainlink)
      - abs(delta_chainlink) >= 0.0005

    Returns {total_consensus_skips, would_trade, would_win, would_lose, net_pnl}
    """
    sql = f"""
        WITH blocked AS (
            SELECT
                snap.delta_chainlink, snap.delta_binance, snap.delta_tiingo,
                COALESCE(snap.actual_direction, UPPER(snap.poly_winner)) AS actual_direction
            FROM (
                SELECT DISTINCT ON (strategy_id, asset, window_ts, timeframe)
                    strategy_id, asset, window_ts, timeframe, action, skip_reason
                FROM strategy_decisions
                WHERE evaluated_at > NOW() - INTERVAL '{hours}hours'
                  AND strategy_id = 'v4_fusion'
                ORDER BY strategy_id, asset, window_ts, timeframe, evaluated_at DESC
            ) sd
            LEFT JOIN window_snapshots snap
                ON snap.asset = sd.asset AND snap.window_ts = sd.window_ts
               AND snap.timeframe = sd.timeframe
            WHERE sd.action = 'SKIP'
              AND sd.skip_reason LIKE '%consensus not safe_to_trade%'
        ),
        override_eligible AS (
            SELECT
                CASE WHEN delta_chainlink > 0 THEN 'UP' ELSE 'DOWN' END AS inferred_dir,
                actual_direction
            FROM blocked
            WHERE delta_chainlink IS NOT NULL
              AND delta_binance IS NOT NULL
              AND delta_tiingo IS NOT NULL
              AND ABS(delta_chainlink) >= 0.0005
              AND SIGN(delta_binance) = SIGN(delta_chainlink)
              AND SIGN(delta_tiingo) = SIGN(delta_chainlink)
              AND actual_direction IS NOT NULL
        )
        SELECT
            (SELECT COUNT(*) FROM blocked) AS total_consensus_skips,
            (SELECT COUNT(*) FROM override_eligible) AS would_trade,
            (SELECT COUNT(*) FROM override_eligible WHERE inferred_dir = actual_direction) AS would_win,
            (SELECT COUNT(*) FROM override_eligible WHERE inferred_dir <> actual_direction) AS would_lose
    """
    row = await conn.fetchrow(sql)
    if not row:
        return {}
    wins = int(row["would_win"] or 0)
    loses = int(row["would_lose"] or 0)
    # Canonical avg win +$2.90, avg loss -$4.77 (empirical from 2026-04-16)
    net = wins * 2.90 - loses * 4.77
    return {
        "total_consensus_skips": int(row["total_consensus_skips"] or 0),
        "would_trade": int(row["would_trade"] or 0),
        "would_win": wins,
        "would_lose": loses,
        "net_pnl": round(net, 2),
    }


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--strategy", help="Filter to one strategy (eg v15m_fusion)")
    parser.add_argument("--regime", help="Filter to one v4_regime value")
    parser.add_argument("--all", action="store_true", help="Show all sections")
    args = parser.parse_args()

    try:
        import asyncpg  # type: ignore
    except ImportError:
        print("ERROR: asyncpg not installed — run on Montreal or pip install asyncpg")
        sys.exit(1)

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set — load engine .env")
        sys.exit(1)

    conn = await asyncpg.connect(db_url)
    try:
        now = datetime.now(timezone.utc)
        print("=" * 90)
        print(
            "SHADOW ANALYSIS  —  "
            + now.strftime("%Y-%m-%d %H:%M UTC")
            + f"  —  last {args.hours}h"
            + (f"  —  strategy={args.strategy}" if args.strategy else "")
        )
        print("=" * 90)

        # SECTION 1 — Per-strategy shadow WR
        print("\n1. PER-STRATEGY SHADOW WIN RATE (TRADE intents → oracle outcome)")
        rows = await _fetch_shadow_summary(conn, args.hours, args.strategy)
        print(f"   {'strategy':<24s} {'trades':>7s} {'wins':>6s} {'loss':>6s} {'pend':>6s} {'WR':>6s}")
        for r in rows:
            print(
                f"   {r['strategy_id']:<24s} "
                f"{r['n_resolved'] or 0:>7d} "
                f"{r['n_wins'] or 0:>6d} "
                f"{r['n_losses'] or 0:>6d} "
                f"{r['n_pending'] or 0:>6d} "
                f"{(str(r['wr_pct']) + '%') if r['wr_pct'] else '-':>6s}"
            )

        # SECTION 2 — Skip reasons w/ would-have outcomes (only meaningful if strategy filter)
        if args.strategy:
            print(
                f"\n2. TOP SKIP REASONS for {args.strategy} (would-have-won / would-have-lost)"
            )
            rows = await _fetch_skip_reasons(conn, args.hours, args.strategy)
            print(f"   {'reason':<60s} {'skips':>6s} {'won':>5s} {'lost':>5s} {'unres':>5s}")
            for r in rows:
                reason = (r["skip_reason"] or "")[:58]
                print(
                    f"   {reason:<60s} "
                    f"{r['n_skips']:>6d} "
                    f"{r['would_have_won'] or 0:>5d} "
                    f"{r['would_have_lost'] or 0:>5d} "
                    f"{r['unresolved'] or 0:>5d}"
                )

        # SECTION 3 — WR by regime
        if args.all or args.regime or not args.strategy:
            print("\n3. WR by (strategy, v4_regime) — needs ≥3 samples per bucket")
            rows = await _fetch_regime_performance(conn, args.hours)
            print(f"   {'strategy':<22s} {'regime':<18s} {'n':>4s} {'wins':>5s} {'WR':>6s}")
            for r in rows:
                if args.regime and r["v4_regime"] != args.regime:
                    continue
                print(
                    f"   {r['strategy_id']:<22s} "
                    f"{(r['v4_regime'] or ''):<18s} "
                    f"{r['n_windows']:>4d} "
                    f"{r['n_wins'] or 0:>5d} "
                    f"{(str(r['wr_pct']) + '%') if r['wr_pct'] else '-':>6s}"
                )

        # SECTION 4 — WR by conviction tier
        print("\n4. WR by (strategy, strategy_conviction) — v4.4.0 column from PR #216")
        rows = await _fetch_conviction_performance(conn, args.hours)
        print(f"   {'strategy':<22s} {'tier':<10s} {'n':>4s} {'wins':>5s} {'avg_score':>9s} {'WR':>6s}")
        for r in rows:
            print(
                f"   {r['strategy_id']:<22s} "
                f"{(r['strategy_conviction'] or ''):<10s} "
                f"{r['n']:>4d} "
                f"{r['n_wins'] or 0:>5d} "
                f"{(str(r['avg_score']) if r['avg_score'] else '-'):>9s} "
                f"{(str(r['wr_pct']) + '%') if r['wr_pct'] else '-':>6s}"
            )

        # SECTION 5 — Consensus-override simulation (Hub note #45 rule)
        print("\n5. CONSENSUS OVERRIDE SIMULATION (v4_fusion, Hub note #45 rule)")
        sim = await _fetch_consensus_override_simulation(conn, args.hours)
        if sim:
            print(f"   Total consensus-blocked windows:  {sim['total_consensus_skips']}")
            print(f"   Eligible for override:            {sim['would_trade']}")
            print(f"     - Would win:                    {sim['would_win']}")
            print(f"     - Would lose:                   {sim['would_lose']}")
            wr = (100.0 * sim["would_win"] / sim["would_trade"]) if sim["would_trade"] else 0
            print(f"     - Override WR:                  {wr:.1f}%")
            print(f"   Simulated net P&L:                ${sim['net_pnl']:+.2f}")
        else:
            print("   No data")

        print()

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
