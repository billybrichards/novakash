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
