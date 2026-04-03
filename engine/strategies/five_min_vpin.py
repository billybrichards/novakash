"""
5-Minute VPIN Strategy — Full-Window Continuous Monitor

Monitors the FULL 5-minute window from open to close.
Fires when composite signal confidence meets the time-tier threshold:
  - DECISIVE (any time): very high confidence → fire early, cheap tokens
  - HIGH (T-120s+): good confidence → fire with decent pricing
  - MODERATE (T-30s+): moderate confidence → fire with standard pricing
  - DEADLINE (T-5s): use best signal seen → fire regardless

Signal components:
  1. Window Delta (weight 5-7)  — primary
  2. VPIN (weight 2-3)          — informed flow detection
  3. Liquidation surge (wt 2)   — CoinGlass 1m (if available)
  4. Long/Short ratio (wt 1.5)  — CoinGlass 1m (if available)
  5. Funding rate (wt 1)        — CoinGlass (if available)
  6. OI delta (wt 1)            — CoinGlass 1m (if available)
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Callable, Awaitable

import structlog

from config.constants import FIVE_MIN_ENTRY_OFFSET
from config.runtime_config import runtime
from data.models import MarketState
from data.feeds.polymarket_5min import WindowInfo, WindowState
from execution.order_manager import Order, OrderManager, OrderStatus
from execution.polymarket_client import PolymarketClient
from execution.risk_manager import RiskManager
from signals.vpin import VPINCalculator
from signals.window_evaluator import WindowEvaluator, WindowState as EvalWindowState, WindowSignal
from strategies.base import BaseStrategy

log = structlog.get_logger(__name__)


@dataclass
class FiveMinSignal:
    """Signal for 5-minute trading decision."""
    window: WindowInfo
    current_price: float
    current_vpin: float
    delta_pct: float
    confidence: str  # "HIGH", "MODERATE", "LOW"
    direction: str   # "UP" or "DOWN"
    cg_modifier: float = 0.0  # CoinGlass confidence modifier applied


class FiveMinVPINStrategy(BaseStrategy):
    """
    5-Minute VPIN Strategy for Polymarket Up/Down markets.
    
    Waits for the T-10s signal from the market discovery feed, then evaluates
    the window delta combined with VPIN to make trading decisions.
    """

    def __init__(
        self,
        order_manager: OrderManager,
        risk_manager: RiskManager,
        poly_client: PolymarketClient,
        vpin_calculator: VPINCalculator,
        alerter=None,
        cg_enhanced=None,
        db_client=None,
        on_window_signal: Optional[Callable[[WindowInfo], Awaitable[None]]] = None,
    ) -> None:
        super().__init__(
            name="five_min_vpin",
            order_manager=order_manager,
            risk_manager=risk_manager,
        )
        self._poly = poly_client
        self._vpin = vpin_calculator
        self._alerter = alerter
        self._cg_enhanced = cg_enhanced  # CoinGlassEnhancedFeed (optional)
        self._db = db_client            # DBClient for window snapshot persistence (optional)
        self._evaluator = WindowEvaluator()
        
        # Track last executed window to avoid duplicates
        self._last_executed_window: Optional[str] = None
        
        # Active window monitoring state (one per window)
        self._active_eval_states: dict[str, EvalWindowState] = {}
        
        # Window info buffer (populated by feed callbacks)
        self._pending_windows: list = []  # Queue of windows to evaluate (multi-asset)
        
        self._log = log.bind(strategy="five_min_vpin")
        
        # Setup window signal callback
        if on_window_signal:
            self._on_window_signal = on_window_signal
        else:
            # Default: store window for evaluation
            self._on_window_signal = self._default_window_handler

    async def _default_window_handler(self, window: WindowInfo) -> None:
        """Default window signal handler - stores window for evaluation."""
        self._pending_windows.append(window)
        # Keep recent windows for token ID lookup
        if not hasattr(self, '_recent_windows'):
            self._recent_windows = []
        self._recent_windows.append(window)
        # Only keep last 10 windows
        if len(self._recent_windows) > 10:
            self._recent_windows = self._recent_windows[-10:]
        self._log.info(
            "window.signal.received",
            asset=window.asset,
            window_ts=window.window_ts,
            open_price=window.open_price,
            up_price=window.up_price,
        )

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the strategy."""
        if not runtime.five_min_enabled:
            self._log.info("strategy.disabled", reason="runtime.five_min_enabled=false")
            return
        
        self._running = True
        self._log.info("strategy.started")

    async def stop(self) -> None:
        """Stop the strategy."""
        self._running = False
        self._log.info("strategy.stopped")

    # ─── Market State Handler ─────────────────────────────────────────────────

    async def on_market_state(self, state: MarketState) -> None:
        """
        Called by the orchestrator on every market state update (~every 1-3s).
        
        REVERTED TO MORNING STRATEGY (2026-04-02):
        Continuous evaluator disabled — it was overconfident and unprofitable.
        Using legacy T-60s single-shot evaluation instead.
        Legacy path fires via _evaluate_window() called from orchestrator
        on each window signal.
        """
        if not self._running or not runtime.five_min_enabled:
            return
        
        # Still register windows (needed for token ID lookup)
        if self._pending_windows:
            for window in self._pending_windows:
                window_key = f"{window.asset}-{window.window_ts}"
                if window_key not in self._active_eval_states and window_key != self._last_executed_window:
                    self._active_eval_states[window_key] = EvalWindowState(
                        window_ts=window.window_ts,
                        open_price=window.open_price or 0,
                    )
                    self._log.info(
                        "window.monitoring_started",
                        window_key=window_key,
                        open_price=window.open_price,
                    )
            self._pending_windows.clear()
        
        # ── CONTINUOUS EVALUATOR DISABLED ──────────────────────────────
        # Reverted to morning's T-60s single-shot strategy.
        # The continuous evaluator was overconfident and lost money.
        # Legacy _evaluate_window() is called by orchestrator on each
        # window signal (T-60s before close).
        # 
        # Morning session: +$93 with simple delta + VPIN at T-60s
        # Afternoon with continuous evaluator: -$258
        # ──────────────────────────────────────────────────────────────
        pass

    # ─── Legacy Window Handler (still used by feed callback) ──────────────────

    async def _evaluate_window(self, window: WindowInfo, state: MarketState) -> None:
        """
        Legacy: evaluate a window from the T-offset signal.
        Now just registers the window for continuous monitoring.
        """
        window_key = f"{window.asset}-{window.window_ts}"
        if self._last_executed_window == window_key:
            self._log.debug("window.already_executed", window=window_key)
            return
        
        # Get current price for this asset
        # BTC comes from the live Binance websocket via state.btc_price
        # Other assets: fetch spot price from Binance REST API
        if window.asset == "BTC":
            current_price = float(state.btc_price) if state.btc_price else None
        else:
            current_price = await self._fetch_current_price(window.asset)
        
        if current_price is None:
            self._log.warning("evaluate.no_current_price", asset=window.asset)
            return
        
        # Get window open price
        open_price = window.open_price
        if open_price is None:
            self._log.warning("evaluate.no_open_price")
            return
        
        # Calculate window delta
        delta_pct = (current_price - open_price) / open_price * 100
        
        # Get current VPIN
        current_vpin = self._vpin.current_vpin
        
        # Evaluate signal
        signal = self._evaluate_signal(window, current_price, current_vpin, delta_pct)

        tf = "15m" if window.duration_secs == 900 else "5m"

        # ── Determine regime for snapshot (mirrors _evaluate_signal logic) ───
        from config.runtime_config import runtime as _runtime
        if current_vpin >= _runtime.vpin_cascade_direction_threshold:
            _snap_regime = "CASCADE"
        elif current_vpin >= _runtime.vpin_informed_threshold:
            _snap_regime = "TRANSITION"
        elif current_vpin >= 0.45:
            _snap_regime = "NORMAL"
        else:
            _snap_regime = "CALM"

        # ── Capture CoinGlass snapshot at evaluation time ────────────────────
        cg = self._cg_enhanced.snapshot if self._cg_enhanced is not None else None

        # ── Build window snapshot dict ───────────────────────────────────────
        window_snapshot = {
            "window_ts": window.window_ts,
            "asset": window.asset,
            "timeframe": tf,
            "open_price": open_price,
            "close_price": current_price,
            "delta_pct": delta_pct,
            "vpin": current_vpin,
            "regime": _snap_regime,
            "btc_price": current_price,
            # CoinGlass
            "cg_connected": cg.connected if cg else False,
            "cg_oi_usd": cg.oi_usd if cg else None,
            "cg_oi_delta_pct": cg.oi_delta_pct_1m if cg else None,
            "cg_liq_long_usd": cg.liq_long_usd_1m if cg else None,
            "cg_liq_short_usd": cg.liq_short_usd_1m if cg else None,
            "cg_liq_total_usd": cg.liq_total_usd_1m if cg else None,
            "cg_long_pct": cg.long_pct if cg else None,
            "cg_short_pct": cg.short_pct if cg else None,
            "cg_long_short_ratio": cg.long_short_ratio if cg else None,
            "cg_top_long_pct": cg.top_position_long_pct if cg else None,
            "cg_top_short_pct": cg.top_position_short_pct if cg else None,
            "cg_top_ratio": cg.top_position_ratio if cg else None,
            "cg_taker_buy_usd": cg.taker_buy_volume_1m if cg else None,
            "cg_taker_sell_usd": cg.taker_sell_volume_1m if cg else None,
            "cg_funding_rate": cg.funding_rate if cg else None,
            # Signal
            "direction": signal.direction if signal else None,
            "confidence": signal.confidence if signal else None,
            "cg_modifier": signal.cg_modifier if signal else 0.0,
            "trade_placed": signal is not None,
            "skip_reason": None if signal else "no_edge",
        }

        # ── Non-blocking DB write ────────────────────────────────────────────
        if self._db is not None:
            try:
                asyncio.create_task(self._db.write_window_snapshot(window_snapshot))
            except Exception:
                pass

        if signal is None:
            self._log.info(
                "evaluate.skip",
                asset=window.asset,
                window_ts=window.window_ts,
                delta_pct=f"{delta_pct:.4f}%",
                reason="no edge",
                entry=f"T-{FIVE_MIN_ENTRY_OFFSET}s",
            )
            if self._alerter:
                try:
                    asyncio.create_task(self._alerter.send_window_report(
                        window_ts=window.window_ts,
                        asset=window.asset,
                        timeframe=tf,
                        open_price=open_price,
                        close_price=current_price,
                        delta_pct=delta_pct,
                        vpin=current_vpin,
                        regime=_snap_regime,
                        cg_snapshot=cg,
                        direction=None,
                        confidence=None,
                        trade_placed=False,
                        skip_reason="no_edge",
                        cg_modifier=0.0,
                    ))
                except Exception:
                    pass
            return

        # ── Send window report (non-blocking) ────────────────────────────────
        if self._alerter:
            try:
                asyncio.create_task(self._alerter.send_window_report(
                    window_ts=window.window_ts,
                    asset=window.asset,
                    timeframe=tf,
                    open_price=open_price,
                    close_price=current_price,
                    delta_pct=delta_pct,
                    vpin=current_vpin,
                    regime=_snap_regime,
                    cg_snapshot=cg,
                    direction=signal.direction,
                    confidence=signal.confidence,
                    trade_placed=True,
                    skip_reason=None,
                    cg_modifier=signal.cg_modifier,
                ))
            except Exception:
                pass

        # Execute trade
        await self._execute_trade(state, signal)
        
        # Track executed window
        self._last_executed_window = window_key

    def _evaluate_signal(
        self,
        window: WindowInfo,
        current_price: float,
        current_vpin: float,
        delta_pct: float,
    ) -> Optional[FiveMinSignal]:
        """
        Evaluate trading signal based on delta, VPIN, and market regime.

        REGIME-AWARE DIRECTION (v4):
        ─────────────────────────────────────────────────────────
        VPIN >= 0.65 (cascade/trend):  MOMENTUM  — ride the trend
        VPIN  < 0.55 (normal/ranging): CONTRARIAN — mean-reversion
        VPIN 0.55–0.65 (transition):   CONTRARIAN with higher delta bar
        ─────────────────────────────────────────────────────────

        Evidence:
        - De Nicola (2021): 5-min BTC autocorrelation = -0.1016 (mean-reversion)
        - 4-day backtest: 55-59% contrarian WR in normal VPIN regime
        - Live data (Apr 3): 83% momentum WR when VPIN > 0.75 (cascade)
        
        Returns None if no trade should be placed.
        """
        # VPIN gate — core thesis: no informed flow = no trade
        if current_vpin < runtime.five_min_vpin_gate:
            return None
        
        # ── REGIME-AWARE DIRECTION (v4.1) ──────────────────────────
        # Cascade/trend regime: VPIN >= 0.65 → MOMENTUM + lower delta bar
        # Transition zone:      VPIN 0.55-0.65 → CONTRARIAN + higher delta bar
        # Normal/ranging:       VPIN < 0.55 → CONTRARIAN + standard delta bar
        #
        # KEY INSIGHT (Billy): In cascade, VPIN IS the signal. A smaller
        # delta still means something when informed flow is extreme.
        # Like how a small temperature rise matters more in a patient
        # who's already septic vs a healthy one.
        
        if current_vpin >= runtime.vpin_cascade_direction_threshold:
            # CASCADE/TREND: ride the momentum, VPIN-scaled delta bar
            # The higher the VPIN, the less delta we need — VPIN IS the signal
            # VPIN 0.65-0.75: use configured cascade min delta (0.03%)
            # VPIN 0.75-0.85: halve it (0.015%)
            # VPIN 0.85+:     near-zero (0.005%) — mega cascade, just go
            base_min = runtime.five_min_cascade_min_delta_pct
            if current_vpin >= 0.85:
                min_delta = 0.005
            elif current_vpin >= 0.75:
                # Linear scale from base_min down to base_min/2
                t = (current_vpin - 0.75) / 0.10
                min_delta = base_min * (1.0 - 0.5 * t)
            else:
                min_delta = base_min
            if abs(delta_pct) < min_delta:
                return None
            direction = "UP" if delta_pct > 0 else "DOWN"
            regime = "CASCADE"
            self._log.debug(
                "evaluate.cascade_delta_bar",
                vpin=f"{current_vpin:.3f}",
                min_delta=f"{min_delta:.4f}%",
                actual_delta=f"{abs(delta_pct):.4f}%",
                scaled="mega" if current_vpin >= 0.85 else ("high" if current_vpin >= 0.75 else "base"),
            )
        elif current_vpin >= runtime.vpin_informed_threshold:
            # TRANSITION: contrarian but require larger delta
            if abs(delta_pct) < 0.12:
                self._log.debug(
                    "evaluate.skip_transition",
                    vpin=f"{current_vpin:.3f}",
                    delta=f"{delta_pct:.4f}%",
                    reason="transition zone needs delta >= 0.12%",
                )
                return None
            direction = "DOWN" if delta_pct > 0 else "UP"
            regime = "TRANSITION"
        else:
            # NORMAL: mean-reversion (contrarian), standard delta bar
            if abs(delta_pct) < runtime.five_min_min_delta_pct:
                return None
            direction = "DOWN" if delta_pct > 0 else "UP"
            regime = "NORMAL"
        
        # Calculate base confidence from VPIN + delta (primary signals)
        confidence = self._calculate_confidence(delta_pct, current_vpin, direction)

        # ── CoinGlass Confirmation Layer ───────────────────────────────────
        # CoinGlass is a CONFIRMING signal only. VPIN + delta are primary.
        # If CG agrees → can lift LOW to MODERATE, boost MODERATE to HIGH.
        # If CG strongly disagrees → reduce confidence or skip.
        cg_confidence_modifier = 0.0
        cg_log_parts: list[str] = []

        cg = self._cg_enhanced.snapshot if self._cg_enhanced is not None else None

        if cg is not None and cg.connected:
            # a) Liquidation confirmation
            # Longs getting wrecked (liq_long >> liq_short) → confirms DOWN
            # Shorts getting wrecked → confirms UP
            if cg.liq_total_usd_1m > 0:
                if cg.liq_long_usd_1m > cg.liq_short_usd_1m * 2:
                    liq_bias = "DOWN"
                    liq_delta = 0.15
                elif cg.liq_short_usd_1m > cg.liq_long_usd_1m * 2:
                    liq_bias = "UP"
                    liq_delta = 0.15
                else:
                    liq_bias = "NEUTRAL"
                    liq_delta = 0.0

                if liq_delta > 0:
                    if liq_bias == direction:
                        cg_confidence_modifier += liq_delta
                        cg_log_parts.append(f"liq_confirms_{direction}(+{liq_delta:.2f})")
                    else:
                        cg_confidence_modifier -= liq_delta
                        cg_log_parts.append(f"liq_contradicts_{direction}(-{liq_delta:.2f})")

            # b) Crowd positioning (contrarian signal in NORMAL/TRANSITION)
            # Crowd overleveraged long → slight DOWN bias
            if regime in ("NORMAL", "TRANSITION"):
                if cg.long_pct > 60.0:
                    crowd_bias = "DOWN"
                    crowd_delta = 0.10
                elif cg.short_pct > 60.0:
                    crowd_bias = "UP"
                    crowd_delta = 0.10
                else:
                    crowd_bias = "NEUTRAL"
                    crowd_delta = 0.0

                if crowd_delta > 0:
                    if crowd_bias == direction:
                        cg_confidence_modifier += crowd_delta
                        cg_log_parts.append(f"crowd_contrarian_{direction}(+{crowd_delta:.2f})")
                    else:
                        cg_confidence_modifier -= crowd_delta
                        cg_log_parts.append(f"crowd_contra_{direction}(-{crowd_delta:.2f})")

            # c) Smart money divergence (strongest contrarian signal)
            # Top traders SHORT but crowd LONG → strong DOWN signal
            # Top traders LONG but crowd SHORT → strong UP signal
            smart_money_short = cg.top_position_short_pct > cg.top_position_long_pct
            crowd_long = cg.long_pct > cg.short_pct
            if smart_money_short and crowd_long:
                # Smart/crowd diverge: smart shorts, crowd longs → contrarian DOWN
                if direction == "DOWN":
                    cg_confidence_modifier += 0.20
                    cg_log_parts.append(f"smart_vs_crowd_confirms_DOWN(+0.20)")
                else:
                    cg_confidence_modifier -= 0.20
                    cg_log_parts.append(f"smart_vs_crowd_contradicts_UP(-0.20)")
            elif not smart_money_short and not crowd_long:
                # Smart longs, crowd shorts → contrarian UP
                if direction == "UP":
                    cg_confidence_modifier += 0.20
                    cg_log_parts.append(f"smart_vs_crowd_confirms_UP(+0.20)")
                else:
                    cg_confidence_modifier -= 0.20
                    cg_log_parts.append(f"smart_vs_crowd_contradicts_DOWN(-0.20)")

            # d) Taker aggression (confirms momentum in CASCADE)
            # Aggressive selling (sell >> buy) confirms DOWN in cascade
            total_taker = cg.taker_buy_volume_1m + cg.taker_sell_volume_1m
            if total_taker > 0:
                taker_sell_ratio = cg.taker_sell_volume_1m / total_taker
                taker_buy_ratio = cg.taker_buy_volume_1m / total_taker
                if taker_sell_ratio > 0.65:
                    taker_bias = "DOWN"
                    taker_delta = 0.15 if regime == "CASCADE" else 0.08
                elif taker_buy_ratio > 0.65:
                    taker_bias = "UP"
                    taker_delta = 0.15 if regime == "CASCADE" else 0.08
                else:
                    taker_bias = "NEUTRAL"
                    taker_delta = 0.0

                if taker_delta > 0:
                    if taker_bias == direction:
                        cg_confidence_modifier += taker_delta
                        cg_log_parts.append(f"taker_aggression_{direction}(+{taker_delta:.2f})")
                    else:
                        cg_confidence_modifier -= taker_delta
                        cg_log_parts.append(f"taker_contra_{direction}(-{taker_delta:.2f})")

            # e) Funding rate extremes → persistent directional pressure
            # Extreme positive funding (>0.01%) → longs paying → DOWN pressure
            # Extreme negative funding (<-0.01%) → shorts paying → UP pressure
            funding = cg.funding_rate
            if funding > 0.0001:  # > 0.01%
                funding_bias = "DOWN"
                funding_delta = min(0.10, abs(funding) * 500)  # scale with magnitude
            elif funding < -0.0001:
                funding_bias = "UP"
                funding_delta = min(0.10, abs(funding) * 500)
            else:
                funding_bias = "NEUTRAL"
                funding_delta = 0.0

            if funding_delta > 0:
                if funding_bias == direction:
                    cg_confidence_modifier += funding_delta
                    cg_log_parts.append(f"funding_confirms_{direction}(+{funding_delta:.2f})")
                else:
                    cg_confidence_modifier -= funding_delta
                    cg_log_parts.append(f"funding_contra_{direction}(-{funding_delta:.2f})")

        # Clamp modifier to [-0.5, +0.5]
        cg_confidence_modifier = max(-0.5, min(0.5, cg_confidence_modifier))

        self._log.debug(
            "evaluate.coinglass_signal",
            regime=regime,
            direction=direction,
            cg_modifier=f"{cg_confidence_modifier:+.2f}",
            cg_connected=cg is not None and cg.connected if cg else False,
            contributions=", ".join(cg_log_parts) if cg_log_parts else "none",
            liq_long=f"${cg.liq_long_usd_1m:,.0f}" if cg else "n/a",
            liq_short=f"${cg.liq_short_usd_1m:,.0f}" if cg else "n/a",
            long_pct=f"{cg.long_pct:.1f}%" if cg else "n/a",
            top_short_pct=f"{cg.top_position_short_pct:.1f}%" if cg else "n/a",
            taker_sell_pct=f"{cg.taker_sell_volume_1m / (cg.taker_buy_volume_1m + cg.taker_sell_volume_1m) * 100:.1f}%" if (cg and (cg.taker_buy_volume_1m + cg.taker_sell_volume_1m) > 0) else "n/a",
            funding=f"{cg.funding_rate * 100:.4f}%" if cg else "n/a",
        )

        # Apply CoinGlass modifier to lift/suppress confidence
        # Modifier > 0.2: can lift LOW → MODERATE
        # Modifier < -0.2: suppress MODERATE → LOW (skip), or HIGH → MODERATE (skip)
        if cg_confidence_modifier >= 0.2 and confidence == "LOW":
            self._log.info(
                "evaluate.cg_lift",
                from_confidence="LOW",
                to_confidence="MODERATE",
                modifier=f"{cg_confidence_modifier:+.2f}",
                contributions=", ".join(cg_log_parts),
            )
            confidence = "MODERATE"
        elif cg_confidence_modifier <= -0.2 and confidence == "MODERATE":
            self._log.info(
                "evaluate.cg_suppress",
                from_confidence="MODERATE",
                to_confidence="LOW",
                modifier=f"{cg_confidence_modifier:+.2f}",
                contributions=", ".join(cg_log_parts),
            )
            confidence = "LOW"
        elif cg_confidence_modifier <= -0.35 and confidence == "HIGH":
            self._log.info(
                "evaluate.cg_suppress",
                from_confidence="HIGH",
                to_confidence="MODERATE",
                modifier=f"{cg_confidence_modifier:+.2f}",
                contributions=", ".join(cg_log_parts),
            )
            confidence = "MODERATE"

        # Block NONE and LOW confidence — only trade MODERATE or HIGH
        if confidence in ("NONE", "LOW"):
            return None

        self._log.info(
            "evaluate.regime_signal",
            regime=regime,
            vpin=f"{current_vpin:.3f}",
            delta=f"{delta_pct:+.4f}%",
            direction=direction,
            confidence=confidence,
            cg_modifier=f"{cg_confidence_modifier:+.2f}",
        )

        return FiveMinSignal(
            window=window,
            current_price=current_price,
            current_vpin=current_vpin,
            delta_pct=delta_pct,
            confidence=confidence,
            direction=direction,
            cg_modifier=cg_confidence_modifier,
        )

    def _calculate_confidence(
        self,
        delta_pct: float,
        current_vpin: float,
        direction: str,
    ) -> str:
        """
        Calculate confidence level based on delta and VPIN.
        
        - If VPIN >= 0.30 AND delta direction aligns → HIGH
        - If delta > 0.10% → HIGH
        - If delta > 0.02% → MODERATE
        - If delta > 0.005% → LOW
        """
        abs_delta = abs(delta_pct)
        
        # High confidence: strong delta alone is enough
        if abs_delta > 0.10:
            return "HIGH"
        
        # Moderate confidence: decent delta
        if abs_delta > 0.02:
            return "MODERATE"
        
        # Low confidence: small but non-trivial delta
        if abs_delta > 0.005:
            return "LOW"
        
        return "NONE"

    # ─── Execution (Full-Window Signal) ─────────────────────────────────────

    async def _execute_from_signal(
        self,
        state: MarketState,
        signal: WindowSignal,
        eval_state: EvalWindowState,
        window_key: str,
    ) -> None:
        """Execute a trade from the full-window evaluator signal."""
        # Parse window key: "BTC-1711900800"
        parts = window_key.split("-", 1)
        asset = parts[0] if len(parts) > 1 else "BTC"
        window_ts = eval_state.window_ts

        # Stake calculated after price is determined (see below)
        # Preliminary risk check with base stake
        base_stake = self._calculate_stake(signal.tier, signal.estimated_token_price)
        approved, reason = await self._check_risk(base_stake)
        if not approved:
            self._log.info("trade.risk_blocked", window=window_key, reason=reason)
            return

        # Direction
        direction = "YES" if signal.direction == "UP" else "NO"
        
        # Start with evaluator's estimate, will override with fresh Gamma price
        price = Decimal(str(round(signal.estimated_token_price, 4)))

        # Get token IDs from recent windows
        token_id = None
        recent = getattr(self, '_recent_windows', [])
        
        # Try exact match first, then closest match
        for window in recent:
            wts = getattr(window, 'window_ts', 0)
            w_asset = getattr(window, 'asset', '')
            if wts == window_ts and w_asset == asset:
                tid = window.up_token_id if direction == "YES" else window.down_token_id
                if tid:
                    token_id = tid
                    if direction == "YES" and window.up_price is not None:
                        price = Decimal(str(round(window.up_price, 4)))
                    elif direction == "NO" and window.down_price is not None:
                        price = Decimal(str(round(window.down_price, 4)))
                    break
        
        # If no exact match, use the most recent window for this asset with token IDs
        if token_id is None:
            for window in reversed(recent):
                w_asset = getattr(window, 'asset', '')
                if w_asset == asset:
                    tid = window.up_token_id if direction == "YES" else window.down_token_id
                    if tid and not tid.startswith("paper"):
                        token_id = tid
                        if direction == "YES" and window.up_price is not None:
                            price = Decimal(str(round(window.up_price, 4)))
                        elif direction == "NO" and window.down_price is not None:
                            price = Decimal(str(round(window.down_price, 4)))
                        self._log.info("execute.token_id_from_recent", window=window_key, source_ts=getattr(window, 'window_ts', 0))
                        break

        if token_id is None:
            self._log.warning("execute.no_token_id_waiting", window=window_key, direction=direction, recent_count=len(recent))
            eval_state.fired = False
            # Cooldown: don't retry for 5 seconds to avoid spamming
            eval_state._retry_after = time.time() + 5.0
            return

        tf = "15m" if (window_ts % 900 == 0 and eval_state.window_ts + 900 > time.time()) else "5m"
        market_slug = f"{asset.lower()}-updown-{tf}-{window_ts}"

        # Fetch FRESH Gamma price right before placing order
        try:
            import aiohttp
            slug = market_slug
            async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
                url = f"https://gamma-api.polymarket.com/events?slug={slug}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data and isinstance(data, list) and data[0].get("markets"):
                            mkt = data[0]["markets"][0]
                            best_ask = mkt.get("bestAsk")
                            if best_ask is not None:
                                fresh_up = float(best_ask)
                                fresh_down = round(1.0 - fresh_up, 4)
                                if direction == "YES":
                                    base_price = fresh_up
                                else:
                                    base_price = fresh_down
                                # Use exact Gamma price first — bump only if no fill
                                price = Decimal(str(round(base_price, 4)))
                                # Store base price for potential retry at +2¢
                                eval_state._base_price = base_price
                                self._log.info(
                                    "execute.fresh_gamma_price",
                                    window=window_key,
                                    direction=direction,
                                    price=str(price),
                                )
        except Exception as exc:
            self._log.debug("execute.fresh_price_failed", error=str(exc))
            # Keep the existing price from window/estimate

        # Recalculate stake with the FRESH price for proper risk/reward scaling
        stake = self._calculate_stake(signal.tier, float(price))

        try:
            clob_order_id = await self._poly.place_order(
                market_slug=market_slug,
                direction=direction,
                price=price,
                stake_usd=stake,
                token_id=token_id or None,
            )
        except Exception as exc:
            self._log.error("execute.order_failed", window=window_key, error=str(exc))
            return

        order_id = clob_order_id if not self._poly.paper_mode else f"5min-{uuid.uuid4().hex[:12]}"

        fee_mult = 0.072
        fee_usd = fee_mult * float(price) * (1.0 - float(price)) * stake

        order = Order(
            order_id=order_id,
            strategy=self.name,
            venue="polymarket",
            direction=direction,
            price=str(price),
            stake_usd=stake,
            fee_usd=fee_usd,
            status=OrderStatus.OPEN,
            btc_entry_price=float(state.btc_price) if state.btc_price else None,
            window_seconds=300,
            market_id=market_slug,
            metadata={
                "window_ts": window_ts,
                "window_open_price": eval_state.open_price,
                "delta_pct": signal.delta_pct,
                "vpin": signal.vpin,
                "confidence": signal.confidence,
                "tier": signal.tier,
                "entry_reason": signal.entry_reason,
                "token_id": token_id,
                "clob_order_id": clob_order_id,
                "market_slug": market_slug,
                "seconds_to_close": signal.seconds_to_close,
                "eval_count": eval_state.eval_count,
                "score": signal.score,
                "liq_surge_weight": signal.liq_surge_weight,
                "ls_imbalance_weight": signal.ls_imbalance_weight,
                "funding_weight": signal.funding_weight,
            },
        )

        await self._om.register_order(order)

        self._log.info(
            "trade.executed",
            order_id=order.order_id[:20] + "..." if len(order.order_id) > 20 else order.order_id,
            window=window_key,
            direction=direction,
            tier=signal.tier,
            confidence=f"{signal.confidence:.2f}",
            delta_pct=f"{signal.delta_pct:+.4f}%",
            score=f"{signal.score:.2f}",
            stake=f"${stake:.2f}",
            token_price=str(price),
            seconds_to_close=f"{signal.seconds_to_close:.0f}",
            entry_reason=signal.entry_reason,
        )

        # Post-trade fill verification (polling loop)
        if not self._poly.paper_mode and order.order_id.startswith("0x"):
            POLL_INTERVAL = 5
            MAX_WAIT = 60
            filled = False
            try:
                elapsed = 0
                while elapsed < MAX_WAIT:
                    await asyncio.sleep(POLL_INTERVAL)
                    elapsed += POLL_INTERVAL
                    status = await self._poly.get_order_status(order.order_id)
                    clob_status = status.get("status", "UNKNOWN")
                    size_matched = status.get("size_matched", "0")
                    filled = float(size_matched) > 0 if size_matched else False
                    if filled or clob_status not in ("LIVE", "UNKNOWN"):
                        break

                order.metadata["filled"] = filled
                order.metadata["fill_wait_seconds"] = elapsed

                if filled and self._alerter:
                    asyncio.create_task(self._alerter.send_entry_alert(order))
                elif not filled:
                    self._log.warning("trade.not_filled", order_id=order.order_id[:20], waited=f"{elapsed}s")
                    
                    # RETRY at +2¢ bump if original didn't fill
                    base_price = getattr(eval_state, '_base_price', None)
                    if base_price and not filled:
                        bumped = round(base_price + 0.02, 4)
                        bumped_price = Decimal(str(bumped))
                        self._log.info(
                            "trade.retry_bumped",
                            original_price=str(price),
                            bumped_price=str(bumped_price),
                            window=window_key,
                        )
                        # Cancel original order
                        try:
                            if hasattr(self._poly, '_clob_client') and self._poly._clob_client:
                                await asyncio.to_thread(self._poly._clob_client.cancel, order.order_id)
                        except Exception:
                            pass
                        
                        # Recalculate stake for bumped price
                        retry_stake = self._calculate_stake(signal.tier, float(bumped_price))
                        
                        try:
                            retry_id = await self._poly.place_order(
                                market_slug=market_slug,
                                direction=direction,
                                price=bumped_price,
                                stake_usd=retry_stake,
                                token_id=token_id,
                            )
                            order.order_id = retry_id
                            order.price = str(bumped_price)
                            order.stake_usd = retry_stake
                            order.metadata["retried_at_bump"] = True
                            order.metadata["bumped_price"] = str(bumped_price)
                            self._log.info("trade.retry_placed", order_id=retry_id[:20] if retry_id else "?", price=str(bumped_price))
                            
                            # Quick fill check on retry (15s)
                            await asyncio.sleep(15)
                            status2 = await self._poly.get_order_status(retry_id)
                            retry_filled = float(status2.get("size_matched", "0") or "0") > 0
                            order.metadata["filled"] = retry_filled
                            if retry_filled and self._alerter:
                                asyncio.create_task(self._alerter.send_entry_alert(order))
                            elif not retry_filled:
                                self._log.warning("trade.retry_not_filled", order_id=retry_id[:20] if retry_id else "?")
                        except Exception as exc:
                            self._log.warning("trade.retry_failed", error=str(exc))
            except Exception as exc:
                self._log.warning("trade.verify_failed", error=str(exc))
                if self._alerter:
                    asyncio.create_task(self._alerter.send_entry_alert(order))
        else:
            if self._alerter:
                asyncio.create_task(self._alerter.send_entry_alert(order))

    # ─── Execution (Legacy) ───────────────────────────────────────────────────

    async def _execute_trade(self, state: MarketState, signal: FiveMinSignal) -> None:
        """
        Execute a trade based on the signal.
        
        Args:
            state: Current market state
            signal: Trading signal
        """
        window = signal.window
        
        # Determine stake — will recalculate with fresh price later
        token_price_est = window.down_price or window.up_price or 0.50
        stake = self._calculate_stake(signal.confidence, token_price_est)
        
        # Risk check
        approved, reason = await self._check_risk(stake)
        if not approved:
            self._log.info(
                "trade.risk_blocked",
                asset=window.asset,
                window_ts=window.window_ts,
                stake=stake,
                reason=reason,
                entry=f"T-{FIVE_MIN_ENTRY_OFFSET}s",
            )
            # Notify on Telegram when risk blocks a trade
            if self._alerter:
                try:
                    tf = "15m" if window.duration_secs == 900 else "5m"
                    asyncio.create_task(self._alerter.send_system_alert(
                        f"Trade BLOCKED — {window.asset} {tf}\n"
                        f"Stake: ${stake:.2f}\n"
                        f"Reason: {reason}\n"
                        f"Delta: {signal.delta_pct:+.4f}%",
                        level="warning",
                    ))
                except Exception:
                    pass
            return
        
        # Select direction and token ID
        if signal.direction == "UP":
            direction = "YES"
            token_id = window.up_token_id
        else:
            direction = "NO"
            token_id = window.down_token_id
        
        if token_id is None:
            self._log.error("execute.no_token_id", direction=signal.direction)
            return
        
        # Price: use real Gamma API prices when available (live mode),
        # fall back to delta-based approximation (paper mode)
        if direction == "YES" and window.up_price is not None:
            token_price = window.up_price
        elif direction == "NO" and window.down_price is not None:
            token_price = window.down_price
        else:
            token_price = self._delta_to_token_price(signal.delta_pct)
        
        price = Decimal(str(round(token_price, 4)))
        tf = "15m" if window.duration_secs == 900 else "5m"
        market_slug = f"{window.asset.lower()}-updown-{tf}-{window.window_ts}"
        
        # Place order — pass real token_id for live mode
        try:
            clob_order_id = await self._poly.place_order(
                market_slug=market_slug,
                direction=direction,
                price=price,
                stake_usd=stake,
                token_id=token_id,
            )
        except Exception as exc:
            self._log.error("execute.order_failed", error=str(exc))
            return
        
        # Use the real CLOB order ID so we can track it on-chain
        order_id = clob_order_id if not self._poly.paper_mode else f"5min-{uuid.uuid4().hex[:12]}"
        
        # Calculate fee
        fee_mult = 0.072  # Polymarket fee
        fee_usd = fee_mult * float(price) * (1.0 - float(price)) * stake
        
        # Create order
        order = Order(
            order_id=order_id,
            strategy=self.name,
            venue="polymarket",
            direction=direction,
            price=str(price),
            stake_usd=stake,
            fee_usd=fee_usd,
            status=OrderStatus.OPEN,
            btc_entry_price=signal.current_price,
            window_seconds=window.duration_secs,
            market_id=market_slug,
            metadata={
                "window_ts": window.window_ts,
                "window_open_price": window.open_price,
                "delta_pct": signal.delta_pct,
                "vpin": signal.current_vpin,
                "confidence": signal.confidence,
                "token_id": token_id,
                "entry_offset_s": FIVE_MIN_ENTRY_OFFSET,
                "entry_label": f"T-{FIVE_MIN_ENTRY_OFFSET}s",
                "timeframe": tf,
                "window_duration_s": window.duration_secs,
                "clob_order_id": clob_order_id if 'clob_order_id' in dir() else None,
                "market_slug": f"{window.asset.lower()}-updown-{tf}-{window.window_ts}",
            },
        )
        
        await self._om.register_order(order)
        
        self._log.info(
            "trade.submitted",
            order_id=order.order_id,
            asset=window.asset,
            direction=direction,
            stake=stake,
            delta_pct=f"{signal.delta_pct:.4f}%",
            vpin=f"{signal.current_vpin:.4f}",
            confidence=signal.confidence,
            entry=f"T-{FIVE_MIN_ENTRY_OFFSET}s",
            token_price=str(price),
        )

        # Post-trade verification — poll until filled or timeout
        # CLOB matching can take 5-30s on thin books near window close.
        # Poll every 5s for up to 60s before giving up.
        if not self._poly.paper_mode and order.order_id.startswith("0x"):
            POLL_INTERVAL = 5    # seconds between checks
            MAX_WAIT = 60        # total seconds to wait for fill
            filled = False

            try:
                elapsed = 0
                while elapsed < MAX_WAIT:
                    await asyncio.sleep(POLL_INTERVAL)
                    elapsed += POLL_INTERVAL

                    status = await self._poly.get_order_status(order.order_id)
                    clob_status = status.get("status", "UNKNOWN")
                    size_matched = status.get("size_matched", "0")
                    filled = float(size_matched) > 0 if size_matched else False

                    self._log.info(
                        "trade.fill_check",
                        order_id=order.order_id[:20] + "...",
                        clob_status=clob_status,
                        size_matched=size_matched,
                        filled=filled,
                        elapsed=f"{elapsed}s",
                    )

                    if filled:
                        break

                    # If order was cancelled or matched (not LIVE), stop polling
                    if clob_status not in ("LIVE", "UNKNOWN"):
                        break

                order.metadata["clob_status"] = clob_status
                order.metadata["size_matched"] = size_matched
                order.metadata["filled"] = filled
                order.metadata["fill_wait_seconds"] = elapsed

                if filled:
                    self._log.info(
                        "trade.verified",
                        order_id=order.order_id[:20] + "...",
                        size_matched=size_matched,
                        wait=f"{elapsed}s",
                    )
                    if self._alerter:
                        asyncio.create_task(self._alerter.send_entry_alert(order))
                else:
                    self._log.warning(
                        "trade.not_filled",
                        order_id=order.order_id[:20] + "...",
                        clob_status=clob_status,
                        waited=f"{elapsed}s",
                    )
                    # ── +2¢ RETRY ──────────────────────────────────────────
                    # Cancel original, bump price by 2¢, re-check risk, retry
                    self._log.info("trade.retry_starting", original_price=str(price), order_id=order.order_id[:20] + "...")
                    try:
                        bumped_price_f = round(float(price) + 0.02, 4)
                        # Only retry if bumped price is still reasonable (<0.70)
                        if bumped_price_f < 0.70:
                            # Cancel the unfilled original order
                            try:
                                if hasattr(self._poly, '_clob_client') and self._poly._clob_client:
                                    await asyncio.to_thread(self._poly._clob_client.cancel, order.order_id)
                                    self._log.info("trade.retry_cancelled_original", order_id=order.order_id[:20] + "...")
                            except Exception:
                                pass

                            # Recalculate stake at bumped price
                            retry_stake = self._calculate_stake(signal.confidence, bumped_price_f)

                            # Re-check risk with new stake
                            retry_approved, retry_reason = await self._check_risk(retry_stake)
                            if not retry_approved:
                                self._log.info("trade.retry_risk_blocked", reason=retry_reason, stake=retry_stake)
                            else:
                                bumped_price_dec = Decimal(str(bumped_price_f))
                                self._log.info(
                                    "trade.retry_bumped",
                                    original_price=str(price),
                                    bumped_price=str(bumped_price_dec),
                                    retry_stake=f"${retry_stake:.2f}",
                                )
                                retry_id = await self._poly.place_order(
                                    market_slug=market_slug,
                                    direction=direction,
                                    price=bumped_price_dec,
                                    stake_usd=retry_stake,
                                    token_id=token_id,
                                )
                                order.order_id = retry_id
                                order.price = str(bumped_price_dec)
                                order.stake_usd = retry_stake
                                order.metadata["retried_at_bump"] = True
                                order.metadata["bumped_price"] = str(bumped_price_dec)
                                self._log.info("trade.retry_placed", order_id=str(retry_id)[:20], price=str(bumped_price_dec))

                                # Quick fill check on retry (15s)
                                await asyncio.sleep(15)
                                status2 = await self._poly.get_order_status(retry_id)
                                retry_filled = float(status2.get("size_matched", "0") or "0") > 0
                                order.metadata["filled"] = retry_filled
                                if retry_filled:
                                    self._log.info("trade.retry_filled", order_id=str(retry_id)[:20], size_matched=status2.get("size_matched"))
                                    if self._alerter:
                                        asyncio.create_task(self._alerter.send_entry_alert(order))
                                else:
                                    self._log.warning("trade.retry_not_filled", order_id=str(retry_id)[:20])
                        else:
                            self._log.info("trade.retry_skip_expensive", bumped_price=bumped_price_f)
                    except Exception as retry_exc:
                        self._log.warning("trade.retry_failed", error=str(retry_exc))
                    # ── END RETRY ──────────────────────────────────────────
            except Exception as exc:
                self._log.warning("trade.verify_failed", order_id=order.order_id[:20], error=str(exc))
                # Still send alert on verify failure — better to over-notify than miss
                if self._alerter:
                    asyncio.create_task(self._alerter.send_entry_alert(order))
        else:
            # Paper mode — always alert
            if self._alerter:
                try:
                    asyncio.create_task(self._alerter.send_entry_alert(order))
                except Exception:
                    pass

        # Legacy log line for backward compat
        self._log.info(
            "trade.executed",
            order_id=order.order_id,
            asset=window.asset,
            direction=direction,
            stake=stake,
            delta_pct=f"{signal.delta_pct:.4f}%",
            vpin=f"{signal.current_vpin:.4f}",
            confidence=signal.confidence,
            entry=f"T-{FIVE_MIN_ENTRY_OFFSET}s",
            token_price=str(price),
        )



    # ─── Price Fetching ─────────────────────────────────────────────────────

    _ASSET_TO_SYMBOL = {
        "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
        "DOGE": "DOGEUSDT", "XRP": "XRPUSDT", "BNB": "BNBUSDT",
        "HYPE": "HYPEUSDT",
    }

    async def _fetch_current_price(self, asset: str) -> float | None:
        """Fetch current spot price from Binance REST API for non-BTC assets."""
        symbol = self._ASSET_TO_SYMBOL.get(asset.upper())
        if not symbol:
            self._log.warning("unknown_asset_symbol", asset=asset)
            return None
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    data = await resp.json()
                    return float(data["price"])
        except Exception as exc:
            self._log.warning("price_fetch_failed", asset=asset, error=str(exc))
            return None

    @staticmethod
    def _delta_to_token_price(delta_pct: float) -> float:
        """
        Convert window delta to realistic token price.
        Matches the backtest pricing model based on observed Polymarket behavior.
        
        When delta is small, tokens are near $0.50 (coin flip).
        As delta grows, the favoured token gets more expensive.
        """
        d = abs(delta_pct)
        if d < 0.005:
            return 0.50
        elif d < 0.02:
            return 0.50 + (d - 0.005) / (0.02 - 0.005) * 0.05  # 0.50-0.55
        elif d < 0.05:
            return 0.55 + (d - 0.02) / (0.05 - 0.02) * 0.10   # 0.55-0.65
        elif d < 0.10:
            return 0.65 + (d - 0.05) / (0.10 - 0.05) * 0.15   # 0.65-0.80
        elif d < 0.15:
            return 0.80 + (d - 0.10) / (0.15 - 0.10) * 0.12   # 0.80-0.92
        else:
            return min(0.92 + (d - 0.15) / 0.10 * 0.05, 0.97)  # 0.92-0.97

    def _calculate_stake(self, confidence: str, token_price: float = 0.50) -> float:
        """
        Calculate stake scaled by token price for consistent risk/reward.
        
        Cheaper tokens (30-45¢) → full stake (best R/R, 100%+ upside)
        Mid tokens (45-55¢)     → full stake (good R/R)
        Expensive tokens (55-65¢) → reduced stake (lower upside)
        
        Formula: base_stake × price_multiplier
        where price_multiplier = (1 - token_price) / 0.50
        
        Examples at $160 bankroll, 20% fraction ($32 base):
          40¢ → $32 × 1.20 = $38.40 (upside: +$57.60)
          50¢ → $32 × 1.00 = $32.00 (upside: +$32.00)
          55¢ → $32 × 0.90 = $28.80 (upside: +$23.52)
          60¢ → $32 × 0.80 = $25.60 (upside: +$17.07)
          65¢ → $32 × 0.70 = $22.40 (upside: +$12.05)
        
        Hard max: $50 (from DB config max_position_usd)
        Safety buffer: 5% below the calculated max
        """
        status = self._rm.get_status()
        bankroll = status["current_bankroll"]
        base_stake = bankroll * runtime.bet_fraction
        
        # Scale stake by token price — cheaper tokens get bigger bets
        # Normalised so 50¢ = 1.0x, 40¢ = 1.2x, 60¢ = 0.8x
        tp = max(0.30, min(0.65, token_price))
        price_multiplier = (1.0 - tp) / 0.50
        
        # Clamp multiplier: 0.5x to 1.5x of base stake
        price_multiplier = max(0.5, min(1.5, price_multiplier))
        
        adjusted_stake = base_stake * price_multiplier
        
        # Apply 5% safety buffer — leave headroom for price bumps + slippage
        # Max stake from risk manager: bankroll × bet_fraction
        # Effective max: 95% of that
        max_stake = bankroll * runtime.bet_fraction
        adjusted_stake = min(adjusted_stake, max_stake * 0.95)
        
        # HARD MAX: Never exceed max_position_usd from config (default $50)
        # If calculated stake exceeds hard max, scale down to stay below
        hard_max = runtime.max_position_usd
        if adjusted_stake > hard_max:
            adjusted_stake = hard_max * 0.95  # Scale to 95% of hard max
            self._log.warning(
                "stake.hard_max_exceeded",
                original=f"${adjusted_stake / 0.95:.2f}",
                scaled=f"${adjusted_stake:.2f}",
                hard_max=f"${hard_max:.2f}",
                reason="Scaling bet to stay under max_position_usd",
            )
        
        self._log.debug(
            "stake.calculated",
            bankroll=f"${bankroll:.2f}",
            base=f"${base_stake:.2f}",
            token_price=f"${tp:.2f}",
            multiplier=f"{price_multiplier:.2f}x",
            max_stake=f"${max_stake:.2f}",
            final=f"${adjusted_stake:.2f}",
            buffer="5% headroom applied",
        )
        
        return adjusted_stake

    # ─── Base Strategy Interface ──────────────────────────────────────────────

    async def evaluate(self, state: MarketState) -> Optional[dict]:
        """Evaluate market state for trading signals."""
        # This strategy uses window signals, not continuous evaluation
        return None

    async def execute(self, state: MarketState, signal: dict) -> Optional[Order]:
        """Execute a trading signal."""
        # This strategy handles execution internally
        return None
