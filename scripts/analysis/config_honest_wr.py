#!/usr/bin/env python3
"""
Honest config win-rate analysis — last N hours of 5m windows.

WHY "HONEST"?
-------------
The Hub UI surfaces headline numbers like "94.2% accuracy" that can be
misleading for several reasons:
  • Directional agreement ≠ Polymarket P&L (payout depends on entry price).
  • WR is often computed over "eligible" windows only (selection bias).
  • Base-rate context is missing — a 75% DOWN WR in a DOWN-heavy regime is
    not the same as a 75% WR in a balanced one.

This script:
  1. Pulls raw `window_snapshots` rows for the last N hours (default 72)
     directly from the Railway Postgres DB (no API pagination cap).
  2. Replicates v5.7c / v5.8 / v7.1 decision logic exactly as
     `hub/api/v58_monitor.py::_calc_outcome_row` does, so numbers match UI.
  3. Reports:
       • Eligible (config's own gate)
       • Windows with a resolvable ground-truth (close vs open OR poly_outcome)
       • Honest directional WR
       • Simulated Polymarket WR using gamma_up/gamma_down as entry price
         (cap at 0.85, 7.2% fee per reference constants)
       • Base rate (UP% / DOWN% across ALL windows, not just eligible)
       • Edge = directional_wr − base-rate-of-chosen-direction

GROUND TRUTH
------------
`actual_direction` = UP if close_price > open_price else DOWN.
If `poly_outcome` + `trade_direction` are present we prefer that (the engine
uses Polymarket resolution as the payout truth), else the Binance T-60
close is the fallback, matching `_calc_outcome_row` semantics.

POLY-SIM P&L (per-config)
-------------------------
entry = gamma_up_price if chosen_dir=="UP" else gamma_down_price
skip  if entry is None or entry <= 0.005 or entry >= 0.995
skip  if entry > 0.85  (conservative cap — deep-in-market entries)
stake = $10 (notional)
fee   = 7.2% (POLYMARKET_CRYPTO_FEE_MULT)
win   pnl = (1 - entry) * stake * (1 - fee)
lose  pnl = -entry * stake

USAGE
-----
On the Hub box (has DATABASE_URL in hub container env):
  ssh ubuntu@16.54.141.121
  docker exec -e DATABASE_URL="$(docker exec hub printenv DATABASE_URL)" \
    hub python3 /tmp/config_honest_wr.py --hours 72

Or exporting manually:
  export DATABASE_URL="postgresql://..."
  python3 scripts/analysis/config_honest_wr.py --hours 72

Outputs a plain-text table to stdout. --out writes a JSON artefact too.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

try:
    import asyncpg  # type: ignore
except ImportError:
    print("ERROR: asyncpg not installed. Run inside hub container or `pip install asyncpg`.",
          file=sys.stderr)
    sys.exit(2)


# ─── Fees / caps (mirror engine/config/constants.py) ─────────────────────────
POLYMARKET_CRYPTO_FEE_MULT = 0.072  # 7.2%
ENTRY_PRICE_CAP = 0.85              # skip deep-in-market entries
STAKE_USD = 10.0                    # notional for sim P&L

# ─── v7.1 thresholds (mirror _calc_v71_retroactive_decision) ────────────────
V71_VPIN_GATE = 0.45
V71_MIN_DELTA_NORMAL = 0.0002     # 0.02% (fraction form in DB)
V71_MIN_DELTA_CASCADE = 0.0001    # 0.01%
V71_CASCADE_THRESHOLD = 0.65
V71_INFORMED_THRESHOLD = 0.55


@dataclass
class ConfigStats:
    name: str
    eligible: int = 0
    resolved: int = 0
    wins: int = 0
    losses: int = 0
    # Polymarket sim
    poly_eligible: int = 0           # has entry price in valid range and ≤ cap
    poly_wins: int = 0
    poly_losses: int = 0
    poly_pnl_usd: float = 0.0
    # direction choice distribution (among eligible with resolution)
    up_picked: int = 0
    down_picked: int = 0
    # Skips
    skipped: int = 0
    skip_reasons: dict = field(default_factory=dict)

    @property
    def directional_wr(self) -> Optional[float]:
        if self.resolved == 0:
            return None
        return self.wins / self.resolved

    @property
    def poly_wr(self) -> Optional[float]:
        n = self.poly_wins + self.poly_losses
        if n == 0:
            return None
        return self.poly_wins / n


def safe_float(x) -> Optional[float]:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def poly_sim_pnl(direction: Optional[str],
                 actual_direction: Optional[str],
                 gamma_up: Optional[float],
                 gamma_down: Optional[float]) -> tuple[Optional[bool], Optional[float], Optional[float]]:
    """Return (is_win, entry_price, pnl_usd). None entry = skip."""
    if not direction or not actual_direction:
        return (None, None, None)
    entry = gamma_up if direction == "UP" else gamma_down
    if entry is None or entry <= 0.005 or entry >= 0.995:
        return (None, None, None)
    if entry > ENTRY_PRICE_CAP:
        return (None, entry, None)
    win = direction == actual_direction
    if win:
        pnl = (1.0 - entry) * STAKE_USD * (1.0 - POLYMARKET_CRYPTO_FEE_MULT)
    else:
        pnl = -entry * STAKE_USD
    return (win, entry, round(pnl, 4))


def compute_v71(row: dict) -> tuple[bool, Optional[str], Optional[str]]:
    """Return (would_trade, direction, skip_reason). Mirrors _calc_v71_retroactive_decision."""
    vpin = safe_float(row.get("vpin"))
    delta_pct = safe_float(row.get("delta_pct"))
    direction = row.get("direction")

    if not direction or vpin is None or delta_pct is None:
        return (False, None, "insufficient_data")
    if vpin < V71_VPIN_GATE:
        return (False, None, f"vpin {vpin:.3f} < {V71_VPIN_GATE}")

    abs_delta = abs(delta_pct)
    if vpin >= V71_CASCADE_THRESHOLD:
        min_delta = V71_MIN_DELTA_CASCADE
    else:
        min_delta = V71_MIN_DELTA_NORMAL

    if abs_delta < min_delta:
        return (False, None, f"delta {abs_delta:.4f} < {min_delta}")
    return (True, direction, None)


def compute_v58(row: dict, actual_direction: Optional[str]) -> tuple[bool, Optional[str], Optional[str]]:
    """v5.8 = ML/TimesFM agrees with v5.7c direction AND no v5.7c skip.

    window_snapshots.timesfm_direction is 100% NULL in current schema, so we fall
    back to signal_evaluations.v2_direction (same ML-v2 model that drives the
    current gate pipeline). If neither present, skip.
    """
    direction = row.get("direction")
    ml_dir = row.get("timesfm_direction") or row.get("v2_direction")
    skip_reason = row.get("skip_reason")

    if not direction:
        return (False, None, "no_v57c")
    if not ml_dir:
        return (False, None, "no_ml_direction")
    if skip_reason:
        return (False, None, f"v57c_skip: {skip_reason[:60]}")
    if ml_dir != direction:
        return (False, None, f"disagree: ml={ml_dir} v57c={direction}")
    return (True, direction, None)


def compute_v57c(row: dict) -> tuple[bool, Optional[str], Optional[str]]:
    """v5.7c = whatever `direction` the engine chose — no additional gate."""
    direction = row.get("direction")
    if not direction:
        return (False, None, "no_direction")
    return (True, direction, None)


def actual_direction_for(row: dict) -> Optional[str]:
    # Prefer explicit column if populated
    ad = row.get("actual_direction")
    if ad in ("UP", "DOWN"):
        return ad
    # poly_winner (if present) can be YES/NO — map to UP/DOWN
    pw = row.get("poly_winner")
    if pw in ("YES", "UP"):
        return "UP"
    if pw in ("NO", "DOWN"):
        return "DOWN"
    # Fallback: Binance close vs open
    open_p = safe_float(row.get("open_price"))
    close_p = safe_float(row.get("close_price"))
    if open_p is not None and close_p is not None:
        return "UP" if close_p > open_p else "DOWN"
    return None


def tally_config(stats: ConfigStats,
                 row: dict,
                 eligible: bool,
                 direction: Optional[str],
                 skip_reason: Optional[str],
                 actual_direction: Optional[str],
                 gamma_up: Optional[float],
                 gamma_down: Optional[float]) -> None:
    if not eligible:
        stats.skipped += 1
        if skip_reason:
            key = skip_reason.split(":")[0].strip()[:40]
            stats.skip_reasons[key] = stats.skip_reasons.get(key, 0) + 1
        return

    stats.eligible += 1
    if direction == "UP":
        stats.up_picked += 1
    elif direction == "DOWN":
        stats.down_picked += 1

    if actual_direction is None:
        return
    stats.resolved += 1
    if direction == actual_direction:
        stats.wins += 1
    else:
        stats.losses += 1

    win, entry, pnl = poly_sim_pnl(direction, actual_direction, gamma_up, gamma_down)
    if win is None:
        return
    stats.poly_eligible += 1
    if pnl is not None:
        stats.poly_pnl_usd += pnl
    if win:
        stats.poly_wins += 1
    else:
        stats.poly_losses += 1


async def run(hours: int, configs: list[str], asset: str = "BTC", timeframe: str = "5m") -> dict:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL env var not set", file=sys.stderr)
        sys.exit(2)

    conn = await asyncpg.connect(db_url)
    try:
        # window_ts is bigint (unix seconds). Pass seconds-since-epoch cutoff directly.
        import time as _time
        cutoff_ts = int(_time.time()) - hours * 3600
        # NOTE: `trades` table does NOT carry window_ts/asset/timeframe/trade_direction,
        # so we can't reliably LEFT JOIN. Ground truth uses `actual_direction` column
        # if populated, else close-vs-open. The `poly_winner` column in window_snapshots
        # (if present) is also checked as a secondary truth source.
        # Dedup: one row per window — match /api/v58/outcomes semantics.
        # Prefer rows with eval_offset IS NULL (final snapshot) else highest offset.
        # Also pull the LAST non-null v2_direction from signal_evaluations as a
        # TimesFM/ML proxy for v5.8 agreement logic — window_snapshots.timesfm_direction
        # is 100% NULL in current schema.
        query = f"""
            WITH se AS (
                SELECT DISTINCT ON (window_ts, asset)
                    window_ts, asset,
                    v2_direction,
                    v2_probability_up,
                    v2_agrees
                FROM signal_evaluations
                WHERE asset = $1 AND timeframe = $2 AND window_ts >= $3
                  AND v2_direction IS NOT NULL
                ORDER BY window_ts, asset, eval_offset DESC
            )
            SELECT DISTINCT ON (ws.window_ts, ws.asset)
                ws.window_ts,
                ws.asset,
                ws.timeframe,
                ws.open_price,
                ws.close_price,
                ws.delta_pct,
                ws.vpin,
                ws.regime,
                ws.direction,
                ws.timesfm_direction,
                ws.twap_direction,
                ws.gamma_up_price,
                ws.gamma_down_price,
                ws.skip_reason,
                ws.trade_placed,
                ws.v71_would_trade,
                ws.v71_correct,
                ws.v71_pnl,
                ws.v71_skip_reason,
                ws.v71_regime,
                ws.engine_version,
                ws.actual_direction,
                ws.poly_winner,
                ws.poly_resolved_outcome,
                ws.oracle_outcome,
                ws.eval_offset,
                se.v2_direction,
                se.v2_probability_up,
                se.v2_agrees
            FROM window_snapshots ws
            LEFT JOIN se ON se.window_ts = ws.window_ts AND se.asset = ws.asset
            WHERE ws.asset = $1
              AND ws.timeframe = $2
              AND ws.window_ts >= $3
            ORDER BY ws.window_ts ASC, ws.asset, ws.eval_offset DESC NULLS LAST
        """
        rows = await conn.fetch(query, asset, timeframe, cutoff_ts)
    finally:
        await conn.close()

    all_rows = [dict(r) for r in rows]
    total_windows = len(all_rows)

    # Base rate across ALL rows with a resolvable actual_direction
    actuals = [actual_direction_for(r) for r in all_rows]
    actual_up = sum(1 for a in actuals if a == "UP")
    actual_down = sum(1 for a in actuals if a == "DOWN")
    actual_resolved = actual_up + actual_down

    stats = {name: ConfigStats(name=name) for name in configs}

    for row in all_rows:
        actual = actual_direction_for(row)
        gup = safe_float(row.get("gamma_up_price"))
        gdn = safe_float(row.get("gamma_down_price"))

        for name in configs:
            if name == "v5_7c":
                elig, d, sk = compute_v57c(row)
            elif name == "v5_8":
                elig, d, sk = compute_v58(row, actual)
            elif name == "v7_1":
                # Use DB value if backfilled, else compute
                db_would = row.get("v71_would_trade")
                if db_would is not None:
                    elig = bool(db_would)
                    d = row.get("direction") if elig else None
                    sk = None if elig else row.get("v71_skip_reason")
                else:
                    elig, d, sk = compute_v71(row)
            elif name == "current_engine":
                # whatever actually traded
                elig = bool(row.get("trade_placed"))
                d = row.get("direction") if elig else None
                sk = row.get("skip_reason") if not elig else None
            else:
                continue
            tally_config(stats[name], row, elig, d, sk, actual, gup, gdn)

    def pct(x: Optional[float]) -> str:
        return "  —  " if x is None else f"{x*100:5.1f}%"

    # Build results dict
    base_up_pct = actual_up / actual_resolved if actual_resolved else None

    results: dict = {
        "params": {
            "hours": hours,
            "asset": asset,
            "timeframe": timeframe,
            "configs": configs,
        },
        "total_windows": total_windows,
        "resolved_windows": actual_resolved,
        "base_rate": {
            "up": actual_up,
            "down": actual_down,
            "up_pct": base_up_pct,
        },
        "window_ts_min": all_rows[0]["window_ts"] if all_rows else None,
        "window_ts_max": all_rows[-1]["window_ts"] if all_rows else None,
        "configs": {},
    }

    for name, s in stats.items():
        dirwr = s.directional_wr
        polywr = s.poly_wr
        # edge vs appropriate base-rate (pick direction with higher count)
        chosen_base = None
        if base_up_pct is not None:
            total_picks = s.up_picked + s.down_picked
            if total_picks > 0:
                chosen_base = (s.up_picked * base_up_pct + s.down_picked * (1 - base_up_pct)) / total_picks
        edge = (dirwr - chosen_base) if (dirwr is not None and chosen_base is not None) else None

        results["configs"][name] = {
            "eligible": s.eligible,
            "skipped": s.skipped,
            "resolved": s.resolved,
            "wins": s.wins,
            "losses": s.losses,
            "directional_wr": dirwr,
            "up_picked": s.up_picked,
            "down_picked": s.down_picked,
            "base_rate_weighted": chosen_base,
            "edge_vs_base": edge,
            "poly_eligible": s.poly_eligible,
            "poly_wins": s.poly_wins,
            "poly_losses": s.poly_losses,
            "poly_wr": polywr,
            "poly_pnl_usd_sim": round(s.poly_pnl_usd, 2),
            "top_skip_reasons": sorted(s.skip_reasons.items(), key=lambda kv: -kv[1])[:5],
        }

    # Print table
    print()
    print(f"Honest config WR — last {hours}h, {asset}/{timeframe}")
    print(f"Total windows: {total_windows} | Resolved: {actual_resolved}"
          f" | UP {actual_up} ({pct(base_up_pct) if base_up_pct else '—'})"
          f" | DOWN {actual_down}")
    print("=" * 112)
    cols = ["config", "eligible", "resolved", "wins", "losses", "dir_wr",
            "up_pick", "dn_pick", "edge_vs_base", "poly_n", "poly_wr", "poly_pnl_$"]
    widths = [14, 9, 9, 6, 7, 8, 8, 8, 13, 7, 8, 11]
    header = "  ".join(c.ljust(w) for c, w in zip(cols, widths))
    print(header)
    print("-" * len(header))
    for name, s in stats.items():
        row_vals = [
            name,
            str(s.eligible),
            str(s.resolved),
            str(s.wins),
            str(s.losses),
            pct(s.directional_wr),
            str(s.up_picked),
            str(s.down_picked),
            pct(results["configs"][name]["edge_vs_base"]),
            str(s.poly_eligible),
            pct(s.poly_wr),
            f"{s.poly_pnl_usd:+.2f}",
        ]
        print("  ".join(v.ljust(w) for v, w in zip(row_vals, widths)))
    print()
    print("Skip reasons (top 3 per config):")
    for name, s in stats.items():
        top = sorted(s.skip_reasons.items(), key=lambda kv: -kv[1])[:3]
        print(f"  {name}: {top}")
    print()
    print("Notes:")
    print("  • 'dir_wr' = honest directional WR (config's chosen dir == actual_direction)")
    print("  • 'poly_wr' = WR after simulating entry at gamma price (skips entry > 0.85)")
    print("  • 'poly_pnl_$' = sum of simulated P&L at $10 stake, 7.2% fee")
    print("  • 'edge_vs_base' = dir_wr − direction-weighted base rate")

    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=72, help="Lookback window (hours)")
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--timeframe", default="5m")
    ap.add_argument("--config", default="v5_7c,v5_8,v7_1,current_engine",
                    help="Comma-separated config list")
    ap.add_argument("--out", default=None, help="Write JSON result to this path")
    args = ap.parse_args()

    configs = [c.strip() for c in args.config.split(",") if c.strip()]
    result = asyncio.run(run(args.hours, configs, asset=args.asset, timeframe=args.timeframe))

    if args.out:
        with open(args.out, "w") as f:
            json.dump(result, f, default=str, indent=2)
        print(f"Wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
