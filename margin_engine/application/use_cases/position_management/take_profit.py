"""
Take profit management for positions.

Handles take profit checks and execution logic.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from margin_engine.domain.entities.position import Position
    from margin_engine.domain.value_objects import Price


logger = logging.getLogger(__name__)


class TakeProfitManager:
    """
    Manages take profit checks for positions.
    """

    async def check_take_profit(
        self,
        position: "Position",
        mark: "Price",
    ) -> bool:
        """
        Check if position should be closed for take profit.

        Returns:
            True if take profit should be triggered
        """
        return position.should_take_profit(mark.value)
