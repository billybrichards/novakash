"""Use case: BuildTradeAlert.

Assembles a ``TradeAlertPayload`` from a strategy decision + execution
result + gate results + cached tallies + computed health.

Key responsibility: relabel confidence on risk-off override (fixes the
conf=NONE display bug the user reported in screenshots).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional

from domain.alert_logic import (
    relabel_confidence_on_override,
    score_signal_health,
)
from domain.alert_values import (
    AlertFooter,
    AlertHeader,
    AlertTier,
    BtcPriceBlock,
    CumulativeTally,
    HealthBadge,
    LifecyclePhase,
    TradeAlertPayload,
)
from domain.ports import TallyQueryPort
from use_cases.ports import Clock


@dataclass
class BuildTradeAlertInput:
    """Everything needed to emit a post-decision / post-fill trade alert."""

    timeframe: str
    strategy_id: str
    strategy_version: str
    mode: str                           # "LIVE" | "GHOST"
    direction: str                      # "UP" | "DOWN"
    confidence: Optional[str]           # raw label from decision
    confidence_score: Optional[float]
    gate_results: Iterable[dict]
    stake_usdc: Decimal
    fill_price_cents: Optional[float]
    fill_size_shares: Optional[float]
    cost_usdc: Optional[Decimal]
    order_submitted: bool
    order_status: str                   # "RESTING" | "FILLED" | "FAILED"
    window_id: str
    order_id: Optional[str]
    btc_now_usd: float
    btc_window_open_usd: float
    btc_chainlink_delta_pct: Optional[float]
    btc_tiingo_delta_pct: Optional[float]
    vpin: Optional[float]
    p_up: Optional[float]
    p_up_distance: Optional[float]
    sources_agree: Optional[bool]
    chainlink_feed_age_s: Optional[float]
    eval_band_in_optimal: bool
    event_ts_unix: int
    t_offset_secs: int
    wallet_usdc: Optional[Decimal] = None
    paper_mode: bool = False


class BuildTradeAlertUseCase:
    """Build a ``TradeAlertPayload`` for a trade decision + execution event."""

    def __init__(
        self,
        tallies: TallyQueryPort,
        clock: Clock,
    ) -> None:
        self._tallies = tallies
        self._clock = clock

    async def execute(self, inp: BuildTradeAlertInput) -> TradeAlertPayload:
        # Display-layer confidence fix for risk-off override.
        override_active = self._override_active(inp.gate_results)
        display_label = relabel_confidence_on_override(
            label=inp.confidence,
            score=inp.confidence_score,
            gate_results=inp.gate_results,
        )

        health = score_signal_health(
            vpin=inp.vpin,
            p_up=inp.p_up,
            p_up_distance=inp.p_up_distance,
            sources_agree=inp.sources_agree,
            confidence_label=inp.confidence,
            confidence_override_active=override_active,
            eval_band_in_optimal=inp.eval_band_in_optimal,
            chainlink_feed_age_s=inp.chainlink_feed_age_s,
        )

        today = await self._safe_tally(self._tallies.today)
        last_hour = await self._safe_tally(self._tallies.last_hour)

        btc = BtcPriceBlock(
            now_price_usd=inp.btc_now_usd,
            window_open_usd=inp.btc_window_open_usd,
            chainlink_delta_pct=inp.btc_chainlink_delta_pct,
            tiingo_delta_pct=inp.btc_tiingo_delta_pct,
            sources_agree=inp.sources_agree,
            t_offset_secs=inp.t_offset_secs,
        )

        now_unix = int(self._clock.now())

        header = AlertHeader(
            phase=LifecyclePhase.DECISION if not inp.order_submitted else LifecyclePhase.EXECUTION,
            title=f"TRADE — BTC {inp.timeframe} | {inp.strategy_id} {inp.strategy_version}",
            event_ts_unix=inp.event_ts_unix,
            emit_ts_unix=now_unix,
            window_id=inp.window_id,
            order_id=inp.order_id,
            t_offset_secs=inp.t_offset_secs,
        )
        footer = AlertFooter(
            emit_ts_unix=now_unix,
            wallet_usdc=inp.wallet_usdc,
            paper_mode=inp.paper_mode,
            window_id=inp.window_id,
            order_id=inp.order_id,
        )

        # TACTICAL for live trades; INFO for paper (audit trail only).
        tier = AlertTier.INFO if inp.paper_mode else AlertTier.TACTICAL

        return TradeAlertPayload(
            header=header,
            footer=footer,
            tier=tier,
            timeframe=inp.timeframe,
            strategy_id=inp.strategy_id,
            strategy_version=inp.strategy_version,
            mode=inp.mode,
            direction=inp.direction,
            confidence_label=display_label,
            confidence_score=inp.confidence_score or 0.0,
            gate_results=tuple(dict(g) if isinstance(g, dict) else {"name": getattr(g, "name", ""), "passed": getattr(g, "passed", False)} for g in (inp.gate_results or ())),
            stake_usdc=inp.stake_usdc,
            fill_price_cents=inp.fill_price_cents,
            fill_size_shares=inp.fill_size_shares,
            cost_usdc=inp.cost_usdc,
            order_submitted=inp.order_submitted,
            order_status=inp.order_status,
            btc=btc,
            health=health,
            today_tally=today,
            last_hour_tally=last_hour,
        )

    @staticmethod
    def _override_active(gate_results: Iterable[dict]) -> bool:
        for g in gate_results or ():
            name = g.get("name") if isinstance(g, dict) else getattr(g, "name", "")
            passed = g.get("passed") if isinstance(g, dict) else getattr(g, "passed", False)
            if name and "risk_off" in str(name).lower() and "override" in str(name).lower() and passed:
                return True
        return False

    @staticmethod
    async def _safe_tally(fn) -> Optional[CumulativeTally]:
        try:
            return await fn()
        except Exception:
            return None
