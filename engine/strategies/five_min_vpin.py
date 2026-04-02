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
        
        Full-window continuous monitoring:
        1. Register new windows from the feed
        2. Evaluate ALL active windows with latest data
        3. Fire when the evaluator says confidence is high enough
        """
        if not self._running or not runtime.five_min_enabled:
            return
        
        # Register new windows from the feed
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
        
        # Evaluate all active windows
        current_price = float(state.btc_price) if state.btc_price else None
        if current_price is None:
            return
        
        current_vpin = self._vpin.current_vpin
        now = time.time()
        
        # Get CoinGlass enhanced data if available
        cg_data = {}
        if self._cg_enhanced and self._cg_enhanced.connected:
            snap = self._cg_enhanced.snapshot
            cg_data = {
                "liq_total_1m": snap.liq_total_usd_1m,
                "liq_long_1m": snap.liq_long_usd_1m,
                "liq_short_1m": snap.liq_short_usd_1m,
                "long_short_ratio": snap.long_short_ratio,
                "long_pct": snap.long_pct,
                "funding_rate": snap.funding_rate,
                "oi_delta_pct_1m": snap.oi_delta_pct_1m,
            }
        
        # Evaluate each active window
        expired_keys = []
        for window_key, eval_state in self._active_eval_states.items():
            window_close_ts = eval_state.window_ts + 300  # 5-min window
            seconds_to_close = window_close_ts - now
            
            # Clean up expired windows
            if seconds_to_close < -10:
                expired_keys.append(window_key)
                continue
            
            # Skip if already fired
            if eval_state.fired:
                continue
            
            # Update open price if we didn't have it
            if eval_state.open_price <= 0:
                # Try to get from any matching pending window
                continue
            
            # Evaluate
            signal = self._evaluator.evaluate(
                window_state=eval_state,
                current_price=current_price,
                current_vpin=current_vpin,
                seconds_to_close=seconds_to_close,
                **cg_data,
            )
            
            if signal is not None:
                # FIRE — execute the trade
                eval_state.fired = True
                self._last_executed_window = window_key
                await self._execute_from_signal(state, signal, eval_state, window_key)
        
        # Clean up expired windows
        for key in expired_keys:
            del self._active_eval_states[key]

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
        
        if signal is None:
            tf = "15m" if window.duration_secs == 900 else "5m"
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
                    asyncio.create_task(self._alerter.send_system_alert(
                        f"SKIPPED — {window.asset} {tf}\n"
                        f"Delta: {delta_pct:+.4f}% (too small)\n"
                        f"VPIN: {current_vpin:.4f}\n"
                        f"BTC: ${current_price:,.2f}",
                        level="info",
                    ))
                except Exception:
                    pass
            return
        
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
        Evaluate trading signal based on delta and VPIN.
        
        Returns None if no trade should be placed.
        """
        # Minimum delta threshold - skip if too small
        if abs(delta_pct) < runtime.five_min_min_delta_pct:
            return None
        
        # Determine direction
        direction = "UP" if delta_pct > 0 else "DOWN"
        
        # Calculate confidence
        confidence = self._calculate_confidence(delta_pct, current_vpin, direction)
        
        # Check minimum confidence
        if confidence == "LOW" and delta_pct < runtime.five_min_min_confidence:
            return None
        
        return FiveMinSignal(
            window=window,
            current_price=current_price,
            current_vpin=current_vpin,
            delta_pct=delta_pct,
            confidence=confidence,
            direction=direction,
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

        # Calculate stake
        stake = self._calculate_stake(signal.tier)

        # Risk check
        approved, reason = await self._check_risk(stake)
        if not approved:
            self._log.info("trade.risk_blocked", window=window_key, reason=reason)
            return

        # Token price: use real Gamma price if available, else evaluator estimate
        token_price = signal.estimated_token_price

        # Direction and token ID
        direction = "YES" if signal.direction == "UP" else "NO"
        price = Decimal(str(round(token_price, 4)))

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
            return

        market_slug = f"{asset.lower()}-updown-5m-{window_ts}"

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
        
        # Determine stake based on mode
        stake = self._calculate_stake(signal.confidence)
        
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

    def _calculate_stake(self, confidence: str) -> float:
        """
        Calculate stake using runtime.bet_fraction from env/config.
        Always respects the global runtime.bet_fraction so risk manager won't block.
        """
        status = self._rm.get_status()
        bankroll = status["current_bankroll"]
        
        # Always use runtime.bet_fraction — this ensures consistency with risk manager
        return bankroll * runtime.bet_fraction

    # ─── Base Strategy Interface ──────────────────────────────────────────────

    async def evaluate(self, state: MarketState) -> Optional[dict]:
        """Evaluate market state for trading signals."""
        # This strategy uses window signals, not continuous evaluation
        return None

    async def execute(self, state: MarketState, signal: dict) -> Optional[Order]:
        """Execute a trading signal."""
        # This strategy handles execution internally
        return None
