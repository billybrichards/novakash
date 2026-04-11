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
        # ── v4 integration (PR B) ──
        v4_snapshot_port: Optional[V4SnapshotPort] = None,
        engine_use_v4_actions: bool = False,
        v4_primary_timescale: str = "15m",
        v4_timescales: tuple[str, ...] = ("5m", "15m", "1h", "4h"),
        v4_entry_edge: float = 0.10,
        v4_min_expected_move_bps: float = 15.0,
        v4_allow_mean_reverting: bool = False,
        fee_rate_per_side: float = 0.00045,  # Hyperliquid taker, for the reward/risk floor
        # ── legacy v2 path (unchanged from PR #10) ──
        min_conviction: float = 0.20,       # |p-0.5| >= 0.20 → p>0.70 or p<0.30
        regime_threshold: float = 0.50,     # |composite_1h| >= 0.50 to trade
        regime_timescale: str = "1h",       # composite horizon for regime gate
        bet_fraction: float = 0.02,
        stop_loss_pct: float = 0.006,       # 0.6% — 3x fee cost
        take_profit_pct: float = 0.005,     # 0.5% — 2.7x fee cost
        venue: str = "binance",             # tag every position with execution venue
        strategy_version: str = "v2-probability",  # tag every position with strategy
    ) -> None:
        self._exchange = exchange
        self._portfolio = portfolio
        self._repo = repository
        self._alerts = alerts
        self._probability_port = probability_port
        self._signal_port = signal_port
        # v4
        self._v4_port = v4_snapshot_port
        self._engine_use_v4_actions = engine_use_v4_actions
        self._v4_primary_timescale = v4_primary_timescale
        self._v4_timescales = v4_timescales
        self._v4_entry_edge = v4_entry_edge
        self._v4_min_expected_move_bps = v4_min_expected_move_bps
        self._v4_allow_mean_reverting = v4_allow_mean_reverting
        self._fee_rate_per_side = fee_rate_per_side
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
                asset="BTC", timescales=list(self._v4_timescales),
            )
            if v4 is not None:
                return await self._execute_v4(v4)
            else:
                logger.debug(
                    "v4 snapshot unavailable — falling back to legacy v2 path"
                )
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
                prob.probability_up, prob.conviction, self._min_conviction,
            )
            return None

        # ── 3. Regime filter (v3 composite magnitude) — SOFT ──
        # When regime_threshold is 0.0 this block only LOGS the composite
        # reading for post-hoc analysis; it does not block trading. Once
        # we have more regime-diverse data and can prove the gate adds
        # edge, bump regime_threshold above 0.0 to activate it.
        regime_signal = await self._signal_port.get_latest_signal(self._regime_timescale)
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
                    self._regime_timescale, regime_signal.strength,
                    self._regime_threshold, prob.probability_up,
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
                side.value, prob.asset, fill.fill_price.value,
                actual_notional.amount, fill.commission, prob.probability_up,
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
        if not payload.is_tradeable:
            self._log_skip("not_tradeable", v4, payload)
            return None

        side = payload.suggested_side

        # ── ② consensus.safe_to_trade ──
        if not v4.consensus.safe_to_trade:
            self._log_skip("consensus_fail", v4, payload)
            return None

        # ── ③ macro direction_gate ──
        # Only enforce when macro status is ok; otherwise ignore the gate
        # (macro observer unavailable shouldn't block trading indefinitely).
        if v4.macro.status == "ok":
            if v4.macro.direction_gate == "SKIP_UP" and side == TradeSide.LONG:
                self._log_skip("macro_skip_up", v4, payload)
                return None
            if v4.macro.direction_gate == "SKIP_DOWN" and side == TradeSide.SHORT:
                self._log_skip("macro_skip_down", v4, payload)
                return None

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

        # ── ⑥ conviction threshold ──
        if not payload.meets_threshold(self._v4_entry_edge):
            self._log_skip("conviction_below_threshold", v4, payload)
            return None

        # ── ⑦ expected move clears the fee wall ──
        if (
            payload.expected_move_bps is None
            or abs(payload.expected_move_bps) < self._v4_min_expected_move_bps
        ):
            self._log_skip("expected_move_below_fee_wall", v4, payload)
            return None

        # ── ⑧ portfolio risk gate (in-memory) ──
        # Calculate collateral with the v4 macro size modifier applied,
        # THEN run the risk check on the potentially-scaled size.
        # This mirrors legacy behaviour where risk gate runs before any
        # exchange side-effect.
        size_mult = v4.macro.size_modifier if v4.macro.status == "ok" else 1.0
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

        # ── ⑩ quantile-derived SL/TP + reward/risk floor ──
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
            v4_entry_macro_confidence=v4.macro.confidence if v4.macro.status == "ok" else None,
            v4_entry_expected_move_bps=payload.expected_move_bps,
            v4_entry_composite_v3=payload.composite_v3,
            v4_entry_consensus_safe=v4.consensus.safe_to_trade,
            v4_entry_window_close_ts=payload.window_close_ts
            if payload.window_close_ts > 0 else None,
            v4_snapshot_ts_at_entry=v4.ts,
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
                price=fill.fill_price.value * (
                    (1 - sl_pct) if side == TradeSide.LONG else (1 + sl_pct)
                )
            )
            position.take_profit = StopLevel(
                price=fill.fill_price.value * (
                    (1 + tp_pct) if side == TradeSide.LONG else (1 - tp_pct)
                )
            )

            await self._repo.save(position)
            await self._alerts.send_trade_opened(position)

            # Mark this window as traded — same dedupe semantics as legacy.
            if payload.window_close_ts > 0:
                self._last_traded_window_close_ts = payload.window_close_ts

            logger.info(
                "Position opened (v4): %s %s @ %.2f notional=%.2f p_up=%.3f "
                "regime=%s macro=%s/%d sl=%.2fbp tp=%.2fbp rr=%.2f",
                side.value, v4.asset, fill.fill_price.value,
                actual_notional.amount, payload.probability_up or 0.0,
                payload.regime, v4.macro.bias, v4.macro.confidence,
                sl_pct * 10000, tp_pct * 10000, tp_pct / sl_pct if sl_pct > 0 else 0.0,
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

        sl_pct = max(0.002, sl_abs / last_price)   # floor 20 bps
        tp_pct = max(0.003, tp_abs / last_price)   # floor 30 bps
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
            if payload and payload.probability_up is not None else "?",
            payload.regime if payload else "?",
            payload.status if payload else "?",
            v4.macro.bias, v4.macro.direction_gate, v4.macro.confidence,
            v4.consensus.safe_to_trade,
            f"{payload.expected_move_bps:.1f}"
            if payload and payload.expected_move_bps is not None else "?",
            v4.max_impact_in_window,
        )
