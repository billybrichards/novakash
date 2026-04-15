"""Unit tests for PublishHeartbeatUseCase.

All ports are mocked.  No DB, no network, no Telegram.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock
from typing import Optional

from domain.value_objects import (
    HeartbeatRow,
    RiskStatus,
    SitrepPayload,
)
from use_cases.publish_heartbeat import PublishHeartbeatUseCase


def _risk(
    current_bankroll=100.0, peak_bankroll=110.0, drawdown_pct=5.0,
    daily_pnl=2.50, consecutive_losses=0, paper_mode=True,
    kill_switch_active=False,
):
    return RiskStatus(
        current_bankroll=current_bankroll, peak_bankroll=peak_bankroll,
        drawdown_pct=drawdown_pct, daily_pnl=daily_pnl,
        consecutive_losses=consecutive_losses, paper_mode=paper_mode,
        kill_switch_active=kill_switch_active,
    )


class FakeEngineState:
    def __init__(
        self, vpin=0.55, btc_price=65000.0, open_positions_count=2,
        paper_mode=True, starting_bankroll=100.0, cascade_state=None,
    ):
        self._vpin = vpin
        self._btc_price = btc_price
        self._open_positions_count = open_positions_count
        self._paper_mode = paper_mode
        self._starting_bankroll = starting_bankroll
        self._cascade_state = cascade_state
        self._feed_status = {
            "binance": True, "coinglass": True, "chainlink": False,
            "polymarket": True, "opinion": True,
        }

    @property
    def vpin(self): return self._vpin
    @property
    def btc_price(self): return self._btc_price
    @property
    def open_positions_count(self): return self._open_positions_count
    @property
    def paper_mode(self): return self._paper_mode
    @property
    def starting_bankroll(self): return self._starting_bankroll
    @property
    def cascade_state(self): return self._cascade_state
    @property
    def feed_status(self): return self._feed_status


class Ports:
    def __init__(self):
        self.risk_manager = MagicMock()
        self.risk_manager.get_status.return_value = _risk()
        self.system_state_repo = AsyncMock()
        self.system_state_repo.get_daily_record.return_value = (5, 3)
        self.alerts = AsyncMock()
        self.clock = MagicMock()
        self.clock.now.return_value = 1700000100.0
        self.engine_state = FakeEngineState()

    def uc(self, sitrep_interval=30):
        return PublishHeartbeatUseCase(
            risk_manager=self.risk_manager,
            system_state_repo=self.system_state_repo,
            alerts=self.alerts,
            clock=self.clock,
            engine_state=self.engine_state,
            sitrep_interval=sitrep_interval,
        )


@pytest.mark.asyncio
async def test_tick_writes_heartbeat():
    p = Ports()
    await p.uc().tick()

    p.system_state_repo.write_heartbeat.assert_called_once()
    row = p.system_state_repo.write_heartbeat.call_args.args[0]
    assert isinstance(row, HeartbeatRow)
    assert row.engine_status == "running"
    assert row.current_balance == 100.0
    assert row.peak_balance == 110.0
    assert row.last_vpin == 0.55
    assert row.active_positions == 2


@pytest.mark.asyncio
async def test_tick_does_not_touch_feed_status():
    """Feed connectivity writes are owned by the runtime (feed objects live
    there). PublishHeartbeatUseCase should NOT call update_feed_status."""
    p = Ports()
    await p.uc().tick()

    p.system_state_repo.update_feed_status.assert_not_called()


@pytest.mark.asyncio
async def test_set_wallet_balance_lands_in_config_snapshot():
    p = Ports()
    uc = p.uc()
    uc.set_wallet_balance(123.45)
    await uc.tick()

    row = p.system_state_repo.write_heartbeat.call_args.args[0]
    assert row.config_snapshot["wallet_balance_usdc"] == 123.45


@pytest.mark.asyncio
async def test_tick_tolerates_dict_risk_status():
    """Real RiskManager.get_status() returns dict, not RiskStatus dataclass.
    The use case must handle both without AttributeError."""
    p = Ports()
    # Replace the dataclass RiskStatus with the dict shape production uses
    p.risk_manager.get_status = MagicMock(return_value={
        "current_bankroll": 100.0,
        "peak_bankroll": 110.0,
        "drawdown_pct": 5.0,
        "daily_pnl": 2.5,
        "consecutive_losses": 0,
        "paper_mode": True,
        "kill_switch_active": False,
        "is_killed": False,
    })
    uc = p.uc(sitrep_interval=1)
    await uc.tick()  # Would raise AttributeError pre-fix

    p.system_state_repo.write_heartbeat.assert_called_once()
    row = p.system_state_repo.write_heartbeat.call_args.args[0]
    assert row.current_balance == 100.0
    assert row.config_snapshot["daily_pnl"] == 2.5


@pytest.mark.asyncio
async def test_tick_writes_runtime_config_to_snapshot():
    """runtime_config must be present in system_state.config for Hub/FE readers."""
    p = Ports()
    await p.uc().tick()

    row = p.system_state_repo.write_heartbeat.call_args.args[0]
    assert "runtime_config" in row.config_snapshot
    assert isinstance(row.config_snapshot["runtime_config"], dict)


@pytest.mark.asyncio
async def test_sitrep_sent_at_interval():
    p = Ports()
    uc = p.uc(sitrep_interval=3)

    await uc.tick()
    await uc.tick()
    p.alerts.send_heartbeat_sitrep.assert_not_called()

    await uc.tick()
    p.alerts.send_heartbeat_sitrep.assert_called_once()
    payload = p.alerts.send_heartbeat_sitrep.call_args.args[0]
    assert isinstance(payload, SitrepPayload)


@pytest.mark.asyncio
async def test_sitrep_not_sent_before_interval():
    p = Ports()
    uc = p.uc(sitrep_interval=30)

    for _ in range(29):
        await uc.tick()
    p.alerts.send_heartbeat_sitrep.assert_not_called()

    await uc.tick()
    p.alerts.send_heartbeat_sitrep.assert_called_once()


@pytest.mark.asyncio
async def test_sitrep_payload_contents():
    p = Ports()
    p.system_state_repo.get_daily_record.return_value = (7, 3)
    uc = p.uc(sitrep_interval=1)

    await uc.tick()

    payload = p.alerts.send_heartbeat_sitrep.call_args.args[0]
    assert payload.engine_status == "ACTIVE"
    assert payload.mode_label == "PAPER"
    assert payload.wins_today == 7
    assert payload.losses_today == 3
    assert payload.win_rate == 70.0
    assert payload.vpin == 0.55
    assert payload.btc_price == 65000.0
    assert payload.open_positions == 2
    assert payload.kill_switch_active is False


@pytest.mark.asyncio
async def test_sitrep_shows_killed_when_kill_switch_active():
    p = Ports()
    p.risk_manager.get_status.return_value = _risk(kill_switch_active=True)
    uc = p.uc(sitrep_interval=1)

    await uc.tick()

    payload = p.alerts.send_heartbeat_sitrep.call_args.args[0]
    assert payload.engine_status == "KILLED"
    assert payload.kill_switch_active is True


@pytest.mark.asyncio
async def test_vpin_regime_calm():
    p = Ports()
    p.engine_state._vpin = 0.30
    uc = p.uc(sitrep_interval=1)
    await uc.tick()
    assert p.alerts.send_heartbeat_sitrep.call_args.args[0].vpin_regime == "CALM"


@pytest.mark.asyncio
async def test_vpin_regime_normal():
    p = Ports()
    p.engine_state._vpin = 0.50
    uc = p.uc(sitrep_interval=1)
    await uc.tick()
    assert p.alerts.send_heartbeat_sitrep.call_args.args[0].vpin_regime == "NORMAL"


@pytest.mark.asyncio
async def test_vpin_regime_transition():
    p = Ports()
    p.engine_state._vpin = 0.70
    uc = p.uc(sitrep_interval=1)
    await uc.tick()
    assert p.alerts.send_heartbeat_sitrep.call_args.args[0].vpin_regime == "TRANSITION"


@pytest.mark.asyncio
async def test_vpin_regime_cascade():
    p = Ports()
    p.engine_state._vpin = 0.90
    uc = p.uc(sitrep_interval=1)
    await uc.tick()
    assert p.alerts.send_heartbeat_sitrep.call_args.args[0].vpin_regime == "CASCADE"


@pytest.mark.asyncio
async def test_win_rate_zero_trades():
    p = Ports()
    p.system_state_repo.get_daily_record.return_value = (0, 0)
    uc = p.uc(sitrep_interval=1)
    await uc.tick()
    assert p.alerts.send_heartbeat_sitrep.call_args.args[0].win_rate == 0.0


@pytest.mark.asyncio
async def test_heartbeat_error_does_not_crash():
    p = Ports()
    p.system_state_repo.write_heartbeat.side_effect = RuntimeError("DB down")
    await p.uc().tick()  # Should not raise


@pytest.mark.asyncio
async def test_sitrep_error_does_not_crash():
    p = Ports()
    p.alerts.send_heartbeat_sitrep.side_effect = RuntimeError("Telegram down")
    await p.uc(sitrep_interval=1).tick()  # Should not raise


@pytest.mark.asyncio
async def test_counter_resets_after_sitrep():
    p = Ports()
    uc = p.uc(sitrep_interval=2)

    await uc.tick()
    await uc.tick()
    assert p.alerts.send_heartbeat_sitrep.call_count == 1

    await uc.tick()
    assert p.alerts.send_heartbeat_sitrep.call_count == 1

    await uc.tick()
    assert p.alerts.send_heartbeat_sitrep.call_count == 2


@pytest.mark.asyncio
async def test_live_mode_label():
    p = Ports()
    p.engine_state._paper_mode = False
    p.risk_manager.get_status.return_value = _risk(paper_mode=False)
    uc = p.uc(sitrep_interval=1)
    await uc.tick()
    assert p.alerts.send_heartbeat_sitrep.call_args.args[0].mode_label == "LIVE"
