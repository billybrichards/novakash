"""
Exit-monitor configuration backtest.

Compares 7 candidate exit-monitor configurations against the actual fills
from `v9_lgb_only` and `v10_lgb_only` over the last 48 hours.

Read-only. No writes. No deploys.

Usage on Montreal:
    cd /home/novakash/novakash
    set -a && source engine/.env && set +a
    python3 scripts/ops/backtest_exit_monitor.py
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg


STRATEGY_IDS = ("v9_lgb_only", "v10_lgb_only")
LOOKBACK_HOURS = 48

# Counterfactual sell mechanics (per task brief)
SIZE_AT_MARK = 0.96
RESIDUAL_PCT = 0.04
MARK_HAIRCUT = 0.7  # conservative — thin-book


@dataclass
class TickSnapshot:
    ts: datetime
    eval_offset: int  # seconds-before-close (window_ts - ts)
    mark: float | None  # bid for held side
    prob_opposite: float | None  # P(opposite of held direction)
    lgb_dist: float | None


@dataclass
class Trade:
    id: int
    strategy_id: str
    direction_held: str  # UP / DOWN
    fill_price: float
    fill_size: float
    pnl_usd: float
    outcome: str | None  # WIN / LOSS / None
    created_at: datetime
    window_ts: int
    direction_won: bool  # actually won


# ---------------------------------------------------------------------------
# Configuration definitions
# ---------------------------------------------------------------------------

# Each config has:
#   tiers: list of (eval_offset_lo, eval_offset_hi, mark_threshold, ticks_required)
#       eval_offset is seconds-before-close; tier active when offset in [lo, hi]
#       fire when mark < threshold for `ticks_required` consecutive snapshots
#   lgb_gate: optional dict with 'p_opposite_min', 'lgb_dist_min'
#       (only fire if LGB predicts opposite direction with these thresholds)

CONFIGS: dict[str, dict[str, Any]] = {
    # A: tier3-only T-60..T-30, mark<0.70, ticks=3 (legacy single-tier)
    "A_legacy_tier3": {
        "tiers": [(30, 60, 0.70, 3)],
        "lgb_gate": None,
    },
    # B: 3-tier 0.50/0.55/0.70 x 5/4/3 ticks T-200..T-30 (current)
    "B_current_3tier": {
        "tiers": [
            (130, 200, 0.50, 5),
            (60, 130, 0.55, 4),
            (30, 60, 0.70, 3),
        ],
        "lgb_gate": None,
    },
    # C: B + strict LGB gate
    "C_3tier_strict_lgb": {
        "tiers": [
            (130, 200, 0.50, 5),
            (60, 130, 0.55, 4),
            (30, 60, 0.70, 3),
        ],
        "lgb_gate": {"p_opposite_min": 0.85, "lgb_dist_min": 0.20},
    },
    # D: B + permissive LGB gate
    "D_3tier_permissive_lgb": {
        "tiers": [
            (130, 200, 0.50, 5),
            (60, 130, 0.55, 4),
            (30, 60, 0.70, 3),
        ],
        "lgb_gate": {"p_opposite_min": 0.55, "lgb_dist_min": 0.10},
    },
    # E: tier3-only + permissive LGB gate
    "E_tier3_permissive_lgb": {
        "tiers": [(30, 60, 0.70, 3)],
        "lgb_gate": {"p_opposite_min": 0.55, "lgb_dist_min": 0.10},
    },
    # F: hold-forever (no exit)
    "F_hold_forever": {
        "tiers": [],
        "lgb_gate": None,
    },
    # G: 3-tier looser thresholds 0.30/0.40/0.60 x 5/4/3 ticks
    "G_3tier_loose": {
        "tiers": [
            (130, 200, 0.30, 5),
            (60, 130, 0.40, 4),
            (30, 60, 0.60, 3),
        ],
        "lgb_gate": None,
    },
}


# ---------------------------------------------------------------------------
# DB loaders
# ---------------------------------------------------------------------------


async def load_trades(conn: asyncpg.Connection) -> list[Trade]:
    rows = await conn.fetch(
        f"""
        SELECT id, strategy_id, direction, fill_price, fill_size, outcome,
               pnl_usd, created_at, metadata
        FROM trades
        WHERE created_at > NOW() - INTERVAL '{LOOKBACK_HOURS} hours'
          AND strategy_id = ANY($1::text[])
        ORDER BY created_at
        """,
        list(STRATEGY_IDS),
    )

    trades: list[Trade] = []
    for r in rows:
        meta = r["metadata"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        if not meta:
            continue

        # window_ts from dedup_key 'strategy:window_ts:DIR'
        window_ts: int | None = None
        dk = meta.get("dedup_key")
        if dk:
            parts = dk.split(":")
            if len(parts) >= 2:
                try:
                    window_ts = int(parts[1])
                except ValueError:
                    pass
        if window_ts is None:
            # fallback — derive from market_slug
            slug = meta.get("market_slug", "")
            tail = slug.rsplit("-", 1)[-1]
            try:
                window_ts = int(tail)
            except ValueError:
                continue

        direction_held = "UP" if r["direction"] == "YES" else "DOWN"

        # ground truth: prefer trades.outcome, else infer from pnl
        if r["outcome"] == "WIN":
            direction_won = True
        elif r["outcome"] == "LOSS":
            direction_won = False
        else:
            # unresolved — skip
            continue

        trades.append(
            Trade(
                id=r["id"],
                strategy_id=r["strategy_id"],
                direction_held=direction_held,
                fill_price=float(r["fill_price"]),
                fill_size=float(r["fill_size"]),
                pnl_usd=float(r["pnl_usd"]) if r["pnl_usd"] is not None else 0.0,
                outcome=r["outcome"],
                created_at=r["created_at"],
                window_ts=window_ts,
                direction_won=direction_won,
            )
        )
    return trades


async def load_clob_ticks(
    conn: asyncpg.Connection, window_ts: int
) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT ts, up_best_bid, down_best_bid
        FROM ticks_clob
        WHERE asset='BTC' AND timeframe='5m' AND window_ts=$1
        ORDER BY ts
        """,
        window_ts,
    )
    return [dict(r) for r in rows]


