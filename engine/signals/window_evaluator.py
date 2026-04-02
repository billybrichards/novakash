"""
Window Evaluator — Full-Window Continuous Signal Monitoring

Replaces fixed-time single-shot entry with continuous monitoring
from window open (T-300s) to deadline (T-5s).

Fires when confidence crosses the threshold for the current time tier:
  - DECISIVE (any time): delta >0.10% + VPIN >0.50 → fire immediately
  - HIGH (T-120s+):      delta >0.05% + confirming signal → fire
  - MODERATE (T-30s+):   delta >0.02% → fire
  - DEADLINE (T-5s):     use best signal seen → fire regardless

Signal Components (weighted composite):
  1. Window Delta          weight 5-7  (from Binance aggTrade)
  2. VPIN                  weight 2-3  (from VPINCalculator)
  3. Liquidation surge     weight 2    (from CoinGlass 1m)
  4. Long/Short imbalance  weight 1.5  (from CoinGlass 1m)
  5. Funding rate bias     weight 1    (from CoinGlass)
  6. OI delta              weight 1    (from CoinGlass 1m)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import structlog

log = structlog.get_logger(__name__)


@dataclass
class WindowSignal:
    """Composite signal for a 5-min window evaluation."""
    direction: str = ""            # "UP" or "DOWN"
    score: float = 0.0             # Raw weighted score (positive=UP, negative=DOWN)
    confidence: float = 0.0        # Normalised 0-1
    tier: str = "NONE"             # DECISIVE / HIGH / MODERATE / LOW / NONE

    # Components
    delta_pct: float = 0.0         # Window delta %
    delta_weight: float = 0.0
    vpin: float = 0.0
    vpin_weight: float = 0.0
    liq_surge_weight: float = 0.0  # Liquidation signal
    ls_imbalance_weight: float = 0.0  # Long/short ratio signal
    funding_weight: float = 0.0
    oi_delta_weight: float = 0.0

    # Timing
    seconds_to_close: float = 300  # How many seconds left in window
    entry_reason: str = ""         # Why we fired

    # Token pricing (delta-based)
    estimated_token_price: float = 0.50


@dataclass
class WindowState:
    """Tracks the monitoring state for a single 5-min window."""
    window_ts: int = 0
    open_price: float = 0.0
    best_signal: Optional[WindowSignal] = None
    fired: bool = False
    eval_count: int = 0
    started_at: float = field(default_factory=time.time)


class WindowEvaluator:
    """
    Continuously evaluates signals during a 5-minute trading window.

    Called every ~2-3 seconds by the strategy with the latest market data.
    Returns a WindowSignal when confidence is high enough for the current
    time tier, or None if we should wait.
    """

    # ── Confidence tiers by time remaining ────────────────────────────────────
    # Earlier = need higher confidence (but get cheaper tokens)
    # Later = lower confidence OK (but tokens are expensive)
    # v3.1 TIERS — matched to ACTUAL morning winning thresholds
    # Morning wins: delta 0.03-0.09%, MODERATE conf, VPIN 0.65-0.93
    # The edge is in MODERATE signals, not just the big deltas
    TIERS = [
        # (min_seconds_remaining, min_confidence, tier_name)
        (180, 0.85, "DECISIVE"),    # T-300s to T-180s: high bar early
        (60,  0.65, "HIGH"),        # T-180s to T-60s: good signal
        (10,  0.50, "MODERATE"),    # T-60s to T-10s: morning's sweet spot
    ]

    def __init__(self) -> None:
        self._log = log.bind(component="window_evaluator")

    def evaluate(
        self,
        window_state: WindowState,
        current_price: float,
        current_vpin: float,
        seconds_to_close: float,
        # CoinGlass enhanced data (optional)
        liq_total_1m: float = 0.0,
        liq_long_1m: float = 0.0,
        liq_short_1m: float = 0.0,
        long_short_ratio: float = 1.0,
        long_pct: float = 50.0,
        funding_rate: float = 0.0,
        oi_delta_pct_1m: float = 0.0,
    ) -> Optional[WindowSignal]:
        """
        Evaluate the composite signal for this window.

        Returns WindowSignal if we should fire now, None if we should wait.
        """
        if window_state.fired:
            return None

        if window_state.open_price <= 0 or current_price <= 0:
            return None

        window_state.eval_count += 1

        # ── 1. Window Delta (THE dominant signal) ─────────────────────────
        delta_pct = (current_price - window_state.open_price) / window_state.open_price * 100
        abs_delta = abs(delta_pct)

        # Dynamic weight: stronger delta → higher weight
        if abs_delta > 0.10:
            delta_weight = 7.0
        elif abs_delta > 0.05:
            delta_weight = 5.0
        elif abs_delta > 0.02:
            delta_weight = 3.0
        elif abs_delta > 0.005:
            delta_weight = 1.0
        else:
            delta_weight = 0.0

        direction = "UP" if delta_pct > 0 else "DOWN"
        score = delta_weight if delta_pct > 0 else -delta_weight

        # ── 2. VPIN Signal ────────────────────────────────────────────────
        vpin_weight = 0.0
        if current_vpin > 0.50:
            # High VPIN = informed trading detected
            # Confirms direction if delta agrees, warns if disagrees
            vpin_weight = min((current_vpin - 0.30) * 10, 3.0)  # 0-3 weight
            # VPIN doesn't have direction by itself — it amplifies delta
            score += vpin_weight if delta_pct > 0 else -vpin_weight

        # ── 3. Liquidation Surge (CoinGlass) ──────────────────────────────
        liq_weight = 0.0
        if liq_total_1m > 500_000:  # $500K+ liquidations in last minute
            # Large liquidations = forced selling/buying = directional signal
            if liq_long_1m > liq_short_1m * 2:
                # Longs getting liquidated → price going DOWN
                liq_weight = -min(liq_total_1m / 2_000_000, 2.0)
            elif liq_short_1m > liq_long_1m * 2:
                # Shorts getting liquidated → price going UP
                liq_weight = min(liq_total_1m / 2_000_000, 2.0)
            score += liq_weight

        # ── 4. Long/Short Imbalance ───────────────────────────────────────
        ls_weight = 0.0
        if long_pct > 65:
            # Crowd is heavily long → contrarian DOWN signal (overleveraged)
            ls_weight = -min((long_pct - 50) / 30, 1.5)
            score += ls_weight
        elif long_pct < 35:
            # Crowd is heavily short → contrarian UP signal
            ls_weight = min((50 - long_pct) / 30, 1.5)
            score += ls_weight

        # ── 5. Funding Rate Bias ──────────────────────────────────────────
        funding_weight = 0.0
        if abs(funding_rate) > 0.0003:  # Extreme funding (>0.03%)
            if funding_rate > 0:
                # Positive funding = longs paying shorts = DOWN pressure
                funding_weight = -min(funding_rate / 0.001, 1.0)
            else:
                # Negative funding = shorts paying longs = UP pressure
                funding_weight = min(abs(funding_rate) / 0.001, 1.0)
            score += funding_weight

        # ── 6. OI Delta ──────────────────────────────────────────────────
        oi_weight = 0.0
        if abs(oi_delta_pct_1m) > 0.005:  # >0.5% OI change in 1 minute
            # Rising OI + rising price = new longs = UP confirmed
            # Rising OI + falling price = new shorts = DOWN confirmed
            # Falling OI = position closing = less conviction
            if oi_delta_pct_1m > 0:
                oi_weight = 1.0 if delta_pct > 0 else -0.5
            else:
                oi_weight = -0.5 if delta_pct > 0 else 0.5
            score += oi_weight

        # ── Composite Confidence ──────────────────────────────────────────
        # v3: Dynamic normalisation — only count ACTIVE components
        # CoinGlass was returning all zeros → inflated confidence
        active_max = 3.0  # delta always active (max 3.0)
        if vpin_weight != 0: active_max += 3.0
        if liq_weight != 0: active_max += 2.0
        if ls_weight != 0: active_max += 1.5
        if funding_weight != 0: active_max += 1.0
        if oi_weight != 0: active_max += 1.0
        active_max = max(active_max, 5.0)  # floor at 5.0
        
        confidence = min(abs(score) / active_max, 0.95)  # NEVER 100%

        # ── Token Price Estimate ──────────────────────────────────────────
        token_price = self._delta_to_token_price(abs_delta)

        # ── Build Signal ──────────────────────────────────────────────────
        signal = WindowSignal(
            direction=direction,
            score=score,
            confidence=confidence,
            delta_pct=delta_pct,
            delta_weight=delta_weight,
            vpin=current_vpin,
            vpin_weight=vpin_weight,
            liq_surge_weight=liq_weight,
            ls_imbalance_weight=ls_weight,
            funding_weight=funding_weight,
            oi_delta_weight=oi_weight,
            seconds_to_close=seconds_to_close,
            estimated_token_price=token_price,
        )

        # Track best signal seen
        if (window_state.best_signal is None or
                confidence > window_state.best_signal.confidence):
            window_state.best_signal = signal

        # ── Check if we should fire ───────────────────────────────────────
        # v3.1 GATES — matched to morning winning session:
        #   1. VPIN >= 0.50 (morning range was 0.58-0.94)
        #   2. |delta| >= 0.02% (morning minimum was ~0.03%)
        #   3. Confidence meets tier threshold
        
        if current_vpin < 0.50:
            return None  # No informed flow = no trade
        
        if abs_delta < 0.02:
            return None  # Below morning's minimum edge
        
        for min_secs, min_conf, tier_name in self.TIERS:
            if seconds_to_close >= min_secs:
                if confidence >= min_conf:
                    signal.tier = tier_name
                    signal.entry_reason = (
                        f"{tier_name} at T-{seconds_to_close:.0f}s: "
                        f"delta={delta_pct:+.4f}%, conf={confidence:.2f}, "
                        f"token~${token_price:.2f}"
                    )
                    self._log.info(
                        "window_eval.fire",
                        direction=direction,
                        tier=tier_name,
                        confidence=f"{confidence:.2f}",
                        delta_pct=f"{delta_pct:+.4f}%",
                        score=f"{score:.2f}",
                        seconds_to_close=f"{seconds_to_close:.0f}",
                        token_price=f"${token_price:.2f}",
                        eval_count=window_state.eval_count,
                    )
                    return signal
                break  # Only check the applicable tier

        # v3: NO DEADLINE fire — don't trade weak signals at the last second
        # The morning system never did this and had 73% win rate

        # ── Spike Detection ───────────────────────────────────────────────
        # v3: Tightened — spike requires strong delta AND VPIN confirmation
        if window_state.best_signal and window_state.eval_count > 5:
            prev_conf = window_state.best_signal.confidence
            if (confidence - prev_conf >= 0.30 and confidence >= 0.65 
                    and abs_delta >= 0.05 and current_vpin >= 0.50):
                signal.tier = "SPIKE"
                signal.entry_reason = (
                    f"SPIKE at T-{seconds_to_close:.0f}s: "
                    f"conf jumped {prev_conf:.2f}→{confidence:.2f}"
                )
                self._log.info(
                    "window_eval.spike_fire",
                    direction=direction,
                    confidence_jump=f"{prev_conf:.2f}→{confidence:.2f}",
                    seconds_to_close=f"{seconds_to_close:.0f}",
                )
                return signal

        # Not ready yet — log periodically
        if window_state.eval_count % 10 == 0:
            self._log.debug(
                "window_eval.waiting",
                direction=direction,
                confidence=f"{confidence:.2f}",
                delta_pct=f"{delta_pct:+.4f}%",
                seconds_to_close=f"{seconds_to_close:.0f}",
                eval_count=window_state.eval_count,
            )

        return None

    @staticmethod
    def _delta_to_token_price(abs_delta_pct: float) -> float:
        """
        Estimate token price based on window delta.
        Matches observed Polymarket pricing behaviour.
        """
        d = abs_delta_pct
        if d < 0.005:
            return 0.50
        elif d < 0.02:
            return 0.50 + (d - 0.005) / (0.02 - 0.005) * 0.05   # 0.50-0.55
        elif d < 0.05:
            return 0.55 + (d - 0.02) / (0.05 - 0.02) * 0.10     # 0.55-0.65
        elif d < 0.10:
            return 0.65 + (d - 0.05) / (0.10 - 0.05) * 0.15     # 0.65-0.80
        elif d < 0.15:
            return 0.80 + (d - 0.10) / (0.15 - 0.10) * 0.12     # 0.80-0.92
        else:
            return min(0.92 + (d - 0.15) / 0.10 * 0.05, 0.97)   # 0.92-0.97
