"""Use case: Execute Trade.

Replaces: ``engine/strategies/five_min_vpin.py::_execute_trade``
          (lines 3178-3714, ~536 LOC).

Responsibility
--------------
Take a StrategyDecision from the registry, validate it against risk
limits, size the position, execute the order on Polymarket CLOB (via
OrderExecutionPort), record the trade, and send a Telegram alert.

Does NOT evaluate strategies -- that is EvaluateStrategiesUseCase.
Does NOT resolve positions -- that is ReconcilePositionsUseCase.

Port dependencies (all from ``engine/domain/ports.py``):
  - PolymarketClientPort: get_window_market (token ID lookup)
  - OrderExecutionPort: execute_order (FAK/GTC/paper)
  - RiskManagerPort: get_status (bankroll, risk checks)
  - WindowStateRepository: was_traded, mark_traded (dedup)
  - AlerterPort: send_trade_alert, send_system_alert
  - TradeRecorderPort: record_trade
  - Clock: deterministic time for testing

Feature flag: ENGINE_REGISTRY_EXECUTE (default false).
"""

from __future__ import annotations

import asyncio
import os
import time as _time
import structlog
from dataclasses import replace
from typing import Any, Coroutine, Optional

from domain.ports import (
    PolymarketClientPort,
    WindowStateRepository,
)
from use_cases.ports import AlerterPort, Clock, OrderExecutionPort, RiskManagerPort, TradeRecorderPort
from domain.value_objects import (
    ExecutionResult,
    RiskStatus,
    StakeCalculation,
    StrategyDecision,
    WindowKey,
    WindowMarket,
)
from config.runtime_config import runtime

log = structlog.get_logger(__name__)

# Constants ported from five_min_vpin.py
PRICE_FLOOR = 0.30
DEFAULT_ENTRY_CAP = 0.65
MIN_BET_USD = 1.0  # floor; runtime.min_bet_usd overrides this if lower
DEFAULT_BET_FRACTION = 0.025
FEE_MULTIPLIER = 0.072  # Polymarket binary options fee

# Guardrail constants
MIN_ORDER_INTERVAL_S = 30
MAX_ORDERS_PER_HOUR = 20
CIRCUIT_BREAKER_ERRORS = 3
CIRCUIT_BREAKER_COOLDOWN_S = 180  # 3 minutes

# Failure reasons that represent real infra/submit errors (CLOB rejection,
# auth failure, signer error, network exception). These count toward the
# consecutive-error counter that trips the circuit breaker.
#
# Everything else (fak_rfq_exhausted, gtc_unfilled, …) is a market
# condition — empty book, price moved past cap, no taker for our GTC —
# and must NOT trip the breaker. Cycling 180s cooldowns on "nobody took
# my price" just silences the strategy without addressing any fault.
_REAL_ERROR_REASON_PREFIXES: tuple[str, ...] = (
    "gtc_submit_error",
    "execution_error",
)

# ── Default eval-offset recheck knobs (v6 late-fill defense, 2026-04-21) ──
#
# Defence-in-depth: even if a strategy's timing gate passes because
# ``surface.eval_offset`` is stale (set once when a CLOSING milestone
# emitted and no newer milestone has fired), we re-verify against the
# wall clock RIGHT BEFORE dispatching to the executor. Two checks:
#
#   1. Past-close guard: if wall-clock is at or past window close, abort
#      unconditionally — any fill now would be after resolution.
#   2. Min-offset guard: if the current offset (close_ts - now) is below
#      the strategy's min_offset_sec, skip with a clear drift reason.
#
# The min floor is read from ``decision.metadata["min_offset_sec"]``
# when the strategy sets it (preferred — matches strategy YAML) and
# falls back to _EVAL_OFFSET_MIN_DEFAULT. The default deliberately
# matches v6_sniper's default and is below every other 5m strategy's
# min_offset so this guard does not mis-fire on well-configured
# strategies. Operators can bump it via EVAL_OFFSET_RECHECK_MIN_SEC.
_EVAL_OFFSET_MIN_DEFAULT = int(
    os.environ.get("EVAL_OFFSET_RECHECK_MIN_SEC", "30")
)

# Known timeframe → window-duration mapping. Keeps the recheck self-contained
# instead of importing value-objects internals. Missing timeframes fall back
# to 300s (5m) which is the dominant case.
_TIMEFRAME_DURATION_SECS: dict[str, int] = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
}

# ── Per-step await timeouts (audit 2026-04-26, 7-minute hang forensics) ───
#
# Forensics on window 1777242300 (v9_lgb_only) and 1777242900 (v9_lgb_only)
# showed a 7+ minute gap between ``execute_trade.entry`` (Step 0 entry log)
# and either:
#   * ``db.try_claim_fill_slot`` (Step 5.5 DB write), or
#   * ``execute_trade.release_fill_slot`` (post-FAK release).
# No log lines fired in between — the awaits silently parked in the asyncio
# event-loop scheduler queue. py-spy ``sched_count: 226`` while the engine
# was idle on selector — i.e. 226 scheduled coroutines piled up.
#
# Root cause: ``polymarket_5min._emit_window_signal`` does
# ``asyncio.create_task(self._on_window_signal(...))`` every 2 seconds. Each
# task fans out to multiple LIVE strategies, and each strategy goes
# sequentially through ``execute_trade``. With no concurrency cap, the
# DB pool (max_size=10) saturates and downstream awaits are queued behind
# pending pool acquisitions. A single hot row UPSERT (try_claim_fill_slot
# under contention) can stall the whole chain for minutes when the
# scheduler keeps creating new tasks faster than the pool drains them.
#
# Defence: every external await between Step 0 entry and Step 6 FAK call is
# wrapped in ``asyncio.wait_for`` with an explicit timeout. If the timeout
# fires we treat it as a benign ``timeout_<phase>`` skip — release any held
# state and bail out of execute_trade so the next eval tick can retry. This
# does NOT fix the upstream task-creation pressure (that requires changes
# to ``_emit_window_signal``), but it bounds the damage so any single
# stalled await cannot hold a fill_slot lease for 7 minutes and
# starve every retry.
DB_AWAIT_TIMEOUT_S = float(os.environ.get("EXECUTE_TRADE_DB_TIMEOUT_S", "5"))
RISK_AWAIT_TIMEOUT_S = float(os.environ.get("EXECUTE_TRADE_RISK_TIMEOUT_S", "2"))


