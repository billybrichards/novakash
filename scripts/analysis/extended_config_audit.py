#!/usr/bin/env python3
"""
Extended config audit — honest WR + Poly-sim P&L for ALL configs.

Loads the two parquet dumps produced by dump_window_snapshots.py:
  scripts/analysis/data/window_snapshots_<hours>h.parquet
  scripts/analysis/data/strategy_decisions_<hours>h.parquet

Configs covered:

LEGACY (retroactive on window_snapshots):
  v5_7c    — row.direction, no gate                                (mirrors config_honest_wr.py)
  v5_8     — v5.7c + ML agreement (v2_direction/timesfm_direction)
  v7_1     — VPIN gate 0.45 + delta thresholds (regime-aware)

LIVE (replay strategy_decisions.action on window_snapshots for outcomes):
  v4_down_only        — timing 90–150, DOWN, min_dist 0.10
  v4_up_asian         — timing 90–150, UP, Asian hrs 23–02 UTC
  v4_up_basic         — timing 60–180, UP, min_dist 0.15
  v4_fusion           — bespoke polymarket_v2 hook
  v4_fusion_v5_9      — polymarket_v2 hook variant
  v5_ensemble         — strategy layer ensemble
  v5_fresh            — fresh-signal layer
  v10_gate            — DUNE + 8-gate pipeline

NOT-IDENTIFIED (no distinct retroactive definition in current DB; see note):
  v5_0 / v5_7  — no separate engine_version or flag in window_snapshots.
                 v5.7c IS the closest retroactive approximation. We skip
                 them to avoid fabricating logic. Commented stubs left below.

OUTPUTS (for each config):
  eligible, wins, losses, directional_wr
  poly_sim_wr, poly_sim_pnl     (entry = gamma_<dir>_price OR clob_<dir>_ask
                                 if gamma missing; cap 0.85; $10 stake; 7.2% fee)
  edge vs direction-weighted base rate

CAVEATS (print in summary):
  • 72h sample — 862 BTC/5m windows, regime heavy (72.9% DOWN in prior run).
  • Live strategies use the engine's OWN action field — if a strategy chose
    SKIP, it is counted as skipped. Re-simulating gates retroactively is not
    attempted (too much gate-pipeline replay surface area).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import pandas as pd
except ImportError:
    print("ERROR: pandas required. pip install pandas pyarrow", file=sys.stderr)
    sys.exit(2)


# ─── Fees / sim constants (mirror engine/config/constants.py) ────────────────
POLYMARKET_CRYPTO_FEE_MULT = 0.072
ENTRY_PRICE_CAP = 0.85
STAKE_USD = 10.0
MIN_ENTRY = 0.005
MAX_ENTRY = 0.995

# ─── v7.1 thresholds ─────────────────────────────────────────────────────────
V71_VPIN_GATE = 0.45
V71_MIN_DELTA_NORMAL = 0.0002
V71_MIN_DELTA_CASCADE = 0.0001
V71_CASCADE_THRESHOLD = 0.65
V71_INFORMED_THRESHOLD = 0.55

LEGACY_CONFIGS = ("v5_7c", "v5_8", "v7_1")
LIVE_STRATEGIES = (
    "v4_down_only",
    "v4_up_asian",
    "v4_up_basic",
    "v4_fusion",
    "v4_fusion_v5_9",
    "v5_ensemble",
    "v5_fresh",
    "v10_gate",
)


@dataclass
class Stats:
    name: str
    eligible: int = 0
    resolved: int = 0
    wins: int = 0
    losses: int = 0
    up_picked: int = 0
    down_picked: int = 0
    poly_eligible: int = 0
    poly_wins: int = 0
    poly_losses: int = 0
    poly_pnl_usd: float = 0.0
    skipped: int = 0
    skip_reasons: dict = field(default_factory=dict)

    @property
    def directional_wr(self) -> Optional[float]:
        return self.wins / self.resolved if self.resolved else None

    @property
    def poly_wr(self) -> Optional[float]:
        n = self.poly_wins + self.poly_losses
        return self.poly_wins / n if n else None


def safe_float(x) -> Optional[float]:
    try:
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def actual_direction(row: dict) -> Optional[str]:
    ad = row.get("actual_direction")
    if ad in ("UP", "DOWN"):
        return ad
    pw = row.get("poly_winner")
    if pw in ("YES", "UP"):
        return "UP"
    if pw in ("NO", "DOWN"):
        return "DOWN"
    op, cp = safe_float(row.get("open_price")), safe_float(row.get("close_price"))
    if op is not None and cp is not None:
        return "UP" if cp > op else "DOWN"
    return None


def entry_price(row: dict, direction: str) -> Optional[float]:
    """Prefer gamma (liquid side), fall back to clob ask on chosen side."""
    if direction == "UP":
        for k in ("gamma_up_price", "clob_up_ask"):
            v = safe_float(row.get(k))
            if v is not None and MIN_ENTRY < v < MAX_ENTRY:
                return v
    else:
        for k in ("gamma_down_price", "clob_down_ask"):
            v = safe_float(row.get(k))
            if v is not None and MIN_ENTRY < v < MAX_ENTRY:
                return v
    return None


def poly_sim(direction: Optional[str], actual: Optional[str],
             row: dict) -> tuple[Optional[bool], Optional[float], Optional[float]]:
    if not direction or not actual:
        return (None, None, None)
    e = entry_price(row, direction)
    if e is None:
        return (None, None, None)
    if e > ENTRY_PRICE_CAP:
        return (None, e, None)
    win = direction == actual
    pnl = (1.0 - e) * STAKE_USD * (1.0 - POLYMARKET_CRYPTO_FEE_MULT) if win else -e * STAKE_USD
    return (win, e, round(pnl, 4))


# ─── Legacy config simulators ───────────────────────────────────────────────
def compute_v57c(row: dict) -> tuple[bool, Optional[str], Optional[str]]:
    d = row.get("direction")
    return ((True, d, None) if d else (False, None, "no_direction"))


def compute_v58(row: dict) -> tuple[bool, Optional[str], Optional[str]]:
    """v5.8 = v5.7c direction ∧ ML-v2 direction agree.

    Deliberately ignores window_snapshots.skip_reason (populated on most v8.0
    windows for unrelated reasons like 'gates passed but signal None') —
    legacy v5.8 only cared about direction + ML agreement.
    """
    d = row.get("direction")
    ml = row.get("se_v2_direction") or row.get("v2_direction") or row.get("timesfm_direction")
    if not d:
        return (False, None, "no_v57c")
    if not ml:
        return (False, None, "no_ml_direction")
    if ml != d:
        return (False, None, f"disagree:ml={ml}/v57c={d}")
    return (True, d, None)


def compute_v71(row: dict) -> tuple[bool, Optional[str], Optional[str]]:
    # prefer DB v71_would_trade if populated
    db = row.get("v71_would_trade")
    if db is not None and not (isinstance(db, float) and pd.isna(db)):
        if bool(db):
            return (True, row.get("direction"), None)
        return (False, None, row.get("v71_skip_reason") or "v71_skip")
    vpin = safe_float(row.get("vpin"))
    delta = safe_float(row.get("delta_pct"))
    d = row.get("direction")
    if not d or vpin is None or delta is None:
        return (False, None, "insufficient_data")
    if vpin < V71_VPIN_GATE:
        return (False, None, f"vpin<{V71_VPIN_GATE}")
    abs_d = abs(delta)
    min_delta = V71_MIN_DELTA_CASCADE if vpin >= V71_CASCADE_THRESHOLD else V71_MIN_DELTA_NORMAL
    if abs_d < min_delta:
        return (False, None, f"delta<{min_delta}")
    return (True, d, None)


def tally(stats: Stats, row: dict, elig: bool, direction: Optional[str],
          skip_reason: Optional[str], actual: Optional[str]) -> None:
    if not elig:
        stats.skipped += 1
        if skip_reason:
            k = str(skip_reason).split(":", 1)[0].strip()[:40]
            stats.skip_reasons[k] = stats.skip_reasons.get(k, 0) + 1
        return

    stats.eligible += 1
    if direction == "UP":
        stats.up_picked += 1
    elif direction == "DOWN":
        stats.down_picked += 1

    if actual is None:
        return
    stats.resolved += 1
    if direction == actual:
        stats.wins += 1
    else:
        stats.losses += 1

    win, entry, pnl = poly_sim(direction, actual, row)
    if win is None:
        return
    stats.poly_eligible += 1
    if pnl is not None:
        stats.poly_pnl_usd += pnl
    if win:
        stats.poly_wins += 1
    else:
        stats.poly_losses += 1


def run(ws_path: Path, sd_path: Path) -> dict:
    ws_df = pd.read_parquet(ws_path)
    sd_df = pd.read_parquet(sd_path)

    # Index strategy_decisions by (strategy_id, window_ts). When both a TRADE
    # and a SKIP row exist for the same window (depends on dump pattern), prefer
    # the TRADE row — that's the decision that would have fired.
    sd_df["asset_u"] = sd_df["asset"].str.upper()
    sd_df = sd_df.sort_values(["strategy_id", "window_ts", "action"],
                              ascending=[True, True, True])  # TRADE before SKIP
    sd_idx: dict[str, dict[int, dict]] = {}
    for sid, grp in sd_df.groupby("strategy_id"):
        sd_idx[sid] = {}
        for _, r in grp.iterrows():
            ts = int(r["window_ts"])
            # only overwrite with a TRADE row (don't let a SKIP clobber a TRADE)
            if ts in sd_idx[sid] and sd_idx[sid][ts].get("action") == "TRADE":
                continue
            sd_idx[sid][ts] = dict(r)

    ws_rows = ws_df.to_dict("records")

    # Base rate
    actuals = [actual_direction(r) for r in ws_rows]
    up = sum(1 for a in actuals if a == "UP")
    down = sum(1 for a in actuals if a == "DOWN")
    resolved = up + down
    base_up_pct = up / resolved if resolved else None

    stats = {name: Stats(name=name) for name in (list(LEGACY_CONFIGS) + ["current_engine"] + list(LIVE_STRATEGIES))}

    for row in ws_rows:
        a = actual_direction(row)

        # Legacy
        for name, fn in [("v5_7c", compute_v57c), ("v5_8", compute_v58), ("v7_1", compute_v71)]:
            elig, d, sk = fn(row)
            tally(stats[name], row, elig, d, sk, a)

        # current_engine = whatever was actually placed
        placed = bool(row.get("trade_placed"))
        tally(stats["current_engine"], row,
              placed,
              row.get("direction") if placed else None,
              None if placed else row.get("skip_reason"),
              a)

        # Live strategies — use strategy_decisions
        ts = int(row["window_ts"])
        for sid in LIVE_STRATEGIES:
            sd = sd_idx.get(sid, {}).get(ts)
            if sd is None:
                stats[sid].skipped += 1
                stats[sid].skip_reasons["no_decision_row"] = stats[sid].skip_reasons.get("no_decision_row", 0) + 1
                continue
            action = sd.get("action")
            direction = sd.get("direction")
            if action == "TRADE" and direction in ("UP", "DOWN"):
                tally(stats[sid], row, True, direction, None, a)
            else:
                tally(stats[sid], row, False, None, sd.get("skip_reason") or action, a)

    # Build results
    def pct(x): return "  —  " if x is None else f"{x*100:5.1f}%"

    results = {
        "window_count": len(ws_rows),
        "resolved": resolved,
        "base_rate": {"up": up, "down": down, "up_pct": base_up_pct},
        "window_ts_min": int(ws_df["window_ts"].min()) if len(ws_df) else None,
        "window_ts_max": int(ws_df["window_ts"].max()) if len(ws_df) else None,
        "configs": {},
    }

    for name, s in stats.items():
        dirwr = s.directional_wr
        polywr = s.poly_wr
        chosen_base = None
        if base_up_pct is not None:
            tp = s.up_picked + s.down_picked
            if tp:
                chosen_base = (s.up_picked * base_up_pct + s.down_picked * (1 - base_up_pct)) / tp
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

    # Print
    print()
    print(f"Extended config audit — {len(ws_rows)} windows | Resolved {resolved} "
          f"| UP {up} ({pct(base_up_pct) if base_up_pct else '—'}) | DOWN {down}")
    print("=" * 120)
    cols = ["config", "eligible", "resolved", "wins", "losses", "dir_wr",
            "up_pick", "dn_pick", "edge", "poly_n", "poly_wr", "poly_pnl_$"]
    widths = [18, 9, 9, 6, 7, 8, 8, 8, 9, 7, 8, 11]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("-" * 120)

    # Sort by poly_pnl_usd_sim desc for the display
    sorted_stats = sorted(stats.items(),
                          key=lambda kv: -kv[1].poly_pnl_usd)
    for name, s in sorted_stats:
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
    print("Notes:")
    print("  • dir_wr = honest directional WR (chosen dir == actual_direction)")
    print("  • poly_wr = WR after sim entry (gamma or clob_ask fallback; cap 0.85)")
    print("  • poly_pnl_$ = $10 stake, 7.2% fee, capped entries")
    print("  • edge = dir_wr − direction-weighted base-rate (72.9% DOWN is typical here)")
    print("  • Live strategies replay the engine's own TRADE/SKIP decisions — no re-simulation.")
    print("  • v5.0 / v5.7 not identified as distinct retroactive configs (see docstring).")

    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", default=None, help="Path to window_snapshots parquet")
    ap.add_argument("--sd", default=None, help="Path to strategy_decisions parquet")
    ap.add_argument("--hours", type=int, default=72, help="(used to derive default paths)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    data_dir = repo_root / "scripts" / "analysis" / "data"
    ws = Path(args.ws) if args.ws else data_dir / f"window_snapshots_{args.hours}h.parquet"
    sd = Path(args.sd) if args.sd else data_dir / f"strategy_decisions_{args.hours}h.parquet"

    if not ws.exists():
        print(f"ERROR: missing {ws} — run dump_window_snapshots.py first", file=sys.stderr)
        sys.exit(2)
    if not sd.exists():
        print(f"ERROR: missing {sd} — run dump_window_snapshots.py first", file=sys.stderr)
        sys.exit(2)

    res = run(ws, sd)
    if args.out:
        Path(args.out).write_text(json.dumps(res, default=str, indent=2))
        print(f"\nWrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
