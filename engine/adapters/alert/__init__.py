"""Alert adapters -- implementations of ``engine.domain.ports.AlerterPort``."""

from engine.adapters.alert.telegram import TelegramAlertAdapter

__all__ = ["TelegramAlertAdapter"]
