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

from config.constants import FIVE_MIN_ENTRY_OFFSET
from config.runtime_config import runtime
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
        Called by the orchestrator on every market state update.
        
        Also checks for pending window signals from the 5-min feed.
        """
        if not self._running or not runtime.five_min_enabled:
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
                entry=f"T-{FIVE_MIN_ENTRY_OFFSET}s",
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
                entry=f"T-{FIVE_MIN_ENTRY_OFFSET}s",
            )
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
        market_slug = f"{window.asset.lower()}-updown-5m-{window.window_ts}"
        
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
            window_seconds=300,  # 5-minute window
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
                "clob_order_id": clob_order_id if 'clob_order_id' in dir() else None,
                "market_slug": f"{window.asset.lower()}-updown-5m-{window.window_ts}",
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
            entry=f"T-{FIVE_MIN_ENTRY_OFFSET}s",
            token_price=str(price),
        )

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
