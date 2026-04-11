"""engine.adapters.persistence -- PostgreSQL repository implementations.

Each repository class implements a domain port from ``engine.domain.ports``
and persists data to the Railway PostgreSQL instance via asyncpg.

Public API
----------
PgSignalRepository   Signal snapshot persistence.
PgSystemRepository   System state (heartbeat, kill-switch, bankroll).
PgTradeRepository    Trade record persistence (placed, resolved, PnL).
PgWindowRepository   Window snapshot persistence.
"""

from engine.adapters.persistence.pg_signal_repo import PgSignalRepository
from engine.adapters.persistence.pg_system_repo import PgSystemRepository
from engine.adapters.persistence.pg_trade_repo import PgTradeRepository
from engine.adapters.persistence.pg_window_repo import PgWindowRepository

__all__ = [
    "PgSignalRepository",
    "PgSystemRepository",
    "PgTradeRepository",
    "PgWindowRepository",
]
