"""Application port: AlerterPort.

Belongs in the use-case layer — not the domain layer.
Moved from domain/ports.py (V7 clean-architecture fix).
"""
from __future__ import annotations

import abc

from domain.value_objects import SitrepPayload, SkipSummary, TradeDecision, WindowKey


class AlerterPort(abc.ABC):
    """Telegram and any future alert channels.

    Wraps today's ``alerts.telegram.TelegramAlerter``.  The concrete
    adapter delegates to the existing class so Phase 2 is purely
    structural.
    """

    @abc.abstractmethod
    async def send_system_alert(self, message: str) -> None:
        """System-level alert (mode switch, kill switch, manual-trade
        failure).  No formatting -- plain text.
        """
        ...

    @abc.abstractmethod
    async def send_trade_alert(
        self,
        window: WindowKey,
        decision: TradeDecision,
    ) -> None:
        """Structured trade-decision alert with Markdown formatting."""
        ...

    @abc.abstractmethod
    async def send_skip_summary(
        self,
        window: WindowKey,
        summary: SkipSummary,
    ) -> None:
        """Consolidated all-offsets-skipped summary at T-0."""
        ...

    @abc.abstractmethod
    async def send_heartbeat_sitrep(self, sitrep: SitrepPayload) -> None:
        """5-minute SITREP message published by ``PublishHeartbeatUseCase``."""
        ...
