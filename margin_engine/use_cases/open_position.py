"""
Use case: Open a new margin position.

Two execution paths coexist, dispatched at the top of `execute()`:

─ v4 path (PR B) — when settings.engine_use_v4_actions is True AND a
  fresh /v4/snapshot is available. Walks a 10-gate decision stack,
  derives SL/TP from TimesFM quantiles, scales bet size by Claude's
  macro_bias modifier, and stamps the full v4 audit snapshot on the
  Position entity so post-trade analysis can reconstruct exactly what
  the engine saw at entry time.

─ legacy v2 path — the existing implementation from PR #10. Used as a
  fallback when v4 is unavailable, or when the feature flag is off.
  Reads a single ProbabilitySignal scalar, applies the soft composite
  regime filter, trades with hardcoded SL/TP from settings.

The legacy path is deliberately preserved intact so toggling the flag
back to False is a clean rollback — there's no state migration to undo.

═══════════════════════════════════════════════════════════════════════
v4 gate stack (evaluated in order, cheapest first)
═══════════════════════════════════════════════════════════════════════

  ①  primary timescale tradeable?         (in-memory check)
  ②  consensus.safe_to_trade                (in-memory)
  ③  macro.direction_gate permits side      (in-memory)
  ④  minutes_to_next_high_impact >= 30      (in-memory)
  ⑤  regime != MEAN_REVERTING or opt-in     (in-memory)
  ⑥  |p_up - 0.5| >= v4_entry_edge          (in-memory)
  ⑦  |expected_move_bps| >= fee wall        (in-memory)
  ⑧  portfolio.can_open_position            (in-memory)
  ⑨  balance query                           (1 exchange call)
  ⑩  SL/TP from quantiles + reward/risk     (math)
     (followed by order placement — first side-effecting call)

Any gate failure returns None with a structured skip log that includes
the gate name and the full v4 context, so operators can bucket skip
rates by reason in a single query.

═══════════════════════════════════════════════════════════════════════
Legacy v2 path (unchanged from PR #10)
═══════════════════════════════════════════════════════════════════════

  1. Probability freshness < 2min
  2. |p_up - 0.5| >= min_conviction
  3. |composite_1h| >= regime_threshold (soft, 0.0 default)
  4. Portfolio risk gate
  5. Market order + fill
"""

from __future__ import annotations

import logging
from typing import Optional

from margin_engine.domain.entities.portfolio import Portfolio
from margin_engine.domain.entities.position import Position
from margin_engine.domain.ports import (
    AlertPort,
    ExchangePort,
    PositionRepository,
    ProbabilityPort,
    SignalPort,
    V4SnapshotPort,
)
from margin_engine.domain.value_objects import (
    CompositeSignal,
    Money,
    Price,
    ProbabilitySignal,
    StopLevel,
    TimescalePayload,
    TradeSide,
    V4Snapshot,
)
from margin_engine.services.regime_adaptive import RegimeAdaptiveRouter

# Lazy import to avoid circular dependency

logger = logging.getLogger(__name__)


