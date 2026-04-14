"""Use case: Execute Trade.

Replaces: ``engine/strategies/five_min_vpin.py::_execute_trade``
          (lines 3178-3714, ~536 LOC).

Responsibility
--------------
Take a StrategyDecision from the registry, validate it against risk
limits, size the position, execute the order on Polymarket CLOB (via
OrderExecutionPort), record the trade, and send a Telegram alert.

Does NOT evaluate strategies -- that is EvaluateStrategiesUseCase.
Does NOT resolve positions -- that is ReconcilePositionsUseCase.

Port dependencies (all from ``engine/domain/ports.py``):
  - PolymarketClientPort: get_window_market (token ID lookup)
  - OrderExecutionPort: execute_order (FAK/GTC/paper)
  - RiskManagerPort: get_status (bankroll, risk checks)
  - WindowStateRepository: was_traded, mark_traded (dedup)
  - AlerterPort: send_trade_alert, send_system_alert
  - TradeRecorderPort: record_trade
  - Clock: deterministic time for testing

Feature flag: ENGINE_REGISTRY_EXECUTE (default false).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Optional

from domain.ports import (
    AlerterPort,
    Clock,
    OrderExecutionPort,
    PolymarketClientPort,
    RiskManagerPort,
    TradeRecorderPort,
    WindowStateRepository,
)
from domain.value_objects import (
    ExecutionResult,
    RiskStatus,
    StakeCalculation,
    StrategyDecision,
    WindowKey,
    WindowMarket,
)
from config.runtime_config import runtime

logger = logging.getLogger(__name__)

# Constants ported from five_min_vpin.py
PRICE_FLOOR = 0.30
DEFAULT_ENTRY_CAP = 0.65
MIN_BET_USD = 1.0  # floor; runtime.min_bet_usd overrides this if lower
DEFAULT_BET_FRACTION = 0.025
FEE_MULTIPLIER = 0.072  # Polymarket binary options fee

# Guardrail constants
MIN_ORDER_INTERVAL_S = 30
MAX_ORDERS_PER_HOUR = 20
CIRCUIT_BREAKER_ERRORS = 3
CIRCUIT_BREAKER_COOLDOWN_S = 180  # 3 minutes


def _failed(
    reason: str,
    *,
    strategy_id: str = "",
    direction: str = "",
    stake_usd: float = 0.0,
    token_id: str = "",
) -> ExecutionResult:
    """Build a failed ExecutionResult."""
    return ExecutionResult(
        success=False,
        failure_reason=reason,
        strategy_id=strategy_id,
        direction=direction,
        stake_usd=stake_usd,
        token_id=token_id,
    )


class ExecuteTradeUseCase:
    """Execute a strategy decision on Polymarket CLOB.

    Single responsibility: take a StrategyDecision, validate it,
    size the position, execute the order, record the trade, alert.

    The 10-step flow:
      1. Dedup check (WindowStateRepository.was_traded)
      2. Calculate stake (bankroll x bet_fraction x price_multiplier)
      3. Risk approval (drawdown, daily loss, kill switch)
      4. Guardrails (rate limit, circuit breaker)
      5. Resolve token ID from direction + window market
      6. Execute order via OrderExecutionPort
      7. Record trade (TradeRecorderPort)
      8. Mark window as traded (WindowStateRepository)
      9. Send Telegram alert
      10. Return ExecutionResult
    """

    def __init__(
        self,
        polymarket: PolymarketClientPort,
        order_executor: OrderExecutionPort,
        risk_manager: RiskManagerPort,
        window_state: WindowStateRepository,
        alerter: AlerterPort,
        trade_recorder: TradeRecorderPort,
        clock: Clock,
        *,
        paper_mode: bool = True,
    ) -> None:
        self._polymarket = polymarket
        self._executor = order_executor
        self._risk = risk_manager
        self._window_state = window_state
        self._alerter = alerter
        self._recorder = trade_recorder
        self._clock = clock
        self._paper_mode = paper_mode

        # Guardrails (stateful -- mirrors five_min_vpin guardrails)
        self._order_timestamps: list[float] = []
        self._last_order_time: float = 0.0
        self._consecutive_errors: int = 0
        self._circuit_break_until: float = 0.0

    async def execute(
        self,
        decision: StrategyDecision,
        window_market: WindowMarket,
        current_btc_price: float,
        open_price: float,
    ) -> ExecutionResult:
        """Execute a trade from a strategy decision.

        Args:
            decision: The StrategyDecision with action="TRADE"
            window_market: Gamma market with token IDs
            current_btc_price: Live BTC price
            open_price: Window open price

        Returns:
            ExecutionResult with fill details or failure info
        """
        sid = decision.strategy_id
        direction = decision.direction or "DOWN"

        # Extract window key from market slug
        window_key = self._make_window_key(window_market)

        # ── Step 1: Dedup ──────────────────────────────────────────────
        try:
            if await self._window_state.was_traded(window_key):
                logger.info(
                    "execute_trade.dedup_hit",
                    extra={"strategy": sid, "window": str(window_key)},
                )
                return _failed(
                    "already_traded",
                    strategy_id=sid,
                    direction=direction,
                )
        except Exception as exc:
            logger.warning(
                "execute_trade.dedup_check_error",
                extra={"error": str(exc)[:200]},
            )
            # Fail safe: if we can't check dedup, proceed anyway
            # The CLOB will reject if already filled

        # ── Step 2: Stake calculation ──────────────────────────────────
        stake = self._calculate_stake(decision)

        # ── Step 3: Risk check ─────────────────────────────────────────
        raw_status = self._risk.get_status()
        # Adapt dict→RiskStatus if the risk manager returns a dict (legacy)
        if isinstance(raw_status, dict):
            from domain.value_objects import RiskStatus

            risk_status = RiskStatus(
                current_bankroll=raw_status.get("current_bankroll", 500),
                peak_bankroll=raw_status.get("peak_bankroll", 500),
                drawdown_pct=raw_status.get("drawdown_pct", 0),
                daily_pnl=raw_status.get("daily_pnl", 0),
                consecutive_losses=raw_status.get("consecutive_losses", 0),
                paper_mode=raw_status.get("paper_mode", True),
                kill_switch_active=raw_status.get("kill_switch_active", False),
            )
        else:
            risk_status = raw_status
        approved, reason = self._check_risk(risk_status, stake)
        if not approved:
            logger.info(
                "execute_trade.risk_blocked",
                extra={
                    "strategy": sid,
                    "stake": stake.adjusted_stake,
                    "reason": reason,
                },
            )
            try:
                await self._alerter.send_system_alert(
                    f"BLOCKED {sid} {decision.strategy_version}\n"
                    f"Direction: {direction}\n"
                    f"Stake: ${stake.adjusted_stake:.2f}\n"
                    f"Reason: {reason}"
                )
            except Exception:
                pass
            return _failed(
                reason,
                strategy_id=sid,
                direction=direction,
                stake_usd=stake.adjusted_stake,
            )

        # ── Step 4: Guardrails ─────────────────────────────────────────
        ok, guard_reason = self._check_guardrails()
        if not ok:
            logger.info(
                "execute_trade.guardrail_blocked",
                extra={"strategy": sid, "reason": guard_reason},
            )
            return _failed(
                guard_reason,
                strategy_id=sid,
                direction=direction,
                stake_usd=stake.adjusted_stake,
            )

        # ── Step 5: Token ID resolution ────────────────────────────────
        if direction == "DOWN":
            token_id = window_market.down_token_id
            side = "NO"
        else:
            token_id = window_market.up_token_id
            side = "YES"

        if not token_id:
            logger.error(
                "execute_trade.no_token_id",
                extra={"strategy": sid, "direction": direction},
            )
            return _failed(
                "no_token_id",
                strategy_id=sid,
                direction=direction,
            )

        # ── Step 6: Execute order ──────────────────────────────────────
        entry_cap = decision.entry_cap or DEFAULT_ENTRY_CAP
        start_ts = self._clock.now()

        try:
            result = await self._executor.execute_order(
                token_id=token_id,
                side=side,
                stake_usd=stake.adjusted_stake,
                entry_cap=entry_cap,
                price_floor=PRICE_FLOOR,
            )
        except Exception as exc:
            self._on_order_error()
            logger.error(
                "execute_trade.execution_error",
                extra={"strategy": sid, "error": str(exc)[:200]},
            )
            return _failed(
                f"execution_error: {str(exc)[:200]}",
                strategy_id=sid,
                direction=direction,
                stake_usd=stake.adjusted_stake,
                token_id=token_id,
            )

        end_ts = self._clock.now()

        # Enrich result with strategy identity and timing
        result = replace(
            result,
            strategy_id=sid,
            direction=direction,
            execution_start=start_ts,
            execution_end=end_ts,
            market_slug=window_market.market_slug,
        )

        if not result.success:
            self._on_order_error()
            logger.info(
                "execute_trade.order_not_filled",
                extra={
                    "strategy": sid,
                    "reason": result.failure_reason,
                },
            )
            return result

        # ── Step 7: Record trade ───────────────────────────────────────
        try:
            await self._recorder.record_trade(decision, result, stake)
        except Exception as exc:
            # Fire-and-forget spirit: log but don't fail the trade
            logger.warning(
                "execute_trade.record_error",
                extra={"error": str(exc)[:200]},
            )

        # ── Step 8: Mark traded ────────────────────────────────────────
        try:
            await self._window_state.mark_traded(
                window_key,
                result.order_id or "unknown",
            )
        except Exception as exc:
            logger.warning(
                "execute_trade.mark_traded_error",
                extra={"error": str(exc)[:200]},
            )

        # ── Step 9: Telegram alert (rich strategy-aware format) ─────────
        try:
            gate_results = decision.metadata.get("gate_results", [])
            sizing_meta = decision.metadata.get("sizing", {})
            # Use rich strategy alert if available, fallback to plain text
            if hasattr(self._alerter, "send_strategy_trade_alert"):
                await self._alerter.send_strategy_trade_alert(
                    strategy_id=sid,
                    strategy_version=decision.strategy_version,
                    direction=direction,
                    confidence=decision.confidence or "?",
                    confidence_score=decision.confidence_score or 0.0,
                    entry_reason=decision.entry_reason,
                    gate_results=gate_results,
                    sizing_modifier=sizing_meta.get("modifier", 1.0),
                    sizing_label=sizing_meta.get("label", "default"),
                    fill_price=result.fill_price or 0.0,
                    fill_size=result.fill_size or 0.0,
                    stake_usd=result.stake_usd,
                    order_type=result.order_type,
                    btc_price=current_btc_price,
                    vpin=getattr(self, "_last_vpin", 0.0),
                    regime=getattr(self, "_last_regime", "?"),
                    eval_offset=getattr(decision, "metadata", {}).get("eval_offset")
                    if decision.metadata
                    else None,
                    paper_mode=self._paper_mode,
                    success=result.success,
                    failure_reason=result.failure_reason or "",
                    elapsed_s=(result.execution_end - result.execution_start)
                    if result.execution_end > result.execution_start
                    else 0.0,
                )
            else:
                alert_msg = self._format_trade_alert(
                    decision,
                    result,
                    stake,
                    current_btc_price,
                    open_price,
                )
                await self._alerter.send_system_alert(alert_msg)
        except Exception as exc:
            logger.warning(
                "execute_trade.alert_error",
                extra={"error": str(exc)[:200]},
            )

        # ── Step 10: Update guardrail state ────────────────────────────
        self._record_order_placed()
        self._on_order_success()

        logger.info(
            "execute_trade.success",
            extra={
                "strategy": sid,
                "direction": direction,
                "order_id": result.order_id,
                "fill_price": result.fill_price,
                "fill_size": result.fill_size,
                "stake": result.stake_usd,
                "mode": result.execution_mode,
            },
        )

        return result

    # ─── Stake Calculation ─────────────────────────────────────────────

    def _calculate_stake(
        self,
        decision: StrategyDecision,
    ) -> StakeCalculation:
        """Calculate stake from risk status and decision sizing.

        Formula: bankroll * bet_fraction * price_multiplier
        where price_multiplier = (1 - token_price) / 0.50, clamped [0.5, 1.5]

        This means:
          - 50c token -> 1.0x multiplier (base stake)
          - 40c token -> 1.2x multiplier (better R/R, bet more)
          - 65c token -> 0.7x multiplier (worse R/R, bet less)
        """
        risk = self._risk.get_status()
        bankroll = (
            risk.get("current_bankroll", runtime.starting_bankroll)
            if isinstance(risk, dict)
            else risk.current_bankroll
        )
        bet_fraction = decision.collateral_pct or runtime.bet_fraction

        base_stake = bankroll * bet_fraction

        # Token price estimate for R/R scaling
        tp = max(0.30, min(0.65, decision.entry_cap or 0.50))
        price_multiplier = (1.0 - tp) / 0.50
        price_multiplier = max(0.5, min(1.5, price_multiplier))

        adjusted = base_stake * price_multiplier

        # Hard caps: enforce the runtime-configured absolute max bet.
        hard_cap = min(runtime.max_position_usd, bankroll * bet_fraction * 0.95)
        adjusted = min(adjusted, hard_cap)
        adjusted = round(adjusted, 2)

        return StakeCalculation(
            base_stake=base_stake,
            price_multiplier=price_multiplier,
            adjusted_stake=adjusted,
            bankroll=bankroll,
            bet_fraction=bet_fraction,
            hard_cap=hard_cap,
        )

    # ─── Risk Check ────────────────────────────────────────────────────

    @staticmethod
    def _check_risk(
        status: RiskStatus,
        stake: StakeCalculation,
    ) -> tuple[bool, str]:
        """Validate trade against risk limits."""
        if status.kill_switch_active:
            return False, "kill_switch_active"
        if status.drawdown_pct > runtime.max_drawdown_kill:
            return (
                False,
                f"drawdown {status.drawdown_pct:.1%} > {runtime.max_drawdown_kill:.0%}",
            )
        min_bet = min(MIN_BET_USD, runtime.min_bet_usd)
        if stake.adjusted_stake < min_bet:
            return (
                False,
                f"stake ${stake.adjusted_stake:.2f} < ${min_bet:.2f} minimum",
            )
        return True, ""

    # ─── Guardrails ────────────────────────────────────────────────────

    def _check_guardrails(self) -> tuple[bool, str]:
        """Rate limit + circuit breaker checks."""
        now = self._clock.now()

        # Circuit breaker
        if self._circuit_break_until > now:
            remaining = self._circuit_break_until - now
            return False, f"circuit_breaker: {remaining:.0f}s remaining"

        # Rate limit: min interval between orders
        if self._last_order_time > 0:
            elapsed = now - self._last_order_time
            if elapsed < MIN_ORDER_INTERVAL_S:
                return False, f"rate_limit: {elapsed:.1f}s < {MIN_ORDER_INTERVAL_S}s"

        # Hourly cap
        cutoff = now - 3600.0
        self._order_timestamps = [ts for ts in self._order_timestamps if ts > cutoff]
        if len(self._order_timestamps) >= MAX_ORDERS_PER_HOUR:
            return (
                False,
                f"rate_limit: {len(self._order_timestamps)} >= {MAX_ORDERS_PER_HOUR}/hr",
            )

        return True, ""

    def _record_order_placed(self) -> None:
        """Track order timestamp for rate limiting."""
        now = self._clock.now()
        self._last_order_time = now
        self._order_timestamps.append(now)

    def _on_order_success(self) -> None:
        """Reset consecutive error counter on success."""
        self._consecutive_errors = 0

    def _on_order_error(self) -> None:
        """Track consecutive errors and trigger circuit breaker."""
        self._consecutive_errors += 1
        if self._consecutive_errors >= CIRCUIT_BREAKER_ERRORS:
            self._circuit_break_until = self._clock.now() + CIRCUIT_BREAKER_COOLDOWN_S
            logger.warning(
                "execute_trade.circuit_breaker_tripped",
                extra={
                    "errors": self._consecutive_errors,
                    "cooldown_s": CIRCUIT_BREAKER_COOLDOWN_S,
                },
            )

    # ─── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _make_window_key(market: WindowMarket) -> WindowKey:
        """Extract WindowKey from a WindowMarket's slug.

        Slug format: "btc-updown-5m-1713000000"
        """
        parts = market.market_slug.split("-")
        asset = parts[0].upper() if parts else "BTC"
        try:
            window_ts = int(parts[-1])
        except (ValueError, IndexError):
            window_ts = 0
        timeframe = "5m"
        if len(parts) >= 3:
            timeframe = parts[2]
        return WindowKey(asset=asset, window_ts=window_ts, timeframe=timeframe)

    @staticmethod
    def calculate_fee(price: float, stake: float) -> float:
        """Polymarket binary options fee: 7.2% * p * (1-p) * stake."""
        return FEE_MULTIPLIER * price * (1.0 - price) * stake

    def _format_trade_alert(
        self,
        decision: StrategyDecision,
        result: ExecutionResult,
        stake: StakeCalculation,
        btc_price: float,
        open_price: float,
    ) -> str:
        """Format the Telegram trade alert message.

        Strategy name always in subject line for immediate identification.
        """
        sid = decision.strategy_id
        ver = decision.strategy_version
        direction = decision.direction or "?"
        mode = result.execution_mode.upper()

        # Gate results summary
        gate_lines = ""
        gate_results = decision.metadata.get("gate_results", [])
        if gate_results:
            checks = []
            for g in gate_results:
                icon = "\u2705" if g.get("passed") else "\u274c"
                checks.append(f"{icon}{g.get('gate', '?')}")
            gate_lines = f"\n\u26a1 Gates: {' '.join(checks)}"

        # Sizing info
        sizing_meta = decision.metadata.get("sizing", {})
        size_label = sizing_meta.get("label", "default")
        modifier = sizing_meta.get("modifier", 1.0)

        # Delta from btc price
        delta_pct = (
            ((btc_price - open_price) / open_price * 100) if open_price > 0 else 0.0
        )

        lines = [
            f"TRADE {sid} {ver}",
            f"Direction: {direction} ({result.token_id[:20]}...)"
            if result.token_id
            else f"Direction: {direction}",
            f"Confidence: {decision.confidence or '?'} ({decision.confidence_score or 0:.2f})",
            "",
        ]

        if gate_lines:
            lines.append(gate_lines.strip())

        lines.extend(
            [
                f"\U0001f4b0 Sizing: {modifier:.1f}x ({size_label})",
                "",
                f"\u2705 {mode} {'FILLED' if result.success else 'FAILED'}",
            ]
        )

        if result.success and result.fill_price:
            fee = self.calculate_fee(result.fill_price, result.stake_usd)
            lines.extend(
                [
                    f"\U0001f4b5 Fill: ${result.fill_price:.2f} | "
                    f"Size: {result.fill_size:.1f} shares | "
                    f"Stake: ${result.stake_usd:.2f}",
                    f"\U0001f4b8 Fee: ${fee:.2f}",
                ]
            )
            if result.execution_end > result.execution_start:
                elapsed = result.execution_end - result.execution_start
                lines.append(f"\u23f1 Filled in {elapsed:.1f}s")
        elif result.failure_reason:
            lines.append(f"\u274c Reason: {result.failure_reason}")

        lines.extend(
            [
                "",
                f"Entry: {decision.entry_reason}",
                f"BTC: ${btc_price:,.0f} -> delta {delta_pct:+.2f}%",
            ]
        )

        if self._paper_mode:
            lines.insert(0, "\U0001f4dd PAPER MODE")

        return "\n".join(lines)
