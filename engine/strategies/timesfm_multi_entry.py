"""
TimesFM Multi-Entry Strategy — Forecast at multiple time points within each window.

Evaluates TimesFM at T-180s, T-120s, T-60s, T-30s before window close.
Records forecast + Gamma price at each checkpoint.
Places trade IMMEDIATELY after forecast (no staggered queue).

This lets us:
1. See if TimesFM accuracy improves closer to close
2. Test which entry point is most profitable
3. Actually get trades submitted with enough time

Only trades BTC (TimesFM only has BTC models).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Dict

import structlog

from config.runtime_config import runtime
from data.models import MarketState
from data.feeds.polymarket_5min import WindowInfo, WindowState
from execution.order_manager import Order, OrderManager, OrderStatus
from execution.polymarket_client import PolymarketClient
from execution.risk_manager import RiskManager
from signals.timesfm_client import TimesFMClient, TimesFMForecast

log = structlog.get_logger(__name__)

# Checkpoints: seconds before window close
CHECKPOINTS = [180, 120, 60, 30]


@dataclass
class CheckpointForecast:
    """Forecast captured at a specific checkpoint."""
    checkpoint_s: int           # e.g. 180 = T-180s
    timestamp: float            # Unix time of forecast
    direction: str              # UP or DOWN
    confidence: float
    predicted_close: float
    current_price: float        # BTC price at checkpoint
    gamma_up: float             # Polymarket UP token price
    gamma_down: float           # Polymarket DOWN token price
    gamma_mid: float            # (up + down) / 2
    window_ts: int
    open_price: float


@dataclass  
class WindowForecasts:
    """All checkpoint forecasts for a single window."""
    window_ts: int
    asset: str
    open_price: float
    close_price: float = 0.0    # Filled after resolution
    checkpoints: Dict[int, CheckpointForecast] = field(default_factory=dict)
    trade_placed: bool = False
    trade_checkpoint: int = 0   # Which checkpoint triggered the trade
    trade_direction: str = ""
    trade_entry_price: float = 0.0
    outcome: str = ""           # WIN/LOSS after resolution


class TimesFMMultiEntryStrategy:
    """
    Evaluates TimesFM at multiple checkpoints within each 5-min window.
    
    Bypasses the staggered execution queue entirely.
    Places paper trades immediately when confident.
    """
    
    def __init__(
        self,
        order_manager: OrderManager,
        risk_manager: RiskManager,
        poly_client: PolymarketClient,
        timesfm_client: TimesFMClient,
        alerter=None,
        db_client=None,
        max_bet: float = 32.0,
        min_confidence: float = 0.30,
    ):
        self._order_manager = order_manager
        self._risk_manager = risk_manager
        self._poly_client = poly_client
        self._timesfm = timesfm_client
        self._alerter = alerter
        self._db = db_client
        self._max_bet = max_bet
        self._min_confidence = min_confidence
        
        # Track active windows and their forecasts
        self._active_windows: Dict[int, WindowForecasts] = {}
        self._checkpoint_tasks: Dict[str, asyncio.Task] = {}
        self._running = False
        self._log = log.bind(strategy="timesfm_multi_entry")
    
    async def start(self):
        self._running = True
        self._log.info("strategy.started")
    
    async def stop(self):
        self._running = False
        # Cancel pending checkpoint tasks
        for task_id, task in self._checkpoint_tasks.items():
            task.cancel()
        self._checkpoint_tasks.clear()
        self._log.info("strategy.stopped")
    
    async def on_window_open(self, window: WindowInfo, state: MarketState):
        """
        Called when a new window opens (ACTIVE state).
        Schedules forecast checkpoints at T-180, T-120, T-60, T-30.
        """
        if not self._running:
            return
        if window.asset != "BTC":
            return
        
        window_ts = window.window_ts
        if window_ts in self._active_windows:
            return  # Already tracking
        
        # Determine window duration
        duration = getattr(window, 'duration_secs', 300)
        
        # Create tracking object
        wf = WindowForecasts(
            window_ts=window_ts,
            asset=window.asset,
            open_price=window.open_price or 0,
        )
        self._active_windows[window_ts] = wf
        
        # Schedule checkpoints
        now = time.time()
        window_close_ts = window_ts + duration
        
        for cp_seconds in CHECKPOINTS:
            eval_time = window_close_ts - cp_seconds
            delay = eval_time - now
            
            if delay < 0:
                # Already past this checkpoint
                continue
            
            task_id = f"tfm-{window_ts}-T{cp_seconds}"
            task = asyncio.create_task(
                self._run_checkpoint(window, state, cp_seconds, delay, duration)
            )
            self._checkpoint_tasks[task_id] = task
            
            self._log.info(
                "checkpoint.scheduled",
                window_ts=window_ts,
                checkpoint=f"T-{cp_seconds}s",
                delay_s=f"{delay:.1f}",
                eval_at=f"{eval_time:.0f}",
            )
    
    async def _run_checkpoint(
        self, window: WindowInfo, state: MarketState, 
        checkpoint_s: int, delay: float, duration: int
    ):
        """Run a single checkpoint evaluation after delay."""
        try:
            # Wait until checkpoint time
            if delay > 0:
                await asyncio.sleep(delay)
            
            if not self._running:
                return
            
            window_ts = window.window_ts
            wf = self._active_windows.get(window_ts)
            if not wf:
                return
            
            # Get current BTC price
            current_state = await self._get_current_state()
            current_price = float(current_state.btc_price) if current_state and current_state.btc_price else None
            
            if not current_price:
                self._log.warning("checkpoint.no_price", window_ts=window_ts, checkpoint=f"T-{checkpoint_s}s")
                return
            
            # Get TimesFM forecast
            forecast = await self._timesfm.get_forecast(open_price=wf.open_price)
            
            if forecast.error:
                self._log.warning("checkpoint.forecast_error", error=forecast.error, checkpoint=f"T-{checkpoint_s}s")
                return
            
            # Get current Gamma prices
            gamma_up = window.up_price or 0.50
            gamma_down = window.down_price or 0.50
            gamma_mid = (gamma_up + gamma_down) / 2
            
            # Record checkpoint
            cp = CheckpointForecast(
                checkpoint_s=checkpoint_s,
                timestamp=time.time(),
                direction=forecast.direction,
                confidence=forecast.confidence,
                predicted_close=forecast.predicted_close,
                current_price=current_price,
                gamma_up=gamma_up,
                gamma_down=gamma_down,
                gamma_mid=gamma_mid,
                window_ts=window_ts,
                open_price=wf.open_price,
            )
            wf.checkpoints[checkpoint_s] = cp
            
            self._log.info(
                "checkpoint.evaluated",
                window_ts=window_ts,
                checkpoint=f"T-{checkpoint_s}s",
                direction=forecast.direction,
                confidence=f"{forecast.confidence:.4f}",
                predicted_close=f"${forecast.predicted_close:,.2f}",
                current_price=f"${current_price:,.2f}",
                gamma_mid=f"${gamma_mid:.4f}",
                gamma_up=f"${gamma_up:.4f}",
                gamma_down=f"${gamma_down:.4f}",
            )
            
            # TRADE DECISION — immediate, no queue
            # Only trade if:
            # 1. Confidence >= min threshold
            # 2. Haven't already traded this window
            # 3. Would be profitable after fees (Gamma mid not too extreme)
            if (
                forecast.confidence >= self._min_confidence
                and not wf.trade_placed
                and 0.10 < gamma_mid < 0.90  # Don't trade when market already decided
            ):
                # Check profitability: entry at gamma_mid, fee ~2%
                # If correct: payout ~$0.90, profit = (0.90 - entry) * bet * 0.98
                # Break-even: entry must be < ~0.88 for profit on correct prediction
                entry_price = gamma_mid
                potential_profit = (0.90 - entry_price) * self._max_bet * 0.98
                potential_loss = (0.10 - entry_price) * self._max_bet * 0.98
                
                if potential_profit > 0:
                    # PLACE TRADE IMMEDIATELY
                    await self._place_trade(wf, cp, forecast)
                else:
                    self._log.info(
                        "checkpoint.skip_no_profit",
                        window_ts=window_ts,
                        checkpoint=f"T-{checkpoint_s}s",
                        gamma_mid=f"${gamma_mid:.4f}",
                        potential_profit=f"${potential_profit:.2f}",
                    )
            
            # Send Telegram alert for this checkpoint
            if self._alerter:
                await self._send_checkpoint_alert(wf, cp, forecast)
                
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log.warning("checkpoint.error", error=str(exc), checkpoint=f"T-{checkpoint_s}s")
    
    async def _place_trade(self, wf: WindowForecasts, cp: CheckpointForecast, forecast: TimesFMForecast):
        """Place paper trade immediately — no queue, no delay."""
        direction = "YES" if cp.direction == "UP" else "NO"
        entry_price = cp.gamma_mid
        
        # Cap at 65¢ for 5-min
        if entry_price > 0.65:
            entry_price = 0.65
        
        order_id = f"tfm-me-{uuid.uuid4().hex[:12]}"
        
        order = Order(
            order_id=order_id,
            venue="polymarket",
            strategy="timesfm_multi_entry",
            direction=direction,
            price=str(round(entry_price, 4)),
            stake_usd=self._max_bet,
            btc_entry_price=cp.current_price,
            window_seconds=300,
            market_id=f"btc-updown-5m-{wf.window_ts}",
            metadata={
                "checkpoint": f"T-{cp.checkpoint_s}s",
                "timesfm_direction": cp.direction,
                "timesfm_confidence": cp.confidence,
                "gamma_mid": cp.gamma_mid,
                "gamma_up": cp.gamma_up,
                "gamma_down": cp.gamma_down,
                "predicted_close": cp.predicted_close,
            },
        )
        
        await self._order_manager.register_order(order)
        wf.trade_placed = True
        wf.trade_checkpoint = cp.checkpoint_s
        wf.trade_direction = cp.direction
        wf.trade_entry_price = entry_price
        
        self._log.info(
            "trade.placed_immediate",
            order_id=order_id,
            window_ts=wf.window_ts,
            checkpoint=f"T-{cp.checkpoint_s}s",
            direction=direction,
            entry_price=f"${entry_price:.4f}",
            confidence=f"{cp.confidence:.4f}",
            gamma_mid=f"${cp.gamma_mid:.4f}",
            seconds_before_close=cp.checkpoint_s,
        )
    
    async def _send_checkpoint_alert(self, wf: WindowForecasts, cp: CheckpointForecast, forecast: TimesFMForecast):
        """Send Telegram alert for this checkpoint evaluation."""
        try:
            from datetime import datetime, timezone
            ts_str = datetime.fromtimestamp(wf.window_ts, tz=timezone.utc).strftime("%H:%M")
            
            # Build comparison with previous checkpoints
            prev_checkpoints = []
            for prev_s in sorted(wf.checkpoints.keys(), reverse=True):
                if prev_s != cp.checkpoint_s:
                    prev = wf.checkpoints[prev_s]
                    agree = "same" if prev.direction == cp.direction else "FLIPPED"
                    prev_checkpoints.append(f"T-{prev_s}s: {prev.direction} ({prev.confidence:.0%}) [{agree}]")
            
            lines = [
                f"🧠 *TimesFM Checkpoint — BTC {ts_str} (T-{cp.checkpoint_s}s)*",
                f"",
                f"Forecast: {'📈' if cp.direction == 'UP' else '📉'} *{cp.direction}* | Conf: *{cp.confidence:.0%}*",
                f"Predicted close: `${cp.predicted_close:,.2f}`",
                f"Current BTC: `${cp.current_price:,.2f}`",
                f"",
                f"Gamma: UP=`${cp.gamma_up:.4f}` DOWN=`${cp.gamma_down:.4f}` Mid=`${cp.gamma_mid:.4f}`",
            ]
            
            if prev_checkpoints:
                lines.append(f"")
                lines.append(f"Previous checkpoints:")
                for p in prev_checkpoints:
                    lines.append(f"  {p}")
            
            if wf.trade_placed:
                lines += [
                    f"",
                    f"Trade: PLACED at T-{wf.trade_checkpoint}s | {wf.trade_direction} @ ${wf.trade_entry_price:.4f}",
                ]
            else:
                # Show why no trade yet
                if cp.confidence < self._min_confidence:
                    lines.append(f"\nNo trade: confidence {cp.confidence:.0%} < {self._min_confidence:.0%}")
                elif cp.gamma_mid <= 0.10 or cp.gamma_mid >= 0.90:
                    lines.append(f"\nNo trade: market already decided (mid=${cp.gamma_mid:.4f})")
                else:
                    lines.append(f"\nReady to trade if confident")
            
            await self._alerter._send("\n".join(lines))
        except Exception as exc:
            self._log.warning("alert.failed", error=str(exc))
    
    async def _get_current_state(self) -> Optional[MarketState]:
        """Get current market state from aggregator."""
        # This will be wired by the orchestrator
        if hasattr(self, '_aggregator') and self._aggregator:
            return await self._aggregator.get_state()
        return None
    
    def set_aggregator(self, aggregator):
        """Set the market state aggregator reference."""
        self._aggregator = aggregator
