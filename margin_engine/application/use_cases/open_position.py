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
    Money,
    Price,
)
from margin_engine.adapters.signal.v4_models import V4Snapshot

from .entry_strategies import V4Strategy, V2Strategy
from ..dto import OpenPositionInput, OpenPositionOutput

logger = logging.getLogger(__name__)


class OpenPositionUseCase:
    """
    ML-directed entry logic. Dispatches to v4 or v2 strategy.

    Dependencies are all domain ports; adapters are injected at wire time.
    """

    def __init__(
        self,
        input: OpenPositionInput,
    ) -> None:
        self._input = input
        self._exchange = input.exchange
        self._portfolio = input.portfolio
        self._repo = input.repository
        self._alerts = input.alerts
        self._probability_port = input.probability_port
        self._signal_port = input.signal_port
        self._v4_port = input.v4_snapshot_port
        self._engine_use_v4_actions = input.engine_use_v4_actions
        self._v4_timescales = input.v4_timescales

        # v4 shared config
        self._bet_fraction = input.bet_fraction

        # Build v4 strategy
        self._v4_strategy = V4Strategy(
            exchange=input.exchange,
            portfolio=input.portfolio,
            repository=input.repository,
            alerts=input.alerts,
            v4_primary_timescale=input.v4_primary_timescale,
            v4_entry_edge=input.v4_entry_edge,
            v4_min_expected_move_bps=input.v4_min_expected_move_bps,
            v4_allow_mean_reverting=input.v4_allow_mean_reverting,
            v4_macro_mode=input.v4_macro_mode,
            v4_macro_hard_veto_confidence_floor=input.v4_macro_hard_veto_confidence_floor,
            v4_macro_advisory_size_mult_on_conflict=input.v4_macro_advisory_size_mult_on_conflict,
            v4_allow_no_edge_if_exp_move_bps_gte=input.v4_allow_no_edge_if_exp_move_bps_gte,
            v4_max_mark_divergence_bps=input.v4_max_mark_divergence_bps,
            fee_rate_per_side=input.fee_rate_per_side,
            regime_adaptive_enabled=input.regime_adaptive_enabled,
            regime_trend_min_prob=input.regime_trend_min_prob,
            regime_trend_size_mult=input.regime_trend_size_mult,
            regime_trend_stop_bps=input.regime_trend_stop_bps,
            regime_trend_tp_bps=input.regime_trend_tp_bps,
            regime_trend_hold_minutes=input.regime_trend_hold_minutes,
            regime_trend_min_expected_move_bps=input.regime_trend_min_expected_move_bps,
            regime_mr_entry_threshold=input.regime_mr_entry_threshold,
            regime_mr_size_mult=input.regime_mr_size_mult,
            regime_mr_stop_bps=input.regime_mr_stop_bps,
            regime_mr_tp_bps=input.regime_mr_tp_bps,
            regime_mr_hold_minutes=input.regime_mr_hold_minutes,
            regime_mr_min_fade_conviction=input.regime_mr_min_fade_conviction,
            regime_no_trade_allow=input.regime_no_trade_allow,
            regime_no_trade_size_mult=input.regime_no_trade_size_mult,
            bet_fraction=input.bet_fraction,
            stop_loss_pct=input.stop_loss_pct,
            take_profit_pct=input.take_profit_pct,
            venue=input.venue,
            strategy_version="v4",
        )

        # Build v2 strategy
        self._v2_strategy = V2Strategy(
            exchange=input.exchange,
            portfolio=input.portfolio,
            repository=input.repository,
            alerts=input.alerts,
            probability_port=input.probability_port,
            signal_port=input.signal_port,
            min_conviction=input.min_conviction,
            regime_threshold=input.regime_threshold,
            regime_timescale=input.regime_timescale,
            bet_fraction=input.bet_fraction,
            stop_loss_pct=input.stop_loss_pct,
            take_profit_pct=input.take_profit_pct,
            venue=input.venue,
            strategy_version=input.strategy_version,
        )

        # Shared window dedupe
        self._last_traded_window_close_ts: Optional[int] = None

    async def execute(self) -> OpenPositionOutput:
        """
        Dispatch entry decision to v4 or legacy path.

        Priority:
          1. v4 path if flag is on AND v4 snapshot is available and fresh
          2. legacy v2 path otherwise (original PR #10 behavior)

        The v4 path may return None without falling through to legacy —
        that means the gates actively rejected the trade with a specific
        reason. Only "v4 unavailable" (snapshot is None) triggers fallback.

        Returns:
            OpenPositionOutput with position (or None) and reason string.
        """
        # v4 path (flag-gated, fresh snapshot required)
        if self._engine_use_v4_actions and self._v4_port is not None:
            v4 = await self._v4_port.get_latest(
                asset="BTC",
                timescales=list(self._v4_timescales),
            )
            if v4 is not None:
                position = await self._execute_v4(v4)
                return OpenPositionOutput(
                    position=position,
                    reason="v4_entry" if position else "v4_rejected",
                    v4_snapshot=v4,
                )
            else:
                logger.debug("v4 snapshot unavailable — falling back to legacy v2 path")

        # Legacy v2 path (PR #10)
        position = await self._execute_legacy()
        return OpenPositionOutput(
            position=position,
            reason="v2_entry" if position else "v2_rejected",
            v4_snapshot=None,
        )

    async def _execute_v4(self, v4: V4Snapshot) -> Optional[Position]:
        """Execute v4 strategy with shared window dedupe."""
        # Share the window dedupe state
        self._v4_strategy._last_traded_window_close_ts = (
            self._last_traded_window_close_ts
        )

        position = await self._v4_strategy.evaluate(v4)

        # Update shared state
        if (
            position is not None
            and self._v4_strategy._last_traded_window_close_ts is not None
        ):
            self._last_traded_window_close_ts = (
                self._v4_strategy._last_traded_window_close_ts
            )

        return position

    async def execute_v4(self, v4: V4Snapshot) -> OpenPositionOutput:
        """
        Public method for tests to call v4 strategy directly.

        Returns:
            OpenPositionOutput with position (or None) and v4 snapshot.
        """
        position = await self._execute_v4(v4)
        return OpenPositionOutput(
            position=position,
            reason="v4_entry" if position else "v4_rejected",
            v4_snapshot=v4,
        )

    async def _execute_legacy(self) -> Optional[Position]:
        """Execute legacy v2 strategy with shared window dedupe."""
        # Share the window dedupe state
        self._v2_strategy._last_traded_window_close_ts = (
            self._last_traded_window_close_ts
        )

        # Create a dummy v4 snapshot for the interface
        from margin_engine.adapters.signal.v4_models import (
            V4Snapshot,
            MacroBias,
            Consensus,
        )

        dummy_v4 = V4Snapshot(
            asset="BTC",
            last_price=0.0,
            ts=0,
            timescales={},
            macro=MacroBias(
                status="ok",
                bias="NEUTRAL",
                direction_gate="SKIP_UP",
                confidence=0,
                size_modifier=1.0,
            ),
            consensus=Consensus(safe_to_trade=True),
            max_impact_in_window="NONE",
            minutes_to_next_high_impact=None,
        )

        position = await self._v2_strategy.evaluate(dummy_v4)

        # Update shared state
        if (
            position is not None
            and self._v2_strategy._last_traded_window_close_ts is not None
        ):
            self._last_traded_window_close_ts = (
                self._v2_strategy._last_traded_window_close_ts
            )

        return position

    async def execute_legacy(self) -> OpenPositionOutput:
        """
        Public method for tests to call legacy v2 strategy directly.

        Returns:
            OpenPositionOutput with position (or None) and reason.
        """
        position = await self._execute_legacy()
        return OpenPositionOutput(
            position=position,
            reason="v2_entry" if position else "v2_rejected",
            v4_snapshot=None,
        )
