"""
Volatility Regime Classifier

Classifies the current BTC market regime to help strategies
adjust position sizing and filter signals.

Regimes:
  LOW_VOL    — ATR < 1.0%, VPIN < 0.45, trending or ranging
  NORMAL     — ATR 1.0–2.5%, VPIN 0.45–0.60, typical conditions
  HIGH_VOL   — ATR > 2.5%, VPIN 0.60–0.70, caution
  CASCADE    — VPIN > 0.70, large OI moves, forced liquidations active

In CASCADE regime: VPIN cascade strategy is active.
In LOW_VOL regime: Sub-$1 arb has the best edge (quieter flow).
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional
import structlog

from config.constants import VPIN_INFORMED_THRESHOLD, VPIN_CASCADE_THRESHOLD

log = structlog.get_logger(__name__)


class VolRegime(str, Enum):
    LOW_VOL = "LOW_VOL"
    NORMAL = "NORMAL"
    HIGH_VOL = "HIGH_VOL"
    CASCADE = "CASCADE"


class RegimeClassifier:
    """
    Classifies current market regime using:
      - VPIN value (from VPINCalculator)
      - ATR (Average True Range, computed from recent BTC prices)
      - OI delta percentage

    Designed to be called on every VPINSignal update.
    """

    ATR_PERIOD = 14
    ATR_LOW_THRESHOLD = 0.01   # 1%
    ATR_HIGH_THRESHOLD = 0.025  # 2.5%

    def __init__(self) -> None:
        self._prices: deque[Decimal] = deque(maxlen=self.ATR_PERIOD + 1)
        self._current_regime = VolRegime.NORMAL
        self._vpin: float = 0.0
        self._oi_delta_pct: float = 0.0
        self._last_classified: Optional[datetime] = None

    def update_price(self, price: Decimal) -> None:
        """Add new BTC price observation."""
        self._prices.append(price)

    def update_vpin(self, vpin: float) -> None:
        """Update VPIN value."""
        self._vpin = vpin

    def update_oi_delta(self, delta_pct: float) -> None:
        """Update open interest delta."""
        self._oi_delta_pct = delta_pct

    def classify(self) -> VolRegime:
        """
        Run classification with current inputs.
        Returns the new regime and updates internal state.
        """
        atr_pct = self._compute_atr_pct()

        if self._vpin >= VPIN_CASCADE_THRESHOLD and abs(self._oi_delta_pct) >= 0.02:
            regime = VolRegime.CASCADE
        elif self._vpin >= VPIN_INFORMED_THRESHOLD or (atr_pct and atr_pct > self.ATR_HIGH_THRESHOLD):
            regime = VolRegime.HIGH_VOL
        elif atr_pct and atr_pct < self.ATR_LOW_THRESHOLD and self._vpin < 0.45:
            regime = VolRegime.LOW_VOL
        else:
            regime = VolRegime.NORMAL

        if regime != self._current_regime:
            log.info("regime.changed", from_=self._current_regime, to=regime, vpin=self._vpin)

        self._current_regime = regime
        self._last_classified = datetime.utcnow()
        return regime

    def _compute_atr_pct(self) -> Optional[float]:
        """Compute simple ATR as % of price using high-low range proxy."""
        if len(self._prices) < 2:
            return None

        prices = list(self._prices)
        ranges = [
            abs(float(prices[i] - prices[i - 1])) / float(prices[i - 1])
            for i in range(1, len(prices))
        ]
        return sum(ranges) / len(ranges)

    @property
    def current_regime(self) -> VolRegime:
        return self._current_regime

    def is_favorable_for_arb(self) -> bool:
        """True when regime is LOW_VOL or NORMAL — best for sub-$1 arb."""
        return self._current_regime in (VolRegime.LOW_VOL, VolRegime.NORMAL)

    def is_favorable_for_cascade(self) -> bool:
        """True when in CASCADE regime — VPIN strategy is active."""
        return self._current_regime == VolRegime.CASCADE
