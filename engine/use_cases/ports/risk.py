"""Application port: RiskManagerPort.

Belongs in the use-case layer — not the domain layer.
Moved from domain/ports.py (V7 clean-architecture fix).
"""
from __future__ import annotations

import abc

from domain.value_objects import RiskStatus


class RiskManagerPort(abc.ABC):
    """Read-only view of the risk manager's state.

    The concrete adapter wraps ``execution.risk_manager.RiskManager`` and
    exposes a frozen RiskStatus value object.  Write operations
    (record_outcome, sync_bankroll) live on the adapter, not the port,
    because they are infrastructure-level side effects triggered by the
    orchestrator -- not by use-case logic.
    """

    @abc.abstractmethod
    def get_status(self) -> RiskStatus:
        """Return a frozen snapshot of the current risk state."""
        ...
