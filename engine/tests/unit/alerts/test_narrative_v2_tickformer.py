"""Tests for tickformer_v* family Telegram trade-alert rendering.

When a tickformer_v16_pure / v17_sniper / v18_t180 / v20_adaptive_early
strategy fires action=TRADE in LIVE mode, the narrative-v2 dual-fire path
must surface the tickformer probability column, the threshold it cleared,
and (when present) the active tier — so the operator can tell tickformer
trades apart from LGB ones in Telegram (mirrors the v5_ensemble extras).

PR: feat/tickformer-telegram-notifs.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

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
    return TelegramAlerter(
        bot_token="",
        chat_id="",
        alerts_paper=True,
        alerts_live=True,
        paper_mode=False,
    )


def _make_alerter_with_v2():
    alerter = _tg_alerter_no_network()
    capturing = _CapturingAlerter()
    publish = PublishAlertUseCase(TelegramRenderer(), capturing)
    tallies = InMemoryTallyRepo()
    tallies.preload(
        today=CumulativeTally(wins=5, losses=2, pnl_usdc=Decimal("12"))
    )
    alerter.set_narrative_v2(
        enabled=True,
        publish_uc=publish,
        clock=_StaticClock(),
        tallies=tallies,
    )
    return alerter, capturing


@pytest.mark.asyncio
async def test_tickformer_v17_live_trade_renders_probability_and_threshold():
    alerter, capturing = _make_alerter_with_v2()
    await alerter.send_strategy_trade_alert(
        strategy_id="tickformer_v17_sniper",
        strategy_version="1.0.0",
        direction="UP",
        confidence="HIGH",
        confidence_score=0.62,
        gate_results=[{"gate": "tickformer_threshold", "passed": True}],
        stake_usd=0.85,
        fill_price=0.62,
        fill_size=1.37,
        order_type="FAK",
        order_id="0xdeadbeef0001",
        execution_mode="fak_filled",
        timeframe="5m",
        btc_price=75_100.50,
        eval_offset=125,
        paper_mode=False,
        decision_metadata={
            "probability_tickformer_v17": 0.812,
            "up_threshold": 0.65,
            "down_threshold": 0.35,
            "eval_offset_remaining": 175,
            "tier": "t60_180",
            "tickformer_trade_signal": "UP",
            "asset": "BTC",
            "direction": "UP",
        },
    )
    await asyncio.sleep(0)
    assert len(capturing.sent) == 1
    msg = capturing.sent[0]
    assert "tickformer_v17_sniper" in msg
    assert "tickformer:" in msg
    assert "probability_tickformer_v17=0.812" in msg
    assert "up_thr=0.650" in msg
    assert "rem=175s" in msg
    assert "tier=t60_180" in msg
    # mode is LIVE because paper_mode=False
    assert "mode=LIVE" in msg


@pytest.mark.asyncio
async def test_tickformer_v16_pure_down_trade_renders_down_threshold():
    alerter, capturing = _make_alerter_with_v2()
    await alerter.send_strategy_trade_alert(
        strategy_id="tickformer_v16_pure",
        strategy_version="1.0.0",
        direction="DOWN",
        confidence="HIGH",
        confidence_score=0.62,
        gate_results=[],
        stake_usd=0.85,
        fill_price=0.41,
        fill_size=2.07,
        order_type="FAK",
        order_id="0xdeadbeef0002",
        execution_mode="fak_filled",
        timeframe="5m",
        btc_price=75_000.0,
        eval_offset=80,
        paper_mode=False,
        decision_metadata={
            "probability_tickformer_v16": 0.190,
            "up_threshold": 0.65,
            "down_threshold": 0.35,
            "eval_offset_remaining": 220,
            "asset": "BTC",
            "direction": "DOWN",
        },
    )
    await asyncio.sleep(0)
    assert len(capturing.sent) == 1
    msg = capturing.sent[0]
    assert "tickformer_v16_pure" in msg
    assert "probability_tickformer_v16=0.190" in msg
    assert "down_thr=0.350" in msg
    # No tier override on v16_pure → tier suffix must NOT appear
    assert "tier=" not in msg


@pytest.mark.asyncio
async def test_tickformer_v18_t180_trade_renders():
    alerter, capturing = _make_alerter_with_v2()
    await alerter.send_strategy_trade_alert(
        strategy_id="tickformer_v18_t180",
        strategy_version="1.0.0",
        direction="UP",
        confidence="HIGH",
        confidence_score=0.80,
        gate_results=[],
        stake_usd=0.85,
        fill_price=0.55,
        fill_size=1.54,
        order_type="FAK",
        order_id="0xdeadbeef0003",
        execution_mode="fak_filled",
        timeframe="5m",
        btc_price=75_200.0,
        eval_offset=145,
        paper_mode=False,
        decision_metadata={
            "probability_tickformer_v18": 0.90,
            "up_threshold": 0.80,
            "down_threshold": 0.20,
            "eval_offset_remaining": 155,
            "asset": "BTC",
            "direction": "UP",
        },
    )
    await asyncio.sleep(0)
    assert len(capturing.sent) == 1
    msg = capturing.sent[0]
    assert "tickformer_v18_t180" in msg
    assert "probability_tickformer_v18=0.900" in msg
    assert "up_thr=0.800" in msg


@pytest.mark.asyncio
async def test_tickformer_v20_adaptive_early_trade_renders():
    alerter, capturing = _make_alerter_with_v2()
    await alerter.send_strategy_trade_alert(
        strategy_id="tickformer_v20_adaptive_early",
        strategy_version="1.0.0",
        direction="UP",
        confidence="MODERATE",
        confidence_score=0.40,
        gate_results=[],
        stake_usd=0.85,
        fill_price=0.58,
        fill_size=1.46,
        order_type="FAK",
        order_id="0xdeadbeef0004",
        execution_mode="fak_filled",
        timeframe="5m",
        btc_price=75_050.0,
        eval_offset=240,
        paper_mode=False,
        decision_metadata={
            "probability_tickformer_v20": 0.70,
            "up_threshold": 0.65,
            "down_threshold": 0.35,
            "eval_offset_remaining": 60,
            "asset": "BTC",
            "direction": "UP",
        },
    )
    await asyncio.sleep(0)
    assert len(capturing.sent) == 1
    msg = capturing.sent[0]
    assert "tickformer_v20_adaptive_early" in msg
    assert "probability_tickformer_v20=0.700" in msg


@pytest.mark.asyncio
async def test_non_tickformer_strategy_does_not_get_tickformer_line():
    """Regression guard: v4_fusion must NOT pick up tickformer extras even
    if its decision_metadata happens to carry a probability_tickformer_*
    field as ambient context.
    """
    alerter, capturing = _make_alerter_with_v2()
    await alerter.send_strategy_trade_alert(
        strategy_id="v4_fusion",
        strategy_version="4.3.0",
        direction="UP",
        confidence="HIGH",
        confidence_score=0.88,
        gate_results=[],
        stake_usd=5.0,
        fill_price=0.55,
        fill_size=9.09,
        order_type="FAK",
        order_id="0xv4f00000001",
        execution_mode="fak_filled",
        timeframe="5m",
        btc_price=75_000.0,
        eval_offset=120,
        paper_mode=False,
        decision_metadata={
            "probability_tickformer_v17": 0.812,  # ambient — must be ignored
            "up_threshold": 0.65,
        },
    )
    await asyncio.sleep(0)
    assert len(capturing.sent) == 1
    msg = capturing.sent[0]
    # Strategy-id gate prevents tickformer rendering for non-tickformer ids.
    assert "🎛 tickformer:" not in msg


@pytest.mark.asyncio
async def test_tickformer_without_metadata_renders_without_extras_line():
    """If somehow a tickformer trade fires without metadata (defensive),
    the legacy card still renders cleanly without the tickformer line.
    """
    alerter, capturing = _make_alerter_with_v2()
    await alerter.send_strategy_trade_alert(
        strategy_id="tickformer_v17_sniper",
        strategy_version="1.0.0",
        direction="UP",
        confidence="HIGH",
        confidence_score=0.62,
        gate_results=[],
        stake_usd=0.85,
        fill_price=0.62,
        fill_size=1.37,
        order_type="FAK",
        order_id="0xdeadbeef0005",
        execution_mode="fak_filled",
        timeframe="5m",
        btc_price=75_100.50,
        eval_offset=125,
        paper_mode=False,
        decision_metadata=None,
    )
    await asyncio.sleep(0)
    assert len(capturing.sent) == 1
    msg = capturing.sent[0]
    assert "tickformer_v17_sniper" in msg
    assert "🎛 tickformer:" not in msg
