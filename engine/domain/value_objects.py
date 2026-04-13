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

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Shared identity types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WindowKey:
    """Unique identifier for a 5-minute binary-options window."""
    asset: str
    window_ts: int
    timeframe: str = "5m"


# ---------------------------------------------------------------------------
# Market feed types (consumed by EvaluateWindowUseCase -- still stubs)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Tick:
    """Single price observation from a market feed."""
    pass


@dataclass(frozen=True)
class WindowClose:
    """Event emitted when a 5-minute window closes."""
    pass


@dataclass(frozen=True)
class DeltaSet:
    """Per-source delta triple (CL/TI/BIN) for a window."""
    pass


# ---------------------------------------------------------------------------
# Signal / evaluation types (consumed by EvaluateWindowUseCase -- still stubs)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SignalEvaluation:
    """One row of the signal_evaluations audit table."""
    pass


@dataclass(frozen=True)
class ClobSnapshot:
    """Point-in-time snapshot of a CLOB order book."""
    pass


@dataclass(frozen=True)
class GateAuditRow:
    """Audit row recording which gates ran and their results."""
    pass


@dataclass(frozen=True)
class WindowSnapshot:
    """Full snapshot of a window for backfill and UI hydration."""
    pass


# ---------------------------------------------------------------------------
# Polymarket trading types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FillResult:
    """Result of a CLOB order placement (filled size, price, fees).

    Fields populated by PolymarketClientPort.place_order().
    """
    order_id: str
    filled_size: float
    filled_price: float
    fees: float = 0.0
    status: str = "FILLED"


@dataclass(frozen=True)
class WindowMarket:
    """Gamma market lookup result for an (asset, window_ts) pair.

    Contains the up/down CLOB token IDs needed for order placement.
    """
    condition_id: str
    up_token_id: str
    down_token_id: str
    market_slug: str
    active: bool = True


@dataclass(frozen=True)
class OrderBook:
    """Live CLOB order book for a single token."""
    pass


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
    direction: str          # "UP" or "DOWN"
    entry_price: float
    stake_usd: float
    window_ts: int
    asset: str = "BTC"
    timeframe: str = "5m"


@dataclass(frozen=True)
class ManualTradeOutcome:
    """Result of processing one pending manual trade.

    Produced by ExecuteManualTradeUseCase.drain_once().
    """
    trade_id: str
    status: str             # "open", "failed_no_token", "failed: <reason>"
    clob_order_id: Optional[str] = None
    paper: bool = False
    token_source: Optional[str] = None  # "recent_windows" | "market_data_db"


# ---------------------------------------------------------------------------
# Trade decision / skip types (consumed by EvaluateWindowUseCase -- stubs)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TradeDecision:
    """Structured decision output from the gate pipeline."""
    pass


@dataclass(frozen=True)
class SkipSummary:
    """Consolidated skip summary for a window where all offsets were skipped."""
    pass


# ---------------------------------------------------------------------------
# Heartbeat / sitrep types (consumed by PublishHeartbeatUseCase)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SitrepPayload:
    """5-minute SITREP payload for the heartbeat Telegram message.

    Built by PublishHeartbeatUseCase.tick() every 5 minutes.
    """
    engine_status: str              # "ACTIVE" | "KILLED"
    mode_label: str                 # "PAPER" | "LIVE"
    wallet_balance: float
    daily_pnl: float
    starting_bankroll: float
    wins_today: int
    losses_today: int
    win_rate: float
    vpin: float
    vpin_regime: str
    btc_price: float
    open_positions: int
    drawdown_pct: float
    kill_switch_active: bool
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


@dataclass(frozen=True)
class HeartbeatRow:
    """One row written to the system_state table by the heartbeat loop.

    Written by PublishHeartbeatUseCase.tick() every 10 seconds.
    """
    engine_status: str
    current_balance: float
    peak_balance: float
    drawdown_pct: float
    last_vpin: Optional[float]
    last_cascade_state: Optional[str]
    active_positions: int
    config_snapshot: dict = field(default_factory=dict)
    timestamp: float = 0.0


# ---------------------------------------------------------------------------
# Window lifecycle types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WindowOutcome:
    """Outcome of a resolved window (win/loss/push, PnL)."""
    outcome: str            # "WIN" | "LOSS" | "PUSH"
    pnl_usd: float
    resolved_at: float      # Unix epoch


# ---------------------------------------------------------------------------
# Reconciliation types (consumed by ReconcilePositionsUseCase)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PositionOutcome:
    """Position outcome data from the Polymarket API.

    Produced by PolymarketClientPort or the reconciler's position poll.
    """
    condition_id: str
    token_id: str
    outcome: str            # "WIN" | "LOSS"
    size: float
    avg_price: float
    cost: float
    value: float
    pnl_raw: float


