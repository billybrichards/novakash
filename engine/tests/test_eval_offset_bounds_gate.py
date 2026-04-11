"""
Tests for engine/signals/gates.py::EvalOffsetBoundsGate (DS-01 / V10.6).

These tests enforce the two invariants that keep the v10.6 eval_offset
bounds gate SAFE on the hot path of live trading:

  1. DEFAULT-OFF NO-OP — when `V10_6_ENABLED` is unset or any
     falsey value, the gate MUST return `passed=True` for every
     possible `ctx.eval_offset` (including None, negative, zero,
     and values that would normally fail the enabled path). This
     guarantees merging this PR to `develop` is zero-behaviour-change
     in production until the operator explicitly flips the flag.

  2. ENABLED BOUNDS — when `V10_6_ENABLED=true`, the gate enforces
     an INCLUSIVE band `[V10_6_MIN_EVAL_OFFSET, V10_6_MAX_EVAL_OFFSET]`:
       - offset < min → fail with "too late" reason
       - offset > max → fail with "too early" reason
       - offset exactly at min (inclusive) → pass
       - offset exactly at max (inclusive) → pass
       - offset strictly inside the band → pass

The test is written as SIX cases, one per requirement bullet from
the DS-01 task spec. Each case uses `monkeypatch.setenv` and then
constructs a FRESH `EvalOffsetBoundsGate()` instance because the
gate reads env vars at `__init__` time (matching the pattern used
by `TakerFlowGate`, `DeltaMagnitudeGate`, etc. in gates.py).

IMPORTANT — env var naming: this test uses `V10_6_MIN_EVAL_OFFSET`
and `V10_6_MAX_EVAL_OFFSET` (namespaced under V10_6_) rather than
the unqualified names in the V10.6 proposal doc, because the
`DuneConfidenceGate` in the same file already reads
`V10_MIN_EVAL_OFFSET` with different semantics (acts as a maximum,
prod value 180/200). See the gate's docstring for the full
rationale.
"""

from __future__ import annotations

import pytest

