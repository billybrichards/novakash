"""
Regime Classifier — classifies the current BTC market regime based on
realised volatility and price-return directionality.

Regimes
-------
LOW_VOL   : annualised 5-min realised vol < 0.5 %
NORMAL    : 0.5 % ≤ vol < 2.0 %
HIGH_VOL  : vol ≥ 2.0 %
TRENDING  : 80 %+ of recent log-returns are the same sign (overrides vol label)

Volatility is computed as:
    σ = std(log_returns) * sqrt(12)   # 12 five-second periods per minute → annualised per minute

The deque holds the last 120 prices (~10 minutes at 5-second sampling).
"""

from __future__ import annotations

import math
from collections import deque
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

# Volatility regime thresholds (as fractions, not percentages)
_LOW_VOL_THRESHOLD: float = 0.005    # 0.5 %
_HIGH_VOL_THRESHOLD: float = 0.020   # 2.0 %

# Trend detection: fraction of returns that must share a direction
_TREND_THRESHOLD: float = 0.80
# Hysteresis: require N consecutive regime changes before switching
_HYSTERESIS_COUNT: int = 2  # Require 2 consecutive flips to change regime

# Annualisation factor: sqrt(number of 5-second periods per minute)
# 60 s / 5 s = 12 periods per minute
_ANNUALISE_FACTOR: float = math.sqrt(12)

_HISTORY_MAXLEN: int = 120  # 120 prices ≈ 10 minutes at 5 s intervals


class RegimeClassifier:
    """
    Classifies the market regime from a rolling window of BTC prices.

    Parameters
    ----------
    history_maxlen:
        Number of price observations to retain.  Defaults to 60 (~5 min).
    """

    LOW_VOL = "LOW_VOL"
    NORMAL = "NORMAL"
    HIGH_VOL = "HIGH_VOL"
    TRENDING = "TRENDING"

    def __init__(self, history_maxlen: int = _HISTORY_MAXLEN) -> None:
        self._prices: deque[float] = deque(maxlen=history_maxlen)
        self._current_regime: str = self.NORMAL
        self._current_vol: float = 0.0
        self._consecutive_flips: int = 0  # Track consecutive regime flips
        self._pending_regime: Optional[str] = None  # Pending regime change

        self._log = log.bind(component="RegimeClassifier")
        self._log.info("initialised", history_maxlen=history_maxlen)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def on_price(self, price: float) -> None:
        """
        Ingest the latest BTC price and recompute the regime.

        Parameters
        ----------
        price:
            Current BTC spot/perp price in USD.
        """
        if price <= 0:
            self._log.warning("invalid_price_ignored", price=price)
            return

        self._prices.append(price)

        if len(self._prices) < 2:
            # Not enough data yet
            return

        regime, vol = self._classify()
        changed = self._check_regime_change(regime)
        if changed:
            self._current_regime = regime
            self._current_vol = vol
            self._consecutive_flips = 0
            self._pending_regime = None
        else:
            self._current_vol = vol
            if self._pending_regime == regime:
                self._consecutive_flips += 1
            else:
                self._consecutive_flips = 1
                self._pending_regime = regime
            # Log the regime but not a change
            self._log.debug(
                "regime_update",
                regime=regime,
                vol_pct=round(vol * 100, 4),
                consecutive_flips=self._consecutive_flips,
            )
            return  # No regime change logged


    @property
    def current_regime(self) -> str:
        """Current market regime string."""
        return self._current_regime

    @property
    def current_vol(self) -> float:
        """
        Most-recently computed 5-minute realised volatility as a fraction
        (e.g. 0.012 = 1.2 %).  Returns 0.0 until there are at least 2 prices.
        """
        return self._current_vol

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log_returns(self) -> list[float]:
        """Compute log returns from the current price history."""
        prices = list(self._prices)
        return [
            math.log(prices[i] / prices[i - 1])
            for i in range(1, len(prices))
        ]

    def _classify(self) -> tuple[str, float]:
        """
        Compute realised vol and classify the regime.

        Returns
        -------
        tuple[str, float]:
            (regime_label, realised_vol_fraction)
        """
        returns = self._log_returns()
        if not returns:
            return self.NORMAL, 0.0

        n = len(returns)
        mean_r = sum(returns) / n
        variance = sum((r - mean_r) ** 2 for r in returns) / n
        std_r = math.sqrt(variance) if variance > 0 else 0.0

        # Annualise to per-minute scale
        vol = std_r * _ANNUALISE_FACTOR

        # Trend detection — are 80%+ of returns the same sign?
        positive = sum(1 for r in returns if r > 0)
        negative = sum(1 for r in returns if r < 0)
        dominant_fraction = max(positive, negative) / n if n > 0 else 0.0
        is_trending = dominant_fraction >= _TREND_THRESHOLD and n >= 5

        if is_trending:
            regime = self.TRENDING
        elif vol >= _HIGH_VOL_THRESHOLD:
            regime = self.HIGH_VOL
        elif vol >= _LOW_VOL_THRESHOLD:
            regime = self.NORMAL
        else:
            regime = self.LOW_VOL

        return regime, vol

    def _check_regime_change(self, new_regime: str) -> bool:
        """Check if regime change should be committed (with hysteresis)."""
        if new_regime == self._current_regime:
            self._consecutive_flips = 0
            self._pending_regime = None
            return False
        
        # Different regime - check hysteresis
        if self._pending_regime == new_regime:
            # Same pending regime, increment counter
            self._consecutive_flips += 1
        else:
            # New pending regime
            self._consecutive_flips = 1
            self._pending_regime = new_regime
        
        # Commit if we have enough consecutive flips
        return self._consecutive_flips >= _HYSTERESIS_COUNT
