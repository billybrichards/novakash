"""Market feed adapters -- implementations of ``engine.domain.ports.MarketFeedPort``.

Three adapters in this package:

- ``BinanceWebSocketAdapter`` -- wraps ``data.feeds.binance_ws.BinanceWebSocketFeed``
- ``ChainlinkDbAdapter`` -- wraps ``DBClient.get_latest_chainlink_price``
- ``TiingoDbAdapter`` -- wraps ``DBClient.get_latest_tiingo_price``

The ``ConsensusPricePort`` implementation in ``engine/adapters/consensus/``
composes three MarketFeedPort instances to produce the CL/TI/BIN delta triple.
"""

from adapters.market_feed.binance_ws import BinanceWebSocketAdapter
from adapters.market_feed.chainlink_db import ChainlinkDbAdapter
from adapters.market_feed.tiingo_db import TiingoDbAdapter

__all__ = [
    "BinanceWebSocketAdapter",
    "ChainlinkDbAdapter",
    "TiingoDbAdapter",
]
