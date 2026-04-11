"""engine.persistence -- Database persistence layer.

Public API
----------
DBClient        Async PostgreSQL client for trade records, signals, and system state.
TickRecorder    Real-time tick-level data recorder (Binance, CoinGlass, Gamma, TimesFM).
MANUAL_TRADE_NOTIFY_CHANNEL
                PostgreSQL LISTEN/NOTIFY channel name for manual trade fast-path.
"""

from persistence.db_client import DBClient, MANUAL_TRADE_NOTIFY_CHANNEL
from persistence.tick_recorder import TickRecorder

__all__ = [
    "DBClient",
    "TickRecorder",
    "MANUAL_TRADE_NOTIFY_CHANNEL",
]
