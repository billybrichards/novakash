"""
v4 snapshot value objects for HTTP API responses.

These dataclasses mirror the /v4/snapshot response shape documented in
novakash-timesfm-repo/docs/V4_API_REFERENCE.md. They are consumed by the
(new) V4SnapshotHttpAdapter and passed around as frozen immutable values.

Design notes:
- All fields are Optional where the upstream /v4/* endpoint can legitimately
  return null (e.g. probability_up on a cold_start payload, cascade fields
  when the FSM isn't active, recommended_action which is Phase 2).
- Frozen dataclasses match the existing CompositeSignal / ProbabilitySignal
  pattern — once constructed, instances are immutable and safe to share
  across asyncio tasks without locking.
- Defensive parsing lives in `V4Snapshot.from_dict`; tolerates missing
  keys by falling back to sane defaults, so a partial payload never
  raises in the hot poll path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from margin_engine.domain.value_objects import TradeSide


# ═══════════════════════════════════════════════════════════════════════════
# v4 snapshot value objects
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Quantiles:
    """TimesFM quantile endpoints at a single horizon step.

    Used for quantile-derived SL/TP: LONG stops at `1.25 * (last - p10)`,
    targets `0.85 * (p90 - last)` (and vice versa for SHORT).
    Any field can be None when the forecaster is in cold-start state.
    """

    p10: Optional[float] = None
    p25: Optional[float] = None
    p50: Optional[float] = None
    p75: Optional[float] = None
    p90: Optional[float] = None


@dataclass(frozen=True)
class Consensus:
    """6-source price reconciliation from /v4/consensus.

    safe_to_trade is the hard gate: when False, no entries AND no
    continuations are allowed, regardless of how strong the model signal is.
    The reason string is useful for post-hoc triage of why a trade got skipped.
    """

    safe_to_trade: bool
    safe_to_trade_reason: str = "unknown"
    reference_price: Optional[float] = None
    max_divergence_bps: float = 0.0
    source_agreement_score: float = 0.0


@dataclass(frozen=True)
class MacroBias:
    """Macro bias from /v4/macro (Qwen observer today; macro_v2 classifier
    after Phase C).

    Written by the macro-observer on the TimesFM server every 60 seconds.
    status='ok' means the producer returned a valid bias; 'unavailable' /
    'no_data' mean the observer hasn't written a row recently — in that
    case the engine ignores the macro gates rather than hard-skipping.

    `bias`/`confidence`/`direction_gate` describe the OVERALL synthesis
    across horizons. `timescale_map` carries the per-horizon breakdown
    (5m/15m/1h/4h) with the same fields per horizon — added in Phase A
    so Phase C can consume it without another engine deploy. Empty dict
    when the producer doesn't return a per-horizon map (e.g. legacy
    responses before the 2026-04-11 schema change).
    """

    bias: str = "NEUTRAL"  # BULL | BEAR | NEUTRAL
    confidence: int = 0  # 0-100
    direction_gate: str = "ALLOW_ALL"  # ALLOW_ALL | SKIP_UP | SKIP_DOWN
    size_modifier: float = 1.0  # 0.5-1.5
    threshold_modifier: float = 1.0
    override_active: bool = False
    reasoning: Optional[str] = None
    age_s: Optional[float] = None
    status: str = "ok"  # ok | unavailable | no_data
    timescale_map: dict = field(default_factory=dict)  # {"5m": {...}, ...}

    def for_timescale(self, ts: str) -> Optional[dict]:
        """Return the per-horizon bias block for a given timescale, or None.

        Used by consumers that want per-primary-timescale bias instead of
        the overall synthesis. The overall block is still the default
        because the gate stack was designed around a single bias per tick;
        Phase C introduces call sites that prefer the per-horizon view.
        """
        return self.timescale_map.get(ts) if self.timescale_map else None


@dataclass(frozen=True)
class Cascade:
    """Cascade FSM state from /v4/regime (per-timescale).

    exhaustion_t is the projected remaining lifetime of the current cascade
    in seconds. When it drops below ~30 and the cascade direction matches
    the open position's side, the engine should exit pre-emptively — the
    cascade is about to exhaust and price will retrace.
    """

    strength: Optional[float] = None
    tau1: Optional[float] = None
    tau2: Optional[float] = None
    exhaustion_t: Optional[float] = None
    signal: Optional[float] = None


@dataclass(frozen=True)
class TimescalePayload:
    """Single timescale block inside a /v4/snapshot response.

    The engine typically only reads one timescale (`v4_primary_timescale`,
    default '15m'), but the snapshot carries all requested timescales so
    alignment / cross-timescale checks can be implemented later without
    changing the adapter contract.
    """

    timescale: str
    status: str  # ok | cold_start | no_model | scorer_error | stale
    window_ts: int = 0
    window_close_ts: int = 0
    seconds_to_close: int = 0

    probability_up: Optional[float] = None
    probability_raw: Optional[float] = None
    model_version: Optional[str] = None

    quantiles_at_close: Quantiles = field(default_factory=Quantiles)
    expected_move_bps: Optional[float] = None
    vol_forecast_bps: Optional[float] = None
    downside_var_bps_p10: Optional[float] = None
    upside_var_bps_p90: Optional[float] = None

    regime: Optional[str] = (
        None  # TRENDING_UP | TRENDING_DOWN | MEAN_REVERTING | CHOPPY | NO_EDGE
    )
    composite_v3: Optional[float] = None
    cascade: Cascade = field(default_factory=Cascade)
    direction_agreement: float = 0.0

    @property
    def is_tradeable(self) -> bool:
        """Hard gate: only `status == 'ok'` with a non-None probability and
        a non-CHOPPY / non-NO_EDGE regime is eligible for entry.

        MEAN_REVERTING is tradeable here but requires opt-in
        (`v4_allow_mean_reverting=True`) at the use-case level.
        """
        return (
            self.status == "ok"
            and self.probability_up is not None
            and self.regime not in ("CHOPPY", "NO_EDGE", None)
        )

    @property
    def suggested_side(self) -> TradeSide:
        """LONG if p_up > 0.5 else SHORT. Caller should only consult this
        when the payload is tradeable."""
        p = self.probability_up if self.probability_up is not None else 0.5
        return TradeSide.LONG if p > 0.5 else TradeSide.SHORT

    def meets_threshold(self, min_conviction: float) -> bool:
        """True iff |probability_up - 0.5| >= min_conviction.

        A min_conviction of 0.10 means p_up > 0.60 or < 0.40 qualifies.
        Returns False if probability_up is None (cold_start / scorer_error).
        """
        if self.probability_up is None:
            return False
        return abs(self.probability_up - 0.5) >= min_conviction


@dataclass(frozen=True)
class V4Snapshot:
    """Top-level /v4/snapshot response.

    Cached by V4SnapshotHttpAdapter and exposed via .get_latest() to the
    use cases. Construct only via `V4Snapshot.from_dict(response_json)` —
    the parser is defensive and tolerates missing / null fields.
    """

    asset: str
    ts: float
    last_price: Optional[float] = None
    server_version: str = "unknown"
    strategy: str = "unknown"

    consensus: Consensus = field(default_factory=lambda: Consensus(safe_to_trade=False))
    macro: MacroBias = field(default_factory=MacroBias)

    # Event-calendar fields. max_impact_in_window is None when there are
    # no events within the lookahead window; otherwise one of LOW/MEDIUM/
    # HIGH/EXTREME.
    max_impact_in_window: Optional[str] = None
    minutes_to_next_high_impact: Optional[float] = None

    timescales: dict = field(default_factory=dict)  # dict[str, TimescalePayload]

    @classmethod
    def from_dict(cls, d: dict) -> "V4Snapshot":
        """Defensive parser over a /v4/snapshot JSON response.

        Never raises on missing keys — fields fall back to sane defaults
        so a partial payload from an upstream hiccup doesn't break the
        adapter's hot loop. Any genuine parse error (e.g. asset missing
        entirely) should surface as a KeyError from the single required
        access, which the adapter catches and logs.
        """
        timescales: dict = {}
        for ts_name, ts_data in (d.get("timescales") or {}).items():
            if not isinstance(ts_data, dict):
                continue
            timescales[ts_name] = _parse_timescale(ts_name, ts_data)

        return cls(
            asset=d["asset"],  # required
            ts=float(d.get("ts", 0.0) or 0.0),
            last_price=d.get("last_price"),
            server_version=str(d.get("server_version", "unknown")),
            strategy=str(d.get("strategy", "unknown")),
            consensus=_parse_consensus(d.get("consensus", {})),
            macro=_parse_macro(d.get("macro", {})),
            max_impact_in_window=d.get("max_impact_in_window"),
            minutes_to_next_high_impact=d.get("minutes_to_next_high_impact"),
            timescales=timescales,
        )

    def get_tradeable(self, timescale: str) -> Optional[TimescalePayload]:
        """Return the timescale payload iff it's in a tradeable state.

        Convenience for the entry use case — collapses "payload exists AND
        is_tradeable" into one call.
        """
        payload = self.timescales.get(timescale)
        if payload is None or not payload.is_tradeable:
            return None
        return payload


def _parse_consensus(d: dict) -> Consensus:
    return Consensus(
        safe_to_trade=bool(d.get("safe_to_trade", False)),
        safe_to_trade_reason=str(d.get("safe_to_trade_reason", "unknown")),
        reference_price=d.get("reference_price"),
        max_divergence_bps=float(d.get("max_divergence_bps", 0.0) or 0.0),
        source_agreement_score=float(d.get("source_agreement_score", 0.0) or 0.0),
    )


def _parse_macro(d: dict) -> MacroBias:
    # timescale_map is passthrough-preserved as raw dict; consumers that
    # want strongly-typed per-horizon blocks can cast it themselves. Keeping
    # it dict-typed here avoids a second parser for the same shape the
    # producer already serialises.
    raw_ts_map = d.get("timescale_map")
    ts_map = raw_ts_map if isinstance(raw_ts_map, dict) else {}
    return MacroBias(
        bias=str(d.get("bias", "NEUTRAL")),
        confidence=int(d.get("confidence", 0) or 0),
        direction_gate=str(d.get("direction_gate", "ALLOW_ALL")),
        size_modifier=float(d.get("size_modifier", 1.0) or 1.0),
        threshold_modifier=float(d.get("threshold_modifier", 1.0) or 1.0),
        override_active=bool(d.get("override_active", False)),
        reasoning=d.get("reasoning"),
        age_s=d.get("age_s"),
        status=str(d.get("status", "ok")),
        timescale_map=ts_map,
    )


def _parse_cascade(d: dict) -> Cascade:
    if not isinstance(d, dict):
        return Cascade()
    return Cascade(
        strength=d.get("strength"),
        tau1=d.get("tau1"),
        tau2=d.get("tau2"),
        exhaustion_t=d.get("exhaustion_t"),
        signal=d.get("signal"),
    )


def _parse_quantiles(d: dict) -> Quantiles:
    if not isinstance(d, dict):
        return Quantiles()
    return Quantiles(
        p10=d.get("p10"),
        p25=d.get("p25"),
        p50=d.get("p50"),
        p75=d.get("p75"),
        p90=d.get("p90"),
    )


def _parse_timescale(name: str, d: dict) -> TimescalePayload:
    alignment = d.get("alignment") or {}
    return TimescalePayload(
        timescale=name,
        status=str(d.get("status", "unknown")),
        window_ts=int(d.get("window_ts", 0) or 0),
        window_close_ts=int(d.get("window_close_ts", 0) or 0),
        seconds_to_close=int(d.get("seconds_to_close", 0) or 0),
        probability_up=d.get("probability_up"),
        probability_raw=d.get("probability_raw"),
        model_version=d.get("model_version"),
        quantiles_at_close=_parse_quantiles(d.get("quantiles_at_close") or {}),
        expected_move_bps=d.get("expected_move_bps"),
        vol_forecast_bps=d.get("vol_forecast_bps"),
        downside_var_bps_p10=d.get("downside_var_bps_p10"),
        upside_var_bps_p90=d.get("upside_var_bps_p90"),
        regime=d.get("regime"),
        composite_v3=d.get("composite_v3"),
        cascade=_parse_cascade(d.get("cascade") or {}),
        direction_agreement=float(alignment.get("direction_agreement", 0.0) or 0.0),
    )
