"""
v10.3 Gate System — Full decision surface with CoinGlass taker flow integration.

7-gate pipeline: Agreement → TakerFlow → CGConfirmation → DUNE → Spread → DynamicCap.

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

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

    # v10.3: CoinGlass modifiers (set by TakerFlowGate + CGConfirmationGate)
    cg_threshold_modifier: float = 0.0  # +0.05 penalty or -0.02 bonus from taker flow
    cg_confirms: int = 0                # 0-3 confirming CG signals
    cg_bonus: float = 0.0              # Confirmation bonus (subtracted from threshold)

    # Gamma / Polymarket CLOB prices (for spread gate)
    gamma_up_price: Optional[float] = None
    gamma_down_price: Optional[float] = None


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
    """G4: DUNE ML model must confirm direction with sufficient confidence.

    v10.3: Full decision surface with 4 threshold components:
      effective = regime_base + offset_penalty + down_penalty + cg_modifier - cg_bonus

    Components:
      - regime_base: per-regime threshold (ELM v3 calibrated)
      - offset_penalty: linear decay, 0 at T-60, 0.005 per 20s (max from env)
      - down_penalty: +0.03 for DOWN predictions (9.3pp less accurate, N=865)
      - cg_modifier: +0.05 if taker opposing, -0.02 if taker aligned (from TakerFlowGate)
      - cg_bonus: -0.02 if 2+ CG confirmation signals (from CGConfirmationGate)
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
        self._down_penalty = float(os.environ.get("V10_DOWN_PENALTY", "0.0"))
        self._client = dune_client
        self._log = log.bind(gate="dune_confidence")

        self._regime_thresholds: dict[str, float] = {}
        for regime, default in self._REGIME_DEFAULTS.items():
            env_key = self._REGIME_ENV[regime]
            self._regime_thresholds[regime] = float(
                os.environ.get(env_key, str(default))
            )

    def _effective_threshold(self, ctx: GateContext, regime: str, eval_offset: Optional[int]) -> float:
        """Calculate threshold = regime_base + offset + down + cg_modifier - cg_bonus.

        Per-20s offset granularity from decision surface spec (Section 5, Gate 2).
        """
        base = self._regime_thresholds.get(regime, self._base_min_p)

        # Offset penalty: 0.005 per 20 seconds beyond T-60, capped
        offset_penalty = 0.0
        if eval_offset is not None and eval_offset > 60:
            excess = eval_offset - 60
            offset_penalty = min(self._offset_penalty_max, excess * 0.005 / 20)

        # DOWN penalty: +0.03 (9.3pp accuracy gap, N=865)
        down_penalty = self._down_penalty if ctx.agreed_direction == "DOWN" else 0.0

        # CG modifiers from upstream gates
        cg_mod = ctx.cg_threshold_modifier  # +0.05 (taker opposing) or 0.0
        cg_bonus = ctx.cg_bonus             # 0.02 (3-signal confirmation) or 0.0

        effective = base + offset_penalty + down_penalty + cg_mod - cg_bonus
        return round(effective, 4)

    async def evaluate(self, ctx: GateContext) -> GateResult:
        if not ctx.agreed_direction:
            return GateResult(
                passed=False, gate_name=self.name,
                reason="no agreed direction (agreement gate must run first)",
            )

        # Global minimum offset — don't trade too early
        _min_offset = int(os.environ.get("V10_MIN_EVAL_OFFSET", "180"))
        if ctx.eval_offset and ctx.eval_offset > _min_offset:
            return GateResult(
                passed=False, gate_name=self.name,
                reason=f"too early: T-{ctx.eval_offset} > T-{_min_offset}",
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
        threshold = self._effective_threshold(ctx, regime, ctx.eval_offset)

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
            _model = os.environ.get("V10_DUNE_MODEL", "oak")
            result = await self._client.get_probability(
                asset=ctx.asset,
                seconds_to_close=seconds_to_close,
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

        self._log.info("dune.evaluated",
            asset=ctx.asset, offset=seconds_to_close,
            p_up=f"{p_up:.4f}", dune_p=f"{dune_p:.4f}",
            agreed_dir=ctx.agreed_direction,
            regime=regime, threshold=f"{threshold:.4f}",
            components=components,
            passed=dune_p >= threshold)

        data = {"dune_p": dune_p, "p_up": p_up, "threshold": threshold,
                "regime": regime, "offset": ctx.eval_offset,
                "down_penalty": self._down_penalty if ctx.agreed_direction == "DOWN" else 0.0,
                "cg_modifier": ctx.cg_threshold_modifier, "cg_bonus": ctx.cg_bonus}

        if dune_p < threshold:
            return GateResult(
                passed=False, gate_name=self.name,
                reason=f"DUNE P({ctx.agreed_direction})={dune_p:.3f} < {threshold:.3f} ({regime} T-{ctx.eval_offset} {components})",
                data=data,
            )

        return GateResult(
            passed=True, gate_name=self.name,
            reason=f"DUNE P({ctx.agreed_direction})={dune_p:.3f} >= {threshold:.3f} ({regime} T-{ctx.eval_offset})",
            data=data,
        )


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


# ── Dynamic Cap Gate ───────────────────────────────────────────────────────

class DynamicCapGate:
    """G4: Calculate entry cap from DUNE confidence.

    cap = DUNE_P - margin (5pp default)
    Bounded by floor ($0.35) and ceiling ($0.68).
    At 76.5% WR, breakeven is ~$0.57. Ceiling $0.68 = 11pp safety margin.

    Falls back to v9 fixed cap ($0.65) if DUNE not available.
    """
    name = "dynamic_cap"

    def __init__(self):
        self._margin = float(os.environ.get("V10_DUNE_CAP_MARGIN", "0.05"))
        self._floor = float(os.environ.get("V10_DUNE_CAP_FLOOR", "0.35"))
        self._ceiling = float(os.environ.get("V10_DUNE_CAP_CEILING", "0.68"))
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
