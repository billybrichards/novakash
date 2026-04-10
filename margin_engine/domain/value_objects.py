"""
Domain value objects — immutable, validated, self-describing types.

These are the building blocks that entities and use cases compose.
All validation happens at construction time; once created, a value object
is guaranteed to be in a valid state.
"""
from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from typing import Optional


class TradeSide(enum.Enum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def opposite(self) -> TradeSide:
        return TradeSide.SHORT if self is TradeSide.LONG else TradeSide.LONG


class ExitReason(enum.Enum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"
    SIGNAL_REVERSAL = "SIGNAL_REVERSAL"          # legacy composite path, kept for old rows
    MAX_HOLD_TIME = "MAX_HOLD_TIME"              # legacy exit, used as v2 fallback
    MANUAL = "MANUAL"
    KILL_SWITCH = "KILL_SWITCH"
    # ── v4-aware exit reasons (PR B) ──
    # Distinct codes so telemetry can separate WHICH gate killed the trade.
    # PROBABILITY_REVERSAL and REGIME_DETERIORATED describe model/market
    # state changes; CONSENSUS_FAIL and MACRO_GATE_FLIP describe external
    # hard-gate flips; EVENT_GUARD and CASCADE_EXHAUSTED are preemptive
    # exits before something predictable happens.
    PROBABILITY_REVERSAL = "PROBABILITY_REVERSAL"   # p_up flipped or conviction too low
    REGIME_DETERIORATED = "REGIME_DETERIORATED"     # regime became CHOPPY / NO_EDGE
    CONSENSUS_FAIL = "CONSENSUS_FAIL"               # 6-source oracle divergence spiked
    MACRO_GATE_FLIP = "MACRO_GATE_FLIP"             # Claude flipped direction_gate against us
    EVENT_GUARD = "EVENT_GUARD"                     # forced exit before HIGH/EXTREME event
    CASCADE_EXHAUSTED = "CASCADE_EXHAUSTED"         # cascade FSM says about to reverse


class PositionState(enum.Enum):
    PENDING_ENTRY = "PENDING_ENTRY"
    OPEN = "OPEN"
    PENDING_EXIT = "PENDING_EXIT"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class Money:
    """
    Non-negative monetary amount with currency.

    Immutable. Arithmetic returns new Money instances.
    Negative amounts are forbidden — use signed floats for P&L deltas.
    """
    amount: float
    currency: str = "USDT"

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise ValueError(f"Money cannot be negative: {self.amount}")
        if math.isnan(self.amount) or math.isinf(self.amount):
            raise ValueError(f"Money must be finite: {self.amount}")

    def __add__(self, other: Money) -> Money:
        if self.currency != other.currency:
            raise ValueError(f"Cannot add {self.currency} + {other.currency}")
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        if self.currency != other.currency:
            raise ValueError(f"Cannot subtract {self.currency} - {other.currency}")
        result = self.amount - other.amount
        if result < 0:
            raise ValueError(f"Money subtraction would go negative: {self.amount} - {other.amount}")
        return Money(result, self.currency)

    def __mul__(self, factor: float) -> Money:
        if factor < 0:
            raise ValueError(f"Cannot multiply Money by negative factor: {factor}")
        return Money(self.amount * factor, self.currency)

    def __gt__(self, other: Money) -> bool:
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        return self.amount >= other.amount

    def __lt__(self, other: Money) -> bool:
        return self.amount < other.amount

    @classmethod
    def zero(cls, currency: str = "USDT") -> Money:
        return cls(0.0, currency)

    @classmethod
    def usd(cls, amount: float) -> Money:
        return cls(amount, "USDT")


@dataclass(frozen=True)
class Price:
    """Validated price — must be positive and finite."""
    value: float
    pair: str = "BTCUSDT"

    def __post_init__(self) -> None:
        if self.value <= 0 or math.isnan(self.value) or math.isinf(self.value):
            raise ValueError(f"Price must be positive and finite: {self.value}")


@dataclass(frozen=True)
class CompositeSignal:
    """
    Validated composite signal from the v3 scorer.
    score must be in [-1, +1], timescale must be one of the 9 defined.
    """
    score: float
    timescale: str
    asset: str = "BTC"
    timestamp: float = 0.0

    VALID_TIMESCALES = ("5m", "15m", "1h", "4h", "24h", "48h", "72h", "1w", "2w")

    def __post_init__(self) -> None:
        if not -1.0 <= self.score <= 1.0:
            raise ValueError(f"CompositeSignal score must be in [-1, 1]: {self.score}")
        if self.timescale not in self.VALID_TIMESCALES:
            raise ValueError(f"Invalid timescale: {self.timescale}")

    @property
    def is_bullish(self) -> bool:
        return self.score > 0

    @property
    def is_bearish(self) -> bool:
        return self.score < 0

    @property
    def strength(self) -> float:
        """Absolute signal strength [0, 1]."""
        return abs(self.score)

    @property
    def suggested_side(self) -> TradeSide:
        return TradeSide.LONG if self.is_bullish else TradeSide.SHORT


@dataclass(frozen=True)
class StopLevel:
    """Stop-loss or take-profit level with price and type."""
    price: float
    is_trailing: bool = False
    trail_pct: float = 0.0

    def __post_init__(self) -> None:
        if self.price <= 0:
            raise ValueError(f"StopLevel price must be positive: {self.price}")
        if self.is_trailing and self.trail_pct <= 0:
            raise ValueError(f"Trailing stop needs positive trail_pct: {self.trail_pct}")


@dataclass(frozen=True)
class ProbabilitySignal:
    """
    Calibrated probability forecast from a trained LightGBM head.

    Sourced from the TimesFM service's /v2/probability/15m endpoint. Unlike
    CompositeSignal (a heuristic blend of indicators in [-1, 1]),
    ProbabilitySignal is the output of a classifier trained on Polymarket
    UpDown window outcomes: it predicts the probability that the current
    window will close ABOVE its open price.

    The crucial difference from CompositeSignal: this is directional and
    calibrated. At probability_up > 0.70 the model was correct 77.5% of the
    time in the 5-day backtest (vs 50% random baseline). At probability_up
    > 0.75 the hit rate is 96.87% and the trade is profitable even after
    round-trip Binance taker fees.

    seconds_to_close is the horizon of this prediction: the model head is
    trained to forecast the window close that many seconds in the future.
    The position should be exited at (or before) window close, NOT on
    signal reversal — the whole point of the calibration is that the
    probability already accounts for interim noise.

    window_open_ts and window_close_ts are the unix timestamps of the
    prediction window boundaries, so the caller can avoid double-trading
    the same window.
    """
    probability_up: float       # [0, 1], calibrated
    asset: str
    timescale: str              # "15m" for now — the only trained horizon
    seconds_to_close: int       # seconds until the prediction target
    window_open_ts: int         # unix seconds, window boundary
    window_close_ts: int        # unix seconds, window boundary
    model_version: str          # for audit trail in margin_positions
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.probability_up <= 1.0:
            raise ValueError(
                f"ProbabilitySignal probability_up must be in [0, 1]: "
                f"{self.probability_up}"
            )
        if self.seconds_to_close < 0:
            raise ValueError(
                f"seconds_to_close cannot be negative: {self.seconds_to_close}"
            )

    @property
    def probability_down(self) -> float:
        return 1.0 - self.probability_up

    @property
    def conviction(self) -> float:
        """Distance from 0.5 — how far from random the model thinks this is."""
        return abs(self.probability_up - 0.5)

    @property
    def suggested_side(self) -> TradeSide:
        return TradeSide.LONG if self.probability_up > 0.5 else TradeSide.SHORT

    def meets_threshold(self, min_conviction: float) -> bool:
        """True if |p_up - 0.5| >= min_conviction (e.g., 0.20 → p>0.70 or p<0.30)."""
        return self.conviction >= min_conviction


@dataclass(frozen=True)
class FillResult:
    """
    Result of a filled market order.

    Carries exchange ground truth so the caller doesn't have to estimate fees
    or filled notional after the fact. For paper mode, the adapter populates
    these from its own simulation — they're still "actual" in that the paper
    calculation IS the paper outcome.

    commission is always expressed in USDT-equivalent. commission_asset
    records the original asset the fee was paid in (e.g. "USDT", "BNB")
    for audit purposes. commission_is_actual is True when the value came
    from the exchange's fill response; False when it's a fallback estimate
    (e.g. the exchange returned no fills array, or the commission was in an
    unrecognized asset that we couldn't convert to USDT).
    """
    order_id: str
    fill_price: Price
    filled_notional: float           # actual USDT filled (sum of price * qty across fills)
    commission: float = 0.0          # USDT-equivalent, always non-negative
    commission_asset: str = "USDT"
    commission_is_actual: bool = False

    def __post_init__(self) -> None:
        if self.commission < 0:
            raise ValueError(f"Commission cannot be negative: {self.commission}")
        if self.filled_notional < 0:
            raise ValueError(f"Filled notional cannot be negative: {self.filled_notional}")
        if math.isnan(self.commission) or math.isinf(self.commission):
            raise ValueError(f"Commission must be finite: {self.commission}")


# ═══════════════════════════════════════════════════════════════════════════
# v4 snapshot value objects
# ═══════════════════════════════════════════════════════════════════════════
#
# These dataclasses mirror the /v4/snapshot response shape documented in
# novakash-timesfm-repo/docs/V4_API_REFERENCE.md. They are consumed by the
# (new) V4SnapshotHttpAdapter and passed around as frozen immutable values.
#
# Design notes:
# - All fields are Optional where the upstream /v4/* endpoint can legitimately
#   return null (e.g. probability_up on a cold_start payload, cascade fields
#   when the FSM isn't active, recommended_action which is Phase 2).
# - Frozen dataclasses match the existing CompositeSignal / ProbabilitySignal
#   pattern — once constructed, instances are immutable and safe to share
#   across asyncio tasks without locking.
# - Defensive parsing lives in `V4Snapshot.from_dict`; tolerates missing
#   keys by falling back to sane defaults, so a partial payload never
#   raises in the hot poll path.


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
    """Claude-generated macro bias from /v4/macro.

    Written by the macro-observer on the TimesFM server every 60 seconds.
    status='ok' means Claude produced a valid bias; 'unavailable' / 'no_data'
    mean the observer hasn't written a row recently — in that case the
    engine should ignore the macro gates rather than hard-skipping.
    """
    bias: str = "NEUTRAL"               # BULL | BEAR | NEUTRAL
    confidence: int = 0                 # 0-100
    direction_gate: str = "ALLOW_ALL"   # ALLOW_ALL | SKIP_UP | SKIP_DOWN
    size_modifier: float = 1.0          # 0.5-1.5
    threshold_modifier: float = 1.0
    override_active: bool = False
    reasoning: Optional[str] = None
    age_s: Optional[float] = None
    status: str = "ok"                  # ok | unavailable | no_data


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
    status: str                              # ok | cold_start | no_model | scorer_error | stale
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

    regime: Optional[str] = None             # TRENDING_UP | TRENDING_DOWN | MEAN_REVERTING | CHOPPY | NO_EDGE
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

    timescales: dict = field(default_factory=dict)    # dict[str, TimescalePayload]

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
            asset=d["asset"],                                          # required
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
        p10=d.get("p10"), p25=d.get("p25"), p50=d.get("p50"),
        p75=d.get("p75"), p90=d.get("p90"),
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
