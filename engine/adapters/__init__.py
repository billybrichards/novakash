"""engine.adapters -- Infrastructure adapters (ports & adapters pattern).

Each sub-package implements one or more domain ports defined in
``engine.domain.ports``.

Sub-packages
------------
market_feed     Market data feed adapters (Tiingo REST)
persistence     PostgreSQL repository implementations
"""

__all__ = [
    "market_feed",
    "persistence",
]
