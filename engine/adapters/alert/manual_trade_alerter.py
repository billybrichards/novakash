"""Manual-trade alerter — routes alerts to an optional separate Telegram chat.

B3 deliverable.

If ``MANUAL_TRADE_TELEGRAM_CHAT_ID`` is set in the environment, manual-trade
alerts are sent to that chat rather than the main ``TELEGRAM_CHAT_ID``.  When
the env var is absent (or empty), alerts fall through to the primary
``TelegramAlerter.send_raw_message`` which uses the default chat_id.

This is a thin delegation wrapper — no business logic lives here.

Injection pattern
-----------------
``ExecuteManualTradeUseCase`` receives an ``AlerterPort`` via its constructor.
When wired in ``CompositionRoot``, we pass a ``ManualTradeAlerter`` whose
``_override_chat_id`` is set if the env var is populated.  The existing
engine trades continue to use the primary ``TelegramAlertAdapter`` / raw
``TelegramAlerter`` unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import structlog

from domain.value_objects import SitrepPayload, SkipSummary, TradeDecision, WindowKey
from use_cases.ports.alerter import AlerterPort

if TYPE_CHECKING:
    from alerts.telegram import TelegramAlerter

log = structlog.get_logger(__name__)


class ManualTradeAlerter(AlerterPort):
    """Wraps :class:`TelegramAlerter` with an optional per-chat override.

    Parameters
    ----------
    alerter:
        The engine's primary ``TelegramAlerter`` instance (already configured
        with bot_token and primary chat_id).
    override_chat_id:
        When non-empty, this chat_id replaces the primary one for manual-trade
        messages.  When empty or ``None``, falls through to the primary chat.
    """

    def __init__(
        self,
        alerter: "TelegramAlerter",
        override_chat_id: Optional[str] = None,
    ) -> None:
        self._alerter = alerter
        # Normalise empty string → None so downstream can use a simple truthiness check.
        self._override_chat_id: Optional[str] = override_chat_id or None
        self._log = log.bind(adapter="manual_trade_alerter", has_override=bool(self._override_chat_id))

    async def send_system_alert(self, message: str) -> None:
        """Send a manual-trade alert, routing to the override chat if configured."""
        try:
            if self._override_chat_id:
                # Send to the dedicated manual-trade chat rather than the main one.
                await self._alerter.send_raw_message_to(
                    text=message,
                    chat_id=self._override_chat_id,
                )
            else:
                # Fall through to the primary alerter path.
                await self._alerter.send_raw_message(message)
        except Exception as exc:
            self._log.warning(
                "manual_trade_alerter.send_failed",
                error=str(exc)[:200],
                message_preview=message[:120],
            )

    # ── Remaining AlerterPort surface — delegate to primary alerter ───────────
    # These methods are not used by ExecuteManualTradeUseCase but are required
    # by the port contract.

    async def send_trade_alert(
        self,
        window: WindowKey,
        decision: TradeDecision,
    ) -> None:
        self._log.debug("manual_trade_alerter.trade_alert_noop", window=str(window))

    async def send_skip_summary(
        self,
        window: WindowKey,
        summary: SkipSummary,
    ) -> None:
        self._log.debug("manual_trade_alerter.skip_summary_noop", window=str(window))

    async def send_heartbeat_sitrep(self, sitrep: SitrepPayload) -> None:
        self._log.debug("manual_trade_alerter.heartbeat_noop")
