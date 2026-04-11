"""
Tests for engine/signals/gates.py::SourceAgreementGate — DQ-01 spot-only mode.

These tests enforce the safety invariants that keep the
V11_POLY_SPOT_ONLY_CONSENSUS flag SAFE on the hot path of live
Polymarket trading:

  1. DEFAULT-OFF NO-OP — when `V11_POLY_SPOT_ONLY_CONSENSUS` is unset
     or any falsey value, the gate MUST behave bit-for-bit identically
     to the legacy v11.1 2/3 majority vote (CL+TI+BIN). This guarantees
     merging this PR to `develop` is zero-behaviour-change in
     production until the operator explicitly flips the flag.

  2. SPOT-ONLY ENABLED — when `V11_POLY_SPOT_ONLY_CONSENSUS=true`,
     the gate ignores `delta_binance` entirely in the consensus vote
     and requires unanimous agreement between Chainlink and Tiingo.
     The SAME CL/TI/BIN windows that pass under the 2/3 rule will
     fail with `spot_disagree` if CL and TI call different
     directions, regardless of what Binance says.

Each case monkeypatches `V11_POLY_SPOT_ONLY_CONSENSUS` and then
constructs a FRESH `SourceAgreementGate()` instance, because the
gate reads env vars at `__init__` time (matching the pattern used
by `EvalOffsetBoundsGate`, `TakerFlowGate`, `DeltaMagnitudeGate`,
etc. in gates.py).

IMPORTANT — gate evaluation ordering: in the prod pipeline the
EvalOffsetBoundsGate runs G0 before this G1, but that ordering is
irrelevant for these tests. We are exercising this gate in
isolation.
"""

from __future__ import annotations

import pytest

