"""
v10 Gate System — Clean, composable trading gates.

Each gate evaluates a single condition and returns a GateResult.
The strategy chains gates in order: Agreement → DUNE → CG Veto → Dynamic Cap.

Usage:
    gates = GatePipeline([
        SourceAgreementGate(),
        DuneConfidenceGate(min_p=0.65),
        CoinGlassVetoGate(),
        DynamicCapGate(margin=0.05, floor=0.30, ceiling=0.75),
    ])
    result = await gates.evaluate(context)
    if result.passed:
        # Trade at result.data['cap']
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional, Protocol

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


# ── Gate Protocol ───────────────────────────────────────────────────────────

class Gate(Protocol):
    """Protocol for gate implementations."""
    name: str
    async def evaluate(self, ctx: GateContext) -> GateResult: ...


# ── Source Agreement Gate ───────────────────────────────────────────────────

class SourceAgreementGate:
    """G1: Chainlink + Tiingo must agree on direction.

    94.7% WR when agree (historical), 9.1% when disagree.
    This is the primary filter — blocks ~17% of windows.
    """
    name = "source_agreement"

    async def evaluate(self, ctx: GateContext) -> GateResult:
        if ctx.delta_chainlink is None or ctx.delta_tiingo is None:
            return GateResult(
                passed=False, gate_name=self.name,
                reason="missing CL or TI data",
            )

        cl_dir = "UP" if ctx.delta_chainlink > 0 else "DOWN"
        ti_dir = "UP" if ctx.delta_tiingo > 0 else "DOWN"

        if cl_dir != ti_dir:
            return GateResult(
                passed=False, gate_name=self.name,
                reason=f"CL={cl_dir} TI={ti_dir} DISAGREE",
                data={"cl_dir": cl_dir, "ti_dir": ti_dir},
            )

        # Store agreed direction in context for downstream gates
        ctx.agreed_direction = cl_dir
        return GateResult(
            passed=True, gate_name=self.name,
            reason=f"CL={cl_dir} TI={ti_dir} AGREE",
            data={"cl_dir": cl_dir, "ti_dir": ti_dir, "direction": cl_dir},
        )


# ── DUNE Confidence Gate ───────────────────────────────────────────────────

class DuneConfidenceGate:
    """G2: DUNE ML model must confirm direction with sufficient confidence.

    Replaces v9's VPIN-based gating. DUNE outputs continuous P(direction)
    trained on actual oracle outcomes (75.9% accuracy at T-60, 83.5% at T-30).

    Args:
        min_p: Minimum DUNE P(agreed direction) to trade (default 0.65)
        dune_client: TimesFMV2Client instance for API calls
    """
    name = "dune_confidence"

    def __init__(self, min_p: float = 0.65, dune_client=None):
        self._min_p = float(os.environ.get("V10_DUNE_MIN_P", str(min_p)))
        self._client = dune_client
        self._log = log.bind(gate="dune_confidence")

    async def evaluate(self, ctx: GateContext) -> GateResult:
        if not ctx.agreed_direction:
            return GateResult(
                passed=False, gate_name=self.name,
                reason="no agreed direction (agreement gate must run first)",
            )

        # v10.1: Minimum offset — don't trade too early
        _min_offset = int(os.environ.get("V10_MIN_EVAL_OFFSET", "180"))
        if ctx.eval_offset and ctx.eval_offset > _min_offset:
            return GateResult(
                passed=False, gate_name=self.name,
                reason=f"too early: T-{ctx.eval_offset} > T-{_min_offset}",
            )

        # Query DUNE API
        if self._client is None:
            # No client — pass through (shadow mode or disabled)
            return GateResult(
                passed=True, gate_name=self.name,
                reason="DUNE client not configured (pass-through)",
            )

        seconds_to_close = ctx.eval_offset or 60
        try:
            # ELM v3 is at the production endpoint (model="oak"), NOT cedar
            # The v3 model was deployed to production /v2/probability
            _model = os.environ.get("V10_DUNE_MODEL", "oak")
            result = await self._client.get_probability(
                asset=ctx.asset,
                seconds_to_close=seconds_to_close,
                model=_model,
            )
        except Exception as exc:
            self._log.warning("dune.query_failed", error=str(exc)[:100])
            # On error, pass through (don't block trades due to API issues)
            return GateResult(
                passed=True, gate_name=self.name,
                reason=f"DUNE query failed: {str(exc)[:50]} (pass-through)",
            )

        if not result or "probability_up" not in result:
            return GateResult(
                passed=True, gate_name=self.name,
                reason="DUNE returned invalid response (pass-through)",
            )

        p_up = float(result["probability_up"])
        p_down = 1.0 - p_up

        # P(agreed direction)
        dune_p = p_up if ctx.agreed_direction == "UP" else p_down

        # Store in context for downstream gates
        ctx.dune_probability_up = p_up
        ctx.dune_direction = "UP" if p_up > 0.5 else "DOWN"
        ctx.dune_model_version = result.get("model_version", "")

        self._log.info("dune.evaluated",
            asset=ctx.asset, offset=seconds_to_close,
            p_up=f"{p_up:.4f}", p_down=f"{p_down:.4f}",
            agreed_dir=ctx.agreed_direction, dune_p=f"{dune_p:.4f}",
            threshold=f"{self._min_p:.2f}",
            passed=dune_p >= self._min_p)

        if dune_p < self._min_p:
            return GateResult(
                passed=False, gate_name=self.name,
                reason=f"DUNE P({ctx.agreed_direction})={dune_p:.3f} < {self._min_p}",
                data={"dune_p": dune_p, "p_up": p_up, "threshold": self._min_p},
            )

        return GateResult(
            passed=True, gate_name=self.name,
            reason=f"DUNE P({ctx.agreed_direction})={dune_p:.3f} >= {self._min_p}",
            data={"dune_p": dune_p, "p_up": p_up, "threshold": self._min_p},
        )


# ── CoinGlass Veto Gate ────────────────────────────────────────────────────

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


# ── Dynamic Cap Gate ───────────────────────────────────────────────────────

class DynamicCapGate:
    """G4: Calculate entry cap from DUNE confidence.

    cap = DUNE_P - margin (5pp default)
    Bounded by floor ($0.30) and ceiling ($0.75).

    Falls back to v9 fixed cap ($0.65) if DUNE not available.
    """
    name = "dynamic_cap"

    def __init__(self):
        self._margin = float(os.environ.get("V10_DUNE_CAP_MARGIN", "0.05"))
        self._floor = float(os.environ.get("V10_DUNE_CAP_FLOOR", "0.30"))
        self._ceiling = float(os.environ.get("V10_DUNE_CAP_CEILING", "0.75"))
        self._v9_fallback = float(os.environ.get("V9_CAP_GOLDEN", "0.65"))

    async def evaluate(self, ctx: GateContext) -> GateResult:
        dune_p = None

        # Get DUNE P(agreed direction) from context
        if ctx.dune_probability_up is not None and ctx.agreed_direction:
            p_up = ctx.dune_probability_up
            dune_p = p_up if ctx.agreed_direction == "UP" else (1.0 - p_up)

        if dune_p is not None:
            cap = round(min(max(dune_p - self._margin, self._floor), self._ceiling), 2)
            return GateResult(
                passed=True, gate_name=self.name,
                reason=f"DUNE cap=${cap:.2f} (P={dune_p:.3f} - {self._margin}pp)",
                data={"cap": cap, "dune_p": dune_p, "source": "dune"},
            )
        else:
            # Fallback to v9 fixed cap
            cap = self._v9_fallback
            return GateResult(
                passed=True, gate_name=self.name,
                reason=f"v9 fallback cap=${cap:.2f} (no DUNE data)",
                data={"cap": cap, "source": "v9_fallback"},
            )


# ── Gate Pipeline ──────────────────────────────────────────────────────────

class GatePipeline:
    """Chains gates in order. Stops at first failure."""

    def __init__(self, gates: list):
        self._gates = gates
        self._log = log.bind(component="gate_pipeline")

    async def evaluate(self, ctx: GateContext) -> PipelineResult:
        results = []

        for gate in self._gates:
            result = await gate.evaluate(ctx)
            results.append(result)

            if not result.passed:
                self._log.info("gate.failed",
                    gate=result.gate_name, reason=result.reason)
                return PipelineResult(
                    passed=False,
                    direction=ctx.agreed_direction,
                    gate_results=results,
                    failed_gate=result.gate_name,
                    skip_reason=result.reason,
                )

        # All gates passed — extract cap and DUNE probability
        cap = None
        dune_p = None
        for r in results:
            if r.data.get("cap"):
                cap = r.data["cap"]
            if r.data.get("dune_p"):
                dune_p = r.data["dune_p"]

        self._log.info("gate.all_passed",
            direction=ctx.agreed_direction,
            cap=f"${cap:.2f}" if cap else "none",
            dune_p=f"{dune_p:.3f}" if dune_p else "none",
            gates_passed=[r.gate_name for r in results])

        return PipelineResult(
            passed=True,
            direction=ctx.agreed_direction,
            cap=cap,
            dune_p=dune_p,
            gate_results=results,
        )
