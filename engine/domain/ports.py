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
import enum
from collections.abc import AsyncIterator
from typing import Optional, Tuple

from domain.alert_values import (
    CumulativeTally,
)


# ═══════════════════════════════════════════════════════════════════════════
# Mark-traded outcome enum
# ═══════════════════════════════════════════════════════════════════════════


class WriteOutcome(enum.Enum):
    """Outcome of a ``WindowStateRepository.mark_traded`` call.

    Used by ``ExecuteTradeUseCase`` to detect the "sub-fill" race: two
    fills land for the same (asset, window_ts, timeframe, strategy_id)
    inside the 25 s STALE_PLACEHOLDER_TTL window because the engine
    fires two CLOB orders before the first has confirmed in DB.

    Values:
        COMMITTED              – first real fill for this window;
                                 the row was inserted (or upgraded from
                                 the 'pending' placeholder) with this
                                 order_id.
        ALREADY_COMMITTED_SAME – the row already had THIS order_id
                                 (idempotent replay / restart recovery).
                                 No second trade row needed.
        SECONDARY_FILL         – the row already had a DIFFERENT real
                                 order_id from this call. The caller is
                                 the second (or later) fill; its
                                 ExecuteTradeUseCase callsite is
                                 responsible for recording a secondary
                                 ``trades`` row tagged
                                 ``is_secondary_fill=True`` and
                                 ``parent_trade_id`` pointing at the
                                 existing primary fill's order_id.
    """

    COMMITTED = "committed"
    ALREADY_COMMITTED_SAME = "already_committed_same"
    SECONDARY_FILL = "secondary_fill"
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
# 4.5  AlerterPort  (canonical location: use_cases/ports/alerter.py)
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# 4.6  Clock  (canonical location: use_cases/ports/clock.py)
# ═══════════════════════════════════════════════════════════════════════════


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
        strategy_id: Optional[str] = None,
    ) -> Optional["WriteOutcome"]:
        """Record that a trade was placed for the given window.

        Audit #321 (2026-04-26): ``strategy_id`` records WHICH strategy
        filled — used by :meth:`has_filled` to enforce
        one-fill-per-strategy-per-window. Older callers passing only
        ``order_id`` still work (the column is nullable for backfill
        compatibility).

        Returns a :class:`WriteOutcome` describing whether this call
        committed the canonical fill row, was an idempotent replay of
        the same ``order_id``, or detected a SECONDARY fill (existing
        row had a different real ``order_id``). Legacy adapters may
        return ``None`` for backwards compatibility — callers that
        want to record sub-fill rows should treat ``None`` the same
        as ``WriteOutcome.COMMITTED``.
        """
        ...

    @abc.abstractmethod
    async def has_filled(
        self,
        key: WindowKey,
        strategy_id: str,
    ) -> bool:
        """Return ``True`` if ``strategy_id`` already has a recorded fill
        for this window.

        Audit #321 (2026-04-26): the missing primary invariant. After
        per-strategy lease keys (#320) the lease alone is in-flight
        protection — once a lease releases on fill, nothing prevented
        the SAME strategy from re-acquiring and filling again on a
        later eval tick within the same window. This method backs the
        terminal "already filled this window" check that runs BEFORE
        lease acquisition.

        Sibling strategies (different ``strategy_id``) are independent —
        v9_lgb_only filling does NOT block v10_lgb_only.
        """
        ...

    async def try_claim_fill_slot(
        self,
        key: WindowKey,
        strategy_id: str,
    ) -> bool:
        """Pessimistic per-(window, strategy) fill-slot claim used to
        prevent multi-fills when the lease auto-expires before a
        slow FAK ladder confirms.

        Returns ``True`` if the slot was claimed (we own it), ``False``
        if another concurrent attempt already holds it.

        Default implementation returns True (no-op fail-open) for
        adapters that have not been upgraded — the lease still
        provides 15s in-flight protection. Production adapters MUST
        override and back this with a UNIQUE-constrained INSERT.

        Audit #322 (2026-04-26): added after a 6-fill smoking-gun on
        v10_lgb_only / window 1777238100 where the lease released
        between successive eval ticks while three FAK ladders were
        in flight.
        """
        return True

    async def release_fill_slot(
        self,
        key: WindowKey,
        strategy_id: str,
    ) -> None:
        """Release a pessimistic fill-slot claim that did NOT result in
        a fill (FAK no-fill, risk block after claim, etc.). Idempotent;
        once :meth:`mark_traded` has stamped a real order_id this is a
        no-op and safe to skip.
        """
        return None

    @abc.abstractmethod
    async def try_claim_trade(
        self,
        key: WindowKey,
        *,
        strategy_id: str,
    ) -> tuple[bool, Optional[str]]:
        """Atomically claim a window for ``strategy_id``.

        Returns ``(True, claim_id)`` on first acquisition, ``(False, None)``
        if the same strategy already holds an active lease.

        Audit #320 (2026-04-26): the lease key is per-strategy, so sibling
        strategies (v9_lgb_only, v9_ensemble, v10_lgb_only, …) do NOT
        contend for the same row. The caller MUST thread the returned
        ``claim_id`` to ``clear_trade_claim`` to release the lease cleanly.
        """
        ...

    @abc.abstractmethod
    async def clear_trade_claim(
        self,
        key: WindowKey,
        claim_id: Optional[str] = None,
    ) -> None:
        """Release a pending claim using the explicit ``claim_id`` from
        ``try_claim_trade``. ``claim_id=None`` is a no-op with a WARN log;
        the lease will TTL out naturally."""
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

    async def get_window_resolution(
        self, key: WindowKey
    ) -> Tuple[Optional[str], Optional[str]]:
        """Return ``(oracle_outcome, actual_direction)`` from ``window_snapshots``.

        Both fields can independently be ``None`` if not yet populated.
        ``oracle_outcome`` is the Polymarket on-chain truth (preferred);
        ``actual_direction`` is the engine-internal Chainlink/Binance sample
        and may disagree with ``oracle_outcome`` near window boundaries.

        Default implementation falls back to :py:meth:`get_actual_direction`
        for adapters that do not yet expose ``oracle_outcome``; subclasses
        that can read both columns should override.
        """
        actual = await self.get_actual_direction(key)
        return (None, actual)

    @abc.abstractmethod
    async def label_resolved_windows(self, min_age_seconds: int = 360) -> int:
        """Bulk-stamp ``actual_direction`` on windows that have close_price but
        no label yet.  Priority: ``oracle_outcome`` (Polymarket Gamma) >
        ``window_predictions.chainlink_close`` vs ``chainlink_open`` >
        ``delta_chainlink`` sign > NULL (never Binance).

        Returns count of newly labeled windows.
        """
        ...

    @abc.abstractmethod
    async def populate_oracle_outcomes(
        self, lookback_seconds: int = 21600, min_age_seconds: int = 360
    ) -> int:
        """Poll Polymarket Gamma for authoritative UP/DOWN on recently-closed
        windows and stamp ``oracle_outcome`` / ``poly_resolved_outcome`` /
        ``poly_winner`` on ``window_snapshots``.

        Windows must be at least ``min_age_seconds`` old (Polymarket needs
        a few minutes to resolve via UMA + Chainlink). Only windows missing
        ``oracle_outcome`` are polled. ``lookback_seconds`` defaults to 6h
        so transient Gamma failures self-heal (audit RDS #614).

        Returns count of rows whose ``oracle_outcome`` was just populated.
        """
        ...

    @abc.abstractmethod
    async def get_oracle_outcome_by_slug(self, market_slug: str) -> Optional[str]:
        """Return ``window_snapshots.oracle_outcome`` for the market slug.

        Returns ``"UP"``, ``"DOWN"``, or ``None`` when the window has not
        been resolved yet (Gamma hasn't published the outcome).

        Used by ``ReconcileRedemptionsUseCase`` to map an on-chain REDEEM
        event to the winning trade side (``"YES"`` for UP, ``"NO"`` for
        DOWN). Falls back to parsing the slug suffix for ``(asset,
        window_ts)`` since window_snapshots is keyed on those.
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
    async def find_by_approximate_cost(
        self,
        cost: float,
        condition_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Cost-based fallback when token matching fails entirely.

        Matches the most recent unresolved live trade within $0.15 of
        *cost*.  When ``condition_id`` is supplied the query adds a
        ``metadata->>'condition_id' = $2`` (or ``conditionId``) clause,
        so same-stake trades from different markets cannot cross-match.
        Hub #455 (2026-05-10): all v_consensus_4way entries carry $5
        stakes — without the condition_id filter a losing $4.94 position
        was stamping the wrong outcome on an unrelated winning trade.
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

    @abc.abstractmethod
    async def has_fill_for_strategy_window_direction(
        self,
        *,
        strategy_id: str,
        window_ts: int,
        direction: str,
        timeframe: str,
        asset: str,
        is_live: bool = True,
    ) -> bool:
        """HARD lock primitive — returns True if any non-cancelled trade row
        exists for the (strategy_id, window_ts, direction, timeframe, asset,
        is_live) tuple.

        Drives the strict single-fire-per-(strategy, window, direction) gate
        introduced after the 2026-05-20 ETH incident (window 1779312000,
        v9_2_eth_raw_lgb fired DOWN 3x in 40s, -$21.91 net).

        Implementation MUST:
        - Filter status NOT IN ('CANCELLED', 'SKIPPED', 'FAILED_EXECUTION')
        - Match via EITHER:
            Clause A: metadata->>'window_ts'/asset/timeframe canonical path
            Clause B: metadata->>'dedup_key' = '{strategy_id}:{window_ts}:{direction}'
          Clause B catches strategies that set dedup_key but historically omit
          window_ts (e.g. tickformer family). Incident: tickformer_v16_pure
          double-fire 2026-05-29 21:58 UTC (trades 9442 + 9443).
        - Match is_live so paper and live are separate domains
        - Be FAIL-CLOSED on any DB exception — better to skip ONE legitimate
          fire than repeat a multi-fire loss.
        """
        ...

    @abc.abstractmethod
    async def find_unresolved_live_trades_for_slug(
        self,
        market_slug: str,
        *,
        winning_direction: Optional[str] = None,
    ) -> list[dict]:
        """Find LIVE unresolved trades on a given market_slug.

        Used by ``ReconcileRedemptionsUseCase`` to map an on-chain
        ``REDEEM`` event back to the trade rows that should be stamped
        WIN. Filters:

          - ``is_live = TRUE`` — paper trades resolve via the oracle path.
          - ``outcome IS NULL`` — idempotency guard; rows already
            stamped WIN/LOSS are not touched.
          - ``order_id NOT LIKE 'paper-%'`` — belt-and-braces with
            ``is_live`` since some legacy paper trades have ``is_live``
            flipped via the SOT reconciler.
          - When ``winning_direction`` is provided
            (``"YES"`` for an UP-resolved market, ``"NO"`` for DOWN),
            only trades with that ``direction`` are returned. This is
            the safe path that prevents stamping LOSS-side trades as WIN
            when both YES and NO trades were placed on the same window.
        """
        ...

    @abc.abstractmethod
    async def stamp_redemption_win(
        self,
        *,
        trade_id: int,
        payout_usd: float,
        pnl_usd: float,
        redemption_tx: str,
        redeemed_at_epoch: int,
    ) -> bool:
        """Atomically stamp a trade as a redeemed WIN.

        Sets, in ONE UPDATE:

          - ``outcome = 'WIN'``
          - ``status = 'RESOLVED_WIN'``
          - ``payout_usd``, ``pnl_usd``, ``resolved_at = now()``
          - ``redeemed = TRUE``, ``redemption_tx``, ``redeemed_at``

        Returns ``True`` if a row was updated, ``False`` if the WHERE
        guard (``outcome IS NULL AND redeemed = FALSE``) rejected the
        write — that prevents double-stamping when two reconciler passes
        race on the same trade.
        """
        ...

    @abc.abstractmethod
    async def find_unresolved_live_trades_older_than(
        self, min_age_seconds: int = 1800
    ) -> list[dict]:
        """Return unresolved LIVE trades older than ``min_age_seconds``.

        Used by ``ReconcileOracleLossesUseCase`` to find worthless-token
        losses that the legacy CLOB reconciler missed (the data-api
        ``positions`` endpoint drops curPrice=0 tokens that the wallet
        never redeemed, so the legacy path cannot see them).

        Filters mirror ``find_unresolved_live_trades_for_slug`` so a trade
        cannot appear in both the WIN path (redemption feed) and the LOSS
        path (this method) in the same pass:

          - ``outcome IS NULL`` — idempotency.
          - ``is_live = TRUE`` — paper trades resolve via the oracle path
            inside ``ReconcilePositionsUseCase._resolve_paper_batch``.
          - ``order_id NOT LIKE 'paper-%'`` — belt-and-braces.
          - ``status NOT IN ('CANCELLED', 'SKIPPED', 'FAILED_EXECUTION')`` —
            never-placed trades aren't loss candidates.
          - ``created_at < now() - interval '<min_age_seconds>s'`` — Gamma
            resolution lag is ~4 min; using a 30-min cutoff by default
            guarantees the oracle has had time to settle.

        The returned dicts include at minimum: ``id``, ``strategy``,
        ``direction``, ``stake_usd``, ``entry_price``, ``fill_price``,
        ``fill_size``, ``market_slug``, ``status``, ``execution_mode``,
        ``polymarket_tx_hash``, and ``created_at``.
        """
        ...

    @abc.abstractmethod
    async def stamp_oracle_loss(
        self,
        *,
        trade_id: int,
        pnl_usd: float,
    ) -> bool:
        """Atomically stamp a trade as an oracle-determined LOSS.

        Sets, in ONE UPDATE:

          - ``outcome = 'LOSS'``
          - ``status = 'RESOLVED_LOSS'``
          - ``pnl_usd`` (always negative — the full stake is lost)
          - ``resolved_at = now()``

        WHERE guard (``outcome IS NULL AND is_live = TRUE``) prevents
        the LOSS path from racing the WIN-stamping redemption reconciler
        (which sets ``outcome='WIN'``) — only one side ever wins the
        race, and the other no-ops.

        Returns ``True`` if a row was updated.
        """
        ...


# =====================================================================
# 4.10  RiskManagerPort  (canonical location: use_cases/ports/risk.py)
# =====================================================================


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

    async def mark_executed(
        self,
        *,
        strategy_id: str,
        asset: str,
        window_ts: int,
        order_id: Optional[str],
        fill_price: Optional[float],
        fill_size: Optional[float],
    ) -> None:
        """Audit #255 F5 — post-execution update of executed / fill details.

        Default is a no-op so in-memory adapters (tests) inherit without
        having to implement. Production adapters (PgStrategyDecisionRepository)
        override this to write back the fill into the strategy_decisions row.
        """
        return None

    async def mark_execution_failed(
        self,
        *,
        strategy_id: str,
        asset: str,
        window_ts: int,
        direction: str,
        failure_reason: str,
    ) -> None:
        """Persist ExecuteTradeUseCase ``_failed()`` reasons into the row.

        Sibling of :meth:`mark_executed`. Decision rows that hit
        ``action='TRADE'`` then fail at the executor (rate limit, exposure
        cap, hard lock, polymarket cap, etc.) today stay at executed=false
        with no record of *why* — the structured log line is the only
        audit trail, which makes fill-rate forensics impossible from SQL.

        Default no-op so test/in-memory adapters inherit cleanly; the
        Postgres adapter UPDATEs the matching row(s). Fire-and-forget at
        the call site so the trade path is never blocked by the write.

        Hub note #841 / Task #55 — BTC tickformer fill-rate investigation.
        """
        return None


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
# 4.16  OrderExecutionPort  (canonical location: use_cases/ports/execution.py)
# 4.17  TradeRecorderPort   (canonical location: use_cases/ports/execution.py)
# =====================================================================


# NOTE: AlertRendererPort (4.18) and OnChainTxQueryPort (4.19) live in
# use_cases/ports/ because they bind to external adapter surfaces (channel
# formatters, on-chain RPCs) — same V7 convention as AlerterPort.


# ═══════════════════════════════════════════════════════════════════════════
# 4.20  ShadowDecisionRepository
# ═══════════════════════════════════════════════════════════════════════════


class ShadowDecisionRepository(abc.ABC):
    """Persist per-window strategy decisions (LIVE + GHOST) for shadow reports.

    Why it exists:
      - ``evaluate_strategies.py`` already evaluates LIVE + GHOST in parallel.
      - This repo captures every decision so the post-resolve shadow report
        can reconstruct what each strategy saw, without re-evaluating.
      - Audit field ``mode`` records what the strategy WAS at eval time,
        not what it is now — stable even after YAML flips.

    Backing table: ``shadow_decisions``
    Unique key: (window_id, strategy_id)

    Implementations:
      - PgShadowDecisionRepository (SQL)
      - InMemoryShadowDecisionRepository (tests)
    """

    @abc.abstractmethod
    async def save(
        self,
        window_key: WindowKey,
        decisions: list[StrategyDecision],
    ) -> None:
        """Persist all evaluated decisions for ``window_key``.

        Upserts by (window_id, strategy_id). Idempotent — safe to call
        multiple times per window.
        """
        ...

    @abc.abstractmethod
    async def find_by_window(
        self,
        window_key: WindowKey,
    ) -> list[StrategyDecision]:
        """Return every persisted decision for ``window_key``.

        Empty list if the window never evaluated or predates persistence.
        Caller treats empty as "no shadow report available".
        """
        ...

    @abc.abstractmethod
    async def find_by_strategy(
        self,
        strategy_id: str,
        since_unix: int,
        limit: int = 1000,
    ) -> list[StrategyDecision]:
        """Return recent decisions for one strategy for tally backfill."""
        ...


# ═══════════════════════════════════════════════════════════════════════════
# 4.21  TallyQueryPort
# ═══════════════════════════════════════════════════════════════════════════


class TallyQueryPort(abc.ABC):
    """Cumulative W/L + P&L rollups (today, last hour, session, by strategy).

    Adapter is responsible for its own TTL cache (target ~60s) so that
    alert-building stays under the per-alert latency budget.

    The ``CumulativeTally`` value object is defined in
    ``engine/domain/alert_values.py``.
    """

    @abc.abstractmethod
    async def today(self) -> "CumulativeTally":
        """Combined LIVE tally since UTC midnight."""
        ...

    @abc.abstractmethod
    async def last_hour(self) -> "CumulativeTally":
        """Combined LIVE tally for the rolling last 60 minutes."""
        ...

    @abc.abstractmethod
    async def session(self, since_unix: int) -> "CumulativeTally":
        """Combined LIVE tally since the given engine-start unix ts."""
        ...

    @abc.abstractmethod
    async def today_by_strategy(
        self,
    ) -> dict[tuple[str, str, str], "CumulativeTally"]:
        """Today's tally keyed on (timeframe, strategy_id, mode).

        Joins ``trades`` (LIVE) + ``shadow_decisions`` (GHOST) so LIVE vs
        GHOST can be compared per strategy per timeframe. Missing keys
        mean that strategy has no activity today.
        """
        ...

    @abc.abstractmethod
    async def today_combined(
        self,
        timeframe: Optional[str] = None,
    ) -> "CumulativeTally":
        """Combined tally for all strategies; optionally filtered by timeframe."""
        ...
