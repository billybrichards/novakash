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
import os
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
from signals.twap_delta import TWAPTracker, TWAPResult
from signals.timesfm_client import TimesFMClient, TimesFMForecast
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
        cg_feeds=None,
        claude_evaluator=None,
        db_client=None,
        on_window_signal: Optional[Callable[[WindowInfo], Awaitable[None]]] = None,
        geoblock_check_fn: Optional[Callable[[], bool]] = None,
        twap_tracker: Optional[TWAPTracker] = None,
        timesfm_client: Optional[TimesFMClient] = None,
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
        self._cg_feeds = cg_feeds or {}
        self._claude_eval = claude_evaluator  # Claude Opus 4.6 evaluator  # Per-asset CG feeds: {"BTC": feed, "ETH": feed, ...}
        self._db = db_client            # DBClient for window snapshot persistence (optional)
        self._geoblock_check_fn = geoblock_check_fn  # G6: Callable to check if geoblock is active
        self._twap = twap_tracker  # v5.7: TWAP-delta direction tracker
        self._timesfm = timesfm_client  # v6.0: TimesFM forecast client (for comparison alerts)
        self._tick_recorder = None  # TickRecorder injected by orchestrator after start
        self._evaluator = WindowEvaluator()
        
        # Track last executed window to avoid duplicates
        self._last_executed_window: Optional[str] = None
        
        # Active window monitoring state (one per window)
        self._active_eval_states: dict[str, EvalWindowState] = {}
        
        # Window info buffer (populated by feed callbacks)
        self._pending_windows: list = []  # Queue of windows to evaluate (multi-asset)

        # ── G4: Order rate limiter state ──────────────────────────────────────
        self._order_timestamps: list[float] = []  # timestamps of recent orders
        self._last_order_time: float = 0.0

        # ── G5: Circuit breaker state ─────────────────────────────────────────
        self._circuit_break_until: float = 0.0
        self._consecutive_errors: int = 0
        
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
        Evaluate a window at the T-offset signal.

        Multi-offset support (v5.9): fires at each configured FIVE_MIN_EVAL_OFFSETS
        (e.g. T-90s, then T-60s). Deduplicates: won't trade the same window twice,
        but will still record the window snapshot and send alerts for all offsets.
        """
        window_key = f"{window.asset}-{window.window_ts}"
        eval_offset = getattr(window, "eval_offset", None)

        # Already TRADED this window — skip evaluation entirely
        if self._last_executed_window == window_key:
            self._log.debug(
                "window.already_traded",
                window=window_key,
                eval_offset=eval_offset,
            )
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

        # ── Multi-source delta calculation (v7.2) ─────────────────────────
        # Binance is always available (websocket). Chainlink + Tiingo from DB.
        # DELTA_PRICE_SOURCE env var controls which source drives the direction.
        _delta_source = os.environ.get("DELTA_PRICE_SOURCE", "chainlink").lower()

        # Binance delta (always computed — legacy baseline)
        binance_price = current_price
        delta_binance = (binance_price - open_price) / open_price * 100

        # Fetch Chainlink + Tiingo prices for delta calculation (best-effort)
        _chainlink_price: Optional[float] = None
        _tiingo_price: Optional[float] = None
        if self._db:
            try:
                _chainlink_price = await self._db.get_latest_chainlink_price(window.asset)
            except Exception:
                pass
            try:
                _tiingo_price = await self._db.get_latest_tiingo_price(window.asset)
            except Exception:
                pass

        delta_chainlink = ((_chainlink_price - open_price) / open_price * 100) if _chainlink_price else None
        delta_tiingo = ((_tiingo_price - open_price) / open_price * 100) if _tiingo_price else None

        # Determine price_consensus: direction agreement across available sources
        _dirs = []
        if delta_binance is not None:
            _dirs.append("UP" if delta_binance > 0 else "DOWN")
        if delta_chainlink is not None:
            _dirs.append("UP" if delta_chainlink > 0 else "DOWN")
        if delta_tiingo is not None:
            _dirs.append("UP" if delta_tiingo > 0 else "DOWN")

        if len(_dirs) >= 2:
            _up_count = _dirs.count("UP")
            _down_count = _dirs.count("DOWN")
            if _up_count == len(_dirs) or _down_count == len(_dirs):
                price_consensus = "AGREE"
            elif _up_count == 0 or _down_count == 0:
                price_consensus = "AGREE"
            elif abs(_up_count - _down_count) >= 1 and len(_dirs) >= 3:
                price_consensus = "MIXED"
            elif len(_dirs) == 2 and _up_count != _down_count:
                price_consensus = "MIXED"
            else:
                price_consensus = "DISAGREE"
        else:
            price_consensus = "AGREE"  # Only one source — no conflict

        # Select primary delta based on DELTA_PRICE_SOURCE env var
        if _delta_source == "chainlink" and delta_chainlink is not None:
            delta_pct = delta_chainlink
            _price_source_used = "chainlink"
        elif _delta_source == "tiingo" and delta_tiingo is not None:
            delta_pct = delta_tiingo
            _price_source_used = "tiingo"
        elif _delta_source == "consensus":
            # Only trade when all sources agree — set delta_pct to chainlink (primary) or binance
            if price_consensus != "AGREE":
                self._log.info(
                    "evaluate.consensus_skip",
                    asset=window.asset,
                    price_consensus=price_consensus,
                    delta_binance=f"{delta_binance:+.4f}%" if delta_binance is not None else "N/A",
                    delta_chainlink=f"{delta_chainlink:+.4f}%" if delta_chainlink is not None else "N/A",
                    delta_tiingo=f"{delta_tiingo:+.4f}%" if delta_tiingo is not None else "N/A",
                )
                return
            delta_pct = delta_chainlink if delta_chainlink is not None else delta_binance
            _price_source_used = "consensus"
        else:
            # Default: chainlink with Binance fallback
            delta_pct = delta_chainlink if delta_chainlink is not None else delta_binance
            _price_source_used = "chainlink" if delta_chainlink is not None else "binance_fallback"

        # Flag LOW confidence if Chainlink and Binance disagree on direction
        _price_confidence_flag = "OK"
        if delta_chainlink is not None:
            _cl_dir = "UP" if delta_chainlink > 0 else "DOWN"
            _bn_dir = "UP" if delta_binance > 0 else "DOWN"
            if _cl_dir != _bn_dir:
                _price_confidence_flag = "LOW"
                self._log.warning(
                    "evaluate.price_source_disagreement",
                    asset=window.asset,
                    chainlink_dir=_cl_dir,
                    binance_dir=_bn_dir,
                    delta_chainlink=f"{delta_chainlink:+.4f}%",
                    delta_binance=f"{delta_binance:+.4f}%",
                    price_source_used=_price_source_used,
                )

        self._log.info(
            "evaluate.multi_source_delta",
            asset=window.asset,
            source=_price_source_used,
            delta_pct=f"{delta_pct:+.4f}%",
            delta_binance=f"{delta_binance:+.4f}%",
            delta_chainlink=f"{delta_chainlink:+.4f}%" if delta_chainlink else "N/A",
            delta_tiingo=f"{delta_tiingo:+.4f}%" if delta_tiingo else "N/A",
            consensus=price_consensus,
            confidence_flag=_price_confidence_flag,
        )
        
        # Get current VPIN
        current_vpin = self._vpin.current_vpin
        
        # ── TWAP-Delta evaluation (v5.7) ─────────────────────────────────
        twap_result: Optional[TWAPResult] = None
        if self._twap:
            twap_result = self._twap.evaluate(
                asset=window.asset,
                window_ts=window.window_ts,
                current_price=current_price,
                gamma_up_price=window.up_price,
                gamma_down_price=window.down_price,
            )
            if twap_result:
                self._log.info(
                    "evaluate.twap_result",
                    asset=window.asset,
                    summary=twap_result.summary(),
                )
                # TWAP skip gate: if TWAP says skip (Gamma block, mixed signal, priced in)
                if twap_result.should_skip:
                    self._log.info(
                        "evaluate.twap_skip",
                        asset=window.asset,
                        reason=twap_result.skip_reason,
                        gamma_gate=twap_result.gamma_gate,
                        trend_pct=f"{twap_result.trend_pct:.2f}",
                    )
            # Cleanup window tracking data after evaluation
            self._twap.cleanup_window(window.asset, window.window_ts)

        # ── TimesFM forecast (v6.0 comparison data for alerts) ────────────
        timesfm_forecast: Optional[TimesFMForecast] = None
        if self._timesfm:
            try:
                # Calculate seconds until this window closes
                _window_close_ts = window.window_ts + window.duration_secs
                _seconds_to_close = max(1, int(_window_close_ts - time.time()))
                timesfm_forecast = await self._timesfm.get_forecast(
                    open_price=open_price,
                    seconds_to_close=_seconds_to_close,
                )
                if timesfm_forecast and not timesfm_forecast.error:
                    self._log.info(
                        "evaluate.timesfm_forecast",
                        asset=window.asset,
                        direction=timesfm_forecast.direction,
                        confidence=f"{timesfm_forecast.confidence:.2f}",
                        predicted_close=f"${timesfm_forecast.predicted_close:,.2f}",
                    )
                    # ── TickRecorder: passive recording only ──────────────
                    if self._tick_recorder:
                        asyncio.create_task(
                            self._tick_recorder.record_timesfm_forecast(
                                timesfm_forecast,
                                asset=window.asset,
                                window_ts=int(window.window_ts),
                            )
                        )
            except Exception as exc:
                self._log.debug("evaluate.timesfm_fetch_failed", error=str(exc))

        # Evaluate signal (with TWAP override for direction + TimesFM agreement)
        signal = self._evaluate_signal(window, current_price, current_vpin, delta_pct, twap_result=twap_result, timesfm_forecast=timesfm_forecast)

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
        # Per-asset CG feed (v5.4d) — fall back to BTC if asset feed unavailable

        cg = None

        if hasattr(self, '_cg_feeds') and self._cg_feeds:

            _asset_feed = self._cg_feeds.get(window.asset, self._cg_feeds.get("BTC"))

            if _asset_feed and _asset_feed.connected:

                cg = _asset_feed.snapshot

        elif self._cg_enhanced and self._cg_enhanced.connected:

            cg = self._cg_enhanced.snapshot

        # ── Build window snapshot dict ───────────────────────────────────────
        # TimesFM data for DB
        _tfm_direction = None
        _tfm_confidence = None
        _tfm_predicted_close = None
        _tfm_agreement = None
        if timesfm_forecast and not getattr(timesfm_forecast, 'error', ''):
            _tfm_direction = getattr(timesfm_forecast, 'direction', None)
            _tfm_confidence = getattr(timesfm_forecast, 'confidence', None)
            _tfm_predicted_close = getattr(timesfm_forecast, 'predicted_close', None)
            # Check agreement with v5.7c direction
            _implied_dir = "UP" if delta_pct > 0 else "DOWN"
            _v57c_dir = signal.direction if signal else _implied_dir
            _tfm_agreement = (_tfm_direction == _v57c_dir) if _tfm_direction else None

        window_snapshot = {
            "window_ts": window.window_ts,
            "asset": window.asset,
            "timeframe": tf,
            "open_price": open_price,
            "close_price": current_price,
            "delta_pct": delta_pct,  # Primary delta (chainlink if available, else binance)
            # Multi-source deltas (v7.2)
            "delta_chainlink": delta_chainlink,
            "delta_tiingo": delta_tiingo,
            "delta_binance": delta_binance,
            "price_consensus": price_consensus,
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
            "direction": signal.direction if signal else ("UP" if delta_pct > 0 else "DOWN"),
            "confidence": signal.confidence if signal else None,
            "cg_modifier": signal.cg_modifier if signal else 0.0,
            "trade_placed": False,  # Updated to True downstream if order succeeds
            # TimesFM (v5.8)
            "timesfm_direction": _tfm_direction,
            "timesfm_confidence": _tfm_confidence,
            "timesfm_predicted_close": _tfm_predicted_close,
            "timesfm_agreement": _tfm_agreement,
            # TWAP data (v5.7)
            "twap_delta_pct": twap_result.twap_delta_pct if twap_result else None,
            "twap_direction": twap_result.twap_direction if twap_result else None,
            "twap_gamma_agree": twap_result.twap_gamma_agree if twap_result else None,
            "twap_agreement_score": twap_result.agreement_score if twap_result else None,
            "twap_confidence_boost": twap_result.confidence_boost if twap_result else None,
            "twap_n_ticks": twap_result.n_ticks if twap_result else None,
            "twap_stability": twap_result.twap_stability if twap_result else None,
            "twap_trend_pct": twap_result.trend_pct if twap_result else None,
            "twap_momentum_pct": twap_result.momentum_pct if twap_result else None,
            "twap_gamma_gate": twap_result.gamma_gate if twap_result else None,
            "twap_should_skip": twap_result.should_skip if twap_result else None,
            "twap_skip_reason": twap_result.skip_reason if twap_result else None,
            # Gamma market prices (Polymarket token prices)
            "gamma_up_price": float(window.up_price) if window.up_price is not None else None,
            "gamma_down_price": float(window.down_price) if window.down_price is not None else None,
            "gamma_mid_price": (window.up_price + window.down_price) / 2 if (window.up_price and window.down_price) else None,
            "gamma_spread": abs(window.up_price - window.down_price) if (window.up_price and window.down_price) else None,
            # Shadow trade fields (v5.8.1) — recorded for EVERY window with a signal direction.
            # Even gated/skipped windows get direction + entry price so what-if P&L
            # can be computed in the API without re-running the engine.
            # shadow_trade_direction: implied direction from delta (always set)
            # shadow_trade_entry_price: Gamma price for the implied direction at evaluation time
            "shadow_trade_direction": signal.direction if signal else ("UP" if delta_pct > 0 else "DOWN"),
            "shadow_trade_entry_price": (
                window.up_price if (signal.direction if signal else ("UP" if delta_pct > 0 else "DOWN")) == "UP"
                else window.down_price
            ) if (window.up_price and window.down_price) else None,
            # skip_reason is set AFTER evaluation via _last_skip_reason (accurate)
            # This field is updated in the post-eval block below
            "skip_reason": None,
            "engine_version": "v7.1",
            # Multi-source prices at evaluation time
            "chainlink_open": None,  # Populated async below
            "tiingo_open": None,     # Populated async below
            # v7.1 retroactive: would this window pass v7.1 VPIN+delta gate?
            "v71_would_trade": (
                current_vpin >= 0.45 and abs(delta_pct) >= (
                    0.01 if current_vpin >= _runtime.vpin_cascade_direction_threshold else 0.02
                )
            ),
            "v71_skip_reason": (
                None if (current_vpin >= 0.45 and abs(delta_pct) >= (0.01 if current_vpin >= _runtime.vpin_cascade_direction_threshold else 0.02))
                else (
                    f"VPIN {current_vpin:.3f} < gate 0.45" if current_vpin < 0.45
                    else f"delta {abs(delta_pct):.4f}% < {'0.01' if current_vpin >= _runtime.vpin_cascade_direction_threshold else '0.02'}%"
                )
            ),
            "v71_regime": (
                "CASCADE" if current_vpin >= 0.65
                else "TRANSITION" if current_vpin >= 0.55
                else "NORMAL"
            ),
        }

        # ── Fetch fresh Gamma prices for ALL windows (not just traded ones) ────
        try:
            _slug = f"btc-updown-5m-{window.window_ts}"
            _fresh_up, _fresh_down, _src = await self._fetch_fresh_gamma_price(_slug)
            if _fresh_up is not None and _fresh_down is not None:
                window_snapshot["gamma_up_price"] = _fresh_up
                window_snapshot["gamma_down_price"] = _fresh_down
                self._log.debug("snapshot.gamma_fetched", up=f"${_fresh_up:.3f}", down=f"${_fresh_down:.3f}")
        except Exception:
            pass

        # ── Populate chainlink_open / tiingo_open from already-fetched prices ──
        # _chainlink_price and _tiingo_price were fetched earlier for delta calc.
        if _chainlink_price:
            window_snapshot["chainlink_open"] = _chainlink_price
        if _tiingo_price:
            window_snapshot["tiingo_open"] = _tiingo_price

        # ── DB write (AWAIT so row exists before trade_placed update) ─────────
        if self._db is not None:
            try:
                await self._db.write_window_snapshot(window_snapshot)
            except Exception as exc:
                self._log.warning("db.snapshot_write_failed", error=str(exc)[:80])

        if signal is None:
            # v7.1: Use the actual skip reason set at the point of rejection
            _skip_reason = getattr(self, '_last_skip_reason', '') or ""
            self._last_skip_reason = ""  # Reset after use
            if not _skip_reason:
                _skip_reason = "Signal evaluation returned None (unknown reason)"
            # Update snapshot with actual reason
            window_snapshot["skip_reason"] = _skip_reason
            if self._db:
                try:
                    asyncio.create_task(self._db.update_window_skip_reason(
                        window.window_ts, window.asset, "5m", _skip_reason
                    ))
                except Exception:
                    pass
            self._log.info(
                "evaluate.skip",
                asset=window.asset,
                window_ts=window.window_ts,
                delta_pct=f"{delta_pct:.4f}%",
                reason=_skip_reason[:80],
                entry=f"T-{FIVE_MIN_ENTRY_OFFSET}s",
            )
            if self._alerter:
                try:
                    _implied_dir = "UP" if delta_pct > 0 else "DOWN"

                    async def _send_skip_alert():
                        try:
                            window_id = f"{window.asset}-{window.window_ts}"
                            signal_dict = {
                                "direction": _implied_dir,
                                "delta_pct": delta_pct,
                                "vpin": current_vpin,
                                "regime": _snap_regime,
                            }
                            
                            # Send skip decision (no AI analysis for skipped trades)
                            await self._alerter.send_trade_decision_detailed(
                                window_id=window_id,
                                signal=signal_dict,
                                decision="SKIP",
                                reason=_skip_reason[:100],
                                gamma_up=window_snapshot.get("gamma_up_price"),
                                gamma_down=window_snapshot.get("gamma_down_price"),
                            )
                        except Exception as alert_exc:
                            self._log.error("alert.skip_decision_failed", error=str(alert_exc), window_ts=window.window_ts)

                    asyncio.create_task(_send_skip_alert())
                except Exception:
                    pass
            return

        # ── Send trade decision + dual-AI analysis (non-blocking) ──────────────
        if self._alerter:
            async def _send_trade_alert():
                try:
                    window_id = f"{window.asset}-{window.window_ts}"
                    signal_dict = {
                        "direction": signal.direction,
                        "delta_pct": delta_pct,
                        "vpin": current_vpin,
                        "regime": _snap_regime,
                    }
                    reason = f"VPIN {current_vpin:.3f} ({_snap_regime}), delta {delta_pct:+.4f}%"
                    
                    # Send decision + AI analysis (separated for timeout resilience)
                    await self._alerter.send_trade_decision_detailed(
                        window_id=window_id,
                        signal=signal_dict,
                        decision="TRADE",
                        reason=reason,
                        gamma_up=window_snapshot.get("gamma_up_price"),
                        gamma_down=window_snapshot.get("gamma_down_price"),
                    )
                except Exception as alert_exc:
                    self._log.error("alert.trade_decision_failed", error=str(alert_exc), window_ts=window.window_ts)
            asyncio.create_task(_send_trade_alert())

        # Execute trade
        await self._execute_trade(state, signal)
        
        # Claude AI evaluation (non-blocking, 1min timeout)
        # Fetches FRESH Gamma price before evaluation so Claude sees real-time data
        if self._claude_eval and signal:
            try:
                _cg_dict = {}
                if cg:
                    _cg_dict = {
                        "oi_usd": cg.oi_usd, "oi_delta_pct": cg.oi_delta_pct_1m,
                        "long_pct": cg.long_pct, "short_pct": cg.short_pct,
                        "top_short_pct": cg.top_position_short_pct,
                        "funding_rate": cg.funding_rate,
                        "taker_buy": cg.taker_buy_volume_1m,
                        "taker_sell": cg.taker_sell_volume_1m,
                    }

                async def _eval_with_fresh_gamma():
                    """Fetch fresh Gamma price, then run Claude evaluation."""
                    slug = f"{window.asset.lower()}-updown-{tf}-{window.window_ts}"
                    fresh_up, fresh_down, source = await self._fetch_fresh_gamma_price(slug)

                    # Use fresh price if available, fall back to window price (stale)
                    if fresh_up is not None:
                        gamma_price = fresh_up if signal.direction == "UP" else fresh_down
                        price_tag = "LIVE"
                        stale_price = window.up_price if signal.direction == "UP" else (window.down_price or 0.50)
                        drift = abs(gamma_price - stale_price) if stale_price else 0
                        self._log.info(
                            "claude_eval.fresh_gamma",
                            asset=window.asset,
                            fresh=f"${gamma_price:.4f}",
                            stale=f"${stale_price:.4f}" if stale_price else "n/a",
                            drift=f"${drift:.4f}",
                            source=source,
                        )
                    else:
                        gamma_price = window.up_price if signal.direction == "UP" else (window.down_price or 0.50)
                        price_tag = "SYN" if window.price_source == "synthetic" else "STALE"
                        self._log.warning(
                            "claude_eval.using_stale_gamma",
                            asset=window.asset,
                            price=f"${gamma_price:.4f}",
                            source=window.price_source,
                        )

                    await self._claude_eval.evaluate_trade_decision(
                        asset=window.asset,
                        timeframe=tf,
                        direction=signal.direction,
                        confidence=signal.confidence,
                        delta_pct=signal.delta_pct,
                        vpin=signal.current_vpin,
                        regime=_snap_regime,
                        cg_snapshot=_cg_dict,
                        token_price=gamma_price,
                        gamma_bestask=gamma_price,
                        window_open_price=open_price,
                        current_price=current_price,
                        trade_placed=True,
                        price_source=price_tag,
                    )

                asyncio.create_task(_eval_with_fresh_gamma())
            except Exception:
                pass
        
        # Track executed window
        self._last_executed_window = window_key

    def _evaluate_signal(
        self,
        window: WindowInfo,
        current_price: float,
        current_vpin: float,
        delta_pct: float,
        twap_result: Optional[TWAPResult] = None,
        timesfm_forecast=None,
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
        # When VPIN is 0 or below gate, we'd be relying solely on TimesFM (no informed flow).
        # TIMESFM_ONLY regime: skip entirely rather than trade on model forecast alone.
        if current_vpin < runtime.five_min_vpin_gate:
            self._log.info(
                "evaluate.skip_timesfm_only_regime",
                vpin=f"{current_vpin:.3f}",
                gate=f"{runtime.five_min_vpin_gate:.3f}",
                reason="VPIN below gate — no informed flow, would be TIMESFM_ONLY regime, skipping",
            )
            self._last_skip_reason = f"VPIN {current_vpin:.3f} < gate {runtime.five_min_vpin_gate} — TIMESFM_ONLY regime, no informed flow"
            return None

        # ── TWAP Gamma Gate (v5.7c) ───────────────────────────────
        # Block trades where the market strongly disagrees with our direction
        # or where the token is already >60¢ (priced in, bad R/R)
        if twap_result and twap_result.should_skip and twap_result.n_ticks >= 5:
            self._log.info(
                "evaluate.twap_gate_blocked",
                reason=twap_result.skip_reason,
                gamma_gate=twap_result.gamma_gate,
                trend_pct=f"{twap_result.trend_pct:.2f}",
                twap_delta=f"{twap_result.twap_delta_pct:+.4f}%",
                asset=window.asset,
            )
            self._last_skip_reason = f"TWAP GATE: {twap_result.skip_reason or 'market disagrees'} (gate={twap_result.gamma_gate})"
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
                self._last_skip_reason = f"CASCADE: delta {abs(delta_pct):.4f}% < scaled threshold {min_delta:.4f}% (VPIN {current_vpin:.3f})"
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
            # TRANSITION: v5.1 ALL MOMENTUM (contrarian = coin flip per 30-day data)
            if abs(delta_pct) < runtime.five_min_min_delta_pct:
                self._last_skip_reason = f"TRANSITION: delta {abs(delta_pct):.4f}% < threshold {runtime.five_min_min_delta_pct:.4f}% (VPIN {current_vpin:.3f})"
                return None
            direction = "UP" if delta_pct > 0 else "DOWN"
            regime = "TRANSITION"
        else:
            # NORMAL: v5.1 ALL MOMENTUM (contrarian = coin flip per 30-day data)
            if abs(delta_pct) < runtime.five_min_min_delta_pct:
                self._last_skip_reason = f"NORMAL: delta {abs(delta_pct):.4f}% < threshold {runtime.five_min_min_delta_pct:.4f}% (VPIN {current_vpin:.3f})"
                return None
            direction = "UP" if delta_pct > 0 else "DOWN"
            regime = "NORMAL"
        
        # ── TWAP Direction Override (v5.7) ────────────────────────────────
        # If TWAP-delta computed a direction, use it when agreement is strong.
        # TWAP is more robust than point delta (smoothed over the window).
        # 
        # Rules:
        #   1. If TWAP + Gamma + point all agree → use that direction (strongest)
        #   2. If TWAP + Gamma agree but point disagrees → use TWAP (point is noisy)
        #   3. If TWAP disagrees with both → stick with point delta (TWAP may be lagging)
        #
        # The confidence_boost from TWAP is applied AFTER base confidence calculation.
        _twap_overrode = False
        if twap_result and twap_result.n_ticks >= 5:
            if twap_result.all_agree:
                # All three agree — highest confidence, use recommended direction
                if twap_result.recommended_direction != direction:
                    self._log.info(
                        "evaluate.twap_override",
                        old_dir=direction,
                        new_dir=twap_result.recommended_direction,
                        reason="all_agree",
                        agreement=f"{twap_result.agreement_score}/3",
                    )
                    direction = twap_result.recommended_direction
                    _twap_overrode = True
            elif twap_result.twap_gamma_agree and twap_result.gamma_direction:
                # TWAP + Gamma agree — strong signal, override point delta
                if twap_result.recommended_direction != direction:
                    self._log.info(
                        "evaluate.twap_override",
                        old_dir=direction,
                        new_dir=twap_result.recommended_direction,
                        reason="twap_gamma_agree",
                        twap_delta=f"{twap_result.twap_delta_pct:+.4f}%",
                        gamma_skew=f"{twap_result.gamma_skew:.3f}",
                    )
                    direction = twap_result.recommended_direction
                    _twap_overrode = True
            # If only TWAP disagrees with point+gamma, don't override — point is fresher

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
            # ── v5.1 DATA-DRIVEN CG MODIFIERS ─────────────────────────────
            # 30-day backtest (8,640 windows) proved:
            #   - L/S ratio: coin flip at ANY threshold (49-51%)
            #   - Crowd positioning: coin flip
            #   - Smart money divergence: insufficient samples
            #   - Taker aggression: not validated within-window
            #   - Funding rate: coin flip
            #   - OI delta >0.10%: +2.9% WR lift (ONLY signal that works)
            #
            # ZEROED: liq, crowd, smart money, taker, funding
            # KEPT: OI delta as sole confirmer
            # ALL data still logged + saved for future analysis

            # OI Delta confirmation — the only CG signal backed by data
            # Rising OI >0.10% confirms real position changes backing the move
            if abs(cg.oi_delta_pct_1m) > 0.001:  # >0.10% OI change
                cg_confidence_modifier += 0.10
                cg_log_parts.append(f"oi_delta_confirms(+0.10, OI_Δ={cg.oi_delta_pct_1m:.3f}%)")

            # ── CoinGlass VETO SYSTEM v7.1 ────────────────────────────────
            # Fires when 2+ signals strongly oppose our direction.
            # (Was 3+ in v5.4d — too loose. Apr 5 trade slipped through with only
            #  1 trigger despite 3 independent bullish signals against a DOWN bet.)
            #
            # Changes from v5.4d → v7.1:
            #   1. Veto threshold: 3+ → 2+  (was too forgiving)
            #   2. Smart money threshold: 55% → 52%  (catch near-majority divergence)
            #   3. Taker threshold: 65% → 60%  (catch clear directional flow)
            #   4. Funding bug fixed: was only checking negative funding for DOWN veto.
            #      Strongly POSITIVE funding (longs paying huge premium) is bullish —
            #      it should also veto a DOWN bet. Threshold: >100% annualised.
            #   5. New: CASCADE + taker divergence. If VPIN ≥ 0.65 (high informed flow)
            #      but taker flow opposes direction >55%, that's the worst case — VPIN
            #      detected real flow but takers are telling us which WAY it goes.
            #
            # Replay of Apr 5 DOWN trade (LOSS):
            #   taker_buying=66.2% > 60%         → veto +1
            #   smart_money_long=54% > 52%        → veto +1
            #   CASCADE_taker_divergence           → veto +1
            #   Total: 3 → TRADE WOULD HAVE BEEN BLOCKED ✓

            _veto_count = 0
            _veto_reasons = []

            # 1. Smart money opposing: top traders >52% on the other side (was 55%)
            if direction == "UP" and cg.top_position_short_pct > 52:
                _veto_count += 1
                _veto_reasons.append(f"smart_money_short={cg.top_position_short_pct:.0f}%")
            elif direction == "DOWN" and cg.top_position_long_pct > 52:
                _veto_count += 1
                _veto_reasons.append(f"smart_money_long={cg.top_position_long_pct:.0f}%")

            # 2. Funding opposing — FIXED: check both directions properly
            # For DOWN bet: both strong positive AND strong negative funding can be signals
            # Strong POSITIVE funding = longs paying big premium = bullish conviction against DOWN
            # Strong NEGATIVE funding = not relevant for DOWN veto (shorts paying = bearish = confirms DOWN)
            _funding_annual = cg.funding_rate * 3 * 365  # 8h rate → annualised
            if direction == "UP" and cg.funding_rate > 0.0005:    # longs paying → bearish → against UP
                _veto_count += 1
                _veto_reasons.append(f"funding_against_up={_funding_annual:.0f}%/yr")
            elif direction == "DOWN" and _funding_annual > 1.0:       # >100%/yr longs paying → bullish → against DOWN
                _veto_count += 1
                _veto_reasons.append(f"funding_bullish_vs_down={_funding_annual:.0f}%/yr")
            elif direction == "DOWN" and cg.funding_rate < -0.0005:  # shorts paying heavily → bearish → confirms DOWN (no veto)
                pass  # This confirms our DOWN bet, not opposes it

            # 3. Crowd overleveraged in opposing direction (unchanged, >60%)
            if direction == "UP" and cg.long_pct > 60:
                _veto_count += 1
                _veto_reasons.append(f"crowd_overleveraged_long={cg.long_pct:.0f}%")
            elif direction == "DOWN" and cg.short_pct > 60:
                _veto_count += 1
                _veto_reasons.append(f"crowd_overleveraged_short={cg.short_pct:.0f}%")

            # 4. Taker volume opposing (was >65%, now >60%)
            _taker_total = cg.taker_buy_volume_1m + cg.taker_sell_volume_1m
            if _taker_total > 0:
                _sell_pct = cg.taker_sell_volume_1m / _taker_total * 100
                _buy_pct = 100 - _sell_pct
                if direction == "UP" and _sell_pct > 60:
                    _veto_count += 1
                    _veto_reasons.append(f"taker_selling={_sell_pct:.0f}%")
                elif direction == "DOWN" and _buy_pct > 60:
                    _veto_count += 1
                    _veto_reasons.append(f"taker_buying={_buy_pct:.0f}%")

            # 5. NEW: CASCADE + taker divergence (worst case — VPIN says "big player"
            #    but takers say "they're going the other way")
            if _taker_total > 0:
                if current_vpin >= 0.65:
                    if direction == "UP" and _sell_pct > 55:
                        _veto_count += 1
                        _veto_reasons.append(f"cascade_taker_divergence: vpin={current_vpin:.2f} but sell={_sell_pct:.0f}%")
                    elif direction == "DOWN" and _buy_pct > 55:
                        _veto_count += 1
                        _veto_reasons.append(f"cascade_taker_divergence: vpin={current_vpin:.2f} but buy={_buy_pct:.0f}%")

            # VETO: 3+ signals opposing = block (restored from v5.4d)
            if _veto_count >= 3:
                self._log.warning(
                    "evaluate.cg_veto",
                    direction=direction,
                    veto_count=_veto_count,
                    reasons=", ".join(_veto_reasons),
                    asset=window.asset,
                )
                self._last_skip_reason = f"CG VETO ({_veto_count} signals): {', '.join(_veto_reasons)}"
                return None
            
            # Log "would have blocked" for tracking — even when not vetoing
            if _veto_count == 1:
                self._log.info(
                    "evaluate.cg_would_warn",
                    direction=direction,
                    veto_count=_veto_count,
                    reasons=", ".join(_veto_reasons),
                    asset=window.asset,
                )

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

        # ── TWAP Confidence Adjustment (v5.7) ──────────────────────────────
        # Apply TWAP boost/penalty to confidence level
        if twap_result and twap_result.n_ticks >= 5:
            _boost = twap_result.confidence_boost
            if _boost >= 0.08 and confidence == "LOW":
                self._log.info(
                    "evaluate.twap_lift",
                    from_confidence="LOW",
                    to_confidence="MODERATE",
                    boost=f"{_boost:+.2f}",
                    agreement=f"{twap_result.agreement_score}/3",
                )
                confidence = "MODERATE"
            elif _boost <= -0.10 and confidence == "MODERATE":
                self._log.info(
                    "evaluate.twap_suppress",
                    from_confidence="MODERATE",
                    to_confidence="LOW",
                    boost=f"{_boost:+.2f}",
                    reason="TWAP disagrees or unstable",
                )
                confidence = "LOW"

        # Block NONE and LOW confidence — only trade MODERATE or HIGH
        if confidence in ("NONE", "LOW"):
            return None

        # ── v5.8: TimesFM Agreement Check (DATA-ONLY — NOT A GATE) ───────────
        # TimesFM is monitored for analysis but does NOT gate, skip, or modify
        # confidence. All agreement data is recorded in the snapshot for
        # post-hoc analysis and what-if P&L tracking.
        #
        # Previously (pre-v5.8.1):
        #   - Agree + ≥70% conf → lift MODERATE→HIGH
        #   - Disagree + ≥70% conf → SKIP (return None)
        #   - Disagree + <70% conf → suppress HIGH→MODERATE
        #
        # NOW: Log for monitoring only. No gating effect whatsoever.
        timesfm_agreement = None  # None = no forecast available
        if timesfm_forecast and not timesfm_forecast.error and timesfm_forecast.direction and window.asset == "BTC":
            _tfm_conf = timesfm_forecast.confidence or 0.0
            if timesfm_forecast.direction == direction:
                timesfm_agreement = True
                # DATA-ONLY: log agreement for monitoring, do not modify confidence
                self._log.info(
                    "evaluate.timesfm_agrees",
                    v57c_dir=direction,
                    tfm_dir=timesfm_forecast.direction,
                    tfm_conf=f"{_tfm_conf:.2f}",
                    confidence=confidence,
                    note="TimesFM agrees — data-only, no gate effect",
                )
            else:
                timesfm_agreement = False
                # DATA-ONLY: log disagreement for monitoring, do not skip or suppress
                self._log.info(
                    "evaluate.timesfm_disagrees",
                    v57c_dir=direction,
                    tfm_dir=timesfm_forecast.direction,
                    tfm_conf=f"{_tfm_conf:.2f}",
                    confidence=confidence,
                    note="TimesFM disagrees — data-only, no gate effect, proceeding",
                )

        self._log.info(
            "evaluate.regime_signal",
            regime=regime,
            vpin=f"{current_vpin:.3f}",
            delta=f"{delta_pct:+.4f}%",
            direction=direction,
            confidence=confidence,
            cg_modifier=f"{cg_confidence_modifier:+.2f}",
            twap_agree=f"{twap_result.agreement_score}/3" if twap_result else "n/a",
            twap_boost=f"{twap_result.confidence_boost:+.2f}" if twap_result else "n/a",
            twap_overrode=_twap_overrode if '_twap_overrode' in dir() else False,
            timesfm_agreement=timesfm_agreement,
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

        # v7.1: Update window_snapshot with trade_placed = True
        if self._db is not None:
            try:
                asyncio.create_task(self._db.update_window_trade_placed(
                    window_ts=signal.window_ts, asset=signal.asset, timeframe="5m"
                ))
            except Exception:
                pass

        # Post-trade fill verification — NO RETRY, NO BUMPING
        # Apr 2 lesson: "Fill rate doesn't matter if fills are at bad prices"
        # Single order at good price, accept miss if no fill.
        if not self._poly.paper_mode and order.order_id.startswith("0x"):
            POLL_INTERVAL = 5
            MAX_WAIT = 30
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
                    
                    # Immediate Telegram notification on MATCHED
                    self._log.info("trade.fill_status", filled=filled, has_alerter=bool(self._alerter), elapsed=elapsed)
                    if filled and self._alerter:
                        try:
                            _shares = float(size_matched)
                            _fill_px = round(order.stake_usd / _shares, 4) if _shares > 0 else 0
                            _rr = round((1 - _fill_px) / _fill_px, 1) if _fill_px > 0 else 0
                            _profit_if_win = round((1 - _fill_px) * _shares * 0.98, 2)
                            _dir = "DOWN" if order.direction == "NO" else "UP"
                            _mode = "📄 PAPER" if self._poly.paper_mode else "🔴 LIVE"
                            _oid = order.order_id
                            _stake = order.stake_usd

                            # Use explicit parameters to avoid closure capture bugs
                            async def _send_fill_notif(
                                _mode=_mode, _dir=_dir, _fill_px=_fill_px,
                                _shares=_shares, _stake=_stake, _rr=_rr,
                                _profit_if_win=_profit_if_win, _elapsed=elapsed,
                                _oid=_oid,
                            ):
                                try:
                                    # Main notification
                                    await self._alerter._send_with_id(
                                        f"💰 *BET PLACED — FILLED*  {_mode}\n"
                                        f"`{_oid[:20]}...`\n\n"
                                        f"Direction: `{_dir}`\n"
                                        f"Fill: `${_fill_px:.3f}` × `{_shares:.1f}` shares\n"
                                        f"Cost: `${_stake:.2f}`\n"
                                        f"R/R: `1:{_rr}` | If WIN: `+${_profit_if_win:.2f}`\n"
                                        f"Fill time: `{_elapsed}s`"
                                    )
                                    # Brief AI analysis on the fill
                                    try:
                                        _prompt = (
                                            f"BTC 5-min bet just FILLED on Polymarket. {_dir} @ ${_fill_px:.3f}, "
                                            f"{_shares:.0f} shares, ${_stake:.0f} stake. "
                                            f"R/R is 1:{_rr}. If win: +${_profit_if_win:.2f}. "
                                            f"In 1 sentence: is this a good fill price and what's the likely outcome?"
                                        )
                                        _ai_text, _ai_src = await self._alerter._ai.assess(_prompt, timeout_s=8)
                                        await self._alerter._send_with_id(
                                            f"🤖 *Fill Analysis* — `{_ai_src.upper()}`\n_{_ai_text}_"
                                        )
                                    except Exception:
                                        pass
                                except Exception as _inner_exc:
                                    self._log.error("trade.fill_notif_inner_error", error=str(_inner_exc)[:100])

                            asyncio.create_task(_send_fill_notif())
                            self._log.info("trade.fill_notif_spawned")
                        except Exception as _notif_err:
                            self._log.error("trade.fill_notif_error", error=str(_notif_err)[:100])
                    
                    self._log.info(
                        "trade.fill_check",
                        order_id=order.order_id[:20] + "...",
                        clob_status=clob_status,
                        size_matched=size_matched,
                        filled=filled,
                        elapsed=f"{elapsed}s",
                    )
                    
                    if filled or clob_status not in ("LIVE", "UNKNOWN"):
                        break

                order.metadata["filled"] = filled
                order.metadata["fill_wait_seconds"] = elapsed

                if filled:
                    # Update entry_price with actual fill price
                    try:
                        _shares = float(size_matched)
                        if _shares > 0:
                            _fill_price = round(order.stake_usd / _shares, 4)
                            order.price = str(_fill_price)
                            order.entry_price = _fill_price
                            order.metadata["actual_fill_price"] = _fill_price
                            self._log.info("trade.verified", order_id=order.order_id[:20] + "...", actual_price=f"${_fill_price:.4f}", size_matched=size_matched, wait=f"{elapsed}s")
                            # Persist actual fill price to DB
                            if self._db:
                                try:
                                    await self._db.write_trade(order)
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    if self._alerter:
                        asyncio.create_task(self._alerter.send_entry_alert(order))
                else:
                    self._log.warning("trade.not_filled_no_retry", order_id=order.order_id[:20], waited=f"{elapsed}s")
                    # Notify on Telegram that order is sitting unfilled
                    if self._alerter:
                        try:
                            asyncio.create_task(self._alerter._send_with_id(
                                f"⏳ *ORDER UNFILLED* — {order.order_id[:16]}...\n"
                                f"Sitting on book after {elapsed}s. GTD will expire at window close.\n"
                                f"No retry — accepting miss at this price."
                            ))
                        except Exception:
                            pass
            except Exception as exc:
                self._log.warning("trade.verify_failed", error=str(exc))
        
        if self._alerter:
            asyncio.create_task(self._alerter.send_entry_alert(order))

    # ─── Guardrail Helpers ────────────────────────────────────────────────────

    def _check_rate_limit(self) -> tuple[bool, str]:
        """
        G4: Check order rate limiting.
        Returns (allowed, reason). reason is empty string when allowed.
        """
        now = time.time()

        # Purge timestamps older than 1 hour
        cutoff = now - 3600.0
        self._order_timestamps = [ts for ts in self._order_timestamps if ts > cutoff]

        # Check minimum interval between orders
        if self._last_order_time > 0:
            elapsed = now - self._last_order_time
            if elapsed < runtime.min_order_interval_seconds:
                return False, (
                    f"rate_limit.too_fast: {elapsed:.1f}s since last order "
                    f"(min {runtime.min_order_interval_seconds:.0f}s)"
                )

        # Check hourly cap
        if len(self._order_timestamps) >= runtime.max_orders_per_hour:
            return False, (
                f"rate_limit.hourly_cap: {len(self._order_timestamps)} orders "
                f"in last hour (max {runtime.max_orders_per_hour})"
            )

        return True, ""

    def _record_order_placed(self) -> None:
        """G4: Record that an order was placed now."""
        now = time.time()
        self._order_timestamps.append(now)
        self._last_order_time = now

    def _check_circuit_breaker(self) -> tuple[bool, str]:
        """
        G5: Check if the circuit breaker is active.
        Returns (allowed, reason).
        """
        now = time.time()
        if self._circuit_break_until > now:
            remaining = self._circuit_break_until - now
            return False, f"circuit_breaker.active: {remaining:.0f}s remaining"
        return True, ""

    def _on_order_error(self, error: Exception) -> None:
        """G5: Handle an order error — activate circuit breaker if needed."""
        now = time.time()
        error_str = str(error)

        # Check for 4xx errors
        is_4xx = any(str(code) in error_str for code in range(400, 500))
        is_error = True  # Any exception counts as consecutive error

        if is_4xx:
            self._circuit_break_until = now + 60  # 60 seconds (was 15 min)
            self._consecutive_errors = 0
            self._log.error(
                "guardrail.circuit_breaker.4xx",
                error=error_str[:200],
                break_until=self._circuit_break_until,
                break_minutes=15,
            )
        elif is_error:
            self._consecutive_errors += 1
            if self._consecutive_errors >= 3:
                self._circuit_break_until = now + 180  # 3 minutes (was 1 hour)
                self._log.error(
                    "guardrail.circuit_breaker.consecutive",
                    consecutive_errors=self._consecutive_errors,
                    break_until=self._circuit_break_until,
                    break_minutes=60,
                )
            else:
                self._log.warning(
                    "guardrail.circuit_breaker.error_count",
                    consecutive_errors=self._consecutive_errors,
                    breaks_at=3,
                )

    def _on_order_success(self) -> None:
        """G5: Reset consecutive error counter on successful order."""
        self._consecutive_errors = 0

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
        
        # Price: SMART LIMIT PRICING (v5.4b)
        # Strategy: Use Gamma bestAsk as limit price if within our cap.
        # This gets us REAL market fills. If bestAsk > cap, skip.
        # The paper winning streak used tokens at $0.45-$0.51 — aim for that.
        
        _model_price = self._delta_to_token_price(signal.delta_pct)
        _fresh_best_ask = None
        _tf_str = "15m" if window.duration_secs == 900 else "5m"
        _slug = f"{window.asset.lower()}-updown-{_tf_str}-{window.window_ts}"
        _fresh_up = None
        _fresh_down = None
        try:
            import aiohttp as _aiohttp
            async with _aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as _sess:
                _url = f"https://gamma-api.polymarket.com/events?slug={_slug}"
                async with _sess.get(_url, timeout=_aiohttp.ClientTimeout(total=5)) as _resp:
                    if _resp.status == 200:
                        _data = await _resp.json()
                        if _data and isinstance(_data, list) and _data[0].get("markets"):
                            _mkt = _data[0]["markets"][0]
                            _up_ask = _mkt.get("bestAsk")     # UP token ask price
                            _up_bid = _mkt.get("bestBid")     # UP token bid price
                            _outcome_prices_raw = _mkt.get("outcomePrices", [])
                            # outcomePrices can be a JSON string or a list
                            if isinstance(_outcome_prices_raw, str):
                                import json as _json
                                _outcome_prices = _json.loads(_outcome_prices_raw)
                            else:
                                _outcome_prices = _outcome_prices_raw
                            
                            if _outcome_prices and len(_outcome_prices) >= 2:
                                # Use outcome prices directly — most accurate
                                _fresh_up = float(_outcome_prices[0])    # UP price
                                _fresh_down = float(_outcome_prices[1])  # DOWN price
                            elif _up_ask is not None and _up_bid is not None:
                                _fresh_up = float(_up_ask)
                                # DOWN effective ask = 1 - UP bid (buying NO = selling YES at bid)
                                _fresh_down = round(1.0 - float(_up_bid), 4)
                            
                            if _fresh_up is not None and _fresh_down is not None:
                                _fresh_best_ask = _fresh_up if direction == "YES" else _fresh_down
                                self._log.info(
                                    "execute.gamma_prices",
                                    up_price=f"${_fresh_up:.4f}",
                                    down_price=f"${_fresh_down:.4f}",
                                    direction=direction,
                                    using=f"${_fresh_best_ask:.4f}",
                                )
                                # Store fresh T-60 gamma prices to snapshot
                                if self._db:
                                    try:
                                        asyncio.create_task(self._db.update_gamma_prices(
                                            window_ts=signal.window_ts, asset=signal.asset, timeframe="5m",
                                            gamma_up=_fresh_up, gamma_down=_fresh_down,
                                        ))
                                    except Exception:
                                        pass
        except Exception as _exc:
            self._log.debug("execute.gamma_fetch_error", error=str(_exc))
        
        # Use Gamma bestAsk if available and within cap, else model price
        is_15m = "15m" in _slug
        _max_price = runtime.fifteen_min_max_entry_price if is_15m else runtime.five_min_max_entry_price
        
        # FOK PRICING (v5.5b): Buy at whatever the market offers, up to our cap
        # FOK order at cap price → fills at best available (could be much cheaper)
        # If Gamma bestAsk > cap → skip (market too expensive)
        # If Gamma bestAsk <= cap → use cap as limit, FOK fills at actual market price
        
        # _fresh_best_ask = real market price for the token we want to buy
        # _max_price = our cap (max we'll pay)
        # token_price = what we record/display (real market price)
        # _fok_limit = what we submit to FOK (the cap — fills at best available)
        
        _fok_limit = _max_price  # Always submit FOK at cap
        
        # FLOOR CHECK: if Gamma bestAsk is below $0.30, the market strongly disagrees
        # with our direction. Don't trade — it's priced cheap for a reason.
        _min_entry = 0.30
        if _fresh_best_ask is not None and _fresh_best_ask < _min_entry:
            _skip_msg = f"PRICE FLOOR: entry ${_fresh_best_ask:.3f} < floor ${_min_entry} — market strongly disagrees, adverse selection risk"
            self._log.warning("execute.price_below_floor", bestask=f"${_fresh_best_ask:.4f}", floor=f"${_min_entry}")
            if self._alerter:
                try:
                    _mode = "📄 PAPER" if self._poly.paper_mode else "🔴 LIVE"
                    asyncio.create_task(self._alerter._send_with_id(
                        f"🚫 *TRADE BLOCKED — PRICE FLOOR*  {_mode}\n"
                        f"`{signal.asset}-{signal.window_ts}`\n\n"
                        f"Direction: `{direction}`\n"
                        f"Entry price: `${_fresh_best_ask:.3f}` ← below floor\n"
                        f"Floor: `${_min_entry}`\n"
                        f"_Market is {(1-_fresh_best_ask)*100:.0f}% confident we're wrong_"
                    ))
                except Exception:
                    pass
            if self._db:
                try:
                    asyncio.create_task(self._db.update_window_skip_reason(
                        signal.window_ts, signal.asset, "5m", _skip_msg
                    ))
                except Exception:
                    pass
            return

        if _fresh_best_ask is not None and _fresh_best_ask >= _min_entry and _fresh_best_ask <= _max_price:
            # Market price within cap — record real price, submit at cap
            token_price = _fresh_best_ask  # Record REAL market price
            _price_source = f"market={_fresh_best_ask:.4f}(fok_limit={_max_price})"
        elif _fresh_best_ask is not None and _fresh_best_ask > _max_price:
            _skip_msg = f"PRICE CAP: entry ${_fresh_best_ask:.3f} > cap ${_max_price:.2f} — too expensive, bad R/R"
            self._log.warning(
                "execute.price_above_cap",
                bestask=f"${_fresh_best_ask:.4f}",
                cap=f"${_max_price:.2f}",
            )
            # Telegram notification for cap-blocked trade
            if self._alerter:
                try:
                    _mode = "📄 PAPER" if self._poly.paper_mode else "🔴 LIVE"
                    asyncio.create_task(self._alerter._send_with_id(
                        f"🚫 *TRADE BLOCKED — PRICE CAP*  {_mode}\n"
                        f"`{signal.asset}-{signal.window_ts}`\n\n"
                        f"Direction: `{direction}`\n"
                        f"Entry price: `${_fresh_best_ask:.3f}` ← above cap\n"
                        f"Cap: `${_max_price:.2f}`\n"
                        f"R/R: risk `${_fresh_best_ask:.2f}` to win `${1-_fresh_best_ask:.2f}` ({((1-_fresh_best_ask)/_fresh_best_ask*100):.0f}% return)\n\n"
                        f"_Trade would have proceeded if price ≤ ${_max_price:.2f}_"
                    ))
                except Exception:
                    pass
            # Record in DB
            if self._db:
                try:
                    asyncio.create_task(self._db.update_window_skip_reason(
                        signal.window_ts, signal.asset, "5m", _skip_msg
                    ))
                except Exception:
                    pass
            return
        elif _model_price <= _max_price and _model_price >= 0.30:
            token_price = min(_model_price, _max_price)  # Best estimate
            _price_source = f"model={_model_price:.4f}(fok_limit={_max_price})"
        else:
            _skip_msg = f"PRICE OUT OF RANGE: model=${_model_price:.4f}, bestask=${_fresh_best_ask:.4f if _fresh_best_ask else 'n/a'}, cap=${_max_price:.2f}"
            self._log.warning(
                "execute.price_out_of_range",
                model=f"${_model_price:.4f}",
                bestask=f"${_fresh_best_ask:.4f}" if _fresh_best_ask else "n/a",
                cap=f"${_max_price:.2f}",
            )
            # Telegram notification
            if self._alerter:
                try:
                    _mode = "📄 PAPER" if self._poly.paper_mode else "🔴 LIVE"
                    asyncio.create_task(self._alerter._send_with_id(
                        f"🚫 *TRADE BLOCKED — PRICE OUT OF RANGE*  {_mode}\n"
                        f"`{signal.asset}-{signal.window_ts}`\n\n"
                        f"Model: `${_model_price:.4f}` | BestAsk: `${_fresh_best_ask:.4f if _fresh_best_ask else 'n/a'}`\n"
                        f"Cap: `${_max_price:.2f}`\n\n"
                        f"_No valid entry price found_"
                    ))
                except Exception:
                    pass
            if self._db:
                try:
                    asyncio.create_task(self._db.update_window_skip_reason(
                        signal.window_ts, signal.asset, "5m", _skip_msg
                    ))
                except Exception:
                    pass
            return
        
        self._log.info(
            "execute.smart_pricing",
            direction=direction,
            used_price=f"${token_price:.4f}",
            source=_price_source,
            model_price=f"${_model_price:.4f}",
            gamma_bestask=f"${_fresh_best_ask:.4f}" if _fresh_best_ask else "n/a",
            cap=f"${_max_price:.2f}",
        )
        
        price = Decimal(str(round(token_price, 4)))        # Real market price (for records/display)
        _fok_price = Decimal(str(round(_fok_limit, 4)))   # Cap price (for FOK order submission)
        tf = "15m" if window.duration_secs == 900 else "5m"
        market_slug = f"{window.asset.lower()}-updown-{tf}-{window.window_ts}"
        
        # ── G6: Geoblock check ────────────────────────────────────────────────
        if self._geoblock_check_fn and self._geoblock_check_fn():
            self._log.error("guardrail.geoblock.blocked")
            if self._alerter:
                asyncio.create_task(self._alerter.send_system_alert(
                    "🚨 GEOBLOCK: Trading blocked. Skipping order.",
                    level="critical",
                ))
            return

        # ── G5: Circuit breaker check ─────────────────────────────────────────
        cb_allowed, cb_reason = self._check_circuit_breaker()
        if not cb_allowed:
            self._log.warning("guardrail.circuit_breaker.blocked", reason=cb_reason)
            if self._alerter:
                asyncio.create_task(self._alerter.send_system_alert(
                    f"⚡ CIRCUIT BREAKER: Trade blocked — {cb_reason}",
                    level="warning",
                ))
            return

        # ── G4: Rate limiter check ────────────────────────────────────────────
        rl_allowed, rl_reason = self._check_rate_limit()
        if not rl_allowed:
            self._log.warning("guardrail.rate_limit.blocked", reason=rl_reason)
            if self._alerter:
                asyncio.create_task(self._alerter.send_system_alert(
                    f"🚦 RATE LIMIT: Trade blocked — {rl_reason}",
                    level="warning",
                ))
            return

        # Place order — TRY RFQ FIRST (UpDown tokens have no CLOB book)
        # Then fall back to GTC limit if RFQ fails
        clob_order_id = None
        _rfq_fill_price = None
        _used_rfq = False
        
        if not self._poly.paper_mode:
            # Calculate size in shares using real market price
            _shares = stake / float(price) if float(price) > 0 else 0
            is_15m = "15m" in market_slug
            _rfq_cap = runtime.fifteen_min_max_entry_price if is_15m else runtime.five_min_max_entry_price
            
            try:
                rfq_id, rfq_price = await self._poly.place_rfq_order(
                    token_id=token_id,
                    direction=direction,
                    price=float(price),  # Gamma bestAsk — client applies mode (cap or bestask)
                    size=_shares,
                    max_price=_rfq_cap,
                )
                if rfq_id:
                    clob_order_id = rfq_id
                    _rfq_fill_price = rfq_price
                    _used_rfq = True
                    self._log.info(
                        "trade.rfq_filled",
                        order_id=str(rfq_id)[:20],
                        fill_price=f"${rfq_price:.4f}" if rfq_price else "n/a",
                    )
                    self._record_order_placed()
                    self._on_order_success()
                else:
                    self._log.info("trade.rfq_no_fill_trying_clob")
            except Exception as rfq_exc:
                self._log.warning("trade.rfq_error", error=str(rfq_exc)[:100])
        
        # Fall back to GTC limit if RFQ didn't fill
        if not clob_order_id:
            try:
                clob_order_id = await self._poly.place_order(
                    market_slug=market_slug,
                    direction=direction,
                    price=price,  # Gamma bestAsk — client applies mode
                    stake_usd=stake,
                    token_id=token_id,
                )
                # G4: Record order placed
                self._record_order_placed()
                # G5: Reset consecutive errors
                self._on_order_success()
            except Exception as exc:
                # G5: Record error for circuit breaker
                self._on_order_error(exc)
                self._log.error("execute.order_failed", error=str(exc))
                return
        
        # Use the real CLOB order ID so we can track it on-chain
        order_id = clob_order_id if not self._poly.paper_mode else f"5min-{uuid.uuid4().hex[:12]}"
        
        # Calculate fee
        fee_mult = 0.072  # Polymarket fee
        fee_usd = fee_mult * float(price) * (1.0 - float(price)) * stake
        
        # Build human-readable entry reason for resolution alerts
        try:
            _cg_snap = self._cg_enhanced.snapshot if self._cg_enhanced is not None else None
            _regime_word = (
                "CASCADE" if signal.current_vpin >= 0.65 else
                "TRANSITION" if signal.current_vpin >= 0.55 else "NORMAL"
            )
            _dir_word = "upward" if signal.direction == "UP" else "downward"
            _entry_reason_detail = (
                f"{_regime_word} momentum: VPIN {signal.current_vpin:.2f} "
                f"with δ{signal.delta_pct:+.3f}% — "
                f"{'high informed flow confirms' if _regime_word == 'CASCADE' else 'mean-reversion signal,'} "
                f"{_dir_word} pressure."
            )
            if _cg_snap is not None and getattr(_cg_snap, "connected", False):
                _total_t = _cg_snap.taker_buy_volume_1m + _cg_snap.taker_sell_volume_1m
                _taker_sell_r = (_cg_snap.taker_sell_volume_1m / _total_t) if _total_t > 0 else 0.5
                _entry_reason_detail += (
                    f" CG: taker sell ratio {_taker_sell_r:.2f}, "
                    f"smart {_cg_snap.top_position_short_pct:.0f}% short, "
                    f"OI Δ{_cg_snap.oi_delta_pct_1m:+.2f}%"
                )
        except Exception:
            _entry_reason_detail = f"T-{FIVE_MIN_ENTRY_OFFSET}s signal — delta {signal.delta_pct:+.4f}%, VPIN {signal.current_vpin:.4f}"

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
                "cg_modifier": signal.cg_modifier,
                "token_id": token_id,
                "entry_offset_s": FIVE_MIN_ENTRY_OFFSET,
                "entry_label": f"T-{FIVE_MIN_ENTRY_OFFSET}s",
                "entry_reason_detail": _entry_reason_detail,
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

        # Post-trade verification — v5.3: faster poll (30s) + dynamic bump + GTD
        # CLOB matching can take 5-30s on thin books near window close.
        # Poll every 5s for 30s, then dynamic bump retry for 15s.
        if not self._poly.paper_mode and order.order_id.startswith("0x"):
            POLL_INTERVAL = 5    # seconds between checks
            MAX_WAIT = 30        # v5.3: reduced from 60s → 30s
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
                    # Calculate ACTUAL fill price from matched size and stake
                    _actual_fill_price = None
                    try:
                        _matched_shares = float(size_matched)
                        if _matched_shares > 0:
                            _actual_fill_price = round(order.stake_usd / _matched_shares, 4)
                            order.price = str(_actual_fill_price)
                            order.metadata["actual_fill_price"] = _actual_fill_price
                            order.metadata["shares_matched"] = _matched_shares
                            self._log.info(
                                "trade.actual_fill_price",
                                submitted_price=f"${token_price:.4f}",
                                actual_price=f"${_actual_fill_price:.4f}",
                                shares=f"{_matched_shares:.2f}",
                            )
                    except Exception:
                        pass
                    
                    self._log.info(
                        "trade.verified",
                        order_id=order.order_id[:20] + "...",
                        size_matched=size_matched,
                        actual_price=f"${_actual_fill_price:.4f}" if _actual_fill_price else "n/a",
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
                    # Notify on Telegram when order doesn't fill
                    if self._alerter:
                        try:
                            asyncio.create_task(self._alerter.send_system_alert(
                                f"⏰ Order NOT FILLED — {window.asset} {tf}\n"
                                f"Direction: {direction} at ${float(price):.2f}\n"
                                f"Waited {elapsed}s, status: {clob_status}\n"
                                f"Attempting retry...",
                                level="warning",
                            ))
                        except Exception:
                            pass
                    # ── v5.3 DYNAMIC BUMP RETRY ────────────────────────────
                    # Read order book spread, bump by min(2¢, spread)
                    self._log.info("trade.retry_starting", original_price=str(price), order_id=order.order_id[:20] + "...")
                    try:
                        # Dynamic bump: read spread, bump intelligently
                        try:
                            spread = await self._poly.get_order_book_spread(token_id)
                            bumped_price_f = self._poly.calculate_dynamic_bump(float(price), spread)
                        except Exception:
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
                                # G4: Check rate limit before retry
                                rl_allowed_retry, rl_reason_retry = self._check_rate_limit()
                                if not rl_allowed_retry:
                                    self._log.warning("guardrail.rate_limit.blocked_on_retry", reason=rl_reason_retry)
                                else:
                                    # G5: Check circuit breaker before retry
                                    cb_allowed_retry, cb_reason_retry = self._check_circuit_breaker()
                                    if not cb_allowed_retry:
                                        self._log.warning("guardrail.circuit_breaker.blocked_on_retry", reason=cb_reason_retry)
                                    else:
                                        bumped_price_dec = Decimal(str(bumped_price_f))
                                        self._log.info(
                                            "trade.retry_bumped",
                                            original_price=str(price),
                                            bumped_price=str(bumped_price_dec),
                                            retry_stake=f"${retry_stake:.2f}",
                                        )
                                        _original_order_id_2 = order.order_id  # save before overwrite
                                        try:
                                            retry_id = await self._poly.place_order(
                                                market_slug=market_slug,
                                                direction=direction,
                                                price=bumped_price_dec,
                                                stake_usd=retry_stake,
                                                token_id=token_id,
                                            )
                                            # G4: Record order placed
                                            self._record_order_placed()
                                            # G5: Reset consecutive errors
                                            self._on_order_success()
                                            
                                            if retry_id:
                                                # FIX: Register retry_id → original order so resolve_order works for both IDs
                                                await self._om.register_retry_order_id(retry_id, _original_order_id_2)
                                            order.order_id = retry_id
                                            order.price = str(bumped_price_dec)
                                            order.stake_usd = retry_stake
                                            order.metadata["retried_at_bump"] = True
                                            order.metadata["bumped_price"] = str(bumped_price_dec)
                                            self._log.info("trade.retry_placed", order_id=str(retry_id)[:20], price=str(bumped_price_dec))
                                        except Exception as retry_place_exc:
                                            # G5: Record error for circuit breaker
                                            self._on_order_error(retry_place_exc)
                                            self._log.error("trade.retry_place_failed", error=str(retry_place_exc))
                                            retry_id = None

                                        # Quick fill check on retry (15s) — only if order was placed
                                        if 'retry_id' in locals() and retry_id:
                                            await asyncio.sleep(15)
                                            status2 = await self._poly.get_order_status(retry_id)
                                            retry_size_matched_2 = status2.get("size_matched", "0") or "0"
                                            retry_filled = float(retry_size_matched_2) > 0
                                            order.metadata["filled"] = retry_filled
                                            if retry_filled:
                                                # FIX: Update entry_price with actual fill price
                                                try:
                                                    _retry_shares_2 = float(retry_size_matched_2)
                                                    if _retry_shares_2 > 0:
                                                        _retry_fill_price_2 = round(order.stake_usd / _retry_shares_2, 4)
                                                        order.price = str(_retry_fill_price_2)
                                                        order.metadata["actual_fill_price"] = _retry_fill_price_2
                                                        self._log.info("trade.retry_actual_fill_price", price=f"${_retry_fill_price_2:.4f}", shares=_retry_shares_2)
                                                except Exception:
                                                    pass
                                                self._log.info("trade.retry_filled", order_id=str(retry_id)[:20], size_matched=status2.get("size_matched"))
                                                if self._alerter:
                                                    asyncio.create_task(self._alerter.send_entry_alert(order))
                                            else:
                                                self._log.warning("trade.retry_not_filled", order_id=str(retry_id)[:20])
                                                if self._alerter:
                                                    try:
                                                        asyncio.create_task(self._alerter.send_system_alert(
                                                            f"❌ Retry also NOT FILLED — {window.asset} {tf}\n"
                                                            f"Direction: {direction}\n"
                                                            f"Order expired unfilled. No position taken.",
                                                            level="warning",
                                                        ))
                                                    except Exception:
                                                        pass
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

    async def _fetch_fresh_gamma_price(self, slug: str) -> tuple[float | None, float | None, str]:
        """
        Fetch fresh Gamma bestAsk right now.

        Returns:
            (up_price, down_price, source) where source is "gamma_api_fresh" or "failed"
        """
        import aiohttp
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
                url = f"https://gamma-api.polymarket.com/events?slug={slug}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data and isinstance(data, list) and data[0].get("markets"):
                            mkt = data[0]["markets"][0]
                            best_ask = mkt.get("bestAsk")
                            if best_ask is not None:
                                up = float(best_ask)
                                down = round(1.0 - up, 4)
                                return up, down, "gamma_api_fresh"
        except Exception as exc:
            self._log.warning("fresh_gamma.fetch_failed", slug=slug, error=str(exc)[:80])
        return None, None, "failed"

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
        
        # ABSOLUTE HARD CAP — configurable via env, default $32 paper / $3 live
        ABSOLUTE_MAX_BET = float(os.environ.get("ABSOLUTE_MAX_BET", "32.0"))
        # Also check .env file
        if ABSOLUTE_MAX_BET == 32.0:
            from pathlib import Path
            _env_file = Path(__file__).parent.parent / ".env"
            if _env_file.exists():
                with open(_env_file) as f:
                    for line in f:
                        if line.startswith("ABSOLUTE_MAX_BET="):
                            ABSOLUTE_MAX_BET = float(line.split("=", 1)[1].strip())
                            break
        if adjusted_stake > ABSOLUTE_MAX_BET:
            self._log.info(
                "stake.absolute_cap",
                original=f"${adjusted_stake:.2f}",
                capped=f"${ABSOLUTE_MAX_BET:.2f}",
            )
            adjusted_stake = ABSOLUTE_MAX_BET
        
        # Also respect max_position_usd from config
        hard_max = min(runtime.max_position_usd, ABSOLUTE_MAX_BET)
        if adjusted_stake > hard_max:
            adjusted_stake = hard_max * 0.95

        # G2: Stake humanisation — round to 2 decimal places
        # Avoids bot-like precision that can flag suspicious order sizes
        adjusted_stake = round(adjusted_stake, 2)

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
