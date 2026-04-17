"""Use case: PublishAlert.

Single dispatch path for every alert payload:
  payload → AlertRendererPort.render() → AlerterPort.send()

Decouples build logic (domain-heavy, per-event) from transport (HTTP,
retries, rate limits). Lets us swap renderers per channel without touching
builders, and swap transports without touching renderers.
"""
from __future__ import annotations

from typing import Optional

import structlog

from use_cases.ports import AlerterPort, AlertRendererPort

logger = structlog.get_logger(__name__)


class PublishAlertUseCase:
    """Render + dispatch one alert payload through the wired channels."""

    def __init__(
        self,
        renderer: AlertRendererPort,
        alerter: AlerterPort,
    ) -> None:
        self._renderer = renderer
        self._alerter = alerter

    async def execute(self, payload: object) -> Optional[int]:
        """Render + send. Returns channel msg_id if available else None.

        Swallows render/send errors so the caller's main flow is never
        blocked by an alert path failure. Errors logged at WARNING.
        """
        try:
            text = self._renderer.render(payload)
        except Exception as exc:
            logger.warning(
                "publish_alert.render_failed",
                payload_type=type(payload).__name__,
                error=str(exc)[:200],
            )
            return None

        try:
            # Send as a system-level alert (plain text — renderer handled format).
            await self._alerter.send_system_alert(text)
        except Exception as exc:
            logger.warning(
                "publish_alert.send_failed",
                payload_type=type(payload).__name__,
                error=str(exc)[:200],
            )
            return None

        return None  # msg_id capture added when AlerterPort surfaces it