from signals.gates import (
    GateContext,
    GateResult,
    SourceAgreementGate,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _enable_spot_only(monkeypatch):
    """Flip V11_POLY_SPOT_ONLY_CONSENSUS=true and return a FRESH gate.

    The gate reads env at __init__ so callers MUST construct after
    setenv, not before.
    """
    monkeypatch.setenv("V11_POLY_SPOT_ONLY_CONSENSUS", "true")
    return SourceAgreementGate()


def _disable_spot_only(monkeypatch):
    """Explicitly disable spot-only mode and return a FRESH gate.

    Uses delenv with raising=False so the test still works on an
    untouched box where the env var has never been set.
    """
    monkeypatch.delenv("V11_POLY_SPOT_ONLY_CONSENSUS", raising=False)
    return SourceAgreementGate()


def _ctx(cl: float | None, ti: float | None, bn: float | None) -> GateContext:
    """Build a minimal GateContext with only the three delta fields the
    source agreement gate consumes."""
    return GateContext(
        delta_chainlink=cl,
        delta_tiingo=ti,
        delta_binance=bn,
    )


# ─── Case 1: Default-off preserves legacy 2/3 majority behaviour ──────────────


@pytest.mark.asyncio
async def test_default_off_matches_legacy_two_thirds_rule(monkeypatch):
    """
    CASE 1 — DEFAULT-OFF NO-OP.

    When V11_POLY_SPOT_ONLY_CONSENSUS is unset, the gate must return
    the v11.1 2/3 majority verdict for every CL/TI/BIN combination,
    including the pathological CL=UP, TI=DOWN, BIN=DOWN case (which
    is precisely the regression DQ-01 was written to prevent — but
    only AFTER the operator flips the flag, not before).

    This is the bit-for-bit zero-behaviour-change guarantee.
    """
    gate = _disable_spot_only(monkeypatch)
    assert gate.name == "source_agreement"

    # Sub-case 1a: all three UP → 3/3 UP, should pass as UP
    result = await gate.evaluate(_ctx(cl=1.0, ti=1.0, bn=1.0))
    assert result.passed is True
    assert result.data["direction"] == "UP"
    assert result.data["up_votes"] == 3

    # Sub-case 1b: all three DOWN → 3/3 DOWN, should pass as DOWN
    result = await gate.evaluate(_ctx(cl=-1.0, ti=-1.0, bn=-1.0))
    assert result.passed is True
    assert result.data["direction"] == "DOWN"
    assert result.data["down_votes"] == 3

    # Sub-case 1c: the regression case — CL=UP, TI=DOWN, BIN=DOWN.
    # Under v11.1 2/3 rule this passes as DOWN (Binance tiebreaker
    # + TI outweighs CL). Case 3 below proves this exact window
    # FAILS once the spot-only flag is flipped.
    result = await gate.evaluate(_ctx(cl=1.0, ti=-1.0, bn=-1.0))
    assert result.passed is True, (
        "Default-off path must preserve v11.1 2/3 behaviour, but CL=UP "
        f"TI=DOWN BIN=DOWN returned passed={result.passed} "
        f"reason={result.reason!r}"
    )
    assert result.data["direction"] == "DOWN"
    assert result.data["down_votes"] == 2
    assert result.data["up_votes"] == 1
    # Mode must NOT be tagged as spot_only in legacy path
    assert "mode" not in result.data or result.data.get("mode") != "spot_only"
    assert "2/3" in result.reason

    # Sub-case 1d: CL=UP, TI=UP, BIN=DOWN → 2/3 UP, passes as UP
    result = await gate.evaluate(_ctx(cl=1.0, ti=1.0, bn=-1.0))
    assert result.passed is True
    assert result.data["direction"] == "UP"
    assert result.data["up_votes"] == 2

    # Sub-case 1e: missing CL → fail with missing-data reason
    # (same fail-closed path as legacy)
    result = await gate.evaluate(_ctx(cl=None, ti=1.0, bn=1.0))
    assert result.passed is False
    assert "missing" in result.reason.lower()

    # Sub-case 1f: missing TI → fail with missing-data reason
    result = await gate.evaluate(_ctx(cl=1.0, ti=None, bn=1.0))
    assert result.passed is False
    assert "missing" in result.reason.lower()


# ─── Case 2: Spot-only with CL=UP TI=UP BIN=DOWN → pass as UP ────────────────


@pytest.mark.asyncio
async def test_spot_only_cl_ti_agree_ignores_binance_dissent(monkeypatch):
    """
    CASE 2 — ENABLED, CL + TI agree UP, BIN calls DOWN.

    Under the legacy 2/3 rule this window passes as UP because CL
    and TI outvote BIN. Under spot-only mode the outcome is the
    same (UP) but arrived at for a different reason: BIN is NOT
    consulted at all. The test pins that the decision still passes
    as UP AND that the result data tags the mode as `spot_only`
    and contains no `bin_dir` key (proving Binance was never read).
    """
    gate = _enable_spot_only(monkeypatch)

    result = await gate.evaluate(_ctx(cl=1.0, ti=1.0, bn=-1.0))

    assert isinstance(result, GateResult)
    assert result.passed is True
    assert result.gate_name == "source_agreement"
    assert result.data["direction"] == "UP"
    assert result.data["mode"] == "spot_only"
    assert result.data["cl_dir"] == "UP"
    assert result.data["ti_dir"] == "UP"
    # Spot-only payload must NOT leak a bin_dir field — it's the
    # visible proof that BIN never entered the decision.
    assert "bin_dir" not in result.data
    # Reason string is for live-log grepping: must say spot-only + UP
    assert "spot-only" in result.reason.lower()
    assert "UP" in result.reason


# ─── Case 3: Spot-only with CL=UP TI=DOWN BIN=DOWN → FAIL (the regression) ────


@pytest.mark.asyncio
async def test_spot_only_disagreement_fails_even_if_binance_tiebreaks(monkeypatch):
    """
    CASE 3 — ENABLED, the pathological v11.1 regression window.

    CL=UP, TI=DOWN, BIN=DOWN is 19.6% of all historical evaluations
    and is the exact window type the DQ-01 flag is meant to skip.
    Under the legacy 2/3 rule this passes as DOWN because Binance's
    biased DOWN call sides with TI and creates a 2-vote majority.
    Under spot-only mode the gate must FAIL because the two
    un-contaminated spot sources disagree — it is the rule that
    kills the structural-bias contamination.
    """
    gate = _enable_spot_only(monkeypatch)

    result = await gate.evaluate(_ctx(cl=1.0, ti=-1.0, bn=-1.0))

    assert result.passed is False
    assert result.gate_name == "source_agreement"
    assert result.data["mode"] == "spot_only"
    assert result.data["cl_dir"] == "UP"
    assert result.data["ti_dir"] == "DOWN"
    # Disagreement payload must NOT leak a bin_dir field either.
    assert "bin_dir" not in result.data
    assert "spot disagree" in result.reason.lower() or "disagree" in result.reason.lower()
    assert "spot-only" in result.reason.lower()


# ─── Case 4: Spot-only, both DOWN, Binance UP → pass as DOWN ─────────────────


@pytest.mark.asyncio
async def test_spot_only_both_spot_down_ignores_binance_up(monkeypatch):
    """
    CASE 4 — ENABLED, CL + TI both DOWN, BIN calls UP.

    Mirrors Case 2 but in the opposite direction, AND tests the
    rarer direction for Binance (UP calls are the 16.9% minority
    per the v11.1 changelog evidence table). The gate must pass
    as DOWN without touching BIN.
    """
    gate = _enable_spot_only(monkeypatch)

    result = await gate.evaluate(_ctx(cl=-0.5, ti=-0.3, bn=0.8))

    assert result.passed is True
    assert result.data["direction"] == "DOWN"
    assert result.data["mode"] == "spot_only"
    assert result.data["cl_dir"] == "DOWN"
    assert result.data["ti_dir"] == "DOWN"
    assert "bin_dir" not in result.data


# ─── Case 5: Spot-only with Binance missing entirely → still works ────────────


@pytest.mark.asyncio
async def test_spot_only_tolerates_missing_binance(monkeypatch):
    """
    CASE 5 — ENABLED, delta_binance is None.

    In legacy 2/3 mode a missing `delta_binance` is coerced to
    DOWN (see gates.py bin_dir logic). In spot-only mode Binance
    is irrelevant — the gate must pass on the spot-only CL/TI
    agreement whether BIN is present, absent, zero, or garbage.

    This is a live-incident safety case: if the Binance futures
    websocket drops, spot-only mode must still allow the engine
    to trade off the two healthy spot sources. The v11.1 2/3
    rule would also still work here (BIN coerces to DOWN) but
    with different semantics — we must prove BOTH modes survive
    BIN-None.
    """
    gate = _enable_spot_only(monkeypatch)

    # Both spot sources UP → pass as UP
    result = await gate.evaluate(_ctx(cl=1.0, ti=1.0, bn=None))
    assert result.passed is True
    assert result.data["direction"] == "UP"
    assert result.data["mode"] == "spot_only"

    # Both spot sources DOWN → pass as DOWN
    result = await gate.evaluate(_ctx(cl=-1.0, ti=-1.0, bn=None))
    assert result.passed is True
    assert result.data["direction"] == "DOWN"

    # Spot sources disagree → fail (BIN-None doesn't become a
    # tiebreaker because we ignore BIN entirely in this mode)
    result = await gate.evaluate(_ctx(cl=1.0, ti=-1.0, bn=None))
    assert result.passed is False
    assert result.data["mode"] == "spot_only"


# ─── Case 6: Spot-only missing CL or TI → fail-closed ───────────────────────


@pytest.mark.asyncio
async def test_spot_only_fails_closed_when_spot_source_missing(monkeypatch):
    """
    CASE 6 — ENABLED, one of the two spot sources is None.

    Spot-only mode depends entirely on CL + TI. If either drops out,
    there is no fallback (Binance is ignored) and the gate must
    fail-closed with the existing `missing CL or TI data` reason
    string. This is the "don't trade blind" invariant.
    """
    gate = _enable_spot_only(monkeypatch)

    result = await gate.evaluate(_ctx(cl=None, ti=1.0, bn=1.0))
    assert result.passed is False
    assert "missing" in result.reason.lower()

    result = await gate.evaluate(_ctx(cl=1.0, ti=None, bn=-1.0))
    assert result.passed is False
    assert "missing" in result.reason.lower()


# ─── Case 7: Flag value parsing is case-insensitive ──────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_value", ["true", "TRUE", "True", "tRuE"])
async def test_spot_only_flag_case_insensitive(monkeypatch, raw_value):
    """
    CASE 7 — flag value parsing.

    Matches the EvalOffsetBoundsGate pattern: any case-variant of
    'true' enables the flag. Any other string (including 'yes',
    '1', 'enabled') remains disabled by the existing
    `.lower() == "true"` guard. This test pins only the case-
    insensitivity contract, not the stricter string-only rule —
    case 8 below covers that.
    """
    monkeypatch.setenv("V11_POLY_SPOT_ONLY_CONSENSUS", raw_value)
    gate = SourceAgreementGate()

    # Regression window: CL=UP TI=DOWN BIN=DOWN.
    # Mode A would pass as DOWN, Mode B fails. A passing result
    # here would prove the flag parser accepted the raw value.
    result = await gate.evaluate(_ctx(cl=1.0, ti=-1.0, bn=-1.0))
    assert result.passed is False
    assert result.data.get("mode") == "spot_only"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_value", ["1", "yes", "on", "enabled", "", "false"])
async def test_non_true_flag_values_leave_legacy_behaviour(monkeypatch, raw_value):
    """
    CASE 8 — anything other than case-insensitive 'true' is DISABLED.

    This is the inverse of Case 7: we pin that the flag does NOT
    accept common boolean-ish strings like '1' or 'yes', because
    the gate's guard is an exact `.lower() == "true"` check. If
    someone in the future loosens the parser we want this test
    to fail loudly so the change is deliberate.
    """
    monkeypatch.setenv("V11_POLY_SPOT_ONLY_CONSENSUS", raw_value)
    gate = SourceAgreementGate()

    # Regression window: CL=UP TI=DOWN BIN=DOWN.
    # Mode A passes as DOWN — confirms we're on the legacy path.
    result = await gate.evaluate(_ctx(cl=1.0, ti=-1.0, bn=-1.0))
    assert result.passed is True
    assert result.data.get("direction") == "DOWN"
    # bin_dir key proves we're on the legacy 2/3 path
    assert "bin_dir" in result.data
    assert result.data.get("mode") != "spot_only"