class OpenPositionUseCase:
    """
    ML-directed entry logic. See module docstring for the rationale.

    Dependencies are all domain ports; adapters are injected at wire time.
    """

    def __init__(
        self,
        exchange: ExchangePort,
        portfolio: Portfolio,
        repository: PositionRepository,
        alerts: AlertPort,
        probability_port: ProbabilityPort,
        signal_port: SignalPort,
        *,
        # ── Strategy decision recorder (V4) ──
        strategy_decision_recorder: Optional[Any] = None,
        # ── v4 integration (PR B) ──
        v4_snapshot_port: Optional[V4SnapshotPort] = None,
        engine_use_v4_actions: bool = False,
        v4_primary_timescale: str = "15m",
        v4_timescales: tuple[str, ...] = ("5m", "15m", "1h", "4h"),
        v4_entry_edge: float = 0.10,
        v4_min_expected_move_bps: float = 15.0,
        v4_fee_wall_continuation_divisor: float = 3.0,
        fee_aware_continuation_enabled: bool = False,
        v4_allow_mean_reverting: bool = False,
        # ── Phase A (2026-04-11): macro advisory mode ──
        v4_macro_mode: str = "advisory",  # "veto" | "advisory"
        v4_macro_hard_veto_confidence_floor: int = 80,
        v4_macro_advisory_size_mult_on_conflict: float = 0.75,
        # ── Phase A: experimental NO_EDGE override (shipped off) ──
        v4_allow_no_edge_if_exp_move_bps_gte: Optional[float] = None,
        # ── DQ-07: defensive mark-divergence gate (default OFF) ──
        # When > 0, gate 9.5 in _execute_v4 fetches exchange.get_mark and
        # rejects the trade if it diverges from v4.last_price by more than
        # this many bps. Agent D's DQ-05 recommendation as a regression
        # safety rail — see settings.py for full rationale.
        v4_max_mark_divergence_bps: float = 0.0,
        fee_rate_per_side: float = 0.00045,  # Hyperliquid taker, for the reward/risk floor
        # ── ME-STRAT-04: regime-adaptive strategy (default OFF) ──
        regime_adaptive_enabled: bool = False,
        regime_trend_min_prob: float = 0.55,
        regime_trend_size_mult: float = 1.2,
        regime_trend_stop_bps: int = 150,
        regime_trend_tp_bps: int = 200,
        regime_trend_hold_minutes: int = 60,
        regime_trend_min_expected_move_bps: float = 30.0,
        regime_mr_entry_threshold: float = 0.70,
        regime_mr_size_mult: float = 0.8,
        regime_mr_stop_bps: int = 80,
        regime_mr_tp_bps: int = 50,
        regime_mr_hold_minutes: int = 15,
        regime_mr_min_fade_conviction: float = 0.55,
        regime_no_trade_allow: bool = False,
        regime_no_trade_size_mult: float = 0.1,
        # ── legacy v2 path (unchanged from PR #10) ──
        min_conviction: float = 0.20,  # |p-0.5| >= 0.20 → p>0.70 or p<0.30
        regime_threshold: float = 0.50,  # |composite_1h| >= 0.50 to trade
        regime_timescale: str = "1h",  # composite horizon for regime gate
        bet_fraction: float = 0.02,
        stop_loss_pct: float = 0.006,  # 0.6% — 3x fee cost
        take_profit_pct: float = 0.005,  # 0.5% — 2.7x fee cost
        venue: str = "binance",  # tag every position with execution venue
        strategy_version: str = "v2-probability",  # tag every position with strategy
    ) -> None:
        self._exchange = exchange
        self._portfolio = portfolio
        self._repo = repository
        self._alerts = alerts
        self._probability_port = probability_port
        self._signal_port = signal_port
        # Strategy decision recorder
        self._strategy_decision_recorder = strategy_decision_recorder
        # v4
        self._v4_port = v4_snapshot_port
        self._engine_use_v4_actions = engine_use_v4_actions
        self._v4_primary_timescale = v4_primary_timescale
        self._v4_timescales = v4_timescales
        self._v4_entry_edge = v4_entry_edge
        self._v4_min_expected_move_bps = v4_min_expected_move_bps
        self._fee_wall_continuation_divisor = v4_fee_wall_continuation_divisor
        self._fee_aware_continuation_enabled = fee_aware_continuation_enabled
        self._v4_allow_mean_reverting = v4_allow_mean_reverting
        # Phase A macro-mode fields
        if v4_macro_mode not in ("veto", "advisory"):
            raise ValueError(
                f"v4_macro_mode must be 'veto' or 'advisory', got {v4_macro_mode!r}"
            )
        self._macro_mode = v4_macro_mode
        self._macro_hard_veto_confidence_floor = v4_macro_hard_veto_confidence_floor
        self._macro_advisory_conflict_mult = v4_macro_advisory_size_mult_on_conflict
        self._allow_no_edge_exp_move_override = v4_allow_no_edge_if_exp_move_bps_gte
        # DQ-07 defensive gate — 0.0 / negative = no-op
        self._v4_max_mark_divergence_bps = v4_max_mark_divergence_bps
        self._fee_rate_per_side = fee_rate_per_side
        # ME-STRAT-04: regime-adaptive strategy
        self._regime_adaptive_enabled = regime_adaptive_enabled
        if regime_adaptive_enabled:
            from margin_engine.services.regime_trend import TrendStrategyConfig
            from margin_engine.services.regime_mean_reversion import MeanReversionConfig

            self._regime_router = RegimeAdaptiveRouter(
                trend_config=TrendStrategyConfig(
                    min_probability=regime_trend_min_prob,
                    size_mult=regime_trend_size_mult,
                    stop_loss_bps=regime_trend_stop_bps,
                    take_profit_bps=regime_trend_tp_bps,
                    hold_minutes=regime_trend_hold_minutes,
                    min_expected_move_bps=regime_trend_min_expected_move_bps,
                ),
                mean_reversion_config=MeanReversionConfig(
                    entry_threshold=regime_mr_entry_threshold,
                    size_mult=regime_mr_size_mult,
                    stop_loss_bps=regime_mr_stop_bps,
                    take_profit_bps=regime_mr_tp_bps,
                    hold_minutes=regime_mr_hold_minutes,
                    min_fade_conviction=regime_mr_min_fade_conviction,
                ),
                no_trade_allow=regime_no_trade_allow,
                no_trade_size_mult=regime_no_trade_size_mult,
            )
        else:
            self._regime_router = None
        # legacy
        self._min_conviction = min_conviction
        self._regime_threshold = regime_threshold
        self._regime_timescale = regime_timescale
        self._bet_fraction = bet_fraction
        self._stop_loss_pct = stop_loss_pct
        self._take_profit_pct = take_profit_pct
        self._venue = venue
        self._strategy_version = strategy_version

        # Track which 15m window we most recently traded in, so we don't
        # re-enter the same window if the poller sees the signal again.
        # Shared between v4 and v2 paths.
        self._last_traded_window_close_ts: Optional[int] = None

    async def execute(self) -> Optional[Position]:
        """
        Dispatch entry decision to v4 or legacy path.

        Priority:
          1. v4 path if flag is on AND v4 snapshot is available and fresh
          2. legacy v2 path otherwise (original PR #10 behavior)

        The v4 path may return None without falling through to legacy —
        that means the gates actively rejected the trade with a specific
        reason. Only "v4 unavailable" (snapshot is None) triggers fallback.
        """
        # ── v4 path (flag-gated, fresh snapshot required) ──
        if self._engine_use_v4_actions and self._v4_port is not None:
            v4 = await self._v4_port.get_latest(
                asset="BTC",
                timescales=list(self._v4_timescales),
            )
            if v4 is not None:
                return await self._execute_v4(v4)
            else:
                logger.debug("v4 snapshot unavailable — falling back to legacy v2 path")
                # fall through

        # ── Legacy v2 path (PR #10) ──
        return await self._execute_legacy()

    async def _execute_legacy(self) -> Optional[Position]:
        """
        Original PR #10 entry logic — reads a single ProbabilitySignal,
        applies soft composite regime filter, trades with fixed SL/TP.

        Kept intact so toggling engine_use_v4_actions=False is a clean
        rollback to the known-working pre-PR-B behavior.
        """
        # ── 1. Probability freshness ──
        prob = await self._probability_port.get_latest(asset="BTC", timescale="15m")
        if prob is None:
            logger.debug("No fresh probability available — skipping tick")
            return None

        # ── 0. Window dedupe ──
        if (
            self._last_traded_window_close_ts is not None
            and prob.window_close_ts == self._last_traded_window_close_ts
        ):
            return None

        # ── 2. Conviction threshold ──
        if not prob.meets_threshold(self._min_conviction):
            logger.info(
                "Probability below conviction threshold: p_up=%.3f "
                "conviction=%.3f < %.3f",
                prob.probability_up,
                prob.conviction,
                self._min_conviction,
            )
            return None

        # ── 3. Regime filter (v3 composite magnitude) — SOFT ──
        # When regime_threshold is 0.0 this block only LOGS the composite
        # reading for post-hoc analysis; it does not block trading. Once
        # we have more regime-diverse data and can prove the gate adds
        # edge, bump regime_threshold above 0.0 to activate it.
        regime_signal = await self._signal_port.get_latest_signal(
            self._regime_timescale
        )
        regime_strength = regime_signal.strength if regime_signal else None
        if self._regime_threshold > 0.0:
            if regime_signal is None:
                logger.info(
                    "Regime data missing (%s composite) — skipping tick",
                    self._regime_timescale,
                )
                return None
            if regime_signal.strength < self._regime_threshold:
                logger.info(
                    "Regime filter blocked: |composite_%s|=%.3f < %.3f "
                    "(p_up=%.3f would have traded)",
                    self._regime_timescale,
                    regime_signal.strength,
                    self._regime_threshold,
                    prob.probability_up,
                )
                return None

        # ── 4. Compute size and run risk gates ──
        balance = await self._exchange.get_balance()
        collateral = Money.usd(balance.amount * self._bet_fraction)
        requested_notional = collateral * self._portfolio.leverage

        allowed, reason = self._portfolio.can_open_position(collateral)
        if not allowed:
            logger.info("Position blocked by risk gate: %s", reason)
            return None

        side = prob.suggested_side

        # ── 5. Create + fill ──
        # venue and strategy_version are stamped here so every position
        # written by this use case is unambiguously attributable, even
        # months later when looking at margin_positions in the database.
        position = Position(
            asset=prob.asset,
            side=side,
            leverage=self._portfolio.leverage,
            # Store the probability as the "entry score" for audit — this
            # matches the schema's entry_signal_score column but now
            # represents a calibrated probability, not a composite blend.
            entry_signal_score=prob.probability_up,
            entry_timescale=prob.timescale,
            venue=self._venue,
            strategy_version=self._strategy_version,
        )
        self._portfolio.add_position(position)

        try:
            fill = await self._exchange.place_market_order(
                symbol=f"{prob.asset}USDT",
                side=side,
                notional=requested_notional,
            )

            actual_notional = (
                Money.usd(fill.filled_notional)
                if fill.filled_notional > 0
                else requested_notional
            )

            position.confirm_entry(
                price=fill.fill_price,
                notional=actual_notional,
                collateral=collateral,
                order_id=fill.order_id,
                commission=fill.commission,
                commission_is_actual=fill.commission_is_actual,
            )

            # Stops derived from the actual fill, not the pre-order estimate.
            position.stop_loss = self._compute_stop_loss(fill.fill_price, side)
            position.take_profit = self._compute_take_profit(fill.fill_price, side)

            await self._repo.save(position)
            await self._alerts.send_trade_opened(position)

            # Mark this window as traded so we don't re-enter it.
            self._last_traded_window_close_ts = prob.window_close_ts

            logger.info(
                "Position opened (v2 ML): %s %s @ %.2f notional=%.2f "
                "commission=%.4f p_up=%.3f regime=%s",
                side.value,
                prob.asset,
                fill.fill_price.value,
                actual_notional.amount,
                fill.commission,
                prob.probability_up,
                f"{regime_strength:.3f}" if regime_strength is not None else "n/a",
            )
            return position

        except Exception as e:
            logger.error("Order placement failed: %s", e)
            await self._alerts.send_error(f"Order failed: {e}")
            if position in self._portfolio.positions:
                self._portfolio.positions.remove(position)
            return None

    def _compute_stop_loss(self, price: Price, side: TradeSide) -> StopLevel:
        if side == TradeSide.LONG:
            return StopLevel(price=price.value * (1 - self._stop_loss_pct))
        else:
            return StopLevel(price=price.value * (1 + self._stop_loss_pct))

    def _compute_take_profit(self, price: Price, side: TradeSide) -> StopLevel:
        if side == TradeSide.LONG:
            return StopLevel(price=price.value * (1 + self._take_profit_pct))
        else:
            return StopLevel(price=price.value * (1 - self._take_profit_pct))

    # ═══════════════════════════════════════════════════════════════════
    # v4 entry path (PR B)
    # ═══════════════════════════════════════════════════════════════════

    async def _execute_v4(self, v4: V4Snapshot) -> Optional[Position]:
        """
        10-gate v4 decision stack. Ordered cheapest-first: every gate
        except #9 (balance query) and #10 (order placement) is in-memory,
        so a rejected trade costs us one list lookup and a few conditionals.

        Returns the opened Position on success, or None with a structured
        skip log on any gate failure.
        """
        payload = v4.timescales.get(self._v4_primary_timescale)
        if payload is None:
            self._log_skip("primary_timescale_missing", v4, None)
            return None

        # ── 0. Window dedupe — don't re-enter the same window twice ──
        if (
            self._last_traded_window_close_ts is not None
            and payload.window_close_ts == self._last_traded_window_close_ts
            and payload.window_close_ts > 0
        ):
            return None

        # ── ① tradeable state (status=ok, prob not None, regime not CHOPPY/NO_EDGE) ──
        # Phase A: optional NO_EDGE override. When the flag is set and TimesFM's
        # quantile-derived expected move clears the bar, allow a NO_EDGE candidate
        # through the gate stack. This exists to capture the 2026-04-11 audit
        # finding of a 100%-hit-rate bucket (NO_EDGE + BEAR + exp_move>3, n=74),
        # but ships OFF until a 7-day replay confirms the edge is real.
        is_tradeable = payload.is_tradeable
        if (
            not is_tradeable
            and payload.regime == "NO_EDGE"
            and self._allow_no_edge_exp_move_override is not None
            and payload.expected_move_bps is not None
            and abs(payload.expected_move_bps) >= self._allow_no_edge_exp_move_override
        ):
            is_tradeable = True
            logger.info(
                "v4 entry: NO_EDGE override applied (exp_move=%.1f bps >= thr=%.1f)",
                payload.expected_move_bps,
                self._allow_no_edge_exp_move_override,
            )
        if not is_tradeable:
            self._log_skip("not_tradeable", v4, payload)
            return None

        side = payload.suggested_side

        # ── ② consensus.safe_to_trade ──
        if not v4.consensus.safe_to_trade:
            self._log_skip("consensus_fail", v4, payload)
            return None

        # ── ③ macro direction_gate ──
        # Phase A (2026-04-11): demoted from hard veto to advisory by default.
        # 24h audit showed Qwen BEAR calls at 20-30% directional hit rate —
        # actively anti-predictive. See docs/MACRO_AUDIT_2026-04-11.md.
        #
        # Two axes govern the gate:
        #   - macro.status: only ok rows are considered (unavailable → no-op)
        #   - macro.confidence >= floor: low-confidence calls are always no-ops
        #                                regardless of mode (prevents a flat
        #                                NEUTRAL/0 fallback row from silently
        #                                scaling down every entry)
        #
        # When both conditions hold AND direction_gate opposes side:
        #   - veto mode     → skip with a specific reason, return None
        #   - advisory mode → set macro_conflict=True, continue walking gates.
        #                     The conflict is consumed at gate ⑧ where
        #                     size_mult is multiplied by the advisory haircut.
        macro_conflict = False
        if (
            v4.macro.status == "ok"
            and v4.macro.confidence >= self._macro_hard_veto_confidence_floor
        ):
            if v4.macro.direction_gate == "SKIP_UP" and side == TradeSide.LONG:
                if self._macro_mode == "veto":
                    self._log_skip("macro_skip_up_veto", v4, payload)
                    return None
                macro_conflict = True
            elif v4.macro.direction_gate == "SKIP_DOWN" and side == TradeSide.SHORT:
                if self._macro_mode == "veto":
                    self._log_skip("macro_skip_down_veto", v4, payload)
                    return None
                macro_conflict = True

        # ── ④ high-impact event guard — no new entries within 30 min of HIGH/EXTREME ──
        if (
            v4.max_impact_in_window in ("HIGH", "EXTREME")
            and v4.minutes_to_next_high_impact is not None
            and v4.minutes_to_next_high_impact < 30
        ):
            self._log_skip("high_impact_event_within_30min", v4, payload)
            return None

        # ── ⑤ regime opt-in (MEAN_REVERTING blocked unless explicitly allowed) ──
        if payload.regime == "MEAN_REVERTING" and not self._v4_allow_mean_reverting:
            self._log_skip("mean_reverting_not_allowed", v4, payload)
            return None

        # ── ⑤.5 ME-STRAT-04: regime-adaptive strategy decision ──
        # When enabled, route to strategy-specific parameters (size_mult, SL, TP, hold_time).
        # If the strategy returns no trade, skip with a specific reason.
        # This adds TO existing gates, not replaces them.
        _regime_decision = None
        if self._regime_adaptive_enabled and self._regime_router is not None:
            regime_decision = self._regime_router.decide(v4)
            if not regime_decision.is_trade:
                self._log_skip(
                    f"regime_strategy_no_trade:{regime_decision.reason}",
                    v4,
                    payload,
                )
                return None
            # Log the regime strategy decision for audit
            logger.info(
                "v4 entry: regime strategy decision — %s size_mult=%.2f "
                "stop=%.1fbp tp=%.1fbp hold=%dm",
                regime_decision.reason,
                regime_decision.size_mult,
                regime_decision.stop_loss_bps,
                regime_decision.take_profit_bps,
                regime_decision.hold_minutes,
            )
            # Store decision for later use (size_mult, SL/TP override)
            _regime_decision = regime_decision

            # Record the strategy decision for backtesting
            if self._strategy_decision_recorder is not None:
                self._strategy_decision_recorder.record_decision(
                    position_id="pending",  # Will be updated when position is created
                    strategy_id="regime_adaptive",
                    decision=f"TRADE_{side.value}",
                    asset=v4.asset,
                    confidence=1.0,  # Regime strategy always trades if it passes
                    timescale=self._v4_primary_timescale,
                    regime=payload.regime,
                    v4_snapshot=v4.to_dict() if hasattr(v4, "to_dict") else None,
                    rationale=f"Regime {regime_decision.reason} with size_mult={regime_decision.size_mult}",
                    size_mult=regime_decision.size_mult,
                    hold_minutes=regime_decision.hold_minutes,
                )

        # ── ⑥ conviction threshold ──
        if not payload.meets_threshold(self._v4_entry_edge):
            self._log_skip("conviction_below_threshold", v4, payload)
            return None

        # ── ⑦ expected move clears the fee wall ──
        # When fee-aware continuation is enabled, positions can ride multiple
        # windows, so the round-trip fee is amortized across the holding period.
        # Divide the wall by the continuation divisor (default 3 = ~3 windows).
        effective_fee_wall = self._v4_min_expected_move_bps
        if (
            self._fee_aware_continuation_enabled
            and self._fee_wall_continuation_divisor > 1.0
        ):
            effective_fee_wall = (
                self._v4_min_expected_move_bps / self._fee_wall_continuation_divisor
            )
        if (
            payload.expected_move_bps is None
            or abs(payload.expected_move_bps) < effective_fee_wall
        ):
            self._log_skip("expected_move_below_fee_wall", v4, payload)
            return None

        # ── ⑧ portfolio risk gate (in-memory) ──
        # Calculate collateral with the v4 macro size modifier applied,
        # THEN run the risk check on the potentially-scaled size.
        # This mirrors legacy behaviour where risk gate runs before any
        # exchange side-effect.
        #
        # Phase A: when macro is in advisory mode and flagged a conflict
        # at gate ③ (macro opposes side at confidence >= floor), apply
        # the advisory haircut on top of whatever size_modifier Qwen
        # returned. This reduces exposure without blocking the trade.
        # Start with macro size modifier
        size_mult = v4.macro.size_modifier if v4.macro.status == "ok" else 1.0
        if macro_conflict:
            size_mult *= self._macro_advisory_conflict_mult
            logger.info(
                "v4 entry: macro advisory conflict — size_mult *= %.2f "
                "(final %.3f, macro=%s/%d/%s, side=%s)",
                self._macro_advisory_conflict_mult,
                size_mult,
                v4.macro.bias,
                v4.macro.confidence,
                v4.macro.direction_gate,
                side.value,
            )

        # Apply regime strategy size multiplier (if enabled and decision exists)
        if _regime_decision is not None:
            size_mult *= _regime_decision.size_mult
            logger.info(
                "v4 entry: regime size mult applied — total size_mult=%.3f "
                "(macro=%.3f × regime=%.2f)",
                size_mult,
                v4.macro.size_modifier if v4.macro.status == "ok" else 1.0,
                _regime_decision.size_mult,
            )

        preliminary_collateral = Money.usd(
            self._portfolio.starting_capital.amount * self._bet_fraction * size_mult
        )
        allowed, reason = self._portfolio.can_open_position(preliminary_collateral)
        if not allowed:
            logger.info("v4 entry blocked by risk gate: %s", reason)
            return None

        # ── ⑨ balance query (first and only exchange call before placement) ──
        balance = await self._exchange.get_balance()
        collateral = Money.usd(balance.amount * self._bet_fraction * size_mult)
        requested_notional = collateral * self._portfolio.leverage

        # ── 9.5 (DQ-07): defensive mark-price divergence check ──
        # v4.last_price is Binance spot from the assembler. The SL/TP ratio
        # math below is mathematically consistent regardless of venue, but a
        # stale/mispriced anchor can still trigger an entry off a bad price
        # (stale spot tick, Hyperliquid basis spike, cross-region latency).
        # When this setting is > 0, we compare against the exchange's live
        # mark and reject if the divergence exceeds the threshold.
        #
        # Ships DEFAULT OFF (0.0 = no-op). Operators flip via
        # MARGIN_V4_MAX_MARK_DIVERGENCE_BPS=20 when ready to activate.
        if self._v4_max_mark_divergence_bps > 0:
            exchange_mark: Optional[float] = None
            try:
                mark_price = await self._exchange.get_mark(
                    symbol=f"{v4.asset}USDT",
                    side=side,
                )
                # get_mark returns a Price value object with a .value float.
                exchange_mark = (
                    float(mark_price.value)
                    if hasattr(mark_price, "value")
                    else float(mark_price)
                )
            except Exception as exc:
                # Graceful degradation: a transient exchange error must not
                # block trades. Log loudly and let the candidate through.
                logger.warning(
                    "dq07.mark_query_failed — graceful passthrough: %s",
                    str(exc)[:200],
                )
                exchange_mark = None

            if (
                exchange_mark is not None
                and v4.last_price is not None
                and v4.last_price > 0
            ):
                divergence_bps = (
                    abs(exchange_mark - v4.last_price) / v4.last_price * 10_000.0
                )
                if divergence_bps > self._v4_max_mark_divergence_bps:
                    logger.warning(
                        "dq07.mark_divergence_gate_failed: "
                        "v4_last_price=%.4f exchange_mark=%.4f "
                        "divergence_bps=%.2f threshold_bps=%.2f side=%s",
                        v4.last_price,
                        exchange_mark,
                        round(divergence_bps, 2),
                        self._v4_max_mark_divergence_bps,
                        side.value,
                    )
                    self._log_skip("mark_divergence", v4, payload)
                    return None
                # Pass log at DEBUG so there's no noise when the gate is hot.
                logger.debug(
                    "dq07.mark_divergence_gate_passed: "
                    "divergence_bps=%.2f threshold_bps=%.2f",
                    round(divergence_bps, 2),
                    self._v4_max_mark_divergence_bps,
                )

        # ── ⑩ SL/TP from regime strategy or quantiles + reward/risk floor ──
        # If regime strategy is enabled, use its SL/TP; otherwise use quantiles
        if _regime_decision is not None:
            sl_pct = _regime_decision.stop_loss_pct
            tp_pct = _regime_decision.take_profit_pct
            logger.info(
                "v4 entry: regime SL/TP used — stop=%.1fbp tp=%.1fbp rr=%.2f",
                _regime_decision.stop_loss_bps,
                _regime_decision.take_profit_bps,
                _regime_decision.reward_risk_ratio,
            )
        else:
            sl_pct, tp_pct = self._sl_tp_from_quantiles(side, payload, v4.last_price)

        # Fee wall: round-trip cost × safety factor
        fee_budget_pct = self._fee_rate_per_side * 2
        if tp_pct < fee_budget_pct * 1.3:
            self._log_skip("tp_below_fee_wall", v4, payload)
            return None
        if sl_pct <= 0 or (tp_pct / sl_pct) < 1.2:
            self._log_skip("win_ratio_below_1.2", v4, payload)
            return None

        # ── All gates passed. Build Position with full v4 audit snapshot. ──
        position = Position(
            asset=v4.asset,
            side=side,
            leverage=self._portfolio.leverage,
            entry_signal_score=payload.probability_up or 0.0,
            entry_timescale=self._v4_primary_timescale,
            venue=self._venue,
            strategy_version=self._strategy_version,
            # v4 audit snapshot — frozen at entry for post-trade analysis
            v4_entry_regime=payload.regime,
            v4_entry_macro_bias=v4.macro.bias if v4.macro.status == "ok" else None,
            v4_entry_macro_confidence=v4.macro.confidence
            if v4.macro.status == "ok"
            else None,
            v4_entry_expected_move_bps=payload.expected_move_bps,
            v4_entry_composite_v3=payload.composite_v3,
            v4_entry_consensus_safe=v4.consensus.safe_to_trade,
            v4_entry_window_close_ts=payload.window_close_ts
            if payload.window_close_ts > 0
            else None,
            v4_snapshot_ts_at_entry=v4.ts,
            # ME-STRAT-04: regime strategy audit fields
            v4_entry_strategy_decision=(
                _regime_decision.reason if _regime_decision else None
            ),
            v4_entry_strategy_size_mult=(
                _regime_decision.size_mult if _regime_decision else None
            ),
            v4_entry_strategy_hold_minutes=(
                _regime_decision.hold_minutes if _regime_decision else None
            ),
        )
        self._portfolio.add_position(position)

        try:
            fill = await self._exchange.place_market_order(
                symbol=f"{v4.asset}USDT",
                side=side,
                notional=requested_notional,
            )

            actual_notional = (
                Money.usd(fill.filled_notional)
                if fill.filled_notional > 0
                else requested_notional
            )

            position.confirm_entry(
                price=fill.fill_price,
                notional=actual_notional,
                collateral=collateral,
                order_id=fill.order_id,
                commission=fill.commission,
                commission_is_actual=fill.commission_is_actual,
            )

            # Quantile-derived stops applied to the ACTUAL fill price, not the
            # snapshot's last_price (slippage may have moved us a few bps).
            position.stop_loss = StopLevel(
                price=fill.fill_price.value
                * ((1 - sl_pct) if side == TradeSide.LONG else (1 + sl_pct))
            )
            position.take_profit = StopLevel(
                price=fill.fill_price.value
                * ((1 + tp_pct) if side == TradeSide.LONG else (1 - tp_pct))
            )

            await self._repo.save(position)
            await self._alerts.send_trade_opened(position)

            # Mark this window as traded — same dedupe semantics as legacy.
            if payload.window_close_ts > 0:
                self._last_traded_window_close_ts = payload.window_close_ts

            logger.info(
                "Position opened (v4): %s %s @ %.2f notional=%.2f p_up=%.3f "
                "regime=%s macro=%s/%d sl=%.2fbp tp=%.2fbp rr=%.2f",
                side.value,
                v4.asset,
                fill.fill_price.value,
                actual_notional.amount,
                payload.probability_up or 0.0,
                payload.regime,
                v4.macro.bias,
                v4.macro.confidence,
                sl_pct * 10000,
                tp_pct * 10000,
                tp_pct / sl_pct if sl_pct > 0 else 0.0,
            )
            return position

        except Exception as e:
            logger.error("v4 order placement failed: %s", e)
            await self._alerts.send_error(f"v4 order failed: {e}")
            if position in self._portfolio.positions:
                self._portfolio.positions.remove(position)
            return None

    def _sl_tp_from_quantiles(
        self,
        side: TradeSide,
        payload: TimescalePayload,
        last_price: Optional[float],
    ) -> tuple[float, float]:
        """
        Derive SL/TP percentages from TimesFM p10/p90 at the window close.

        For LONG:
          SL = 1.25 × (last - p10) / last   (25% buffer below worst-case)
          TP = 0.85 × (p90 - last) / last   (15% headroom below best-case)

        For SHORT: mirrored.

        Floors: 20 bps SL, 30 bps TP — keeps stops wider than the bid-ask
        spread during flat markets when TimesFM's quantiles collapse.

        Falls back to the instance's fixed stop_loss_pct / take_profit_pct
        settings when quantiles or last_price are unavailable.
        """
        p10 = payload.quantiles_at_close.p10
        p90 = payload.quantiles_at_close.p90
        if p10 is None or p90 is None or last_price is None or last_price <= 0:
            return self._stop_loss_pct, self._take_profit_pct

        if side == TradeSide.LONG:
            sl_abs = (last_price - p10) * 1.25
            tp_abs = (p90 - last_price) * 0.85
        else:
            sl_abs = (p90 - last_price) * 1.25
            tp_abs = (last_price - p10) * 0.85

        sl_pct = max(0.002, sl_abs / last_price)  # floor 20 bps
        tp_pct = max(0.003, tp_abs / last_price)  # floor 30 bps
        return sl_pct, tp_pct

    def _log_skip(
        self,
        reason: str,
        v4: V4Snapshot,
        payload: Optional[TimescalePayload],
    ) -> None:
        """
        Structured skip log — all fields named so post-hoc grep works.

        Every skip logs at INFO with the specific gate reason, the primary
        timescale state, and the macro / consensus context. This is the
        dataset we'll use to measure skip distributions and tune gate
        thresholds over time.
        """
        logger.info(
            "v4 entry skip: reason=%s primary_ts=%s p_up=%s regime=%s status=%s "
            "macro=%s/%s confidence=%d consensus_safe=%s "
            "expected_move=%s event=%s",
            reason,
            self._v4_primary_timescale,
            f"{payload.probability_up:.3f}"
            if payload and payload.probability_up is not None
            else "?",
            payload.regime if payload else "?",
            payload.status if payload else "?",
            v4.macro.bias,
            v4.macro.direction_gate,
            v4.macro.confidence,
            v4.consensus.safe_to_trade,
            f"{payload.expected_move_bps:.1f}"
            if payload and payload.expected_move_bps is not None
            else "?",
            v4.max_impact_in_window,
        )
