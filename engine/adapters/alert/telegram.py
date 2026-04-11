"""Telegram alert adapter -- wraps ``alerts.telegram.TelegramAlerter``.

Implements :class:`engine.domain.ports.AlerterPort` by delegating to
the existing ``TelegramAlerter`` concrete class.  The adapter is a thin
shim -- all formatting logic stays in the original ``TelegramAlerter``.

The ``AlerterPort`` contract defines four message types:
  - ``send_system_alert`` -- plain-text system messages
  - ``send_trade_alert`` -- structured trade-decision messages
  - ``send_skip_summary`` -- consolidated skip summary at T-0
  - ``send_heartbeat_sitrep`` -- 5-minute SITREP heartbeat

Each method delegates to the closest matching method on the underlying
``TelegramAlerter``.  Where the port signature uses domain value objects
(``WindowKey``, ``TradeDecision``, etc.), the adapter will convert to the
dict/kwargs format the legacy alerter expects once VOs have fields.

Phase 2 deliverable (CA-02).  Nothing imports this adapter yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from engine.domain.ports import AlerterPort
from engine.domain.value_objects import (
    SitrepPayload,
    SkipSummary,
    TradeDecision,
    WindowKey,
)

if TYPE_CHECKING:
    from engine.alerts.telegram import TelegramAlerter

log = structlog.get_logger(__name__)


class TelegramAlertAdapter(AlerterPort):
    """Wraps :class:`TelegramAlerter` behind :class:`AlerterPort`.

    Parameters
    ----------
    alerter : TelegramAlerter
        The concrete Telegram alerter, pre-configured with bot_token,
        chat_id, and optional risk_manager/poly_client references.
    """

    def __init__(self, alerter: "TelegramAlerter") -> None:
        self._alerter = alerter
        self._log = log.bind(adapter="telegram_alert")

    # -- AlerterPort: send_system_alert -------------------------------------

    async def send_system_alert(self, message: str) -> None:
        """Send a plain-text system alert via Telegram.

        Delegates to the underlying alerter's send methods.  System
        alerts are unformatted text -- mode switches, kill switches,
        manual-trade failures.
        """
        try:
            # TODO: TECH_DEBT - wire to TelegramAlerter.send_window_report
            # or a dedicated send_system_alert method once identified.
            self._log.info("telegram.system_alert", message=message[:120])
        except Exception as exc:
            self._log.warning(
                "telegram.send_system_alert_failed",
                error=str(exc)[:80],
            )

    # -- AlerterPort: send_trade_alert --------------------------------------

    async def send_trade_alert(
        self,
        window: WindowKey,
        decision: TradeDecision,
    ) -> None:
        """Send a structured trade-decision alert via Telegram.

        Delegates to ``TelegramAlerter.send_trade_decision_detailed``
        once the domain VOs carry the necessary fields.
        """
        try:
            # TODO: TECH_DEBT - extract WindowKey/TradeDecision fields and
            # call self._alerter.send_trade_decision_detailed(...) once VOs
            # are populated.
            self._log.info("telegram.trade_alert", window=str(window))
        except Exception as exc:
            self._log.warning(
                "telegram.send_trade_alert_failed",
                error=str(exc)[:80],
            )

    # -- AlerterPort: send_skip_summary -------------------------------------

    async def send_skip_summary(
        self,
        window: WindowKey,
        summary: SkipSummary,
    ) -> None:
        """Send a consolidated skip summary for a window.

        Delegates to ``TelegramAlerter.send_window_report`` (the skip
        variant) once VOs carry the required fields.
        """
        try:
            # TODO: TECH_DEBT - extract SkipSummary fields and call
            # self._alerter.send_window_report(...) once VOs are populated.
            self._log.info("telegram.skip_summary", window=str(window))
        except Exception as exc:
            self._log.warning(
                "telegram.send_skip_summary_failed",
                error=str(exc)[:80],
            )

    # -- AlerterPort: send_heartbeat_sitrep ---------------------------------

    async def send_heartbeat_sitrep(self, sitrep: SitrepPayload) -> None:
        """Send the 5-minute SITREP heartbeat message.

        Delegates to ``TelegramAlerter.send_window_snapshot`` (the
        heartbeat variant) once VOs carry the required fields.
        """
        try:
            # TODO: TECH_DEBT - extract SitrepPayload fields and call
            # self._alerter.send_window_snapshot(...) once VOs are populated.
            self._log.info("telegram.heartbeat_sitrep")
        except Exception as exc:
            self._log.warning(
                "telegram.send_heartbeat_sitrep_failed",
                error=str(exc)[:80],
            )
