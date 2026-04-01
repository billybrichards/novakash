"""
Cascade Detector — Finite State Machine.

Detects liquidation cascades in BTC perpetual futures and signals when the
cascade is exhausting (i.e. the optimal moment to enter a mean-reversion bet).

State transitions
-----------------
IDLE
  → CASCADE_DETECTED  when VPIN ≥ 0.70 AND |oi_delta_pct| ≥ 2% AND liq_5m ≥ $5 M

CASCADE_DETECTED
  → EXHAUSTING        when liq volume declining (< 85% of previous reading)
                      OR VPIN drops below 0.70

EXHAUSTING
  → BET_SIGNAL        when VPIN < 0.55 AND liq_volume < $2.5 M

BET_SIGNAL
  → COOLDOWN          immediately after signal is emitted

COOLDOWN
  → IDLE              after runtime.cooldown_seconds (900 s)
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable, Awaitable, Optional

import structlog

from data.models import CascadeSignal

log = structlog.get_logger(__name__)

# Thresholds — read from config/constants.py (which reads env vars)
from config.runtime_config import runtime

_VPIN_CASCADE_ENTRY: float = runtime.vpin_cascade_threshold
_VPIN_EXHAUSTION: float = runtime.vpin_informed_threshold
_OI_DELTA_MIN: float = runtime.cascade_oi_drop_threshold
_LIQ_VOLUME_CASCADE: float = runtime.cascade_liq_volume_threshold
_LIQ_VOLUME_EXHAUSTION: float = runtime.cascade_liq_volume_threshold / 2.0
_LIQ_DECLINE_RATIO: float = 0.85


class CascadeDetector:
    """
    FSM that monitors VPIN, open-interest delta, and liquidation volume to
    detect and time liquidation cascades.

    Parameters
    ----------
    on_signal:
        Optional async callback invoked with a :class:`CascadeSignal` on each
        state transition that produces a public signal.
    """

    # Valid states
    IDLE = "IDLE"
    CASCADE_DETECTED = "CASCADE_DETECTED"
    EXHAUSTING = "EXHAUSTING"
    BET_SIGNAL = "BET_SIGNAL"
    COOLDOWN = "COOLDOWN"

    def __init__(
        self,
        on_signal: Optional[Callable[[CascadeSignal], Awaitable[None]]] = None,
    ) -> None:
        self._on_signal = on_signal
        self._state: str = self.IDLE
        self._direction: Optional[str] = None

        # Timing for COOLDOWN
        self._cooldown_start: Optional[float] = None

        # Previous liq volume reading (for decline detection)
        self._prev_liq_volume: Optional[float] = None

        self._log = log.bind(component="CascadeDetector")
        self._log.info("initialised", state=self._state)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        """Current FSM state string."""
        return self._state

    @property
    def direction(self) -> Optional[str]:
        """
        Direction of the detected cascade: ``"down"`` (longs liquidated,
        price fell) or ``"up"`` (shorts liquidated, price rose).
        ``None`` when no cascade is active.
        """
        return self._direction

    async def update(
        self,
        vpin: float,
        oi_delta_pct: float,
        liq_volume_5m: float,
        btc_price: float,
        btc_price_5m_ago: float,
    ) -> None:
        """
        Drive the FSM with the latest market readings.

        Parameters
        ----------
        vpin:
            Current VPIN value in [0, 1].
        oi_delta_pct:
            Fractional change in open interest over the recent window
            (e.g. -0.03 = −3 %).
        liq_volume_5m:
            Total liquidation volume (USD) in the last 5 minutes.
        btc_price:
            Current BTC price.
        btc_price_5m_ago:
            BTC price 5 minutes ago (used to determine cascade direction).
        """
        # Determine direction from price movement
        direction = "down" if btc_price < btc_price_5m_ago else "up"

        self._log.debug(
            "update",
            state=self._state,
            vpin=round(vpin, 4),
            oi_delta_pct=round(oi_delta_pct, 4),
            liq_volume_5m=liq_volume_5m,
            direction=direction,
        )

        if self._state == self.IDLE:
            await self._handle_idle(vpin, oi_delta_pct, liq_volume_5m, direction)

        elif self._state == self.CASCADE_DETECTED:
            await self._handle_cascade_detected(
                vpin, oi_delta_pct, liq_volume_5m, direction
            )

        elif self._state == self.EXHAUSTING:
            await self._handle_exhausting(vpin, oi_delta_pct, liq_volume_5m, direction)

        elif self._state == self.BET_SIGNAL:
            # Immediately transition to COOLDOWN after signal was emitted
            await self._transition(
                self.COOLDOWN, vpin, oi_delta_pct, liq_volume_5m, emit=False
            )
            self._cooldown_start = time.monotonic()

        elif self._state == self.COOLDOWN:
            await self._handle_cooldown(vpin, oi_delta_pct, liq_volume_5m)

        # Track previous liq volume for decline detection
        self._prev_liq_volume = liq_volume_5m

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------

    async def _handle_idle(
        self,
        vpin: float,
        oi_delta_pct: float,
        liq_volume_5m: float,
        direction: str,
    ) -> None:
        if (
            vpin >= _VPIN_CASCADE_ENTRY
            and abs(oi_delta_pct) >= _OI_DELTA_MIN
            and liq_volume_5m >= _LIQ_VOLUME_CASCADE
        ):
            self._direction = direction
            await self._transition(
                self.CASCADE_DETECTED, vpin, oi_delta_pct, liq_volume_5m, emit=True
            )

    async def _handle_cascade_detected(
        self,
        vpin: float,
        oi_delta_pct: float,
        liq_volume_5m: float,
        direction: str,
    ) -> None:
        # Update direction continuously while cascade is active
        self._direction = direction

        liq_declining = (
            self._prev_liq_volume is not None
            and liq_volume_5m < self._prev_liq_volume * _LIQ_DECLINE_RATIO
        )
        vpin_falling = vpin < _VPIN_CASCADE_ENTRY

        if liq_declining or vpin_falling:
            await self._transition(
                self.EXHAUSTING, vpin, oi_delta_pct, liq_volume_5m, emit=True
            )

    async def _handle_exhausting(
        self,
        vpin: float,
        oi_delta_pct: float,
        liq_volume_5m: float,
        direction: str,
    ) -> None:
        # Still update direction
        self._direction = direction

        if vpin < _VPIN_EXHAUSTION and liq_volume_5m < _LIQ_VOLUME_EXHAUSTION:
            await self._transition(
                self.BET_SIGNAL, vpin, oi_delta_pct, liq_volume_5m, emit=True
            )
            # Immediately move to COOLDOWN
            await self._transition(
                self.COOLDOWN, vpin, oi_delta_pct, liq_volume_5m, emit=False
            )
            self._cooldown_start = time.monotonic()

    async def _handle_cooldown(
        self,
        vpin: float,
        oi_delta_pct: float,
        liq_volume_5m: float,
    ) -> None:
        if self._cooldown_start is None:
            self._cooldown_start = time.monotonic()

        elapsed = time.monotonic() - self._cooldown_start
        if elapsed >= runtime.cooldown_seconds:
            self._log.info(
                "cooldown_expired",
                elapsed_s=round(elapsed, 1),
                cooldown_s=runtime.cooldown_seconds,
            )
            self._direction = None
            self._cooldown_start = None
            self._prev_liq_volume = None
            await self._transition(
                self.IDLE, vpin, oi_delta_pct, liq_volume_5m, emit=False
            )

    # ------------------------------------------------------------------
    # Transition helper
    # ------------------------------------------------------------------

    async def _transition(
        self,
        new_state: str,
        vpin: float,
        oi_delta_pct: float,
        liq_volume_5m: float,
        emit: bool,
    ) -> None:
        old_state = self._state
        self._state = new_state

        self._log.info(
            "state_transition",
            from_state=old_state,
            to_state=new_state,
            direction=self._direction,
            vpin=round(vpin, 4),
            oi_delta_pct=round(oi_delta_pct, 4),
            liq_volume_5m=liq_volume_5m,
        )

        if emit and self._on_signal is not None:
            from datetime import datetime, timezone

            signal = CascadeSignal(
                state=new_state,
                direction=self._direction,
                vpin=vpin,
                oi_delta_pct=oi_delta_pct,
                liq_volume_usd=liq_volume_5m,
                timestamp=datetime.now(tz=timezone.utc),
            )
            try:
                await self._on_signal(signal)
            except Exception:
                self._log.exception(
                    "on_signal callback raised", state=new_state
                )
