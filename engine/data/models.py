"""
Data Models — Pydantic schemas for market data flowing through the engine.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel, Field


class AggTrade(BaseModel):
    """Binance aggregated trade."""
    symbol: str
    price: Decimal
    quantity: Decimal
    is_buyer_maker: bool
    trade_time: datetime


class OrderBookSnapshot(BaseModel):
    """Top-of-book snapshot from Binance depth feed."""
    symbol: str
    bids: list[tuple[Decimal, Decimal]]  # (price, qty)
    asks: list[tuple[Decimal, Decimal]]
    last_update_id: int
    timestamp: datetime


class ForcedLiquidation(BaseModel):
    """Binance forceOrder event — forced liquidation."""
    symbol: str
    side: str  # BUY or SELL
    price: Decimal
    quantity: Decimal
    timestamp: datetime


class OpenInterestSnapshot(BaseModel):
    """CoinGlass OI data point."""
    symbol: str
    open_interest_usd: Decimal
    open_interest_delta_pct: float  # vs previous snapshot
    timestamp: datetime


class LiquidationVolume(BaseModel):
    """CoinGlass liquidation volume in window."""
    symbol: str
    liq_volume_usd: Decimal
    window_seconds: int
    timestamp: datetime


class ChainlinkPrice(BaseModel):
    """Chainlink oracle price from Polygon RPC."""
    feed: str  # e.g. "BTC/USD"
    price: Decimal
    round_id: int
    timestamp: datetime


class PolymarketOrderBook(BaseModel):
    """Polymarket CLOB order book snapshot for a market."""
    market_slug: str
    token_id: str
    yes_bids: list[tuple[Decimal, Decimal]]
    yes_asks: list[tuple[Decimal, Decimal]]
    no_bids: list[tuple[Decimal, Decimal]]
    no_asks: list[tuple[Decimal, Decimal]]
    timestamp: datetime


class VPINSignal(BaseModel):
    """Output from the VPIN calculator."""
    value: float = Field(..., ge=0.0, le=1.0, description="VPIN metric 0–1")
    buckets_filled: int
    informed_threshold_crossed: bool
    cascade_threshold_crossed: bool
    timestamp: datetime


class CascadeSignal(BaseModel):
    """Output from the Cascade Detector FSM."""
    state: str  # IDLE | CASCADE_DETECTED | EXHAUSTING | BET_SIGNAL | COOLDOWN
    direction: Optional[str] = None  # YES | NO | None
    vpin: float
    oi_delta_pct: float
    liq_volume_usd: float
    timestamp: datetime


class ArbOpportunity(BaseModel):
    """Sub-$1 arbitrage opportunity."""
    market_slug: str
    yes_price: Decimal  # Best ask for YES
    no_price: Decimal   # Best ask for NO
    combined_price: Decimal  # yes + no combined
    net_spread: Decimal  # After fees
    max_position_usd: float
    timestamp: datetime


class MarketState(BaseModel):
    """Unified snapshot of all market data."""
    btc_price: Optional[Decimal] = None
    chainlink_price: Optional[Decimal] = None
    open_interest_usd: Optional[Decimal] = None
    oi_delta_pct: Optional[float] = None
    liq_volume_usd: Optional[Decimal] = None
    vpin: Optional[VPINSignal] = None
    cascade: Optional[CascadeSignal] = None
    arb_opportunities: list[ArbOpportunity] = Field(default_factory=list)
    last_updated: datetime = Field(default_factory=datetime.utcnow)
