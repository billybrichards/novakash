"""Per-strategy trade-attempt card — outcome enum coverage."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from alerts.telegram import TelegramAlerter


def _alerter() -> TelegramAlerter:
    # Non-empty creds so the method doesn't early-return; _send is patched.
    return TelegramAlerter(
        bot_token="t", chat_id="c", alerts_paper=True, alerts_live=True,
        paper_mode=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,expected_emoji,expected_fragment",
    [
        ("FILLED", "✅", "filled"),
        ("SKIPPED_NO_EDGE", "⏸️", "no edge"),
        ("SKIPPED_PRICE_BAND", "🚫", "price outside band"),
        ("SKIPPED_RISK_GATED", "🛑", "risk gate"),
        ("SKIPPED_COOLDOWN", "🧊", "cooldown"),
        ("SKIPPED_CONSENSUS", "⚖️", "consensus"),
        ("FAILED_EXECUTION", "❌", "execution failed"),
    ],
)
async def test_each_outcome_renders_expected_emoji_and_reason(
    outcome, expected_emoji, expected_fragment
):
    alerter = _alerter()
    sent: list[str] = []

    async def _fake_send(text: str) -> None:
        sent.append(text)

    with patch.object(alerter, "_send", side_effect=_fake_send):
        await alerter.send_trade_attempt_result(
            strategy="v4_fusion",
            window_ts=1_712_345_678,
            side="UP",
            outcome=outcome,
            stake_usd=5.0 if outcome == "FILLED" else None,
            price=0.55 if outcome == "FILLED" else None,
        )

    assert len(sent) == 1
    msg = sent[0]
    assert expected_emoji in msg
    assert "v4_fusion" in msg
    assert expected_fragment in msg
    assert outcome in msg


@pytest.mark.asyncio
async def test_filled_card_includes_stake_and_price():
    alerter = _alerter()
    sent: list[str] = []
    with patch.object(alerter, "_send", side_effect=lambda t: sent.append(t)):
        await alerter.send_trade_attempt_result(
            strategy="v15m_gate",
            window_ts=1_712_345_678,
            side="DOWN",
            outcome="FILLED",
            stake_usd=7.50,
            price=0.62,
            order_id="0xfeedbeef",
            timeframe="15m",
        )
    msg = sent[0]
    assert "$7.50" in msg
    assert "$0.620" in msg
    assert "0xfeedbeef" in msg
    assert "15m" in msg


@pytest.mark.asyncio
async def test_skipped_card_includes_gate_reason():
    alerter = _alerter()
    sent: list[str] = []
    with patch.object(alerter, "_send", side_effect=lambda t: sent.append(t)):
        await alerter.send_trade_attempt_result(
            strategy="v4_fusion",
            window_ts=1_712_345_678,
            side="UP",
            outcome="SKIPPED_CONSENSUS",
            blocking_gate="source_agreement",
            gate_reason="only 2 of 3 sources agree (chainlink missing)",
        )
    msg = sent[0]
    assert "source_agreement" in msg
    assert "chainlink missing" in msg


@pytest.mark.asyncio
async def test_unknown_outcome_falls_back_but_still_sends():
    alerter = _alerter()
    sent: list[str] = []
    with patch.object(alerter, "_send", side_effect=lambda t: sent.append(t)):
        await alerter.send_trade_attempt_result(
            strategy="v4_fusion",
            window_ts=1_712_345_678,
            side="UP",
            outcome="WEIRD_UNEXPECTED",
        )
    # Fallback emoji, but the raw outcome is in the message so it's visible.
    assert len(sent) == 1
    assert "WEIRD_UNEXPECTED" in sent[0]


@pytest.mark.asyncio
async def test_no_creds_silent():
    alerter = TelegramAlerter(bot_token="", chat_id="")
    with patch.object(alerter, "_send", new=AsyncMock()) as mock_send:
        await alerter.send_trade_attempt_result(
            strategy="v4_fusion",
            window_ts=1,
            side="UP",
            outcome="FILLED",
        )
        mock_send.assert_not_called()
