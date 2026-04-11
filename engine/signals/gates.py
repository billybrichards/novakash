"""
v10.5 Gate System — Full decision surface with CoinGlass taker flow + delta magnitude.

8-gate pipeline: Agreement → DeltaMagnitude → TakerFlow → CGConfirmation → DUNE → Spread → DynamicCap.

Based on:
  - v10.1 Decision Surface spec (timesfm repo, 909 lines)
  - CG alignment data (719 trades): taker aligned = 81.7% WR, both opposing = 58.3%
  - ELM v3 calibration (865 windows): P>=0.65 = 78.4% acc, DOWN -9.3pp

Usage:
    gates = GatePipeline([
        SourceAgreementGate(),
        TakerFlowGate(),
        CGConfirmationGate(),
        DuneConfidenceGate(dune_client=timesfm_v2),
        SpreadGate(),
        DynamicCapGate(),
    ])
    result = await gates.evaluate(context)
    if result.passed:
        # Trade at result.data['cap']
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Optional, Protocol, Union

import structlog

log = structlog.get_logger(__name__)


# ── Result Types ────────────────────────────────────────────────────────────

@dataclass
class GateResult:
    """Result from a single gate evaluation."""
    passed: bool
    gate_name: str
    reason: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class PipelineResult:
    """Result from the full gate pipeline."""
    passed: bool
    direction: Optional[str] = None       # UP or DOWN
    cap: Optional[float] = None           # Dynamic entry cap
    dune_p: Optional[float] = None        # DUNE model probability
    gate_results: list[GateResult] = field(default_factory=list)
    failed_gate: Optional[str] = None     # Name of first gate that failed
    skip_reason: Optional[str] = None     # Human-readable reason


@dataclass
class GateContext:
    """All data needed by gates for evaluation."""
    # Price deltas
    delta_chainlink: Optional[float] = None
    delta_tiingo: Optional[float] = None
    delta_binance: Optional[float] = None
    delta_pct: float = 0.0

    # VPIN / regime
    vpin: float = 0.0
    regime: str = "UNKNOWN"

    # Window info
    asset: str = "BTC"
    eval_offset: Optional[int] = None
    window_ts: Optional[int] = None

    # DUNE model data (populated by DuneConfidenceGate)
    dune_probability_up: Optional[float] = None
    dune_direction: Optional[str] = None
    dune_model_version: Optional[str] = None

    # CoinGlass snapshot
    cg_snapshot: Optional[object] = None

    # Direction from agreement gate
    agreed_direction: Optional[str] = None

    # v10.3: CoinGlass modifiers (set by TakerFlowGate + CGConfirmationGate)
    cg_threshold_modifier: float = 0.0  # +0.05 penalty or -0.02 bonus from taker flow
    cg_confirms: int = 0                # 0-3 confirming CG signals
    cg_bonus: float = 0.0              # Confirmation bonus (subtracted from threshold)

    # Gamma / Polymarket CLOB prices (for spread gate)
    gamma_up_price: Optional[float] = None
    gamma_down_price: Optional[float] = None

    # v11.1: extra scalars needed to build the v5 push-mode feature body
    # from inside the DUNE gate. The strategy populates these at context
    # construction time (near five_min_vpin.py:624) so that
    # DuneConfidenceGate can build an identical V5FeatureBody to the one
    # the strategy's own v8.1 fetch builds. Train/serve parity across
    # both decision-path call sites depends on these being set.
    twap_delta: Optional[float] = None        # TWAP delta percentage from twap_result
    tiingo_close: Optional[float] = None      # Tiingo REST candle close price
    current_price: Optional[float] = None     # Binance last price at eval time
    chainlink_price: Optional[float] = None   # Chainlink spot (when available)
    delta_source: Optional[str] = None        # "binance" | "chainlink" | "tiingo" raw string
    prev_v2_probability_up: Optional[float] = None  # For v2_logit — prior scorer output

    # v11.1: Pre-built V5 feature body, attached by the strategy. When
    # set, DuneConfidenceGate calls the scorer with this body directly
    # instead of rebuilding one from the scalar GateContext fields.
    # None means "build it from scalars at gate time" as a fallback,
    # which happens if the strategy didn't populate it (old callers,
    # test harnesses, etc.). Typed as Any to avoid a circular import
    # with signals.v2_feature_body — the DUNE gate type-checks it
    # locally when it uses it.
    v5_features: Optional[object] = None




# -- Feature flag: immutable gate context (CA-03) -------------------------

_IMMUTABLE_GATES = os.environ.get("ENGINE_IMMUTABLE_GATES", "false").lower() == "true"


@dataclass(frozen=True)
class GateContextDelta:
    """Immutable delta capturing fields a gate wants to change on GateContext.

    Only fields that are not None are applied during merge. Gates that
    do not modify context return EMPTY_DELTA.
    """
    agreed_direction: Optional[str] = None
    cg_threshold_modifier: Optional[float] = None
    cg_confirms: Optional[int] = None
    cg_bonus: Optional[float] = None
    dune_probability_up: Optional[float] = None
    dune_direction: Optional[str] = None
    dune_model_version: Optional[str] = None


EMPTY_DELTA = GateContextDelta()


def _merge_context(ctx: GateContext, delta: GateContextDelta) -> GateContext:
    """Return a NEW GateContext with delta fields applied."""
    overrides = {}
    for fn in (
        "agreed_direction", "cg_threshold_modifier", "cg_confirms",
        "cg_bonus", "dune_probability_up", "dune_direction", "dune_model_version",
    ):
        val = getattr(delta, fn)
        if val is not None:
            overrides[fn] = val
    if not overrides:
        return ctx
    return replace(ctx, **overrides)


# ── Gate Protocol ───────────────────────────────────────────────────────────

class Gate(Protocol):
    """Protocol for gate implementations.

    CA-03: When ENGINE_IMMUTABLE_GATES=true, GatePipeline calls
    evaluate_immutable() if present, falling back to evaluate()
    with a mutable copy for unmigrated gates.
    """
    name: str
    async def evaluate(self, ctx: GateContext) -> GateResult: ...


# ── V10.6 Eval Offset Bounds Gate (DS-01) ───────────────────────────────────

class EvalOffsetBoundsGate:
    """G0: V10.6 hard eval_offset bounds — default OFF.

    The V10.6 decision surface (docs/V10_6_DECISION_SURFACE_PROPOSAL.md
    in the novakash-timesfm-repo, §3.4) identifies a single safe
    tradeable window `[V10_6_MIN_EVAL_OFFSET, V10_6_MAX_EVAL_OFFSET]`
    inside which Sequoia v5's calibration holds. Trades outside the
    window are unconditionally skipped because:

      - eval_offset > max → too far from close → model calibration not
        yet resolved. Empirical: T-180-240 had 47.62% WR and −33.96%
        ROI across 865 resolved v4 predictions + 21 tagged live trades.
      - eval_offset < min → too close to close → insufficient reaction
        time for the strategy to react if conditions flip. v10.6 sets
        this floor at T-90.

    This is DS-01 in the audit checklist — the single simplest V10.6
    component. The full grid (per-regime min_p, UP penalty, proportional
    sizing, confidence haircut) is NOT implemented here; those are
    tracked as separate audit tasks.

    ⚠ SAFETY — default OFF ⚠

    This gate is gated by `V10_6_ENABLED` and defaults to `false`. When
    disabled (the default after merging this PR to develop → EC2), the
    gate is a pure no-op that returns `passed=True` unconditionally and
    never even reads `ctx.eval_offset`. The existing 7-gate pipeline
    (SourceAgreement → DeltaMagnitude → TakerFlow → CGConfirmation →
    DuneConfidence → Spread → DynamicCap) remains bit-for-bit
    unchanged in its trading behaviour.

    The operator flips `V10_6_ENABLED=true` on the host to turn the
    hard-block logic on. This matches the "default off + operator flip"
    pattern used for `MARGIN_ENGINE_USE_V4_ACTIONS` in PR #16.

    ⚠ ENV VAR NAMING — deliberate divergence from the V10.6 proposal ⚠

    The V10.6 proposal doc §3.4 names the env vars `V10_MIN_EVAL_OFFSET`
    and `V10_MAX_EVAL_OFFSET`. Those names are already in use by the
    existing `DuneConfidenceGate` (gates.py line ~358) with different
    semantics — there, `V10_MIN_EVAL_OFFSET` acts as a MAXIMUM offset
    (blocks `ctx.eval_offset > _min_offset`, currently set to 180/200
    in production). Re-using the same name here would silently
    repurpose the variable on flag flip and immediately break trading.

    To protect against that foot-gun, this gate uses NAMESPACED env
    vars `V10_6_MIN_EVAL_OFFSET` / `V10_6_MAX_EVAL_OFFSET`. A separate
    follow-up task can rename or consolidate once all v10.6 components
    land. The defaults (90 and 180) match the proposal doc exactly.

    Env vars:
      - `V10_6_ENABLED`            — master flag. Default `false`.
      - `V10_6_MIN_EVAL_OFFSET`    — lower bound, inclusive. Default 90.
      - `V10_6_MAX_EVAL_OFFSET`    — upper bound, inclusive. Default 180.

    Evidence:
      865 resolved Polymarket outcomes analysed against v4 predictions:
        T-60-120:   105 trades, 67.62% WR, −2.23% ROI (near-breakeven)
        T-120-180:   72 trades, 55.56% WR, −13.39% ROI
        T-180-240:   21 trades, 47.62% WR, −33.96% ROI (catastrophic)
      Only the T-90-180 band is reliable under v4/v5 calibration.
    """
    name = "eval_offset_bounds"

    def __init__(self):
        # Read env vars at gate construction time (module import time),
        # same pattern as DeltaMagnitudeGate, TakerFlowGate, etc. This
        # means operator must restart the engine to pick up flag
        # changes — matches existing gate ergonomics.
        self._enabled = os.environ.get("V10_6_ENABLED", "false").lower() == "true"
        self._min_offset = int(os.environ.get("V10_6_MIN_EVAL_OFFSET", "90"))
        self._max_offset = int(os.environ.get("V10_6_MAX_EVAL_OFFSET", "180"))
        self._log = log.bind(gate="eval_offset_bounds")

    async def evaluate(self, ctx: GateContext) -> GateResult:
        # ── SAFETY: default-off no-op path ──
        # When V10_6_ENABLED is false, this gate is a pure pass-through.
        # It deliberately does NOT read ctx.eval_offset or any other
        # context field — the goal is to be as close to "not in the
        # pipeline at all" as possible while still being wired up. This
        # guarantees zero behaviour change in production after merging
        # this PR to develop, until an operator flips the flag.
        if not self._enabled:
            return GateResult(
                passed=True, gate_name=self.name,
                reason="disabled (V10_6_ENABLED=false)",
            )

        # ── ENABLED: hard-block outside [min, max] ──
        offset = ctx.eval_offset

        # Missing eval_offset: fail closed. Better to skip one trade
        # than to take a bad trade because the context plumbing
        # dropped the field — but log it so telemetry catches the bug.
        if offset is None:
            self._log.info(
                "gate.v10_6_eval_offset_blocked",
                offset=None,
                min=self._min_offset,
                max=self._max_offset,
                reason="missing eval_offset",
            )
            return GateResult(
                passed=False, gate_name=self.name,
                reason="V10.6: missing eval_offset (fail-closed)",
                data={"offset": None, "min": self._min_offset, "max": self._max_offset},
            )

        # Below min: too close to close, not enough reaction time
        if offset < self._min_offset:
            reason = f"V10.6: too late (T-{offset} < T-{self._min_offset})"
            self._log.info(
                "gate.v10_6_eval_offset_blocked",
                offset=offset,
                min=self._min_offset,
                max=self._max_offset,
                reason=reason,
            )
            return GateResult(
                passed=False, gate_name=self.name, reason=reason,
                data={"offset": offset, "min": self._min_offset, "max": self._max_offset},
            )

        # Above max: too far from close, v4/v5 calibration not yet
        # resolved — catastrophic historically (−33.96% ROI at T-180-240)
        if offset > self._max_offset:
            reason = f"V10.6: too early (T-{offset} > T-{self._max_offset})"
            self._log.info(
                "gate.v10_6_eval_offset_blocked",
                offset=offset,
                min=self._min_offset,
                max=self._max_offset,
                reason=reason,
            )
            return GateResult(
                passed=False, gate_name=self.name, reason=reason,
                data={"offset": offset, "min": self._min_offset, "max": self._max_offset},
            )

        # Inside [min, max] inclusive — the safe tradeable band
        return GateResult(
            passed=True, gate_name=self.name,
            reason=f"V10.6: T-{offset} in safe band [T-{self._min_offset}, T-{self._max_offset}]",
            data={"offset": offset, "min": self._min_offset, "max": self._max_offset},
        )


    async def evaluate_immutable(self, ctx: GateContext) -> tuple[GateResult, GateContextDelta]:
        """CA-03 immutable path."""
        result = await self.evaluate(ctx)
        return (result, EMPTY_DELTA)

# ── Source Agreement Gate ───────────────────────────────────────────────────

class SourceAgreementGate:
    """G1: source agreement vote — direction consensus between price feeds.

    This gate runs in one of two modes, selected at engine start by the
    `V11_POLY_SPOT_ONLY_CONSENSUS` env var:

    ── Mode A (default, V11_POLY_SPOT_ONLY_CONSENSUS=false) ──
    2/3 majority vote across Chainlink, Tiingo, and Binance.

    v11.1 ruleset. Introduced to lift the old v11.0 unanimous CL+TI
    pass rate (56.9%) to 98.2% by adding Binance as a tiebreaker. The
    assumption was that Binance's systematic DOWN bias (83.1% DOWN
    calls, see docs/CHANGELOG-v11.1-SOURCE-AGREEMENT-2-3-MAJORITY.md)
    would be neutralised in aggregate because CL+TI unanimity already
    captures most valid signals — Binance only tips the vote on
    windows where CL and TI disagree.

    Issue discovered 2026-04-11: on a CL=UP, TI=DOWN, BIN=DOWN window
    (19.6% of all evaluations by historical frequency) the 2/3 rule
    sides with BIN's systematic DOWN and approves a DOWN trade even
    though one of the two unbiased spot sources was calling UP. The
    gate uses futures data to break a spot-source tie, and futures
    has a known structural lean.

    ── Mode B (V11_POLY_SPOT_ONLY_CONSENSUS=true) ──
    Spot-only consensus: Chainlink + Tiingo only. Both spot feeds
    must agree on direction or the gate fails with `spot_disagree`.

    Binance spot/futures feeds are STILL READ AND USED by downstream
    gates and by VPIN / taker-flow / liquidations — this flag only
    removes Binance from the **consensus vote**, nothing else.

    Activation plan: this ships default OFF so merge is a zero-
    behaviour-change deploy. Once operator flips the env var on the
    Montreal host and restarts the engine, Mode B takes effect. If
    Mode B's pass rate drops too low in live telemetry, operator
    flips it back to false and we're back on the v11.1 2/3 rule with
    no code change.

    Evidence snapshot (2026-04-08 → 2026-04-10, 7 evaluations, see
    v11.1 changelog):
      - CL+TI unanimous: 56.9% pass rate
      - 2/3 CL+TI+BIN: 98.2% pass rate
      - Binance: 83.1% DOWN signals (biased, not a market signal)

    Env vars (read at __init__ time, same pattern as
    EvalOffsetBoundsGate / DeltaMagnitudeGate — operator must restart
    the engine to pick up flag changes):
      - `V11_POLY_SPOT_ONLY_CONSENSUS` — master flag for Mode B.
        Default `false`. Accepts `true`/`false` case-insensitively.
    """
    name = "source_agreement"

    def __init__(self):
        # Read env at construction time — same pattern as
        # EvalOffsetBoundsGate at gates.py:196. Flipping the flag
        # requires an engine restart. This is a deliberate ergonomic
        # trade: keeps the hot path free of env lookups (one per
        # window × thousands of windows per day).
        self._spot_only = (
            os.environ.get("V11_POLY_SPOT_ONLY_CONSENSUS", "false").lower() == "true"
        )
        self._log = log.bind(gate="source_agreement")

    async def evaluate(self, ctx: GateContext) -> GateResult:
        if ctx.delta_chainlink is None or ctx.delta_tiingo is None:
            return GateResult(
                passed=False, gate_name=self.name,
                reason="missing CL or TI data",
            )

        cl_dir = "UP" if ctx.delta_chainlink > 0 else "DOWN"
        ti_dir = "UP" if ctx.delta_tiingo > 0 else "DOWN"

        # ── Mode B: spot-only consensus (V11_POLY_SPOT_ONLY_CONSENSUS=true) ──
        # Binance is excluded from the vote. The two spot sources must
        # agree or the gate fails. This is stricter than the 2/3 rule
        # and matches the pre-v11.1 unanimous CL+TI behaviour, but the
        # name / reason strings tag it as v11/DQ-01 so operators can
        # tell from logs which mode the engine is running.
        if self._spot_only:
            if cl_dir == ti_dir:
                agreed_dir = cl_dir
                ctx.agreed_direction = agreed_dir
                return GateResult(
                    passed=True, gate_name=self.name,
                    reason=f"spot-only {agreed_dir} (CL={cl_dir} TI={ti_dir})",
                    data={
                        "mode": "spot_only",
                        "cl_dir": cl_dir,
                        "ti_dir": ti_dir,
                        "direction": agreed_dir,
                    },
                )
            # Spot sources disagree — Binance is intentionally ignored
            # in this mode, so there is no tiebreaker and the window
            # is skipped. This is the v11.0 pass/fail boundary.
            self._log.info(
                "gate.source_agreement.spot_disagree",
                mode="spot_only",
                cl_dir=cl_dir,
                ti_dir=ti_dir,
            )
            return GateResult(
                passed=False, gate_name=self.name,
                reason=f"spot disagree: CL={cl_dir} TI={ti_dir} (spot-only mode)",
                data={
                    "mode": "spot_only",
                    "cl_dir": cl_dir,
                    "ti_dir": ti_dir,
                },
            )

        # ── Mode A: 2/3 majority vote (default, v11.1 legacy path) ──
        bin_dir = "UP" if ctx.delta_binance is not None and ctx.delta_binance > 0 else "DOWN"

        # Count votes for each direction
        up_votes = sum([cl_dir == "UP", ti_dir == "UP", bin_dir == "UP"])
        down_votes = 3 - up_votes

        # 2/3 majority required
        if up_votes >= 2:
            agreed_dir = "UP"
        elif down_votes >= 2:
            agreed_dir = "DOWN"
        else:
            # 2-2 split impossible with 3 sources, but handle gracefully
            return GateResult(
                passed=False, gate_name=self.name,
                reason=f"CL={cl_dir} TI={ti_dir} BIN={bin_dir} NO MAJORITY",
                data={"cl_dir": cl_dir, "ti_dir": ti_dir, "bin_dir": bin_dir, "up_votes": up_votes, "down_votes": down_votes},
            )

        # Store agreed direction in context for downstream gates
        ctx.agreed_direction = agreed_dir
        return GateResult(
            passed=True, gate_name=self.name,
            reason=f"2/3 {agreed_dir} (CL={cl_dir} TI={ti_dir} BIN={bin_dir})",
            data={"cl_dir": cl_dir, "ti_dir": ti_dir, "bin_dir": bin_dir, "direction": agreed_dir, "up_votes": up_votes, "down_votes": down_votes},
        )


    async def evaluate_immutable(self, ctx: GateContext) -> tuple[GateResult, GateContextDelta]:
        """CA-03 immutable path -- returns agreed_direction delta."""
        if ctx.delta_chainlink is None or ctx.delta_tiingo is None:
            return (GateResult(passed=False, gate_name=self.name, reason="missing CL or TI data"), EMPTY_DELTA)

        cl_dir = "UP" if ctx.delta_chainlink > 0 else "DOWN"
        ti_dir = "UP" if ctx.delta_tiingo > 0 else "DOWN"

        if self._spot_only:
            if cl_dir == ti_dir:
                return (
                    GateResult(passed=True, gate_name=self.name,
                        reason=f"spot-only {cl_dir} (CL={cl_dir} TI={ti_dir})",
                        data={"mode": "spot_only", "cl_dir": cl_dir, "ti_dir": ti_dir, "direction": cl_dir}),
                    GateContextDelta(agreed_direction=cl_dir),
                )
            self._log.info("gate.source_agreement.spot_disagree", mode="spot_only", cl_dir=cl_dir, ti_dir=ti_dir)
            return (
                GateResult(passed=False, gate_name=self.name,
                    reason=f"spot disagree: CL={cl_dir} TI={ti_dir} (spot-only mode)",
                    data={"mode": "spot_only", "cl_dir": cl_dir, "ti_dir": ti_dir}),
                EMPTY_DELTA,
            )

        bin_dir = "UP" if ctx.delta_binance is not None and ctx.delta_binance > 0 else "DOWN"
        up_votes = sum([cl_dir == "UP", ti_dir == "UP", bin_dir == "UP"])
        down_votes = 3 - up_votes
        if up_votes >= 2:
            agreed_dir = "UP"
        elif down_votes >= 2:
            agreed_dir = "DOWN"
        else:
            return (
                GateResult(passed=False, gate_name=self.name,
                    reason=f"CL={cl_dir} TI={ti_dir} BIN={bin_dir} NO MAJORITY",
                    data={"cl_dir": cl_dir, "ti_dir": ti_dir, "bin_dir": bin_dir, "up_votes": up_votes, "down_votes": down_votes}),
                EMPTY_DELTA,
            )

        return (
            GateResult(passed=True, gate_name=self.name,
                reason=f"2/3 {agreed_dir} (CL={cl_dir} TI={ti_dir} BIN={bin_dir})",
                data={"cl_dir": cl_dir, "ti_dir": ti_dir, "bin_dir": bin_dir, "direction": agreed_dir, "up_votes": up_votes, "down_votes": down_votes}),
            GateContextDelta(agreed_direction=agreed_dir),
        )

# ── Delta Magnitude Gate (v10.5) ───────────────────────────────────────────

class DeltaMagnitudeGate:
    """G2: Delta magnitude must exceed regime-specific floor.

    v10.5: Blocks trades where |delta_pct| is too small — direction
    agreement is meaningless if price hasn't actually moved.

    Evidence (50 trades, Apr 9 2026):
      |delta| < 0.01% in TRANSITION: 0W/2L (losses #3250, #3301)
      |delta| >= 0.01% in TRANSITION: 23W/5L (82.1% WR)
      CASCADE exempt — forced liq reversals can start at tiny delta.
    """
    name = "delta_magnitude"

    def __init__(self):
        self._global_min = float(os.environ.get("V10_MIN_DELTA_PCT", "0.0"))
        self._transition_min = float(os.environ.get("V10_TRANSITION_MIN_DELTA", "0.0"))
        self._log = log.bind(gate="delta_magnitude")

    async def evaluate(self, ctx: GateContext) -> GateResult:
        abs_delta = abs(ctx.delta_pct) if ctx.delta_pct else 0.0
        regime = ctx.regime or "NORMAL"

        # CASCADE exempt — tiny deltas are normal pre-liquidation
        if regime == "CASCADE":
            return GateResult(
                passed=True, gate_name=self.name,
                reason=f"CASCADE exempt (|delta|={abs_delta:.4f}%)",
            )

        # Regime-specific floor
        floor = self._global_min
        if regime == "TRANSITION" and self._transition_min > 0:
            floor = self._transition_min

        if floor > 0 and abs_delta < floor:
            self._log.info("delta_magnitude.blocked",
                regime=regime, abs_delta=f"{abs_delta:.6f}",
                floor=f"{floor:.4f}")
            return GateResult(
                passed=False, gate_name=self.name,
                reason=f"|delta|={abs_delta:.4f}% < {floor:.4f}% ({regime})",
                data={"abs_delta": abs_delta, "floor": floor, "regime": regime},
            )

        return GateResult(
            passed=True, gate_name=self.name,
            reason=f"|delta|={abs_delta:.4f}% >= {floor:.4f}% ({regime})",
            data={"abs_delta": abs_delta, "floor": floor},
        )


    async def evaluate_immutable(self, ctx: GateContext) -> tuple[GateResult, GateContextDelta]:
        """CA-03 immutable path."""
        result = await self.evaluate(ctx)
        return (result, EMPTY_DELTA)

# ── DUNE Confidence Gate ───────────────────────────────────────────────────

class DuneConfidenceGate:
    """G4: DUNE ML model must confirm direction with sufficient confidence.

    v10.3: Full decision surface with 4 threshold components:
      effective = regime_base + offset_penalty + down_penalty + cg_modifier - cg_bonus

    Components:
      - regime_base: per-regime threshold (ELM v3 calibrated)
      - offset_penalty: linear decay, 0 at T-60, 0.005 per 20s (max from env)
      - down_penalty: +0.03 for DOWN predictions (9.3pp less accurate, N=865)
      - cg_modifier: +0.05 if taker opposing, -0.02 if taker aligned (from TakerFlowGate)
      - cg_bonus: -0.02 if 2+ CG confirmation signals (from CGConfirmationGate)
    
    v11.0: Dynamic TimesFM confidence gating
      - timesfm.confidence >= 0.90: Allow P(UP) >= 0.55 (was 0.65)
      - timesfm.confidence >= 0.80: Allow P(UP) >= 0.58
      - timesfm.confidence >= 0.70: Allow P(UP) >= 0.60
      - timesfm.confidence < 0.70: Require HIGH confidence (p > 0.65 or p < 0.35)
    """
    name = "dune_confidence"

    # v10.3: ELM-calibrated regime thresholds
    _REGIME_DEFAULTS = {
        "TRANSITION": 0.70,   # 85% WR best regime (was 0.85 in v10.2, 9.99 in v10.1)
        "CASCADE":    0.72,   # 67% WR small sample, keep tighter
        "NORMAL":     0.65,   # ELM P>=0.65 = 78.4% acc on 93% coverage
        "LOW_VOL":    0.65,   # Same as NORMAL
        "TRENDING":   0.72,   # Same as CASCADE
        "CALM":       0.72,   # Conservative — low signal environment
    }

    _REGIME_ENV = {
        "TRANSITION": "V10_TRANSITION_MIN_P",
        "CASCADE":    "V10_CASCADE_MIN_P",
        "NORMAL":     "V10_NORMAL_MIN_P",
        "LOW_VOL":    "V10_LOW_VOL_MIN_P",
        "TRENDING":   "V10_TRENDING_MIN_P",
        "CALM":       "V10_CALM_MIN_P",
    }

    def __init__(self, dune_client=None):
        self._base_min_p = float(os.environ.get("V10_DUNE_MIN_P", "0.65"))
        self._offset_penalty_max = float(os.environ.get("V10_OFFSET_PENALTY_MAX", "0.06"))
        self._early_penalty_max = float(os.environ.get("V10_OFFSET_PENALTY_EARLY_MAX", "0.0"))
        self._early_min_conf = float(os.environ.get("V10_EARLY_ENTRY_MIN_CONF", "0.90"))
        self._down_penalty = float(os.environ.get("V10_DOWN_PENALTY", "0.0"))
        # v11.0: Dynamic confidence gating
        self._cascade_min_conf = float(os.environ.get("V10_CASCADE_MIN_CONF", "0.90"))
        self._cascade_conf_bonus = float(os.environ.get("V10_CASCADE_CONF_BONUS", "0.05"))
        self._client = dune_client
        self._log = log.bind(gate="dune_confidence")

        self._regime_thresholds: dict[str, float] = {}
        for regime, default in self._REGIME_DEFAULTS.items():
            env_key = self._REGIME_ENV[regime]
            self._regime_thresholds[regime] = float(
                os.environ.get(env_key, str(default))
            )

    def _effective_threshold(self, ctx: GateContext, regime: str, eval_offset: Optional[int],
                            p_up: Optional[float] = None, timesfm_conf: Optional[float] = None) -> float:
        """Calculate threshold = regime_base + offset + down + cg_modifier - cg_bonus.

        Per-20s offset granularity from decision surface spec (Section 5, Gate 2).
        
        v11.0: Dynamic TimesFM confidence adjustment
          - If timesfm.conf >= 0.90: Apply -0.05 bonus to threshold (allow P(UP) >= 0.55)
          - If timesfm.conf >= 0.80: Apply -0.03 bonus
          - If timesfm.conf >= 0.70: Apply -0.01 bonus
          - If timesfm.conf < 0.70: No bonus (require HIGH confidence p > 0.65)
        """
        base = self._regime_thresholds.get(regime, self._base_min_p)

        # Offset penalty: two-tier ramp (v10.6)
        # Tier 1: linear from 0 at T-60 to _offset_penalty_max at T-180
        # Tier 2: linear from 0 at T-180 to _early_penalty_max at T-200 (steeper)
        offset_penalty = 0.0
        if eval_offset is not None and eval_offset > 60:
            base_penalty = min(self._offset_penalty_max,
                               (eval_offset - 60) / 120.0 * self._offset_penalty_max)
            early_penalty = 0.0
            if eval_offset > 180 and self._early_penalty_max > 0:
                early_penalty = min(self._early_penalty_max,
                                    (eval_offset - 180) / 20.0 * self._early_penalty_max)
            offset_penalty = base_penalty + early_penalty

        # DOWN penalty: +0.03 (9.3pp accuracy gap, N=865)
        down_penalty = self._down_penalty if ctx.agreed_direction == "DOWN" else 0.0

        # CG modifiers from upstream gates
        cg_mod = ctx.cg_threshold_modifier  # +0.05 (taker opposing) or 0.0
        cg_bonus = ctx.cg_bonus             # 0.02 (3-signal confirmation) or 0.0

        # v11.0: TimesFM confidence adjustment
        conf_bonus = 0.0
        if timesfm_conf is not None and timesfm_conf >= self._cascade_min_conf:
            conf_bonus = self._cascade_conf_bonus  # -0.05 when conf >= 0.90
            # Scale bonus for lower confidence
            if timesfm_conf >= 0.80 and timesfm_conf < 0.90:
                conf_bonus = 0.03
            elif timesfm_conf >= 0.70 and timesfm_conf < 0.80:
                conf_bonus = 0.01

        effective = base + offset_penalty + down_penalty + cg_mod - cg_bonus - conf_bonus
        return round(effective, 4)

    async def evaluate(self, ctx: GateContext) -> GateResult:
        if not ctx.agreed_direction:
            return GateResult(
                passed=False, gate_name=self.name,
                reason="no agreed direction (agreement gate must run first)",
            )

        # Initialize p_up and timesfm_conf for _effective_threshold (fixes "referenced before assignment" bug)
        p_up = ctx.dune_probability_up
        timesfm_conf = None  # Not yet implemented in query path

        # Global minimum offset — don't trade too early
        _min_offset = int(os.environ.get("V10_MIN_EVAL_OFFSET", "200"))
        if ctx.eval_offset and ctx.eval_offset > _min_offset:
            return GateResult(
                passed=False, gate_name=self.name,
                reason=f"too early: T-{ctx.eval_offset} > T-{_min_offset}",
            )

        # v10.6: Early entry zone (T-180..200) requires minimum directional confidence
        if ctx.eval_offset and ctx.eval_offset > 180 and self._early_min_conf > 0:
            dir_conf = None
            if ctx.dune_probability_up is not None and ctx.agreed_direction:
                p_up = ctx.dune_probability_up
                dir_conf = p_up if ctx.agreed_direction == "UP" else (1.0 - p_up)
            if dir_conf is None or dir_conf < self._early_min_conf:
                return GateResult(
                    passed=False, gate_name=self.name,
                    reason=f"early entry T-{ctx.eval_offset}: conf {dir_conf:.3f} < {self._early_min_conf:.2f}" if dir_conf else f"early entry T-{ctx.eval_offset}: no confidence",
                    data={"offset": ctx.eval_offset, "conf": dir_conf, "min_conf": self._early_min_conf},
                )

        regime = ctx.regime or "NORMAL"

        # v10.4: per-regime offset limits
        # NORMAL before T-100: 25% WR (1W/3L), too early for low-VPIN regime
        _normal_min = int(os.environ.get("V10_NORMAL_MIN_OFFSET", "0"))
        if regime == "NORMAL" and _normal_min > 0 and ctx.eval_offset and ctx.eval_offset > _normal_min:
            return GateResult(
                passed=False, gate_name=self.name,
                reason=f"NORMAL too early: T-{ctx.eval_offset} > T-{_normal_min}",
                data={"regime": regime, "offset": ctx.eval_offset, "limit": _normal_min},
            )

        # TRANSITION+DOWN after T-140: 56.3% WR, collapses while UP stays strong
        _trans_down_max = int(os.environ.get("V10_TRANSITION_MAX_DOWN_OFFSET", "0"))
        if (regime == "TRANSITION" and _trans_down_max > 0
                and ctx.agreed_direction == "DOWN"
                and ctx.eval_offset and ctx.eval_offset > _trans_down_max):
            return GateResult(
                passed=False, gate_name=self.name,
                reason=f"TRANSITION+DOWN too early: T-{ctx.eval_offset} > T-{_trans_down_max}",
                data={"regime": regime, "direction": "DOWN", "offset": ctx.eval_offset, "limit": _trans_down_max},
            )
        threshold = self._effective_threshold(ctx, regime, ctx.eval_offset, p_up, timesfm_conf)

        # Fast-reject if threshold is unreachable
        if threshold >= 1.0:
            return GateResult(
                passed=False, gate_name=self.name,
                reason=f"regime {regime} blocked (threshold={threshold:.2f})",
                data={"regime": regime, "threshold": threshold},
            )

        # Query DUNE API
        if self._client is None:
            return GateResult(
                passed=True, gate_name=self.name,
                reason="DUNE client not configured (pass-through)",
            )

        seconds_to_close = ctx.eval_offset or 60
        try:
            from signals.v2_feature_body import (
                V5FeatureBody,
                build_v5_feature_body,
                confidence_from_result,
            )

            # Prefer the strategy-built body attached to GateContext —
            # this is the single source of truth for feature extraction
            # and guarantees the DUNE gate sees the exact same feature
            # vector the v8.1 fetch sees. If it's missing (old callers,
            # unit tests, etc.), fall back to rebuilding from the
            # enriched GateContext scalars. The fallback may have lower
            # coverage but uses the same `build_v5_feature_body` helper
            # so the extraction logic is never duplicated.
            #
            # Gate booleans in the v10 pipeline have different semantics
            # than the v8.0 `signal_evaluations` columns the v5 model
            # trained on, so they stay None here regardless of which
            # path we take — the scorer gets NaN for those and
            # LightGBM's missing-default branch handles it exactly as
            # in training.
            _gate_features: Optional[V5FeatureBody] = None
            if isinstance(ctx.v5_features, V5FeatureBody):
                _gate_features = ctx.v5_features
            else:
                _gate_features = build_v5_feature_body(
                    eval_offset=float(seconds_to_close),
                    vpin=ctx.vpin,
                    delta_pct=ctx.delta_pct,
                    twap_delta=ctx.twap_delta,
                    clob_up_price=ctx.gamma_up_price,
                    clob_down_price=ctx.gamma_down_price,
                    binance_price=ctx.current_price,
                    chainlink_price=ctx.chainlink_price,
                    tiingo_close=ctx.tiingo_close,
                    delta_binance=ctx.delta_binance,
                    delta_chainlink=ctx.delta_chainlink,
                    delta_tiingo=ctx.delta_tiingo,
                    regime=ctx.regime,
                    delta_source=ctx.delta_source,
                    prev_v2_probability_up=ctx.prev_v2_probability_up,
                    # gate_* intentionally omitted — see note above.
                )

            _model = os.environ.get("V10_DUNE_MODEL", "oak")
            result = await self._client.score_with_features(
                asset=ctx.asset,
                seconds_to_close=seconds_to_close,
                features=_gate_features,
                model=_model,
            )
        except Exception as exc:
            self._log.warning("dune.query_failed", error=str(exc)[:100])
            # BLOCK on DUNE failure — never trade without model confidence
            return GateResult(
                passed=False, gate_name=self.name,
                reason=f"DUNE query failed: {str(exc)[:50]} (BLOCKED)",
            )

        if not result or "probability_up" not in result:
            return GateResult(
                passed=False, gate_name=self.name,
                reason="DUNE returned invalid response (BLOCKED)",
            )

        p_up = float(result["probability_up"])
        dune_p = p_up if ctx.agreed_direction == "UP" else (1.0 - p_up)

        # v11.1: Extract confidence via the shared helper — prefers a
        # scorer-emitted top-level `confidence` field, falls back to
        # max(p, 1-p). Reading `result["timesfm"]["confidence"]` as
        # P(UP) confidence was the v11 bug — that field is the v1
        # forecaster's own metric, not confidence in this call.
        timesfm_conf = confidence_from_result(result)

        # Store in context for downstream gates
        ctx.dune_probability_up = p_up
        ctx.dune_direction = "UP" if p_up > 0.5 else "DOWN"
        ctx.dune_model_version = result.get("model_version", "")

        # Build components string for logging
        components = f"base={self._regime_thresholds.get(regime, self._base_min_p):.3f}"
        if ctx.agreed_direction == "DOWN" and self._down_penalty > 0:
            components += f" +down={self._down_penalty:.3f}"
        if ctx.cg_threshold_modifier != 0:
            components += f" +cg_mod={ctx.cg_threshold_modifier:+.03f}"
        if ctx.cg_bonus > 0:
            components += f" -cg_bonus={ctx.cg_bonus:.03f}"
        # v11.0: Add TimesFM confidence component
        if timesfm_conf is not None:
            conf_bonus = self._cascade_conf_bonus if timesfm_conf >= self._cascade_min_conf else (
                0.03 if timesfm_conf >= 0.80 else (0.01 if timesfm_conf >= 0.70 else 0.0))
            if conf_bonus > 0:
                components += f" -timesfm_conf={timesfm_conf:.2f} (bonus={conf_bonus:.03f})"

        self._log.info("dune.evaluated",
            asset=ctx.asset, offset=seconds_to_close,
            p_up=f"{p_up:.4f}", dune_p=f"{dune_p:.4f}",
            agreed_dir=ctx.agreed_direction,
            regime=regime, threshold=f"{threshold:.4f}",
            timesfm_conf=f"{timesfm_conf:.2f}",  # v11.0: TimesFM confidence
            components=components,
            passed=dune_p >= threshold)

        data = {"dune_p": dune_p, "p_up": p_up, "threshold": threshold,
                "regime": regime, "offset": ctx.eval_offset,
                "down_penalty": self._down_penalty if ctx.agreed_direction == "DOWN" else 0.0,
                "cg_modifier": ctx.cg_threshold_modifier, "cg_bonus": ctx.cg_bonus,
                "timesfm_conf": timesfm_conf}  # v11.0: TimesFM confidence

        if dune_p < threshold:
            return GateResult(
                passed=False, gate_name=self.name,
                reason=f"DUNE P({ctx.agreed_direction})={dune_p:.3f} < {threshold:.3f} (timesfm_conf={timesfm_conf:.2f}, {regime} T-{ctx.eval_offset} {components})",
                data=data,
            )

        return GateResult(
            passed=True, gate_name=self.name,
            reason=f"DUNE P({ctx.agreed_direction})={dune_p:.3f} >= {threshold:.3f} ({regime} T-{ctx.eval_offset})",
            data=data,
        )


    async def evaluate_immutable(self, ctx: GateContext) -> tuple[GateResult, GateContextDelta]:
        """CA-03 immutable path -- runs evaluate on a mutable copy, diffs for delta."""
        mutable_ctx = replace(ctx)
        result = await self.evaluate(mutable_ctx)
        kw = {}
        if mutable_ctx.dune_probability_up != ctx.dune_probability_up:
            kw["dune_probability_up"] = mutable_ctx.dune_probability_up
        if mutable_ctx.dune_direction != ctx.dune_direction:
            kw["dune_direction"] = mutable_ctx.dune_direction
        if mutable_ctx.dune_model_version != ctx.dune_model_version:
            kw["dune_model_version"] = mutable_ctx.dune_model_version
        return (result, GateContextDelta(**kw) if kw else EMPTY_DELTA)

# ── Taker Flow Gate (v10.3 — replaces CoinGlassVetoGate) ─────────────────

class TakerFlowGate:
    """G2: CoinGlass taker flow alignment gate.

    Based on 719 live trades with CG data (Apr 2026):
      Taker aligned + Smart aligned:  81.7% WR, +$295.66 (N=327)
      Taker aligned + Smart opposing: 79.6% WR, +$71.76  (N=162)
      Taker opposing + Smart aligned: 73.3% WR, -$23.38  (N=86)
      Taker opposing + Smart opposing: 58.3% WR, +$42.51  (N=144)

    Actions:
      Both opposing → HARD SKIP (58% WR, below breakeven at any cap)
      Taker opposing only → raise threshold +0.05 (marginal 73% WR)
      Taker aligned → lower threshold -0.02 (80%+ WR confirmation bonus)
      Neutral → pass through

    Also checks CG data freshness (stale > 120s → SKIP).
    Runs BEFORE DuneConfidenceGate so modifier is available.
    Feature flag: V10_CG_TAKER_GATE (default false for safe rollout).
    """
    name = "taker_flow"

    def __init__(self):
        self._enabled = os.environ.get("V10_CG_TAKER_GATE", "false").lower() == "true"
        self._taker_opposing_pct = float(os.environ.get("V10_CG_TAKER_OPPOSING_PCT", "55"))
        self._smart_opposing_pct = float(os.environ.get("V10_CG_SMART_OPPOSING_PCT", "52"))
        self._taker_opposing_penalty = float(os.environ.get("V10_CG_TAKER_OPPOSING_PENALTY", "0.05"))
        self._taker_aligned_bonus = float(os.environ.get("V10_CG_TAKER_ALIGNED_BONUS", "0.02"))
        self._max_age_ms = int(os.environ.get("V10_CG_MAX_AGE_MS", "120000"))
        self._log = log.bind(gate="taker_flow")

    async def evaluate(self, ctx: GateContext) -> GateResult:
        if not self._enabled:
            return GateResult(passed=True, gate_name=self.name, reason="disabled (V10_CG_TAKER_GATE=false)")

        cg = ctx.cg_snapshot
        direction = ctx.agreed_direction

        if cg is None or not hasattr(cg, 'connected') or not cg.connected:
            return GateResult(passed=True, gate_name=self.name, reason="CG not connected (pass-through)")

        if not direction:
            return GateResult(passed=True, gate_name=self.name, reason="no direction")

        # Freshness check — stale CG data is no signal
        if hasattr(cg, 'timestamp') and cg.timestamp:
            age_ms = (datetime.now(timezone.utc) - cg.timestamp).total_seconds() * 1000
            if age_ms > self._max_age_ms:
                return GateResult(
                    passed=False, gate_name=self.name,
                    reason=f"CG stale ({age_ms:.0f}ms > {self._max_age_ms}ms)",
                    data={"age_ms": age_ms},
                )

        # Calculate taker alignment
        taker_total = cg.taker_buy_volume_1m + cg.taker_sell_volume_1m
        if taker_total <= 0:
            return GateResult(passed=True, gate_name=self.name, reason="no taker volume data")

        buy_pct = cg.taker_buy_volume_1m / taker_total * 100
        sell_pct = 100 - buy_pct

        if direction == "UP":
            taker_aligned = buy_pct > (100 - self._taker_opposing_pct)
            taker_opposing = sell_pct > self._taker_opposing_pct
            smart_opposing = cg.top_position_short_pct > self._smart_opposing_pct
        else:  # DOWN
            taker_aligned = sell_pct > (100 - self._taker_opposing_pct)
            taker_opposing = buy_pct > self._taker_opposing_pct
            smart_opposing = cg.top_position_long_pct > self._smart_opposing_pct

        flow_info = f"buy={buy_pct:.0f}% sell={sell_pct:.0f}% smart_opp={smart_opposing}"

        # Decision matrix
        if taker_opposing and smart_opposing:
            self._log.info("taker_flow.both_opposing", direction=direction, flow=flow_info)
            return GateResult(
                passed=False, gate_name=self.name,
                reason=f"BOTH OPPOSING ({direction}) — taker+smart against, 58% WR bucket. {flow_info}",
                data={"taker_opposing": True, "smart_opposing": True, "buy_pct": buy_pct},
            )

        if taker_opposing:
            ctx.cg_threshold_modifier = self._taker_opposing_penalty
            self._log.info("taker_flow.taker_opposing", direction=direction, penalty=self._taker_opposing_penalty, flow=flow_info)
            return GateResult(
                passed=True, gate_name=self.name,
                reason=f"taker opposing +{self._taker_opposing_penalty} penalty ({direction}). {flow_info}",
                data={"taker_opposing": True, "smart_opposing": False, "modifier": self._taker_opposing_penalty, "buy_pct": buy_pct},
            )

        if taker_aligned:
            ctx.cg_threshold_modifier = -self._taker_aligned_bonus
            self._log.info("taker_flow.taker_aligned", direction=direction, bonus=self._taker_aligned_bonus, flow=flow_info)
            return GateResult(
                passed=True, gate_name=self.name,
                reason=f"taker aligned -{self._taker_aligned_bonus} bonus ({direction}). {flow_info}",
                data={"taker_aligned": True, "modifier": -self._taker_aligned_bonus, "buy_pct": buy_pct},
            )

        # Neutral — no strong taker signal
        return GateResult(
            passed=True, gate_name=self.name,
            reason=f"neutral taker flow ({direction}). {flow_info}",
            data={"neutral": True, "buy_pct": buy_pct},
        )


    async def evaluate_immutable(self, ctx: GateContext) -> tuple[GateResult, GateContextDelta]:
        """CA-03 immutable path -- runs evaluate on a mutable copy, diffs for delta."""
        if not self._enabled:
            return (GateResult(passed=True, gate_name=self.name, reason="disabled (V10_CG_TAKER_GATE=false)"), EMPTY_DELTA)
        mutable_ctx = replace(ctx)
        result = await self.evaluate(mutable_ctx)
        kw = {}
        if mutable_ctx.cg_threshold_modifier != ctx.cg_threshold_modifier:
            kw["cg_threshold_modifier"] = mutable_ctx.cg_threshold_modifier
        return (result, GateContextDelta(**kw) if kw else EMPTY_DELTA)

# ── CoinGlass Confirmation Gate (v10.3 — 3-signal bonus) ─────────────────

class CGConfirmationGate:
    """G3: CoinGlass 3-signal confirmation — stronger version.

    From v10.1 decision surface spec (Section 5, Gate 5), strengthened based on
    live data showing 17pp WR delta between CG-aligned (81.7%) and CG-opposing (58.3%).

    3 signals checked: net taker flow, OI delta, long/short ratio.
      2+ confirms → BONUS: lower threshold by 0.03 (rescue borderline trades)
      0 confirms  → PENALTY: raise threshold by 0.02 (zero alignment = weak signal)
      1 confirm   → neutral (no modifier)

    Always passes — only modifies ctx.cg_bonus and ctx.cg_confirms.
    The bonus/penalty is applied in DuneConfidenceGate via ctx.cg_bonus.
    """
    name = "cg_confirmation"

    def __init__(self):
        self._bonus = float(os.environ.get("V10_CG_CONFIRM_BONUS", "0.03"))
        self._zero_penalty = float(os.environ.get("V10_CG_ZERO_CONFIRM_PENALTY", "0.02"))
        self._min_confirms = int(os.environ.get("V10_CG_CONFIRM_MIN", "2"))

    async def evaluate(self, ctx: GateContext) -> GateResult:
        cg = ctx.cg_snapshot
        direction = ctx.agreed_direction

        if cg is None or not hasattr(cg, 'connected') or not cg.connected or not direction:
            return GateResult(passed=True, gate_name=self.name, reason="no CG data (no bonus)")

        confirms = 0
        details = []

        # Signal 1: Net taker flow direction
        net_flow = cg.taker_buy_volume_1m - cg.taker_sell_volume_1m
        if direction == "UP" and net_flow > 0:
            confirms += 1
            details.append("net_buying")
        elif direction == "DOWN" and net_flow < 0:
            confirms += 1
            details.append("net_selling")

        # Signal 2: OI momentum
        oi_delta = getattr(cg, 'oi_delta_pct_1m', 0) or 0
        if direction == "UP" and oi_delta > 0:
            confirms += 1
            details.append(f"oi_rising({oi_delta:+.2f}%)")
        elif direction == "DOWN" and oi_delta < 0:
            confirms += 1
            details.append(f"oi_falling({oi_delta:+.2f}%)")

        # Signal 3: Long/short ratio
        lsr = getattr(cg, 'long_short_ratio', 1.0) or 1.0
        if direction == "UP" and lsr > 1.0:
            confirms += 1
            details.append(f"lsr={lsr:.2f}>1")
        elif direction == "DOWN" and lsr < 1.0:
            confirms += 1
            details.append(f"lsr={lsr:.2f}<1")

        ctx.cg_confirms = confirms

        # Strengthened: 2+ = bonus, 0 = penalty, 1 = neutral
        if confirms >= self._min_confirms:
            ctx.cg_bonus = self._bonus
            action = f"bonus=-{self._bonus}"
        elif confirms == 0 and self._zero_penalty > 0:
            # Zero confirms = all 3 signals oppose direction. Raise the bar.
            ctx.cg_bonus = -self._zero_penalty  # negative bonus = penalty
            action = f"penalty=+{self._zero_penalty}"
        else:
            action = "neutral"

        detail_str = ", ".join(details) if details else "none"
        return GateResult(
            passed=True, gate_name=self.name,
            reason=f"CG confirms={confirms}/3 ({detail_str}) → {action}",
            data={"confirms": confirms, "bonus": ctx.cg_bonus, "details": details},
        )


    async def evaluate_immutable(self, ctx: GateContext) -> tuple[GateResult, GateContextDelta]:
        """CA-03 immutable path -- runs evaluate on a mutable copy, diffs for delta."""
        mutable_ctx = replace(ctx)
        result = await self.evaluate(mutable_ctx)
        kw = {}
        if mutable_ctx.cg_confirms != ctx.cg_confirms:
            kw["cg_confirms"] = mutable_ctx.cg_confirms
        if mutable_ctx.cg_bonus != ctx.cg_bonus:
            kw["cg_bonus"] = mutable_ctx.cg_bonus
        return (result, GateContextDelta(**kw) if kw else EMPTY_DELTA)

# ── Polymarket Spread Gate (v10.3) ───────────────────────────────────────

class SpreadGate:
    """G5: Kill trade if Polymarket orderbook spread is too wide.

    From v10.1 decision surface spec (Section 5, Gate 6):
      Wide spread = thin liquidity = high slippage risk.
      Default threshold: 8% spread.
    """
    name = "spread_gate"

    def __init__(self):
        self._max_spread_pct = float(os.environ.get("V10_MAX_SPREAD_PCT", "8"))

    async def evaluate(self, ctx: GateContext) -> GateResult:
        up = ctx.gamma_up_price
        down = ctx.gamma_down_price

        if up is None or down is None:
            return GateResult(passed=True, gate_name=self.name, reason="no Gamma data (pass-through)")

        total = up + down
        if total <= 0:
            return GateResult(passed=True, gate_name=self.name, reason="zero Gamma prices")

        spread_pct = abs(up - down) / (total / 2) * 100

        if spread_pct > self._max_spread_pct:
            return GateResult(
                passed=False, gate_name=self.name,
                reason=f"spread {spread_pct:.1f}% > {self._max_spread_pct}% (thin liquidity)",
                data={"spread_pct": spread_pct, "up": up, "down": down},
            )

        return GateResult(
            passed=True, gate_name=self.name,
            reason=f"spread {spread_pct:.1f}% OK",
            data={"spread_pct": spread_pct},
        )


    async def evaluate_immutable(self, ctx: GateContext) -> tuple[GateResult, GateContextDelta]:
        """CA-03 immutable path."""
        result = await self.evaluate(ctx)
        return (result, EMPTY_DELTA)

# ── CoinGlass Veto Gate (LEGACY — kept for V10_CG_TAKER_GATE=false) ─────

class CoinGlassVetoGate:
    """G3: CoinGlass micro-structure veto (3+ opposing signals = skip).

    Kept from v9 — checks smart money, funding, crowd positioning,
    taker volume, and cascade divergence.
    """
    name = "cg_veto"

    async def evaluate(self, ctx: GateContext) -> GateResult:
        cg = ctx.cg_snapshot
        direction = ctx.agreed_direction

        if cg is None or not hasattr(cg, 'connected') or not cg.connected:
            return GateResult(
                passed=True, gate_name=self.name,
                reason="CG not connected (pass-through)",
            )

        if not direction:
            return GateResult(
                passed=True, gate_name=self.name,
                reason="no direction to check",
            )

        veto_count = 0
        veto_reasons = []

        # 1. Smart money opposing (>52%)
        if direction == "UP" and cg.top_position_short_pct > 52:
            veto_count += 1
            veto_reasons.append(f"smart_short={cg.top_position_short_pct:.0f}%")
        elif direction == "DOWN" and cg.top_position_long_pct > 52:
            veto_count += 1
            veto_reasons.append(f"smart_long={cg.top_position_long_pct:.0f}%")

        # 2. Funding opposing
        funding_annual = cg.funding_rate * 3 * 365
        if direction == "UP" and cg.funding_rate > 0.0005:
            veto_count += 1
            veto_reasons.append(f"funding_vs_up={funding_annual:.0f}%/yr")
        elif direction == "DOWN" and funding_annual > 1.0:
            veto_count += 1
            veto_reasons.append(f"funding_bullish={funding_annual:.0f}%/yr")

        # 3. Crowd overleveraged (>60%)
        if direction == "UP" and cg.long_pct > 60:
            veto_count += 1
            veto_reasons.append(f"crowd_long={cg.long_pct:.0f}%")
        elif direction == "DOWN" and cg.short_pct > 60:
            veto_count += 1
            veto_reasons.append(f"crowd_short={cg.short_pct:.0f}%")

        # 4. Taker volume opposing (>60%)
        taker_total = cg.taker_buy_volume_1m + cg.taker_sell_volume_1m
        if taker_total > 0:
            sell_pct = cg.taker_sell_volume_1m / taker_total * 100
            buy_pct = 100 - sell_pct
            if direction == "UP" and sell_pct > 60:
                veto_count += 1
                veto_reasons.append(f"taker_sell={sell_pct:.0f}%")
            elif direction == "DOWN" and buy_pct > 60:
                veto_count += 1
                veto_reasons.append(f"taker_buy={buy_pct:.0f}%")

            # 5. CASCADE + taker divergence
            if ctx.vpin >= 0.65:
                if direction == "UP" and sell_pct > 55:
                    veto_count += 1
                    veto_reasons.append(f"cascade_taker_diverge")
                elif direction == "DOWN" and buy_pct > 55:
                    veto_count += 1
                    veto_reasons.append(f"cascade_taker_diverge")

        if veto_count >= 3:
            return GateResult(
                passed=False, gate_name=self.name,
                reason=f"CG VETO ({veto_count}): {', '.join(veto_reasons)}",
                data={"veto_count": veto_count, "reasons": veto_reasons},
            )

        return GateResult(
            passed=True, gate_name=self.name,
            reason=f"CG OK (veto={veto_count})",
            data={"veto_count": veto_count, "reasons": veto_reasons},
        )


    async def evaluate_immutable(self, ctx: GateContext) -> tuple[GateResult, GateContextDelta]:
        """CA-03 immutable path."""
        result = await self.evaluate(ctx)
        return (result, EMPTY_DELTA)

# ── Dynamic Cap Gate ───────────────────────────────────────────────────────

class DynamicCapGate:
    """G7: Confidence-scaled entry cap (v10.6).

    cap = base + (ceiling - base) × (conf - min_conf) / (max_conf - min_conf)

    SEQUOIA outputs smooth 0.65-0.88 probabilities. Higher confidence →
    willing to pay more (still profitable). Lower confidence → demands
    cheaper entry (better risk/reward compensates for uncertainty).

    Early entry zone (T-180..200) has a hard cap override to limit risk
    on low-accuracy early bets.

    Evidence (Apr 9, 56 trades):
      At $0.68 flat: WIN +$1.60, LOSS -$3.40 (2.1 wins to recover)
      At $0.55:     WIN +$2.78, LOSS -$3.40 (1.2 wins to recover)
      Confidence 80-90% = 86% WR, 70-80% = 73% WR — scaling is justified.

    Falls back to v9 fixed cap ($0.65) if DUNE not available.
    """
    name = "dynamic_cap"

    def __init__(self):
        # Confidence-scaled cap parameters
        self._scale_base = float(os.environ.get("V10_CAP_SCALE_BASE", "0.48"))
        self._scale_ceiling = float(os.environ.get("V10_CAP_SCALE_CEILING", "0.72"))
        self._scale_min_conf = float(os.environ.get("V10_CAP_SCALE_MIN_CONF", "0.65"))
        self._scale_max_conf = float(os.environ.get("V10_CAP_SCALE_MAX_CONF", "0.88"))
        self._floor = float(os.environ.get("V10_DUNE_CAP_FLOOR", "0.35"))
        # Early entry zone cap override (T-180..200)
        self._early_cap_max = float(os.environ.get("V10_EARLY_ENTRY_CAP_MAX", "0.63"))
        self._early_offset_threshold = int(os.environ.get("V10_EARLY_ENTRY_OFFSET", "180"))
        self._v9_fallback = float(os.environ.get("V9_CAP_GOLDEN", "0.65"))
        self._log = log.bind(gate="dynamic_cap")

    async def evaluate(self, ctx: GateContext) -> GateResult:
        dune_p = None

        # Get DUNE P(agreed direction) from context
        if ctx.dune_probability_up is not None and ctx.agreed_direction:
            p_up = ctx.dune_probability_up
            dune_p = p_up if ctx.agreed_direction == "UP" else (1.0 - p_up)

        if dune_p is not None:
            # Confidence-scaled cap: linear interpolation across confidence range
            conf_range = self._scale_max_conf - self._scale_min_conf
            if conf_range > 0:
                t = max(0.0, min(1.0, (dune_p - self._scale_min_conf) / conf_range))
            else:
                t = 0.5
            raw_cap = self._scale_base + (self._scale_ceiling - self._scale_base) * t
            cap = round(max(raw_cap, self._floor), 2)

            # Early entry zone: hard cap override for T-180..200
            if ctx.eval_offset and ctx.eval_offset > self._early_offset_threshold:
                cap = min(cap, self._early_cap_max)
                cap = round(cap, 2)

            self._log.info("cap.scaled",
                dune_p=f"{dune_p:.3f}", t=f"{t:.2f}", cap=f"${cap:.2f}",
                early_zone=bool(ctx.eval_offset and ctx.eval_offset > self._early_offset_threshold))
            return GateResult(
                passed=True, gate_name=self.name,
                reason=f"cap=${cap:.2f} (P={dune_p:.3f} t={t:.2f} [{self._scale_base:.2f},{self._scale_ceiling:.2f}])",
                data={"cap": cap, "dune_p": dune_p, "t": t, "source": "scaled"},
            )
        else:
            # Fallback to v9 fixed cap
            cap = self._v9_fallback
            return GateResult(
                passed=True, gate_name=self.name,
                reason=f"v9 fallback cap=${cap:.2f} (no DUNE data)",
                data={"cap": cap, "source": "v9_fallback"},
            )


    async def evaluate_immutable(self, ctx: GateContext) -> tuple[GateResult, GateContextDelta]:
        """CA-03 immutable path."""
        result = await self.evaluate(ctx)
        return (result, EMPTY_DELTA)

# ── Gate Pipeline ──────────────────────────────────────────────────────────

class GatePipeline:
    """Chains gates in order. Stops at first failure.

    CA-03: ENGINE_IMMUTABLE_GATES=true uses the immutable path where each gate
    returns (GateResult, GateContextDelta) and the pipeline folds deltas into
    a new GateContext between gates. The original ctx is never mutated.
    Default (false): legacy mutable path, gates mutate ctx in-place.
    """

    def __init__(self, gates: list):
        self._gates = gates
        self._log = log.bind(component="gate_pipeline")

    async def evaluate(self, ctx: GateContext) -> PipelineResult:
        if _IMMUTABLE_GATES:
            return await self._evaluate_immutable(ctx)
        return await self._evaluate_mutable(ctx)

    async def _evaluate_mutable(self, ctx: GateContext) -> PipelineResult:
        """Legacy mutable path -- gates mutate ctx in-place."""
        results = []
        for gate in self._gates:
            result = await gate.evaluate(ctx)
            results.append(result)
            if not result.passed:
                self._log.info("gate.failed", gate=result.gate_name, reason=result.reason)
                return PipelineResult(
                    passed=False, direction=ctx.agreed_direction,
                    gate_results=results, failed_gate=result.gate_name,
                    skip_reason=result.reason,
                )

        cap, dune_p = None, None
        for r in results:
            if r.data.get("cap"): cap = r.data["cap"]
            if r.data.get("dune_p"): dune_p = r.data["dune_p"]

        self._log.info("gate.all_passed",
            direction=ctx.agreed_direction,
            cap=f"${cap:.2f}" if cap else "none",
            dune_p=f"{dune_p:.3f}" if dune_p else "none",
            gates_passed=[r.gate_name for r in results])

        return PipelineResult(
            passed=True, direction=ctx.agreed_direction,
            cap=cap, dune_p=dune_p, gate_results=results,
        )

    async def _evaluate_immutable(self, ctx: GateContext) -> PipelineResult:
        """CA-03 immutable path -- folds GateContextDelta between gates.

        The original ctx is NEVER mutated. Each gate that implements
        evaluate_immutable() returns (GateResult, GateContextDelta).
        Gates without the method get a mutable COPY passed to evaluate().
        """
        results = []
        current_ctx = ctx  # never mutated

        for gate in self._gates:
            if hasattr(gate, 'evaluate_immutable'):
                result, delta = await gate.evaluate_immutable(current_ctx)
            else:
                mutable_copy = replace(current_ctx)
                result = await gate.evaluate(mutable_copy)
                delta = _infer_delta(current_ctx, mutable_copy)

            results.append(result)
            if not result.passed:
                self._log.info("gate.failed", gate=result.gate_name, reason=result.reason)
                return PipelineResult(
                    passed=False, direction=current_ctx.agreed_direction,
                    gate_results=results, failed_gate=result.gate_name,
                    skip_reason=result.reason,
                )
            current_ctx = _merge_context(current_ctx, delta)

        cap, dune_p = None, None
        for r in results:
            if r.data.get("cap"): cap = r.data["cap"]
            if r.data.get("dune_p"): dune_p = r.data["dune_p"]

        self._log.info("gate.all_passed",
            direction=current_ctx.agreed_direction,
            cap=f"${cap:.2f}" if cap else "none",
            dune_p=f"{dune_p:.3f}" if dune_p else "none",
            gates_passed=[r.gate_name for r in results])

        return PipelineResult(
            passed=True, direction=current_ctx.agreed_direction,
            cap=cap, dune_p=dune_p, gate_results=results,
        )


def _infer_delta(before: GateContext, after: GateContext) -> GateContextDelta:
    """Infer a GateContextDelta by diffing two GateContext instances."""
    kwargs = {}
    for fn in (
        "agreed_direction", "cg_threshold_modifier", "cg_confirms",
        "cg_bonus", "dune_probability_up", "dune_direction", "dune_model_version",
    ):
        bv = getattr(before, fn)
        av = getattr(after, fn)
        if bv != av:
            kwargs[fn] = av
    return GateContextDelta(**kwargs) if kwargs else EMPTY_DELTA