from signals.gates import (
    EvalOffsetBoundsGate,
    GateContext,
    GateResult,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_ctx(eval_offset):
    """Build a minimal GateContext with only the field the gate reads."""
    return GateContext(eval_offset=eval_offset)


def _enable(monkeypatch, min_offset: int = 90, max_offset: int = 180):
    """Enable V10.6 and set the band, then return a FRESH gate instance.

    The gate reads env at __init__ time so we must construct AFTER
    setenv. Callers can override `min_offset` / `max_offset` but the
    defaults match the v10.6 proposal doc §3.4.
    """
    monkeypatch.setenv("V10_6_ENABLED", "true")
    monkeypatch.setenv("V10_6_MIN_EVAL_OFFSET", str(min_offset))
    monkeypatch.setenv("V10_6_MAX_EVAL_OFFSET", str(max_offset))
    return EvalOffsetBoundsGate()


def _disable(monkeypatch):
    """Explicitly disable V10.6 and return a FRESH gate instance.

    Uses delenv with raising=False to tolerate the var not being set
    in the ambient environment — this mirrors how the gate behaves
    on an untouched box (V10_6_ENABLED absent == false).
    """
    monkeypatch.delenv("V10_6_ENABLED", raising=False)
    monkeypatch.delenv("V10_6_MIN_EVAL_OFFSET", raising=False)
    monkeypatch.delenv("V10_6_MAX_EVAL_OFFSET", raising=False)
    return EvalOffsetBoundsGate()


# ─── Case 1: Default-off no-op (the critical safety invariant) ────────────────


@pytest.mark.asyncio
async def test_default_off_passes_for_every_offset(monkeypatch):
    """
    CASE 1 — DEFAULT-OFF NO-OP.

    When `V10_6_ENABLED` is unset (the default after merging this PR
    to develop until an operator flips the flag), the gate must pass
    for EVERY value of `ctx.eval_offset`, including ones that would
    fail the enabled path. This is the bit-for-bit zero-behaviour-
    change guarantee — it means the gate is equivalent to "not in
    the pipeline at all" in production until the flag is flipped.

    Values tested include:
      - `None` (would fail-closed in enabled mode)
      -   0   (would fail "too late" in enabled mode)
      -  50   (would fail "too late" in enabled mode)
      - 120   (would pass either way)
      - 200   (would fail "too early" in enabled mode)
      - 300   (would fail "too early" in enabled mode)
    """
    gate = _disable(monkeypatch)
    assert gate.name == "eval_offset_bounds"

    for offset in (None, 0, 50, 120, 200, 300):
        ctx = _make_ctx(offset)
        result = await gate.evaluate(ctx)
        assert isinstance(result, GateResult)
        assert result.passed is True, (
            f"Default-off path must pass for eval_offset={offset}, "
            f"got passed={result.passed} reason={result.reason!r}"
        )
        assert result.gate_name == "eval_offset_bounds"
        assert "disabled" in result.reason.lower()
        assert "v10_6_enabled" in result.reason.lower()


# ─── Case 2: Enabled + eval_offset below min → fail "too late" ────────────────


@pytest.mark.asyncio
async def test_enabled_below_min_fails_too_late(monkeypatch):
    """
    CASE 2 — ENABLED, offset < min.

    With `V10_6_ENABLED=true` and `V10_6_MIN_EVAL_OFFSET=90`, an
    `eval_offset=50` means we have only 50s to window close — too
    close to the wire. The gate must hard-block with a "too late"
    reason referencing both the actual and threshold offsets.
    """
    gate = _enable(monkeypatch, min_offset=90, max_offset=180)

    ctx = _make_ctx(eval_offset=50)
    result = await gate.evaluate(ctx)

    assert result.passed is False
    assert result.gate_name == "eval_offset_bounds"
    assert "too late" in result.reason.lower()
    # The reason must reference both the actual and threshold offsets
    # so operators reading live logs can diagnose instantly.
    assert "T-50" in result.reason
    assert "T-90" in result.reason
    # Structured data block also carries the raw numbers.
    assert result.data.get("offset") == 50
    assert result.data.get("min") == 90
    assert result.data.get("max") == 180


# ─── Case 3: Enabled + eval_offset above max → fail "too early" ───────────────


@pytest.mark.asyncio
async def test_enabled_above_max_fails_too_early(monkeypatch):
    """
    CASE 3 — ENABLED, offset > max.

    With `V10_6_ENABLED=true` and `V10_6_MAX_EVAL_OFFSET=180`, an
    `eval_offset=200` means we have more than 3 minutes to window
    close — too far from close. Historically this is the
    catastrophic T-180-240 bucket (47.62% WR, −33.96% ROI across
    21 tagged trades). The gate must hard-block with a "too early"
    reason.
    """
    gate = _enable(monkeypatch, min_offset=90, max_offset=180)

    ctx = _make_ctx(eval_offset=200)
    result = await gate.evaluate(ctx)

    assert result.passed is False
    assert result.gate_name == "eval_offset_bounds"
    assert "too early" in result.reason.lower()
    assert "T-200" in result.reason
    assert "T-180" in result.reason
    assert result.data.get("offset") == 200
    assert result.data.get("min") == 90
    assert result.data.get("max") == 180


# ─── Case 4: Enabled + eval_offset exactly at min (inclusive) → pass ──────────


@pytest.mark.asyncio
async def test_enabled_at_exact_min_passes(monkeypatch):
    """
    CASE 4 — ENABLED, offset == min (inclusive boundary).

    `V10_6_MIN_EVAL_OFFSET=90` is an INCLUSIVE lower bound — an
    offset of exactly 90 must pass. This is documented in the gate
    docstring and in the v10.6 proposal doc pseudocode (§3.5:
    `if eval_offset < V10_MIN_EVAL_OFFSET or eval_offset > V10_MAX_EVAL_OFFSET`).
    """
    gate = _enable(monkeypatch, min_offset=90, max_offset=180)

    ctx = _make_ctx(eval_offset=90)
    result = await gate.evaluate(ctx)

    assert result.passed is True, (
        f"offset=90 must pass with min=90 (inclusive), got "
        f"reason={result.reason!r}"
    )
    assert "safe band" in result.reason.lower()


# ─── Case 5: Enabled + eval_offset exactly at max (inclusive) → pass ──────────


@pytest.mark.asyncio
async def test_enabled_at_exact_max_passes(monkeypatch):
    """
    CASE 5 — ENABLED, offset == max (inclusive boundary).

    `V10_6_MAX_EVAL_OFFSET=180` is an INCLUSIVE upper bound — an
    offset of exactly 180 must pass. The proposal doc's pseudocode
    uses `offset > max` (strict) so 180 itself is in the safe band.
    """
    gate = _enable(monkeypatch, min_offset=90, max_offset=180)

    ctx = _make_ctx(eval_offset=180)
    result = await gate.evaluate(ctx)

    assert result.passed is True, (
        f"offset=180 must pass with max=180 (inclusive), got "
        f"reason={result.reason!r}"
    )
    assert "safe band" in result.reason.lower()


# ─── Case 6: Enabled + eval_offset strictly inside the band → pass ────────────


@pytest.mark.asyncio
async def test_enabled_in_middle_passes(monkeypatch):
    """
    CASE 6 — ENABLED, offset strictly inside [min, max].

    An offset of 120 is in the T-120 bucket which historically is
    the sweet spot (lowest ECE, +6.70pp skill in TRANSITION per the
    v10.6 proposal doc §1.1). This must pass cleanly.
    """
    gate = _enable(monkeypatch, min_offset=90, max_offset=180)

    ctx = _make_ctx(eval_offset=120)
    result = await gate.evaluate(ctx)

    assert result.passed is True, (
        f"offset=120 must pass with band [90, 180], got "
        f"reason={result.reason!r}"
    )
    assert result.data.get("offset") == 120
    assert "T-120" in result.reason


# ─── Extra safety coverage: enabled + eval_offset=None → fail-closed ──────────
#
# This isn't one of the 6 cases required by the task spec but it
# enforces the "fail-closed on missing context" invariant documented
# in the gate. If the strategy context plumbing ever drops
# `eval_offset`, we want to SKIP the trade loudly rather than silently
# pass and take a trade blind. Kept as an additional regression test.


@pytest.mark.asyncio
async def test_enabled_missing_offset_fails_closed(monkeypatch):
    """When enabled, a None eval_offset fails closed (safer than passing)."""
    gate = _enable(monkeypatch, min_offset=90, max_offset=180)

    ctx = _make_ctx(eval_offset=None)
    result = await gate.evaluate(ctx)

    assert result.passed is False
    assert "missing" in result.reason.lower()
    assert result.data.get("offset") is None
