"""TDD tests for G16 BlockCellsGate in scripts/sim/tick_simulator.py.

G16 evaluates per-cell block predicates from strategy_runtime_overrides (or yaml
gate_params) against the current tick context.  A matching predicate causes the
tick to be denied with skip_reason="block_cells: <desc>".

All tests are CI-safe (no DB access required).

Spec reference: audit #9c follow-up to PR #511.
Hub note: G16 BlockCellsGate implementation note (to be posted post-PR).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# sys.path setup (mirrors test_tick_simulator_validation.py)
# ---------------------------------------------------------------------------
_ENGINE_ROOT = Path(__file__).resolve().parent.parent.parent  # engine/
_REPO_ROOT = _ENGINE_ROOT.parent                              # repo root
_SCRIPTS_ROOT = _REPO_ROOT / "scripts"

if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tick(
    window_ts: int = 1_777_700_000,
    eval_offset: int = 90,
    evaluated_at: Optional[float] = None,
    delta_tiingo: float = 0.001,
    delta_chainlink: float = 0.001,
    delta_binance: float = 0.001,
    vpin: float = 0.55,
    regime: Optional[str] = "TRANSITION",
    clob_up_ask: float = 0.55,
    clob_down_ask: float = 0.45,
    probability_lgb_v9_1: Optional[float] = 0.65,
    probability_lgb_v12: Optional[float] = 0.66,
    outcome: Optional[str] = "WIN",
    poly_winner: str = "UP",
):
    """Build a TickRow with sane defaults for G16 testing."""
    from sim.tick_simulator import TickRow
    return TickRow(
        window_ts=window_ts,
        asset="BTC",
        timeframe="5m",
        eval_offset=eval_offset,
        evaluated_at=evaluated_at or float(window_ts + (300 - eval_offset)),
        delta_tiingo=delta_tiingo,
        delta_chainlink=delta_chainlink,
        delta_binance=delta_binance,
        delta_pct=delta_tiingo,
        vpin=vpin,
        regime=regime,
        v4_regime=None,
        clob_up_ask=clob_up_ask,
        clob_down_ask=clob_down_ask,
        clob_up_bid=clob_up_ask - 0.01,
        clob_down_bid=clob_down_ask - 0.01,
        probability_lgb_v9_1=probability_lgb_v9_1,
        probability_lgb_v12=probability_lgb_v12,
        outcome=outcome,
        poly_winner=poly_winner,
    )


def _make_cfg(block_cells=None, **overrides):
    """Build a minimal gate config dict with block_cells set."""
    from sim.tick_simulator import _DEFAULT_GATE_CONFIG
    cfg = dict(_DEFAULT_GATE_CONFIG)
    cfg["min_consecutive_pass_ticks"] = 1  # disable consec for single-tick tests
    cfg["block_cells"] = block_cells or []
    cfg.update(overrides)
    return cfg


def _fresh_state():
    from sim.tick_simulator import SimState
    return SimState(strategy_id="v12_lgb_combo")


# ---------------------------------------------------------------------------
# G16 unit tests — gate function directly
# ---------------------------------------------------------------------------

class TestG16BlockCellsGateDirect:
    """Test _gate_G16_block_cells directly, without going through GateStack."""

    def test_g16_blocks_matching_cell(self):
        """Cell {regime:CASCADE, direction:DOWN} blocks DOWN intent in CASCADE."""
        from sim.tick_simulator import _gate_G16_block_cells, SimState

        tick = _make_tick(regime="CASCADE", eval_offset=90)
        state = SimState(strategy_id="v12_lgb_combo")
        cfg = _make_cfg(block_cells=[{"regime": "CASCADE", "direction": "DOWN"}])

        passed, reason = _gate_G16_block_cells(tick, "DOWN", state, 0.15, cfg)
        assert not passed, "G16 should block matching DOWN+CASCADE cell"
        assert reason is not None and "block_cells" in reason

    def test_g16_allows_non_matching_cell(self):
        """Same cell {regime:CASCADE, direction:DOWN} allows UP direction in CASCADE."""
        from sim.tick_simulator import _gate_G16_block_cells, SimState

        tick = _make_tick(regime="CASCADE", eval_offset=90)
        state = SimState(strategy_id="v12_lgb_combo")
        cfg = _make_cfg(block_cells=[{"regime": "CASCADE", "direction": "DOWN"}])

        passed, reason = _gate_G16_block_cells(tick, "UP", state, 0.15, cfg)
        assert passed, f"G16 should allow UP in CASCADE (cell only blocks DOWN). reason={reason}"
        assert reason is None

    def test_g16_allows_when_regime_null(self):
        """Cell with regime predicate does NOT match when tick regime is NULL."""
        from sim.tick_simulator import _gate_G16_block_cells, SimState

        tick = _make_tick(regime=None, eval_offset=90)
        state = SimState(strategy_id="v12_lgb_combo")
        cfg = _make_cfg(block_cells=[{"regime": "CASCADE", "direction": "DOWN"}])

        passed, reason = _gate_G16_block_cells(tick, "DOWN", state, 0.15, cfg)
        assert passed, f"G16 should allow when regime=NULL (can't match regime predicate). reason={reason}"
        assert reason is None

    def test_g16_conf_min_predicate_blocks_above_threshold(self):
        """Cell {conf_min:0.30} blocks when confidence=0.4 (>= 0.30)."""
        from sim.tick_simulator import _gate_G16_block_cells, SimState

        tick = _make_tick(regime="CASCADE", eval_offset=90)
        state = SimState(strategy_id="v12_lgb_combo")
        cfg = _make_cfg(block_cells=[{"conf_min": 0.30}])

        # combo_dist = confidence_score = 0.40 >= 0.30 → blocked
        passed, reason = _gate_G16_block_cells(tick, "DOWN", state, 0.40, cfg)
        assert not passed, "G16 should block when conf=0.40 >= conf_min=0.30"
        assert reason is not None and "block_cells" in reason

    def test_g16_conf_min_predicate_allows_below_threshold(self):
        """Cell {conf_min:0.30} allows when confidence=0.20 (< 0.30)."""
        from sim.tick_simulator import _gate_G16_block_cells, SimState

        tick = _make_tick(regime="CASCADE", eval_offset=90)
        state = SimState(strategy_id="v12_lgb_combo")
        cfg = _make_cfg(block_cells=[{"conf_min": 0.30}])

        # combo_dist = confidence_score = 0.20 < 0.30 → allowed
        passed, reason = _gate_G16_block_cells(tick, "DOWN", state, 0.20, cfg)
        assert passed, f"G16 should allow when conf=0.20 < conf_min=0.30. reason={reason}"
        assert reason is None

    def test_g16_combined_predicates_all_must_match(self):
        """Multi-field cell only blocks when ALL fields match."""
        from sim.tick_simulator import _gate_G16_block_cells, SimState

        # Cell: regime=CASCADE AND direction=DOWN AND conf_min=0.30
        cell = {"regime": "CASCADE", "direction": "DOWN", "conf_min": 0.30}

        # Case 1: all match → blocked
        tick_cascade = _make_tick(regime="CASCADE", eval_offset=90)
        state = SimState(strategy_id="v12_lgb_combo")
        cfg = _make_cfg(block_cells=[cell])
        passed, reason = _gate_G16_block_cells(tick_cascade, "DOWN", state, 0.40, cfg)
        assert not passed, "All predicates match → should block"

        # Case 2: regime doesn't match → allowed
        tick_transition = _make_tick(regime="TRANSITION", eval_offset=90)
        passed, reason = _gate_G16_block_cells(tick_transition, "DOWN", state, 0.40, cfg)
        assert passed, f"Regime mismatch → should allow. reason={reason}"

        # Case 3: direction doesn't match → allowed
        passed, reason = _gate_G16_block_cells(tick_cascade, "UP", state, 0.40, cfg)
        assert passed, f"Direction mismatch → should allow. reason={reason}"

        # Case 4: conf below min → allowed
        passed, reason = _gate_G16_block_cells(tick_cascade, "DOWN", state, 0.20, cfg)
        assert passed, f"Conf below min → should allow. reason={reason}"

    def test_g16_no_block_cells_config_allows_all(self):
        """Empty block_cells list → G16 always allows."""
        from sim.tick_simulator import _gate_G16_block_cells, SimState

        tick = _make_tick(regime="CASCADE", eval_offset=90)
        state = SimState(strategy_id="v12_lgb_combo")
        cfg = _make_cfg(block_cells=[])  # explicitly empty

        passed, reason = _gate_G16_block_cells(tick, "DOWN", state, 0.40, cfg)
        assert passed
        assert reason is None

    def test_g16_missing_block_cells_key_allows_all(self):
        """If block_cells not in cfg at all, G16 always allows."""
        from sim.tick_simulator import _gate_G16_block_cells, SimState

        tick = _make_tick(regime="CASCADE", eval_offset=90)
        state = SimState(strategy_id="v12_lgb_combo")
        from sim.tick_simulator import _DEFAULT_GATE_CONFIG
        cfg = dict(_DEFAULT_GATE_CONFIG)
        # block_cells deliberately absent from cfg

        passed, reason = _gate_G16_block_cells(tick, "DOWN", state, 0.40, cfg)
        assert passed
        assert reason is None

    def test_g16_t_band_match(self):
        """Cell {t_band: "T-91-120"} matches eval_offset 91-120 (sec-to-close, engine T-minus).

        cell_bucketing.t_band(eval_offset=110) = "T-91-120" (110 in (90, 120]).
        """
        from sim.tick_simulator import _gate_G16_block_cells, SimState

        # eval_offset=110 → cell_bucketing.t_band(110) = T-91-120
        tick = _make_tick(eval_offset=110)
        state = SimState(strategy_id="v12_lgb_combo")
        cfg = _make_cfg(block_cells=[{"t_band": "T-91-120"}])

        passed, reason = _gate_G16_block_cells(tick, "DOWN", state, 0.15, cfg)
        assert not passed, f"t_band=T-91-120 should block eval_offset=110. reason={reason}"
        assert reason is not None and "block_cells" in reason

    def test_g16_t_band_no_match_adjacent_band(self):
        """Cell {t_band: "T-91-120"} does NOT match eval_offset=60 (→ T-31-60)."""
        from sim.tick_simulator import _gate_G16_block_cells, SimState

        # eval_offset=60 → cell_bucketing.t_band(60) = T-31-60 (not T-91-120)
        tick = _make_tick(eval_offset=60)
        state = SimState(strategy_id="v12_lgb_combo")
        cfg = _make_cfg(block_cells=[{"t_band": "T-91-120"}])

        passed, reason = _gate_G16_block_cells(tick, "DOWN", state, 0.15, cfg)
        assert passed, f"t_band=T-91-120 should NOT block eval_offset=60. reason={reason}"

    def test_g16_hour_utc_match(self):
        """Cell {hour_utc:9} blocks when evaluated_at hour UTC is 9."""
        from sim.tick_simulator import _gate_G16_block_cells, SimState

        # window_ts at 09:00 UTC: 1_777_700_000 is arbitrary, use evaluated_at to set hour
        # 2026-01-01 09:05 UTC
        import datetime
        dt = datetime.datetime(2026, 1, 1, 9, 5, 0, tzinfo=datetime.timezone.utc)
        wts = int(dt.timestamp())
        tick = _make_tick(window_ts=wts, eval_offset=90, evaluated_at=float(wts + 210))

        state = SimState(strategy_id="v12_lgb_combo")
        cfg = _make_cfg(block_cells=[{"hour_utc": 9}])

        passed, reason = _gate_G16_block_cells(tick, "DOWN", state, 0.15, cfg)
        assert not passed, "hour_utc=9 should block when tick is at hour 9 UTC"
        assert reason is not None and "block_cells" in reason

    def test_g16_hour_utc_no_match_different_hour(self):
        """Cell {hour_utc:9} does NOT block when evaluated_at hour UTC is 14."""
        from sim.tick_simulator import _gate_G16_block_cells, SimState

        import datetime
        dt = datetime.datetime(2026, 1, 1, 14, 5, 0, tzinfo=datetime.timezone.utc)
        wts = int(dt.timestamp())
        tick = _make_tick(window_ts=wts, eval_offset=90, evaluated_at=float(wts + 210))

        state = SimState(strategy_id="v12_lgb_combo")
        cfg = _make_cfg(block_cells=[{"hour_utc": 9}])

        passed, reason = _gate_G16_block_cells(tick, "DOWN", state, 0.15, cfg)
        assert passed, f"hour_utc=9 should NOT block when tick is at hour 14. reason={reason}"

    def test_g16_session_match_us_pm(self):
        """Cell {session:'us_pm'} blocks when evaluated_at falls in us_pm hours (14-17 UTC)."""
        from sim.tick_simulator import _gate_G16_block_cells, SimState

        import datetime
        # 15:30 UTC → us_pm (14-17)
        dt = datetime.datetime(2026, 1, 1, 15, 30, 0, tzinfo=datetime.timezone.utc)
        wts = int(dt.timestamp())
        tick = _make_tick(window_ts=wts, eval_offset=90, evaluated_at=float(wts + 210))

        state = SimState(strategy_id="v12_lgb_combo")
        cfg = _make_cfg(block_cells=[{"session": "us_pm"}])

        passed, reason = _gate_G16_block_cells(tick, "DOWN", state, 0.15, cfg)
        assert not passed, "session=us_pm should block a 15:30 UTC tick"
        assert reason is not None and "block_cells" in reason

    def test_g16_session_no_match_asian_session(self):
        """Cell {session:'us_pm'} does NOT block during asian_early (02:00 UTC)."""
        from sim.tick_simulator import _gate_G16_block_cells, SimState

        import datetime
        dt = datetime.datetime(2026, 1, 1, 2, 0, 0, tzinfo=datetime.timezone.utc)
        wts = int(dt.timestamp())
        tick = _make_tick(window_ts=wts, eval_offset=90, evaluated_at=float(wts + 210))

        state = SimState(strategy_id="v12_lgb_combo")
        cfg = _make_cfg(block_cells=[{"session": "us_pm"}])

        passed, reason = _gate_G16_block_cells(tick, "DOWN", state, 0.15, cfg)
        assert passed, f"session=us_pm should NOT block asian_early (02:00 UTC). reason={reason}"

    def test_g16_skip_reason_format(self):
        """Denied tick has skip_reason starting with 'block_cells:'."""
        from sim.tick_simulator import _gate_G16_block_cells, SimState

        tick = _make_tick(regime="CASCADE", eval_offset=90)
        state = SimState(strategy_id="v12_lgb_combo")
        cfg = _make_cfg(block_cells=[{"regime": "CASCADE", "direction": "DOWN"}])

        passed, reason = _gate_G16_block_cells(tick, "DOWN", state, 0.15, cfg)
        assert not passed
        assert reason is not None
        assert reason.startswith("block_cells:"), (
            f"skip_reason must start with 'block_cells:' but got: {reason!r}"
        )


# ---------------------------------------------------------------------------
# G16 via GateStack (integration with disable-flag)
# ---------------------------------------------------------------------------

class TestG16ViaGateStack:
    """Test G16 via GateStack, including the --disable-gates G16 flag."""

    def _down_tick(self, regime="CASCADE", eval_offset=90):
        """Build a tick that passes all gates *except* G16 for DOWN direction.

        Uses negative deltas so source_agreement (G4) passes for DOWN,
        and clob prices in fill_band, vpin in range, combo_dist above floor.
        """
        return _make_tick(
            regime=regime,
            eval_offset=eval_offset,
            delta_tiingo=-0.001,       # DOWN
            delta_chainlink=-0.001,    # DOWN
            delta_binance=-0.001,      # DOWN
            probability_lgb_v9_1=0.35,  # DOWN, dist=0.15
            probability_lgb_v12=0.34,   # DOWN, dist=0.16
            clob_up_ask=0.55,
            clob_down_ask=0.45,        # fill in valid range
            vpin=0.55,
            outcome="LOSS",
            poly_winner="UP",
        )

    def test_g16_via_gatestack_blocks_matching_cell(self):
        """GateStack with block_cells denies the tick at G16."""
        from sim.tick_simulator import GateStack, SimState

        cfg = _make_cfg(block_cells=[{"regime": "CASCADE", "direction": "DOWN"}])
        stack = GateStack(cfg)
        state = SimState(strategy_id="v12_lgb_combo")

        tick = self._down_tick(regime="CASCADE", eval_offset=90)
        combo_dist = 0.15

        passed, reason = stack.evaluate(tick, "DOWN", state, combo_dist=combo_dist)
        assert not passed
        assert reason is not None and "block_cells" in reason

    def test_g16_disable_flag_makes_g16_noop(self):
        """--disable-gates G16 makes G16 pass-through (no-op)."""
        from sim.tick_simulator import GateStack, SimState

        cfg = _make_cfg(block_cells=[{"regime": "CASCADE", "direction": "DOWN"}])
        # Disable G16 — should pass even though cell would block
        stack = GateStack(cfg, disabled_gates={"G16"})
        state = SimState(strategy_id="v12_lgb_combo")

        tick = self._down_tick(regime="CASCADE", eval_offset=90)
        combo_dist = 0.15

        passed, reason = stack.evaluate(tick, "DOWN", state, combo_dist=combo_dist)
        assert passed, (
            f"G16 disabled → should pass even with matching block_cell. reason={reason}"
        )

    def test_g16_disable_flag_no_longer_in_unimplemented_set(self):
        """G16 must NOT be in _UNIMPLEMENTED_GATES after implementation."""
        from sim.tick_simulator import _UNIMPLEMENTED_GATES
        assert "G16" not in _UNIMPLEMENTED_GATES, (
            "G16 is now implemented — it must be removed from _UNIMPLEMENTED_GATES"
        )


# ---------------------------------------------------------------------------
# G16 in full replay loop
# ---------------------------------------------------------------------------

class TestG16InReplayLoop:
    """Test G16 effect through the full run_replay() pipeline."""

    def _make_down_cascade_ticks(self, wts: int, n: int = 3):
        """3+ ticks in one window with DOWN signals + CASCADE regime.

        Negative deltas ensure source_agreement (G4) passes for DOWN.
        """
        ticks = []
        offsets = [110, 108, 106, 104, 102][:n]
        for i, offset in enumerate(offsets):
            ticks.append(_make_tick(
                window_ts=wts,
                eval_offset=offset,
                evaluated_at=float(wts + (300 - offset) + i * 0.1),
                probability_lgb_v9_1=0.35,   # DOWN (dist=0.15)
                probability_lgb_v12=0.34,    # DOWN (dist=0.16)
                delta_tiingo=-0.001,          # DOWN for source_agreement
                delta_chainlink=-0.001,       # DOWN for source_agreement
                delta_binance=-0.001,         # DOWN for source_agreement
                vpin=0.55,
                regime="CASCADE",
                clob_down_ask=0.45,
                clob_up_ask=0.55,
                outcome="LOSS",
                poly_winner="UP",
            ))
        return ticks

    def test_g16_suppresses_fire_in_replay(self):
        """Block cell {regime:CASCADE, direction:DOWN} prevents fire in CASCADE+DOWN window."""
        from sim.tick_simulator import run_replay

        wts = 1_777_700_000
        # 3 ticks — needed for min_consecutive_pass_ticks=3 default
        ticks = self._make_down_cascade_ticks(wts, n=3)

        # Without block_cells → fires
        cfg_no_block = _make_cfg(block_cells=[])
        fires_no_block, _ = run_replay("v12_lgb_combo", ticks, cfg_no_block)
        down_fires_no_block = [f for f in fires_no_block if f.direction == "DOWN"]
        assert len(down_fires_no_block) >= 1, "Should fire at least once without block_cells"

        # With block_cells = [{regime:CASCADE, direction:DOWN}] → no fire
        cfg_with_block = _make_cfg(
            block_cells=[{"regime": "CASCADE", "direction": "DOWN"}]
        )
        fires_with_block, _ = run_replay("v12_lgb_combo", ticks, cfg_with_block)
        down_fires_with_block = [f for f in fires_with_block if f.direction == "DOWN"]
        assert len(down_fires_with_block) == 0, (
            f"G16 should suppress all DOWN+CASCADE fires. Got {len(down_fires_with_block)}"
        )

    def test_g16_off_fires_more_than_g16_on(self):
        """Disabling G16 fires more (or equal) vs G16 on, never less."""
        from sim.tick_simulator import run_replay

        wts = 1_777_700_000
        ticks = self._make_down_cascade_ticks(wts, n=3)

        cfg = _make_cfg(block_cells=[{"regime": "CASCADE", "direction": "DOWN"}])

        fires_on, _ = run_replay("v12_lgb_combo", ticks, cfg, disabled_gates=None)
        fires_off, _ = run_replay(
            "v12_lgb_combo", ticks, cfg, disabled_gates={"G16"}
        )

        assert len(fires_off) >= len(fires_on), (
            f"G16-off should fire >= G16-on. on={len(fires_on)} off={len(fires_off)}"
        )
