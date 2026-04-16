"""Use case: Publish Heartbeat.

Replaces: ``engine/strategies/orchestrator.py::_heartbeat_loop``
          (lines 1725-2190, ~466 LOC).

Responsibility
--------------
Every 10 seconds (per call to :meth:`tick`), read risk-manager state,
wallet balance, open-order count, and runtime-config snapshot.  Write a
``HeartbeatRow`` to the system_state table.

Every 5 minutes (30th call), additionally build a ``SitrepPayload`` and
send it to Telegram via the AlerterPort.

The mode-sync logic (reading paper/live toggles from DB and switching
the engine mode) is explicitly excluded from this use case -- it remains
in the orchestrator as a separate concern per migration plan section 5.4.

This use case is **not** wired into the orchestrator yet.  It exists
alongside the god class so the orchestrator continues to run its own
``_heartbeat_loop`` unchanged.  The wiring will happen in Phase 3.

Port dependencies (all from ``engine/domain/ports.py``):
  - RiskManagerPort -- get_status() -> RiskStatus
  - SystemStateRepository -- write_heartbeat, get_daily_record
  - AlerterPort -- send_heartbeat_sitrep
  - Clock -- deterministic time for testing
"""

from __future__ import annotations

import logging
from typing import Optional, Protocol

from domain.ports import SystemStateRepository
from use_cases.ports import AlerterPort, Clock, RiskManagerPort
from domain.value_objects import (
    HeartbeatRow,
    RiskStatus,
    SitrepPayload,
)

logger = logging.getLogger(__name__)


def _rs(risk_status, key: str, default=None):
    """Dict-or-dataclass accessor.

    Real ``RiskManager.get_status()`` returns ``dict``; unit tests build a
    ``RiskStatus`` dataclass. Support both without tying this use case to
    either shape.
    """
    if isinstance(risk_status, dict):
        return risk_status.get(key, default)
    return getattr(risk_status, key, default)


class EngineStateReader(Protocol):
    """Read-only view of live engine state for heartbeat assembly.

    This protocol defines the minimal surface area the heartbeat use case
    needs from the running engine.  The orchestrator (or a dedicated
    adapter) implements this protocol and injects it at construction time.

    Using a Protocol here avoids coupling the use case to the Orchestrator
    class, the Aggregator, the VPIN calculator, or any other concrete
    engine component.
    """

    @property
    def vpin(self) -> float:
        ...

    @property
    def btc_price(self) -> float:
        ...

    @property
    def open_positions_count(self) -> int:
        ...

    @property
    def paper_mode(self) -> bool:
        ...

    @property
    def starting_bankroll(self) -> float:
        ...

    @property
    def cascade_state(self) -> Optional[str]:
        ...

    @property
    def feed_status(self) -> dict[str, bool]:
        ...


