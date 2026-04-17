"""Telegram sender — implements ``AlerterPort.send_system_alert`` via HTTP.

Thin wrapper over the existing ``alerts.telegram.TelegramAlerter`` — the
``PublishAlertUseCase`` renders the payload to a markdown string and this
adapter ships it. Other AlerterPort methods delegate to existing stubs
for backward-compat during migration.

Phase D of the TG narrative refactor.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from domain.value_objects import (
    SitrepPayload,
    SkipSummary,
    TradeDecision,
    WindowKey,
)
from use_cases.ports.alerter import AlerterPort

if TYPE_CHECKING:
    from alerts.telegram import TelegramAlerter

log = structlog.get_logger(__name__)


class TelegramSender(AlerterPort):
    """Live TG sender — wires `send_system_alert` to actual HTTP dispatch.

    Requires an existing :class:`TelegramAlerter` (pre-configured with
    bot_token + chat_id). We call its ``send_raw_message`` method, which
    is the only existing path that accepts pre-rendered markdown.
    """

    def __init__(self, alerter: "TelegramAlerter") -> None:
        self._alerter = alerter
        self._log = log.bind(adapter="telegram_sender")

    async def send_system_alert(self, message: str) -> None:
        try:
            await self._alerter.send_raw_message(message)
        except Exception as exc:
            self._log.warning(
                "telegram_sender.send_failed",
                error=str(exc)[:200],
                message_preview=message[:120],
            )

    # Legacy port surface — unused by the new pipeline but kept for
    # backward-compat. Logs only until migration completes.

    async def send_trade_alert(
        self,
        window: WindowKey,
        decision: TradeDecision,
    ) -> None:
        self._log.debug("legacy.send_trade_alert", window=str(window))

    async def send_skip_summary(
        self,
        window: WindowKey,
        summary: SkipSummary,
    ) -> None:
        self._log.debug("legacy.send_skip_summary", window=str(window))

    async def send_heartbeat_sitrep(self, sitrep: SitrepPayload) -> None:
        self._log.debug("legacy.send_heartbeat_sitrep")
