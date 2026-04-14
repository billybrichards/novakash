"""
Trailing stop management for positions.

Handles trailing stop updates and logic.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from margin_engine.domain.value_objects import StopLevel

if TYPE_CHECKING:
    from margin_engine.domain.entities.position import Position


logger = logging.getLogger(__name__)


class TrailingStopManager:
    """
    Manages trailing stop updates for positions.
    """

    def __init__(self, default_trail_pct: float = 0.003) -> None:
        """
        Initialize trailing stop manager.

        Args:
            default_trail_pct: Default trail percentage (default 0.3%)
        """
        self._default_trail_pct = default_trail_pct

    def update_trailing_stop(
        self,
        position: "Position",
        current_price: float,
    ) -> None:
        """
        Update trailing stop if price has moved favourably.

        Args:
            position: The position to update
            current_price: Current mark price
        """
        if not position.trailing_stop or not position.trailing_stop.is_trailing:
            return

        trail_pct = position.trailing_stop.trail_pct

        if position.side == position.side.LONG:
            new_stop = current_price * (1 - trail_pct)
            if new_stop > position.trailing_stop.price:
                position.trailing_stop = StopLevel(
                    price=new_stop,
                    is_trailing=True,
                    trail_pct=trail_pct,
                )
                # Also update the main stop_loss to the trailing level
                position.stop_loss = StopLevel(price=new_stop)
        else:
            new_stop = current_price * (1 + trail_pct)
            if new_stop < position.trailing_stop.price:
                position.trailing_stop = StopLevel(
                    price=new_stop,
                    is_trailing=True,
                    trail_pct=trail_pct,
                )
                position.stop_loss = StopLevel(price=new_stop)
