"""Domain value objects for the Telegram notification narrative refactor.

Phase A deliverable (see /Users/billyrichards/.claude/plans/serialized-drifting-clover.md).

Alert payloads are frozen dataclasses consumed by AlertRendererPort implementations.
Every payload carries (timeframe, strategy_id, mode) where applicable so shadow
reports, tallies, health badges all flow through without hardcoded strategy lists.

No I/O, no framework imports. Standard library only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum as _Enum
from typing import Optional, Tuple

__all__ = [
    # Enums
    "LifecyclePhase",
    "HealthStatus",
    "OutcomeQuadrant",
    "WalletDeltaKind",
    "AlertTier",
    # Primitive blocks
    "AlertHeader",
    "AlertFooter",
    "BtcPriceBlock",
    "HealthBadge",
    "CumulativeTally",
    "ShadowRow",
    "MatchedTradeRow",
    "OrphanDrift",
    "WalletDelta",
    "OutflowTx",
    "StrategyEligibility",
    # Payloads
    "TradeAlertPayload",
    "WindowSignalPayload",
    "WindowOpenPayload",
    "ReconcilePayload",
    "ShadowReportPayload",
    "ResolvedAlertPayload",
    "WalletDeltaPayload",
    "RelayerCooldownPayload",
]


# ---------------------------------------------------------------------------
# Enums (str-backed to match OrderStatus precedent)
# ---------------------------------------------------------------------------


class LifecyclePhase(str, _Enum):
    """Where in the 5m / 15m window lifecycle an alert belongs."""

    MARKET = "MARKET"         # T-300 window open
    STATE = "STATE"           # T-300 → T-90 snapshots
    DECISION = "DECISION"     # T-90 → T-30 eval band
    EXECUTION = "EXECUTION"   # T-30 → T0 order / fill
    RESOLVE = "RESOLVE"       # T0 → T+5 outcome
    OPS = "OPS"               # reconcile, redeem, wallet, relayer


class HealthStatus(str, _Enum):
    """Composite signal-health rollup."""

    OK = "OK"                 # all green
    DEGRADED = "DEGRADED"     # one amber dim
    UNSAFE = "UNSAFE"         # two+ reds or fatal condition


class OutcomeQuadrant(str, _Enum):
    """Signal skill × P&L cross-product for resolved trades."""

    CORRECT_WIN = "CORRECT_WIN"     # predicted right + won
    CORRECT_LOSS = "CORRECT_LOSS"   # predicted right + lost (crossed pre-close)
    WRONG_WIN = "WRONG_WIN"         # predicted wrong + won (lucky cross)
    WRONG_LOSS = "WRONG_LOSS"       # predicted wrong + lost


class WalletDeltaKind(str, _Enum):
    """Classification of a wallet balance change."""

    MANUAL_WITHDRAWAL = "MANUAL_WITHDRAWAL"   # known OWNER_EOA dest
    TRADING_FLOW = "TRADING_FLOW"             # Polymarket contracts
    REDEMPTION = "REDEMPTION"                 # redeemer batch outflow
    UNEXPECTED = "UNEXPECTED"                 # unknown destination
    DRIFT = "DRIFT"                           # no matching on-chain tx


class AlertTier(str, _Enum):
    """Urgency classification for routing / mute predicates.

    Aligned with 2026-04-16-telegram-overhaul-proposal.md Section 1.
    """

    TACTICAL = "TACTICAL"       # fire immediately, always audible
    HEARTBEAT = "HEARTBEAT"     # cadence-driven, quiet
    DIAGNOSTIC = "DIAGNOSTIC"   # silent unless exception
    INFO = "INFO"               # audit trail, low urgency


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

_VALID_TIMEFRAMES = frozenset({"5m", "15m"})
_VALID_MODES = frozenset({"LIVE", "GHOST", "DISABLED"})
_VALID_DIRECTIONS = frozenset({"UP", "DOWN"})


def _check_timeframe(tf: str) -> None:
    if tf not in _VALID_TIMEFRAMES:
        raise ValueError(f"timeframe must be one of {sorted(_VALID_TIMEFRAMES)}, got {tf!r}")


def _check_mode(mode: str) -> None:
    if mode not in _VALID_MODES:
        raise ValueError(f"mode must be one of {sorted(_VALID_MODES)}, got {mode!r}")


def _check_direction_opt(d: Optional[str]) -> None:
    if d is not None and d not in _VALID_DIRECTIONS:
        raise ValueError(f"direction must be one of {sorted(_VALID_DIRECTIONS)} or None, got {d!r}")


# ---------------------------------------------------------------------------
# Primitive building blocks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlertHeader:
    """Standard header: tier emoji + title + event/emit timestamps + ids."""

    phase: LifecyclePhase
    title: str
    event_ts_unix: int             # event time (window open, signal time, etc.)
    emit_ts_unix: int              # when we're sending right now
    window_id: Optional[str] = None
    order_id: Optional[str] = None
    t_offset_secs: Optional[int] = None   # T-NNN relative to window close
    is_replay: bool = False        # event's window already closed

    def __post_init__(self) -> None:
        if not self.title:
            raise ValueError("title must be non-empty")
        if self.event_ts_unix <= 0:
            raise ValueError("event_ts_unix must be positive")
        if self.emit_ts_unix <= 0:
            raise ValueError("emit_ts_unix must be positive")


@dataclass(frozen=True)
class AlertFooter:
    """Standard footer: emit_ts + wallet + mode tag + identifiers."""

    emit_ts_unix: int
    wallet_usdc: Optional[Decimal] = None
    pending_redeem_usdc: Optional[Decimal] = None
    paper_mode: bool = False
    window_id: Optional[str] = None
    order_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.emit_ts_unix <= 0:
            raise ValueError("emit_ts_unix must be positive")


@dataclass(frozen=True)
class BtcPriceBlock:
    """Canonical BTC price block — one per alert max, unifies 5+ scattered numbers.

    Chainlink is authoritative (per memory reference_clob_audit; oracle source
    of truth for Polymarket resolution). Tiingo shown for cross-check.
    Binance aggTrade feeds VPIN but is NOT displayed as price.
    """

    now_price_usd: float
    window_open_usd: float
    chainlink_delta_pct: Optional[float] = None
    tiingo_delta_pct: Optional[float] = None
    sources_agree: Optional[bool] = None   # True/False/None (no Tiingo data)
    t_offset_secs: Optional[int] = None
    close_price_usd: Optional[float] = None   # only after window resolves

    def __post_init__(self) -> None:
        if self.now_price_usd <= 0:
            raise ValueError("now_price_usd must be positive")
        if self.window_open_usd <= 0:
            raise ValueError("window_open_usd must be positive")


@dataclass(frozen=True)
class HealthBadge:
    """Composite signal-health rollup + per-dimension detail for diagnostics."""

    status: HealthStatus
    reasons: Tuple[str, ...] = ()   # e.g. ("sources:mixed", "vpin:high")

    def __post_init__(self) -> None:
        if not isinstance(self.reasons, tuple):
            raise TypeError("reasons must be a tuple (frozen)")


@dataclass(frozen=True)
class CumulativeTally:
    """W/L + PnL rollup over a time window."""

    wins: int
    losses: int
    pnl_usdc: Decimal
    timeframe: Optional[str] = None     # None = all; or "5m"/"15m"
    strategy_id: Optional[str] = None   # None = all
    mode: Optional[str] = None          # None = all; or "LIVE"/"GHOST"

    def __post_init__(self) -> None:
        if self.wins < 0:
            raise ValueError("wins must be >= 0")
        if self.losses < 0:
            raise ValueError("losses must be >= 0")
        if self.timeframe is not None:
            _check_timeframe(self.timeframe)
        if self.mode is not None:
            _check_mode(self.mode)

    @property
    def total(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> Optional[float]:
        t = self.total
        return (self.wins / t) if t > 0 else None


@dataclass(frozen=True)
class ShadowRow:
    """One strategy's hypothetical outcome for a resolved window.

    Emitted for both LIVE (matched) and GHOST (hypothetical) strategies so
    the shadow report shows them side-by-side.
    """

    timeframe: str
    strategy_id: str
    mode: str                       # "LIVE" | "GHOST" at eval time
    action: str                     # "TRADE" | "SKIP"
    direction: Optional[str]        # "UP" | "DOWN" | None
    outcome: Optional[OutcomeQuadrant]  # None if SKIP
    hypothetical_pnl_usdc: Optional[Decimal]    # None if SKIP
    entry_price_cents: Optional[float]          # CLOB price if TRADE
    skip_reason: Optional[str] = None

    def __post_init__(self) -> None:
        _check_timeframe(self.timeframe)
        _check_mode(self.mode)
        if self.action not in {"TRADE", "SKIP"}:
            raise ValueError(f"action must be TRADE or SKIP, got {self.action!r}")
        _check_direction_opt(self.direction)


@dataclass(frozen=True)
class MatchedTradeRow:
    """One row of a reconcile pass matched-trade block."""

    timeframe: str
    strategy_id: str
    order_id: Optional[str]
    outcome: str                    # "WIN" | "LOSS"
    direction: str                  # "UP" | "DOWN"
    entry_price_cents: float
    pnl_usdc: Decimal
    cost_usdc: Decimal
    window_id: Optional[str] = None

    def __post_init__(self) -> None:
        _check_timeframe(self.timeframe)
        if self.outcome not in {"WIN", "LOSS"}:
            raise ValueError(f"outcome must be WIN or LOSS, got {self.outcome!r}")
        if self.direction not in _VALID_DIRECTIONS:
            raise ValueError(f"direction must be UP or DOWN, got {self.direction!r}")


@dataclass(frozen=True)
class OrphanDrift:
    """Delta vs prior orphan roster; only non-None when count changes."""

    prior_count: int
    current_count: int
    new_condition_ids: Tuple[str, ...] = ()
    auto_redeemed_wins: int = 0
    worthless_tokens: int = 0

    def __post_init__(self) -> None:
        if self.prior_count < 0 or self.current_count < 0:
            raise ValueError("counts must be >= 0")

    @property
    def delta(self) -> int:
        return self.current_count - self.prior_count

    @property
    def changed(self) -> bool:
        return self.prior_count != self.current_count


@dataclass(frozen=True)
class OutflowTx:
    """Polygon transaction outflow used for wallet delta classification."""

    tx_hash: str
    to_addr: str
    amount_usdc: Decimal
    block_number: int
    timestamp_unix: int

    def __post_init__(self) -> None:
        if not self.tx_hash:
            raise ValueError("tx_hash required")
        if not self.to_addr:
            raise ValueError("to_addr required")
        if self.amount_usdc <= 0:
            raise ValueError("amount_usdc must be positive")


@dataclass(frozen=True)
class WalletDelta:
    """Classified wallet balance change."""

    kind: WalletDeltaKind
    amount_usdc: Decimal            # negative = outflow
    prior_balance_usdc: Decimal
    new_balance_usdc: Decimal
    dest_addr: Optional[str] = None
    tx_hash: Optional[str] = None
    realized_trade_pnl_usdc: Optional[Decimal] = None

    def __post_init__(self) -> None:
        if self.prior_balance_usdc < 0:
            raise ValueError("prior_balance_usdc must be >= 0")
        if self.new_balance_usdc < 0:
            raise ValueError("new_balance_usdc must be >= 0")


@dataclass(frozen=True)
class StrategyEligibility:
    """What a strategy decided for this window (live snapshot)."""

    strategy_id: str
    strategy_version: str
    timeframe: str
    mode: str                       # "LIVE" | "GHOST" | "DISABLED"
    action: str                     # "TRADE" | "SKIP" | "ALREADY_TRADED"
    direction: Optional[str]
    confidence: Optional[str]       # label like "HIGH", "MODERATE"
    confidence_score: Optional[float]
    skip_reason: Optional[str] = None
    already_traded_at_offset: Optional[int] = None

    def __post_init__(self) -> None:
        _check_timeframe(self.timeframe)
        _check_mode(self.mode)
        if self.action not in {"TRADE", "SKIP", "ALREADY_TRADED"}:
            raise ValueError(
                f"action must be TRADE/SKIP/ALREADY_TRADED, got {self.action!r}"
            )
        _check_direction_opt(self.direction)


# ---------------------------------------------------------------------------
# Payloads — one per alert type. All inherit via common header+footer fields.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeAlertPayload:
    """Alert for a fresh trade decision + submission/fill."""

    header: AlertHeader
    footer: AlertFooter
    tier: AlertTier
    timeframe: str
    strategy_id: str
    strategy_version: str
    mode: str
    direction: str
    confidence_label: str           # post-override relabel, user-facing
    confidence_score: float
    gate_results: Tuple[dict, ...]  # [{name, passed, reason}, ...]
    stake_usdc: Decimal
    fill_price_cents: Optional[float]
    fill_size_shares: Optional[float]
    cost_usdc: Optional[Decimal]
    order_submitted: bool           # True = resting/filled, False = failed
    order_status: str               # "RESTING" | "FILLED" | "FAILED"
    btc: BtcPriceBlock
    health: HealthBadge
    today_tally: Optional[CumulativeTally] = None
    last_hour_tally: Optional[CumulativeTally] = None

    def __post_init__(self) -> None:
        _check_timeframe(self.timeframe)
        _check_mode(self.mode)
        if self.direction not in _VALID_DIRECTIONS:
            raise ValueError(f"direction must be UP or DOWN, got {self.direction!r}")
        if self.order_status not in {"RESTING", "FILLED", "FAILED"}:
            raise ValueError(
                f"order_status must be RESTING/FILLED/FAILED, got {self.order_status!r}"
            )


@dataclass(frozen=True)
class WindowSignalPayload:
    """T-XXX signal snapshot for a (window, timeframe)."""

    header: AlertHeader
    footer: AlertFooter
    tier: AlertTier
    timeframe: str
    btc: BtcPriceBlock
    vpin: Optional[float]
    p_up: Optional[float]
    p_up_distance: Optional[float]
    sources_agree: Optional[bool]
    health: HealthBadge
    strategies: Tuple[StrategyEligibility, ...]

    def __post_init__(self) -> None:
        _check_timeframe(self.timeframe)


@dataclass(frozen=True)
class WindowOpenPayload:
    """Window-open v11.2 gamma box + T-300 anchor."""

    header: AlertHeader
    footer: AlertFooter
    tier: AlertTier
    timeframe: str
    btc: BtcPriceBlock
    gamma_up_cents: Optional[float]
    gamma_down_cents: Optional[float]
    gamma_tilt: Optional[str]        # "UP" | "DOWN" | "BALANCED"

    def __post_init__(self) -> None:
        _check_timeframe(self.timeframe)


@dataclass(frozen=True)
class ReconcilePayload:
    """Reconcile pass block — grouped by timeframe + strategy."""

    header: AlertHeader
    footer: AlertFooter
    tier: AlertTier
    matched: Tuple[MatchedTradeRow, ...]
    paper_matched: Tuple[MatchedTradeRow, ...] = ()
    orphan_drift: Optional[OrphanDrift] = None
    cumulative: Optional[CumulativeTally] = None


@dataclass(frozen=True)
class ShadowReportPayload:
    """Post-resolution shadow report for a window."""

    header: AlertHeader
    footer: AlertFooter
    tier: AlertTier
    timeframe: str
    window_id: str
    actual_direction: str            # "UP" | "DOWN"
    actual_open_usd: float
    actual_close_usd: float
    rows: Tuple[ShadowRow, ...]
    live_pnl_today_usdc: Optional[Decimal] = None
    ghost_pnl_today_usdc: Optional[Decimal] = None

    def __post_init__(self) -> None:
        _check_timeframe(self.timeframe)
        if self.actual_direction not in _VALID_DIRECTIONS:
            raise ValueError(
                f"actual_direction must be UP or DOWN, got {self.actual_direction!r}"
            )


@dataclass(frozen=True)
class ResolvedAlertPayload:
    """Single live trade resolution with four-quadrant outcome."""

    header: AlertHeader
    footer: AlertFooter
    tier: AlertTier
    timeframe: str
    strategy_id: str
    mode: str
    predicted_direction: str
    actual_direction: str
    outcome_quadrant: OutcomeQuadrant
    pnl_usdc: Decimal
    entry_price_cents: float
    stake_usdc: Decimal
    btc: BtcPriceBlock
    today_tally: Optional[CumulativeTally] = None
    session_tally: Optional[CumulativeTally] = None

    def __post_init__(self) -> None:
        _check_timeframe(self.timeframe)
        _check_mode(self.mode)
        if self.predicted_direction not in _VALID_DIRECTIONS:
            raise ValueError("predicted_direction must be UP or DOWN")
        if self.actual_direction not in _VALID_DIRECTIONS:
            raise ValueError("actual_direction must be UP or DOWN")


@dataclass(frozen=True)
class WalletDeltaPayload:
    """Classified wallet balance change — manual/trading/redemption/unexpected."""

    header: AlertHeader
    footer: AlertFooter
    tier: AlertTier
    delta: WalletDelta
    owner_eoa_matched: Optional[str] = None    # which allowlist entry matched
    today_realized_pnl_usdc: Optional[Decimal] = None


@dataclass(frozen=True)
class RelayerCooldownPayload:
    """Relayer throttled or resumed."""

    header: AlertHeader
    footer: AlertFooter
    tier: AlertTier
    resumed: bool                    # False = cooldown, True = resumed
    quota_left: int
    quota_total: int
    cooldown_reset_unix: Optional[int] = None
    reason: str = ""
