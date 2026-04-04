"""Pydantic request/response models for TimesFM forecast service."""

from typing import Optional
from pydantic import BaseModel, Field


class ForecastRequest(BaseModel):
    """Request body for POST /forecast."""
    prices: list[float] = Field(
        ...,
        min_length=10,
        description="List of BTC prices (at least 10 data points)"
    )
    horizon: int = Field(
        default=60,
        ge=1,
        le=512,
        description="Number of steps to forecast ahead"
    )


class QuantileForecasts(BaseModel):
    """Quantile forecast values."""
    p10: list[float]
    p25: list[float]
    p50: list[float]
    p75: list[float]
    p90: list[float]


class ForecastResponse(BaseModel):
    """Response body for forecast endpoints."""
    point_forecast: list[float] = Field(
        description="Point forecast prices for each step ahead"
    )
    quantiles: QuantileForecasts = Field(
        description="Quantile forecasts (P10, P25, P50, P75, P90)"
    )
    predicted_close: float = Field(
        description="Final predicted price at end of horizon"
    )
    direction: str = Field(
        description="'UP' or 'DOWN' vs window open price"
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence score (0-1) based on spread relative to price"
    )
    spread: float = Field(
        description="P90 - P10 at horizon end (uncertainty width)"
    )
    horizon: int = Field(
        description="Forecast horizon used"
    )
    input_length: int = Field(
        description="Number of input price points used"
    )
    timestamp: Optional[float] = Field(
        default=None,
        description="Unix timestamp when forecast was generated"
    )


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    model_loaded: bool
    price_feed_connected: bool
    buffer_size: int
    uptime_seconds: float