class PublishHeartbeatUseCase:
    """Write system-state heartbeat and optionally publish SITREP.

    Each call to :meth:`tick` writes a heartbeat row.  Every 30th call
    (configurable via ``sitrep_interval``), it also builds and sends a
    SITREP to Telegram.

    The caller (orchestrator) decides the tick interval (currently 10s).
    """

    def __init__(
        self,
        risk_manager: RiskManagerPort,
        system_state_repo: SystemStateRepository,
        alerts: AlerterPort,
        clock: Clock,
        engine_state: EngineStateReader,
        *,
        sitrep_interval: int = 30,
        wallet_check_interval: int = 6,
        vpin_thresholds: Optional[dict[str, float]] = None,
    ) -> None:
        self._risk_manager = risk_manager
        self._system_state_repo = system_state_repo
        self._alerts = alerts
        self._clock = clock
        self._engine_state = engine_state

        self._sitrep_interval = sitrep_interval
        self._wallet_check_interval = wallet_check_interval
        self._vpin_thresholds = vpin_thresholds or {
            "cascade": 0.85,
            "transition": 0.65,
            "normal": 0.45,
        }

        # Internal counters
        self._tick_count: int = 0
        self._wallet_check_counter: int = 0
        self._cached_wallet_balance: Optional[float] = None

    def set_wallet_balance(self, balance: Optional[float]) -> None:
        """Inject wallet balance refreshed by the caller (RunHeartbeatTickUseCase).

        Keeps the two use cases in sync without coupling — the heartbeat tick
        owns the poly_client poll cadence; this UC just needs the latest value
        to write into system_state.config and the SITREP payload.
        """
        self._cached_wallet_balance = balance

    async def tick(self) -> None:
        """Called every heartbeat interval (10s).

        1. Read risk state
        2. Write HeartbeatRow to DB
        3. Every sitrep_interval ticks: build and send SITREP

        Feed-connectivity writes remain in the runtime (it owns the feed
        objects and their real `.connected` state); this UC only persists
        risk + balance + runtime_config to system_state.
        """
        try:
            risk_status = self._risk_manager.get_status()
            await self._write_heartbeat(risk_status)

            self._tick_count += 1
            if self._tick_count >= self._sitrep_interval:
                self._tick_count = 0
                import os as _os
                if _os.environ.get("LEGACY_EXECUTION_DISABLED", "").lower() != "true":
                    await self._send_sitrep(risk_status)

        except Exception as exc:
            # exc_info=True so the traceback lands in the structlog stdlib
            # bridge (extra= was being dropped by the processor).
            logger.error("heartbeat.tick_error: %s", str(exc), exc_info=True)

    async def _write_heartbeat(self, risk_status: RiskStatus) -> None:
        """Persist a HeartbeatRow to the system_state table."""
        # Lazy import: config.__init__ triggers settings validation which
        # fails in unit tests without env vars. Import at call site instead.
        try:
            from config.runtime_config import runtime as _runtime
            runtime_snapshot = _runtime.snapshot()
        except Exception as exc:
            logger.debug("heartbeat.runtime_snapshot_failed", extra={"error": str(exc)[:120]})
            runtime_snapshot = {}

        config_snapshot = {
            "wallet_balance_usdc": self._cached_wallet_balance,
            "daily_pnl": _rs(risk_status, "daily_pnl", 0.0),
            "consecutive_losses": _rs(risk_status, "consecutive_losses", 0),
            "paper_mode": _rs(risk_status, "paper_mode", True),
            "kill_switch_active": _rs(risk_status, "kill_switch_active", False),
            "runtime_config": runtime_snapshot,
        }

        row = HeartbeatRow(
            engine_status="running",
            current_balance=_rs(risk_status, "current_bankroll", 0.0),
            peak_balance=_rs(risk_status, "peak_bankroll", 0.0),
            drawdown_pct=_rs(risk_status, "drawdown_pct", 0.0),
            last_vpin=self._engine_state.vpin,
            last_cascade_state=self._engine_state.cascade_state,
            active_positions=self._engine_state.open_positions_count,
            config_snapshot=config_snapshot,
            timestamp=self._clock.now(),
        )

        await self._system_state_repo.write_heartbeat(row)

    async def _send_sitrep(self, risk_status: RiskStatus) -> None:
        """Build and send a 5-minute SITREP to Telegram."""
        try:
            wins_today, losses_today = await self._system_state_repo.get_daily_record()
            total = wins_today + losses_today
            win_rate = (wins_today / total) if total > 0 else 0.0  # fraction 0-1

            vpin = self._engine_state.vpin
            vpin_regime = self._classify_vpin_regime(vpin)

            wallet = (
                self._cached_wallet_balance
                or _rs(risk_status, "current_bankroll", 0.0)
                or 0.0
            )

            payload = SitrepPayload(
                engine_status="KILLED" if _rs(risk_status, "kill_switch_active", False) else "ACTIVE",
                mode_label="PAPER" if self._engine_state.paper_mode else "LIVE",
                wallet_balance=wallet,
                daily_pnl=_rs(risk_status, "daily_pnl", 0.0),
                starting_bankroll=self._engine_state.starting_bankroll,
                wins_today=wins_today,
                losses_today=losses_today,
                win_rate=win_rate,
                vpin=vpin,
                vpin_regime=vpin_regime,
                btc_price=self._engine_state.btc_price,
                open_positions=self._engine_state.open_positions_count,
                drawdown_pct=_rs(risk_status, "drawdown_pct", 0.0),
                kill_switch_active=_rs(risk_status, "kill_switch_active", False),
            )

            await self._alerts.send_heartbeat_sitrep(payload)
            logger.info("sitrep.sent")

        except Exception as exc:
            logger.warning(
                "sitrep.failed",
                extra={"error": str(exc)},
            )

    def _classify_vpin_regime(self, vpin: float) -> str:
        """Classify the current VPIN into a regime label."""
        t = self._vpin_thresholds
        if vpin >= t.get("cascade", 0.85):
            return "CASCADE"
        elif vpin >= t.get("transition", 0.65):
            return "TRANSITION"
        elif vpin >= t.get("normal", 0.45):
            return "NORMAL"
        else:
            return "CALM"
