"""Tests for CA-03: Immutable GateContext pipeline (ENGINE_IMMUTABLE_GATES).

Verifies that the immutable pipeline path produces IDENTICAL gate decisions
to the legacy mutable path for all gate combinations and context states.
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))
import copy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import AsyncMock
import pytest
from signals.gates import (
    CGConfirmationGate, DeltaMagnitudeGate, DuneConfidenceGate,
    DynamicCapGate, EMPTY_DELTA, EvalOffsetBoundsGate, GateContext,
    GateContextDelta, GatePipeline, GateResult, PipelineResult,
    SourceAgreementGate, SpreadGate, TakerFlowGate, _infer_delta, _merge_context,
)

@dataclass
class FakeCGSnapshot:
    connected: bool = True
    taker_buy_volume_1m: float = 5_000_000
    taker_sell_volume_1m: float = 3_000_000
    top_position_short_pct: float = 48.0
    top_position_long_pct: float = 52.0
    funding_rate: float = 0.0001
    long_pct: float = 50.0
    short_pct: float = 50.0
    oi_delta_pct_1m: float = 0.5
    long_short_ratio: float = 1.1
    timestamp: Optional[datetime] = None
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc)

def _make_ctx(**ov) -> GateContext:
    d = dict(delta_chainlink=0.05, delta_tiingo=0.03, delta_binance=0.04,
             delta_pct=0.05, vpin=0.55, regime="TRANSITION", asset="BTC",
             eval_offset=120, gamma_up_price=0.55, gamma_down_price=0.45)
    d.update(ov)
    return GateContext(**d)

def _dune_client(p_up=0.78):
    c = AsyncMock()
    c.score_with_features = AsyncMock(return_value={
        "probability_up": p_up, "model_version": "oak-v5.2-test", "confidence": 0.85})
    return c

def _pipeline(dune_client=None, taker=False):
    gates = [SourceAgreementGate(), DeltaMagnitudeGate()]
    if taker:
        gates += [TakerFlowGate(), CGConfirmationGate()]
    gates += [DuneConfidenceGate(dune_client=dune_client), SpreadGate(), DynamicCapGate()]
    return GatePipeline(gates)

class TestGateContextDelta:
    def test_frozen(self):
        d = GateContextDelta(agreed_direction="UP")
        with pytest.raises(Exception):
            d.agreed_direction = "DOWN"

    def test_empty_delta_identity(self):
        ctx = _make_ctx()
        assert _merge_context(ctx, EMPTY_DELTA) is ctx

    def test_merge_applies_fields(self):
        ctx = _make_ctx()
        merged = _merge_context(ctx, GateContextDelta(agreed_direction="UP", cg_threshold_modifier=0.05))
        assert merged.agreed_direction == "UP"
        assert merged.cg_threshold_modifier == 0.05
        assert ctx.agreed_direction is None

    def test_merge_preserves_originals(self):
        ctx = _make_ctx(vpin=0.72, regime="CASCADE")
        merged = _merge_context(ctx, GateContextDelta(agreed_direction="UP"))
        assert merged.vpin == 0.72 and merged.regime == "CASCADE"

class TestInferDelta:
    def test_no_changes(self):
        assert _infer_delta(_make_ctx(), replace(_make_ctx())) is EMPTY_DELTA

    def test_detects_changes(self):
        before = _make_ctx()
        after = replace(before)
        after.agreed_direction = "UP"
        after.cg_bonus = 0.03
        d = _infer_delta(before, after)
        assert d.agreed_direction == "UP" and d.cg_bonus == 0.03

class TestSourceAgreementImmutable:
    @pytest.mark.asyncio
    async def test_up(self):
        r, d = await SourceAgreementGate().evaluate_immutable(_make_ctx(delta_chainlink=0.1, delta_tiingo=0.2))
        assert r.passed and d.agreed_direction == "UP"

    @pytest.mark.asyncio
    async def test_down(self):
        r, d = await SourceAgreementGate().evaluate_immutable(_make_ctx(delta_chainlink=-0.1, delta_tiingo=-0.2, delta_binance=-0.3))
        assert r.passed and d.agreed_direction == "DOWN"

    @pytest.mark.asyncio
    async def test_missing(self):
        r, d = await SourceAgreementGate().evaluate_immutable(_make_ctx(delta_chainlink=None, delta_tiingo=None))
        assert not r.passed and d is EMPTY_DELTA

class TestTakerFlowImmutable:
    @pytest.mark.asyncio
    async def test_disabled(self, monkeypatch):
        monkeypatch.setenv("V10_CG_TAKER_GATE", "false")
        r, d = await TakerFlowGate().evaluate_immutable(_make_ctx(agreed_direction="UP", cg_snapshot=FakeCGSnapshot()))
        assert r.passed and d is EMPTY_DELTA

    @pytest.mark.asyncio
    async def test_opposing(self, monkeypatch):
        monkeypatch.setenv("V10_CG_TAKER_GATE", "true")
        cg = FakeCGSnapshot(taker_buy_volume_1m=2e6, taker_sell_volume_1m=8e6)
        r, d = await TakerFlowGate().evaluate_immutable(_make_ctx(agreed_direction="UP", cg_snapshot=cg))
        assert r.passed and d.cg_threshold_modifier == 0.05

class TestCGConfirmImmutable:
    @pytest.mark.asyncio
    async def test_no_cg(self):
        r, d = await CGConfirmationGate().evaluate_immutable(_make_ctx(agreed_direction="UP", cg_snapshot=None))
        assert r.passed and d is EMPTY_DELTA

    @pytest.mark.asyncio
    async def test_confirms(self):
        cg = FakeCGSnapshot(taker_buy_volume_1m=6e6, taker_sell_volume_1m=4e6, oi_delta_pct_1m=0.5, long_short_ratio=1.2)
        r, d = await CGConfirmationGate().evaluate_immutable(_make_ctx(agreed_direction="UP", cg_snapshot=cg))
        assert r.passed and d.cg_confirms == 3 and d.cg_bonus == 0.03

class TestDuneImmutable:
    @pytest.mark.asyncio
    async def test_sets_delta(self, monkeypatch):
        monkeypatch.setenv("V10_MIN_EVAL_OFFSET", "300")
        ctx = _make_ctx(agreed_direction="UP")
        r, d = await DuneConfidenceGate(dune_client=_dune_client(0.78)).evaluate_immutable(ctx)
        assert d.dune_probability_up == 0.78 and d.dune_direction == "UP" and ctx.dune_probability_up is None

    @pytest.mark.asyncio
    async def test_no_direction(self):
        r, d = await DuneConfidenceGate(dune_client=_dune_client()).evaluate_immutable(_make_ctx(agreed_direction=None))
        assert not r.passed and d is EMPTY_DELTA

class TestNonMutatingGates:
    @pytest.mark.asyncio
    async def test_eval_offset(self):
        _, d = await EvalOffsetBoundsGate().evaluate_immutable(_make_ctx())
        assert d is EMPTY_DELTA

    @pytest.mark.asyncio
    async def test_delta_magnitude(self):
        _, d = await DeltaMagnitudeGate().evaluate_immutable(_make_ctx())
        assert d is EMPTY_DELTA

    @pytest.mark.asyncio
    async def test_spread(self):
        _, d = await SpreadGate().evaluate_immutable(_make_ctx())
        assert d is EMPTY_DELTA

    @pytest.mark.asyncio
    async def test_dynamic_cap(self):
        _, d = await DynamicCapGate().evaluate_immutable(_make_ctx())
        assert d is EMPTY_DELTA

def _assert_parity(m, i, label=""):
    p = f"[{label}] " if label else ""
    assert m.passed == i.passed, f"{p}passed"
    assert m.direction == i.direction, f"{p}direction"
    assert m.cap == i.cap, f"{p}cap"
    assert m.failed_gate == i.failed_gate, f"{p}failed_gate"
    assert len(m.gate_results) == len(i.gate_results), f"{p}gate count"
    for j, (mr, ir) in enumerate(zip(m.gate_results, i.gate_results)):
        assert mr.passed == ir.passed and mr.gate_name == ir.gate_name, f"{p}gate[{j}]"

class TestPipelineParity:
    @pytest.mark.asyncio
    async def test_all_pass(self, monkeypatch):
        monkeypatch.setenv("V10_MIN_EVAL_OFFSET", "300")
        rm = await _pipeline(dune_client=_dune_client(0.78))._evaluate_mutable(_make_ctx())
        ri = await _pipeline(dune_client=_dune_client(0.78))._evaluate_immutable(_make_ctx())
        _assert_parity(rm, ri, "all_pass")

    @pytest.mark.asyncio
    async def test_early_fail(self):
        rm = await _pipeline()._evaluate_mutable(_make_ctx(delta_chainlink=None, delta_tiingo=None))
        ri = await _pipeline()._evaluate_immutable(_make_ctx(delta_chainlink=None, delta_tiingo=None))
        _assert_parity(rm, ri, "early_fail")
        assert rm.failed_gate == "source_agreement"

    @pytest.mark.asyncio
    async def test_dune_fail(self, monkeypatch):
        monkeypatch.setenv("V10_MIN_EVAL_OFFSET", "300")
        rm = await _pipeline(dune_client=_dune_client(0.51))._evaluate_mutable(_make_ctx())
        ri = await _pipeline(dune_client=_dune_client(0.51))._evaluate_immutable(_make_ctx())
        _assert_parity(rm, ri, "dune_fail")
        assert rm.failed_gate == "dune_confidence"

    @pytest.mark.asyncio
    async def test_with_taker(self, monkeypatch):
        monkeypatch.setenv("V10_CG_TAKER_GATE", "true")
        monkeypatch.setenv("V10_MIN_EVAL_OFFSET", "300")
        cg = FakeCGSnapshot(taker_buy_volume_1m=6e6, taker_sell_volume_1m=4e6, oi_delta_pct_1m=0.5, long_short_ratio=1.2)
        rm = await _pipeline(dune_client=_dune_client(0.80), taker=True)._evaluate_mutable(_make_ctx(cg_snapshot=cg))
        ri = await _pipeline(dune_client=_dune_client(0.80), taker=True)._evaluate_immutable(_make_ctx(cg_snapshot=cg))
        _assert_parity(rm, ri, "taker")

    @pytest.mark.asyncio
    async def test_down_direction(self, monkeypatch):
        monkeypatch.setenv("V10_MIN_EVAL_OFFSET", "300")
        kw = dict(delta_chainlink=-0.05, delta_tiingo=-0.03, delta_binance=-0.04)
        rm = await _pipeline(dune_client=_dune_client(0.22))._evaluate_mutable(_make_ctx(**kw))
        ri = await _pipeline(dune_client=_dune_client(0.22))._evaluate_immutable(_make_ctx(**kw))
        _assert_parity(rm, ri, "down")

class TestContextImmutability:
    @pytest.mark.asyncio
    async def test_pipeline_does_not_mutate_input(self, monkeypatch):
        monkeypatch.setenv("V10_MIN_EVAL_OFFSET", "300")
        ctx = _make_ctx()
        fields = ("agreed_direction", "cg_threshold_modifier", "cg_confirms",
                   "cg_bonus", "dune_probability_up", "dune_direction", "dune_model_version")
        before = {f: getattr(ctx, f) for f in fields}
        await _pipeline(dune_client=_dune_client(0.80))._evaluate_immutable(ctx)
        after = {f: getattr(ctx, f) for f in fields}
        assert before == after, f"Mutated: {[k for k in before if before[k] != after[k]]}"
