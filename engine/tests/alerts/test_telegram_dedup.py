"""LRU dedup for TelegramAlerter.emit_per_trade_resolved_v2."""
from __future__ import annotations

from typing import Optional

import pytest

from adapters.alert.telegram_renderer import TelegramRenderer
from adapters.persistence.in_memory_tally_repo import InMemoryTallyRepo
from alerts.telegram import TelegramAlerter
from use_cases.alerts import PublishAlertUseCase
from use_cases.ports import AlerterPort, Clock


class _Clock(Clock):
    def now(self) -> float:
        return 1_712_400_000.0


class _CapturingAlerter(AlerterPort):
    def __init__(self):
        self.sent: list[str] = []

    async def send_system_alert(self, message: str) -> None:
        self.sent.append(message)

    async def send_trade_alert(self, window, decision):  # noqa: D401
        pass

    async def send_skip_summary(self, window, summary):  # noqa: D401
        pass

    async def send_heartbeat_sitrep(self, sitrep):  # noqa: D401
        pass


def _wire() -> tuple[TelegramAlerter, _CapturingAlerter]:
    alerter = TelegramAlerter(
        bot_token="", chat_id="", alerts_paper=True, alerts_live=True, paper_mode=False
    )
    cap = _CapturingAlerter()
    alerter.set_narrative_v2(
        enabled=True,
        publish_uc=PublishAlertUseCase(TelegramRenderer(), cap),
        clock=_Clock(),
        tallies=InMemoryTallyRepo(),
    )
    return alerter, cap


@pytest.mark.asyncio
async def test_same_trade_id_and_condition_dedup():
    alerter, cap = _wire()
    kwargs = dict(
        direction="YES",
        outcome="WIN",
        pnl=2.10,
        entry_price=0.52,
        cost=5.0,
        window_ts=1_712_345_678,
        strategy="v4_fusion",
        trade_id="trade-007",
        condition_id="0xcid007",
    )
    await alerter.emit_per_trade_resolved_v2(**kwargs)
    await alerter.emit_per_trade_resolved_v2(**kwargs)
    assert len(cap.sent) == 1, "second emit should be deduped"


@pytest.mark.asyncio
async def test_different_condition_not_deduped():
    alerter, cap = _wire()
    base = dict(
        direction="YES",
        outcome="WIN",
        pnl=2.10,
        entry_price=0.52,
        cost=5.0,
        window_ts=1_712_345_678,
        strategy="v4_fusion",
        trade_id="trade-007",
    )
    await alerter.emit_per_trade_resolved_v2(**base, condition_id="0xcidA")
    await alerter.emit_per_trade_resolved_v2(**base, condition_id="0xcidB")
    assert len(cap.sent) == 2


@pytest.mark.asyncio
async def test_window_ts_fallback_when_no_trade_id():
    """Without trade_id, key falls back to window_ts + condition_id."""
    alerter, cap = _wire()
    base = dict(
        direction="YES",
        outcome="WIN",
        pnl=2.10,
        entry_price=0.52,
        cost=5.0,
        window_ts=1_712_345_678,
        strategy="v4_fusion",
        condition_id="0xsame",
    )
    await alerter.emit_per_trade_resolved_v2(**base)
    await alerter.emit_per_trade_resolved_v2(**base)
    assert len(cap.sent) == 1


@pytest.mark.asyncio
async def test_dedup_cap_trims_oldest():
    """When the dedup cache crosses its cap, oldest entries are evicted."""
    alerter, _ = _wire()
    alerter._resolved_dedup_cap = 3  # shrink to make the test tractable
    base = dict(
        direction="YES",
        outcome="WIN",
        pnl=1.0,
        entry_price=0.5,
        cost=1.0,
        window_ts=100,
        strategy="v4_fusion",
    )
    for i in range(5):
        await alerter.emit_per_trade_resolved_v2(
            **base, trade_id=f"t-{i}", condition_id=f"c-{i}"
        )
    assert len(alerter._resolved_dedup) == 3
    # Oldest entries (t-0, t-1) are gone; newest three remain.
    remaining_trade_ids = {k[0] for k in alerter._resolved_dedup.keys()}
    assert "t-0" not in remaining_trade_ids
    assert "t-4" in remaining_trade_ids
