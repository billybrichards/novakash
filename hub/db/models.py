"""
ORM Models

SQLAlchemy async-compatible models matching the full schema.

Tables:
  - users          — Dashboard login accounts
  - trades         — All placed and resolved orders/bets
  - signals        — VPIN, cascade, arb, regime signal history
  - daily_pnl      — Pre-aggregated daily P&L stats
  - system_state   — Single-row engine heartbeat + config
  - backtest_runs  — Historical backtest results
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.database import Base


class User(Base):
    """Dashboard authentication user."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(256), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Trade(Base):
    """Record of a single placed bet/order and its resolution."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    market_slug: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # YES | NO | ARB
    entry_price: Mapped[Optional[float]] = mapped_column(Numeric(10, 6))
    stake_usd: Mapped[Optional[float]] = mapped_column(Numeric(12, 4))
    fee_usd: Mapped[Optional[float]] = mapped_column(Numeric(10, 6))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING", index=True)
    outcome: Mapped[Optional[str]] = mapped_column(String(8))  # WIN | LOSS | PUSH
    payout_usd: Mapped[Optional[float]] = mapped_column(Numeric(12, 4))
    pnl_usd: Mapped[Optional[float]] = mapped_column(Numeric(12, 4))
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSON, name="metadata")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Signal(Base):
    """VPIN, cascade, arb, or regime signal snapshot."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class DailyPnL(Base):
    """Pre-aggregated daily P&L stats (written by engine at end of day)."""

    __tablename__ = "daily_pnl"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, unique=True, index=True)
    total_pnl: Mapped[Optional[float]] = mapped_column(Numeric(12, 4))
    num_trades: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[Optional[float]] = mapped_column(Float)
    bankroll_end: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))
    strategy_breakdown: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SystemState(Base):
    """
    Single-row engine heartbeat, health metrics, and runtime config.

    id is always 1. Upserted by the engine on each heartbeat.
    """

    __tablename__ = "system_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # Always 1
    state: Mapped[Optional[dict]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BacktestRun(Base):
    """Results of a historical backtest run."""

    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    start_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    total_pnl: Mapped[Optional[float]] = mapped_column(Numeric(12, 4))
    num_trades: Mapped[Optional[int]] = mapped_column(Integer)
    win_rate: Mapped[Optional[float]] = mapped_column(Float)
    sharpe_ratio: Mapped[Optional[float]] = mapped_column(Float)
    max_drawdown: Mapped[Optional[float]] = mapped_column(Float)
    params: Mapped[Optional[dict]] = mapped_column(JSON)
    trades_json: Mapped[Optional[list]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
