"""Polymarket client adapters -- paper and live implementations."""

try:
    from adapters.polymarket.paper_client import PaperPolymarketClient
    from adapters.polymarket.live_client import LivePolymarketClient

    __all__ = ["PaperPolymarketClient", "LivePolymarketClient"]
except Exception:  # pragma: no cover — settings not available in unit-test env
    pass
