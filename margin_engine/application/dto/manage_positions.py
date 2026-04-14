"""
DTOs for ManagePositions use case.
"""

from dataclasses import dataclass
from typing import List, Optional

from margin_engine.domain.entities.portfolio import Portfolio
from margin_engine.domain.entities.position import Position
from margin_engine.domain.ports import (
    AlertPort,
    ExchangePort,
    PositionRepository,
    ProbabilityPort,
    V4SnapshotPort,
)
from margin_engine.domain.value_objects import V4Snapshot


@dataclass
class ManagePositionsInput:
    """
    Input configuration for managing positions.

    All constructor parameters of ManagePositionsUseCase that are configuration
    (not dependencies) are captured here.
    """

    # Dependencies
    exchange: ExchangePort
    portfolio: Portfolio
    repository: PositionRepository
    alerts: AlertPort

    # Configuration
    trailing_stop_pct: float = 0.003
    v4_snapshot_port: Optional[V4SnapshotPort] = None
    probability_port: Optional[ProbabilityPort] = None
    engine_use_v4_actions: bool = False
    v4_primary_timescale: str = "15m"
    v4_timescales: tuple[str, ...] = ("5m", "15m", "1h", "4h")
    v4_continuation_min_conviction: float = 0.10
    v4_continuation_max: Optional[int] = None
    v4_event_exit_seconds: int = 120
    v4_macro_mode: str = "advisory"
    v4_macro_hard_veto_confidence_floor: int = 80


@dataclass
class ManagePositionsOutput:
    """
    Output from ManagePositionsUseCase.tick().

    Wraps the list of closed positions with execution context.
    """

    closed_positions: List[Position]
    actions_taken: List[str]
    v4_snapshot: Optional[V4Snapshot] = None
