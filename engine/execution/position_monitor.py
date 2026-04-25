"""Post-fill position monitoring and exit system.

After a FAK/FOK buy fills, the PositionMonitor tracks the position and
continues evaluating every 2s (on the existing eval loop cadence). If
the CLOB bid drops below a mark-to-market threshold for N consecutive
ticks, it triggers a sell via the CLOB client.

Architecture:
  - MonitoredPosition: dataclass tracking a single open position
  - PositionMonitor: manages all open positions, evaluates exits,
    executes sells

Integration:
  - on_fill() called from registry.py after ExecuteTradeUseCase succeeds
  - evaluate_exit() called every 2s in the eval loop for each open position
  - execute_exit() places a SELL FAK order on the CLOB to close the position

Exit logic (mark-to-market stop-loss):
  - For UP positions: mark = clob_up_bid (or 1 - clob_down_ask)
  - For DOWN positions: mark = clob_down_bid (or 1 - clob_up_ask)
  - If mark < exit_mark_min_pct * fill_price for exit_mark_ticks
    consecutive ticks, trigger exit.

Safety:
  - exit_shadow_mode: when True, logs exits but does NOT place sell orders
  - exit_min_hold_seconds: minimum hold time before exit can trigger
  - exit_no_exit_last_seconds: no exits in final N seconds (thin book)
  - exit_max_retries: sell retry count
  - exit_retry_timeout_seconds: per-sell-attempt timeout

See Hub notes #236 (exit system spec), #298/#299 (audit tasks), #240.
"""
from __future__ import annotations

import asyncio
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

import structlog

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface

log = structlog.get_logger(__name__)


@dataclass
class MonitoredPosition:
    """Tracks a single open position awaiting exit evaluation."""

    strategy_id: str
    window_ts: int
    direction: str  # "UP" or "DOWN"
    fill_price: float
    fill_size: float
    order_id: str
    token_id: str
    filled_at_epoch: float
    consecutive_flip_count: int = 0  # legacy, kept for compat
    mark_loss_tick_count: int = 0


