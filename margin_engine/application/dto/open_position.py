"""
DTOs for OpenPosition use case.
"""

from dataclasses import dataclass
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
from margin_engine.adapters.signal.v4_models import V4Snapshot


@dataclass
class OpenPositionInput:
    """
    Input configuration for opening a position.

    All constructor parameters of OpenPositionUseCase that are configuration
    (not dependencies) are captured here.
    """

    # Dependencies
    exchange: ExchangePort
    portfolio: Portfolio
    repository: PositionRepository
    alerts: AlertPort
    probability_port: ProbabilityPort
    signal_port: SignalPort

    # v4 integration
    v4_snapshot_port: Optional[V4SnapshotPort] = None
    engine_use_v4_actions: bool = False

    # v4 strategy config
    v4_primary_timescale: str = "15m"
    v4_timescales: tuple[str, ...] = ("5m", "15m", "1h", "4h")
    v4_entry_edge: float = 0.10
    v4_min_expected_move_bps: float = 15.0
    v4_allow_mean_reverting: bool = False
    v4_macro_mode: str = "advisory"
    v4_macro_hard_veto_confidence_floor: int = 80
    v4_macro_advisory_size_mult_on_conflict: float = 0.75
    v4_allow_no_edge_if_exp_move_bps_gte: Optional[float] = None
    v4_max_mark_divergence_bps: float = 0.0
    fee_rate_per_side: float = 0.00045

    # regime adaptive
    regime_adaptive_enabled: bool = False
    regime_trend_min_prob: float = 0.55
    regime_trend_size_mult: float = 1.2
    regime_trend_stop_bps: int = 150
    regime_trend_tp_bps: int = 200
    regime_trend_hold_minutes: int = 60
    regime_trend_min_expected_move_bps: float = 30.0
    regime_mr_entry_threshold: float = 0.70
    regime_mr_size_mult: float = 0.8
    regime_mr_stop_bps: int = 80
    regime_mr_tp_bps: int = 50
    regime_mr_hold_minutes: int = 15
    regime_mr_min_fade_conviction: float = 0.55
    regime_no_trade_allow: bool = False
    regime_no_trade_size_mult: float = 0.1

    # v2 strategy config
    min_conviction: float = 0.20
    regime_threshold: float = 0.50
    regime_timescale: str = "1h"
    bet_fraction: float = 0.02
    stop_loss_pct: float = 0.006
    take_profit_pct: float = 0.005
    venue: str = "binance"
    strategy_version: str = "v2-probability"


@dataclass
class OpenPositionOutput:
    """
    Output from OpenPositionUseCase.execute().

    Wraps the return value with additional context for post-trade analysis.
    """

    position: Optional[Position]
    reason: str = ""
    v4_snapshot: Optional[V4Snapshot] = None
