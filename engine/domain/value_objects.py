"""Domain value objects for the engine's Clean Architecture layer.

Phase 0 deliverable (CA-01, CA-02).  These frozen dataclasses define the
data contracts exchanged between domain ports and use cases.  Inner layers
depend only on these types -- never on ORM models, raw dicts, or framework
types.

Stubs that are not yet consumed by an extracted use case retain ``pass``
bodies and will be fleshed out when the corresponding use case lands.

Fields are added incrementally: a value object gains fields when the first
use case that *produces or consumes* it is extracted, so we know exactly
which fields the contract requires.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Tuple


# ---------------------------------------------------------------------------
# Shared identity types
# ---------------------------------------------------------------------------

_VALID_TIMEFRAMES = {"5m", "15m", "1h", "4h", "1d"}
_VALID_DURATIONS = {300, 900, 3600, 14400, 86400}
_TF_TO_SECS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
_SECS_TO_TF = {v: k for k, v in _TF_TO_SECS.items()}


@dataclass(frozen=True)
class WindowKey:
    """Unique identifier for a trading window.

    Accepts either ``timeframe`` ("5m", "15m", …) or ``duration_secs`` (300, 900, …)
    as the 3rd positional / keyword argument.  Both forms are valid — the stored
    field is ``timeframe`` for backwards-compatibility with all production callers.

    Tests may use ``duration_secs=`` as a keyword; the value is converted to the
    canonical timeframe string in ``__post_init__``.
    """

    asset: str
    window_ts: int
    timeframe: str = "5m"
    # duration_secs as an accepted keyword — stored internally then converted
    duration_secs: int = field(default=0, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Handle duration_secs → timeframe conversion
        if self.duration_secs and self.duration_secs in _SECS_TO_TF:
            object.__setattr__(self, "timeframe", _SECS_TO_TF[self.duration_secs])
        elif self.duration_secs and self.duration_secs not in _SECS_TO_TF:
            raise ValueError(f"duration_secs must be one of {set(_VALID_DURATIONS)}")

        if not self.asset:
            raise ValueError("asset must be non-empty")
        if self.window_ts <= 0:
            raise ValueError("window_ts must be positive")
        if self.timeframe not in _VALID_TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {_VALID_TIMEFRAMES}")
        # Sync duration_secs from timeframe
        object.__setattr__(self, "duration_secs", _TF_TO_SECS.get(self.timeframe, 300))

    @property
    def key(self) -> str:
        """Short key without timeframe: '{asset}-{window_ts}'."""
        return f"{self.asset}-{self.window_ts}"

    def __str__(self) -> str:
        """Canonical string representation: BTC-1776201300-5m."""
        return f"{self.asset}-{self.window_ts}-{self.timeframe}"


# ---------------------------------------------------------------------------
# Market feed types (consumed by EvaluateWindowUseCase)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tick:
    """Single price observation from a market feed."""

    source: str
    asset: str
    price: float
    timestamp: float

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("source must be non-empty")
        if not math.isfinite(self.price):
            raise ValueError("price must be finite")
        if self.price <= 0:
            raise ValueError("price must be positive")
        if self.timestamp <= 0:
            raise ValueError("timestamp must be positive")


@dataclass(frozen=True)
class WindowClose:
    """Event emitted when a 5-minute window closes."""

    asset: str
    window_ts: int
    duration_secs: int
    open_price: float
    close_ts: float

    def __post_init__(self) -> None:
        if self.duration_secs not in _VALID_DURATIONS:
            raise ValueError(f"duration_secs must be one of {_VALID_DURATIONS}")
        if self.open_price <= 0:
            raise ValueError("open_price must be positive")

    @property
    def window_key(self) -> "WindowKey":
        return WindowKey(asset=self.asset, window_ts=self.window_ts, duration_secs=self.duration_secs)


@dataclass(frozen=True)
class DeltaSet:
    """Per-source delta triple (CL/TI/BIN) for a window."""

    delta_chainlink: Optional[float] = None
    delta_tiingo: Optional[float] = None
    delta_binance: Optional[float] = None

    def __post_init__(self) -> None:
        for name in ("delta_chainlink", "delta_tiingo", "delta_binance"):
            val = getattr(self, name)
            if val is not None and not math.isfinite(val):
                raise ValueError(f"{name} must be finite")

    @property
    def available_count(self) -> int:
        return sum(1 for d in (self.delta_chainlink, self.delta_tiingo, self.delta_binance) if d is not None)

    @property
    def agreeing_sign(self) -> Optional[str]:
        """'UP' | 'DOWN' | None — majority sign or None if no majority."""
        signs = [d for d in (self.delta_chainlink, self.delta_tiingo, self.delta_binance) if d is not None]
        if not signs:
            return None
        ups = sum(1 for s in signs if s > 0)
        downs = sum(1 for s in signs if s < 0)
        if ups > downs:
            return "UP"
        if downs > ups:
            return "DOWN"
        return None


# ---------------------------------------------------------------------------
# Signal / evaluation types (consumed by EvaluateWindowUseCase)
# ---------------------------------------------------------------------------

_VALID_EVAL_TIMEFRAMES = {"5m", "15m"}
_VALID_DECISIONS = {"TRADE", "SKIP", "ERROR"}


@dataclass(frozen=True)
class SignalEvaluation:
    """One row of the signal_evaluations audit table."""

    window_ts: int
    asset: str
    timeframe: str
    eval_offset: int
    decision: str = "SKIP"

    def __post_init__(self) -> None:
        if self.timeframe not in _VALID_EVAL_TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {_VALID_EVAL_TIMEFRAMES}")
        if self.eval_offset < 0:
            raise ValueError("eval_offset must be >= 0")
        if self.decision not in _VALID_DECISIONS:
            raise ValueError(f"decision must be one of {_VALID_DECISIONS}")


@dataclass(frozen=True)
class ClobSnapshot:
    """Point-in-time snapshot of a CLOB order book."""

    asset: str
    timeframe: str
    window_ts: int

    def __post_init__(self) -> None:
        if self.timeframe not in _VALID_EVAL_TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {_VALID_EVAL_TIMEFRAMES}")


@dataclass(frozen=True)
class GateAuditRow:
    """Audit row recording which gates ran and their results."""

    window_ts: int
    asset: str
    timeframe: str
    eval_offset: int
    decision: str = "SKIP"

    def __post_init__(self) -> None:
        if self.timeframe not in _VALID_EVAL_TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {_VALID_EVAL_TIMEFRAMES}")
        if self.eval_offset < 0:
            raise ValueError("eval_offset must be >= 0")
        if self.decision not in _VALID_DECISIONS:
            raise ValueError(f"decision must be one of {_VALID_DECISIONS}")


@dataclass(frozen=True)
class WindowSnapshot:
    """Full snapshot of a window for backfill and UI hydration."""

    window_ts: int
    asset: str
    timeframe: str
    is_live: bool = False

    def __post_init__(self) -> None:
        if self.timeframe not in _VALID_EVAL_TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {_VALID_EVAL_TIMEFRAMES}")


@dataclass(frozen=True)
class WindowEvaluationTrace:
    """Shared signal surface for one window evaluation tick.

    One row per ``(asset, window_ts, timeframe, eval_offset)``.
    This is the window-centric source of truth for what the engine saw
    before individual strategies evaluated.
    """

    asset: str
    window_ts: int
    timeframe: str
    eval_offset: Optional[int]
    surface_data: dict = field(default_factory=dict)
    assembled_at: float = 0.0


@dataclass(frozen=True)
class StrategyEvaluationTrace:
    """Structured strategy-level decision for one evaluation tick.

    Current persistence is handled by ``strategy_decisions``. This value
    object defines the cleaner long-term contract for application/use-case
    code that wants a domain-level strategy trace.
    """

    strategy_id: str
    strategy_version: str
    asset: str
    window_ts: int
    timeframe: str
    eval_offset: Optional[int]
    mode: str
    action: str
    direction: Optional[str]
    confidence: Optional[str]
    confidence_score: Optional[float]
    entry_cap: Optional[float]
    collateral_pct: Optional[float]
    entry_reason: str
    skip_reason: Optional[str]
    metadata: dict = field(default_factory=dict)
    evaluated_at: float = 0.0


@dataclass(frozen=True)
class GateCheckTrace:
    """Structured outcome for one gate check within a strategy evaluation."""

    asset: str
    window_ts: int
    timeframe: str
    eval_offset: Optional[int]
    strategy_id: str
    gate_order: int
    gate_name: str
    passed: bool
    mode: str
    action: str
    direction: Optional[str]
    reason: str = ""
    skip_reason: Optional[str] = None
    observed_data: dict = field(default_factory=dict)
    config_data: dict = field(default_factory=dict)
    evaluated_at: float = 0.0


@dataclass(frozen=True)
class WindowOutcomeTrace:
    """Resolved outcome linked back to a window and any executed trade."""

    asset: str
    window_ts: int
    timeframe: str
    actual_direction: Optional[str]
    outcome: Optional[str]
    pnl_usd: Optional[float]
    order_id: Optional[str] = None
    strategy_id: Optional[str] = None
    resolved_at: float = 0.0


@dataclass(frozen=True)
class WindowTraceView:
    """Aggregated read model for one window evaluation trace."""

    asset: str
    window_ts: int
    timeframe: str
    eval_offset: Optional[int]
    surface_data: dict = field(default_factory=dict)
    strategy_decisions: list = field(default_factory=list)
    gate_checks: list = field(default_factory=list)
    eligible_now: list[str] = field(default_factory=list)
    blocked_by_signal: list[str] = field(default_factory=list)
    blocked_by_timing: list[str] = field(default_factory=list)
    inactive_this_offset: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StrategyWindowAnalysis:
    """Grouped analysis of one strategy over many window evaluation ticks."""

    strategy_id: str
    timeframe: str
    asset: str
    total_evaluations: int
    tradeable_evaluations: int
    non_tradeable_evaluations: int
    executed_trades: int
    inactive_evaluations: int = 0
    blocked_by_timing: int = 0
    blocked_by_signal: int = 0
    latest_surface_examples: list[dict] = field(default_factory=list)
    recent_tradeable_examples: list[dict] = field(default_factory=list)
    recent_non_tradeable_examples: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Polymarket trading types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FillResult:
    """Result of a CLOB order placement (filled size, price, fees).

    Fields populated by PolymarketClientPort.place_order().
    """

    filled: bool = False
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    shares: Optional[float] = None
    attempts: int = 0
    # Legacy aliases retained for production adapters
    filled_size: Optional[float] = None
    filled_price: Optional[float] = None
    fees: float = 0.0
    status: str = "FILLED"

    def __post_init__(self) -> None:
        if self.fill_price is not None:
            if not math.isfinite(self.fill_price):
                raise ValueError("fill_price must be finite")
            if self.fill_price < 0:
                raise ValueError("fill_price cannot be negative")
        if self.attempts < 0:
            raise ValueError("attempts cannot be negative")
        if self.filled and not self.order_id:
            raise ValueError("filled result must have an order_id")


@dataclass(frozen=True)
class WindowMarket:
    """Gamma market lookup result for an (asset, window_ts) pair.

    Contains the up/down CLOB token IDs needed for order placement.
    """

    asset: str = ""
    window_ts: int = 0
    up_token_id: Optional[str] = None
    down_token_id: Optional[str] = None
    up_price: Optional[float] = None
    down_price: Optional[float] = None
    # Legacy fields retained for production adapters
    condition_id: Optional[str] = None
    market_slug: Optional[str] = None
    active: bool = True

    def __post_init__(self) -> None:
        if self.up_price is not None and not math.isfinite(self.up_price):
            raise ValueError("up_price must be finite")
        if self.down_price is not None and not math.isfinite(self.down_price):
            raise ValueError("down_price must be finite")

    @property
    def has_tokens(self) -> bool:
        return bool(self.up_token_id and self.down_token_id)


@dataclass(frozen=True)
class OrderBook:
    """Live CLOB order book for a single token."""

    token_id: str = ""
    bids: Tuple = field(default_factory=tuple)
    asks: Tuple = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.token_id:
            raise ValueError("token_id must be non-empty")

    @property
    def spread(self) -> Optional[float]:
        if not self.bids or not self.asks:
            return None
        best_bid = self.bids[0][0]
        best_ask = self.asks[0][0]
        return best_ask - best_bid


# ---------------------------------------------------------------------------
# Manual-trade types (consumed by ExecuteManualTradeUseCase)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingTrade:
    """A manual-trade row with status='pending_live' from the dashboard.

    Produced by PolymarketClientPort.poll_pending_trades().
    Maps to the manual_trades DB table row.
    """

    trade_id: str
    window_ts: int
    asset: str
    direction: str  # "UP" or "DOWN"
    entry_price: float
    stake_usd: float
    timeframe: str = "5m"

    def __post_init__(self) -> None:
        if self.direction not in ("UP", "DOWN"):
            raise ValueError(f"direction must be 'UP' or 'DOWN', got {self.direction!r}")
        if not math.isfinite(self.entry_price):
            raise ValueError("entry_price must be finite")
        if self.entry_price > 1.0:
            raise ValueError("entry_price must be <= 1.0")

    @property
    def clob_side(self) -> str:
        return "YES" if self.direction == "UP" else "NO"


@dataclass(frozen=True)
class ManualTradeOutcome:
    """Result of processing one pending manual trade.

    Produced by ExecuteManualTradeUseCase.drain_once().
    """

    trade_id: str
    status: str  # "open", "failed_no_token", "failed: <reason>"
    clob_order_id: Optional[str] = None
    paper: bool = False
    token_source: Optional[str] = None  # "recent_windows" | "market_data_db"

    _VALID_STATUSES = frozenset({"open", "failed_no_token"})

    def __post_init__(self) -> None:
        # Allow "open", "failed_no_token", or anything starting with "failed:"
        if self.status not in self._VALID_STATUSES and not self.status.startswith("failed:"):
            raise ValueError(f"invalid manual trade status: {self.status!r}")


# ---------------------------------------------------------------------------
# Trade decision / skip types (consumed by EvaluateWindowUseCase -- stubs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeDecision:
    """Structured decision output from the gate pipeline."""

    window_ts: int
    asset: str
    timeframe: str
    direction: str  # "YES" | "NO"
    eval_offset: int
    entry_price: float
    stake_usd: float
    engine_version: str = "v10.0"

    def __post_init__(self) -> None:
        if self.direction not in ("YES", "NO"):
            raise ValueError(f"direction must be 'YES' or 'NO', got {self.direction!r}")
        if self.eval_offset < 0:
            raise ValueError("eval_offset must be >= 0")


@dataclass(frozen=True)
class SkipSummary:
    """Consolidated skip summary for a window where all offsets were skipped."""

    window_key: str
    asset: str
    window_ts: int
    n_evals: int

    def __post_init__(self) -> None:
        if not self.window_key:
            raise ValueError("window_key must be non-empty")
        if self.n_evals < 0:
            raise ValueError("n_evals must be >= 0")


# ---------------------------------------------------------------------------
# Heartbeat / sitrep types (consumed by PublishHeartbeatUseCase)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SitrepPayload:
    """5-minute SITREP payload for the heartbeat Telegram message.

    Built by PublishHeartbeatUseCase.tick() every 5 minutes.

    win_rate is computed from wins_today / (wins_today + losses_today).
    Any win_rate= keyword argument passed to the constructor is silently
    ignored (the field below accepts and discards it via the sentinel
    mechanism in __post_init__).  This allows the legacy production
    call-site to still pass win_rate=<float> without crashing.
    """

    engine_status: str  # "ACTIVE" | "KILLED" | "running"
    paper_mode: bool = False
    is_killed: bool = False
    wallet_balance: float = 0.0
    bankroll: float = 0.0
    starting_bankroll: float = 0.0
    daily_pnl: float = 0.0
    portfolio_value: float = 0.0
    wins_today: int = 0
    losses_today: int = 0
    # Legacy production fields (retained for backwards compat)
    mode_label: str = ""  # "PAPER" | "LIVE"
    # win_rate is stored but overwritten in __post_init__ with computed value
    win_rate: float = 0.0
    vpin: float = 0.0
    vpin_regime: str = ""
    btc_price: float = 0.0
    open_positions: int = 0
    drawdown_pct: float = 0.0
    kill_switch_active: bool = False
    # Optional rich blocks (pre-formatted Markdown strings)
    recent_trades_block: str = ""
    pending_block: str = ""
    coinglass_block: str = ""
    # v12: HMM regime from V4 snapshot
    hmm_regime: Optional[str] = None
    hmm_regime_confidence: Optional[float] = None
    # v12: Feed health status
    feed_health: Optional[dict] = None
    # v12: Strategy port last decisions summary
    strategy_summary: Optional[str] = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.wallet_balance):
            raise ValueError("wallet_balance must be finite")
        if self.wins_today < 0:
            raise ValueError("wins_today must be >= 0")
        if self.losses_today < 0:
            raise ValueError("losses_today must be >= 0")
        # Compute win_rate from wins/losses, overriding any passed-in value
        total = self.wins_today + self.losses_today
        computed = self.wins_today / total if total > 0 else 0.0
        object.__setattr__(self, "win_rate", computed)


@dataclass(frozen=True)
class HeartbeatRow:
    """One row written to the system_state table by the heartbeat loop.

    Written by PublishHeartbeatUseCase.tick() every 10 seconds.
    """

    engine_status: str
    active_positions: int = 0
    current_balance: Optional[float] = None
    peak_balance: Optional[float] = None
    drawdown_pct: float = 0.0
    last_vpin: Optional[float] = None
    last_cascade_state: Optional[str] = None
    config_snapshot: dict = field(default_factory=dict)
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if not self.engine_status:
            raise ValueError("engine_status must be non-empty")
        if self.current_balance is not None and not math.isfinite(self.current_balance):
            raise ValueError("current_balance must be finite")


# ---------------------------------------------------------------------------
# Window lifecycle types
# ---------------------------------------------------------------------------


_VALID_WINDOW_OUTCOMES = {"WIN", "LOSS", "PUSH"}


class WindowOutcome:
    """Outcome of a resolved window (win/loss/push, PnL).

    Used both as a value object (instantiated with outcome/pnl/etc.) and as
    a namespace for class-level direction constants (WindowOutcome.UP / .DOWN)
    that are passed to mark_resolved() as plain strings.
    """

    # Class-level direction constants used by pg_window_repo.mark_resolved()
    UP: str = "UP"
    DOWN: str = "DOWN"

    def __init__(
        self,
        window_ts: int = 0,
        asset: str = "",
        outcome: str = "PUSH",
        pnl_usd: float = 0.0,
        resolved_at: float = 0.0,
        actual_direction: Optional[str] = None,
    ) -> None:
        if outcome not in _VALID_WINDOW_OUTCOMES:
            raise ValueError(f"outcome must be one of {_VALID_WINDOW_OUTCOMES}, got {outcome!r}")
        if actual_direction is not None and actual_direction not in ("UP", "DOWN"):
            raise ValueError(
                f"actual_direction must be 'UP', 'DOWN', or None, got {actual_direction!r}"
            )
        self.window_ts = window_ts
        self.asset = asset
        self.outcome = outcome
        self.pnl_usd = pnl_usd
        self.resolved_at = resolved_at
        self.actual_direction = actual_direction

    def __str__(self) -> str:
        return self.outcome

    def __repr__(self) -> str:
        return f"WindowOutcome(outcome={self.outcome!r}, pnl_usd={self.pnl_usd})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, WindowOutcome):
            return (self.window_ts, self.asset, self.outcome, self.pnl_usd) == (
                other.window_ts, other.asset, other.outcome, other.pnl_usd
            )
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.window_ts, self.asset, self.outcome, self.pnl_usd))


# ---------------------------------------------------------------------------
# Reconciliation types (consumed by ReconcilePositionsUseCase)
# ---------------------------------------------------------------------------


_VALID_POSITION_OUTCOMES = {"WIN", "LOSS", "OPEN", "UNKNOWN"}


@dataclass(frozen=True)
class PositionOutcome:
    """Position outcome data from the Polymarket API.

    Produced by PolymarketClientPort or the reconciler's position poll.
    """

    condition_id: str
    outcome: str  # "WIN" | "LOSS"
    size: float = 0.0
    avg_price: float = 0.0
    cur_price: Optional[float] = None
    value: float = 0.0
    cost: float = 0.0
    pnl: Optional[float] = None
    # Legacy fields retained for production adapters
    token_id: Optional[str] = None
    pnl_raw: Optional[float] = None

    def __post_init__(self) -> None:
        if self.outcome not in _VALID_POSITION_OUTCOMES:
            raise ValueError(f"outcome must be one of {_VALID_POSITION_OUTCOMES}")
        if self.size < 0:
            raise ValueError("size must be >= 0")


_VALID_RESOLUTION_OUTCOMES = {"RESOLVED_WIN", "RESOLVED_LOSS"}


@dataclass(frozen=True)
class ResolutionResult:
    """Result of resolving one position against the Polymarket outcome.

    Produced by ReconcilePositionsUseCase.resolve_one().
    """

    condition_id: str
    outcome: str  # "RESOLVED_WIN" | "RESOLVED_LOSS"
    pnl_usd: float
    matched_trade_id: Optional[str] = None
    status: Optional[str] = None  # legacy alias (same as outcome)
    token_id: Optional[str] = None
    match_method: Optional[str] = None  # "exact" | "prefix" | "cost_fallback"

    def __post_init__(self) -> None:
        if self.outcome not in _VALID_RESOLUTION_OUTCOMES:
            raise ValueError(f"outcome must be one of {_VALID_RESOLUTION_OUTCOMES}")


@dataclass(frozen=True)
class ReconcileResult:
    """Aggregate result from one ReconcilePositionsUseCase.execute() call.

    Produced by ReconcilePositionsUseCase.execute().
    """

    live_resolved: int  # live positions resolved via CLOB API
    paper_resolved: int  # paper trades resolved via oracle
    paper_skipped: int  # paper trades skipped (window not resolved yet)
    errors: int  # exceptions caught during resolution
    windows_labeled: int = 0  # windows stamped with actual_direction for ML training


# ---------------------------------------------------------------------------
# Risk / wallet types (consumed by PublishHeartbeatUseCase)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskStatus:
    """Read-only snapshot of the risk manager's current state.

    Produced by RiskManagerPort.get_status().
    """

    current_bankroll: float
    peak_bankroll: float
    drawdown_pct: float
    daily_pnl: float
    consecutive_losses: int
    paper_mode: bool = False
    kill_switch_active: bool = False
    win_streak: int = 0
    loss_streak: int = 0

    def __post_init__(self) -> None:
        if not math.isfinite(self.current_bankroll):
            raise ValueError("current_bankroll must be finite")
        if math.isfinite(self.drawdown_pct) and self.drawdown_pct > 1.0:
            raise ValueError("drawdown_pct must be in [0.0, 1.0] (fraction, not percent)")

    @property
    def is_killed(self) -> bool:
        return self.kill_switch_active


@dataclass(frozen=True)
class WalletSnapshot:
    """Point-in-time wallet balance snapshot."""

    balance_usdc: float
    timestamp: float
    source: str = "polymarket_clob"

    def __post_init__(self) -> None:
        if self.balance_usdc < 0:
            raise ValueError("balance_usdc cannot be negative")
        if not self.source:
            raise ValueError("source must be non-empty")


# ---------------------------------------------------------------------------
# Strategy Port types (SP-01 through SP-05)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategyContext:
    """Immutable snapshot of all data a strategy might need.

    Constructed once per (window, eval_offset) by the
    EvaluateStrategiesUseCase.  Strategies pick the fields they need;
    unused fields are None.  This is the SINGLE input contract --
    strategies never reach outside this object for data.
    """

    # Identity
    asset: str
    window_ts: int
    timeframe: str
    eval_offset: Optional[int]

    # Price deltas (from ConsensusPricePort)
    delta_chainlink: Optional[float]
    delta_tiingo: Optional[float]
    delta_binance: Optional[float]
    delta_pct: float  # Primary delta (source-selected)
    delta_source: str  # "tiingo_rest_candle" | "chainlink" | etc.

    # Market state
    current_price: float
    open_price: float
    vpin: float
    regime: str  # "CALM" | "NORMAL" | "TRANSITION" | "CASCADE"

    # CoinGlass
    cg_snapshot: Optional[object]  # CoinGlassEnhancedFeed.snapshot

    # TWAP
    twap_delta: Optional[float]

    # Tiingo detail
    tiingo_close: Optional[float]

    # Gamma / CLOB prices
    gamma_up_price: Optional[float]
    gamma_down_price: Optional[float]
    clob_up_bid: Optional[float]
    clob_up_ask: Optional[float]
    clob_down_bid: Optional[float]
    clob_down_ask: Optional[float]

    # V4 fusion snapshot (populated by V4SnapshotPort before strategies run)
    v4_snapshot: Optional["V4Snapshot"] = None

    # Prior DUNE probability (for v2_logit feature)
    prev_dune_probability_up: Optional[float] = None


@dataclass(frozen=True)
class StrategyDecision:
    """What a strategy decided for this window evaluation.

    Every strategy returns exactly one of these per evaluate() call.
    The use case uses ``action`` to determine what happens next.

    **Construction path (2026-04-17 Three-Builders convergence):**

    Prefer the ``trade()`` / ``skip()`` / ``error()`` classmethods below
    over direct construction. The factories accept a ``DecisionMetadata``
    Value Object (``engine/domain/decision_metadata.py``) instead of a
    loose dict — which prevents the key-convention divergence between
    ``registry._evaluate_common``, ``v4_fusion_strategy.py``, and
    ``configs/v4_fusion.py`` that originally motivated this refactor.

    Direct-construction paths remain temporarily supported (``metadata``
    can still be a plain dict) so the three existing builders migrate
    incrementally without a flag-day cutover. Subsequent PRs tighten
    the type once all call sites are converted.
    """

    action: str  # "TRADE" | "SKIP" | "ERROR"
    direction: Optional[str]  # "UP" | "DOWN" | None (if SKIP/ERROR)
    confidence: Optional[str]  # "DECISIVE" | "HIGH" | "MODERATE" | "LOW" | None
    confidence_score: Optional[float]  # 0.0-1.0 numeric confidence

    # Entry pricing
    entry_cap: Optional[float]  # Max acceptable CLOB price (e.g. 0.65)
    collateral_pct: Optional[float]  # Fraction of bankroll to risk (V4 sizing)

    # Audit trail
    strategy_id: str
    strategy_version: str
    entry_reason: str  # Human-readable, e.g. "v10_DUNE_TRANSITION_T120_FAK"
    skip_reason: Optional[str]  # Why SKIP/ERROR, None if TRADE

    # Strategy-specific metadata (JSON-serializable dict).
    # Prefer using DecisionMetadata.to_dict() via the classmethod factories
    # below rather than assigning a dict literal here.
    metadata: dict = field(default_factory=dict)

    # ── Factory methods (Three-Builders convergence) ─────────────────────────
    #
    # These are the one blessed construction path for TRADE / SKIP / ERROR
    # decisions. All three historic builders should migrate to call these.
    # Accepting either a DecisionMetadata VO (preferred) or a legacy dict
    # (deprecated, for migration safety) means builders can be cut over one
    # at a time.

    @classmethod
    def trade(
        cls,
        *,
        direction: str,
        strategy_id: str,
        strategy_version: str,
        entry_reason: str,
        metadata: "Any" = None,
        confidence: Optional[str] = None,
        confidence_score: Optional[float] = None,
        entry_cap: Optional[float] = None,
        collateral_pct: Optional[float] = None,
    ) -> "StrategyDecision":
        """Construct a TRADE decision.

        :param direction: "UP" or "DOWN". Required — SKIP/ERROR have no
            direction so they use the other factories.
        :param metadata: Either a ``DecisionMetadata`` VO (preferred) or
            a plain ``dict`` (legacy). ``None`` is treated as empty.
        :raises ValueError: if ``direction`` is not "UP" or "DOWN".
        """
        if direction not in ("UP", "DOWN"):
            raise ValueError(
                f"StrategyDecision.trade direction must be 'UP' or 'DOWN', "
                f"got {direction!r}"
            )
        return cls(
            action="TRADE",
            direction=direction,
            confidence=confidence,
            confidence_score=confidence_score,
            entry_cap=entry_cap,
            collateral_pct=collateral_pct,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            entry_reason=entry_reason,
            skip_reason=None,
            metadata=_coerce_metadata(metadata),
        )

    @classmethod
    def skip(
        cls,
        *,
        reason: str,
        strategy_id: str,
        strategy_version: str,
        metadata: "Any" = None,
        direction: Optional[str] = None,
        confidence: Optional[str] = None,
        confidence_score: Optional[float] = None,
        entry_cap: Optional[float] = None,
        collateral_pct: Optional[float] = None,
        entry_reason: str = "",
    ) -> "StrategyDecision":
        """Construct a SKIP decision.

        :param reason: Human-readable skip reason (e.g. "regime:calm_skip",
            "health:degraded"). Stored in ``skip_reason`` — used by the
            Signal Explorer and gate-audit analytics.
        :param metadata: DecisionMetadata VO or legacy dict; see
            ``trade()``.
        :param direction: Optional direction (``UP`` / ``DOWN``). Default
            ``None``. Set when the strategy had computed a direction
            before a subsequent gate vetoed the trade — preserves the
            "strategy would have traded X but was filtered" record for
            post-hoc analytics (e.g. gate-audit-matrix rendering in the
            FE Signal Explorer). A bare SKIP (no prior decision) leaves
            direction ``None``.
        :param confidence / confidence_score / entry_cap / collateral_pct:
            Optional fields carrying the decision state at the point of
            skip. Used by the registry's post-hook-gate branch, which
            already has a TRADE decision from the hook but ran YAML
            gates as post-filters and caught a veto.
        :param entry_reason: Default ``""``. Only populated for SKIPs
            that followed a hook-computed entry reason; generic gate
            skips leave it empty.
        """
        return cls(
            action="SKIP",
            direction=direction,
            confidence=confidence,
            confidence_score=confidence_score,
            entry_cap=entry_cap,
            collateral_pct=collateral_pct,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            entry_reason=entry_reason,
            skip_reason=reason,
            metadata=_coerce_metadata(metadata),
        )

    @classmethod
    def error(
        cls,
        *,
        reason: str,
        strategy_id: str,
        strategy_version: str,
        metadata: "Any" = None,
    ) -> "StrategyDecision":
        """Construct an ERROR decision (strategy raised during evaluation).

        Distinct from SKIP: SKIP means "strategy ran, decided not to
        trade", ERROR means "strategy could not reach a decision"
        (exception path, surface missing, etc).
        """
        return cls(
            action="ERROR",
            direction=None,
            confidence=None,
            confidence_score=None,
            entry_cap=None,
            collateral_pct=None,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            entry_reason="",
            skip_reason=reason,
            metadata=_coerce_metadata(metadata),
        )


def _coerce_metadata(metadata: "Any") -> dict:
    """Accept a DecisionMetadata VO, a dict, or None — return the dict form.

    Temporary shim for the migration window: existing call sites that
    pass dicts keep working; new call sites pass the VO and get its
    canonical ``to_dict()`` output. Once the three historic builders
    are all migrated, the ``dict`` branch can be deleted and the
    factories tightened to accept only ``DecisionMetadata``.
    """
    if metadata is None:
        return {}
    # Avoid importing DecisionMetadata at module top (keeps
    # domain/value_objects.py free of intra-domain import ordering
    # concerns; decision_metadata.py itself imports nothing from here).
    from domain.decision_metadata import DecisionMetadata

    if isinstance(metadata, DecisionMetadata):
        return metadata.to_dict()
    if isinstance(metadata, dict):
        return metadata
    raise TypeError(
        f"StrategyDecision metadata must be DecisionMetadata, dict, or None; "
        f"got {type(metadata).__name__}"
    )


@dataclass(frozen=True)
class V4Snapshot:
    """Parsed response from /v4/snapshot endpoint.

    Immutable -- V4FusionStrategy reads fields but never mutates.
    """

    probability_up: float
    conviction_score: float  # 0.0-1.0
    regime: str  # "calm_trend" | "volatile_trend" | "chop" | "risk_off"
    regime_confidence: float
    regime_persistence: float
    regime_transition: Optional[dict]  # Transition probability matrix

    # Recommended action
    recommended_side: Optional[str]  # "UP" | "DOWN" | None
    recommended_collateral_pct: Optional[float]
    recommended_sl_pct: Optional[float]
    recommended_tp_pct: Optional[float]
    recommended_reason: Optional[str]
    recommended_conviction_score: Optional[float]

    # Sub-signals
    sub_signals: dict  # 7 sub-signal values
    consensus: dict  # 6 source consensus + safe_to_trade
    macro: dict  # Qwen/LightGBM bias, direction_gate, size_modifier
    quantiles: dict  # p10-p90

    # Fields with defaults (must come after non-default fields)
    probability_raw: Optional[float] = None  # uncalibrated LightGBM score
    conviction: str = "NONE"  # "NONE" | "LOW" | "MEDIUM" | "HIGH"

    # Polymarket live recommended outcome (clean venue-specific block)
    polymarket_outcome: Optional[dict] = (
        None  # direction, trade_advised, confidence, extras
    )

    # Metadata
    timescale: str = "5m"
    timestamp: float = 0.0


@dataclass(frozen=True)
class StrategyRegistration:
    """Runtime config for a registered strategy.

    The orchestrator holds a list of these.  Exactly one has
    mode='LIVE'; the rest are 'GHOST'.  Switchable at runtime
    via ConfigPort or env var.
    """

    strategy_id: str
    mode: str  # "LIVE" | "GHOST"
    enabled: bool  # False = don't even evaluate
    priority: int  # Tie-breaking for display order


@dataclass(frozen=True)
class StrategyDecisionRecord:
    """One row in the strategy_decisions table.

    Written for EVERY evaluation (TRADE, SKIP, ERROR) of EVERY
    enabled strategy (LIVE and GHOST).  This is the Strategy Lab's
    source of truth.
    """

    # Identity
    strategy_id: str
    strategy_version: str
    asset: str
    window_ts: int
    timeframe: str
    eval_offset: Optional[int]
    mode: str  # "LIVE" | "GHOST"

    # Decision
    action: str  # "TRADE" | "SKIP" | "ERROR"
    direction: Optional[str]
    confidence: Optional[str]
    confidence_score: Optional[float]
    entry_cap: Optional[float]
    collateral_pct: Optional[float]
    entry_reason: str
    skip_reason: Optional[str]

    # Execution outcome (filled ONLY for LIVE + TRADE, after execution)
    executed: bool = False
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    fill_size: Optional[float] = None

    # Audit
    metadata_json: str = "{}"  # JSON-serialized strategy metadata
    evaluated_at: float = 0.0  # Unix epoch


@dataclass
class EvaluateStrategiesResult:
    """Output of EvaluateStrategiesUseCase.execute()."""

    live_decision: Optional[StrategyDecision]  # None if LIVE strategy skipped
    all_decisions: list  # All strategies' outputs
    context: Optional[StrategyContext]  # Shared input (for audit)
    window_key: str  # "{asset}-{window_ts}"
    already_traded: bool  # True if was_traded check hit


# ---------------------------------------------------------------------------
# Execution types (consumed by ExecuteTradeUseCase -- SP-06 / CA-01 Phase 4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionRequest:
    """Input to ExecuteTradeUseCase. Built from StrategyDecision + market state."""

    # Identity
    asset: str
    window_ts: int
    timeframe: str

    # Decision (from strategy)
    strategy_id: str
    strategy_version: str
    direction: str  # "UP" | "DOWN"
    confidence: Optional[str]  # "DECISIVE" | "HIGH" | "MODERATE" | "LOW"
    confidence_score: Optional[float]
    entry_reason: str

    # Pricing
    entry_cap: float  # Max acceptable CLOB price (e.g. 0.65)
    price_floor: float = 0.30  # Min acceptable price

    # Sizing (from strategy decision)
    collateral_pct: Optional[float] = None

    # Market context (for DB record)
    current_btc_price: float = 0.0
    open_price: float = 0.0
    delta_pct: float = 0.0
    vpin: float = 0.0

    # Audit trail
    gate_results: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    # Eval offset (seconds before window close)
    eval_offset: Optional[int] = None


@dataclass(frozen=True)
class StakeCalculation:
    """Result of stake sizing calculation."""

    base_stake: float
    price_multiplier: float
    adjusted_stake: float
    bankroll: float
    bet_fraction: float
    hard_cap: float


@dataclass(frozen=True)
class ExecutionResult:
    """Output of ExecuteTradeUseCase."""

    success: bool
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    fill_size: Optional[float] = None
    stake_usd: float = 0.0
    fee_usd: float = 0.0

    # Execution metadata
    execution_mode: str = "none"  # "fak" | "rfq" | "gtc" | "paper" | "none"
    fak_attempts: int = 0
    fak_prices: list = field(default_factory=list)

    # Failure info
    failure_reason: Optional[str] = None

    # Token used
    token_id: str = ""
    market_slug: str = ""

    # Timing
    execution_start: float = 0.0
    execution_end: float = 0.0

    # Strategy identity (for logging/alerting)
    strategy_id: str = ""
    direction: str = ""


@dataclass(frozen=True)
class PreTradeCheckResult:
    """Result of the pre-execution gate. approved=False means SKIP."""

    approved: bool
    reason: str
    live_bankroll: float = 0.0
    clob_price_age_s: float = 0.0


# ---------------------------------------------------------------------------
# Window Summary VO (PR C — clean-arch extraction of telegram grouping)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SummaryDecisionLine:
    """One per-strategy row rendered in the window summary.

    Populated by :class:`BuildWindowSummaryUseCase` (see
    ``engine/use_cases/build_window_summary.py``) from the list of
    :class:`StrategyDecision` emitted for a single eval offset. Rendered
    to Telegram text by the adapter formatter.
    """

    strategy_id: str
    mode: str  # "LIVE" | "GHOST" | "?"
    text: str  # fully-formatted line body (without indent / emoji prefix)


@dataclass(frozen=True)
class WindowSummaryContext:
    """Structured input to the Telegram window-summary formatter.

    Replaces the ad-hoc ``decision_lines`` list + ``decisions_text``
    string that used to live inline in ``StrategyRegistry`` (registry.py
    L670-775). The use case produces this VO; the adapter renders it
    to text. Registry becomes thin.

    Field contract:

    - ``window_ts``         — unix-seconds of window open
    - ``eval_offset``       — T-minus seconds at this eval (countdown)
    - ``timescale``         — "5m" | "15m"
    - ``open_price``        — window open price (None if unavailable)
    - ``current_price``     — live BTC price at eval moment (None ok)
    - ``eligible``          — LIVE strategies that returned TRADE this offset
    - ``blocked_signal``    — LIVE SKIPs from signal gates (confidence, delta, etc.)
    - ``blocked_exec_timing`` — LIVE SKIPs from execution-safety "too late"
                               hooks that fired WHILE the strategy was still
                               in its eligible window (rare — final-offset
                               "too late" is moved to ``window_expired``)
    - ``off_window``        — LIVE SKIPs from YAML timing gate or custom
                               "outside window" hook at a non-final offset
    - ``window_expired``    — LIVE strategies whose eligible window closed
                               without a trade. Line body summarizes the
                               dominant in-window skip reason from prior
                               offsets (so "why didn't it trade?" is answered
                               with actual blockers, not the trivial
                               "<70s too late" at final offset).
    - ``already_traded``    — LIVE strategies that already traded at an
                               earlier offset this window (contradiction-killer)
    - ``ghost_shadow``      — GHOST strategies (collapsed summary)
    - ``sources_agree``     — pre-formatted cross-source agreement tag
    """

    window_ts: int
    eval_offset: int
    timescale: str
    open_price: Optional[float]
    current_price: Optional[float]
    eligible: tuple["SummaryDecisionLine", ...]
    blocked_signal: tuple["SummaryDecisionLine", ...]
    blocked_exec_timing: tuple["SummaryDecisionLine", ...]
    off_window: tuple["SummaryDecisionLine", ...]
    already_traded: tuple["SummaryDecisionLine", ...]
    ghost_shadow: tuple["SummaryDecisionLine", ...]
    window_expired: tuple["SummaryDecisionLine", ...] = ()
    sources_agree: str = ""

    def has_any_actionable(self) -> bool:
        """True if any LIVE bucket has content."""
        return bool(
            self.eligible
            or self.blocked_signal
            or self.blocked_exec_timing
            or self.off_window
            or self.already_traded
            or self.window_expired
        )


# ---------------------------------------------------------------------------
# Order / execution tracking types
# ---------------------------------------------------------------------------
# Order and OrderStatus are domain ENTITIES (mutable, stateful) — see
# domain/entities.py.  The window constants live in config/constants.py.
# Both are re-exported from execution.order_manager for backward compat.
