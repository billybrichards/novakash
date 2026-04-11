"""engine.adapters -- Infrastructure adapters (ports & adapters pattern).

Each sub-package implements one or more domain ports defined in
``engine.domain.ports``.

Sub-packages
------------
alert           Telegram alerter (AlerterPort)
clock           System clock (Clock)
consensus       Three-source consensus price feed (ConsensusPricePort)
market_feed     Market data feed adapters (MarketFeedPort)
persistence     PostgreSQL repository implementations
prediction      TimesFM v1/v2 forecast adapters
"""
