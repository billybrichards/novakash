"""Application port: AlertRendererPort.

Renders a domain AlertPayload into a channel-specific string.
Separates rendering (markdown, length caps) from dispatch (HTTP send,
retries). Same payload → different rendering per channel (TG markdown,
Slack blocks, plain text).

Belongs in the use-case layer — adapter-facing, not a domain repo.
Phase B of the TG narrative refactor (plans/serialized-drifting-clover.md).
"""
from __future__ import annotations

import abc


class AlertRendererPort(abc.ABC):
    """Render a domain AlertPayload to a channel-ready string.

    Implementations:
      - TelegramRenderer: markdown + ━ dividers + TG emojis
      - (future) SlackRenderer, DiscordRenderer, PlainTextRenderer

    The input type is intentionally ``object`` so each renderer can dispatch
    on the concrete payload class (isinstance/match). Domain doesn't know
    the concrete payload list; the renderer does.
    """

    @abc.abstractmethod
    def render(self, payload: object) -> str:
        """Return the channel-ready string for ``payload``.

        ``payload`` is one of the frozen dataclasses defined in
        ``engine/domain/alert_values.py``.

        MUST be synchronous and side-effect free — pure formatting.
        MUST raise ``TypeError`` for unknown payload types; renderers
        should not silently swallow unknown dataclasses.
        """
        ...
