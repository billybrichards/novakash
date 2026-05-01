"""Domain layer for the strategy-comparison rollup system.

See docs/architecture/2026-05-01-strategy-comparison-system.md.

Pure domain — no IO, no SQLAlchemy, no asyncio. Only value objects, entities,
and pure-function math.
"""
