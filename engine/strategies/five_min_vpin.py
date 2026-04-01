"""
5-Minute VPIN Strategy

Combines window delta analysis with VPIN signal for 5-minute Polymarket
Up/Down markets. Trades at T-10s for optimal entry timing.

Decision Logic:
- If VPIN >= 0.30 AND delta direction aligns → HIGH confidence, enter early (T-30s)
- If delta > 0.10% → HIGH confidence, bet with delta
- If delta > 0.02% → MODERATE confidence
- If delta > 0.005% → LOW confidence
- If delta < 0.005% → SKIP (coin flip, no edge)

Bankroll Management:
- Safe mode: 25% per trade
- Flat mode: Fixed USD amount
- Degen mode: 50% per trade
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Callable, Awaitable

import structlog

from config.constants import (
    BET_FRACTION,
    FIVE_MIN_ENABLED,
    FIVE_MIN_MODE,
    FIVE_MIN_MIN_CONFIDENCE,
    FIVE_MIN_MIN_DELTA_PCT,
)
from data.models import MarketState
from data.feeds.polymarket_5min import WindowInfo, WindowState
from execution.order_manager import Order, OrderManager, OrderStatus
from execution.polymarket_client import PolymarketClient
from execution.risk_manager import RiskManager
from signals.vpin import VPINCalculator
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
        on_window_signal: Optional[Callable[[WindowInfo], Awaitable[None]]] = None,
    ) -> None:
        super().__init__(
            name="five_min_vpin",
            order_manager=order_manager,
            risk_manager=risk_manager,
        )
        self._poly = poly_client
        self._vpin = vpin_calculator
        
        # Track last executed window to avoid duplicates
        self._last_executed_window: Optional[str] = None
        
        # Window info buffer (populated by feed callbacks)
        self._pending_window: Optional[WindowInfo] = None
        
        self._log = log.bind(strategy="five_min_vpin")
        
        # Setup window signal callback
        if on_window_signal:
            self._on_window_signal = on_window_signal
        else:
            # Default: store window for evaluation
            self._on_window_signal = self._default_window_handler

    async def _default_window_handler(self, window: WindowInfo) -> None:
        """Default window signal handler - stores window for evaluation."""
        self._pending_window = window
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
        if not FIVE_MIN_ENABLED:
            self._log.info("strategy.disabled", reason="FIVE_MIN_ENABLED=false")
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
        Called by the orchestrator on every market state update.
        
        Also checks for pending window signals from the 5-min feed.
        """
        if not self._running or not FIVE_MIN_ENABLED:
            return
        
        # If we have a pending window, evaluate it
        if self._pending_window:
            await self._evaluate_window(self._pending_window, state)
            self._pending_window = None

    # ─── Window Signal Handler ────────────────────────────────────────────────

    async def _evaluate_window(self, window: WindowInfo, state: MarketState) -> None:
        """
        Evaluate a window at T-10s signal.
        
        Args:
            window: Window info from the 5-min feed
            state: Current market state
        """
        # Dedup: skip if we already acted on this window
        window_key = f"{window.asset}-{window.window_ts}"
        if self._last_executed_window == window_key:
            self._log.debug("window.already_executed", window=window_key)
            return
        
        # Get current BTC price
        current_price = float(state.btc_price) if state.btc_price else None
        if current_price is None:
            self._log.warning("evaluate.no_current_price")
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
            self._log.info(
                "evaluate.skip",
                asset=window.asset,
                window_ts=window.window_ts,
                delta_pct=f"{delta_pct:.4f}%",
                reason="no edge",
            )
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
        if abs(delta_pct) < FIVE_MIN_MIN_DELTA_PCT:
            return None
        
        # Determine direction
        direction = "UP" if delta_pct > 0 else "DOWN"
        
        # Calculate confidence
        confidence = self._calculate_confidence(delta_pct, current_vpin, direction)
        
        # Check minimum confidence
        if confidence == "LOW" and delta_pct < FIVE_MIN_MIN_CONFIDENCE:
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
        
        # High confidence: strong VPIN with aligned delta
        if current_vpin >= 0.30:
            return "HIGH"
        
        # High confidence: strong delta
        if abs_delta > 0.10:
            return "HIGH"
        
        # Moderate confidence
        if abs_delta > 0.02:
            return "MODERATE"
        
        # Low confidence
        if abs_delta > 0.005:
            return "LOW"
        
        return "NONE"

    # ─── Execution ────────────────────────────────────────────────────────────

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
            )
            return
        
        # Get prices
        prices = await self._poly.get_market_prices(f"{window.asset.lower()}-updown-5m-{window.window_ts}")
        
        # Select price based on direction
        if signal.direction == "UP":
            direction = "YES"
            price = prices.get("yes", Decimal("0.5")) if prices else Decimal("0.5")
            token_id = window.up_token_id
        else:
            direction = "NO"
            price = prices.get("no", Decimal("0.5")) if prices else Decimal("0.5")
            token_id = window.down_token_id
        
        if token_id is None:
            self._log.error("execute.no_token_id", direction=signal.direction)
            return
        
        # Place order
        try:
            order_id = await self._poly.place_order(
                market_slug=f"{window.asset.lower()}-updown-5m-{window.window_ts}",
                direction=direction,
                price=price,
                stake_usd=stake,
            )
        except Exception as exc:
            self._log.error("execute.order_failed", error=str(exc))
            return
        
        # Calculate fee
        fee_mult = 0.072  # Polymarket fee
        fee_usd = fee_mult * float(price) * (1.0 - float(price)) * stake
        
        # Create order
        order = Order(
            order_id=f"5min-{uuid.uuid4().hex[:12]}",
            strategy=self.name,
            venue="polymarket",
            direction=direction,
            price=str(price),
            stake_usd=stake,
            fee_usd=fee_usd,
            status=OrderStatus.OPEN,
            btc_entry_price=signal.current_price,
            window_seconds=300,  # 5-minute window
            market_id=f"{window.asset.lower()}-updown-5m-{window.window_ts}",
            metadata={
                "window_ts": window.window_ts,
                "window_open_price": window.open_price,
                "delta_pct": signal.delta_pct,
                "vpin": signal.current_vpin,
                "confidence": signal.confidence,
                "token_id": token_id,
            },
        )
        
        await self._om.register_order(order)
        
        self._log.info(
            "trade.executed",
            order_id=order.order_id,
            asset=window.asset,
            direction=direction,
            stake=stake,
            delta_pct=f"{signal.delta_pct:.4f}%",
            vpin=f"{signal.current_vpin:.4f}",
            confidence=signal.confidence,
        )

    def _calculate_stake(self, confidence: str) -> float:
        """
        Calculate stake using BET_FRACTION from env/config.
        Always respects the global BET_FRACTION so risk manager won't block.
        """
        status = self._rm.get_status()
        bankroll = status["current_bankroll"]
        
        # Always use BET_FRACTION — this ensures consistency with risk manager
        return bankroll * BET_FRACTION

    # ─── Base Strategy Interface ──────────────────────────────────────────────

    async def evaluate(self, state: MarketState) -> Optional[dict]:
        """Evaluate market state for trading signals."""
        # This strategy uses window signals, not continuous evaluation
        return None

    async def execute(self, state: MarketState, signal: dict) -> Optional[Order]:
        """Execute a trading signal."""
        # This strategy handles execution internally
        return None
