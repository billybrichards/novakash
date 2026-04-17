"""Alert-builder use cases — Phase C of TG narrative refactor.

Each builder assembles a frozen domain payload from inputs + port-queried
context (tallies, shadow decisions, on-chain txs). Payload then flows
through ``PublishAlertUseCase`` → ``AlertRendererPort`` → ``AlerterPort``.

See plans/serialized-drifting-clover.md.
"""
from use_cases.alerts.build_reconcile_alert import BuildReconcileAlertUseCase
from use_cases.alerts.build_resolved_alert import BuildResolvedAlertUseCase
from use_cases.alerts.build_shadow_report import BuildShadowReportUseCase
from use_cases.alerts.build_trade_alert import BuildTradeAlertUseCase
from use_cases.alerts.build_wallet_delta_alert import BuildWalletDeltaAlertUseCase
from use_cases.alerts.build_window_signal_alert import (
    BuildWindowSignalAlertUseCase,
)
from use_cases.alerts.publish_alert import PublishAlertUseCase

__all__ = [
    "BuildReconcileAlertUseCase",
    "BuildResolvedAlertUseCase",
    "BuildShadowReportUseCase",
    "BuildTradeAlertUseCase",
    "BuildWalletDeltaAlertUseCase",
    "BuildWindowSignalAlertUseCase",
    "PublishAlertUseCase",
]
