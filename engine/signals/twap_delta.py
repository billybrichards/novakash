"""
TWAP-Delta — Time-Weighted Average Price Delta for Direction Detection

Instead of a single-point delta at T-60s, this module collects price
samples throughout the 5-minute window and computes the time-weighted
average delta. This is more robust against:
  - Last-second spikes (one large trade distorts point delta)
  - Thin-book noise (price oscillates on low volume)
  - Window-open price being stale or mid-spread

Gamma Overlay:
  The Polymarket token prices (UP/DOWN) represent the market's real-time
  directional consensus. When TWAP-delta and Gamma agree on direction,
  confidence is higher. When they disagree, it's a warning signal.

Usage:
  tracker = TWAPTracker()
  tracker.start_window("BTC", window_ts, open_price)
  # On each price tick (every ~1-3s from Binance WS):
  tracker.add_tick("BTC", window_ts, current_price, timestamp)
  # At evaluation time:
  result = tracker.evaluate("BTC", window_ts, gamma_up_price, gamma_down_price)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import structlog

log = structlog.get_logger(__name__)


@dataclass
class TWAPResult:
    """Result of TWAP-delta evaluation for a single window."""

    # TWAP metrics
    twap_price: float = 0.0          # Time-weighted average price over window
    twap_delta_pct: float = 0.0      # TWAP delta vs open price (%)
    point_delta_pct: float = 0.0     # Current (point) delta vs open (%)
    twap_direction: str = ""         # "UP" or "DOWN" based on TWAP
    point_direction: str = ""        # "UP" or "DOWN" based on current price

    # Gamma overlay
    gamma_direction: str = ""        # "UP" or "DOWN" based on which token > 0.50
    gamma_up_price: float = 0.50     # Current UP token price
    gamma_down_price: float = 0.50   # Current DOWN token price
    gamma_skew: float = 0.0          # How far from 50/50 (0 = neutral, >0 = skewed)

    # Agreement
    all_agree: bool = False          # TWAP + point + Gamma all same direction
    twap_gamma_agree: bool = False   # TWAP and Gamma agree (strongest signal)
    twap_point_agree: bool = False   # TWAP and point delta agree
    agreement_score: int = 0         # 0-3: how many sources agree on direction

    # Quality metrics
    n_ticks: int = 0                 # Number of price samples collected
    window_coverage_pct: float = 0.0 # % of window duration covered by samples
    twap_stability: float = 0.0      # How stable TWAP direction was (0-1)
    trend_strength: float = 0.0      # Slope of TWAP over time (positive = trending)

    # Final recommendation
    recommended_direction: str = ""  # Final direction recommendation
    confidence_boost: float = 0.0    # Modifier to apply to base confidence

    def summary(self) -> str:
        """One-line summary for logging."""
        return (
            f"TWAP δ{self.twap_delta_pct:+.4f}%→{self.twap_direction} | "
            f"Point δ{self.point_delta_pct:+.4f}%→{self.point_direction} | "
            f"Gamma {self.gamma_up_price:.2f}/{self.gamma_down_price:.2f}→{self.gamma_direction} | "
            f"Agree: {self.agreement_score}/3 | "
            f"Dir: {self.recommended_direction} (boost {self.confidence_boost:+.2f})"
        )


@dataclass
class _PriceTick:
    """A single price observation with timestamp."""
    price: float
    timestamp: float  # Unix timestamp


@dataclass
class _WindowTracker:
    """Tracks price ticks for a single window."""
    asset: str
    window_ts: int
    open_price: float
    ticks: list[_PriceTick] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    window_duration_s: float = 300.0  # 5min default

    # Running TWAP state for efficiency
    _weighted_sum: float = 0.0
    _total_weight: float = 0.0
    _last_tick_time: float = 0.0

    # Direction stability tracking
    _direction_flips: int = 0
    _last_direction: str = ""


class TWAPTracker:
    """
    Tracks TWAP across multiple concurrent windows.

    Designed to be called from the orchestrator on every Binance WS tick
    and from the strategy at evaluation time.
    """

    def __init__(self, max_windows: int = 50) -> None:
        self._windows: dict[str, _WindowTracker] = {}  # key: "ASSET-window_ts"
        self._max_windows = max_windows
        self._log = log.bind(component="twap_tracker")

    def start_window(
        self,
        asset: str,
        window_ts: int,
        open_price: float,
        duration_s: float = 300.0,
    ) -> None:
        """Register a new window for TWAP tracking."""
        key = f"{asset}-{window_ts}"
        if key in self._windows:
            return  # Already tracking

        self._windows[key] = _WindowTracker(
            asset=asset,
            window_ts=window_ts,
            open_price=open_price,
            window_duration_s=duration_s,
        )

        # Prune old windows if we're over the limit
        if len(self._windows) > self._max_windows:
            self._prune_old_windows()

        self._log.debug(
            "twap.window_started",
            key=key,
            open_price=f"{open_price:.2f}",
            duration=f"{duration_s:.0f}s",
        )

    def add_tick(
        self,
        asset: str,
        window_ts: int,
        price: float,
        timestamp: Optional[float] = None,
    ) -> None:
        """
        Add a price tick for an active window.

        Called on every Binance WS trade/aggTrade for the relevant asset.
        Lightweight — designed for high-frequency calling (~10-50 ticks/sec).
        """
        key = f"{asset}-{window_ts}"
        tracker = self._windows.get(key)
        if tracker is None:
            return  # Not tracking this window

        ts = timestamp or time.time()
        tick = _PriceTick(price=price, timestamp=ts)
        tracker.ticks.append(tick)

        # Update running TWAP (incremental — O(1) per tick)
        if tracker._last_tick_time > 0:
            dt = ts - tracker._last_tick_time
            if dt > 0:
                # Weight by time duration since last tick
                tracker._weighted_sum += price * dt
                tracker._total_weight += dt
        else:
            # First tick after open — use small weight
            tracker._weighted_sum += price * 0.001
            tracker._total_weight += 0.001

        tracker._last_tick_time = ts

        # Track direction stability
        delta = price - tracker.open_price
        current_dir = "UP" if delta > 0 else "DOWN"
        if tracker._last_direction and current_dir != tracker._last_direction:
            tracker._direction_flips += 1
        tracker._last_direction = current_dir

    def evaluate(
        self,
        asset: str,
        window_ts: int,
        current_price: float,
        gamma_up_price: Optional[float] = None,
        gamma_down_price: Optional[float] = None,
    ) -> Optional[TWAPResult]:
        """
        Evaluate TWAP-delta with Gamma overlay for a window.

        Call this at evaluation time (T-60s or whenever the strategy decides).
        Returns TWAPResult with direction recommendation and confidence boost.
        """
        key = f"{asset}-{window_ts}"
        tracker = self._windows.get(key)

        if tracker is None or tracker.open_price <= 0:
            return None

        now = time.time()

        # ── Calculate TWAP ─────────────────────────────────────────────
        # Include current price in TWAP (add final tick)
        if tracker._last_tick_time > 0:
            dt = now - tracker._last_tick_time
            if dt > 0:
                weighted_sum = tracker._weighted_sum + current_price * dt
                total_weight = tracker._total_weight + dt
            else:
                weighted_sum = tracker._weighted_sum
                total_weight = tracker._total_weight
        else:
            weighted_sum = current_price
            total_weight = 1.0

        twap_price = weighted_sum / total_weight if total_weight > 0 else current_price

        # Deltas
        twap_delta_pct = (twap_price - tracker.open_price) / tracker.open_price * 100
        point_delta_pct = (current_price - tracker.open_price) / tracker.open_price * 100

        # Directions
        twap_direction = "UP" if twap_delta_pct > 0 else "DOWN"
        point_direction = "UP" if point_delta_pct > 0 else "DOWN"

        # ── Gamma Overlay ──────────────────────────────────────────────
        gamma_direction = ""
        gamma_skew = 0.0
        g_up = gamma_up_price or 0.50
        g_down = gamma_down_price or 0.50

        if gamma_up_price is not None and gamma_down_price is not None:
            if g_up > g_down:
                gamma_direction = "UP"
                gamma_skew = g_up - 0.50
            elif g_down > g_up:
                gamma_direction = "DOWN"
                gamma_skew = g_down - 0.50
            else:
                gamma_direction = twap_direction  # Neutral — follow TWAP
                gamma_skew = 0.0

        # ── Agreement Score ────────────────────────────────────────────
        directions = [twap_direction, point_direction]
        if gamma_direction:
            directions.append(gamma_direction)

        # Count agreement
        up_count = sum(1 for d in directions if d == "UP")
        down_count = sum(1 for d in directions if d == "DOWN")
        agreement_score = max(up_count, down_count)
        majority_direction = "UP" if up_count >= down_count else "DOWN"

        twap_point_agree = twap_direction == point_direction
        twap_gamma_agree = (gamma_direction == twap_direction) if gamma_direction else True
        all_agree = agreement_score == len(directions)

        # ── Quality Metrics ────────────────────────────────────────────
        n_ticks = len(tracker.ticks)
        elapsed = now - tracker.started_at
        window_coverage = min(elapsed / tracker.window_duration_s, 1.0) * 100

        # Stability: fewer direction flips = more stable signal
        # Normalise: 0 flips = 1.0 stability, 20+ flips = 0.0
        max_flips = max(n_ticks * 0.3, 20)  # Expect some noise
        stability = max(0.0, 1.0 - (tracker._direction_flips / max_flips))

        # Trend strength: is TWAP moving consistently in one direction?
        # Compare first-half TWAP vs second-half TWAP
        trend_strength = 0.0
        if n_ticks >= 10:
            mid = n_ticks // 2
            first_half = tracker.ticks[:mid]
            second_half = tracker.ticks[mid:]
            if first_half and second_half:
                avg_first = sum(t.price for t in first_half) / len(first_half)
                avg_second = sum(t.price for t in second_half) / len(second_half)
                trend_delta = (avg_second - avg_first) / tracker.open_price * 100
                # Positive trend_delta = price trending UP over window
                trend_strength = trend_delta

        # ── Confidence Boost ───────────────────────────────────────────
        # This modifier is ADDED to the base confidence from the existing evaluator
        confidence_boost = 0.0

        # 1. Agreement bonus: all sources agree → +0.10
        if all_agree and len(directions) >= 3:
            confidence_boost += 0.10
        elif all_agree and len(directions) == 2:
            confidence_boost += 0.05

        # 2. TWAP-Gamma agreement (strongest signal) → +0.08
        if twap_gamma_agree and gamma_direction:
            confidence_boost += 0.08

        # 3. Stability bonus: TWAP direction was consistent → +0.05
        if stability > 0.7:
            confidence_boost += 0.05

        # 4. Strong Gamma skew (token price far from 50¢) → +0.05
        if gamma_skew > 0.08:  # Token at 58¢+ in favoured direction
            confidence_boost += 0.05

        # 5. Trend confirmation: trend matches TWAP direction → +0.05
        if trend_strength != 0:
            trend_dir = "UP" if trend_strength > 0 else "DOWN"
            if trend_dir == twap_direction and abs(trend_strength) > 0.01:
                confidence_boost += 0.05

        # 6. Penalty: TWAP disagrees with point delta → -0.10
        if not twap_point_agree and abs(twap_delta_pct) > 0.01:
            confidence_boost -= 0.10

        # 7. Penalty: Gamma opposes TWAP → -0.08
        if gamma_direction and not twap_gamma_agree and gamma_skew > 0.05:
            confidence_boost -= 0.08

        # 8. Penalty: low stability (lots of direction flips) → -0.05
        if stability < 0.3:
            confidence_boost -= 0.05

        # Clamp boost
        confidence_boost = max(-0.20, min(0.20, confidence_boost))

        # ── Recommended Direction ──────────────────────────────────────
        # Priority: TWAP direction (smoothed signal), confirmed by agreement
        # If TWAP and point disagree: use TWAP (it's more robust)
        # If all disagree: use majority
        if all_agree:
            recommended_direction = majority_direction
        elif twap_gamma_agree and gamma_direction:
            recommended_direction = twap_direction  # TWAP + Gamma beats point
        else:
            recommended_direction = twap_direction  # Default to TWAP

        result = TWAPResult(
            twap_price=twap_price,
            twap_delta_pct=twap_delta_pct,
            point_delta_pct=point_delta_pct,
            twap_direction=twap_direction,
            point_direction=point_direction,
            gamma_direction=gamma_direction,
            gamma_up_price=g_up,
            gamma_down_price=g_down,
            gamma_skew=gamma_skew,
            all_agree=all_agree,
            twap_gamma_agree=twap_gamma_agree,
            twap_point_agree=twap_point_agree,
            agreement_score=agreement_score,
            n_ticks=n_ticks,
            window_coverage_pct=window_coverage,
            twap_stability=stability,
            trend_strength=trend_strength,
            recommended_direction=recommended_direction,
            confidence_boost=confidence_boost,
        )

        self._log.info(
            "twap.evaluated",
            key=key,
            twap_delta=f"{twap_delta_pct:+.4f}%",
            point_delta=f"{point_delta_pct:+.4f}%",
            twap_dir=twap_direction,
            gamma_dir=gamma_direction or "n/a",
            agree=f"{agreement_score}/{len(directions)}",
            stability=f"{stability:.2f}",
            ticks=n_ticks,
            coverage=f"{window_coverage:.0f}%",
            boost=f"{confidence_boost:+.2f}",
            recommended=recommended_direction,
        )

        return result

    def get_current_twap(self, asset: str, window_ts: int) -> Optional[float]:
        """Get current running TWAP price for a window (no evaluation, just the number)."""
        key = f"{asset}-{window_ts}"
        tracker = self._windows.get(key)
        if tracker is None or tracker._total_weight <= 0:
            return None
        return tracker._weighted_sum / tracker._total_weight

    def get_tick_count(self, asset: str, window_ts: int) -> int:
        """Get number of ticks collected for a window."""
        key = f"{asset}-{window_ts}"
        tracker = self._windows.get(key)
        return len(tracker.ticks) if tracker else 0

    def cleanup_window(self, asset: str, window_ts: int) -> None:
        """Remove a completed window's tracking data."""
        key = f"{asset}-{window_ts}"
        self._windows.pop(key, None)

    def _prune_old_windows(self) -> None:
        """Remove oldest windows when over the limit."""
        if len(self._windows) <= self._max_windows:
            return

        # Sort by start time, remove oldest
        sorted_keys = sorted(
            self._windows.keys(),
            key=lambda k: self._windows[k].started_at,
        )
        to_remove = len(self._windows) - self._max_windows
        for key in sorted_keys[:to_remove]:
            del self._windows[key]
            self._log.debug("twap.pruned", key=key)

    @property
    def active_windows(self) -> list[str]:
        """List of currently tracked window keys."""
        return list(self._windows.keys())

    def get_historical_accuracy(
        self,
        results: list[tuple[TWAPResult, str]],  # (result, actual_outcome "UP"/"DOWN")
    ) -> dict:
        """
        Compute historical TWAP accuracy from past results.

        Args:
            results: List of (TWAPResult, actual_outcome) tuples

        Returns:
            Dict with accuracy stats for TWAP vs point vs Gamma
        """
        if not results:
            return {"n": 0}

        n = len(results)
        twap_correct = sum(1 for r, o in results if r.twap_direction == o)
        point_correct = sum(1 for r, o in results if r.point_direction == o)
        gamma_correct = sum(
            1 for r, o in results
            if r.gamma_direction and r.gamma_direction == o
        )
        gamma_n = sum(1 for r, _ in results if r.gamma_direction)
        all_agree_correct = sum(
            1 for r, o in results
            if r.all_agree and r.recommended_direction == o
        )
        all_agree_n = sum(1 for r, _ in results if r.all_agree)

        return {
            "n": n,
            "twap_accuracy": twap_correct / n if n > 0 else 0,
            "point_accuracy": point_correct / n if n > 0 else 0,
            "gamma_accuracy": gamma_correct / gamma_n if gamma_n > 0 else 0,
            "gamma_n": gamma_n,
            "all_agree_accuracy": all_agree_correct / all_agree_n if all_agree_n > 0 else 0,
            "all_agree_n": all_agree_n,
            "twap_better_than_point": twap_correct > point_correct,
        }
