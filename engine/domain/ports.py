"""Domain ports -- interfaces that the domain declares and outer layers implement.

Phase 0 deliverable (CA-01).  These are the dependency-inversion boundaries
for ``engine/``.  The domain layer never imports from adapters or
infrastructure; instead it depends on these abstract ports.  Adapters
implement them via the composition root wired in ``engine/main.py``.

Eight ports, one per subsystem boundary identified in the migration plan
(docs/CLEAN_ARCHITECT_MIGRATION_PLAN.md section 4):

  4.1  MarketFeedPort
  4.2  ConsensusPricePort
  4.3  SignalRepository
  4.4  PolymarketClientPort
  4.5  AlerterPort
  4.6  Clock
  4.7  WindowStateRepository
  4.8  ConfigPort
  4.9  TradeRepository
  4.10 RiskManagerPort
  4.11 SystemStateRepository
  4.12 ManualTradeRepository

Reference implementation: ``margin_engine/domain/ports.py``.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from typing import Optional

from domain.value_objects import (
    ClobSnapshot,
    DeltaSet,
    ExecutionResult,
    FillResult,
    GateAuditRow,
    GateCheckTrace,
    HeartbeatRow,
    OrderBook,
    PendingTrade,
    RiskStatus,
    SignalEvaluation,
    SitrepPayload,
    SkipSummary,
    StakeCalculation,
    StrategyContext,
    StrategyDecision,
    StrategyDecisionRecord,
    StrategyEvaluationTrace,
    StrategyWindowAnalysis,
    Tick,
    TradeDecision,
    V4Snapshot,
    WindowClose,
    WindowEvaluationTrace,
    WindowKey,
    WindowMarket,
    WindowOutcomeTrace,
    WindowOutcome,
    WindowSnapshot,
    WindowTraceView,
)


# ═══════════════════════════════════════════════════════════════════════════
# 4.1  MarketFeedPort
# ═══════════════════════════════════════════════════════════════════════════


class MarketFeedPort(abc.ABC):
    """Reads live and recent-historical prices for a single asset.

    Implementations: BinanceWebSocketAdapter (live mid), TiingoRestAdapter
    (5-min candles), ChainlinkDbAdapter (latest on-chain price from PG).

    The port is intentionally narrow -- the full historical query surface
    belongs on a separate HistoricalFeedPort if we need it later.  For now
    this is what ``_evaluate_window`` needs at window close.
    """

    @abc.abstractmethod
    async def get_latest_tick(self, asset: str) -> Optional[Tick]:
        """Return the most recent price observation this feed has seen.

        MUST NOT block on the network -- implementations should cache the
        latest value from their ingest loop.  Returns ``None`` if the feed
        has never produced a tick (cold start) or the latest tick is
        older than the feed's own staleness threshold.
        """
        ...

    @abc.abstractmethod
    async def get_window_delta(
        self,
        asset: str,
        window_ts: int,
        open_price: float,
    ) -> Optional[float]:
        """Percentage delta open->eval for the 5m window starting at
        *window_ts*, using this feed's price series.

        Returns ``None`` when the feed cannot answer -- a miss is NOT an
        error, it's a normal fallback signal.  Implementations MUST
        swallow network errors, timeouts, non-200 statuses, parse
        failures, and missing-field errors into a single ``return None``
        path.  They log at DEBUG (not WARNING) so the skip summary can
        distinguish expected-miss from unexpected-miss.
        """
        ...

    @abc.abstractmethod
    def subscribe_window_close(
        self,
        asset: str,
        timeframe: str,
    ) -> AsyncIterator[WindowClose]:
        """Async iterator that yields once per window close.

        The orchestrator consumes this to drive the EvaluateWindowUseCase
        loop -- each yield produces a :class:`WindowClose` value object
        with the ``window_ts``, ``open_price``, ``close_ts``, and a
        snapshot of the feed's latest tick at the moment of close.
        """
        ...


# ═══════════════════════════════════════════════════════════════════════════
# 4.2  ConsensusPricePort
# ═══════════════════════════════════════════════════════════════════════════


class ConsensusPricePort(abc.ABC):
    """Computes the CL/TI/BIN delta triple for a window.

    One implementation composes three :class:`MarketFeedPort` instances
    (chainlink_db, tiingo_rest, binance_ws) and returns a
    :class:`DeltaSet`.
    """

    @abc.abstractmethod
    async def get_deltas(
        self,
        asset: str,
        window_ts: int,
        open_price: float,
    ) -> DeltaSet:
        """Fetch deltas from all sources in parallel.

        Returns a :class:`DeltaSet` with per-source ``Optional[float]``
        entries -- missing sources are ``None``, not errors.  The caller
        decides how to handle partial data (currently: require at least
        2/3 sources with matching sign for the SourceAgreementGate to
        pass).
        """
        ...


# ═══════════════════════════════════════════════════════════════════════════
# 4.3  SignalRepository
# ═══════════════════════════════════════════════════════════════════════════


class SignalRepository(abc.ABC):
    """Append-only sink for per-evaluation audit + execution trail.

    Replaces the scattered ``DBClient.write_window_snapshot``,
    ``write_evaluation``, ``write_signal_evaluation``,
    ``write_clob_book_snapshot``,
    ``write_fok_ladder_attempt`` methods -- each of those becomes one
    ``save_*`` method on this repository.

    Note: ``write_gate_audit`` was retired.  Gate-check persistence now uses
    ``WindowTraceRepository.save_gate_check_traces`` which writes to
    ``gate_check_traces``.
    """

    @abc.abstractmethod
    async def write_signal_evaluation(self, row: SignalEvaluation) -> None:
        """Persist one :class:`SignalEvaluation` VO to ``signal_evaluations`` table.

        Idempotent by ``(asset, window_ts, eval_offset)`` -- second write
        for the same key is a no-op.
        """
        ...

    @abc.abstractmethod
    async def write_clob_snapshot(self, row: ClobSnapshot) -> None:
        """Persist one :class:`ClobSnapshot` VO to ``clob_book_snapshots`` table."""
        ...

    async def write_gate_audit(self, audit: "GateAuditRow") -> None:
        """Retired — gate_audit superseded by gate_check_traces (feat/trace PR).

        Kept as a concrete no-op so callers that still reference it at runtime
        do not raise AttributeError.  All gate-check writes should use
        ``WindowTraceRepository.save_gate_check_traces`` instead.
        """
        return  # no-op

    @abc.abstractmethod
    async def write_window_snapshot(self, snapshot: WindowSnapshot) -> None:
        """Persist a :class:`WindowSnapshot` VO to ``windows`` table.

        Used for backfill and UI hydration, not for trading decisions.
        """
        ...


# ═══════════════════════════════════════════════════════════════════════════
# 4.4  PolymarketClientPort
# ═══════════════════════════════════════════════════════════════════════════


class PolymarketClientPort(abc.ABC):
    """Trading side of Polymarket (CLOB + Gamma reads + manual-trade poll).

    Wraps today's ``execution.polymarket_client.PolymarketClient``.  The
    concrete adapter delegates to the existing class so zero behaviour
    changes during Phase 2.
    """

    @abc.abstractmethod
    async def place_order(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
    ) -> FillResult:
        """Place a CLOB order.

        ``side`` is ``'YES'`` | ``'NO'``, ``price`` is in ``[0.0, 1.0]``
        Polymarket units.  Returns a :class:`FillResult` with actual
        filled size, filled price, fees, order_id.  Raises
        ``PolymarketError`` on definitive failure (network, rejection,
        insufficient funds).
        """
        ...

    @abc.abstractmethod
    async def get_window_market(
        self,
        asset: str,
        window_ts: int,
    ) -> Optional[WindowMarket]:
        """Look up the Gamma market for ``(asset, window_ts)``.

        Returns ``None`` if the market doesn't exist yet or has been
        delisted.
        """
        ...

    @abc.abstractmethod
    async def get_book(self, token_id: str) -> Optional[OrderBook]:
        """Read the live CLOB book for a token.  Returns ``None`` on miss."""
        ...

    @abc.abstractmethod
    async def poll_pending_trades(self) -> list[PendingTrade]:
        """Poll the manual-trades table for rows with ``status='pending'``.

        Used by ``ExecuteManualTradeUseCase`` as its input source.
        """
        ...


# ═══════════════════════════════════════════════════════════════════════════
# 4.5  AlerterPort  (moved to use_cases/ports/ — re-exported for compat)
# ═══════════════════════════════════════════════════════════════════════════

# Moved to use_cases/ports/ — re-exported here for backward compat
from use_cases.ports.alerter import AlerterPort  # noqa: F401

# ═══════════════════════════════════════════════════════════════════════════
# 4.6  Clock  (moved to use_cases/ports/ — re-exported for compat)
# ═══════════════════════════════════════════════════════════════════════════

# Moved to use_cases/ports/ — re-exported here for backward compat
from use_cases.ports.clock import Clock  # noqa: F401


# ═══════════════════════════════════════════════════════════════════════════
# 4.7  WindowStateRepository
# ═══════════════════════════════════════════════════════════════════════════


class WindowStateRepository(abc.ABC):
    """Single owner of 'has this window been traded / resolved?'.

    Replaces:
      - ``FiveMinVPINStrategy._traded_windows`` (in-memory set)
      - ``CLOBReconciler._known_resolved`` (in-memory set)
      - ``Orchestrator._resolved_by_order_manager`` (in-memory set)
    """

    @abc.abstractmethod
    async def was_traded(self, key: WindowKey) -> bool:
        """Return ``True`` if the given window has already been traded."""
        ...

    @abc.abstractmethod
    async def mark_traded(
        self,
        key: WindowKey,
        order_id: str,
    ) -> None:
        """Record that a trade was placed for the given window."""
        ...

    @abc.abstractmethod
    async def try_claim_trade(self, key: WindowKey) -> bool:
        """Atomically claim a window for execution.

        Returns ``True`` only for the first caller that acquires the claim.
        """
        ...

    @abc.abstractmethod
    async def clear_trade_claim(self, key: WindowKey) -> None:
        """Release a pending claim when execution never placed an order."""
        ...

    @abc.abstractmethod
    async def was_resolved(self, key: WindowKey) -> bool:
        """Return ``True`` if the given window has already been resolved."""
        ...

    @abc.abstractmethod
    async def mark_resolved(
        self,
        key: WindowKey,
        outcome: WindowOutcome,
    ) -> None:
        """Record the resolution outcome for the given window."""
        ...

    @abc.abstractmethod
    async def load_recent_traded(self, hours: int) -> set[WindowKey]:
        """Bulk load at engine startup to warm any in-memory cache the
        adapter chooses to maintain.
        """
        ...

    @abc.abstractmethod
    async def get_actual_direction(self, key: WindowKey) -> Optional[str]:
        """Return ``'UP'`` or ``'DOWN'`` from ``window_snapshots.actual_direction``.

        Returns ``None`` if the window hasn't resolved yet or is not in the DB.
        """
        ...

    @abc.abstractmethod
    async def label_resolved_windows(self, min_age_seconds: int = 360) -> int:
        """Bulk-stamp ``actual_direction`` on windows that have close_price but
        no label yet.  Uses ``close_price > open_price → UP, else DOWN``
        (matches Chainlink oracle resolution used by Polymarket).

        Returns count of newly labeled windows.
        """
        ...


class WindowExecutionGuard(abc.ABC):
    """Strategy-level dedup: has (strategy_id, window_ts) been executed?

    Backed by DB so state survives engine restarts.
    FAIL-CLOSED: if DB is unreachable, has_executed() returns True (block trade).
    """

    @abc.abstractmethod
    async def has_executed(self, strategy_id: str, window_ts: int) -> bool: ...

    @abc.abstractmethod
    async def mark_executed(
        self, strategy_id: str, window_ts: int, order_id: str
    ) -> None: ...

    @abc.abstractmethod
    async def load_recent(self, hours: int = 2) -> None:
        """Warm in-memory cache from DB on startup."""
        ...


class WalletBalancePort(abc.ABC):
    """Live wallet balance — never returns a .env default."""

    @abc.abstractmethod
    async def get_live_balance(self) -> float: ...


# ═══════════════════════════════════════════════════════════════════════════
# 4.7b  RedeemAttemptsRepository  (PR D)
# ═══════════════════════════════════════════════════════════════════════════


class RedeemAttemptsRepository(abc.ABC):
    """Track every Builder Relayer redeem attempt so the Redeemer can
    skip condition_ids that are repeatedly failing.

    The concrete Postgres implementation lives in
    ``engine/adapters/persistence/pg_redeem_attempts.py`` and backs the
    ``redeem_attempts`` table (migration: add_redeem_attempts_table.sql).

    Outcome is one of ``"SUCCESS" | "FAILED" | "COOLDOWN"``. The Redeemer
    should only record ``FAILED`` entries against the skip threshold —
    ``COOLDOWN`` rows exist for observability and should NOT count as
    genuine failures (they just mean "we were blocked by 429").
    """

    @abc.abstractmethod
    async def record(
        self,
        condition_id: str,
        outcome: str,
        tx_hash: str | None = None,
        error: str | None = None,
    ) -> None:
        """Insert one attempt row."""
        ...

    @abc.abstractmethod
    async def recent_failures(
        self,
        condition_id: str,
        hours: int = 24,
    ) -> int:
        """Count ``FAILED`` attempts for ``condition_id`` within the
        trailing ``hours`` window. ``COOLDOWN`` and ``SUCCESS`` rows do
        NOT count. Returns 0 if the DB pool is unavailable (never raises)."""
        ...


# ═══════════════════════════════════════════════════════════════════════════
# 4.8  ConfigPort  (deferred -- tracked as CFG-01)
# ═══════════════════════════════════════════════════════════════════════════


class ConfigPort(abc.ABC):
    """DB-backed runtime config.  Only declared here for future use;
    the Phase 0-8 migration does NOT wire this -- the engine continues
    to read ``os.environ`` directly, gated by the existing
    ``runtime_config`` hot-reload path.  When CFG-01 lands, the
    :class:`ConfigPort` replaces those reads without touching use-case
    code.
    """

    @abc.abstractmethod
    async def get_float(self, key: str, default: float) -> float:
        """Read a float config value, returning *default* if missing."""
        ...

    @abc.abstractmethod
    async def get_str(self, key: str, default: str) -> str:
        """Read a string config value, returning *default* if missing."""
        ...

    @abc.abstractmethod
    async def get_bool(self, key: str, default: bool) -> bool:
        """Read a boolean config value, returning *default* if missing."""
        ...


# =====================================================================
# 4.9  TradeRepository  (Phase 2 -- ReconcilePositionsUseCase)
# =====================================================================


class TradeRepository(abc.ABC):
    """Read/write access to the trades table for reconciliation.

    Extracted from the inline SQL in ``reconciliation/reconciler.py::_resolve_position``
    (lines 757--835).  The adapter implementation wraps asyncpg queries and
    encapsulates the PE-02 / PE-05 type-deduction workarounds that are currently
    inline in the reconciler.
    """

    @abc.abstractmethod
    async def find_by_token_id(self, token_id: str) -> Optional[dict]:
        """Exact-match lookup by CLOB token_id in trades.metadata->>'token_id'.

        Returns ``None`` if no unresolved trade matches.  The returned dict
        contains at minimum: ``id``, ``entry_reason``, ``token_id``,
        ``stake_usd``, ``entry_price``.
        """
        ...

    @abc.abstractmethod
    async def find_by_token_prefix(self, token_id: str) -> Optional[dict]:
        """Prefix-match fallback when exact match fails.

        Uses bidirectional LIKE with explicit ``::text`` cast (PE-02 fix).
        """
        ...

    @abc.abstractmethod
    async def find_by_approximate_cost(self, cost: float) -> Optional[dict]:
        """Cost-based fallback when token matching fails entirely.

        Matches the most recent unresolved live trade within $0.50 of *cost*.
        """
        ...

    @abc.abstractmethod
    async def resolve_trade(
        self,
        trade_id: str,
        outcome: str,
        pnl_usd: float,
        status: str,
    ) -> None:
        """UPDATE trades SET outcome, pnl_usd, resolved_at, status WHERE id.

        Idempotent -- no-op if the trade already has an outcome.
        """
        ...

    @abc.abstractmethod
    async def find_unresolved_paper_trades(
        self, min_age_seconds: int = 360
    ) -> list[dict]:
        """Return paper trades with outcome IS NULL older than min_age_seconds.

        Paper trades are identified by ``execution_mode = 'paper'``.
        The returned dicts contain: ``id``, ``order_id``, ``direction``,
        ``stake_usd``, ``entry_price``, ``asset``, ``window_ts``,
        ``execution_mode``, ``metadata``.

        ``window_ts`` is extracted from ``metadata->>'window_ts'`` — it is a
        string in the returned dict; callers must cast to ``int``.
        """
        ...


# =====================================================================
# 4.10  RiskManagerPort  (moved to use_cases/ports/ — re-exported for compat)
# =====================================================================

# Moved to use_cases/ports/ — re-exported here for backward compat
from use_cases.ports.risk import RiskManagerPort  # noqa: F401


# =====================================================================
# 4.11  SystemStateRepository  (Phase 2 -- PublishHeartbeatUseCase)
# =====================================================================


class SystemStateRepository(abc.ABC):
    """Writes heartbeat rows and reads mode toggles.

    Extracted from ``persistence.db_client.DBClient.update_system_state``
    and ``get_mode_toggles``.
    """

    @abc.abstractmethod
    async def write_heartbeat(self, row: HeartbeatRow) -> None:
        """Persist a HeartbeatRow to the system_state table."""
        ...

    @abc.abstractmethod
    async def update_feed_status(
        self,
        binance: bool,
        coinglass: bool,
        chainlink: bool,
        polymarket: bool,
        opinion: bool,
    ) -> None:
        """Update the feed connectivity flags in the system_state table."""
        ...

    @abc.abstractmethod
    async def get_daily_record(self) -> tuple[int, int]:
        """Return (wins_today, losses_today) from trade_bible."""
        ...


# =====================================================================
# 4.12  ManualTradeRepository  (Phase 2 -- ExecuteManualTradeUseCase)
# =====================================================================


class ManualTradeRepository(abc.ABC):
    """Persistence for the manual_trades table status transitions.

    Extracted from ``persistence.db_client.DBClient.update_manual_trade_status``
    and ``get_token_ids_from_market_data``.
    """

    @abc.abstractmethod
    async def update_status(
        self,
        trade_id: str,
        status: str,
        clob_order_id: Optional[str] = None,
    ) -> None:
        """Transition a manual trade row to a new status."""
        ...

    @abc.abstractmethod
    async def get_token_ids(
        self,
        asset: str,
        window_ts: int,
        timeframe: str,
    ) -> Optional[dict]:
        """Look up token IDs from the market_data table.

        Returns a dict with ``up_token_id`` and ``down_token_id``, or
        ``None`` if no row exists.
        """
        ...


# =====================================================================
# 4.13  StrategyPort  (SP-01 -- Pluggable multi-strategy architecture)
# =====================================================================


class StrategyPort(abc.ABC):
    """Evaluates a window and returns a structured decision.

    Each implementation encapsulates one trading strategy's decision
    logic.  The port is PURELY EVALUATIVE -- it never places orders.
    Execution is the caller's responsibility (EvaluateStrategiesUseCase).

    Implementations:
      - V10GateStrategy   (wraps GatePipeline from signals/gates.py)
      - V4FusionStrategy  (wraps /v4/snapshot from timesfm service)
    """

    @property
    @abc.abstractmethod
    def strategy_id(self) -> str:
        """Unique identifier, e.g. 'v10_gate', 'v4_fusion'."""
        ...

    @property
    @abc.abstractmethod
    def version(self) -> str:
        """Semantic version string for audit trail, e.g. '10.5.3'."""
        ...

    @abc.abstractmethod
    async def evaluate(
        self,
        ctx: StrategyContext,
    ) -> StrategyDecision:
        """Evaluate the window and return a decision.

        MUST be side-effect-free (no DB writes, no HTTP calls that
        mutate state).  Network reads (fetching V4 snapshot) are
        allowed because they are idempotent.

        MUST NOT raise -- implementation swallows all exceptions and
        returns a StrategyDecision with action='ERROR' and the
        exception message in skip_reason.

        Timeout: caller enforces a 5-second asyncio.wait_for around
        this call.  If the strategy needs longer (V4 HTTP), it should
        use its own internal timeout and return ERROR on timeout.
        """
        ...


# =====================================================================
# 4.14  V4SnapshotPort  (SP-03 -- V4 fusion snapshot fetch)
# =====================================================================


class V4SnapshotPort(abc.ABC):
    """Fetches a V4 fusion snapshot from the timesfm service.

    Separated from StrategyPort because multiple strategies or analysis
    tools might consume V4 data.  The adapter wraps HTTP to /v4/snapshot.
    """

    @abc.abstractmethod
    async def get_snapshot(
        self,
        asset: str,
        timescale: str,
    ) -> Optional[V4Snapshot]:
        """Fetch the latest V4 snapshot for (asset, timescale).

        Returns None on timeout, HTTP error, or missing data.
        MUST NOT raise.
        """
        ...


# =====================================================================
# 4.15  StrategyDecisionRepository  (SP-05 -- Strategy Lab persistence)
# =====================================================================


class StrategyDecisionRepository(abc.ABC):
    """Persists strategy decisions for the Strategy Lab.

    One row per (strategy_id, window_key, eval_offset) tuple.
    Both LIVE and GHOST decisions are written.
    """

    @abc.abstractmethod
    async def write_decision(self, decision: StrategyDecisionRecord) -> None:
        """Persist one strategy decision row.

        Idempotent by (strategy_id, asset, window_ts, eval_offset).
        """
        ...

    @abc.abstractmethod
    async def get_decisions_for_window(
        self,
        asset: str,
        window_ts: int,
    ) -> list[StrategyDecisionRecord]:
        """Read all strategy decisions for a window (for Strategy Lab)."""
        ...

    @abc.abstractmethod
    async def get_decisions_in_range(
        self,
        *,
        asset: str,
        timeframe: str,
        strategy_id: str,
        start_window_ts: int,
        end_window_ts: int,
    ) -> list[StrategyDecisionRecord]:
        """Read strategy decisions for a strategy across a window range."""
        ...


class WindowTraceRepository(abc.ABC):
    """Persists structured per-window/per-gate decision traces.

    Complements ``strategy_decisions`` by storing the shared window signal
    surface and per-gate check records needed for operator review.
    """

    @abc.abstractmethod
    async def ensure_tables(self) -> None:
        """Create any required trace tables if missing."""
        ...

    @abc.abstractmethod
    async def write_window_evaluation_trace(self, trace: WindowEvaluationTrace) -> None:
        """Persist one shared window evaluation trace row."""
        ...

    @abc.abstractmethod
    async def get_window_evaluation_trace(
        self,
        asset: str,
        window_ts: int,
        timeframe: str,
        eval_offset: Optional[int] = None,
    ) -> Optional[WindowEvaluationTrace]:
        """Read one shared window evaluation trace row."""
        ...

    @abc.abstractmethod
    async def write_gate_check_traces(self, traces: list[GateCheckTrace]) -> None:
        """Persist all gate checks for one or more strategy evaluations."""
        ...

    @abc.abstractmethod
    async def get_gate_check_traces(
        self,
        asset: str,
        window_ts: int,
        timeframe: str,
    ) -> list[GateCheckTrace]:
        """Read all gate-check traces for a window."""
        ...

    @abc.abstractmethod
    async def get_window_evaluation_traces_in_range(
        self,
        *,
        asset: str,
        timeframe: str,
        start_window_ts: int,
        end_window_ts: int,
    ) -> list[WindowEvaluationTrace]:
        """Read shared window evaluation traces across a window range."""
        ...


# =====================================================================
# 4.16  OrderExecutionPort  (moved to use_cases/ports/ — re-exported for compat)
# 4.17  TradeRecorderPort   (moved to use_cases/ports/ — re-exported for compat)
# =====================================================================

# Moved to use_cases/ports/ — re-exported here for backward compat
from use_cases.ports.execution import OrderExecutionPort, TradeRecorderPort  # noqa: F401
