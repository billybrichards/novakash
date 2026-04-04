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

    # Trend consistency (what % of samples were on the winning side)
    trend_pct: float = 0.50          # 0-1: fraction of ticks above open (0.9 = 90% UP)
    trend_consistent: bool = False   # True if trend_pct > 0.6 (UP) or < 0.4 (DOWN)
    trend_mixed: bool = False        # True if 0.4 <= trend_pct <= 0.6 (unclear)

    # Recent momentum (last ~30 seconds)
    momentum_pct: float = 0.0       # Price change over last ~30s (%)
    momentum_direction: str = ""     # "UP" or "DOWN" based on recent momentum

    # Gamma overlay
    gamma_direction: str = ""        # "UP" or "DOWN" based on which token > 0.50
    gamma_up_price: float = 0.50     # Current UP token price
    gamma_down_price: float = 0.50   # Current DOWN token price
    gamma_skew: float = 0.0          # How far from 50/50 (0 = neutral, >0 = skewed)
    gamma_gate: str = "OK"           # "BLOCK" / "SKIP" / "REDUCE" / "OK" / "PRICED_IN"

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
    should_skip: bool = False        # True if signal is mixed/unclear → don't trade
    skip_reason: str = ""            # Why we recommend skipping
    confidence_boost: float = 0.0    # Modifier to apply to base confidence

    def summary(self) -> str:
        """One-line summary for logging."""
        skip_tag = f" ⛔ {self.skip_reason}" if self.should_skip else ""
        return (
            f"TWAP δ{self.twap_delta_pct:+.4f}%→{self.twap_direction} | "
            f"Point δ{self.point_delta_pct:+.4f}%→{self.point_direction} | "
            f"Trend {self.trend_pct:.0%} | Mom {self.momentum_pct:+.3f}%→{self.momentum_direction} | "
            f"Gamma {self.gamma_up_price:.2f}/{self.gamma_down_price:.2f}→{self.gamma_direction} [{self.gamma_gate}] | "
            f"Agree: {self.agreement_score}/3 | "
            f"Dir: {self.recommended_direction} (boost {self.confidence_boost:+.2f}){skip_tag}"
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

        # ── Trend Consistency ───────────────────────────────────────────
        # What % of the window was price above open?
        # 0.9 = 90% of ticks above open → strong UP trend
        # 0.1 = 90% below open → strong DOWN trend
        # 0.5 = mixed, no clear direction
        n_ticks = len(tracker.ticks)
        if n_ticks > 0:
            above_open = sum(1 for t in tracker.ticks if t.price > tracker.open_price)
            trend_pct = above_open / n_ticks
        else:
            trend_pct = 0.5

        trend_consistent = trend_pct > 0.60 or trend_pct < 0.40
        trend_mixed = 0.40 <= trend_pct <= 0.60

        # ── Recent Momentum (last ~30 seconds) ────────────────────────
        # How is price moving RIGHT NOW vs ~30s ago?
        # Catches "was UP all window, just dipped" scenarios
        momentum_pct = 0.0
        momentum_direction = point_direction
        if n_ticks >= 4:
            # Look back ~4 ticks (at ~10s interval = ~30-40s)
            lookback = min(4, n_ticks - 1)
            recent_price = tracker.ticks[-1].price
            past_price = tracker.ticks[-(lookback + 1)].price
            if past_price > 0:
                momentum_pct = (recent_price - past_price) / past_price * 100
                momentum_direction = "UP" if momentum_pct > 0 else "DOWN"

        # ── Gamma Overlay ──────────────────────────────────────────────
        gamma_direction = ""
        gamma_skew = 0.0
        gamma_gate = "OK"
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
                gamma_direction = twap_direction
                gamma_skew = 0.0

            # Gamma gate: check if market agrees with our TWAP direction
            # Our token = UP token if TWAP says UP, DOWN token if TWAP says DOWN
            our_gamma = g_up if twap_direction == "UP" else g_down
            if our_gamma < 0.15:
                gamma_gate = "BLOCK"   # Market STRONGLY disagrees (<15¢)
            elif our_gamma < 0.25:
                gamma_gate = "SKIP"    # Market leans against us (15-25¢)
            elif our_gamma < 0.40:
                gamma_gate = "REDUCE"  # Mild disagreement (25-40¢)
            elif our_gamma <= 0.60:
                gamma_gate = "OK"      # Sweet spot (40-60¢)
            else:
                gamma_gate = "PRICED_IN"  # Already >60¢ = bad R/R

        # ── Agreement Score ────────────────────────────────────────────
        directions = [twap_direction, point_direction]
        if gamma_direction:
            directions.append(gamma_direction)

        up_count = sum(1 for d in directions if d == "UP")
        down_count = sum(1 for d in directions if d == "DOWN")
        agreement_score = max(up_count, down_count)
        majority_direction = "UP" if up_count >= down_count else "DOWN"

        twap_point_agree = twap_direction == point_direction
        twap_gamma_agree = (gamma_direction == twap_direction) if gamma_direction else True
        all_agree = agreement_score == len(directions)

        # ── Quality Metrics ────────────────────────────────────────────
        elapsed = now - tracker.started_at
        window_coverage = min(elapsed / tracker.window_duration_s, 1.0) * 100

        max_flips = max(n_ticks * 0.3, 20)
        stability = max(0.0, 1.0 - (tracker._direction_flips / max_flips))

        trend_strength = 0.0
        if n_ticks >= 10:
            mid = n_ticks // 2
            first_half = tracker.ticks[:mid]
            second_half = tracker.ticks[mid:]
            if first_half and second_half:
                avg_first = sum(t.price for t in first_half) / len(first_half)
                avg_second = sum(t.price for t in second_half) / len(second_half)
                trend_strength = (avg_second - avg_first) / tracker.open_price * 100

        # ── Should Skip? ──────────────────────────────────────────────
        should_skip = False
        skip_reason = ""

        if gamma_gate == "BLOCK":
            should_skip = True
            skip_reason = f"Gamma BLOCK: market strongly disagrees (our token <15¢)"
        elif gamma_gate == "SKIP":
            should_skip = True
            skip_reason = f"Gamma SKIP: market leans against us (our token 15-25¢)"
        elif gamma_gate == "PRICED_IN":
            should_skip = True
            skip_reason = f"Gamma PRICED_IN: already >60¢, bad R/R"
        elif trend_mixed and not all_agree:
            should_skip = True
            skip_reason = f"Mixed signal: trend_pct={trend_pct:.2f} (40-60%), sources disagree"

        # ── Confidence Boost ───────────────────────────────────────────
        confidence_boost = 0.0

        if should_skip:
            confidence_boost = -0.20  # Max penalty
        else:
            # 1. All agree + consistent trend → strong boost
            if all_agree and trend_consistent:
                confidence_boost += 0.15
            elif all_agree:
                confidence_boost += 0.10

            # 2. TWAP-Gamma agreement
            if twap_gamma_agree and gamma_direction:
                confidence_boost += 0.05

            # 3. High trend consistency (>70% on one side)
            if trend_pct > 0.70 or trend_pct < 0.30:
                confidence_boost += 0.05

            # 4. Momentum confirms direction
            if momentum_direction == twap_direction and abs(momentum_pct) > 0.005:
                confidence_boost += 0.03

            # 5. Gamma in sweet spot (40-60¢)
            if gamma_gate == "OK":
                confidence_boost += 0.02

            # Penalties
            # 6. TWAP disagrees with point delta
            if not twap_point_agree and abs(twap_delta_pct) > 0.01:
                confidence_boost -= 0.08

            # 7. Gamma reduced confidence
            if gamma_gate == "REDUCE":
                confidence_boost -= 0.05

            # 8. Low stability
            if stability < 0.3:
                confidence_boost -= 0.05

            # 9. Momentum opposes TWAP (dipping at evaluation time)
            if momentum_direction != twap_direction and abs(momentum_pct) > 0.01:
                confidence_boost -= 0.08

        confidence_boost = max(-0.20, min(0.25, confidence_boost))

        # ── Recommended Direction ──────────────────────────────────────
        # Use TWAP + trend consistency as primary signal
        # Point delta is noisy at T-60s, TWAP is smoothed
        if should_skip:
            recommended_direction = twap_direction  # Still set, but should_skip=True
        elif all_agree and trend_consistent:
            recommended_direction = majority_direction
        elif twap_gamma_agree and trend_consistent:
            recommended_direction = twap_direction
        elif trend_consistent:
            # Trend says one direction clearly — use it
            recommended_direction = "UP" if trend_pct > 0.60 else "DOWN"
        else:
            recommended_direction = twap_direction

        result = TWAPResult(
            twap_price=twap_price,
            twap_delta_pct=twap_delta_pct,
            point_delta_pct=point_delta_pct,
            twap_direction=twap_direction,
            point_direction=point_direction,
            trend_pct=trend_pct,
            trend_consistent=trend_consistent,
            trend_mixed=trend_mixed,
            momentum_pct=momentum_pct,
            momentum_direction=momentum_direction,
            gamma_direction=gamma_direction,
            gamma_up_price=g_up,
            gamma_down_price=g_down,
            gamma_skew=gamma_skew,
            gamma_gate=gamma_gate,
            all_agree=all_agree,
            twap_gamma_agree=twap_gamma_agree,
            twap_point_agree=twap_point_agree,
            agreement_score=agreement_score,
            n_ticks=n_ticks,
            window_coverage_pct=window_coverage,
            twap_stability=stability,
            trend_strength=trend_strength,
            recommended_direction=recommended_direction,
            should_skip=should_skip,
            skip_reason=skip_reason,
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
