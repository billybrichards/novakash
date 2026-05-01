"""ManualTradePoller — B1 deliverable.

Wraps ``ExecuteManualTradeUseCase.drain_once()`` in an asyncio task that:

  1. Opens a long-lived asyncpg LISTEN connection on boot via the existing
     ``DBClient.ensure_listening()`` helper.
  2. Awaits PG NOTIFY on ``manual_trade_pending`` (set() via a callback) with
     a 5-second timeout as a fallback poll cadence — so a missed NOTIFY never
     leaves a trade stuck forever.
  3. Runs ``drain_once()`` on every wakeup (NOTIFY or timeout).
  4. Shuts down cleanly when its ``asyncio.Task`` is cancelled.

Design notes
------------
- The poller does NOT own the DB connection pool; it receives the pre-connected
  ``DBClient`` from the composition root.
- LISTEN failure on boot is non-fatal — the 5 s poll still picks up trades.
- ``drain_once()`` is always called even on NOTIFY (not just timeout) because
  a single NOTIFY may correspond to multiple pending rows.
- Structlog is used for all logging (consistent with the rest of the engine).
"""

from __future__ import annotations

import asyncio
from typing import Optional, TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from persistence.db_client import DBClient
    from use_cases.execute_manual_trade import ExecuteManualTradeUseCase

log = structlog.get_logger(__name__)

# Channel name — must match the NOTIFY emitted by the hub (hub/api/desk_manual.py)
# and the constant in persistence/db_client.py::MANUAL_TRADE_NOTIFY_CHANNEL.
_CHANNEL = "manual_trade_pending"

# Fall-through poll interval in seconds. Trades will be picked up within this
# many seconds even if all NOTIFY deliveries are lost (connection drop, etc.).
_FALLBACK_POLL_S: float = 5.0


class ManualTradePoller:
    """Background task: LISTEN/NOTIFY + 5 s fallback poll for manual trades.

    Instantiated by the composition root and started inside the orchestrator's
    ``start()`` as an ``asyncio.create_task``.

    Parameters
    ----------
    db:
        Pre-connected :class:`~persistence.db_client.DBClient`.
    use_case:
        Fully-wired :class:`~use_cases.execute_manual_trade.ExecuteManualTradeUseCase`.
    """

    def __init__(
        self,
        db: "DBClient",
        use_case: "ExecuteManualTradeUseCase",
    ) -> None:
        self._db = db
        self._use_case = use_case
        # The asyncpg NOTIFY callback sets this event to wake drain_once
        # immediately rather than waiting for the next poll tick.
        self._notify_event: asyncio.Event = asyncio.Event()
        self._log = log.bind(task="manual_trade_poller")

    # ── asyncpg NOTIFY callback (runs on the LISTEN connection's read loop) ──

    def _on_notify(
        self,
        conn,  # asyncpg.Connection
        pid: int,
        channel: str,
        payload: str,
    ) -> None:
        """Wake the poller when the hub emits pg_notify on the channel.

        Must be non-blocking: we set an asyncio.Event and return immediately.
        The payload (trade_id) is informational — we always re-fetch all
        pending rows so a missed or duplicate NOTIFY has no ill effect.
        """
        try:
            self._notify_event.set()
            self._log.debug(
                "manual_trade_poller.notify_received",
                channel=channel,
                payload=(payload[:40] if payload else ""),
            )
        except Exception as exc:
            # Never let a logging error kill the LISTEN connection.
            self._log.warning("manual_trade_poller.notify_callback_error", error=str(exc))

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start the poller and run until cancelled.

        Called as ``asyncio.create_task(poller.run())``.  The task is
        cancelled by the orchestrator on shutdown, which raises
        ``asyncio.CancelledError`` here and exits cleanly.
        """
        self._log.info("manual_trade_poller.starting")

        # Boot-time LISTEN subscription.  Failure is non-fatal — the 5 s
        # fallback poll still works.
        await self._ensure_listening()

        self._log.info("manual_trade_poller.started", fallback_poll_s=_FALLBACK_POLL_S)

        try:
            while True:
                await self._wait_for_notify_or_timeout()
                await self._drain()
        except asyncio.CancelledError:
            self._log.info("manual_trade_poller.shutdown")
            raise

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _ensure_listening(self) -> None:
        """Re-subscribe to the NOTIFY channel, logging any error non-fatally."""
        try:
            await self._db.ensure_listening(_CHANNEL, self._on_notify)
            self._log.info("manual_trade_poller.listening", channel=_CHANNEL)
        except Exception as exc:
            self._log.warning(
                "manual_trade_poller.listen_failed",
                channel=_CHANNEL,
                error=str(exc)[:200],
            )

    async def _wait_for_notify_or_timeout(self) -> None:
        """Wait for NOTIFY or _FALLBACK_POLL_S seconds, whichever comes first.

        On return the event is cleared so the next iteration starts fresh.
        """
        try:
            await asyncio.wait_for(
                self._notify_event.wait(),
                timeout=_FALLBACK_POLL_S,
            )
        except asyncio.TimeoutError:
            # Fallback poll — no NOTIFY arrived within the poll window.
            pass
        finally:
            self._notify_event.clear()

        # Re-ensure the LISTEN connection on every iteration so a dropped
        # connection is automatically recovered on the next poll tick.
        await self._ensure_listening()

    async def _drain(self) -> None:
        """Invoke drain_once, logging outcomes and swallowing errors."""
        try:
            outcomes = await self._use_case.drain_once()
            if outcomes:
                self._log.info(
                    "manual_trade_poller.drained",
                    count=len(outcomes),
                    statuses=[o.status for o in outcomes],
                )
            else:
                self._log.debug("manual_trade_poller.drain_empty")
        except Exception as exc:
            self._log.error(
                "manual_trade_poller.drain_error",
                error=str(exc)[:300],
            )