@dataclass(frozen=True)
class ResolutionResult:
    """Result of resolving one position against the Polymarket outcome.

    Produced by ReconcilePositionsUseCase.resolve_one().
    """
    condition_id: str
    matched_trade_id: Optional[str]
    outcome: str            # "WIN" | "LOSS"
    pnl_usd: float
    status: str             # "RESOLVED_WIN" | "RESOLVED_LOSS"
    token_id: Optional[str] = None
    match_method: Optional[str] = None  # "exact" | "prefix" | "cost_fallback"


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
    paper_mode: bool
    kill_switch_active: bool
    win_streak: int = 0
    loss_streak: int = 0


@dataclass(frozen=True)
class WalletSnapshot:
    """Point-in-time wallet balance snapshot."""
    balance_usdc: float
    timestamp: float


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
    delta_pct: float                    # Primary delta (source-selected)
    delta_source: str                   # "tiingo_rest_candle" | "chainlink" | etc.

    # Market state
    current_price: float
    open_price: float
    vpin: float
    regime: str                         # "CALM" | "NORMAL" | "TRANSITION" | "CASCADE"

    # CoinGlass
    cg_snapshot: Optional[object]       # CoinGlassEnhancedFeed.snapshot

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
    """
    action: str                         # "TRADE" | "SKIP" | "ERROR"
    direction: Optional[str]            # "UP" | "DOWN" | None (if SKIP/ERROR)
    confidence: Optional[str]           # "DECISIVE" | "HIGH" | "MODERATE" | "LOW" | None
    confidence_score: Optional[float]   # 0.0-1.0 numeric confidence

    # Entry pricing
    entry_cap: Optional[float]          # Max acceptable CLOB price (e.g. 0.65)
    collateral_pct: Optional[float]     # Fraction of bankroll to risk (V4 sizing)

    # Audit trail
    strategy_id: str
    strategy_version: str
    entry_reason: str                   # Human-readable, e.g. "v10_DUNE_TRANSITION_T120_FAK"
    skip_reason: Optional[str]          # Why SKIP/ERROR, None if TRADE

    # Strategy-specific metadata (JSON-serializable dict)
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class V4Snapshot:
    """Parsed response from /v4/snapshot endpoint.

    Immutable -- V4FusionStrategy reads fields but never mutates.
    """
    probability_up: float
    conviction_score: float             # 0.0-1.0
    regime: str                         # "calm_trend" | "volatile_trend" | "chop" | "risk_off"
    regime_confidence: float
    regime_persistence: float
    regime_transition: Optional[dict]   # Transition probability matrix

    # Recommended action
    recommended_side: Optional[str]     # "UP" | "DOWN" | None
    recommended_collateral_pct: Optional[float]
    recommended_sl_pct: Optional[float]
    recommended_tp_pct: Optional[float]
    recommended_reason: Optional[str]
    recommended_conviction_score: Optional[float]

    # Sub-signals
    sub_signals: dict                   # 7 sub-signal values
    consensus: dict                     # 6 source consensus + safe_to_trade
    macro: dict                         # Qwen/LightGBM bias, direction_gate, size_modifier
    quantiles: dict                     # p10-p90

    # Fields with defaults (must come after non-default fields)
    probability_raw: Optional[float] = None  # uncalibrated LightGBM score
    conviction: str = "NONE"           # "NONE" | "LOW" | "MEDIUM" | "HIGH"

    # Polymarket live recommended outcome (clean venue-specific block)
    polymarket_outcome: Optional[dict] = None  # direction, trade_advised, confidence, extras

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
    mode: str                           # "LIVE" | "GHOST"
    enabled: bool                       # False = don't even evaluate
    priority: int                       # Tie-breaking for display order


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
    mode: str                           # "LIVE" | "GHOST"

    # Decision
    action: str                         # "TRADE" | "SKIP" | "ERROR"
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
    metadata_json: str = "{}"           # JSON-serialized strategy metadata
    evaluated_at: float = 0.0           # Unix epoch


@dataclass
class EvaluateStrategiesResult:
    """Output of EvaluateStrategiesUseCase.execute()."""
    live_decision: Optional[StrategyDecision]   # None if LIVE strategy skipped
    all_decisions: list                          # All strategies' outputs
    context: Optional[StrategyContext]           # Shared input (for audit)
    window_key: str                             # "{asset}-{window_ts}"
    already_traded: bool                        # True if was_traded check hit


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
    direction: str              # "UP" | "DOWN"
    confidence: Optional[str]   # "DECISIVE" | "HIGH" | "MODERATE" | "LOW"
    confidence_score: Optional[float]
    entry_reason: str

    # Pricing
    entry_cap: float            # Max acceptable CLOB price (e.g. 0.65)
    price_floor: float = 0.30   # Min acceptable price

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
    execution_mode: str = "none"   # "fak" | "rfq" | "gtc" | "paper" | "none"
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
