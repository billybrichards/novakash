"""Market feed adapters -- implementations of ``engine.domain.ports.MarketFeedPort``."""

from engine.adapters.market_feed.tiingo_rest import TiingoRestAdapter

__all__ = ["TiingoRestAdapter"]
