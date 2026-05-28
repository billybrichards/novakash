"""MonitorOpenTradesUseCase — evaluates open trades per tick for shadow exits.

Called from the engine's per-tick orchestrator AFTER the strategy pass,
fire-and-forget via asyncio.create_task. Any exception is caught and logged
at WARNING — this use case MUST NOT crash the engine.

Architecture: repo-pattern for DB writes (ShadowExitGateway). The caller
passes the current surface snapshot and CLOB book, not DB connections
directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional, Protocol

import structlog

from exit_monitor.domain.exit_signal_detector import (
    DEFAULT_THRESHOLDS,
    ExitSignalDetector,
)
from exit_monitor.domain.shadow_trigger import ShadowTrigger

log = structlog.get_logger(__name__)

# One detector per open trade, keyed by decision_id.
_detectors: dict[int, ExitSignalDetector] = {}


# ── Ports (repo-pattern interfaces) ──────────────────────────────────────────


class ShadowExitRepo(Protocol):
    """Async write interface — implemented by ShadowExitGateway."""

    async def insert_trigger(
        self,
        trigger: ShadowTrigger,
        clob_best_bid_held: Optional[float],
        clob_best_ask_held: Optional[float],
        clob_best_bid_against: Optional[float],
        clob_best_ask_against: Optional[float],
        clob_book_depth_usd: Optional[float],
    ) -> None: ...


# ── DTOs ─────────────────────────────────────────────────────────────────────


@dataclass
class OpenTradeState:
    """Minimal descriptor for one open monitored trade.

    Populated by the wire-up layer from strategy_decisions / PositionMonitor
    data. The use case is agnostic about how these are fetched.
    """

    decision_id: int
    asset: str
    window_ts: int
    strategy_id: str          # e.g. 'tickformer_v18_t180'
    side: str                 # 'UP' | 'DN'
    entry_p: Optional[float]  # TickFormer prob at entry tick
    entry_eval_offset: Optional[int]


@dataclass
class CLOBSnapshot:
    """Live CLOB best bid/ask for both sides at evaluation tick.

    All fields may be None — the CLOBSnapshotReader falls back to None
    when the sidecar feed is unavailable (better to bank prob data without
    CLOB context than to drop the row entirely).
    """

    best_bid_held: Optional[float] = None
    best_ask_held: Optional[float] = None
    best_bid_against: Optional[float] = None
    best_ask_against: Optional[float] = None
    book_depth_usd: Optional[float] = None


# ── Use case ─────────────────────────────────────────────────────────────────


class MonitorOpenTradesUseCase:
    """Evaluates all open trades against TickFormer p_against thresholds.

    On each eval tick:
      1. Reads open trade states from the wire-up layer (already filtered
         to non-expired windows).
      2. For each trade, looks up (or creates) its ExitSignalDetector.
      3. Calls detector.should_shadow_exit() — pure function, no IO.
      4. For each trigger returned, calls repo.insert_trigger() with
         the CLOB snapshot — fire-and-forget.
      5. Cleans up detectors for trades that are no longer open.

    This use case is instantiated once at engine startup and injected into
    the per-tick hook. It maintains detector state across ticks.
    """

    def __init__(
        self,
        repo: ShadowExitRepo,
        thresholds: tuple[float, ...] = DEFAULT_THRESHOLDS,
    ) -> None:
        self._repo = repo
        self._thresholds = thresholds
        self._detectors: dict[int, ExitSignalDetector] = {}

    async def execute(
        self,
        open_trades: list[OpenTradeState],
        prob_tickformer_by_asset: dict[str, float],  # asset -> P(UP)
        clob_by_asset: dict[str, CLOBSnapshot],      # asset -> CLOBSnapshot
        eval_offset_by_asset: dict[str, int],        # asset -> seconds-to-close
    ) -> None:
        """Evaluate all open trades; write shadow rows for new threshold crossings.

        Wrapped by the wire-up layer in try/except — exceptions here are
        already guarded at the call site. We add an inner guard anyway per
        the design contract (this use case MUST NOT crash the engine).
        """
        try:
            await self._run(
                open_trades,
                prob_tickformer_by_asset,
                clob_by_asset,
                eval_offset_by_asset,
            )
        except Exception as exc:
            log.warning(
                "exit_monitor.monitor_open_trades.error",
                error=str(exc)[:300],
            )

    async def _run(
        self,
        open_trades: list[OpenTradeState],
        prob_tickformer_by_asset: dict[str, float],
        clob_by_asset: dict[str, CLOBSnapshot],
        eval_offset_by_asset: dict[str, int],
    ) -> None:
        active_ids: set[int] = set()

        for trade in open_trades:
            active_ids.add(trade.decision_id)

            prob = prob_tickformer_by_asset.get(trade.asset)
            if prob is None:
                log.debug(
                    "exit_monitor.no_tickformer_prob",
                    decision_id=trade.decision_id,
                    asset=trade.asset,
                )
                continue

            eval_offset = eval_offset_by_asset.get(trade.asset)
            if eval_offset is None:
                continue

            clob = clob_by_asset.get(trade.asset, CLOBSnapshot())

            detector = self._detectors.setdefault(
                trade.decision_id, ExitSignalDetector()
            )

            # Detect tickformer_model from strategy_id
            tickformer_model = "v18"
            if "v20" in (trade.strategy_id or "").lower():
                tickformer_model = "v20"

            triggers = detector.should_shadow_exit(
                decision_id=trade.decision_id,
                asset=trade.asset,
                window_ts=trade.window_ts,
                strategy_id=trade.strategy_id,
                side=trade.side,
                prob_tickformer=prob,
                eval_offset=eval_offset,
                tickformer_model=tickformer_model,
                entry_p=trade.entry_p,
                entry_eval_offset=trade.entry_eval_offset,
                thresholds=self._thresholds,
            )

            for trigger in triggers:
                log.info(
                    "exit_monitor.shadow_trigger",
                    decision_id=trigger.decision_id,
                    asset=trigger.asset,
                    strategy_id=trigger.strategy_id,
                    side=trigger.side,
                    threshold=f"{trigger.threshold:.3f}",
                    p_against=f"{trigger.p_against:.4f}",
                    eval_offset=trigger.trigger_eval_offset,
                    tickformer_model=trigger.tickformer_model,
                )
                try:
                    await self._repo.insert_trigger(
                        trigger=trigger,
                        clob_best_bid_held=clob.best_bid_held,
                        clob_best_ask_held=clob.best_ask_held,
                        clob_best_bid_against=clob.best_bid_against,
                        clob_best_ask_against=clob.best_ask_against,
                        clob_book_depth_usd=clob.book_depth_usd,
                    )
                except Exception as exc:
                    log.warning(
                        "exit_monitor.insert_trigger_error",
                        decision_id=trigger.decision_id,
                        threshold=trigger.threshold,
                        error=str(exc)[:200],
                    )

        # Clean up detectors for closed positions
        closed_ids = set(self._detectors.keys()) - active_ids
        for did in closed_ids:
            self._detectors.pop(did, None)
            log.debug("exit_monitor.detector_removed", decision_id=did)
