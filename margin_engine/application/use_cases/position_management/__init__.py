"""
Position management strategies.

This module contains specialized managers for different aspects of
position lifecycle management:

- StopLossManager: Stop loss checks
- TakeProfitManager: Take profit checks
- TrailingStopManager: Trailing stop updates
- PositionExpiryManager: Expiry and continuation logic
"""

from margin_engine.application.use_cases.position_management.stop_loss import (
    StopLossManager,
)
from margin_engine.application.use_cases.position_management.take_profit import (
    TakeProfitManager,
)
from margin_engine.application.use_cases.position_management.trailing import (
    TrailingStopManager,
)
from margin_engine.application.use_cases.position_management.expiry import (
    PositionExpiryManager,
)

__all__ = [
    "StopLossManager",
    "TakeProfitManager",
    "TrailingStopManager",
    "PositionExpiryManager",
]
