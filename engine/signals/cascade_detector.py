"""
Cascade Detector — Finite State Machine

Detects forced liquidation cascades in BTC perpetuals and generates
directional betting signals for the prediction market strategies.

State Machine:
  IDLE
    → on VPIN > CASCADE_THRESHOLD + OI drop > 2% + liq vol > $5M
    → CASCADE_DETECTED

  CASCADE_DETECTED
    → monitor direction of cascade (which side is liquidating)
    → EXHAUSTING (when liq volume starts declining or OI stabilises)

  EXHAUSTING
    → wait for cascade to exhaust (price reverting, liq vol dropping)
    → BET_SIGNAL (emit directional signal: fade the cascade)

  BET_SIGNAL
    → signal emitted; wait for strategies to act
    → COOLDOWN (immediately after emission)

  COOLDOWN (COOLDOWN_SECONDS = 900)
    → wait for cooldown to expire
    → IDLE

Rationale:
  Forced liquidation cascades are predictably mean-reverting events.
  After a cascade exhausts, the overshot price tends to recover toward
  pre-cascade levels. This creates a time-limited edge on prediction
  markets that price the next BTC price range.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Awaitable, Optional
import structlog

from config.constants import (
    VPIN_CASCADE_THRESHOLD,
    CASCADE_OI_DROP_THRESHOLD,
    CASCADE_LIQ_VOLUME_THRESHOLD,
    COOLDOWN_SECONDS,
)
from data.models import VPINSignal, OpenInterestSnapshot, CascadeSignal

log = structlog.get_logger(__name__)


class CascadeState(str, Enum):
    IDLE = "IDLE"
    CASCADE_DETECTED = "CASCADE_DETECTED"
    EXHAUSTING = "EXHAUSTING"
    BET_SIGNAL = "BET_SIGNAL"
    COOLDOWN = "COOLDOWN"


class CascadeDetector:
    """
    FSM that tracks liquidation cascade lifecycle and emits
    directional CascadeSignal when a fade opportunity is detected.
    """

    def __init__(
        self,
        on_signal: Callable[[CascadeSignal], Awaitable[None]] | None = None,
    ) -> None:
        self._on_signal = on_signal
        self._state = CascadeState.IDLE
        self._direction: Optional[str] = None  # YES (price up) | NO (price down)
        self._cascade_start: Optional[datetime] = None
        self._cooldown_until: Optional[datetime] = None

        # Latest inputs
        self._vpin: float = 0.0
        self._oi_delta_pct: float = 0.0
        self._liq_volume_usd: float = 0.0
        self._prev_liq_volume: float = 0.0

    async def on_vpin(self, signal: VPINSignal) -> None:
        """Update VPIN value and evaluate state transition."""
        self._vpin = signal.value
        await self._evaluate()

    async def on_open_interest(self, oi: OpenInterestSnapshot) -> None:
        """Update OI delta and evaluate state transition."""
        self._oi_delta_pct = oi.open_interest_delta_pct
        await self._evaluate()

    def update_liquidation_volume(self, liq_volume_usd: float) -> None:
        """Update latest liquidation volume (called by aggregator)."""
        self._prev_liq_volume = self._liq_volume_usd
        self._liq_volume_usd = liq_volume_usd

    @property
    def state(self) -> CascadeState:
        return self._state

    async def _evaluate(self) -> None:
        """Main FSM transition logic."""
        now = datetime.utcnow()

        if self._state == CascadeState.IDLE:
            await self._check_idle(now)

        elif self._state == CascadeState.CASCADE_DETECTED:
            await self._check_cascade_detected(now)

        elif self._state == CascadeState.EXHAUSTING:
            await self._check_exhausting(now)

        elif self._state == CascadeState.BET_SIGNAL:
            # Immediately transition to cooldown after signal is emitted
            await self._transition(CascadeState.COOLDOWN, now)

        elif self._state == CascadeState.COOLDOWN:
            await self._check_cooldown(now)

    async def _check_idle(self, now: datetime) -> None:
        """IDLE → CASCADE_DETECTED when all cascade conditions met."""
        cascade_conditions = (
            self._vpin >= VPIN_CASCADE_THRESHOLD
            and abs(self._oi_delta_pct) >= CASCADE_OI_DROP_THRESHOLD
            and self._liq_volume_usd >= CASCADE_LIQ_VOLUME_THRESHOLD
        )
        if cascade_conditions:
            # Determine direction: OI drop with price selling → NO; buying → YES
            self._direction = "NO" if self._oi_delta_pct < 0 else "YES"
            self._cascade_start = now
            log.info("cascade.detected", direction=self._direction, vpin=self._vpin)
            await self._transition(CascadeState.CASCADE_DETECTED, now)

    async def _check_cascade_detected(self, now: datetime) -> None:
        """CASCADE_DETECTED → EXHAUSTING when liq volume peaks and starts declining."""
        liq_declining = self._liq_volume_usd < self._prev_liq_volume * 0.85
        if liq_declining or self._vpin < VPIN_CASCADE_THRESHOLD:
            log.info("cascade.exhausting", vpin=self._vpin, liq=self._liq_volume_usd)
            await self._transition(CascadeState.EXHAUSTING, now)

    async def _check_exhausting(self, now: datetime) -> None:
        """EXHAUSTING → BET_SIGNAL when cascade is clearly exhausted."""
        fully_exhausted = (
            self._vpin < 0.55  # Flow normalising
            and self._liq_volume_usd < CASCADE_LIQ_VOLUME_THRESHOLD * 0.5
        )
        if fully_exhausted:
            log.info("cascade.bet_signal", direction=self._direction)
            await self._transition(CascadeState.BET_SIGNAL, now)
            await self._emit_signal(now)

    async def _check_cooldown(self, now: datetime) -> None:
        """COOLDOWN → IDLE when cooldown period expires."""
        if self._cooldown_until and now >= self._cooldown_until:
            log.info("cascade.cooldown_expired")
            self._direction = None
            await self._transition(CascadeState.IDLE, now)

    async def _transition(self, new_state: CascadeState, now: datetime) -> None:
        """Apply state transition."""
        log.debug("cascade.transition", from_=self._state, to=new_state)
        self._state = new_state
        if new_state == CascadeState.COOLDOWN:
            self._cooldown_until = now + timedelta(seconds=COOLDOWN_SECONDS)

    async def _emit_signal(self, now: datetime) -> None:
        """Emit CascadeSignal to registered handler."""
        signal = CascadeSignal(
            state=CascadeState.BET_SIGNAL,
            direction=self._direction,
            vpin=self._vpin,
            oi_delta_pct=self._oi_delta_pct,
            liq_volume_usd=self._liq_volume_usd,
            timestamp=now,
        )
        if self._on_signal:
            await self._on_signal(signal)
