"""Use case: BuildWindowSignalAlert.

Emits a ``WindowSignalPayload`` for a T-XXX snapshot of a window.
Includes canonical BTC price block (unifies 5+ scattered price numbers)
plus per-strategy eligibility table pulled from the registry.

Strategies argument is a plain list so the registry (YAML loader) can
supply LIVE + GHOST + DISABLED strategies and each one's current action.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from domain.alert_logic import score_signal_health
from domain.alert_values import (
    AlertFooter,
    AlertHeader,
    AlertTier,
    BtcPriceBlock,
    HealthBadge,
    LifecyclePhase,
    StrategyEligibility,
    WindowSignalPayload,
)
from use_cases.ports import Clock


@dataclass
class BuildWindowSignalAlertInput:
    timeframe: str
    window_id: str
    event_ts_unix: int
    t_offset_secs: int
    btc_now_usd: float
    btc_window_open_usd: float
    btc_chainlink_delta_pct: Optional[float]
    btc_tiingo_delta_pct: Optional[float]
    sources_agree: Optional[bool]
    vpin: Optional[float]
    p_up: Optional[float]
    p_up_distance: Optional[float]
    strategies: list[StrategyEligibility] = field(default_factory=list)
    chainlink_feed_age_s: Optional[float] = None
    eval_band_in_optimal: bool = True
    confidence_override_active: bool = False
    dominant_confidence_label: Optional[str] = None  # representative for health


class BuildWindowSignalAlertUseCase:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    async def execute(
        self, inp: BuildWindowSignalAlertInput
    ) -> WindowSignalPayload:
        btc = BtcPriceBlock(
            now_price_usd=inp.btc_now_usd,
            window_open_usd=inp.btc_window_open_usd,
            chainlink_delta_pct=inp.btc_chainlink_delta_pct,
            tiingo_delta_pct=inp.btc_tiingo_delta_pct,
            sources_agree=inp.sources_agree,
            t_offset_secs=inp.t_offset_secs,
        )
        health = score_signal_health(
            vpin=inp.vpin,
            p_up=inp.p_up,
            p_up_distance=inp.p_up_distance,
            sources_agree=inp.sources_agree,
            confidence_label=inp.dominant_confidence_label,
            confidence_override_active=inp.confidence_override_active,
            eval_band_in_optimal=inp.eval_band_in_optimal,
            chainlink_feed_age_s=inp.chainlink_feed_age_s,
        )
        now_unix = int(self._clock.now())
        return WindowSignalPayload(
            header=AlertHeader(
                phase=LifecyclePhase.STATE,
                title=f"BTC {inp.timeframe}",
                event_ts_unix=inp.event_ts_unix,
                emit_ts_unix=now_unix,
                window_id=inp.window_id,
                t_offset_secs=inp.t_offset_secs,
            ),
            footer=AlertFooter(
                emit_ts_unix=now_unix,
                window_id=inp.window_id,
            ),
            tier=AlertTier.HEARTBEAT,
            timeframe=inp.timeframe,
            btc=btc,
            vpin=inp.vpin,
            p_up=inp.p_up,
            p_up_distance=inp.p_up_distance,
            sources_agree=inp.sources_agree,
            health=health,
            strategies=tuple(inp.strategies),
        )
