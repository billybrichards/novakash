"""Phase G.1 test: TelegramAlerter.send_strategy_trade_alert dual-fires
through the narrative-v2 pipeline when enabled.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

import pytest

from adapters.alert.telegram_renderer import TelegramRenderer
from adapters.persistence.in_memory_tally_repo import InMemoryTallyRepo
from alerts.telegram import TelegramAlerter
from domain.alert_values import CumulativeTally
from use_cases.alerts import PublishAlertUseCase
from use_cases.ports import AlerterPort, Clock


class _StaticClock(Clock):
    def now(self) -> float:
        return 1_712_400_000.0


class _CapturingAlerter(AlerterPort):
    def __init__(self):
        self.sent: list[str] = []

    async def send_system_alert(self, message: str) -> None:
        self.sent.append(message)

    async def send_trade_alert(self, window, decision):
        pass

    async def send_skip_summary(self, window, summary):
        pass

    async def send_heartbeat_sitrep(self, sitrep):
        pass


def _tg_alerter_no_network() -> TelegramAlerter:
    # Empty token/chat → TG HTTP path is silent; legacy body still runs but
    # sends nothing. We're only testing the v2 dual-fire branch.
    return TelegramAlerter(
        bot_token="",
        chat_id="",
        alerts_paper=True,
        alerts_live=True,
        paper_mode=False,
    )


@pytest.mark.asyncio
async def test_dual_fire_disabled_by_default():
    alerter = _tg_alerter_no_network()
    capturing = _CapturingAlerter()
    renderer = TelegramRenderer()
    publish = PublishAlertUseCase(renderer, capturing)
    tallies = InMemoryTallyRepo()
    tallies.preload(today=CumulativeTally(wins=3, losses=1, pnl_usdc=Decimal("5")))
    alerter.set_narrative_v2(
        enabled=False, publish_uc=publish, clock=_StaticClock(), tallies=tallies
    )
    await alerter.send_strategy_trade_alert(
        strategy_id="v4_fusion",
        strategy_version="4.3.0",
        direction="DOWN",
        confidence="NONE",
        confidence_score=0.65,
        gate_results=[{"name": "regime_risk_off_override", "passed": True}],
        stake_usd=3.94,
        fill_price=0.49,
        fill_size=8.04,
        order_type="FAK",
        order_id="0xabcdef012345",
        execution_mode="fak_filled",
        timeframe="5m",
        btc_price=74_937.90,
        vpin=0.62,
        eval_offset=62,
        paper_mode=False,
    )
    assert capturing.sent == []


@pytest.mark.asyncio
async def test_dual_fire_renders_override_label_when_enabled():
    """Screenshot scenario: conf=NONE(0.65)+risk_off_override → OVERRIDE:risk_off."""
    alerter = _tg_alerter_no_network()
    capturing = _CapturingAlerter()
    publish = PublishAlertUseCase(TelegramRenderer(), capturing)
    tallies = InMemoryTallyRepo()
    tallies.preload(today=CumulativeTally(wins=3, losses=1, pnl_usdc=Decimal("5")))
    alerter.set_narrative_v2(
        enabled=True, publish_uc=publish, clock=_StaticClock(), tallies=tallies
    )
    await alerter.send_strategy_trade_alert(
        strategy_id="v4_fusion",
        strategy_version="4.3.0",
        direction="DOWN",
        confidence="NONE",
        confidence_score=0.65,
        gate_results=[{"name": "regime_risk_off_override", "passed": True}],
        stake_usd=3.94,
        fill_price=0.49,
        fill_size=8.04,
        order_type="FAK",
        order_id="0xabcdef012345",
        execution_mode="fak_filled",
        timeframe="5m",
        btc_price=74_937.90,
        vpin=0.62,
        eval_offset=62,
        paper_mode=False,
    )
    assert len(capturing.sent) == 1
    msg = capturing.sent[0]
    assert "conf=OVERRIDE:risk_off (0.65)" in msg
    assert "today: 3W/1L" in msg
    assert "ord=`0xabcdef01`" in msg


@pytest.mark.asyncio
async def test_dual_fire_legacy_gate_key_accepted():
    """Legacy send_strategy_trade_alert uses "gate" key; must still work."""
    alerter = _tg_alerter_no_network()
    capturing = _CapturingAlerter()
    publish = PublishAlertUseCase(TelegramRenderer(), capturing)
    alerter.set_narrative_v2(
        enabled=True, publish_uc=publish, clock=_StaticClock(), tallies=InMemoryTallyRepo()
    )
    await alerter.send_strategy_trade_alert(
        strategy_id="v4_fusion",
        strategy_version="4.3.0",
        direction="UP",
        confidence="HIGH",
        confidence_score=0.88,
        gate_results=[
            {"gate": "confidence", "passed": True},
            {"gate": "direction", "passed": True},
        ],
        stake_usd=5.0,
        fill_price=0.55,
        fill_size=9.09,
        order_type="FAK",
        order_id="0x1234567890",
        execution_mode="fak_filled",
        timeframe="5m",
        btc_price=75_000.0,
        vpin=0.60,
        paper_mode=False,
    )
    assert len(capturing.sent) == 1
    assert "conf=HIGH" in capturing.sent[0]


@pytest.mark.asyncio
async def test_dual_fire_swallows_errors_and_keeps_legacy_running():
    """If the v2 pipeline raises, legacy body must still complete."""
    class _RaisingPublish:
        async def execute(self, payload):
            raise RuntimeError("render boom")

    alerter = _tg_alerter_no_network()
    alerter.set_narrative_v2(
        enabled=True,
        publish_uc=_RaisingPublish(),
        clock=_StaticClock(),
        tallies=InMemoryTallyRepo(),
    )
    # Must not raise.
    await alerter.send_strategy_trade_alert(
        strategy_id="v4_fusion",
        strategy_version="4.3.0",
        direction="DOWN",
        confidence="HIGH",
        confidence_score=0.88,
        gate_results=[],
        stake_usd=5.0,
        order_type="FAK",
        order_id="0xabc",
        execution_mode="fak_filled",
        timeframe="5m",
        btc_price=75_000.0,
        paper_mode=False,
    )


@pytest.mark.asyncio
async def test_dual_fire_skips_when_btc_price_zero():
    """Defensive: no BTC price means no payload build — skip silently."""
    alerter = _tg_alerter_no_network()
    capturing = _CapturingAlerter()
    publish = PublishAlertUseCase(TelegramRenderer(), capturing)
    alerter.set_narrative_v2(
        enabled=True, publish_uc=publish, clock=_StaticClock(), tallies=InMemoryTallyRepo()
    )
    await alerter.send_strategy_trade_alert(
        strategy_id="v4_fusion",
        strategy_version="4.3.0",
        direction="UP",
        confidence="HIGH",
        confidence_score=0.88,
        gate_results=[],
        stake_usd=5.0,
        order_type="FAK",
        order_id="0x1",
        execution_mode="fak_filled",
        timeframe="5m",
        btc_price=0.0,
        paper_mode=False,
    )
    assert capturing.sent == []


@pytest.mark.asyncio
async def test_dual_fire_skips_unknown_direction():
    alerter = _tg_alerter_no_network()
    capturing = _CapturingAlerter()
    publish = PublishAlertUseCase(TelegramRenderer(), capturing)
    alerter.set_narrative_v2(
        enabled=True, publish_uc=publish, clock=_StaticClock(), tallies=InMemoryTallyRepo()
    )
    await alerter.send_strategy_trade_alert(
        strategy_id="v4_fusion",
        strategy_version="4.3.0",
        direction="?",
        confidence="NONE",
        confidence_score=0.0,
        gate_results=[],
        stake_usd=0.0,
        order_type="FAK",
        order_id="",
        execution_mode="fak_filled",
        timeframe="5m",
        btc_price=75_000.0,
    )
    assert capturing.sent == []
