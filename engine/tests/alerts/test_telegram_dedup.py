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


# Audit #350: emit_per_trade_resolved_v2 no longer synthesizes
# ``$100,000 → $100,001`` placeholders when prices are missing — the
# caller MUST pass real ``actual_open_usd`` / ``actual_close_usd`` (or
# inject a DB client that resolves them). These tests pass real prices
# directly.
_REAL_OPEN = 80_267.93
_REAL_CLOSE_UP = 80_300.50


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
        actual_open_usd=_REAL_OPEN,
        actual_close_usd=_REAL_CLOSE_UP,
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
        actual_open_usd=_REAL_OPEN,
        actual_close_usd=_REAL_CLOSE_UP,
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
        actual_open_usd=_REAL_OPEN,
        actual_close_usd=_REAL_CLOSE_UP,
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
        actual_open_usd=_REAL_OPEN,
        actual_close_usd=_REAL_CLOSE_UP,
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


@pytest.mark.asyncio
async def test_skips_emit_when_no_prices_and_no_db(caplog):
    """Audit #350: when neither caller-provided prices nor a DB client
    exist, the v2 card MUST be skipped (with a warning) — never
    synthesize ``$100,000 → $100,001``."""
    alerter, cap = _wire()
    # No actual_open_usd / actual_close_usd; no DB client wired
    await alerter.emit_per_trade_resolved_v2(
        direction="YES",
        outcome="WIN",
        pnl=2.10,
        entry_price=0.52,
        cost=5.0,
        window_ts=1_712_345_678,
        strategy="v4_fusion",
        trade_id="trade-skip-001",
        condition_id="0xnoprice",
    )
    assert cap.sent == [], "card must NOT emit when prices unavailable"


@pytest.mark.asyncio
async def test_no_synthetic_100k_anywhere_in_emit_path(caplog):
    """The literal ``$100,000`` / ``$100,001`` MUST NOT appear in any
    rendered v2 card. Pin this so the regression cannot return."""
    alerter, cap = _wire()
    await alerter.emit_per_trade_resolved_v2(
        direction="NO",
        outcome="WIN",
        pnl=13.57,
        entry_price=0.656,
        cost=30.0,
        window_ts=1_777_933_200,  # trade 7378's window
        strategy="v12_lgb_combo",
        trade_id="trade-7378",
        condition_id="0xreal",
        actual_open_usd=80_267.93,
        actual_close_usd=80_250.10,
    )
    assert len(cap.sent) == 1
    rendered = cap.sent[0]
    assert "100,000" not in rendered
    assert "100,001" not in rendered
    assert "99,999" not in rendered
