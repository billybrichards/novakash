"""
Stop loss management for positions.

Handles stop loss checks and execution logic.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from margin_engine.domain.entities.position import Position
    from margin_engine.domain.ports import ExchangePort, AlertPort
    from margin_engine.domain.value_objects import Price, ExitReason


logger = logging.getLogger(__name__)


class StopLossManager:
    """
    Manages stop loss checks and execution for positions.
    """

    async def check_stop_loss(
        self,
        position: "Position",
        mark: "Price",
    ) -> bool:
        """
        Check if position should be stopped out.

        Returns:
            True if stop loss should be triggered
        """
        return position.should_stop_loss(mark.value)