def _recheck_timing_before_execute(
    window_key: "WindowKey",
    decision: "StrategyDecision",
    *,
    now_fn: Any = None,
    min_offset_sec: Optional[int] = None,
) -> Optional[str]:
    """Defence-in-depth timing recheck, called right before FAK dispatch.

    Returns None if the trade should proceed; otherwise returns a string
    skip_reason that the caller must surface. Purely stateless — no
    side effects, no logging.

    Gates, in order:
      * ``eval_offset_past_close`` — wall clock is at/past window close.
      * ``eval_offset_drift`` — current offset < strategy min_offset.
    """
    now = (now_fn() if callable(now_fn) else _time.time())

    # Compute window close ts from the key. WindowKey stores duration_secs
    # post-__post_init__ (synced from the timeframe string).
    duration = getattr(window_key, "duration_secs", 0) or _TIMEFRAME_DURATION_SECS.get(
        getattr(window_key, "timeframe", "5m"), 300
    )
    window_ts = int(getattr(window_key, "window_ts", 0) or 0)
    if window_ts <= 0:
        # Can't reason about timing without a window anchor — fall open so
        # we don't silently block every legitimate trade. The strategy's
        # own gates remain authoritative in this degenerate case.
        return None

    close_ts = window_ts + int(duration)
    current_offset = close_ts - int(now)

    # Cross-window guard — if wall-clock is past close, the window is
    # resolved or resolving; any fill at this point is a stale-surface
    # artefact and must abort.
    if current_offset <= 0:
        return (
            f"eval_offset_past_close: offset={current_offset}s "
            f"(window {window_ts} already closed)"
        )

    # Minimum-offset floor — pulled from decision metadata when the
    # strategy propagates it (preferred). Numeric fallback matches
    # v6_sniper's default; operators can override via env.
    if min_offset_sec is None:
        min_offset_sec = _EVAL_OFFSET_MIN_DEFAULT
        meta = getattr(decision, "metadata", None) or {}
        # Strategies may publish the gate param directly or under a
        # nested gate_params dict. Accept both for forward compat.
        candidate = meta.get("min_offset_sec")
        if candidate is None:
            gp = meta.get("gate_params") or {}
            if isinstance(gp, dict):
                candidate = gp.get("min_offset_sec")
        if candidate is not None:
            try:
                min_offset_sec = int(candidate)
            except (TypeError, ValueError):
                pass

    # Backstop: even if the strategy gate accepted the eval at T-N, the
    # decision can sit on a queue or a slow tick path long enough that
    # wall-clock has crept under the strategy's min_offset_sec by the
    # time we reach execute_trade. Re-check against wall-clock so we
    # never fire FAKs in the dead-zone. Audit 2026-04-26 (window
    # 1777227600 dedup_hits 30+s past close): the past-close guard
    # alone is insufficient when registry queues TRADE decisions and
    # processes them several seconds after the strategy gate ran.
    #
    # Default floor is conservative (env 30s) — below every 5m strategy's
    # min_offset_sec (45/30/300) so this never false-blocks a passing
    # strategy that hasn't propagated min_offset_sec into metadata.
    # Strategies that DO propagate it via metadata get the exact same
    # gate the strategy itself enforces, eliminating the drift window.
    if int(min_offset_sec) > 0 and current_offset < int(min_offset_sec):
        return (
            f"eval_offset_drift: current={current_offset}s "
            f"< min={int(min_offset_sec)}s (surface stale?)"
        )

    return None


def _is_real_order_error(failure_reason: str | None) -> bool:
    if not failure_reason:
        return False
    return failure_reason.startswith(_REAL_ERROR_REASON_PREFIXES)


def _failed(
    reason: str,
    *,
    strategy_id: str = "",
    direction: str = "",
    stake_usd: float = 0.0,
    token_id: str = "",
) -> ExecutionResult:
    """Build a failed ExecutionResult."""
    return ExecutionResult(
        success=False,
        failure_reason=reason,
        strategy_id=strategy_id,
        direction=direction,
        stake_usd=stake_usd,
        token_id=token_id,
    )