class PositionMonitor:
    """Monitors open positions and triggers exits when signals flip.

    Designed as a singleton per engine instance. Thread-safe via asyncio
    semantics (single event loop, no locks needed).

    Parameters are read from the strategy's YAML gate_params at call time
    so they can be tuned without code changes.
    """

    def __init__(
        self,
        poly_client: Any = None,
        alerter: Any = None,
        decision_repo: Any = None,
    ) -> None:
        self._positions: dict[str, MonitoredPosition] = {}
        self._poly_client = poly_client
        self._alerter = alerter
        self._decision_repo = decision_repo
        self._log = log.bind(component="position_monitor")

    # ------------------------------------------------------------------
    # Fill registration
    # ------------------------------------------------------------------

    def on_fill(
        self,
        strategy_id: str,
        window_ts: int,
        direction: str,
        fill_price: float,
        fill_size: float,
        order_id: str,
        token_id: str = "",
    ) -> None:
        """Register a new fill for exit monitoring.

        Called from registry.py after a successful LIVE execution.
        """
        key = f"{strategy_id}:{window_ts}"
        self._positions[key] = MonitoredPosition(
            strategy_id=strategy_id,
            window_ts=window_ts,
            direction=direction,
            fill_price=fill_price,
            fill_size=fill_size,
            order_id=order_id,
            token_id=token_id,
            filled_at_epoch=time.time(),
        )
        self._log.info(
            "position_monitor.registered",
            key=key,
            direction=direction,
            fill_price=f"${fill_price:.3f}",
            fill_size=f"{fill_size:.2f}",
        )

    # ------------------------------------------------------------------
    # Exit evaluation
    # ------------------------------------------------------------------

    def evaluate_exit(
        self,
        strategy_id: str,
        window_ts: int,
        surface: "FullDataSurface",
        *,
        exit_monitor_enabled: bool = True,
        exit_min_hold_seconds: int = 45,
        exit_no_exit_last_seconds: int = 30,
        exit_mark_min_pct: float = 0.45,
        exit_mark_ticks: int = 10,
        # Legacy params (ignored, kept for call-site compat during rollout)
        exit_consecutive_flip_ticks: int = 5,
        exit_lgb_flip_enabled: bool = False,
        exit_oracle_flip_enabled: bool = False,
    ) -> Optional[str]:
        """Check whether a monitored position should exit via mark-to-market.

        Called every 2s from the strategy eval loop. Uses CLOB bid prices
        to determine if the position has lost too much value. If the
        mark-to-market ratio (current bid / fill price) stays below
        exit_mark_min_pct for exit_mark_ticks consecutive evaluations,
        triggers a stop-loss exit.

        Args:
            strategy_id: Strategy that owns the position.
            window_ts: Window timestamp of the position.
            surface: Current FullDataSurface with CLOB bid/ask data.
            exit_monitor_enabled: Master switch for exit monitoring.
            exit_min_hold_seconds: Minimum seconds to hold before allowing exit.
            exit_no_exit_last_seconds: Don't exit in final N seconds before close.
            exit_mark_min_pct: Exit if bid < this fraction of fill price.
            exit_mark_ticks: Number of consecutive ticks below threshold to trigger.
            exit_consecutive_flip_ticks: Legacy (unused).
            exit_lgb_flip_enabled: Legacy (unused).
            exit_oracle_flip_enabled: Legacy (unused).

        Returns:
            Exit reason string or None.
        """
        if not exit_monitor_enabled:
            return None

        key = f"{strategy_id}:{window_ts}"
        pos = self._positions.get(key)
        if pos is None:
            return None

        now = time.time()
        held_seconds = now - pos.filled_at_epoch

        # Safety rail: don't exit too early
        if held_seconds < exit_min_hold_seconds:
            return None

        # Safety rail: don't exit in last N seconds (thin book risk)
        seconds_to_close = getattr(surface, "eval_offset_sec", None) or getattr(surface, "eval_offset", None) or 0
        if seconds_to_close > 0 and seconds_to_close < exit_no_exit_last_seconds:
            return None

        # Mark-to-market: get current bid for our token
        if pos.direction == "UP":
            up_bid = getattr(surface, "clob_up_bid", None)
            dn_ask = getattr(surface, "clob_down_ask", None)
            if up_bid is not None and float(up_bid) > 0.01:
                mark = float(up_bid)
            elif dn_ask is not None:
                mark = 1.0 - float(dn_ask)
            else:
                return None  # can't price, skip
        else:  # DOWN
            dn_bid = getattr(surface, "clob_down_bid", None)
            up_ask = getattr(surface, "clob_up_ask", None)
            if dn_bid is not None and float(dn_bid) > 0.01:
                mark = float(dn_bid)
            elif up_ask is not None:
                mark = 1.0 - float(up_ask)
            else:
                return None  # can't price, skip

        mark_pct = mark / pos.fill_price if pos.fill_price > 0 else 1.0

        # Stop-loss check
        if mark_pct < exit_mark_min_pct:
            pos.mark_loss_tick_count += 1
        else:
            pos.mark_loss_tick_count = 0  # reset on recovery

        if pos.mark_loss_tick_count >= exit_mark_ticks:
            return (
                f"mark_stop_loss: mark={mark:.3f} "
                f"({mark_pct:.0%} of fill) for "
                f"{pos.mark_loss_tick_count} ticks"
            )

        return None

    # ------------------------------------------------------------------
    # Exit execution
    # ------------------------------------------------------------------

    async def execute_exit(
        self,
        strategy_id: str,
        window_ts: int,
        reason: str,
        *,
        exit_shadow_mode: bool = True,
        exit_max_retries: int = 1,
        exit_retry_timeout_seconds: int = 5,
    ) -> bool:
        """Sell the position on CLOB.

        In shadow mode (default), logs the exit but does NOT place a sell
        order. This lets operators validate exit signals before enabling
        real sells.

        Args:
            strategy_id: Strategy that owns the position.
            window_ts: Window timestamp.
            reason: Exit reason string.
            exit_shadow_mode: If True, log only (don't sell).
            exit_max_retries: Number of sell retries.
            exit_retry_timeout_seconds: Timeout per sell attempt.

        Returns:
            True if exit was executed (or shadow-logged), False if no position found.
        """
        key = f"{strategy_id}:{window_ts}"
        pos = self._positions.pop(key, None)  # pop IMMEDIATELY — prevents double-entry
        if pos is None:
            return False

        held_seconds = time.time() - pos.filled_at_epoch

        if exit_shadow_mode:
            self._log.info(
                "position_monitor.exit_shadow",
                strategy_id=strategy_id,
                window_ts=window_ts,
                direction=pos.direction,
                fill_price=f"${pos.fill_price:.3f}",
                fill_size=f"{pos.fill_size:.2f}",
                held_seconds=f"{held_seconds:.0f}",
                reason=reason,
            )
            # Record shadow exit decision
            await self._record_exit_decision(
                pos, reason, executed=False, shadow=True
            )
            # Send TG alert for shadow exit
            await self._send_exit_alert(
                pos, reason, executed=False, shadow=True
            )
            return True

        # Real exit: place SELL FAK order
        sell_success = False
        for attempt in range(1, exit_max_retries + 1):
            try:
                sell_success = await self._place_sell_order(
                    pos, timeout=exit_retry_timeout_seconds
                )
                if sell_success:
                    break
            except Exception as exc:
                self._log.warning(
                    "position_monitor.sell_attempt_failed",
                    attempt=attempt,
                    max_retries=exit_max_retries,
                    error=str(exc)[:200],
                )

        # Record and alert
        await self._record_exit_decision(
            pos, reason, executed=sell_success, shadow=False
        )
        await self._send_exit_alert(
            pos, reason, executed=sell_success, shadow=False
        )

        return sell_success

    async def _place_sell_order(
        self,
        pos: MonitoredPosition,
        timeout: int = 5,
    ) -> bool:
        """Place a SELL FAK order on the CLOB to close the position.

        Uses the same CLOB client as buy-side but with side=SELL.
        """
        if self._poly_client is None:
            self._log.warning(
                "position_monitor.no_poly_client",
                order_id=pos.order_id[:20],
            )
            return False

        if not pos.token_id:
            self._log.warning(
                "position_monitor.no_token_id",
                strategy_id=pos.strategy_id,
                window_ts=pos.window_ts,
            )
            return False

        try:
            # Price: sell at market (use a low limit to ensure fill)
            # For binary tokens: sell YES at any price above 0.01
            # The py_clob_client supports SELL side via the same OrderArgs
            sell_price = 0.01  # aggressive: take any bid, we want OUT fast
            sell_size = pos.fill_size

            # Round size to 3dp to match CLOB precision
            sell_size = round(sell_size, 3)
            if sell_size <= 0:
                return False

            result = await self._poly_client.place_sell_fak(
                token_id=pos.token_id,
                price=sell_price,
                size=sell_size,
            )

            filled = result.get("filled", False)
            self._log.info(
                "position_monitor.sell_result",
                strategy_id=pos.strategy_id,
                window_ts=pos.window_ts,
                filled=filled,
                sell_price=f"${sell_price:.4f}",
                size_matched=result.get("size_matched", 0),
                order_id=result.get("order_id", "none")[:20],
            )
            return filled

        except Exception as exc:
            self._log.error(
                "position_monitor.sell_error",
                strategy_id=pos.strategy_id,
                window_ts=pos.window_ts,
                error=str(exc)[:200],
            )
            return False

    async def _record_exit_decision(
        self,
        pos: MonitoredPosition,
        reason: str,
        executed: bool,
        shadow: bool,
    ) -> None:
        """Write EXIT decision to strategy_decisions table."""
        if self._decision_repo is None:
            return
        try:
            import json

            from domain.value_objects import StrategyDecisionRecord

            record = StrategyDecisionRecord(
                strategy_id=pos.strategy_id,
                strategy_version="exit-monitor-1.0",
                asset="BTC",
                window_ts=pos.window_ts,
                timeframe="5m",
                eval_offset=0,
                mode="SHADOW" if shadow else "LIVE",
                action="EXIT",
                direction=pos.direction,
                confidence=None,
                confidence_score=None,
                entry_cap=None,
                collateral_pct=None,
                entry_reason="",
                skip_reason=None,
                metadata_json=json.dumps({
                    "exit_reason": reason,
                    "executed": executed,
                    "shadow": shadow,
                    "fill_price": pos.fill_price,
                    "fill_size": pos.fill_size,
                    "order_id": pos.order_id,
                    "held_seconds": time.time() - pos.filled_at_epoch,
                    "mark_loss_tick_count": pos.mark_loss_tick_count,
                }),
                evaluated_at=time.time(),
            )

            await self._decision_repo.write_decision(record)
        except Exception as exc:
            self._log.warning(
                "position_monitor.record_error",
                error=str(exc)[:200],
            )

    async def _send_exit_alert(
        self,
        pos: MonitoredPosition,
        reason: str,
        executed: bool,
        shadow: bool,
    ) -> None:
        """Send Telegram alert for exit event."""
        if self._alerter is None:
            return

        held_secs = time.time() - pos.filled_at_epoch
        mode = "SHADOW" if shadow else ("EXECUTED" if executed else "FAILED")
        emoji = {"SHADOW": "\U0001f441\ufe0f", "EXECUTED": "\U0001f4b0", "FAILED": "\u274c"}.get(mode, "\u2753")

        msg = (
            f"{emoji} *EXIT {mode}* — {pos.strategy_id}\n"
            f"direction: `{pos.direction}` window: `{pos.window_ts}`\n"
            f"fill: `${pos.fill_price:.3f}` size: `{pos.fill_size:.2f}`\n"
            f"held: `{held_secs:.0f}s` mark_ticks: `{pos.mark_loss_tick_count}`\n"
            f"reason: {reason}"
        )

        try:
            send = getattr(self._alerter, "send_raw_message", None)
            if send is not None:
                await send(msg)
            elif hasattr(self._alerter, "send_system_alert"):
                await self._alerter.send_system_alert(msg)
        except Exception as exc:
            self._log.warning(
                "position_monitor.alert_error",
                error=str(exc)[:200],
            )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def remove_position(self, strategy_id: str, window_ts: int) -> None:
        """Remove a position from monitoring (e.g. window resolved)."""
        key = f"{strategy_id}:{window_ts}"
        self._positions.pop(key, None)

    def get_open_positions(self) -> dict[str, MonitoredPosition]:
        """Return all currently monitored positions."""
        return dict(self._positions)

    @property
    def position_count(self) -> int:
        return len(self._positions)