async def load_decisions(
    conn: asyncpg.Connection, strategy_id: str, window_ts: int
) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT eval_offset, metadata_json
        FROM strategy_decisions
        WHERE strategy_id=$1 AND window_ts=$2
          AND action IN ('TRADE','SKIP')
        ORDER BY eval_offset DESC
        """,
        strategy_id,
        window_ts,
    )
    out = []
    for r in rows:
        meta = r["metadata_json"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        if not meta:
            continue
        out.append(
            {
                "eval_offset": r["eval_offset"],
                "lgb_dist": meta.get("lgb_dist"),
                "probability_lgb": meta.get("probability_lgb"),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------


def build_tick_snapshots(
    trade: Trade,
    clob_ticks: list[dict],
    decisions: list[dict],
) -> list[TickSnapshot]:
    """Walk forward in 2s cadence; for each clob tick after fill, attach
    nearest LGB decision (closest eval_offset)."""
    snaps: list[TickSnapshot] = []
    # window_ts is the OPEN time; close = open + 300s. eval_offset = sec-before-close.
    close_ts = trade.window_ts + 300
    for ct in clob_ticks:
        ts = ct["ts"]
        if ts < trade.created_at:
            continue
        offset = close_ts - int(ts.timestamp())
        if offset < 0:
            break
        mark = ct["up_best_bid"] if trade.direction_held == "UP" else ct["down_best_bid"]
        # nearest decision by offset
        prob_op = None
        lgb_d = None
        if decisions:
            best = min(decisions, key=lambda d: abs(d["eval_offset"] - offset))
            if abs(best["eval_offset"] - offset) <= 30:  # within 30s
                p_up = best.get("probability_lgb")
                if p_up is not None:
                    prob_op = (1.0 - p_up) if trade.direction_held == "UP" else p_up
                lgb_d = best.get("lgb_dist")
        snaps.append(
            TickSnapshot(
                ts=ts,
                eval_offset=offset,
                mark=float(mark) if mark is not None else None,
                prob_opposite=prob_op,
                lgb_dist=lgb_d,
            )
        )
    return snaps


def simulate(
    trade: Trade, snaps: list[TickSnapshot], cfg: dict[str, Any]
) -> tuple[float, bool, TickSnapshot | None]:
    """Run config against snapshots, return (pnl, exit_fired, exit_snap)."""
    tiers = cfg["tiers"]
    lgb_gate = cfg["lgb_gate"]

    if not tiers:
        # no exit
        return trade.pnl_usd, False, None

    consec = 0
    active_tier_idx = -1
    for snap in snaps:
        if snap.mark is None:
            continue
        # find applicable tier
        tier_idx = -1
        for i, (lo, hi, _, _) in enumerate(tiers):
            if lo <= snap.eval_offset <= hi:
                tier_idx = i
                break
        if tier_idx == -1:
            consec = 0
            active_tier_idx = -1
            continue
        if tier_idx != active_tier_idx:
            consec = 0
            active_tier_idx = tier_idx
        _, _, threshold, ticks_required = tiers[tier_idx]
        if snap.mark < threshold:
            consec += 1
        else:
            consec = 0
            continue
        if consec >= ticks_required:
            # candidate exit — apply LGB gate
            if lgb_gate is not None:
                if snap.prob_opposite is None or snap.lgb_dist is None:
                    # missing LGB → don't fire
                    continue
                if (
                    snap.prob_opposite < lgb_gate["p_opposite_min"]
                    or snap.lgb_dist < lgb_gate["lgb_dist_min"]
                ):
                    continue
            # FIRE EXIT
            mark = snap.mark
            sell_proceeds = (
                SIZE_AT_MARK * trade.fill_size * (mark * MARK_HAIRCUT)
                + RESIDUAL_PCT * trade.fill_size * (1.0 if trade.direction_won else 0.0)
            )
            cf_pnl = sell_proceeds - (trade.fill_price * trade.fill_size)
            return cf_pnl, True, snap
    # no exit fired
    return trade.pnl_usd, False, None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def fmt(x: float, w: int = 8) -> str:
    return f"{x:>{w}.2f}"


async def main() -> None:
    url = (
        os.environ["DATABASE_URL"]
        .replace("postgresql+asyncpg://", "postgresql://")
        .replace("+asyncpg", "")
    )
    conn = await asyncpg.connect(url)
    try:
        print(f"Loading trades (last {LOOKBACK_HOURS}h)...")
        trades = await load_trades(conn)
        print(f"  {len(trades)} resolved trades")
        if not trades:
            print("No trades — abort.")
            return

        # Pre-load per-window clob ticks + decisions
        per_window_clob: dict[int, list[dict]] = {}
        per_window_decisions: dict[tuple[str, int], list[dict]] = {}
        windows = sorted({t.window_ts for t in trades})
        print(f"Loading clob ticks for {len(windows)} windows...")
        for w in windows:
            per_window_clob[w] = await load_clob_ticks(conn, w)
        print("Loading strategy_decisions...")
        for t in trades:
            key = (t.strategy_id, t.window_ts)
            if key not in per_window_decisions:
                per_window_decisions[key] = await load_decisions(
                    conn, t.strategy_id, t.window_ts
                )

        # diagnostic — coverage
        covered = sum(1 for t in trades if per_window_clob.get(t.window_ts))
        dec_covered = sum(
            1 for t in trades if per_window_decisions.get((t.strategy_id, t.window_ts))
        )
        print(
            f"Coverage: clob={covered}/{len(trades)} decisions={dec_covered}/{len(trades)}"
        )

        # Pre-build snapshots once per trade (independent of config)
        snap_cache: dict[int, list[TickSnapshot]] = {}
        for t in trades:
            snap_cache[t.id] = build_tick_snapshots(
                t,
                per_window_clob.get(t.window_ts, []),
                per_window_decisions.get((t.strategy_id, t.window_ts), []),
            )

        # Run configs
        results: dict[str, dict[str, Any]] = {}
        for cfg_name, cfg in CONFIGS.items():
            n_exits = 0
            premature = 0  # exit fired but trade actually won
            saved_loss = 0  # exit fired and trade actually lost
            cf_total = 0.0
            actual_total = 0.0
            cf_by_strat: dict[str, float] = defaultdict(float)
            actual_by_strat: dict[str, float] = defaultdict(float)
            n_by_strat: dict[str, int] = defaultdict(int)
            for t in trades:
                snaps = snap_cache[t.id]
                cf_pnl, fired, _ = simulate(t, snaps, cfg)
                cf_total += cf_pnl
                actual_total += t.pnl_usd
                cf_by_strat[t.strategy_id] += cf_pnl
                actual_by_strat[t.strategy_id] += t.pnl_usd
                n_by_strat[t.strategy_id] += 1
                if fired:
                    n_exits += 1
                    if t.direction_won:
                        premature += 1
                    else:
                        saved_loss += 1
            n_actual_losses = sum(1 for t in trades if not t.direction_won)
            results[cfg_name] = {
                "n_fills": len(trades),
                "n_exits": n_exits,
                "premature": premature,
                "saved_loss": saved_loss,
                "premature_pct": (premature / n_exits * 100) if n_exits else 0.0,
                "loss_avoidance_pct": (
                    saved_loss / n_actual_losses * 100 if n_actual_losses else 0.0
                ),
                "cf_total": cf_total,
                "actual_total": actual_total,
                "delta_total": cf_total - actual_total,
                "cf_by_strat": dict(cf_by_strat),
                "actual_by_strat": dict(actual_by_strat),
                "n_by_strat": dict(n_by_strat),
            }

        # Output
        print()
        print("=" * 110)
        print(
            f"{'Config':<26} {'n_fills':>8} {'n_exits':>8} {'prem%':>8} {'avoid%':>8} "
            f"{'Δv9':>10} {'Δv10':>10} {'Δtotal':>10}"
        )
        print("=" * 110)
        # baseline (actual)
        any_r = next(iter(results.values()))
        actual_v9 = any_r["actual_by_strat"].get("v9_lgb_only", 0.0)
        actual_v10 = any_r["actual_by_strat"].get("v10_lgb_only", 0.0)
        print(
            f"{'BASELINE (actual pnl)':<26} {len(trades):>8} {'-':>8} {'-':>8} {'-':>8} "
            f"{fmt(actual_v9,10)} {fmt(actual_v10,10)} {fmt(actual_v9 + actual_v10,10)}"
        )
        for name, r in results.items():
            cf_v9 = r["cf_by_strat"].get("v9_lgb_only", 0.0)
            cf_v10 = r["cf_by_strat"].get("v10_lgb_only", 0.0)
            d_v9 = cf_v9 - actual_v9
            d_v10 = cf_v10 - actual_v10
            print(
                f"{name:<26} {r['n_fills']:>8} {r['n_exits']:>8} "
                f"{r['premature_pct']:>7.1f}% {r['loss_avoidance_pct']:>7.1f}% "
                f"{fmt(d_v9,10)} {fmt(d_v10,10)} {fmt(r['delta_total'],10)}"
            )
        print("=" * 110)
        print()
        print("Per-strategy fill counts:")
        for s, n in any_r["n_by_strat"].items():
            print(f"  {s}: {n}")

        # JSON dump for downstream
        out = {
            "lookback_hours": LOOKBACK_HOURS,
            "n_trades": len(trades),
            "n_by_strategy": any_r["n_by_strat"],
            "actual_pnl_v9": actual_v9,
            "actual_pnl_v10": actual_v10,
            "actual_pnl_total": actual_v9 + actual_v10,
            "n_actual_losses": sum(1 for t in trades if not t.direction_won),
            "configs": {
                k: {
                    "n_exits": v["n_exits"],
                    "premature_pct": v["premature_pct"],
                    "loss_avoidance_pct": v["loss_avoidance_pct"],
                    "delta_v9": v["cf_by_strat"].get("v9_lgb_only", 0.0) - actual_v9,
                    "delta_v10": v["cf_by_strat"].get("v10_lgb_only", 0.0) - actual_v10,
                    "delta_total": v["delta_total"],
                }
                for k, v in results.items()
            },
        }
        out_path = "/tmp/exit_monitor_backtest_results.json"
        with open(out_path, "w") as f:
            json.dump(out, f, indent=2, default=str)
        print(f"\nJSON results written to {out_path}")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
