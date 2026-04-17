"""End-to-end Phase E wiring test: builder → renderer → sender (fake)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from adapters.alert.telegram_renderer import TelegramRenderer
from adapters.onchain.in_memory_onchain import InMemoryOnChainQuery
from adapters.persistence.in_memory_shadow_decision_repo import (
    InMemoryShadowDecisionRepository,
)
from adapters.persistence.in_memory_tally_repo import InMemoryTallyRepo
from domain.alert_values import CumulativeTally, OutflowTx
from domain.value_objects import StrategyDecision, WindowKey
from use_cases.alerts import (
    BuildReconcileAlertUseCase,
    BuildResolvedAlertUseCase,
    BuildShadowReportUseCase,
    BuildTradeAlertUseCase,
    BuildWalletDeltaAlertUseCase,
    PublishAlertUseCase,
)
from use_cases.alerts.build_resolved_alert import BuildResolvedAlertInput
from use_cases.alerts.build_shadow_report import BuildShadowReportInput
from use_cases.alerts.build_trade_alert import BuildTradeAlertInput
from use_cases.alerts.build_wallet_delta_alert import (
    BuildWalletDeltaAlertInput,
)
from use_cases.ports import AlerterPort, Clock
from domain.value_objects import SitrepPayload, SkipSummary, TradeDecision


class _Clock(Clock):
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


@pytest.mark.asyncio
async def test_conf_none_override_renders_and_sends():
    """Screenshot-exact scenario: DOWN conf=NONE (0.65) + risk_off_override
    should now render as `conf=OVERRIDE:risk_off (0.65)` and be dispatched.
    """
    tallies = InMemoryTallyRepo()
    tallies.preload(today=CumulativeTally(wins=3, losses=1, pnl_usdc=Decimal("5.00")))
    builder = BuildTradeAlertUseCase(tallies, _Clock())
    renderer = TelegramRenderer()
    alerter = _CapturingAlerter()
    publish = PublishAlertUseCase(renderer, alerter)

    payload = await builder.execute(
        BuildTradeAlertInput(
            timeframe="5m",
            strategy_id="v4_fusion",
            strategy_version="4.3.0",
            mode="LIVE",
            direction="DOWN",
            confidence="NONE",
            confidence_score=0.65,
            gate_results=[
                {"name": "confidence", "passed": True},
                {"name": "regime_risk_off_override", "passed": True},
            ],
            stake_usdc=Decimal("3.94"),
            fill_price_cents=0.49,
            fill_size_shares=8.04,
            cost_usdc=Decimal("3.94"),
            order_submitted=True,
            order_status="FILLED",
            window_id="BTC-1712345678",
            order_id="0xabcdef0123",
            btc_now_usd=74_937.90,
            btc_window_open_usd=75_034.96,
            btc_chainlink_delta_pct=-0.13,
            btc_tiingo_delta_pct=-0.12,
            vpin=0.62,
            p_up=0.14,
            p_up_distance=0.36,
            sources_agree=True,
            chainlink_feed_age_s=3.0,
            eval_band_in_optimal=True,
            event_ts_unix=1_712_345_678,
            t_offset_secs=62,
        )
    )
    assert payload.confidence_label == "OVERRIDE:risk_off"

    await publish.execute(payload)
    assert len(alerter.sent) == 1
    msg = alerter.sent[0]
    assert "conf=OVERRIDE:risk_off (0.65)" in msg
    assert "today: 3W/1L" in msg
    assert "ord=`0xabcdef01`" in msg
    assert "━" in msg   # DIVIDER present


@pytest.mark.asyncio
async def test_wallet_withdrawal_scenario_sends_info_alert():
    """$267 → $80 MetaMask withdrawal scenario (screenshot evidence)."""
    tx = OutflowTx(
        tx_hash="0xwithdraw",
        to_addr="0xOwnerMeta",
        amount_usdc=Decimal("186.62"),
        block_number=50,
        timestamp_unix=1,
    )
    onchain = InMemoryOnChainQuery()
    onchain.preload([tx], latest_block=100)

    builder = BuildWalletDeltaAlertUseCase(onchain, _Clock())
    renderer = TelegramRenderer()
    alerter = _CapturingAlerter()
    publish = PublishAlertUseCase(renderer, alerter)

    payload = await builder.execute(
        BuildWalletDeltaAlertInput(
            wallet_addr="0xWallet",
            prior_balance_usdc=Decimal("267.03"),
            new_balance_usdc=Decimal("80.41"),
            since_block=0,
            owner_eoas=frozenset({"0xOwnerMeta"}),
            poly_contracts=frozenset({"0xCTF"}),
            redeemer_addr="0xRedeemer",
            event_ts_unix=1,
        )
    )
    await publish.execute(payload)
    assert len(alerter.sent) == 1
    msg = alerter.sent[0]
    assert "MANUAL WITHDRAWAL" in msg
    assert "🏦" in msg
    assert "-$186.62" in msg


@pytest.mark.asyncio
async def test_shadow_report_end_to_end():
    repo = InMemoryShadowDecisionRepository()
    wk = WindowKey(asset="BTC", window_ts=1_712_345_678)
    decisions = [
        StrategyDecision(
            action="TRADE",
            direction="DOWN",
            confidence="HIGH",
            confidence_score=0.88,
            entry_cap=0.49,
            collateral_pct=0.025,
            strategy_id="v4_fusion",
            strategy_version="4.3.0",
            entry_reason="r",
            skip_reason=None,
            metadata={"mode": "LIVE", "stake_usdc": "3.94"},
        ),
        StrategyDecision(
            action="TRADE",
            direction="UP",
            confidence="MODERATE",
            confidence_score=0.62,
            entry_cap=0.52,
            collateral_pct=0.025,
            strategy_id="v10_gate",
            strategy_version="1.0.0",
            entry_reason="r",
            skip_reason=None,
            metadata={"mode": "GHOST", "stake_usdc": "5.00"},
        ),
    ]
    await repo.save(wk, decisions)

    builder = BuildShadowReportUseCase(repo, _Clock())
    renderer = TelegramRenderer()
    alerter = _CapturingAlerter()
    publish = PublishAlertUseCase(renderer, alerter)

    payload = await builder.execute(
        BuildShadowReportInput(
            window_key=wk,
            timeframe="5m",
            window_id=wk.key,
            actual_direction="DOWN",
            actual_open_usd=75_034.96,
            actual_close_usd=74_937.90,
            default_stake_usdc=Decimal("5.00"),
            event_ts_unix=1,
        )
    )
    assert payload is not None
    await publish.execute(payload)
    msg = alerter.sent[0]
    assert "actual: DOWN" in msg
    assert "v4_fusion" in msg
    assert "v10_gate" in msg
    assert "CORRECT + WIN" in msg
    assert "WRONG + LOSS" in msg


@pytest.mark.asyncio
async def test_reconcile_dedupe_silent_on_repeat():
    """Two identical orphan passes → first emits drift, second silent."""
    builder = BuildReconcileAlertUseCase(_Clock())
    renderer = TelegramRenderer()
    alerter = _CapturingAlerter()
    publish = PublishAlertUseCase(renderer, alerter)

    from use_cases.alerts.build_reconcile_alert import BuildReconcileAlertInput

    inp = BuildReconcileAlertInput(
        matched=[],
        paper_matched=[],
        current_orphan_condition_ids=[f"0xorphan{i}" for i in range(26)],
        orphan_auto_redeemed_wins=0,
        orphan_worthless_tokens=26,
        event_ts_unix=1,
    )
    p1 = await builder.execute(inp)
    if p1 is not None:
        await publish.execute(p1)
    p2 = await builder.execute(inp)
    # Second pass: same orphans → None, nothing sent.
    assert p2 is None
    assert len(alerter.sent) == 1
    assert "ORPHAN DRIFT" in alerter.sent[0]
