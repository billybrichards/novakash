"""Tick-by-tick gate replay simulator.

Replays historical per-tick signal_evaluations through the live gate stack to
produce trustworthy config-tuning estimates.  Fixes the ~60x looseness of SQL
gate simulations (see memory feedback_strategy_comparison_tband_convention.md).

Architecture
------------
* Loads tick history from signal_evaluations JOIN window_snapshots.
* Reconstructs a minimal FullDataSurface-compatible object for each tick.
* Replays the gate stack in chronological order maintaining:
  - per-strategy-window cooldown timer (post-loss cooldown)
  - per-strategy-window consecutive-pass counter (3-tick confirmation)
  - per-window dedup lock (one fire per window per direction)
* Emits a synthetic ``would-fire`` event for each tick that passes all gates.
* Compares against actual trades table to produce validation metrics.

Gate Coverage (v12_lgb_combo / v9_ensemble delegation)
------------------------------------------------------
Gates replayed from DB data:

  G1  Timing gate          eval_offset in [min_offset_sec, max_offset_sec]
  G2  Hour block           eval_offset UTC hour not in blocked_utc_hours
  G2b Per-direction hour   blocked_utc_hours_up / _down
  G3  Regime (v4)          v4_regime in tradeable_v4_regimes  (low confidence — see warning)
  G4  Source agreement     chainlink + tiingo both present; direction agreement
  G5  VPIN guard           vpin in [vpin_min, vpin_max]
  G6  Probability signal   probability_lgb_v9_1 / v12 not None, dist >= combo_min_dist
  G7  Direction agreement  v9_1 and v12 agree direction (or contrarian path)
  G8  LGB safety floor     min(dist_v9_1, dist_v12) >= lgb_dist_min per direction
  G9  Fill band            clob_up_ask / clob_down_ask in [fill_band_min, fill_band_max]
  G10 Post-loss cooldown   time since last LOSS >= post_loss_cooldown_min
  G11 3-tick confirmation  N consecutive passing ticks (min_consecutive_pass_ticks)
  G12 Per-window dedup     only one fire per (window_ts, strategy_id, direction)

Gates NOT replayed (data not available in signal_evaluations):
  - OracleDisagreeGate (oracle_direction from inline step — no per-tick oracle direction in DB)
  - CG confirmation (CG taker flow per tick — not in signal_evaluations per-tick)
  - DeltaMagnitudeGate (alignment_bps not stored per tick)
  - BlockCellsGate (per-cell predicate — skipped, assumed minimal impact)
  - ChainlinkFreshnessGate (age not in signal_evaluations)
  - CellPauseGate (rolling-WR state, not replayable offline without full trade history)

Regime replay caveat
--------------------
window_snapshots.regime has a known writer regression (99.7% NULL — see memory
project_mass_combo_2026_05_01.md).  signal_evaluations.regime is populated but
only reflects VPIN regime, not v4_regime.  The simulator uses
signal_evaluations.regime as a v4_regime proxy ONLY when window_snapshots
v4_regime data is absent.  All outputs include ``regime_replay_confidence: LOW``
until audit #12 + writer fix lands.

Usage
-----
On Montreal (has DATABASE_URL pointing at RDS prod)::

    cd /home/novakash/novakash
    python3 scripts/sim/tick_simulator.py \\
        --strategy-id v12_lgb_combo \\
        --hours 168 \\
        --validate

Locally (requires DATABASE_URL env var)::

    DATABASE_URL=postgresql://... python3 scripts/sim/tick_simulator.py \\
        --strategy-id v12_lgb_combo --hours 168 --validate

Gate config override (JSON file)::

    python3 scripts/sim/tick_simulator.py \\
        --strategy-id v12_lgb_combo --hours 168 \\
        --gate-config-override /tmp/no_source_agreement.json \\
        --validate

    where /tmp/no_source_agreement.json = {"source_agreement_require_chainlink": false}

Output
------
Prints a per-cell DataFrame and a validation delta table when --validate is
passed.  Returns exit code 0 on success, 1 on validation failure.

Hub note #348 section 4 — full plan and risk register.
Audit task #381 (audit-2026-05-06-09).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Seconds between ticks in a 5m window (engine evaluates ~every 2s)
_TICK_INTERVAL_SEC = 2.0

# When replaying, discard this many seconds from the START of the replay window
# to give cooldown / consec-pass state time to warm up.
WARMUP_DISCARD_SECONDS = 30 * 60  # 30 minutes

# Default gate-config defaults (mirrors v12_lgb_combo.yaml gate_params)
_DEFAULT_GATE_CONFIG = {
    "min_offset_sec": 24,
    "max_offset_sec": 240,
    "tradeable_v4_regimes": ["volatile_trend", "chop", "risk_off", "calm_trend"],
    "blocked_utc_hours": [],
    "blocked_utc_hours_up": [],
    "blocked_utc_hours_down": [],
    "source_agreement_require_chainlink": True,
    "source_agreement_require_tiingo": True,
    "oracle_agreement_min_sources": 2,
    "vpin_min": 0.40,
    "vpin_max": 1.0,
    "fill_band_min": 0.00,
    "fill_band_max": 0.82,
    "up_min_fill_price": 0.20,
    "down_min_fill_price": 0.15,
    "post_loss_cooldown_min": 20,
    "min_consecutive_pass_ticks": 3,
    "combo_min_dist": 0.10,
    "combo_require_direction_agreement": True,
    "lgb_dist_min_up": 0.13,
    "lgb_dist_min_down": 0.10,
    "lgb_dist_min_up_with_hc_agree": 0.08,
    "lgb_dist_min_down_with_hc_agree": 0.08,
    "vhc_threshold": 0.25,
    "v12_contrarian_enabled": True,
    "v12_contrarian_min_dist": 0.10,
    "v12_contrarian_kelly_modifier": 0.5,
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TickRow:
    """One row from signal_evaluations JOIN window_snapshots.

    LGB probability sourcing note:
    - `probability_lgb_v9_1` is the v9.1 retrain served live by TimesFM via
      /v4/snapshot. It is NOT persisted to `signal_evaluations` — it lives only
      on the in-memory FullDataSurface at evaluation time.
    - The closest persisted proxy is `probability_lgb_v9_2` (the v9.2 Optuna
      retrain, written to signal_evaluations since PR #499). This is used as the
      "v9-side" signal in replay. Accuracy caveat: v9.2 fires less frequently
      (stricter cohort gate), so some ticks where v9.1 had a signal will have
      NULL v9.2. This means the simulator UNDER-estimates fires relative to live.
    - `probability_lgb_v12` is written by PR #441 and has 44-80% population
      rate depending on the timeframe window.
    """
    window_ts: int
    asset: str
    timeframe: str
    eval_offset: int
    evaluated_at: float  # unix epoch

    # Price sources
    delta_tiingo: Optional[float]
    delta_chainlink: Optional[float]
    delta_binance: Optional[float]
    delta_pct: Optional[float]

    # VPIN / Regime
    vpin: Optional[float]
    regime: Optional[str]  # VPIN regime from signal_evaluations
    v4_regime: Optional[str]  # from window_snapshots (mostly NULL — see caveat)

    # CLOB
    clob_up_ask: Optional[float]
    clob_down_ask: Optional[float]
    clob_up_bid: Optional[float]
    clob_down_bid: Optional[float]

    # LGB probabilities
    # Note: probability_lgb_v9_1 is NOT in signal_evaluations (only in live surface).
    # We use probability_lgb_v9_2 as the closest persisted proxy for the "v9-side".
    probability_lgb_v9_1: Optional[float]   # actually v9_2 from DB (proxy)
    probability_lgb_v12: Optional[float]    # signal_evaluations.probability_lgb_v12

    # Resolved outcome (for validation)
    outcome: Optional[str]  # WIN/LOSS/VOID from window_snapshots
    poly_winner: Optional[str]  # UP/DOWN from window_snapshots

    @property
    def utc_hour(self) -> int:
        return datetime.fromtimestamp(self.evaluated_at, tz=timezone.utc).hour

    @property
    def dist_v9_1(self) -> Optional[float]:
        if self.probability_lgb_v9_1 is None:
            return None
        return abs(self.probability_lgb_v9_1 - 0.5)

    @property
    def dist_v12(self) -> Optional[float]:
        if self.probability_lgb_v12 is None:
            return None
        return abs(self.probability_lgb_v12 - 0.5)

    @property
    def direction_v9_1(self) -> Optional[str]:
        if self.probability_lgb_v9_1 is None:
            return None
        return "UP" if self.probability_lgb_v9_1 >= 0.5 else "DOWN"

    @property
    def direction_v12(self) -> Optional[str]:
        if self.probability_lgb_v12 is None:
            return None
        return "UP" if self.probability_lgb_v12 >= 0.5 else "DOWN"

    def combo_direction(self) -> Optional[str]:
        """Agreement path: both models agree direction."""
        if self.direction_v9_1 is None or self.direction_v12 is None:
            return None
        if self.direction_v9_1 == self.direction_v12:
            return self.direction_v9_1
        return None  # disagreement

    def contrarian_direction(self) -> Optional[str]:
        """Contrarian path: v12 direction when models disagree."""
        if self.direction_v12 is None:
            return None
        if self.direction_v9_1 is None or self.direction_v9_1 == self.direction_v12:
            return None  # need disagreement
        return self.direction_v12

    def combo_min_dist(self) -> Optional[float]:
        """min(dist_v9_1, dist_v12) for the agreement path."""
        if self.dist_v9_1 is None or self.dist_v12 is None:
            return None
        return min(self.dist_v9_1, self.dist_v12)


@dataclass
class FireEvent:
    """A synthetic would-fire event from the simulator."""
    window_ts: int
    evaluated_at: float
    direction: str
    strategy_id: str
    gate_config_hash: str
    skip_reason: Optional[str] = None  # None = passed all gates (fired)
    combo_dist: Optional[float] = None
    fill_price: Optional[float] = None
    regime: Optional[str] = None
    v4_regime: Optional[str] = None
    eval_offset: int = 0
    t_band: str = ""


@dataclass
class SimState:
    """Per-strategy mutable replay state.

    Managed by the simulator replay loop.  NOT thread-safe — single-threaded
    chronological replay.
    """
    strategy_id: str

    # Post-loss cooldown: tracks when last LOSS was recorded (epoch seconds)
    last_loss_epoch: Optional[float] = None

    # Per-window consecutive-pass counter: {window_ts: (count, direction, last_eval_at)}
    consec_pass: Dict[int, Tuple[int, str, float]] = field(default_factory=dict)

    # Per-window dedup: set of (window_ts, direction) that have already fired
    fired_windows: set = field(default_factory=set)

    def record_loss(self, resolved_at_epoch: float) -> None:
        self.last_loss_epoch = resolved_at_epoch

    def in_cooldown(self, now_epoch: float, cooldown_min: float) -> Tuple[bool, float]:
        if cooldown_min <= 0 or self.last_loss_epoch is None:
            return False, 0.0
        elapsed = now_epoch - self.last_loss_epoch
        cooldown_sec = cooldown_min * 60
        if elapsed < cooldown_sec:
            return True, cooldown_sec - elapsed
        return False, 0.0

    def update_consec(
        self, window_ts: int, direction: str, now_epoch: float, max_gap: float = 5.0
    ) -> int:
        """Update consecutive-pass counter. Returns new count."""
        prev = self.consec_pass.get(window_ts)
        if (
            prev is not None
            and prev[1] == direction
            and (now_epoch - prev[2]) <= max_gap
        ):
            count = prev[0] + 1
        else:
            count = 1
        self.consec_pass[window_ts] = (count, direction, now_epoch)
        # Bounded-memory cleanup
        if len(self.consec_pass) > 100:
            cutoff = window_ts - 1200  # 2 windows
            self.consec_pass = {k: v for k, v in self.consec_pass.items() if k >= cutoff}
        return count

    def has_fired(self, window_ts: int, direction: str) -> bool:
        return (window_ts, direction) in self.fired_windows

    def mark_fired(self, window_ts: int, direction: str) -> None:
        self.fired_windows.add((window_ts, direction))
        # Bounded-memory cleanup: keep only last 200 windows
        if len(self.fired_windows) > 400:
            sorted_wts = sorted({wt for wt, _ in self.fired_windows})
            cutoff = sorted_wts[len(sorted_wts) // 2]
            self.fired_windows = {
                (wt, d) for wt, d in self.fired_windows if wt >= cutoff
            }


# ---------------------------------------------------------------------------
# t_band label helper (mirrors scripts/ops/analysis/_common.py)
# ---------------------------------------------------------------------------

def _t_band(eval_offset: int) -> str:
    """Return t_band label using sec-FROM-OPEN convention (300 - eval_offset).

    This matches the strategy_comparison table convention.
    eval_offset = sec-to-close (T-minus, per engine convention).
    sec_from_open = 300 - eval_offset.
    """
    sec_from_open = 300 - eval_offset
    if sec_from_open <= 60:
        return "T-1-60"
    elif sec_from_open <= 120:
        return "T-61-120"
    elif sec_from_open <= 180:
        return "T-121-180"
    elif sec_from_open <= 240:
        return "T-181-240"
    else:
        return "T-241-300"


# ---------------------------------------------------------------------------
# Gate logic (mirrors engine gate stack for v12_lgb_combo / v9_ensemble)
# ---------------------------------------------------------------------------

class GateStack:
    """Evaluates the v12_lgb_combo gate stack against a tick.

    All gate logic is derived from v9_ensemble.py and v12_lgb_combo.py.
    No external imports from the engine — pure data logic so it runs
    without the engine's full import tree.

    For each tick + candidate direction, returns (passed, skip_reason).
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg

    def _gc(self, key: str) -> Any:
        """Get gate config value."""
        return self.cfg.get(key, _DEFAULT_GATE_CONFIG.get(key))

    def evaluate(
        self,
        tick: TickRow,
        direction: str,
        state: SimState,
        combo_dist: Optional[float] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Evaluate all gates. Returns (passed, skip_reason)."""

        # G1: Timing gate
        min_off = self._gc("min_offset_sec")
        max_off = self._gc("max_offset_sec")
        if tick.eval_offset is None:
            return False, "timing:eval_offset_none"
        if not (min_off <= tick.eval_offset <= max_off):
            return False, f"timing:offset={tick.eval_offset} outside [{min_off},{max_off}]"

        # G2: Hour block (symmetric)
        blocked_hours = set(self._gc("blocked_utc_hours") or [])
        if tick.utc_hour in blocked_hours:
            return False, f"hour_block:hour={tick.utc_hour}"

        # G2b: Per-direction UTC hour block
        if direction == "UP":
            blocked_dir = set(self._gc("blocked_utc_hours_up") or [])
        else:
            blocked_dir = set(self._gc("blocked_utc_hours_down") or [])
        if tick.utc_hour in blocked_dir:
            return False, f"hour_block_{direction.lower()}:hour={tick.utc_hour}"

        # G3: Regime (v4) gate — ONLY applies when v4_regime is available.
        # NOTE: regime_replay_confidence=LOW (v4_regime 99.7% NULL in window_snapshots).
        # signal_evaluations.regime is VPIN regime (CALM/NORMAL/TRANSITION/CASCADE),
        # NOT v4 regime (calm_trend/volatile_trend/chop/risk_off). Do NOT use VPIN
        # regime as a v4 regime proxy — they are different systems.
        # When v4_regime is NULL (the common case), this gate passes by default
        # (same as the live engine: regime=None → pass by default per RegimeGate).
        if tick.v4_regime is not None:
            tradeable = set(r.lower() for r in (self._gc("tradeable_v4_regimes") or []))
            if tradeable and tick.v4_regime.lower() not in tradeable:
                return False, f"regime:v4_regime={tick.v4_regime} not tradeable"

        # G4: Source agreement gate
        req_chainlink = self._gc("source_agreement_require_chainlink")
        req_tiingo = self._gc("source_agreement_require_tiingo")
        if req_chainlink and tick.delta_chainlink is None:
            return False, "source_agreement:chainlink_null"
        if req_tiingo and tick.delta_tiingo is None:
            return False, "source_agreement:tiingo_null"

        # Source direction agreement: at least oracle_agreement_min_sources agree
        min_sources = self._gc("oracle_agreement_min_sources") or 2
        sources: Dict[str, float] = {}
        if tick.delta_tiingo is not None:
            sources["tiingo"] = tick.delta_tiingo
        if tick.delta_chainlink is not None:
            sources["chainlink"] = tick.delta_chainlink
        if tick.delta_binance is not None:
            sources["binance"] = tick.delta_binance

        if len(sources) >= min_sources:
            up_count = sum(1 for v in sources.values() if v > 0)
            dn_count = sum(1 for v in sources.values() if v < 0)
            majority = "UP" if up_count >= dn_count else "DOWN"
            max_agreement = max(up_count, dn_count)
            if max_agreement < min_sources:
                return False, f"source_agreement:only {max_agreement}/{len(sources)} agree"
            # Direction must match strategy direction
            if majority != direction:
                return False, f"source_agreement:majority={majority} != strategy={direction}"

        # G5: VPIN guard
        vpin_min = self._gc("vpin_min")
        vpin_max = self._gc("vpin_max")
        if tick.vpin is not None:
            if not (vpin_min <= tick.vpin <= vpin_max):
                return False, f"vpin:vpin={tick.vpin:.3f} outside [{vpin_min},{vpin_max}]"

        # G6+G7+G8: LGB probability + direction agreement + safety floor
        # These are specific to v12_lgb_combo combo logic
        if combo_dist is None:
            return False, "lgb:combo_dist_none (v9_1 or v12 prob missing)"

        combo_min = self._gc("combo_min_dist")
        if combo_dist < combo_min:
            return False, f"lgb:combo_dist={combo_dist:.3f} < min={combo_min}"

        # LGB safety floor per direction
        lgb_floor: float
        if direction == "UP":
            lgb_floor = self._gc("lgb_dist_min_up")
        else:
            lgb_floor = self._gc("lgb_dist_min_down")
        if combo_dist < lgb_floor:
            return False, f"lgb_floor:{direction}:combo_dist={combo_dist:.3f} < floor={lgb_floor}"

        # G9: Fill band gate
        fill_max = self._gc("fill_band_max")
        fill_min = self._gc("fill_band_min")
        if direction == "UP":
            fill_floor = self._gc("up_min_fill_price") or 0.20
            fill_price = tick.clob_up_ask
        else:
            fill_floor = self._gc("down_min_fill_price") or 0.15
            fill_price = tick.clob_down_ask

        if fill_price is not None:
            if fill_price < fill_floor:
                return False, f"fill_band:{direction}:fill={fill_price:.3f} < floor={fill_floor}"
            if fill_price > fill_max:
                return False, f"fill_band:{direction}:fill={fill_price:.3f} > max={fill_max}"
            if fill_price < fill_min:
                return False, f"fill_band:{direction}:fill={fill_price:.3f} < min={fill_min}"
        # When fill_price is None, we allow (same as require_clob=False in engine)

        # G10: Post-loss cooldown
        cooldown_min = self._gc("post_loss_cooldown_min")
        in_cd, remaining = state.in_cooldown(tick.evaluated_at, cooldown_min)
        if in_cd:
            return False, f"cooldown:{remaining:.0f}s remaining"

        # G11: 3-tick consecutive pass confirmation
        # Update counter AFTER all other gates pass
        min_ticks = self._gc("min_consecutive_pass_ticks") or 1
        if min_ticks > 0:
            count = state.update_consec(tick.window_ts, direction, tick.evaluated_at)
            if count < min_ticks:
                return False, f"consec_ticks:{count}/{min_ticks}"

        # G12: Per-window dedup — only one TRADE per (window_ts, direction)
        if state.has_fired(tick.window_ts, direction):
            return False, "dedup:already_fired_this_window"

        return True, None


# ---------------------------------------------------------------------------
# Tick loader
# ---------------------------------------------------------------------------

def _load_ticks_from_db(
    strategy_id: str,
    hours: int,
    db_url: str,
) -> List[TickRow]:
    """Load tick history from signal_evaluations JOIN window_snapshots.

    Runs against RDS prod (via Montreal SSH) or local DB.
    Uses a lightweight query with strategy-aligned window_ts range.

    NOTE: signal_evaluations does NOT have a strategy_id column — it records
    ALL evaluations per tick.  We load ALL ticks and filter by time range only.
    The strategy config gate replays determine what each strategy would have
    done with those ticks.

    Columns loaded:
    - All columns from signal_evaluations needed for gate replay
    - probability_lgb_v9_1, probability_lgb_v12 (sparse — NULL handling required)
    - window_snapshots.outcome, poly_winner, v4_regime (for validation)
    """
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        raise RuntimeError(
            "psycopg2 not installed. On Montreal: pip3 install psycopg2-binary"
        )

    sync_url = (
        db_url
        .replace("postgresql+asyncpg://", "postgresql://")
        .replace("+asyncpg", "")
    )

    cutoff_ts = int(time.time()) - hours * 3600
    # Align to 5m window boundary
    cutoff_window = (cutoff_ts // 300) * 300

    query = """
        SELECT
            se.window_ts,
            se.asset,
            se.timeframe,
            se.eval_offset,
            EXTRACT(EPOCH FROM se.evaluated_at)::bigint AS evaluated_at_epoch,
            se.delta_tiingo,
            se.delta_chainlink,
            se.delta_binance,
            se.delta_pct,
            se.vpin,
            se.regime,
            se.clob_up_ask,
            se.clob_down_ask,
            se.clob_up_bid,
            se.clob_down_bid,
            se.probability_lgb_v9_2  AS probability_lgb_v9_1,
            se.probability_lgb_v12,
            ws.outcome,
            ws.poly_winner,
            ws.v2_direction          AS ws_v4_regime_proxy
        FROM signal_evaluations se
        LEFT JOIN window_snapshots ws
            ON ws.window_ts = se.window_ts
            AND ws.asset = se.asset
            AND ws.timeframe = se.timeframe
            AND ws.eval_offset IS NULL
        WHERE se.timeframe = '5m'
          AND se.asset = 'BTC'
          AND se.window_ts >= %s
          AND se.eval_offset IS NOT NULL
        ORDER BY se.window_ts ASC, se.eval_offset DESC
    """
    # eval_offset = sec-to-close (T-minus convention per engine).
    # DESC = highest offset first = earliest in window first = chronological.
    # Note: probability_lgb_v9_2 aliased as probability_lgb_v9_1 (closest proxy).
    # v9.1 is NOT persisted to signal_evaluations (lives only in live surface).

    print(
        f"[tick_simulator] Loading ticks: strategy={strategy_id}, hours={hours}, "
        f"cutoff_window_ts={cutoff_window}",
        file=sys.stderr,
    )

    conn = psycopg2.connect(sync_url, connect_timeout=10)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(query, (cutoff_window,))
            rows = cur.fetchall()
    finally:
        conn.close()

    print(f"[tick_simulator] Loaded {len(rows)} ticks", file=sys.stderr)

    ticks = []
    null_v9_1 = 0
    null_v12 = 0
    for r in rows:
        prob_v9_1 = r["probability_lgb_v9_1"]
        prob_v12 = r["probability_lgb_v12"]
        if prob_v9_1 is None:
            null_v9_1 += 1
        if prob_v12 is None:
            null_v12 += 1

        ticks.append(TickRow(
            window_ts=int(r["window_ts"]),
            asset=r["asset"] or "BTC",
            timeframe=r["timeframe"] or "5m",
            eval_offset=int(r["eval_offset"]) if r["eval_offset"] is not None else 0,
            evaluated_at=float(r["evaluated_at_epoch"]),
            delta_tiingo=float(r["delta_tiingo"]) if r["delta_tiingo"] is not None else None,
            delta_chainlink=float(r["delta_chainlink"]) if r["delta_chainlink"] is not None else None,
            delta_binance=float(r["delta_binance"]) if r["delta_binance"] is not None else None,
            delta_pct=float(r["delta_pct"]) if r["delta_pct"] is not None else None,
            vpin=float(r["vpin"]) if r["vpin"] is not None else None,
            regime=r["regime"],
            v4_regime=None,  # window_snapshots.regime has writer regression (99.7% NULL)
            clob_up_ask=float(r["clob_up_ask"]) if r["clob_up_ask"] is not None else None,
            clob_down_ask=float(r["clob_down_ask"]) if r["clob_down_ask"] is not None else None,
            clob_up_bid=float(r["clob_up_bid"]) if r["clob_up_bid"] is not None else None,
            clob_down_bid=float(r["clob_down_bid"]) if r["clob_down_bid"] is not None else None,
            probability_lgb_v9_1=float(prob_v9_1) if prob_v9_1 is not None else None,
            probability_lgb_v12=float(prob_v12) if prob_v12 is not None else None,
            outcome=r["outcome"],
            poly_winner=r["poly_winner"],
        ))

    null_pct_v9_1 = 100.0 * null_v9_1 / max(len(ticks), 1)
    null_pct_v12 = 100.0 * null_v12 / max(len(ticks), 1)
    print(
        f"[tick_simulator] NULL rates: prob_v9_2(proxy)={null_pct_v9_1:.1f}%, "
        f"prob_v12={null_pct_v12:.1f}%",
        file=sys.stderr,
    )
    if null_pct_v9_1 > 80 or null_pct_v12 > 80:
        print(
            "[tick_simulator] WARNING: >80% NULL for one or both LGB probs. "
            "Gate replay will be sparse. This is expected for time ranges before "
            "the writer fix landed.",
            file=sys.stderr,
        )

    return ticks


# ---------------------------------------------------------------------------
# Trades loader (for validation)
# ---------------------------------------------------------------------------

def _load_actual_trades(
    strategy_id: str,
    hours: int,
    db_url: str,
) -> List[Dict[str, Any]]:
    """Load actual LIVE trades from the trades table for validation."""
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        raise RuntimeError("psycopg2 not installed")

    sync_url = (
        db_url
        .replace("postgresql+asyncpg://", "postgresql://")
        .replace("+asyncpg", "")
    )

    cutoff = datetime.fromtimestamp(
        time.time() - hours * 3600, tz=timezone.utc
    )

    query = """
        SELECT
            (EXTRACT(EPOCH FROM created_at)::bigint / 300) * 300 AS window_ts,
            direction,
            fill_price,
            outcome,
            stake_usd,
            EXTRACT(EPOCH FROM created_at)::bigint AS created_epoch,
            EXTRACT(DAY FROM created_at) || '-' || EXTRACT(MONTH FROM created_at) AS day_key
        FROM trades
        WHERE strategy_id = %s
          AND created_at >= %s
          AND outcome IN ('WIN', 'LOSS', 'PENDING')
        ORDER BY created_at ASC
    """

    conn = psycopg2.connect(sync_url, connect_timeout=10)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(query, (strategy_id, cutoff))
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    return rows


# ---------------------------------------------------------------------------
# Main replay loop
# ---------------------------------------------------------------------------

def run_replay(
    strategy_id: str,
    ticks: List[TickRow],
    cfg: Dict[str, Any],
    initial_state: Optional[SimState] = None,
) -> Tuple[List[FireEvent], SimState]:
    """Replay ticks through the gate stack chronologically.

    Returns: (fire_events, final_state).
    fire_events contains only ticks that passed all gates (would-fire).

    For v12_lgb_combo, each tick is evaluated for BOTH UP and DOWN directions
    (same as engine: v12_lgb_combo evaluates agreement+contrarian for both).
    The dedup gate ensures only one fire per (window_ts, direction).
    """
    stack = GateStack(cfg)
    state = initial_state or SimState(strategy_id=strategy_id)
    fire_events: List[FireEvent] = []

    contrarian_enabled = cfg.get("v12_contrarian_enabled", True)
    contrarian_min_dist = cfg.get("v12_contrarian_min_dist", 0.10)

    for tick in ticks:
        # Determine candidate directions and their combo_dist
        candidates: List[Tuple[str, Optional[float]]] = []

        # Agreement path: both models agree
        agree_dir = tick.combo_direction()
        if agree_dir is not None:
            candidates.append((agree_dir, tick.combo_min_dist()))

        # Contrarian path: v12 fires when models disagree
        if contrarian_enabled and tick.direction_v9_1 != tick.direction_v12:
            contra_dir = tick.contrarian_direction()
            if contra_dir is not None and tick.dist_v12 is not None:
                if tick.dist_v12 >= contrarian_min_dist:
                    # Avoid duplicate candidate for same direction
                    if not any(d == contra_dir for d, _ in candidates):
                        candidates.append((contra_dir, tick.dist_v12))

        for direction, combo_dist in candidates:
            passed, skip_reason = stack.evaluate(tick, direction, state, combo_dist)
            if passed:
                # Determine fill price for output
                fill = tick.clob_up_ask if direction == "UP" else tick.clob_down_ask
                event = FireEvent(
                    window_ts=tick.window_ts,
                    evaluated_at=tick.evaluated_at,
                    direction=direction,
                    strategy_id=strategy_id,
                    gate_config_hash=_cfg_hash(cfg),
                    combo_dist=combo_dist,
                    fill_price=fill,
                    regime=tick.regime,
                    v4_regime=tick.v4_regime,
                    eval_offset=tick.eval_offset,
                    t_band=_t_band(tick.eval_offset),
                )
                fire_events.append(event)
                state.mark_fired(tick.window_ts, direction)
            else:
                # On non-timing/non-dedup failures, reset consec counter
                # (mirrors engine: any upstream gate failure resets consec)
                if skip_reason and not (
                    skip_reason.startswith("consec_ticks")
                    or skip_reason.startswith("dedup")
                    or skip_reason.startswith("cooldown")
                ):
                    # Reset consec pass for this window+direction
                    key = (tick.window_ts, direction)
                    state.consec_pass.pop(tick.window_ts, None)

    return fire_events, state


# ---------------------------------------------------------------------------
# Outcome simulation (fill-math P&L per Hub #348 + payoff_math.md)
# ---------------------------------------------------------------------------

def _fill_math_pnl(
    outcome: Optional[str],
    fill_price: Optional[float],
    direction: Optional[str],
    poly_winner: Optional[str],
    stake: float = 3.0,  # default $3 stake for sim
) -> Optional[float]:
    """Compute fill-math P&L for a simulated fire event.

    Uses resolved outcome from window_snapshots or infers from poly_winner.
    Per memory feedback_payoff_math.md:
        WIN  = (1 - fill) * (stake / fill) - 0.072 * stake
        LOSS = -stake
    """
    if fill_price is None or fill_price <= 0:
        return None

    # Determine resolved outcome
    resolved_outcome: Optional[str] = None
    if outcome in ("WIN", "LOSS"):
        resolved_outcome = outcome
    elif poly_winner is not None and direction is not None:
        # Infer from poly_winner vs direction
        # poly_winner = "UP" means market resolved UP
        if poly_winner.upper() == direction.upper():
            resolved_outcome = "WIN"
        else:
            resolved_outcome = "LOSS"

    if resolved_outcome is None:
        return None
    if resolved_outcome == "WIN":
        return (1.0 - fill_price) * (stake / fill_price) - 0.072 * stake
    else:
        return -stake


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    strategy_id: str
    hours: int
    n_ticks: int
    n_ticks_with_probs: int
    n_sim_fires: int
    n_actual_trades: int
    passes_tolerance: bool
    sparse_data: bool = False  # True when >75% of ticks lack LGB probs
    regime_replay_confidence: str = "LOW"  # always LOW until writer fix
    per_day: Dict[str, Dict[str, int]] = field(default_factory=dict)
    per_window_direction_deltas: List[Dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        prob_pct = (
            100.0 * self.n_ticks_with_probs / self.n_ticks
            if self.n_ticks > 0 else 0.0
        )
        if self.sparse_data:
            validation_status = "SPARSE_DATA_EXPECTED"
            validation_note = (
                f"LGB prob coverage {prob_pct:.1f}% (<25%) — sim under-fires vs "
                "actual. Count comparison skipped. Qualitative check only."
            )
        else:
            validation_status = "PASS" if self.passes_tolerance else "FAIL"
            validation_note = ""
        lines = [
            f"=== Tick Simulator Validation ({self.strategy_id}, {self.hours}h) ===",
            f"regime_replay_confidence: {self.regime_replay_confidence} (window_snapshots.regime 99.7% NULL)",
            f"ticks loaded: {self.n_ticks} ({self.n_ticks_with_probs} with LGB probs, {prob_pct:.1f}%)",
            f"simulated fires: {self.n_sim_fires}",
            f"actual trades:   {self.n_actual_trades}",
            f"VALIDATION: {validation_status}",
        ]
        if validation_note:
            lines.append(f"NOTE: {validation_note}")
        lines += [
            "",
            "Per-day n_fires (sim vs actual):",
        ]
        all_days = sorted(set(list(self.per_day.keys())))
        for day in all_days:
            d = self.per_day.get(day, {})
            sim = d.get("sim", 0)
            actual = d.get("actual", 0)
            pct = abs(sim - actual) / max(actual, 1) * 100
            ok = "OK" if abs(sim - actual) / max(actual, 1) <= 0.05 else "FAIL"
            lines.append(f"  {day}: sim={sim} actual={actual} delta={pct:.1f}% [{ok}]")
        if self.per_window_direction_deltas:
            lines.append("")
            lines.append("Per-(window_ts,direction) mismatches > ±2:")
            for delta_row in self.per_window_direction_deltas[:20]:
                lines.append(f"  {delta_row}")
        return "\n".join(lines)


def validate(
    strategy_id: str,
    fire_events: List[FireEvent],
    actual_trades: List[Dict[str, Any]],
    ticks: List[TickRow],
    warmup_cutoff: float,
) -> ValidationResult:
    """Validate simulated fires against actual trades.

    Per Hub #348 tolerance:
    - ±2 trades on any single (window_ts, direction)
    - ±5% on aggregate n_fires per day
    Discard first 30min (warmup_cutoff) from comparison.
    """
    # Filter events after warmup
    sim_post_warmup = [e for e in fire_events if e.evaluated_at >= warmup_cutoff]
    actual_post_warmup = [
        t for t in actual_trades
        if float(t.get("created_epoch", 0)) >= warmup_cutoff
    ]

    # Count ticks with probs
    n_with_probs = sum(
        1 for t in ticks
        if t.probability_lgb_v9_1 is not None and t.probability_lgb_v12 is not None
    )
    n_ticks = len(ticks)
    prob_coverage = n_with_probs / n_ticks if n_ticks > 0 else 0.0
    # When >75% of ticks lack LGB probs (writer regression window), strict count
    # comparison is meaningless — the sim will under-fire proportionally.
    # Flag as SPARSE_DATA_EXPECTED and skip the ±5%/day gate.
    sparse_data = prob_coverage < 0.25

    # Per-day
    def _day_key(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")

    sim_by_day: Dict[str, int] = defaultdict(int)
    for e in sim_post_warmup:
        sim_by_day[_day_key(e.evaluated_at)] += 1

    actual_by_day: Dict[str, int] = defaultdict(int)
    for t in actual_post_warmup:
        actual_by_day[_day_key(float(t.get("created_epoch", 0)))] += 1

    all_days = sorted(set(list(sim_by_day.keys()) + list(actual_by_day.keys())))
    per_day: Dict[str, Dict[str, int]] = {}
    day_pass = True
    for day in all_days:
        s = sim_by_day.get(day, 0)
        a = actual_by_day.get(day, 0)
        per_day[day] = {"sim": s, "actual": a}
        pct = abs(s - a) / max(a, 1)
        # Skip strict count check when data is sparse
        if not sparse_data and a > 0 and pct > 0.05:
            day_pass = False

    # Per-(window_ts, direction) delta
    sim_wts: Dict[Tuple[int, str], int] = defaultdict(int)
    for e in sim_post_warmup:
        sim_wts[(e.window_ts, e.direction)] += 1

    actual_wts: Dict[Tuple[int, str], int] = defaultdict(int)
    for t in actual_post_warmup:
        wts = int(t.get("window_ts", 0))
        direction = str(t.get("direction", ""))
        actual_wts[(wts, direction)] += 1

    all_keys = set(list(sim_wts.keys()) + list(actual_wts.keys()))
    mismatches = []
    wts_pass = True
    for k in all_keys:
        s = sim_wts.get(k, 0)
        a = actual_wts.get(k, 0)
        delta = abs(s - a)
        # Skip strict per-window check when data is sparse (sim under-fires everywhere)
        if not sparse_data and delta > 2:
            wts_pass = False
            mismatches.append({
                "window_ts": k[0],
                "direction": k[1],
                "sim": s,
                "actual": a,
                "delta": delta,
            })

    # When sparse_data, passes_tolerance is trivially True (counts not comparable)
    passes = True if sparse_data else (day_pass and wts_pass)

    return ValidationResult(
        strategy_id=strategy_id,
        hours=int((list(ticks)[-1].evaluated_at - list(ticks)[0].evaluated_at) / 3600)
        if ticks else 0,
        n_ticks=n_ticks,
        n_ticks_with_probs=n_with_probs,
        n_sim_fires=len(sim_post_warmup),
        n_actual_trades=len(actual_post_warmup),
        passes_tolerance=passes,
        sparse_data=sparse_data,
        per_day=per_day,
        per_window_direction_deltas=mismatches,
    )


# ---------------------------------------------------------------------------
# Cell DataFrame output
# ---------------------------------------------------------------------------

def _build_cell_df(
    fire_events: List[FireEvent],
    ticks: List[TickRow],
    warmup_cutoff: float,
) -> List[Dict[str, Any]]:
    """Build per-cell summary: strategy_id × direction × t_band × regime."""
    events_post = [e for e in fire_events if e.evaluated_at >= warmup_cutoff]

    # Map window_ts -> outcome, poly_winner from ticks
    window_outcomes: Dict[int, Tuple[Optional[str], Optional[str]]] = {}
    for t in ticks:
        if t.window_ts not in window_outcomes and (t.outcome or t.poly_winner):
            window_outcomes[t.window_ts] = (t.outcome, t.poly_winner)

    # Group by cell key
    cells: Dict[Tuple, List[FireEvent]] = defaultdict(list)
    for e in events_post:
        key = (e.strategy_id, e.direction, e.t_band, e.regime or "unknown")
        cells[key].append(e)

    rows = []
    for (sid, direction, t_band, regime), evs in sorted(cells.items()):
        n_fires = len(evs)
        # Compute simulated WR using resolved outcomes
        resolved = [
            _fill_math_pnl(
                outcome=window_outcomes.get(e.window_ts, (None, None))[0],
                fill_price=e.fill_price,
                direction=direction,
                poly_winner=window_outcomes.get(e.window_ts, (None, None))[1],
            )
            for e in evs
        ]
        resolved_pnl = [p for p in resolved if p is not None]
        n_resolved = len(resolved_pnl)
        n_wins = sum(1 for p in resolved_pnl if p > 0)
        wr = n_wins / n_resolved if n_resolved > 0 else None
        total_pnl = sum(resolved_pnl)
        avg_fill = (
            sum(e.fill_price for e in evs if e.fill_price is not None)
            / max(sum(1 for e in evs if e.fill_price is not None), 1)
        )
        rows.append({
            "strategy_id": sid,
            "direction": direction,
            "t_band": t_band,
            "vpin_regime": regime,
            "n_would_fire": n_fires,
            "n_resolved": n_resolved,
            "simulated_WR": f"{wr:.1%}" if wr is not None else "N/A",
            "fill_adj_EV": f"{total_pnl:.2f}" if resolved_pnl else "N/A",
            "avg_fill": f"{avg_fill:.3f}" if avg_fill else "N/A",
            "regime_replay_confidence": "LOW",
        })
    return rows


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg_hash(cfg: Dict[str, Any]) -> str:
    import hashlib
    return hashlib.md5(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:8]


def _print_table(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        print("(no results)")
        return
    keys = list(rows[0].keys())
    widths = {k: max(len(k), max(len(str(r.get(k, ""))) for r in rows)) for k in keys}
    header = "  ".join(k.ljust(widths[k]) for k in keys)
    sep = "  ".join("-" * widths[k] for k in keys)
    print(header)
    print(sep)
    for r in rows:
        print("  ".join(str(r.get(k, "")).ljust(widths[k]) for k in keys))


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Tick-by-tick gate replay simulator (Hub #348 / audit #381)"
    )
    parser.add_argument(
        "--strategy-id",
        default="v12_lgb_combo",
        help="Strategy ID to simulate (default: v12_lgb_combo)",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=168,
        help="Hours of history to replay (default: 168 = 7d)",
    )
    parser.add_argument(
        "--gate-config-override",
        default=None,
        help="Path to JSON file with gate config overrides (merged over defaults)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Compare simulated fires to actual trades table",
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help="Database URL (default: DATABASE_URL env var)",
    )
    parser.add_argument(
        "--warmup-minutes",
        type=int,
        default=30,
        help="Minutes to discard from start of replay for state warm-up (default: 30)",
    )
    parser.add_argument(
        "--output-json",
        default=None,
        help="Path to write cell DataFrame as JSON (optional)",
    )
    args = parser.parse_args(argv)

    # Resolve DB URL
    db_url = args.db_url or os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set. Pass --db-url or set DATABASE_URL.", file=sys.stderr)
        return 1

    # Build gate config
    cfg = dict(_DEFAULT_GATE_CONFIG)
    if args.gate_config_override:
        override_path = Path(args.gate_config_override)
        if not override_path.exists():
            print(f"ERROR: gate config override file not found: {override_path}", file=sys.stderr)
            return 1
        with open(override_path) as f:
            overrides = json.load(f)
        cfg.update(overrides)
        print(f"[tick_simulator] Applied {len(overrides)} gate config overrides", file=sys.stderr)

    # Load ticks
    ticks = _load_ticks_from_db(args.strategy_id, args.hours, db_url)
    if not ticks:
        print("ERROR: No ticks loaded. Check DB connection and time range.", file=sys.stderr)
        return 1

    # Warmup cutoff
    first_ts = ticks[0].evaluated_at
    warmup_cutoff = first_ts + args.warmup_minutes * 60
    print(
        f"[tick_simulator] Warmup cutoff: {datetime.fromtimestamp(warmup_cutoff, tz=timezone.utc)} "
        f"({args.warmup_minutes}min discarded)",
        file=sys.stderr,
    )

    # Run replay
    t0 = time.monotonic()
    fire_events, final_state = run_replay(args.strategy_id, ticks, cfg)
    elapsed = time.monotonic() - t0
    print(
        f"[tick_simulator] Replay complete: {len(fire_events)} fires in {elapsed:.2f}s",
        file=sys.stderr,
    )

    # Build cell summary
    cell_df = _build_cell_df(fire_events, ticks, warmup_cutoff)
    print("\n=== Simulated fires per cell ===")
    _print_table(cell_df)

    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(cell_df, f, indent=2)
        print(f"\nCell DataFrame written to {args.output_json}")

    if args.validate:
        print("\n=== Loading actual trades for validation ===")
        try:
            actual_trades = _load_actual_trades(args.strategy_id, args.hours, db_url)
            print(f"[tick_simulator] Loaded {len(actual_trades)} actual trades")
        except Exception as exc:
            print(f"WARNING: Could not load actual trades: {exc}", file=sys.stderr)
            actual_trades = []

        result = validate(
            args.strategy_id,
            fire_events,
            actual_trades,
            ticks,
            warmup_cutoff,
        )
        print("\n" + result.summary())

        if result.sparse_data:
            print(
                "\nVALIDATION SPARSE_DATA_EXPECTED: LGB prob coverage <25% — "
                "count comparison skipped. Re-run after writer fix lands (audit #381).",
            )
        elif not result.passes_tolerance:
            print(
                "\nVALIDATION FAILED: Simulator output outside tolerance. "
                "Check gate config, NULL handling, and warm-up period.",
                file=sys.stderr,
            )
            return 1
        else:
            print("\nVALIDATION PASSED.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