class ExecuteTradeUseCase:
    """Execute a strategy decision on Polymarket CLOB.

    Single responsibility: take a StrategyDecision, validate it,
    size the position, execute the order, record the trade, alert.

    The 10-step flow:
      1. Dedup check (WindowStateRepository.was_traded)
      2. Calculate stake (bankroll x bet_fraction x price_multiplier)
      3. Risk approval (drawdown, daily loss, kill switch)
      4. Guardrails (rate limit, circuit breaker)
      5. Resolve token ID from direction + window market
      6. Execute order via OrderExecutionPort
      7. Record trade (TradeRecorderPort)
      8. Mark window as traded (WindowStateRepository)
      9. Send Telegram alert
      10. Return ExecutionResult
    """

    def __init__(
        self,
        polymarket: PolymarketClientPort,
        order_executor: OrderExecutionPort,
        risk_manager: RiskManagerPort,
        window_state: WindowStateRepository,
        alerter: AlerterPort,
        trade_recorder: TradeRecorderPort,
        clock: Clock,
        *,
        paper_mode: bool = True,
    ) -> None:
        self._polymarket = polymarket
        self._executor = order_executor
        self._risk = risk_manager
        self._window_state = window_state
        self._alerter = alerter
        self._recorder = trade_recorder
        self._clock = clock
        self._paper_mode = paper_mode

        # Guardrails (stateful -- mirrors five_min_vpin guardrails)
        self._order_timestamps: list[float] = []
        self._last_order_time: float = 0.0
        self._consecutive_errors: int = 0
        self._circuit_break_until: float = 0.0

    async def execute(
        self,
        decision: StrategyDecision,
        window_market: WindowMarket,
        current_btc_price: float,
        open_price: float,
    ) -> ExecutionResult:
        """Execute a trade from a strategy decision.

        Args:
            decision: The StrategyDecision with action="TRADE"
            window_market: Gamma market with token IDs
            current_btc_price: Live BTC price
            open_price: Window open price

        Returns:
            ExecutionResult with fill details or failure info
        """
        sid = decision.strategy_id
        direction = decision.direction or "DOWN"

        # Extract window key from market slug
        window_key = self._make_window_key(window_market)

        claim_id: Optional[str] = None  # threaded from try_claim_trade → clear_trade_claim

        # ── Diagnostic: log entry inputs so post-mortem can see exactly
        # what timing_recheck saw. Forensics 2026-04-26 (window 1777232100
        # dedup_hits 5+ minutes past close with no timing_recheck_blocked):
        # without entry visibility we couldn't tell whether the recheck
        # was even being called. Cheap log; one per execute attempt.
        try:
            _entry_now = self._clock.now()
            _entry_duration = getattr(window_key, "duration_secs", 0) or 300
            _entry_close_ts = int(window_key.window_ts) + int(_entry_duration)
            _entry_offset = _entry_close_ts - int(_entry_now)
            log.info(
                "execute_trade.entry",
                strategy=sid,
                direction=direction,
                window=str(window_key),
                window_ts=int(window_key.window_ts),
                duration_secs=int(_entry_duration),
                close_ts=int(_entry_close_ts),
                now_ts=int(_entry_now),
                current_offset=int(_entry_offset),
                slug=getattr(window_market, "market_slug", ""),
            )
        except Exception as _diag_exc:
            log.warning(
                "execute_trade.entry_diag_error",
                error=str(_diag_exc)[:200],
            )

        # ── Step 0: Timing recheck (BEFORE claim acquisition) ──────────
        # CRITICAL ORDERING (audit #317 root-cause, 2026-04-26): the
        # wall-clock past-close guard MUST run before we touch the lease.
        # Stale-surface artefacts where the strategy emits TRADE on a
        # closed window were poisoning the dedup state — we'd acquire a
        # 15s lease, then immediately abort here, then call
        # clear_trade_claim. If the release SQL ever no-ops (claim_id
        # mismatch, transient DB hiccup, shim race), the lease lingers
        # for the full TTL window, and every subsequent eval inside that
        # TTL hits dedup_hit. Multiply by N evals/sec * minutes-past-close
        # and you get the smoking-gun pattern: hundreds of dedup_hits on
        # closed windows, no fills, no recovery until the natural lease
        # expiry steals the row. By aborting before any lease activity,
        # the dedup state stays clean regardless of release-path bugs.
        timing_skip = _recheck_timing_before_execute(
            window_key,
            decision,
            now_fn=self._clock.now,
        )
        if timing_skip:
            log.warning(
                "execute_trade.timing_recheck_blocked",
                strategy=sid,
                direction=direction,
                window=str(window_key),
                failure_reason=timing_skip,
            )
            return _failed(
                timing_skip,
                strategy_id=sid,
                direction=direction,
            )

        # ── Per-step timing diagnostics (audit 2026-04-26) ────────────
        # Forensics on the 7-minute hang between Step 0 entry log and
        # Step 5.5 try_claim_fill_slot showed py-spy ``sched_count: 226``
        # — the asyncio loop had 226 scheduled coroutines piled up.
        # Without per-step timing logs we couldn't tell which await was
        # responsible. Stamp ``_step_t`` after each external await and
        # log every transition with elapsed_ms. ~10 log lines per
        # execute_trade call; volume is fine.
        _step_t = _time.monotonic()
        _entry_t = _step_t

        def _log_step(step: str, **extra: Any) -> None:
            nonlocal _step_t
            t = _time.monotonic()
            elapsed_ms = int((t - _step_t) * 1000)
            total_ms = int((t - _entry_t) * 1000)
            _step_t = t
            log.info(
                f"execute_trade.step.{step}",
                strategy=sid,
                window=str(window_key),
                elapsed_ms=elapsed_ms,
                total_ms=total_ms,
                **extra,
            )

        # ── Step 0.5: Per-strategy filled-marker (audit #321) ──────────
        # SMOKING GUN, 2026-04-26: v10_lgb_only filled 3x on window
        # 1777234800 (20:21:12, 20:21:40, 20:22:11). After per-strategy
        # lease keys (#390) was_traded was demoted to elif and never
        # fired; the lease (15s TTL, released on fill) was the ONLY
        # protection — once it released, the same strategy could re-
        # acquire and re-fill seconds later.
        #
        # The terminal invariant lives in strategy_window_fills (audit
        # #321): one row per (asset, window_ts, timeframe, strategy_id)
        # written by mark_traded after a successful fill. Checking it
        # here, BEFORE lease acquisition, ensures:
        #   * Repeat attempts short-circuit without ever touching the
        #     lease table (no wasted 15s TTL holds).
        #   * The dedup state cannot be poisoned by a failed release on
        #     a window we already filled.
        #   * Sibling strategies (different strategy_id) are NOT
        #     blocked — independent rows.
        #
        # Fail-open on DB error: if has_filled errors, the lease still
        # provides 15s in-flight protection as a backstop.
        if hasattr(self._window_state, "has_filled"):
            try:
                # Audit 2026-04-26: timeout wrapper bounds the worst-case
                # stall when DB pool (asyncpg max_size=10) is saturated.
                # See module-level DB_AWAIT_TIMEOUT_S note.
                _has_filled = await asyncio.wait_for(
                    self._window_state.has_filled(window_key, sid),
                    timeout=DB_AWAIT_TIMEOUT_S,
                )
                _log_step("has_filled", result=bool(_has_filled))
                if _has_filled:
                    log.info(
                        "execute_trade.already_filled_this_window",
                        strategy=sid,
                        window=str(window_key),
                        direction=direction,
                    )
                    return _failed(
                        "already_filled_this_window",
                        strategy_id=sid,
                        direction=direction,
                    )
            except asyncio.TimeoutError:
                # Stalled DB call. Bail out as a benign skip — the next
                # eval tick will retry. Critically: do NOT fall through
                # to lease acquisition because the upstream contention
                # would just stall there too.
                log.warning(
                    "execute_trade.has_filled_timeout",
                    strategy=sid,
                    window=str(window_key),
                    timeout_s=DB_AWAIT_TIMEOUT_S,
                )
                return _failed(
                    f"timeout_has_filled: {DB_AWAIT_TIMEOUT_S:.1f}s",
                    strategy_id=sid,
                    direction=direction,
                )
            except Exception as exc:
                log.warning(
                    "execute_trade.has_filled_check_error",
                    strategy=sid,
                    window=str(window_key),
                    error=str(exc)[:200],
                )
                # Fall through to lease — better to take a duplicate-fill
                # risk than freeze the strategy on a transient DB blip.

        # ── Step 1: Dedup / atomic claim ───────────────────────────────
        # Audit #320 (2026-04-26): try_claim_trade is now per-strategy AND
        # returns (bool, claim_id). The claim_id MUST be threaded back to
        # clear_trade_claim on failure paths, otherwise the DB row lingers
        # for the full LEASE_TTL_SECONDS and blocks the same strategy's
        # subsequent eval retries within the window.
        #
        # Audit #321 (2026-04-26): was_traded re-promoted to a parallel
        # check (no longer elif). The lease is in-flight only; the
        # terminal "already filled" check is in Step 0.5 above. was_traded
        # remains as a defensive backstop — it's per-window not per-
        # strategy in the current schema, so it errs on the side of
        # blocking a fill if the strategy_window_fills table is missing
        # for any reason on an upgrade.
        try:
            if hasattr(self._window_state, "try_claim_trade"):
                # Audit 2026-04-26: timeout wrapper. acquire_lease is a
                # single UPSERT under a hot row — under pool contention
                # this can stall behind queued connections.
                claim_result = await asyncio.wait_for(
                    self._window_state.try_claim_trade(
                        window_key, strategy_id=sid
                    ),
                    timeout=DB_AWAIT_TIMEOUT_S,
                )
                # Backward-compat: pre-#320 returned plain bool. New API
                # returns (bool, claim_id). Detect either shape.
                if isinstance(claim_result, tuple):
                    claim_acquired, claim_id = claim_result
                else:
                    claim_acquired = bool(claim_result)
                    claim_id = None
                _log_step("try_claim_trade", acquired=bool(claim_acquired))
                if not claim_acquired:
                    log.info(
                        "execute_trade.dedup_hit",
                        strategy=sid,
                        window=str(window_key),
                    )
                    return _failed(
                        "already_traded",
                        strategy_id=sid,
                        direction=direction,
                    )
            else:
                _was_traded = await asyncio.wait_for(
                    self._window_state.was_traded(window_key),
                    timeout=DB_AWAIT_TIMEOUT_S,
                )
                _log_step("was_traded", result=bool(_was_traded))
                if _was_traded:
                    log.info(
                        "execute_trade.dedup_hit",
                        strategy=sid,
                        window=str(window_key),
                    )
                    return _failed(
                        "already_traded",
                        strategy_id=sid,
                        direction=direction,
                    )
        except asyncio.TimeoutError:
            log.warning(
                "execute_trade.try_claim_trade_timeout",
                strategy=sid,
                window=str(window_key),
                timeout_s=DB_AWAIT_TIMEOUT_S,
            )
            return _failed(
                f"timeout_try_claim_trade: {DB_AWAIT_TIMEOUT_S:.1f}s",
                strategy_id=sid,
                direction=direction,
            )
        except Exception as exc:
            log.warning(
                "execute_trade.dedup_check_error",
                error=str(exc)[:200],
            )
            # Fail safe: if we can't check dedup, proceed anyway
            # The CLOB will reject if already filled

        # ── Step 2: Stake calculation ──────────────────────────────────
        stake = self._calculate_stake(decision)

        # Track release state for fill-slot and lease so the audit #398
        # finally backstop can skip redundant calls on the common
        # explicit-release paths. Declared BEFORE _release_claim because
        # Python's free-variable resolution is by static enclosing scope:
        # the closure must find these names assigned in execute() before
        # any call to a nested helper that references them.
        slot_claimed = False  # set to True iff Step 5.5 wins the slot
        slot_released = False
        claim_released = False

        # Helper: release the lease on any early-return path between
        # acquire (Step 1) and order placement (Step 6). Audit 2026-04-26
        # forensics on window 1777227900: lease was acquired by
        # v9_lgb_only at attempt_n=2, then NOTHING — no FAK ladder, no
        # error, no order_not_filled. The execute path was bailing
        # silently between Step 2 and Step 5 (risk_blocked /
        # guardrail_blocked / no_token_id) and leaking the lease. The
        # 15s TTL had to expire before the next eval could re-steal,
        # giving zero fills for the entire window. Every early-return
        # below MUST go through _release_claim so the next attempt for
        # the same (window, strategy) starts with a clean slate.
        async def _release_claim(phase: str) -> None:
            nonlocal claim_released
            if claim_released:
                return
            if claim_id and hasattr(self._window_state, "clear_trade_claim"):
                try:
                    # Audit 2026-04-26: timeout wrapper. clear_trade_claim
                    # is a single DELETE — under DB pool saturation it
                    # could be the await that holds the post-FAK release
                    # for minutes. Bound at DB_AWAIT_TIMEOUT_S; the lease
                    # TTL self-recovers within 15s anyway.
                    await asyncio.wait_for(
                        self._window_state.clear_trade_claim(
                            window_key, claim_id
                        ),
                        timeout=DB_AWAIT_TIMEOUT_S,
                    )
                    claim_released = True
                except asyncio.TimeoutError:
                    log.warning(
                        "execute_trade.clear_claim_timeout",
                        window=str(window_key),
                        phase=phase,
                        timeout_s=DB_AWAIT_TIMEOUT_S,
                    )
                except Exception as _clr_exc:
                    log.warning(
                        "execute_trade.clear_claim_failed",
                        window=str(window_key),
                        phase=phase,
                        error=str(_clr_exc)[:200],
                    )

        # ── Step 3: Risk check ─────────────────────────────────────────
        raw_status = self._risk.get_status()
        # Adapt dict→RiskStatus if the risk manager returns a dict (legacy)
        if isinstance(raw_status, dict):
            from domain.value_objects import RiskStatus

            risk_status = RiskStatus(
                current_bankroll=raw_status.get("current_bankroll", 500),
                peak_bankroll=raw_status.get("peak_bankroll", 500),
                drawdown_pct=raw_status.get("drawdown_pct", 0),
                daily_pnl=raw_status.get("daily_pnl", 0),
                consecutive_losses=raw_status.get("consecutive_losses", 0),
                paper_mode=raw_status.get("paper_mode", True),
                kill_switch_active=raw_status.get("kill_switch_active", False),
            )
        else:
            risk_status = raw_status
        approved, reason = self._check_risk(risk_status, stake)
        if not approved:
            log.info(
                "execute_trade.risk_blocked",
                strategy=sid,
                stake=stake.adjusted_stake,
                failure_reason=reason,
                window=str(window_key),
                claim_released=bool(claim_id),
            )
            self._fire_alert_async(
                "risk_blocked",
                self._alerter.send_system_alert(
                    f"BLOCKED {sid} {decision.strategy_version}\n"
                    f"Direction: {direction}\n"
                    f"Stake: ${stake.adjusted_stake:.2f}\n"
                    f"Reason: {reason}"
                ),
            )
            await _release_claim("risk_blocked")
            return _failed(
                reason,
                strategy_id=sid,
                direction=direction,
                stake_usd=stake.adjusted_stake,
            )

        # ── Step 4: Guardrails ─────────────────────────────────────────
        ok, guard_reason = self._check_guardrails()
        if not ok:
            log.info(
                "execute_trade.guardrail_blocked",
                strategy=sid,
                failure_reason=guard_reason,
                window=str(window_key),
                claim_released=bool(claim_id),
            )
            await _release_claim("guardrail_blocked")
            return _failed(
                guard_reason,
                strategy_id=sid,
                direction=direction,
                stake_usd=stake.adjusted_stake,
            )

        # ── Step 5: Token ID resolution ────────────────────────────────
        if direction == "DOWN":
            token_id = window_market.down_token_id
            side = "NO"
        else:
            token_id = window_market.up_token_id
            side = "YES"

        if not token_id:
            log.error(
                "execute_trade.no_token_id",
                strategy=sid,
                direction=direction,
                failure_reason="no_token_id",
                window=str(window_key),
                claim_released=bool(claim_id),
            )
            await _release_claim("no_token_id")
            return _failed(
                "no_token_id",
                strategy_id=sid,
                direction=direction,
            )

        # ── Step 5.5: Pessimistic fill-slot claim (audit #322) ─────────
        # SMOKING GUN, 2026-04-26: v10_lgb_only filled SIX times on
        # window 1777238100 (PID 838090, 21:18:24–21:20:56). Forensics:
        #
        #   * Step 0.5 has_filled() check passed all six times — the
        #     marker is only written AFTER FAK confirms in Step 8, but
        #     the FAK ladder takes ~2 minutes.
        #   * Per-strategy lease (15s TTL) auto-expired between eval
        #     ticks while the previous attempt's FAK was still in
        #     flight. Each new attempt found a clean lease to acquire.
        #   * All six FAKs eventually confirmed; only the FIRST mark_traded
        #     INSERT succeeded — the other five silently lost on the
        #     UNIQUE constraint AFTER the trades were already booked.
        #
        # Fix: pessimistically claim strategy_window_fills with a
        # placeholder ('pending') BEFORE firing FAK. UNIQUE constraint
        # on (asset, window_ts, timeframe, strategy_id) ensures only
        # one concurrent attempt wins. Subsequent has_filled() checks
        # observe the placeholder row and short-circuit.
        #
        # Placement: AFTER risk/guardrail/token checks so a blocked
        # attempt doesn't claim and immediately release the slot
        # (cheaper to fail fast on the cheaper checks first). BEFORE
        # the executor call so the slot is held for the full FAK
        # lifetime.
        #
        # Failure mode: try_claim_fill_slot returns False if another
        # in-process attempt already holds the placeholder. We treat
        # this exactly like a dedup hit — release the lease (we still
        # hold it), DO NOT call execute_order, return a benign skip.
        # ``slot_claimed`` was declared (False) earlier alongside
        # ``slot_released`` / ``claim_released`` so the closures bound
        # at definition time can name-resolve correctly.
        if hasattr(self._window_state, "try_claim_fill_slot"):
            try:
                # Audit 2026-04-26: timeout wrapper. UPSERT on a hot row
                # (same window, multiple strategies racing) is the most
                # likely candidate for the 7-min hang under DB pool
                # saturation. Capping at DB_AWAIT_TIMEOUT_S means a
                # stalled UPSERT bails out instead of holding the lease
                # for minutes.
                slot_claimed = await asyncio.wait_for(
                    self._window_state.try_claim_fill_slot(
                        window_key, sid
                    ),
                    timeout=DB_AWAIT_TIMEOUT_S,
                )
                _log_step("try_claim_fill_slot", claimed=bool(slot_claimed))
            except asyncio.TimeoutError:
                log.warning(
                    "execute_trade.try_claim_fill_slot_timeout",
                    strategy=sid,
                    window=str(window_key),
                    timeout_s=DB_AWAIT_TIMEOUT_S,
                )
                await _release_claim("fill_slot_claim_timeout")
                return _failed(
                    f"timeout_try_claim_fill_slot: {DB_AWAIT_TIMEOUT_S:.1f}s",
                    strategy_id=sid,
                    direction=direction,
                    stake_usd=stake.adjusted_stake,
                    token_id=token_id,
                )
            except Exception as exc:
                # Fail-closed defensive: refuse to fire FAK if our
                # pessimistic guard is broken. The lease (acquired in
                # Step 1) provides 15s residual in-flight protection;
                # next eval tick will retry.
                log.warning(
                    "execute_trade.try_claim_fill_slot_error",
                    strategy=sid,
                    window=str(window_key),
                    error=str(exc)[:200],
                )
                await _release_claim("fill_slot_claim_error")
                return _failed(
                    "fill_slot_claim_error",
                    strategy_id=sid,
                    direction=direction,
                    stake_usd=stake.adjusted_stake,
                    token_id=token_id,
                )
            if not slot_claimed:
                log.info(
                    "execute_trade.already_claimed_this_window",
                    strategy=sid,
                    window=str(window_key),
                    direction=direction,
                )
                await _release_claim("already_claimed_this_window")
                return _failed(
                    "already_claimed_this_window",
                    strategy_id=sid,
                    direction=direction,
                    stake_usd=stake.adjusted_stake,
                    token_id=token_id,
                )

        # ``slot_released`` was declared earlier alongside
        # ``claim_released`` so the closures defined here bind it via
        # nonlocal correctly (Python resolves free variables by static
        # enclosing scope at function-definition time).

        # Helper: release the pessimistic fill-slot claim if FAK did
        # not result in a real fill. Safe to call on the success
        # path too — the WHERE clause inside the adapter restricts
        # the DELETE to placeholder rows only.
        #
        # Audit #398 (2026-04-26): promoted release log to INFO and
        # surfaced rows_deleted so post-mortem can prove a release
        # actually happened. Sets ``slot_released`` so the finally
        # backstop can skip a redundant second call on common paths.
        async def _release_fill_slot(phase: str) -> bool:
            nonlocal slot_released
            if not slot_claimed or slot_released:
                return False
            if not hasattr(self._window_state, "release_fill_slot"):
                return False
            try:
                # Audit 2026-04-26: timeout wrapper. Forensics on window
                # 1777242900 showed a 2:21 gap between FAK return and
                # ``release_fill_slot`` log — the await silently parked
                # while the DB pool was saturated. Bounding at
                # DB_AWAIT_TIMEOUT_S means the strategy unblocks fast;
                # a stale 'pending' row will be cleaned up by either the
                # next try_claim_fill_slot's stale-takeover (60s TTL)
                # or the finally backstop on the next attempt.
                await asyncio.wait_for(
                    self._window_state.release_fill_slot(
                        window_key, sid
                    ),
                    timeout=DB_AWAIT_TIMEOUT_S,
                )
                slot_released = True
                log.info(
                    "execute_trade.release_fill_slot",
                    strategy=sid,
                    window=str(window_key),
                    phase=phase,
                )
                return True
            except asyncio.TimeoutError:
                log.warning(
                    "execute_trade.release_fill_slot_timeout",
                    strategy=sid,
                    window=str(window_key),
                    phase=phase,
                    timeout_s=DB_AWAIT_TIMEOUT_S,
                )
                # Don't set slot_released — the DELETE may still complete
                # in the background pool; mark it as "tried but unsure".
                # Stale-takeover on the next try_claim_fill_slot covers
                # the worst case.
                return False
            except Exception as _rel_exc:
                log.warning(
                    "execute_trade.release_fill_slot_failed",
                    strategy=sid,
                    window=str(window_key),
                    phase=phase,
                    error=str(_rel_exc)[:200],
                )
                return False

        # ── Step 6: Execute order ──────────────────────────────────────
        # Audit #398 (2026-04-26): SECOND smoking gun on the fill-slot
        # invariant. Window 1777240500 / v9_lgb_only at 22:07:47 left
        # a 'pending' placeholder row in strategy_window_fills with
        # NO matching trade row — the strategy was permanently locked
        # out for the rest of the window because every subsequent
        # has_filled() returned True.
        #
        # Root-cause analysis: the existing release paths only catch
        # ``Exception``. ``asyncio.CancelledError`` (BaseException
        # subclass since Python 3.8) propagates THROUGH them — so any
        # task cancellation between try_claim_fill_slot.win and
        # mark_traded.success leaves the placeholder dangling.
        # Plausible cancellation triggers: registry-level timeout,
        # orchestrator shutdown, parent task gc'd while awaiting
        # FAK ladder, asyncio.wait_for elsewhere on the call stack.
        #
        # Defence-in-depth fix: wrap the entire post-claim flow in
        # ``try / finally``. The ``committed`` flag is set ONLY after
        # mark_traded succeeds; the finally block releases the slot
        # whenever ``committed=False`` regardless of how we exited
        # (return, raise, BaseException). The explicit release calls
        # in the failure paths remain — they preserve the existing
        # log breadcrumbs and short-circuit the redundant finally
        # call (release_fill_slot is idempotent, but skipping it
        # keeps logs clean on the common paths).
        entry_cap = decision.entry_cap or DEFAULT_ENTRY_CAP
        start_ts = self._clock.now()

        committed = False
        result: Optional[ExecutionResult] = None
        try:
            try:
                # The FAK ladder has its own internal total-elapsed
                # timeout (FAK_LADDER_MAX_ELAPSED_S, default 20s). We
                # do NOT wrap with asyncio.wait_for here because that
                # would CancelError-bomb the executor mid-flight and
                # potentially leave a CLOB order half-submitted.
                _log_step("pre_execute_order")
                result = await self._executor.execute_order(
                    token_id=token_id,
                    side=side,
                    stake_usd=stake.adjusted_stake,
                    entry_cap=entry_cap,
                    price_floor=PRICE_FLOOR,
                )
                _log_step(
                    "post_execute_order",
                    success=bool(getattr(result, "success", False)),
                    failure_reason=getattr(result, "failure_reason", None),
                )
            except Exception as exc:
                await _release_fill_slot("execution_error")
                await _release_claim("execution_error")
                await self._on_order_error()
                log.error(
                    "execute_trade.execution_error",
                    strategy=sid,
                    failure_reason=f"execution_error: {str(exc)[:200]}",
                    exc_info=True,
                )
                return _failed(
                    f"execution_error: {str(exc)[:200]}",
                    strategy_id=sid,
                    direction=direction,
                    stake_usd=stake.adjusted_stake,
                    token_id=token_id,
                )

            end_ts = self._clock.now()

            # Enrich result with strategy identity and timing
            result = replace(
                result,
                strategy_id=sid,
                direction=direction,
                execution_start=start_ts,
                execution_end=end_ts,
                market_slug=window_market.market_slug,
            )

            if not result.success:
                # Audit #322: release the pessimistic fill-slot too — FAK
                # didn't produce a real fill so the slot can be retried by
                # the next eval tick. Order matters less but mirrors the
                # acquire order in reverse.
                await _release_fill_slot("order_not_filled")
                await _release_claim("order_not_filled")
                # Only real submit/infra errors increment the breaker counter.
                # Benign no-fills (empty book, FAK exhausted, GTC unfilled) are
                # market conditions, not faults — they skip cleanly.
                if _is_real_order_error(result.failure_reason):
                    await self._on_order_error()
                log.info(
                    "execute_trade.order_not_filled",
                    strategy=sid,
                    failure_reason=result.failure_reason,
                )
                return result

            # ── Step 7: Record trade ───────────────────────────────────────
            try:
                # Audit 2026-04-26: timeout wrapper. record_trade is
                # multiple INSERTs (trades, trade_events, etc) — slow
                # under DB pool saturation but never load-bearing for
                # the fill itself.
                await asyncio.wait_for(
                    self._recorder.record_trade(decision, result, stake),
                    timeout=DB_AWAIT_TIMEOUT_S,
                )
                _log_step("record_trade")
            except asyncio.TimeoutError:
                log.warning(
                    "execute_trade.record_trade_timeout",
                    strategy=sid,
                    window=str(window_key),
                    timeout_s=DB_AWAIT_TIMEOUT_S,
                )
            except Exception as exc:
                # Fire-and-forget spirit: log but don't fail the trade
                log.warning(
                    "execute_trade.record_error",
                    error=str(exc)[:200],
                )

            # ── Step 8: Mark traded ────────────────────────────────────────
            # Audit #321 (2026-04-26): pass strategy_id so the per-strategy
            # filled-marker (strategy_window_fills) is written. Without this,
            # has_filled() at Step 0.5 on the next eval tick would return
            # False and we'd happily fill again. Best-effort: mark_traded is
            # idempotent (ON CONFLICT DO NOTHING on the new table); legacy
            # WindowStateRepository implementations without the strategy_id
            # kwarg fall back via the Optional default to the old signature.
            #
            # ``committed=True`` after this block: the placeholder row has
            # been UPSERT'd to a real order_id, so the finally backstop must
            # NOT delete it (release_fill_slot is order_id='pending' scoped,
            # but skipping the call entirely is cleaner than relying on the
            # WHERE clause being correct forever).
            try:
                try:
                    # Audit 2026-04-26: timeout wrapper. mark_traded is
                    # the UPSERT that flips placeholder → real order_id.
                    # If it stalls under pool saturation we still want
                    # ``committed=True`` to fire so the finally backstop
                    # doesn't roll back the slot (the fill is real on
                    # CLOB regardless of our DB state).
                    await asyncio.wait_for(
                        self._window_state.mark_traded(
                            window_key,
                            result.order_id or "unknown",
                            strategy_id=sid,
                        ),
                        timeout=DB_AWAIT_TIMEOUT_S,
                    )
                except TypeError:
                    # Legacy port impl without strategy_id kwarg.
                    await asyncio.wait_for(
                        self._window_state.mark_traded(
                            window_key,
                            result.order_id or "unknown",
                        ),
                        timeout=DB_AWAIT_TIMEOUT_S,
                    )
                _log_step("mark_traded")
            except asyncio.TimeoutError:
                log.warning(
                    "execute_trade.mark_traded_timeout",
                    strategy=sid,
                    window=str(window_key),
                    timeout_s=DB_AWAIT_TIMEOUT_S,
                )
            except Exception as exc:
                log.warning(
                    "execute_trade.mark_traded_error",
                    error=str(exc)[:200],
                )
            # Mark slot as committed BEFORE alerts/Telegram fire — the
            # placeholder is now a real fill marker; the finally backstop
            # must not race a delete against the UPSERT. mark_traded
            # failure is logged but does NOT roll back commit: we'd
            # rather leak a placeholder than double-fill on a transient
            # DB hiccup, and has_filled() reads the same table so future
            # attempts still short-circuit on a successful UPSERT.
            committed = True

            # ── Step 8b: Unconditional FILL card (fire-and-forget) ─────────
            # Every successful fill on Polymarket gets a short confirmation
            # message. Complements send_strategy_trade_alert (which is a
            # richer entry card) and send_trade_resolved (which fires later
            # at window resolve). User-requested visibility guarantee.
            #
            # Audit 2026-04-26 (PR #397): NEVER await alerter on the success
            # path — see _fire_alert_async docstring. A hung Telegram POST
            # was holding execute() for ~2 minutes, delaying registry's
            # post-fill position_monitor.on_fill() registration past the
            # exit-evaluation window.
            if (
                not self._paper_mode
                and result.fill_size
                and result.fill_size > 0
                and hasattr(self._alerter, "send_fill_confirmed")
            ):
                self._fire_alert_async(
                    "fill_confirmed",
                    self._alerter.send_fill_confirmed(
                        strategy=sid,
                        window_ts=int(window_key.window_ts or 0),
                        side=direction,
                        price=float(result.fill_price or 0.0),
                        shares=float(result.fill_size or 0.0),
                        stake_usd=float(result.stake_usd or 0.0),
                        condition_id=getattr(window_market, "condition_id", None),
                        tx_hash=getattr(result, "tx_hash", None),
                        timeframe=window_key.timeframe,
                    ),
                )

            # ── Step 9: Telegram alert (rich strategy-aware, fire-and-forget) ──
            # See PR #397 note above: dispatched via _fire_alert_async so the
            # success log + ExecutionResult return reach the registry within
            # milliseconds, allowing position_monitor.on_fill to register the
            # exit watcher before the strategy's exit-eval window closes.
            try:
                gate_results = decision.metadata.get("gate_results", [])
                sizing_meta = decision.metadata.get("sizing", {})
                # Use rich strategy alert if available, fallback to plain text
                if hasattr(self._alerter, "send_strategy_trade_alert"):
                    self._fire_alert_async(
                        "strategy_trade_alert",
                        self._alerter.send_strategy_trade_alert(
                            strategy_id=sid,
                            strategy_version=decision.strategy_version,
                            direction=direction,
                            confidence=decision.confidence or "?",
                            confidence_score=decision.confidence_score or 0.0,
                            entry_reason=decision.entry_reason,
                            gate_results=gate_results,
                            sizing_modifier=sizing_meta.get("modifier", 1.0),
                            sizing_label=sizing_meta.get("label", "default"),
                            fill_price=result.fill_price or 0.0,
                            fill_size=result.fill_size or 0.0,
                            stake_usd=result.stake_usd,
                            order_type=(result.execution_mode or "paper").upper(),
                            order_id=result.order_id,
                            execution_mode=result.execution_mode,
                            timeframe=window_key.timeframe,
                            btc_price=current_btc_price,
                            vpin=getattr(self, "_last_vpin", 0.0),
                            regime=getattr(self, "_last_regime", "?"),
                            eval_offset=getattr(decision, "metadata", {}).get("eval_offset")
                            if decision.metadata
                            else None,
                            paper_mode=self._paper_mode,
                            success=result.success,
                            failure_reason=result.failure_reason or "",
                            elapsed_s=(result.execution_end - result.execution_start)
                            if result.execution_end > result.execution_start
                            else 0.0,
                            # Forward raw decision metadata so strategy-specific TG
                            # surfaces (e.g. v5_ensemble's signal_source / p_lgb /
                            # p_classifier / ensemble_config) flow to the renderer
                            # without each strategy needing its own kwarg.
                            decision_metadata=decision.metadata,
                        ),
                    )
                else:
                    alert_msg = self._format_trade_alert(
                        decision,
                        result,
                        stake,
                        current_btc_price,
                        open_price,
                    )
                    self._fire_alert_async(
                        "system_alert_fallback",
                        self._alerter.send_system_alert(alert_msg),
                    )
            except Exception as exc:
                log.warning(
                    "execute_trade.alert_error",
                    error=str(exc)[:200],
                )

            # ── Step 10: Update guardrail state ────────────────────────────
            self._record_order_placed()
            self._on_order_success()

            log.info(
                "execute_trade.success",
                strategy=sid,
                direction=direction,
                order_id=result.order_id,
                fill_price=result.fill_price,
                fill_size=result.fill_size,
                stake=result.stake_usd,
                mode=result.execution_mode,
            )

            return result
        finally:
            # Audit #398 (2026-04-26): unconditional release backstop.
            # Catches any path that exited with slot_claimed=True but
            # committed=False — most importantly asyncio.CancelledError
            # mid-FAK (BaseException, not caught by ``except Exception``).
            # No-op on the success path (committed=True), no-op on the
            # explicit-release paths (the slot was already deleted; the
            # adapter's WHERE clause restricts to placeholder rows so
            # the redundant call is a 0-row DELETE either way).
            #
            # Order matters: release_fill_slot first (long-lived, no TTL),
            # then _release_claim (15s TTL self-recovers but still nicer
            # to clear explicitly). Both are best-effort; failures log
            # WARN but never raise.
            if slot_claimed and not committed:
                try:
                    await _release_fill_slot("finally_uncommitted")
                except BaseException as _fin_exc:  # noqa: BLE001
                    # Last-resort guard: even the release helper itself
                    # mustn't propagate a fresh exception out of finally
                    # while the engine is unwinding (would mask the
                    # original cause). Log and move on.
                    log.warning(
                        "execute_trade.finally_release_fill_slot_failed",
                        strategy=sid,
                        window=str(window_key),
                        error=str(_fin_exc)[:200],
                    )
                try:
                    await _release_claim("finally_uncommitted")
                except BaseException as _fin_exc2:  # noqa: BLE001
                    log.warning(
                        "execute_trade.finally_release_claim_failed",
                        strategy=sid,
                        window=str(window_key),
                        error=str(_fin_exc2)[:200],
                    )

    # ─── Stake Calculation ─────────────────────────────────────────────

    def _calculate_stake(
        self,
        decision: StrategyDecision,
    ) -> StakeCalculation:
        """Calculate stake from risk status and decision sizing.

        Formula: bankroll * bet_fraction * price_multiplier
        where price_multiplier = (1 - token_price) / 0.50, clamped [0.5, 1.5]

        This means:
          - 50c token -> 1.0x multiplier (base stake)
          - 40c token -> 1.2x multiplier (better R/R, bet more)
          - 65c token -> 0.7x multiplier (worse R/R, bet less)
        """
        risk = self._risk.get_status()
        bankroll = (
            risk.get("current_bankroll", runtime.starting_bankroll)
            if isinstance(risk, dict)
            else risk.current_bankroll
        )
        # Always use runtime.bet_fraction (operator-set). YAML collateral_pct was
        # designed for $500 bankroll and overrides runtime on smaller wallets.
        bet_fraction = runtime.bet_fraction

        base_stake = bankroll * bet_fraction

        # Token price estimate for R/R scaling
        tp = max(0.30, min(0.65, decision.entry_cap or 0.50))
        price_multiplier = (1.0 - tp) / 0.50
        price_multiplier = max(0.5, min(1.5, price_multiplier))

        adjusted = base_stake * price_multiplier

        # Hard caps: enforce the runtime-configured absolute max bet.
        hard_cap = min(runtime.max_position_usd, bankroll * bet_fraction * 0.95)
        adjusted = min(adjusted, hard_cap)
        adjusted = round(adjusted, 2)

        return StakeCalculation(
            base_stake=base_stake,
            price_multiplier=price_multiplier,
            adjusted_stake=adjusted,
            bankroll=bankroll,
            bet_fraction=bet_fraction,
            hard_cap=hard_cap,
        )

    # ─── Risk Check ────────────────────────────────────────────────────

    @staticmethod
    def _check_risk(
        status: RiskStatus,
        stake: StakeCalculation,
    ) -> tuple[bool, str]:
        """Validate trade against risk limits."""
        if status.kill_switch_active:
            return False, "kill_switch_active"
        if status.drawdown_pct > runtime.max_drawdown_kill:
            return (
                False,
                f"drawdown {status.drawdown_pct:.1%} > {runtime.max_drawdown_kill:.0%}",
            )
        min_bet = min(MIN_BET_USD, runtime.min_bet_usd)
        if stake.adjusted_stake < min_bet:
            return (
                False,
                f"stake ${stake.adjusted_stake:.2f} < ${min_bet:.2f} minimum",
            )
        return True, ""

    # ─── Guardrails ────────────────────────────────────────────────────

    def _check_guardrails(self) -> tuple[bool, str]:
        """Rate limit + circuit breaker checks."""
        now = self._clock.now()

        # Circuit breaker
        if self._circuit_break_until > now:
            remaining = self._circuit_break_until - now
            return False, f"circuit_breaker: {remaining:.0f}s remaining"

        # Rate limit: min interval between orders
        if self._last_order_time > 0:
            elapsed = now - self._last_order_time
            if elapsed < MIN_ORDER_INTERVAL_S:
                return False, f"rate_limit: {elapsed:.1f}s < {MIN_ORDER_INTERVAL_S}s"

        # Hourly cap
        cutoff = now - 3600.0
        self._order_timestamps = [ts for ts in self._order_timestamps if ts > cutoff]
        if len(self._order_timestamps) >= MAX_ORDERS_PER_HOUR:
            return (
                False,
                f"rate_limit: {len(self._order_timestamps)} >= {MAX_ORDERS_PER_HOUR}/hr",
            )

        return True, ""

    def _record_order_placed(self) -> None:
        """Track order timestamp for rate limiting."""
        now = self._clock.now()
        self._last_order_time = now
        self._order_timestamps.append(now)

    def _on_order_success(self) -> None:
        """Reset consecutive error counter on success."""
        self._consecutive_errors = 0

    async def _on_order_error(self) -> None:
        """Track consecutive errors and trigger circuit breaker."""
        self._consecutive_errors += 1
        if self._consecutive_errors >= CIRCUIT_BREAKER_ERRORS:
            self._circuit_break_until = self._clock.now() + CIRCUIT_BREAKER_COOLDOWN_S
            log.warning(
                "execute_trade.circuit_breaker_tripped",
                errors=self._consecutive_errors,
                cooldown_s=CIRCUIT_BREAKER_COOLDOWN_S,
            )
            self._fire_alert_async(
                "circuit_breaker_tripped",
                self._alerter.send_system_alert(
                    f"⚠️ *Circuit breaker tripped*\n"
                    f"{self._consecutive_errors} consecutive order errors\n"
                    f"Cooldown: {CIRCUIT_BREAKER_COOLDOWN_S}s"
                ),
            )

    # ─── Async alerter dispatch ────────────────────────────────────────
    #
    # Audit 2026-04-26 (PR #397): a hung `aiohttp` POST inside
    # ``alerter.send_*`` was blocking ``execute()`` for ~2 minutes after
    # the FAK fill. The registry only registers the position with
    # ``position_monitor.on_fill`` AFTER ``execute()`` returns, so the
    # exit-monitor evaluation window (``exit_eval_start_offset`` ..
    # ``exit_eval_end_offset``) had already elapsed by the time the
    # monitor was wired up — stop-loss never had a chance to fire.
    #
    # Rule: alerter calls are advisory. They MUST NOT sit on the
    # critical path that gates exit-monitor registration. Every
    # alerter coroutine on the success / risk-block / circuit-breaker
    # path is dispatched via ``asyncio.create_task`` so ``execute()``
    # returns immediately. Failures are caught and logged inside the
    # background task — the trading path never sees them.
    def _fire_alert_async(
        self, label: str, coro: Coroutine[Any, Any, Any]
    ) -> None:
        """Run an alerter coroutine in the background, swallow & log errors.

        ``label`` identifies which alerter call emitted the warning if
        the coroutine raises (e.g. ``"strategy_trade_alert"``).
        """

        async def _runner() -> None:
            try:
                await coro
            except Exception as exc:
                log.warning(
                    "execute_trade.alerter_send_failed",
                    label=label,
                    error=str(exc)[:200],
                )

        try:
            asyncio.create_task(_runner())
        except RuntimeError:
            # No running loop (e.g. during a sync test path). Close the
            # coroutine to avoid a "coroutine was never awaited" warning
            # and bail silently — this branch never hits in production.
            try:
                coro.close()
            except Exception:
                pass

    # ─── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _make_window_key(market: WindowMarket) -> WindowKey:
        """Extract WindowKey from a WindowMarket's slug.

        Slug format: "btc-updown-5m-1713000000"
        """
        parts = market.market_slug.split("-")
        asset = parts[0].upper() if parts else "BTC"
        try:
            window_ts = int(parts[-1])
        except (ValueError, IndexError):
            window_ts = 0
        timeframe = "5m"
        if len(parts) >= 3:
            timeframe = parts[2]
        return WindowKey(asset=asset, window_ts=window_ts, timeframe=timeframe)

    @staticmethod
    def calculate_fee(price: float, stake: float) -> float:
        """Polymarket binary options fee: 7.2% * p * (1-p) * stake."""
        return FEE_MULTIPLIER * price * (1.0 - price) * stake

    def _format_trade_alert(
        self,
        decision: StrategyDecision,
        result: ExecutionResult,
        stake: StakeCalculation,
        btc_price: float,
        open_price: float,
    ) -> str:
        """Format the Telegram trade alert message.

        Strategy name always in subject line for immediate identification.
        """
        sid = decision.strategy_id
        ver = decision.strategy_version
        direction = decision.direction or "?"
        mode = result.execution_mode.upper()

        # Gate results summary
        gate_lines = ""
        gate_results = decision.metadata.get("gate_results", [])
        if gate_results:
            checks = []
            for g in gate_results:
                icon = "\u2705" if g.get("passed") else "\u274c"
                checks.append(f"{icon}{g.get('gate', '?')}")
            gate_lines = f"\n\u26a1 Gates: {' '.join(checks)}"

        # Sizing info
        sizing_meta = decision.metadata.get("sizing", {})
        size_label = sizing_meta.get("label", "default")
        modifier = sizing_meta.get("modifier", 1.0)

        # Delta from btc price
        delta_pct = (
            ((btc_price - open_price) / open_price * 100) if open_price > 0 else 0.0
        )

        lines = [
            f"TRADE {sid} {ver}",
            f"Direction: {direction} ({result.token_id[:20]}...)"
            if result.token_id
            else f"Direction: {direction}",
            f"Confidence: {decision.confidence or '?'} ({decision.confidence_score or 0:.2f})",
            "",
        ]

        if gate_lines:
            lines.append(gate_lines.strip())

        lines.extend(
            [
                f"\U0001f4b0 Sizing: {modifier:.1f}x ({size_label})",
                "",
                f"\u2705 {mode} {'FILLED' if result.success else 'FAILED'}",
            ]
        )

        if result.success and result.fill_price:
            fee = self.calculate_fee(result.fill_price, result.stake_usd)
            lines.extend(
                [
                    f"\U0001f4b5 Fill: ${result.fill_price:.2f} | "
                    f"Size: {result.fill_size:.1f} shares | "
                    f"Stake: ${result.stake_usd:.2f}",
                    f"\U0001f4b8 Fee: ${fee:.2f}",
                ]
            )
            if result.execution_end > result.execution_start:
                elapsed = result.execution_end - result.execution_start
                lines.append(f"\u23f1 Filled in {elapsed:.1f}s")
        elif result.failure_reason:
            lines.append(f"\u274c Reason: {result.failure_reason}")

        lines.extend(
            [
                "",
                f"Entry: {decision.entry_reason}",
                f"BTC: ${btc_price:,.0f} -> delta {delta_pct:+.2f}%",
            ]
        )

        if self._paper_mode:
            lines.insert(0, "\U0001f4dd PAPER MODE")

        return "\n".join(lines)
