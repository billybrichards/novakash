#!/usr/bin/env python3
"""
Query margin engine performance from Railway PostgreSQL.
Run on AWS margin-engine server: python3 /opt/margin-engine/scripts/query_perf.py
"""

import asyncpg
import asyncio
from datetime import datetime, timezone


async def main():
    # Railway proxy connection (works from anywhere)
    conn = await asyncpg.connect(
        host="hopper.proxy.railway.net",
        port=35772,
        database="railway",
        user="postgres",
        password="wKbsHjsWoWaUKkzSqgCUIijtnOKHIcQj",
        timeout=30,
    )

    print("=" * 60)
    print("MARGIN ENGINE PERFORMANCE REPORT")
    print(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60)

    # Basic counts
    row = await conn.fetchrow("""
        SELECT 
            COUNT(*) as total_trades,
            SUM(CASE WHEN state = 'CLOSED' THEN 1 ELSE 0 END) as closed_trades,
            SUM(CASE WHEN state = 'OPEN' THEN 1 ELSE 0 END) as open_trades
        FROM margin_positions
    """)

    print(f"\n📊 POSITION COUNTS")
    print(f"   Total trades:    {row['total_trades']}")
    print(f"   Closed trades:   {row['closed_trades']}")
    print(f"   Open trades:     {row['open_trades']}")

    # Closed trade stats
    row = await conn.fetchrow("""
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN exit_reason = 'TAKE_PROFIT' THEN 1 ELSE 0 END) as tp,
            SUM(CASE WHEN exit_reason = 'STOP_LOSS' THEN 1 ELSE 0 END) as sl,
            SUM(CASE WHEN exit_reason = 'TRAILING_STOP' THEN 1 ELSE 0 END) as trailing,
            SUM(CASE WHEN exit_reason = 'PROBABILITY_REVERSAL' THEN 1 ELSE 0 END) as prob_rev,
            SUM(CASE WHEN exit_reason = 'REGIME_DETERIORATED' THEN 1 ELSE 0 END) as regime_exit,
            SUM(CASE WHEN exit_reason = 'MAX_HOLD_TIME' THEN 1 ELSE 0 END) as time_exit,
            SUM(CASE WHEN realised_pnl >= 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN realised_pnl < 0 THEN 1 ELSE 0 END) as losses,
            ROUND(AVG(realised_pnl)::numeric, 2) as avg_pnl,
            ROUND(SUM(realised_pnl)::numeric, 2) as total_pnl,
            MIN(realised_pnl) as worst_trade,
            MAX(realised_pnl) as best_trade
        FROM margin_positions 
        WHERE state = 'CLOSED'
    """)

    print(f"\n💰 CLOSED TRADE STATS")
    print(f"   Total closed:       {row['total']}")
    print(f"   Take Profits:       {row['tp']}")
    print(f"   Stop Losses:        {row['sl']}")
    print(f"   Trailing Stops:     {row['trailing']}")
    print(f"   Probability Rev:    {row['prob_rev']}")
    print(f"   Regime Exit:        {row['regime_exit']}")
    print(f"   Time Exits:         {row['time_exit']}")

    if row["total"] > 0:
        win_rate = row["wins"] / row["total"] * 100
        print(f"\n   Wins:               {row['wins']} ({win_rate:.1f}%)")
        print(f"   Losses:             {row['losses']}")
        print(f"   Win Rate:           {win_rate:.1f}%")

    print(f"\n   Avg P&L:            ${row['avg_pnl']}")
    print(f"   Total P&L:          ${row['total_pnl']}")
    print(f"   Best Trade:         ${row['best_pnl']}")
    print(f"   Worst Trade:        ${row['worst_pnl']}")

    # V4 entry analysis (if data exists)
    row = await conn.fetchrow("""
        SELECT 
            COUNT(*) as v4_trades,
            ROUND(AVG(v4_entry_macro_confidence)::numeric, 1) as avg_macro_conf,
            SUM(CASE WHEN v4_entry_macro_bias = 'BULL' THEN 1 ELSE 0 END) as bull_macro,
            SUM(CASE WHEN v4_entry_macro_bias = 'BEAR' THEN 1 ELSE 0 END) as bear_macro,
            SUM(CASE WHEN v4_entry_consensus_safe = true THEN 1 ELSE 0 END) as safe_consensus
        FROM margin_positions 
        WHERE v4_entry_macro_bias IS NOT NULL
    """)

    if row["v4_trades"] > 0:
        print(f"\n🔮 V4 FUSION CONTEXT")
        print(f"   Trades with V4:     {row['v4_trades']}")
        print(f"   Avg Macro Conf:     {row['avg_macro_conf']}%")
        print(f"   Bull Macro:         {row['bull_macro']}")
        print(f"   Bear Macro:         {row['bear_macro']}")
        print(f"   Safe Consensus:     {row['safe_consensus']}")

    # Continuation stats
    row = await conn.fetchrow("""
        SELECT 
            COUNT(*) as continued_trades,
            ROUND(AVG(continuation_count)::numeric, 1) as avg_continuations,
            MAX(continuation_count) as max_continuations
        FROM margin_positions 
        WHERE continuation_count > 0
    """)

    if row["continued_trades"] > 0:
        print(f"\n🔄 CONTINUATION STATS")
        print(f"   Continued trades:   {row['continued_trades']}")
        print(f"   Avg continuations:  {row['avg_continuations']}")
        print(f"   Max continuations:  {row['max_continuations']}")

    # Venue breakdown
    row = await conn.fetchrow("""
        SELECT 
            venue,
            COUNT(*) as count,
            ROUND(AVG(realised_pnl)::numeric, 2) as avg_pnl,
            SUM(realised_pnl) as total_pnl
        FROM margin_positions 
        WHERE state = 'CLOSED' AND venue IS NOT NULL
        GROUP BY venue
    """)

    if row:
        print(f"\n🏪 VENUE BREAKDOWN")
        print(
            f"   {row['venue']}: {row['count']} trades, avg ${row['avg_pnl']}, total ${row['total_pnl']}"
        )

    # Recent 10 trades
    recent = await conn.fetch("""
        SELECT 
            id, side, state, realised_pnl, exit_reason,
            opened_at, closed_at, v4_entry_regime, v4_entry_macro_bias,
            continuation_count, venue, strategy_version
        FROM margin_positions 
        ORDER BY opened_at DESC 
        LIMIT 10
    """)

    print(f"\n📝 RECENT 10 TRADES")
    print(
        f"{'Time':<12} {'Side':<6} {'State':<8} {'P&L':<10} {'Exit Reason':<25} {'V4 Context'}"
    )
    print("-" * 80)
    for r in recent:
        ts = r["opened_at"].strftime("%m/%d %H:%M") if r["opened_at"] else "???"
        side = r["side"]
        state = r["state"]
        pnl = f"${r['realised_pnl']:.2f}" if state == "CLOSED" else "OPEN    "
        exit_reason = r["exit_reason"] or "-"
        v4_ctx = f"{r['v4_entry_regime'] or '?'} | {r['v4_entry_macro_bias'] or '?'}"
        if r["continuation_count"] > 0:
            v4_ctx += f" (cont: {r['continuation_count']})"
        print(f"{ts:<12} {side:<6} {state:<8} {pnl:<10} {exit_reason:<25} {v4_ctx}")

    print("\n" + "=" * 60)

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
