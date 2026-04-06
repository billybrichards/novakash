"""Data feeds package."""

from data.feeds.binance_ws import BinanceWebSocketFeed
from data.feeds.coinglass_api import CoinGlassAPIFeed
from data.feeds.chainlink_rpc import ChainlinkRPCFeed
from data.feeds.chainlink_feed import ChainlinkFeed
from data.feeds.tiingo_feed import TiingoFeed
from data.feeds.polymarket_ws import PolymarketWebSocketFeed
from data.feeds.polymarket_5min import Polymarket5MinFeed

__all__ = [
    "BinanceWebSocketFeed",
    "CoinGlassAPIFeed",
    "ChainlinkRPCFeed",
    "ChainlinkFeed",
    "TiingoFeed",
    "PolymarketWebSocketFeed",
    "Polymarket5MinFeed",
]
