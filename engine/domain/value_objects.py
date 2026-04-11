"""Forward-declaration stubs for domain value objects.

Phase 0 deliverable -- these are placeholder frozen dataclasses with
``pass`` bodies.  Each stub will be replaced with a full implementation
(fields, ``__post_init__`` validation, helper methods) in Phase 1.

Every type referenced by a port signature in ``engine/domain/ports.py``
must have a stub here so the port module can be imported and type-checked
without circular dependencies.

Audit IDs: CA-01, CA-02.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WindowKey:
    """Unique identifier for a 5-minute binary-options window."""
    pass


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


@dataclass(frozen=True)
class FillResult:
    """Result of a CLOB order placement (filled size, price, fees)."""
    pass


@dataclass(frozen=True)
class WindowMarket:
    """Gamma market lookup result for an (asset, window_ts) pair."""
    pass


@dataclass(frozen=True)
class OrderBook:
    """Live CLOB order book for a single token."""
    pass


@dataclass(frozen=True)
class PendingTrade:
    """A manual-trade row with status='pending'."""
    pass


@dataclass(frozen=True)
class TradeDecision:
    """Structured decision output from the gate pipeline."""
    pass


@dataclass(frozen=True)
class SkipSummary:
    """Consolidated skip summary for a window where all offsets were skipped."""
    pass


@dataclass(frozen=True)
class SitrepPayload:
    """5-minute SITREP payload for the heartbeat Telegram message."""
    pass


@dataclass(frozen=True)
class WindowOutcome:
    """Outcome of a resolved window (win/loss/push, PnL)."""
    pass


@dataclass(frozen=True)
class ManualTradeOutcome:
    """Result of processing one pending manual trade."""
    pass


@dataclass(frozen=True)
class RiskStatus:
    """Read-only snapshot of the risk manager's current state."""
    pass


@dataclass(frozen=True)
class WalletSnapshot:
    """Point-in-time wallet balance snapshot."""
    pass


@dataclass(frozen=True)
class HeartbeatRow:
    """One row written to the system_state table by the heartbeat loop."""
    pass


@dataclass(frozen=True)
class ResolutionResult:
    """Result of resolving one position against the Polymarket outcome."""
    pass


@dataclass(frozen=True)
class PositionOutcome:
    """Position outcome data from the Polymarket API."""
    pass
