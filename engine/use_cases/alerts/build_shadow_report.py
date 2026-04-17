"""Use case: BuildShadowReport.

Post-resolution: assembles a ``ShadowReportPayload`` showing every
strategy's hypothetical outcome for the resolved window.

Flow:
  1. Load all persisted ``StrategyDecision`` rows for the window (LIVE+GHOST)
     via ``ShadowDecisionRepository.find_by_window``.
  2. For each decision, compute ``ShadowRow`` via pure domain function
     ``compute_shadow_outcome`` using the window's actual close direction.
  3. Emit one payload grouped by timeframe (K.4 in plan).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from domain.alert_logic import compute_shadow_outcome
from domain.alert_values import (
    AlertFooter,
    AlertHeader,
    AlertTier,
    LifecyclePhase,
    ShadowReportPayload,
    ShadowRow,
)
from domain.ports import ShadowDecisionRepository
from domain.value_objects import StrategyDecision, WindowKey
from use_cases.ports import Clock


@dataclass
class BuildShadowReportInput:
    window_key: WindowKey
    timeframe: str                       # "5m" | "15m"
    window_id: str                       # e.g. "BTC-1712345678"
    actual_direction: str                # "UP" | "DOWN"
    actual_open_usd: float
    actual_close_usd: float
    default_stake_usdc: Decimal          # fallback if decision lacks stake
    event_ts_unix: int
    live_pnl_today_usdc: Optional[Decimal] = None
    ghost_pnl_today_usdc: Optional[Decimal] = None


class BuildShadowReportUseCase:
    def __init__(
        self,
        shadow_repo: ShadowDecisionRepository,
        clock: Clock,
    ) -> None:
        self._shadow_repo = shadow_repo
        self._clock = clock

    async def execute(
        self, inp: BuildShadowReportInput
    ) -> Optional[ShadowReportPayload]:
        decisions = await self._shadow_repo.find_by_window(inp.window_key)
        if not decisions:
            return None

        rows: list[ShadowRow] = []
        for d in decisions:
            # mode inferred from decision metadata; fall back to GHOST
            mode = str(d.metadata.get("mode", "GHOST")) if d.metadata else "GHOST"
            if mode not in {"LIVE", "GHOST"}:
                mode = "GHOST"

            stake = inp.default_stake_usdc
            if d.metadata and "stake_usdc" in d.metadata:
                try:
                    stake = Decimal(str(d.metadata["stake_usdc"]))
                except Exception:
                    stake = inp.default_stake_usdc

            rows.append(
                compute_shadow_outcome(
                    timeframe=inp.timeframe,
                    strategy_id=d.strategy_id,
                    mode=mode,
                    action=d.action,
                    direction=d.direction,
                    confidence=d.confidence,
                    confidence_score=d.confidence_score,
                    entry_price_cents=d.entry_cap,
                    stake_usdc=stake,
                    actual_direction=inp.actual_direction,
                    skip_reason=d.skip_reason,
                )
            )

        now_unix = int(self._clock.now())
        return ShadowReportPayload(
            header=AlertHeader(
                phase=LifecyclePhase.RESOLVE,
                title=f"SHADOW REPORT — BTC {inp.timeframe}",
                event_ts_unix=inp.event_ts_unix,
                emit_ts_unix=now_unix,
                window_id=inp.window_id,
            ),
            footer=AlertFooter(
                emit_ts_unix=now_unix,
                window_id=inp.window_id,
            ),
            tier=AlertTier.INFO,
            timeframe=inp.timeframe,
            window_id=inp.window_id,
            actual_direction=inp.actual_direction,
            actual_open_usd=inp.actual_open_usd,
            actual_close_usd=inp.actual_close_usd,
            rows=tuple(rows),
            live_pnl_today_usdc=inp.live_pnl_today_usdc,
            ghost_pnl_today_usdc=inp.ghost_pnl_today_usdc,
        )
