"""
BTC Trader Engine — Async Entry Point

Bootstraps all components, wires data feeds to the aggregator,
and runs the strategy orchestrator until shutdown.
"""

from __future__ import annotations

import asyncio
import structlog

from config.logging import configure_logging
from config.settings import settings

# Configure structlog before importing anything that uses it
configure_logging(level="INFO" if not __debug__ else "DEBUG")

from alerts.telegram import TelegramAlerter
from data.aggregator import MarketAggregator
from data.feeds.binance_ws import BinanceWebSocketFeed
from data.feeds.coinglass_api import CoinGlassAPIFeed
from data.feeds.chainlink_rpc import ChainlinkRPCFeed
from data.feeds.polymarket_ws import PolymarketWebSocketFeed
from execution.order_manager import OrderManager
from execution.risk_manager import RiskManager
from persistence.db_client import DBClient
from strategies.orchestrator import Orchestrator

log = structlog.get_logger(__name__)


async def main() -> None:
    """Bootstrap and run the trading engine."""
    log.info(
        "engine.starting",
        paper_mode=settings.paper_mode,
        bankroll=settings.starting_bankroll,
    )

    # ── 1. Persistence ────────────────────────────────────────────────────────
    db = DBClient(settings=settings)

    # ── 2. Market Aggregator ──────────────────────────────────────────────────
    aggregator = MarketAggregator()

    # ── 3. Data Feeds (wired to aggregator callbacks) ─────────────────────────
    binance_feed = BinanceWebSocketFeed(
        symbol="btcusdt",
        on_trade=aggregator.on_agg_trade,
        on_book=None,              # Order book not needed by aggregator directly
        on_liquidation=aggregator.on_liquidation,
    )

    coinglass_feed = CoinGlassAPIFeed(
        api_key=settings.coinglass_api_key,
        symbol="BTC",
        poll_interval=30,
        on_oi=aggregator.on_open_interest,
        on_liq=aggregator.on_liquidation_volume,
    )

    chainlink_feed = ChainlinkRPCFeed(
        rpc_url=settings.polygon_rpc_url or "https://polygon-rpc.com",
        poll_interval=10,
        on_price=aggregator.on_chainlink_price,
    )

    # Parse token IDs from settings (comma-separated string)
    poly_token_ids: list[str] = (
        [t.strip() for t in settings.poly_btc_token_ids.split(",") if t.strip()]
        if settings.poly_btc_token_ids
        else []
    )

    polymarket_feed = PolymarketWebSocketFeed(
        token_ids=poly_token_ids,
        on_book=aggregator.on_polymarket_book,
    )

    # ── 4. Execution Layer ────────────────────────────────────────────────────
    order_manager = OrderManager(db=db, bankroll=settings.starting_bankroll)

    risk_manager = RiskManager(
        order_manager=order_manager,
        starting_bankroll=settings.starting_bankroll,
        paper_mode=settings.paper_mode,
    )

    # ── 5. Alerts ─────────────────────────────────────────────────────────────
    alerter = TelegramAlerter(settings=settings)

    # ── 6. Strategies (empty list for Phase 2 — focus on feeds + DB) ──────────
    # Strategies will be added in Phase 3 once the data layer is validated.
    strategies = []

    # ── 7. Orchestrator ───────────────────────────────────────────────────────
    orchestrator = Orchestrator(
        strategies=strategies,
        aggregator=aggregator,
        binance_feed=binance_feed,
        coinglass_feed=coinglass_feed,
        chainlink_feed=chainlink_feed,
        polymarket_feed=polymarket_feed,
        order_manager=order_manager,
        risk_manager=risk_manager,
        db_client=db,
        alerter=alerter,
    )

    # ── 8. Run ────────────────────────────────────────────────────────────────
    try:
        await orchestrator.start()
        await orchestrator.run()
    except KeyboardInterrupt:
        log.info("engine.keyboard_interrupt")
    finally:
        await orchestrator.stop()
        log.info("engine.stopped")


if __name__ == "__main__":
    asyncio.run(main())
