"""Integration tests for scripts/sim/tick_simulator.py.

These tests run in two modes:

1. **Unit mode (CI-safe, no DB)**
   - Tests gate logic, state machines, and replay loop with synthetic tick data.
   - Runs in CI with zero external dependencies.

2. **DB mode (requires_db mark)**
   - Loads real ticks from RDS prod, runs 7d replay of v12_lgb_combo,
     validates against actual trades table.
   - Skipped in CI when DATABASE_URL is a test stub; run manually on Montreal.

Gate-signature regression test
-------------------------------
Imports each gate class and asserts __init__ + evaluate() signatures remain
stable. Breaks loudly if a gate refactor would break the simulator.

Usage (unit tests only):
    cd engine && pytest tests/integration/test_tick_simulator_validation.py -v

Usage (DB validation, Montreal):
    DATABASE_URL=postgresql://... pytest \\
        tests/integration/test_tick_simulator_validation.py \\
        -m requires_db -v
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import List, Optional

import pytest

# ---------------------------------------------------------------------------
# sys.path setup — allow importing from scripts/sim/
# ---------------------------------------------------------------------------
_ENGINE_ROOT = Path(__file__).resolve().parent.parent.parent  # engine/
_REPO_ROOT = _ENGINE_ROOT.parent                               # repo root
_SCRIPTS_ROOT = _REPO_ROOT / "scripts"

# Ensure engine/ is on path for gate imports
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))
# Ensure scripts/ is on path for tick_simulator import
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------
requires_db = pytest.mark.requires_db

_DB_URL = os.environ.get("DATABASE_URL", "")
_IS_TEST_DB = not _DB_URL or "sqlite" in _DB_URL or "memory" in _DB_URL


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
    regime: str = "TRANSITION",
    clob_up_ask: float = 0.55,
    clob_down_ask: float = 0.45,
    probability_lgb_v9_1: Optional[float] = 0.65,
    probability_lgb_v12: Optional[float] = 0.66,
    outcome: Optional[str] = "WIN",
    poly_winner: str = "UP",
):
    """Build a TickRow with sane defaults for testing."""
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


# ---------------------------------------------------------------------------
# Gate signature regression tests (CI-safe)
# ---------------------------------------------------------------------------

# These tests import actual engine gate classes to verify that __init__ and
# evaluate() signatures remain stable. The engine requires Python ≥3.10 on
# prod (Montreal: Python 3.11). On macOS with Python 3.9, the settings module
# uses `X | None` union syntax which fails on import. Skip gracefully.
_ENGINE_IMPORT_OK: bool = False
try:
    from strategies.gates.timing import TimingGate  # noqa: F401
    _ENGINE_IMPORT_OK = True
except Exception:
    pass

_requires_engine = pytest.mark.skipif(
    not _ENGINE_IMPORT_OK,
    reason="Engine import requires Python ≥3.10 (macOS Python 3.9 not supported for engine code)",
)


class TestGateSignatures:
    """Verify gate __init__ and evaluate() signatures remain stable.

    If any gate refactor would break the simulator, this test fails immediately.
    Requires Python ≥3.10 (Montreal engine environment).
    """

    @_requires_engine
    def test_timing_gate_importable(self):
        from strategies.gates.timing import TimingGate
        g = TimingGate(min_offset=24, max_offset=240)
        assert g.name == "timing"
        assert hasattr(g, "evaluate")

    @_requires_engine
    def test_source_agreement_gate_importable(self):
        from strategies.gates.source_agreement import SourceAgreementGate
        g = SourceAgreementGate(min_sources=2)
        assert g.name == "source_agreement"
        assert hasattr(g, "evaluate")

    @_requires_engine
    def test_fill_band_gate_importable(self):
        from strategies.gates.fill_band import FillBandGate
        g = FillBandGate(direction="UP", min_fill_price=0.20, max_fill_price=0.82)
        assert g.name == "fill_band"
        assert hasattr(g, "evaluate")

    @_requires_engine
    def test_consecutive_pass_ticks_gate_importable(self):
        from strategies.gates.consecutive_pass_ticks import ConsecutivePassTicksGate
        g = ConsecutivePassTicksGate(min_ticks=3)
        assert g.name == "consecutive_pass_ticks"
        assert hasattr(g, "evaluate")

    @_requires_engine
    def test_regime_gate_importable(self):
        from strategies.gates.regime import RegimeGate
        g = RegimeGate(allowed=["chop", "volatile_trend"])
        assert g.name == "regime"
        assert hasattr(g, "evaluate")

    @_requires_engine
    def test_confidence_gate_importable(self):
        from strategies.gates.confidence import ConfidenceGate
        g = ConfidenceGate(min_dist=0.10)
        assert g.name == "confidence"
        assert hasattr(g, "evaluate")


# ---------------------------------------------------------------------------
# SimState unit tests (CI-safe)
# ---------------------------------------------------------------------------

class TestSimState:
    """Test the SimState machine without DB access."""

    def test_cooldown_initial_no_cooldown(self):
        from sim.tick_simulator import SimState
        s = SimState(strategy_id="v12_lgb_combo")
        in_cd, remaining = s.in_cooldown(time.time(), cooldown_min=20.0)
        assert not in_cd
        assert remaining == 0.0

    def test_cooldown_active_after_loss(self):
        from sim.tick_simulator import SimState
        s = SimState(strategy_id="v12_lgb_combo")
        now = time.time()
        s.record_loss(now - 5 * 60)  # 5 minutes ago
        in_cd, remaining = s.in_cooldown(now, cooldown_min=20.0)
        assert in_cd
        assert 14 * 60 <= remaining <= 16 * 60  # ~15 min remaining

    def test_cooldown_expired(self):
        from sim.tick_simulator import SimState
        s = SimState(strategy_id="v12_lgb_combo")
        now = time.time()
        s.record_loss(now - 25 * 60)  # 25 minutes ago
        in_cd, remaining = s.in_cooldown(now, cooldown_min=20.0)
        assert not in_cd

    def test_consec_pass_counter_increments(self):
        from sim.tick_simulator import SimState
        s = SimState(strategy_id="v12_lgb_combo")
        now = time.time()
        wts = 1_777_700_000
        c1 = s.update_consec(wts, "UP", now)
        c2 = s.update_consec(wts, "UP", now + 2.0)
        c3 = s.update_consec(wts, "UP", now + 4.0)
        assert c1 == 1
        assert c2 == 2
        assert c3 == 3

    def test_consec_pass_counter_resets_on_direction_change(self):
        from sim.tick_simulator import SimState
        s = SimState(strategy_id="v12_lgb_combo")
        now = time.time()
        wts = 1_777_700_000
        s.update_consec(wts, "UP", now)
        s.update_consec(wts, "UP", now + 2.0)
        c = s.update_consec(wts, "DOWN", now + 4.0)
        assert c == 1  # reset on direction flip

    def test_consec_pass_counter_resets_on_gap(self):
        from sim.tick_simulator import SimState
        s = SimState(strategy_id="v12_lgb_combo")
        now = time.time()
        wts = 1_777_700_000
        s.update_consec(wts, "UP", now)
        # Gap > 5.0s (max_gap)
        c = s.update_consec(wts, "UP", now + 10.0)
        assert c == 1  # reset on gap

    def test_dedup_marks_and_detects_fired(self):
        from sim.tick_simulator import SimState
        s = SimState(strategy_id="v12_lgb_combo")
        wts = 1_777_700_000
        assert not s.has_fired(wts, "UP")
        s.mark_fired(wts, "UP")
        assert s.has_fired(wts, "UP")
        assert not s.has_fired(wts, "DOWN")  # different direction


# ---------------------------------------------------------------------------
# GateStack unit tests (CI-safe)
# ---------------------------------------------------------------------------

class TestGateStack:
    """Test the GateStack evaluation with synthetic ticks."""

    def _default_cfg(self):
        from sim.tick_simulator import _DEFAULT_GATE_CONFIG
        return dict(_DEFAULT_GATE_CONFIG)

    def _fresh_state(self):
        from sim.tick_simulator import SimState
        return SimState(strategy_id="v12_lgb_combo")

    def test_passes_all_gates_for_valid_up_tick(self):
        from sim.tick_simulator import GateStack, SimState
        cfg = self._default_cfg()
        cfg["min_consecutive_pass_ticks"] = 1  # disable consec for single-tick test
        stack = GateStack(cfg)
        state = SimState(strategy_id="v12_lgb_combo")

        tick = _make_tick(
            probability_lgb_v9_1=0.65,  # UP, dist=0.15
            probability_lgb_v12=0.66,   # UP, dist=0.16
        )
        # Use combo_dist=0.15 (both agree UP)
        combo_dist = min(abs(0.65 - 0.5), abs(0.66 - 0.5))
        passed, reason = stack.evaluate(tick, "UP", state, combo_dist=combo_dist)
        assert passed, f"Expected pass, got: {reason}"

    def test_fails_timing_gate(self):
        from sim.tick_simulator import GateStack, SimState
        cfg = self._default_cfg()
        stack = GateStack(cfg)
        state = SimState(strategy_id="v12_lgb_combo")

        tick = _make_tick(eval_offset=300)  # > max_offset_sec=240
        passed, reason = stack.evaluate(tick, "UP", state, combo_dist=0.15)
        assert not passed
        assert "timing" in reason

    def test_fails_vpin_gate(self):
        from sim.tick_simulator import GateStack, SimState
        cfg = self._default_cfg()
        stack = GateStack(cfg)
        state = SimState(strategy_id="v12_lgb_combo")

        tick = _make_tick(vpin=0.20)  # < vpin_min=0.40
        passed, reason = stack.evaluate(tick, "UP", state, combo_dist=0.15)
        assert not passed
        assert "vpin" in reason

    def test_fails_fill_band_ceiling(self):
        from sim.tick_simulator import GateStack, SimState
        cfg = self._default_cfg()
        cfg["min_consecutive_pass_ticks"] = 1
        stack = GateStack(cfg)
        state = SimState(strategy_id="v12_lgb_combo")

        tick = _make_tick(clob_up_ask=0.90)  # > fill_band_max=0.82
        combo_dist = min(0.15, 0.16)
        passed, reason = stack.evaluate(tick, "UP", state, combo_dist=combo_dist)
        assert not passed
        assert "fill_band" in reason

    def test_fails_combo_dist_below_min(self):
        from sim.tick_simulator import GateStack, SimState
        cfg = self._default_cfg()
        stack = GateStack(cfg)
        state = SimState(strategy_id="v12_lgb_combo")

        tick = _make_tick(
            probability_lgb_v9_1=0.54,  # dist=0.04 < combo_min_dist=0.10
            probability_lgb_v12=0.54,
        )
        passed, reason = stack.evaluate(tick, "UP", state, combo_dist=0.04)
        assert not passed
        assert "lgb" in reason

    def test_fails_cooldown(self):
        from sim.tick_simulator import GateStack, SimState
        cfg = self._default_cfg()
        cfg["min_consecutive_pass_ticks"] = 1
        stack = GateStack(cfg)
        state = SimState(strategy_id="v12_lgb_combo")

        tick = _make_tick()
        state.record_loss(tick.evaluated_at - 5 * 60)  # 5 min ago (< 20 min cooldown)
        combo_dist = min(abs(0.65 - 0.5), abs(0.66 - 0.5))
        passed, reason = stack.evaluate(tick, "UP", state, combo_dist=combo_dist)
        assert not passed
        assert "cooldown" in reason

    def test_consec_ticks_accumulation(self):
        """3-tick confirmation: first 2 ticks fail G11, 3rd passes."""
        from sim.tick_simulator import GateStack, SimState, TickRow
        cfg = self._default_cfg()
        stack = GateStack(cfg)
        state = SimState(strategy_id="v12_lgb_combo")

        wts = 1_777_700_000
        combo_dist = 0.15
        for i in range(3):
            tick = _make_tick(
                window_ts=wts,
                eval_offset=90 - i * 2,
                evaluated_at=float(wts + 210 + i * 2),
            )
            passed, reason = stack.evaluate(tick, "UP", state, combo_dist=combo_dist)
            if i < 2:
                assert not passed, f"Tick {i} should NOT have passed"
                assert "consec_ticks" in (reason or ""), f"Expected consec_ticks, got {reason}"
            else:
                assert passed, f"Tick 2 SHOULD have passed, got: {reason}"

    def test_dedup_prevents_second_fire_same_window(self):
        """After one fire, dedup blocks second fire in same window."""
        from sim.tick_simulator import GateStack, SimState
        cfg = self._default_cfg()
        cfg["min_consecutive_pass_ticks"] = 1
        stack = GateStack(cfg)
        state = SimState(strategy_id="v12_lgb_combo")

        wts = 1_777_700_000
        combo_dist = 0.15

        tick1 = _make_tick(window_ts=wts, eval_offset=90, evaluated_at=float(wts + 210))
        passed1, _ = stack.evaluate(tick1, "UP", state, combo_dist=combo_dist)
        assert passed1
        state.mark_fired(wts, "UP")

        tick2 = _make_tick(window_ts=wts, eval_offset=88, evaluated_at=float(wts + 212))
        passed2, reason2 = stack.evaluate(tick2, "UP", state, combo_dist=combo_dist)
        assert not passed2
        assert "dedup" in reason2


# ---------------------------------------------------------------------------
# Replay loop integration test (CI-safe, no DB)
# ---------------------------------------------------------------------------

class TestReplayLoop:
    """Test the full replay loop with synthetic tick sequences."""

    def test_replay_single_window_fires_once(self):
        from sim.tick_simulator import run_replay, _DEFAULT_GATE_CONFIG

        cfg = dict(_DEFAULT_GATE_CONFIG)
        cfg["min_consecutive_pass_ticks"] = 3

        wts = 1_777_700_000
        # Build 5 ticks for one window: eval_offset 110,108,106,104,102 (ascending)
        ticks = []
        for i, offset in enumerate([110, 108, 106, 104, 102]):
            ticks.append(_make_tick(
                window_ts=wts,
                eval_offset=offset,
                evaluated_at=float(wts + (300 - offset) + i * 0.1),
                probability_lgb_v9_1=0.65,
                probability_lgb_v12=0.66,
                vpin=0.55,
                clob_up_ask=0.55,
                clob_down_ask=0.45,
            ))

        fires, state = run_replay("v12_lgb_combo", ticks, cfg)
        # Should fire once for UP (direction agreement path)
        up_fires = [f for f in fires if f.direction == "UP"]
        assert len(up_fires) == 1, f"Expected 1 UP fire, got {len(up_fires)}"

    def test_replay_does_not_fire_without_probs(self):
        from sim.tick_simulator import run_replay, _DEFAULT_GATE_CONFIG

        cfg = dict(_DEFAULT_GATE_CONFIG)
        ticks = [
            _make_tick(
                probability_lgb_v9_1=None,  # NULL
                probability_lgb_v12=None,   # NULL
            )
        ]
        fires, _ = run_replay("v12_lgb_combo", ticks, cfg)
        assert len(fires) == 0

    def test_replay_contrarian_path(self):
        """When v9_1 and v12 disagree, contrarian path fires v12's direction."""
        from sim.tick_simulator import run_replay, _DEFAULT_GATE_CONFIG

        cfg = dict(_DEFAULT_GATE_CONFIG)
        cfg["min_consecutive_pass_ticks"] = 1
        cfg["v12_contrarian_enabled"] = True
        cfg["v12_contrarian_min_dist"] = 0.10

        wts = 1_777_700_000
        tick = _make_tick(
            window_ts=wts,
            probability_lgb_v9_1=0.35,  # DOWN (dist=0.15)
            probability_lgb_v12=0.65,   # UP (dist=0.15) — disagrees with v9_1
            vpin=0.55,
            clob_up_ask=0.55,
            clob_down_ask=0.45,
            delta_tiingo=0.001,   # sources agree UP
            delta_chainlink=0.001,
            delta_binance=0.001,
        )

        fires, _ = run_replay("v12_lgb_combo", [tick], cfg)
        # v9_1 says DOWN, v12 says UP: contrarian path should fire in UP direction
        up_fires = [f for f in fires if f.direction == "UP"]
        assert len(up_fires) == 1, f"Expected contrarian UP fire, got {fires}"

    def test_replay_state_loss_blocks_subsequent_ticks(self):
        """Record a LOSS partway through replay; subsequent ticks are blocked."""
        from sim.tick_simulator import run_replay, SimState, _DEFAULT_GATE_CONFIG

        cfg = dict(_DEFAULT_GATE_CONFIG)
        cfg["min_consecutive_pass_ticks"] = 1
        cfg["post_loss_cooldown_min"] = 20

        now = time.time()
        state = SimState(strategy_id="v12_lgb_combo")
        state.record_loss(now - 5 * 60)  # loss 5 min ago → 15 min cooldown remaining

        ticks = [
            _make_tick(
                window_ts=int(now // 300) * 300,
                eval_offset=90,
                evaluated_at=now,
            )
        ]
        fires, _ = run_replay("v12_lgb_combo", ticks, cfg, initial_state=state)
        assert len(fires) == 0, "Expected no fires during cooldown"


# ---------------------------------------------------------------------------
# Validation logic unit test (CI-safe)
# ---------------------------------------------------------------------------

class TestValidation:
    def test_validate_perfect_match_passes(self):
        from sim.tick_simulator import validate, FireEvent, TickRow

        wts = 1_777_700_000
        fires = [
            FireEvent(
                window_ts=wts,
                evaluated_at=float(wts + 210),
                direction="UP",
                strategy_id="v12_lgb_combo",
                gate_config_hash="abc",
                eval_offset=90,
                t_band="T-121-180",
            )
        ]
        actual_trades = [
            {
                "window_ts": wts,
                "direction": "UP",
                "created_epoch": float(wts + 210),
                "outcome": "WIN",
            }
        ]
        ticks = [
            _make_tick(window_ts=wts, evaluated_at=float(wts + 210))
        ]
        result = validate(
            "v12_lgb_combo",
            fires,
            actual_trades,
            ticks,
            warmup_cutoff=0.0,  # no warmup
        )
        assert result.passes_tolerance

    def test_validate_large_delta_fails(self):
        from sim.tick_simulator import validate, FireEvent, TickRow

        wts = 1_777_700_000
        now = float(wts + 210)
        fires = [
            FireEvent(
                window_ts=wts + 300 * i,
                evaluated_at=now + 300 * i,
                direction="UP",
                strategy_id="v12_lgb_combo",
                gate_config_hash="abc",
                eval_offset=90,
                t_band="T-121-180",
            )
            for i in range(20)  # 20 sim fires on day 1
        ]
        actual_trades = [
            {
                "window_ts": wts,
                "direction": "UP",
                "created_epoch": now,
                "outcome": "WIN",
            }
        ]  # only 1 actual trade — big mismatch
        ticks = [_make_tick(window_ts=wts, evaluated_at=now)]
        result = validate(
            "v12_lgb_combo",
            fires,
            actual_trades,
            ticks,
            warmup_cutoff=0.0,
        )
        assert not result.passes_tolerance

    def test_validate_sparse_data_passes_with_big_delta(self):
        """When LGB prob coverage <25%, validation should report SPARSE_DATA_EXPECTED
        and not fail even when sim fires << actual trades."""
        from sim.tick_simulator import validate, FireEvent, TickRow

        wts = 1_777_700_000
        now = float(wts + 210)

        fires = [
            FireEvent(
                window_ts=wts + 300 * i,
                evaluated_at=now + 300 * i,
                direction="UP",
                strategy_id="v12_lgb_combo",
                gate_config_hash="abc",
                eval_offset=90,
                t_band="T-121-180",
            )
            for i in range(5)  # 5 sim fires
        ]
        actual_trades = [
            {
                "window_ts": wts + 300 * i,
                "direction": "UP",
                "created_epoch": now + 300 * i,
                "outcome": "WIN",
            }
            for i in range(100)  # 100 actual trades — huge gap
        ]
        # Build ticks where <25% have LGB probs (simulate writer regression)
        ticks_no_probs = [
            _make_tick(
                window_ts=wts + 300 * i,
                evaluated_at=now + 300 * i,
                probability_lgb_v9_1=None,
                probability_lgb_v12=None,
            )
            for i in range(97)
        ]
        ticks_with_probs = [
            _make_tick(
                window_ts=wts + 300 * i,
                evaluated_at=now + 300 * i,
            )
            for i in range(3)
        ]
        result = validate(
            "v12_lgb_combo",
            fires,
            actual_trades,
            ticks_no_probs + ticks_with_probs,
            warmup_cutoff=0.0,
        )
        assert result.sparse_data, "Expected sparse_data=True when <25% have probs"
        assert result.passes_tolerance, "Sparse data should not trigger FAIL"


# ---------------------------------------------------------------------------
# v2: Gate toggle (--disable-gates) tests (CI-safe)
# ---------------------------------------------------------------------------

class TestGateToggle:
    """Tests for the gate toggle architecture (EXPLORATION mode)."""

    def test_disable_g10_bypasses_cooldown(self):
        """Disabling G10 means a post-loss cooldown does NOT block the fire."""
        from sim.tick_simulator import GateStack, SimState, _DEFAULT_GATE_CONFIG

        cfg = dict(_DEFAULT_GATE_CONFIG)
        cfg["min_consecutive_pass_ticks"] = 1

        state = SimState(strategy_id="v12_lgb_combo")
        import time
        now = time.time()
        state.record_loss(now - 5 * 60)  # 5 min ago — normally blocks

        tick = _make_tick(evaluated_at=now)
        combo_dist = 0.15

        # Without disable: blocked
        stack_on = GateStack(cfg)
        passed_on, reason_on = stack_on.evaluate(tick, "UP", state, combo_dist)
        assert not passed_on and "cooldown" in (reason_on or "")

        # With G10 disabled: passes
        state2 = SimState(strategy_id="v12_lgb_combo")
        state2.record_loss(now - 5 * 60)
        stack_off = GateStack(cfg, disabled_gates={"G10"})
        passed_off, reason_off = stack_off.evaluate(tick, "UP", state2, combo_dist)
        assert passed_off, f"Expected pass with G10 disabled, got: {reason_off}"

    def test_disable_g4_bypasses_source_agreement(self):
        """Disabling G4 lets ticks pass even with NULL chainlink/tiingo deltas."""
        from sim.tick_simulator import GateStack, SimState, _DEFAULT_GATE_CONFIG

        cfg = dict(_DEFAULT_GATE_CONFIG)
        cfg["min_consecutive_pass_ticks"] = 1
        cfg["source_agreement_require_chainlink"] = True
        cfg["source_agreement_require_tiingo"] = True

        state = SimState(strategy_id="v12_lgb_combo")
        tick = _make_tick(delta_tiingo=None, delta_chainlink=None)

        # Without disable: blocked on source_agreement
        stack_on = GateStack(cfg)
        passed_on, reason_on = stack_on.evaluate(tick, "UP", state, combo_dist=0.15)
        assert not passed_on and "source_agreement" in (reason_on or "")

        # With G4 disabled: passes
        state2 = SimState(strategy_id="v12_lgb_combo")
        stack_off = GateStack(cfg, disabled_gates={"G4"})
        passed_off, reason_off = stack_off.evaluate(tick, "UP", state2, combo_dist=0.15)
        assert passed_off, f"Expected pass with G4 disabled, got: {reason_off}"

    def test_disable_multiple_gates(self):
        """Multiple gates can be disabled simultaneously."""
        from sim.tick_simulator import GateStack, SimState, _DEFAULT_GATE_CONFIG

        cfg = dict(_DEFAULT_GATE_CONFIG)
        cfg["min_consecutive_pass_ticks"] = 1

        import time
        now = time.time()
        # Create a state that would fail both G10 (cooldown) and G4 (source agreement)
        state = SimState(strategy_id="v12_lgb_combo")
        state.record_loss(now - 5 * 60)

        tick = _make_tick(delta_tiingo=None, delta_chainlink=None, evaluated_at=now)
        stack = GateStack(cfg, disabled_gates={"G4", "G10"})
        passed, reason = stack.evaluate(tick, "UP", state, combo_dist=0.15)
        assert passed, f"Expected pass with G4+G10 disabled, got: {reason}"

    def test_disabled_gates_in_fire_event(self):
        """disabled_gates string is propagated into FireEvent for traceability."""
        from sim.tick_simulator import run_replay, _DEFAULT_GATE_CONFIG

        cfg = dict(_DEFAULT_GATE_CONFIG)
        cfg["min_consecutive_pass_ticks"] = 1

        wts = 1_777_700_000
        ticks = [_make_tick(window_ts=wts, eval_offset=90)]
        fires, _ = run_replay(
            "v12_lgb_combo", ticks, cfg, disabled_gates={"G10"}, stake=5.0
        )
        if fires:
            assert fires[0].disabled_gates == "G10", (
                f"Expected disabled_gates='G10', got {fires[0].disabled_gates!r}"
            )

    def test_unimplemented_gate_disable_warns_but_does_not_error(self):
        """Disabling G16 (not implemented) emits a warning but doesn't raise."""
        import io, contextlib
        from sim.tick_simulator import GateStack, SimState, _DEFAULT_GATE_CONFIG

        cfg = dict(_DEFAULT_GATE_CONFIG)
        cfg["min_consecutive_pass_ticks"] = 1

        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            stack = GateStack(cfg, disabled_gates={"G16"})

        warn_output = stderr_capture.getvalue()
        assert "not yet implemented" in warn_output or "G16" in warn_output, (
            f"Expected warning about G16, got: {warn_output!r}"
        )

        # Should still evaluate without error
        state = SimState(strategy_id="v12_lgb_combo")
        tick = _make_tick()
        passed, _ = stack.evaluate(tick, "UP", state, combo_dist=0.15)
        # Just verify it doesn't raise


# ---------------------------------------------------------------------------
# v2: EV aggregation tests (CI-safe)
# ---------------------------------------------------------------------------

class TestEVAggregation:
    """Tests for WIN/LOSS resolution + EV per fire."""

    def test_fill_math_pnl_win(self):
        """WIN: (1-fill) * (stake/fill) - 0.072*stake"""
        from sim.tick_simulator import _fill_math_pnl
        pnl = _fill_math_pnl("WIN", 0.5, "UP", None, stake=5.0)
        assert pnl is not None
        # (1-0.5)*(5/0.5) - 0.072*5 = 0.5*10 - 0.36 = 4.64
        assert abs(pnl - 4.64) < 0.001

    def test_fill_math_pnl_loss(self):
        from sim.tick_simulator import _fill_math_pnl
        pnl = _fill_math_pnl("LOSS", 0.5, "UP", None, stake=5.0)
        assert pnl == -5.0

    def test_fill_math_pnl_inferred_from_poly_winner(self):
        """Outcome inferred when outcome=None but poly_winner is available."""
        from sim.tick_simulator import _fill_math_pnl
        # direction=UP, poly_winner=UP → WIN
        pnl_win = _fill_math_pnl(None, 0.6, "UP", "UP", stake=5.0)
        assert pnl_win is not None and pnl_win > 0
        # direction=UP, poly_winner=DOWN → LOSS
        pnl_loss = _fill_math_pnl(None, 0.6, "UP", "DOWN", stake=5.0)
        assert pnl_loss == -5.0

    def test_fill_math_pnl_none_when_fill_missing(self):
        from sim.tick_simulator import _fill_math_pnl
        pnl = _fill_math_pnl("WIN", None, "UP", None, stake=5.0)
        assert pnl is None

    def test_fill_math_pnl_none_when_outcome_missing(self):
        from sim.tick_simulator import _fill_math_pnl
        pnl = _fill_math_pnl(None, 0.5, "UP", None, stake=5.0)
        assert pnl is None

    def test_ev_aggregate_totals(self):
        from sim.tick_simulator import FireEvent, _ev_aggregate

        wts = 1_777_700_000
        events = [
            FireEvent(
                window_ts=wts, evaluated_at=float(wts + 210), direction="UP",
                strategy_id="v12_lgb_combo", gate_config_hash="abc",
                regime="TRANSITION", t_band="T-61-120", session="us_open",
                outcome="WIN", ev_usd=4.64, eval_offset=120, disabled_gates="",
            ),
            FireEvent(
                window_ts=wts + 300, evaluated_at=float(wts + 510), direction="DOWN",
                strategy_id="v12_lgb_combo", gate_config_hash="abc",
                regime="CASCADE", t_band="T-121-180", session="us_open",
                outcome="LOSS", ev_usd=-5.0, eval_offset=90, disabled_gates="",
            ),
            FireEvent(
                window_ts=wts + 600, evaluated_at=float(wts + 810), direction="UP",
                strategy_id="v12_lgb_combo", gate_config_hash="abc",
                regime="TRANSITION", t_band="T-61-120", session="asian_early",
                outcome=None, ev_usd=None, eval_offset=120, disabled_gates="",
            ),
        ]
        agg = _ev_aggregate(events)
        assert agg["total"]["n_fires"] == 3
        assert agg["total"]["n_resolved"] == 2
        assert agg["total"]["n_wins"] == 1
        assert agg["total"]["n_losses"] == 1
        assert abs(agg["total"]["wr_pct"] - 50.0) < 0.1
        assert abs(agg["total"]["total_ev_usd"] - (4.64 - 5.0)) < 0.01

    def test_ev_aggregate_by_direction(self):
        from sim.tick_simulator import FireEvent, _ev_aggregate

        wts = 1_777_700_000
        events = [
            FireEvent(
                window_ts=wts, evaluated_at=float(wts + 210), direction="UP",
                strategy_id="v12_lgb_combo", gate_config_hash="abc",
                regime="NORMAL", t_band="T-61-120", session="us_open",
                outcome="WIN", ev_usd=4.64, eval_offset=120, disabled_gates="",
            ),
            FireEvent(
                window_ts=wts + 300, evaluated_at=float(wts + 510), direction="UP",
                strategy_id="v12_lgb_combo", gate_config_hash="abc",
                regime="NORMAL", t_band="T-61-120", session="us_open",
                outcome="LOSS", ev_usd=-5.0, eval_offset=120, disabled_gates="",
            ),
            FireEvent(
                window_ts=wts + 600, evaluated_at=float(wts + 810), direction="DOWN",
                strategy_id="v12_lgb_combo", gate_config_hash="abc",
                regime="CASCADE", t_band="T-181-240", session="us_pm",
                outcome="WIN", ev_usd=3.20, eval_offset=60, disabled_gates="",
            ),
        ]
        agg = _ev_aggregate(events)
        by_dir = agg["by_direction"]
        assert by_dir["UP"]["n_fires"] == 2
        assert by_dir["DOWN"]["n_fires"] == 1
        assert by_dir["DOWN"]["n_wins"] == 1

    def test_ev_aggregate_by_cell(self):
        """Per-cell key format is direction|regime|t_band|session."""
        from sim.tick_simulator import FireEvent, _ev_aggregate

        wts = 1_777_700_000
        events = [
            FireEvent(
                window_ts=wts, evaluated_at=float(wts + 210), direction="UP",
                strategy_id="v12_lgb_combo", gate_config_hash="abc",
                regime="CASCADE", t_band="T-181-240", session="us_open",
                outcome="WIN", ev_usd=4.64, eval_offset=60, disabled_gates="",
            ),
        ]
        agg = _ev_aggregate(events)
        cell_key = "UP|CASCADE|T-181-240|us_open"
        assert cell_key in agg["by_cell"]
        assert agg["by_cell"][cell_key]["n_fires"] == 1
        assert agg["by_cell"][cell_key]["n_wins"] == 1

    def test_replay_ev_computed_in_fire_events(self):
        """run_replay populates ev_usd on FireEvent from window_snapshots.outcome."""
        from sim.tick_simulator import run_replay, _DEFAULT_GATE_CONFIG

        cfg = dict(_DEFAULT_GATE_CONFIG)
        cfg["min_consecutive_pass_ticks"] = 1

        wts = 1_777_700_000
        # tick has outcome=WIN and clob_up_ask=0.55 — EV should be positive
        tick = _make_tick(
            window_ts=wts, eval_offset=90,
            clob_up_ask=0.55,
            outcome="WIN", poly_winner="UP",
        )
        fires, _ = run_replay("v12_lgb_combo", [tick], cfg, stake=5.0)
        up_fires = [f for f in fires if f.direction == "UP"]
        assert len(up_fires) > 0
        fire = up_fires[0]
        assert fire.ev_usd is not None, "Expected ev_usd to be computed"
        assert fire.ev_usd > 0, f"Expected positive EV for WIN fire, got {fire.ev_usd}"
        assert fire.outcome == "WIN"
        # Verify fill-math: (1-0.55)*(5/0.55) - 0.072*5 = 0.45*(9.09) - 0.36 ≈ 3.73
        expected_ev = (1 - 0.55) * (5.0 / 0.55) - 0.072 * 5.0
        assert abs(fire.ev_usd - expected_ev) < 0.01, (
            f"Expected EV={expected_ev:.3f} got {fire.ev_usd:.3f}"
        )

    def test_replay_ev_none_when_outcome_unresolved(self):
        """Fires with NULL outcome have ev_usd=None (not included in EV stats)."""
        from sim.tick_simulator import run_replay, _DEFAULT_GATE_CONFIG

        cfg = dict(_DEFAULT_GATE_CONFIG)
        cfg["min_consecutive_pass_ticks"] = 1

        wts = 1_777_700_000
        tick = _make_tick(
            window_ts=wts, eval_offset=90,
            outcome=None, poly_winner=None,  # unresolved
        )
        fires, _ = run_replay("v12_lgb_combo", [tick], cfg, stake=5.0)
        for fire in fires:
            assert fire.ev_usd is None, f"Expected None ev_usd for unresolved outcome, got {fire.ev_usd}"

    def test_session_label_all_hours(self):
        """All UTC hours 0-23 map to a valid session label."""
        from sim.tick_simulator import _session_label
        for h in range(24):
            label = _session_label(h)
            assert label in {"asian_early", "asian_late", "eu_am", "us_open", "us_pm", "us_late"}, (
                f"Hour {h} mapped to unexpected label: {label}"
            )


# ---------------------------------------------------------------------------
# v2: State seeding tests (CI-safe — no DB, tests logic only)
# ---------------------------------------------------------------------------

class TestStateSeedingLogic:
    """Test the seeded SimState logic used by --seed-from-prod-state."""

    def test_seeded_daily_loss_halts_when_limit_exceeded(self):
        """When seeded daily_loss exceeds limit, daily_loss_halted returns True."""
        from sim.tick_simulator import SimState
        import time

        state = SimState(strategy_id="v12_lgb_combo")
        now = time.time()
        day_key = __import__("datetime").datetime.fromtimestamp(
            now, tz=__import__("datetime").timezone.utc
        ).strftime("%Y-%m-%d")
        state.daily_loss[day_key] = 100.0  # seeded $100 loss

        assert state.daily_loss_halted(now, limit_usd=50.0)   # limit exceeded
        assert not state.daily_loss_halted(now, limit_usd=200.0)  # not exceeded
        assert not state.daily_loss_halted(now, limit_usd=None)   # no limit

    def test_seeded_dedup_blocks_replay_fire_for_same_window(self):
        """Pre-seeded dedup set prevents simulator from firing for that window."""
        from sim.tick_simulator import run_replay, SimState, _DEFAULT_GATE_CONFIG

        cfg = dict(_DEFAULT_GATE_CONFIG)
        cfg["min_consecutive_pass_ticks"] = 1

        wts = 1_777_700_000
        # Seed state with this window already fired
        state = SimState(strategy_id="v12_lgb_combo")
        state.mark_fired(wts, "UP")

        ticks = [_make_tick(window_ts=wts, eval_offset=90)]
        fires, _ = run_replay(
            "v12_lgb_combo", ticks, cfg, initial_state=state, stake=5.0
        )
        up_fires = [f for f in fires if f.direction == "UP"]
        assert len(up_fires) == 0, (
            "Seeded dedup should prevent firing for this window"
        )

    def test_seeded_cooldown_blocks_replay_fires(self):
        """Pre-seeded last_loss_epoch prevents simulator from firing during cooldown."""
        from sim.tick_simulator import run_replay, SimState, _DEFAULT_GATE_CONFIG
        import time

        cfg = dict(_DEFAULT_GATE_CONFIG)
        cfg["min_consecutive_pass_ticks"] = 1
        cfg["post_loss_cooldown_min"] = 20

        now = time.time()
        state = SimState(strategy_id="v12_lgb_combo")
        state.last_loss_epoch = now - 5 * 60  # loss 5 minutes ago

        wts = int(now // 300) * 300
        ticks = [_make_tick(window_ts=wts, eval_offset=90, evaluated_at=now)]
        fires, _ = run_replay(
            "v12_lgb_combo", ticks, cfg, initial_state=state, stake=5.0
        )
        assert len(fires) == 0, "Seeded cooldown should block replay fires"


# ---------------------------------------------------------------------------
# DB validation test (requires_db — skipped in CI)
# ---------------------------------------------------------------------------

@requires_db
class TestDBValidation:
    """7d replay of v12_lgb_combo against actual trades.

    Run manually on Montreal after deploy:
        DATABASE_URL=postgresql://... pytest \\
            tests/integration/test_tick_simulator_validation.py \\
            -m requires_db -v
    """

    @pytest.mark.skipif(_IS_TEST_DB, reason="Requires real DATABASE_URL (not test stub)")
    def test_7d_v12_lgb_combo_validation(self):
        from sim.tick_simulator import (
            _DEFAULT_GATE_CONFIG,
            _load_ticks_from_db,
            _load_actual_trades,
            run_replay,
            validate,
        )

        db_url = _DB_URL
        strategy_id = "v12_lgb_combo"
        hours = 168  # 7 days

        # Load ticks
        ticks = _load_ticks_from_db(strategy_id, hours, db_url)
        assert len(ticks) > 1000, f"Expected >1000 ticks, got {len(ticks)}"

        # Load actual trades
        actual_trades = _load_actual_trades(strategy_id, hours, db_url)
        print(f"\nActual trades: {len(actual_trades)}")

        # Run replay with production config
        cfg = dict(_DEFAULT_GATE_CONFIG)
        fires, _ = run_replay(strategy_id, ticks, cfg)
        print(f"Simulated fires: {len(fires)}")

        # Validate with 30min warmup
        warmup_cutoff = ticks[0].evaluated_at + 30 * 60
        result = validate(strategy_id, fires, actual_trades, ticks, warmup_cutoff)
        print("\n" + result.summary())

        # Hub #348 tolerance: ±5% per day, ±2 per (window_ts, direction)
        assert result.passes_tolerance, (
            f"Validation FAILED: n_sim={result.n_sim_fires}, "
            f"n_actual={result.n_actual_trades}. "
            f"See mismatches: {result.per_window_direction_deltas[:5]}"
        )
