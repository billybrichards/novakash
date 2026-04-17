"""Use case: BuildResolvedAlert.

Single live-trade resolution. Uses ``classify_outcome`` to emit the
four-quadrant label (CORRECT+WIN, CORRECT+LOSS, WRONG+WIN, WRONG+LOSS)
so signal skill is visibly separated from luck.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from domain.alert_logic import classify_outcome
from domain.alert_values import (
    AlertFooter,
    AlertHeader,
    AlertTier,
    BtcPriceBlock,
    CumulativeTally,
    LifecyclePhase,
    ResolvedAlertPayload,
)
from domain.ports import TallyQueryPort
from use_cases.ports import Clock


@dataclass
class BuildResolvedAlertInput:
    timeframe: str
    strategy_id: str
    mode: str                               # "LIVE" | "GHOST"
    predicted_direction: str
    actual_direction: str
    pnl_usdc: Decimal
    entry_price_cents: float
    stake_usdc: Decimal
    window_id: str
    order_id: Optional[str]
    event_ts_unix: int
    actual_open_usd: float
    actual_close_usd: float
    chainlink_delta_pct: Optional[float] = None


class BuildResolvedAlertUseCase:
    def __init__(
        self,
        tallies: TallyQueryPort,
        clock: Clock,
        session_start_unix: Optional[int] = None,
    ) -> None:
        self._tallies = tallies
        self._clock = clock
        self._session_start = session_start_unix

    async def execute(
        self, inp: BuildResolvedAlertInput
    ) -> ResolvedAlertPayload:
        quadrant = classify_outcome(
            predicted=inp.predicted_direction,
            actual=inp.actual_direction,
            pnl_usdc=inp.pnl_usdc,
        )

        today = await self._safe(self._tallies.today)
        session = None
        if self._session_start is not None:
            session = await self._safe(
                lambda: self._tallies.session(self._session_start)
            )

        now_unix = int(self._clock.now())
        btc = BtcPriceBlock(
            now_price_usd=inp.actual_close_usd,
            window_open_usd=inp.actual_open_usd,
            chainlink_delta_pct=inp.chainlink_delta_pct,
            close_price_usd=inp.actual_close_usd,
        )
        return ResolvedAlertPayload(
            header=AlertHeader(
                phase=LifecyclePhase.RESOLVE,
                title=f"RESOLVED — BTC {inp.timeframe} | {inp.strategy_id}",
                event_ts_unix=inp.event_ts_unix,
                emit_ts_unix=now_unix,
                window_id=inp.window_id,
                order_id=inp.order_id,
            ),
            footer=AlertFooter(
                emit_ts_unix=now_unix,
                window_id=inp.window_id,
                order_id=inp.order_id,
            ),
            tier=AlertTier.TACTICAL,
            timeframe=inp.timeframe,
            strategy_id=inp.strategy_id,
            mode=inp.mode,
            predicted_direction=inp.predicted_direction,
            actual_direction=inp.actual_direction,
            outcome_quadrant=quadrant,
            pnl_usdc=inp.pnl_usdc,
            entry_price_cents=inp.entry_price_cents,
            stake_usdc=inp.stake_usdc,
            btc=btc,
            today_tally=today,
            session_tally=session,
        )

    @staticmethod
    async def _safe(fn) -> Optional[CumulativeTally]:
        try:
            return await fn()
        except Exception:
            return None
