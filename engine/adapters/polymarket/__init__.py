"""Polymarket client adapters -- paper and live implementations."""

from adapters.polymarket.paper_client import PaperPolymarketClient
from adapters.polymarket.live_client import LivePolymarketClient

__all__ = ["PaperPolymarketClient", "LivePolymarketClient"]
