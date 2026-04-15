"""EngineStateReaderAdapter — sync property bridge for PublishHeartbeatUseCase.

Implements the EngineStateReader protocol from use_cases/publish_heartbeat.py.
Call update() before each tick() to refresh cached async state.
"""
from __future__ import annotations
from typing import Any, Optional


class EngineStateReaderAdapter:
    """Bridges async aggregator state to sync EngineStateReader protocol.

    update() must be called each heartbeat tick with the latest state snapshot
    before publish_heartbeat_uc.tick() is invoked.
    """

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._vpin: float = 0.0
        self._btc_price: float = 0.0
        self._open_positions: int = 0
        self._cascade_state: Optional[str] = None
        self._feed_status: dict[str, bool] = {}

    def update(
        self,
        state: Any,
        open_positions: int,
        feed_status: dict[str, bool],
    ) -> None:
        """Refresh cached state. Call before publish_heartbeat_uc.tick()."""
        self._vpin = float(state.vpin.value) if getattr(state, "vpin", None) else 0.0
        self._btc_price = float(state.btc_price) if getattr(state, "btc_price", None) else 0.0
        self._open_positions = open_positions
        self._cascade_state = state.cascade.state if getattr(state, "cascade", None) else None
        self._feed_status = feed_status

    @property
    def vpin(self) -> float:
        return self._vpin

    @property
    def btc_price(self) -> float:
        return self._btc_price

    @property
    def open_positions_count(self) -> int:
        return self._open_positions

    @property
    def paper_mode(self) -> bool:
        return self._settings.paper_mode

    @property
    def starting_bankroll(self) -> float:
        return float(getattr(self._settings, "starting_bankroll", 500.0))

    @property
    def cascade_state(self) -> Optional[str]:
        return self._cascade_state

    @property
    def feed_status(self) -> dict[str, bool]:
        return self._feed_status
