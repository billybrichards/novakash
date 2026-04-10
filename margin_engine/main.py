"""
Margin engine entry point — wires all layers together and runs the main loop.

Architecture: Clean Architecture (dependency rule enforced)
  Domain (entities, value objects, ports)
    ← Use Cases (open_position, manage_positions)
      ← Adapters (binance/paper exchange, WS signal, telegram, PG repo)
        ← Infrastructure (config, DB pool, this file)

The main loop:
  1. Connect to v3 composite signal WS
  2. Every tick_interval_s:
     a. Check for new signals above threshold → open positions
     b. Manage open positions (stops, trailing, expiry, reversals)
  3. Graceful shutdown on SIGINT/SIGTERM
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import asyncpg

from margin_engine.infrastructure.config.settings import MarginSettings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("margin_engine")


async def run() -> None:
    """Main async entry point — wires adapters and runs the trading loop."""
    settings = MarginSettings()
    logger.info("Margin engine starting (paper_mode=%s, leverage=%dx)", settings.paper_mode, settings.leverage)

    # ── Database ──
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3, command_timeout=10)

    # ── Repositories ──
    from margin_engine.adapters.persistence.pg_repository import PgPositionRepository
    repo = PgPositionRepository(pool)
    await repo.ensure_table()

    from margin_engine.adapters.persistence.pg_log_repository import PgLogRepository, AsyncPgLogHandler
    log_repo = PgLogRepository(pool)
    await log_repo.ensure_table()

    # Attach DB log handler to root logger
    log_handler = AsyncPgLogHandler(log_repo, asyncio.get_running_loop())
    logging.getLogger().addHandler(log_handler)
    log_handler.start()

    # Passive signal recorder — writes every composite_score to margin_signals
    # for offline edge analysis. Write-only; trading never reads this table.
    from margin_engine.adapters.persistence.pg_signal_repository import (
        PgSignalRepository, AsyncPgSignalRecorder,
    )
    signal_repo = PgSignalRepository(pool)
    await signal_repo.ensure_table()
    signal_recorder = AsyncPgSignalRecorder(signal_repo, asyncio.get_running_loop())
    signal_recorder.start()

    # ── Exchange adapter (2x2 matrix: paper × venue) ──
    # paper_mode and exchange_venue are orthogonal:
    #   paper + binance     → PaperExchangeAdapter, Binance fee model, no external price
    #   paper + hyperliquid → PaperExchangeAdapter, HL fee model + HL price feed
    #   live  + binance     → BinanceMarginAdapter (existing, unchanged)
    #   live  + hyperliquid → HyperliquidLiveAdapter (EIP-712 agent wallet)
    venue = settings.exchange_venue
    paper = settings.paper_mode
    price_feed = None  # set in the HL paper branch only — must be reachable from shutdown
    effective_fee_rate: float | None = None
    effective_spread_bps: float | None = None

    if paper and venue == "binance":
        from margin_engine.adapters.exchange.paper import PaperExchangeAdapter
        effective_fee_rate = settings.effective_paper_fee_rate
        effective_spread_bps = settings.effective_paper_spread_bps
        exchange = PaperExchangeAdapter(
            starting_balance=settings.starting_capital,
            spread_bps=effective_spread_bps,
            fee_rate=effective_fee_rate,
        )
        price_feed_source = "internal"
        logger.info(
            "Using PAPER exchange (Binance model): fee=%.5f/side spread=%.2fbp",
            effective_fee_rate, effective_spread_bps,
        )

    elif paper and venue == "hyperliquid":
        from margin_engine.adapters.exchange.paper import PaperExchangeAdapter
        from margin_engine.adapters.exchange.hyperliquid_price_feed import (
            HyperliquidPriceFeed,
        )
        effective_fee_rate = settings.effective_paper_fee_rate
        effective_spread_bps = settings.effective_paper_spread_bps
        price_feed = HyperliquidPriceFeed(
            info_url=settings.hyperliquid_info_url,
            asset=settings.hyperliquid_asset,
            poll_interval_s=settings.hyperliquid_poll_interval_s,
            freshness_s=settings.hyperliquid_price_freshness_s,
        )
        await price_feed.connect()
        exchange = PaperExchangeAdapter(
            starting_balance=settings.starting_capital,
            spread_bps=effective_spread_bps,
            fee_rate=effective_fee_rate,
            price_getter=price_feed.get_price,
        )
        price_feed_source = "hyperliquid"
        logger.info(
            "Using PAPER exchange (Hyperliquid model): fee=%.5f/side spread=%.2fbp",
            effective_fee_rate, effective_spread_bps,
        )

    elif (not paper) and venue == "binance":
        from margin_engine.adapters.exchange.binance_margin import BinanceMarginAdapter
        exchange = BinanceMarginAdapter(
            api_key=settings.binance_api_key,
            private_key_path=settings.binance_private_key_path,
        )
        price_feed_source = "binance"
        logger.info("Using LIVE Binance margin adapter")

    else:  # not paper and venue == "hyperliquid"
        # ── LIVE HYPERLIQUID ──
        # Requires two pieces of config set by scripts/setup_hyperliquid_agent.sh:
        #   MARGIN_HYPERLIQUID_MAIN_ADDRESS    — your MetaMask public 0x…
        #   MARGIN_HYPERLIQUID_AGENT_KEY_PATH  — path to agent private key file
        # The adapter itself performs:
        #   - chmod 600 check on the key file
        #   - main != agent address sanity check
        #   - hex format validation
        # so failures here surface as clear HyperliquidLiveError messages
        # instead of obscure SDK tracebacks.
        if not settings.hyperliquid_main_address:
            raise RuntimeError(
                "MARGIN_HYPERLIQUID_MAIN_ADDRESS is not set. "
                "Run scripts/setup_hyperliquid_agent.sh on the box first, "
                "or set MARGIN_PAPER_MODE=true to stay in paper mode."
            )
        from margin_engine.adapters.exchange.hyperliquid_live import (
            HyperliquidLiveAdapter,
        )
        exchange = HyperliquidLiveAdapter(
            agent_key_path=settings.hyperliquid_agent_key_path,
            main_account_address=settings.hyperliquid_main_address,
            base_url=(settings.hyperliquid_api_base_url or None),
            asset=settings.hyperliquid_asset,
            leverage=settings.leverage,
            cross_margin=settings.hyperliquid_cross_margin,
        )
        await exchange.connect()
        price_feed_source = "hyperliquid"
        logger.info(
            "Using LIVE Hyperliquid adapter: account=%s leverage=%dx cross=%s",
            settings.hyperliquid_main_address, settings.leverage,
            settings.hyperliquid_cross_margin,
        )

    # Closure that returns the freshest execution context for /status. Built
    # AFTER all the locals above are bound so it captures live state like
    # price_feed.is_healthy at call time, not boot time.
    def build_execution_info() -> dict:
        if price_feed is not None:
            pf = price_feed.info()
        else:
            pf = {
                "source": price_feed_source,
                "healthy": True,
                "last_price": None,
                "last_price_age_s": None,
                "asset": settings.hyperliquid_asset,
            }
        return {
            "venue": venue,
            "paper_mode": paper,
            "fee_rate_per_side": effective_fee_rate,
            "fee_rate_per_side_bps": (effective_fee_rate * 10000)
            if effective_fee_rate is not None
            else None,
            "round_trip_fee_bps": (effective_fee_rate * 20000)
            if effective_fee_rate is not None
            else None,
            "spread_bps": effective_spread_bps,
            "price_feed": pf,
            "strategy": "v2-probability",
            "regime_threshold": settings.regime_threshold,
            "regime_timescale": settings.regime_timescale,
            "min_conviction": settings.probability_min_conviction,
        }

    # ── Signal adapter ──
    from margin_engine.adapters.signal.ws_signal import WsSignalAdapter
    signal_adapter = WsSignalAdapter(
        url=settings.timesfm_ws_url,
        on_message=signal_recorder.record,
    )
    await signal_adapter.connect()

    # ── Alert adapter ──
    from margin_engine.adapters.alert.telegram import TelegramAlertAdapter

    class NoopAlerts:
        async def send_trade_opened(self, p): pass
        async def send_trade_closed(self, p): pass
        async def send_kill_switch(self, r): pass
        async def send_error(self, m): logger.warning("Alert: %s", m)

    if settings.telegram_enabled and settings.telegram_bot_token:
        alerts = TelegramAlertAdapter(settings.telegram_bot_token, settings.telegram_chat_id)
    else:
        alerts = NoopAlerts()

    # ── Domain: Portfolio ──
    from margin_engine.domain.entities.portfolio import Portfolio
    from margin_engine.domain.value_objects import Money

    portfolio = Portfolio(
        starting_capital=Money.usd(settings.starting_capital),
        leverage=settings.leverage,
        max_open_positions=settings.max_open_positions,
        max_exposure_pct=settings.max_exposure_pct,
        daily_loss_limit_pct=settings.daily_loss_limit_pct,
        consecutive_loss_cooldown=settings.consecutive_loss_cooldown,
        cooldown_seconds=settings.cooldown_seconds,
    )

    # Restore open positions from DB
    open_positions = await repo.get_open_positions()
    for p in open_positions:
        portfolio.add_position(p)
    if open_positions:
        logger.info("Restored %d open positions from DB", len(open_positions))

    # ── Probability adapter (v2 ML direction signal) ──
    from margin_engine.adapters.signal.probability_http import ProbabilityHttpAdapter
    probability_adapter = ProbabilityHttpAdapter(
        base_url=settings.probability_http_url,
        asset=settings.probability_asset,
        timescale=settings.probability_timescale,
        seconds_to_close=settings.probability_seconds_to_close,
        poll_interval_s=settings.probability_poll_interval_s,
        freshness_seconds=settings.probability_freshness_s,
    )
    await probability_adapter.connect()

    # ── Use Cases ──
    from margin_engine.use_cases.open_position import OpenPositionUseCase
    from margin_engine.use_cases.manage_positions import ManagePositionsUseCase

    open_uc = OpenPositionUseCase(
        exchange=exchange,
        portfolio=portfolio,
        repository=repo,
        alerts=alerts,
        probability_port=probability_adapter,
        signal_port=signal_adapter,
        min_conviction=settings.probability_min_conviction,
        regime_threshold=settings.regime_threshold,
        regime_timescale=settings.regime_timescale,
        bet_fraction=settings.bet_fraction,
        stop_loss_pct=settings.stop_loss_pct,
        take_profit_pct=settings.take_profit_pct,
        venue=venue,
        strategy_version="v2-probability",
    )

    manage_uc = ManagePositionsUseCase(
        exchange=exchange,
        portfolio=portfolio,
        repository=repo,
        alerts=alerts,
        trailing_stop_pct=settings.trailing_stop_pct,
    )

    # ── Status HTTP server (for dashboard proxy) ──
    # Pass position_repo so /history works, and execution_info_fn so /status
    # surfaces venue/fee/price-feed health for the dashboard.
    from margin_engine.infrastructure.status_server import StatusServer
    status_server = StatusServer(
        portfolio,
        exchange,
        port=settings.status_port,
        log_repo=log_repo,
        position_repo=repo,
        execution_info_fn=build_execution_info,
    )
    await status_server.start()

    # ── Graceful shutdown ──
    shutdown_event = asyncio.Event()

    def _signal_handler(signum, frame):
        logger.info("Shutdown signal received (%s)", signum)
        shutdown_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # ── Main trading loop (v2 ML-directed) ──
    logger.info(
        "Margin engine ready (v2) — ML direction from %s %s, "
        "regime gate |composite_%s|>=%.2f, conviction>=%.2f",
        settings.probability_timescale,
        f"seconds_to_close={settings.probability_seconds_to_close}",
        settings.regime_timescale, settings.regime_threshold,
        settings.probability_min_conviction,
    )

    try:
        while not shutdown_event.is_set():
            try:
                # 1. Try to open a new position — the use case fetches
                #    both the probability signal AND the composite regime
                #    signal internally, so no per-timescale fan-out here.
                await open_uc.execute()

                # 2. Manage existing positions — price/time exits only.
                closed = await manage_uc.tick()
                for pos in closed:
                    logger.info(
                        "Position %s closed: PnL=%.2f (%s)",
                        pos.id, pos.realised_pnl,
                        pos.exit_reason.value if pos.exit_reason else "unknown",
                    )

            except Exception as e:
                logger.error("Main loop error: %s", e, exc_info=True)
                await alerts.send_error(f"Main loop error: {e}")

            await asyncio.sleep(settings.tick_interval_s)

    finally:
        logger.info("Shutting down margin engine...")
        await status_server.stop()
        # Disconnect WS first so no new messages arrive while flushing buffers
        await signal_adapter.disconnect()
        await probability_adapter.disconnect()
        # Hyperliquid price feed only exists in paper+hyperliquid wiring path
        if price_feed is not None:
            await price_feed.disconnect()
        await signal_recorder.stop()  # flush remaining signals before pool closes
        await log_handler.stop()  # flush remaining logs before pool closes
        if hasattr(exchange, "close"):
            await exchange.close()
        if hasattr(alerts, "close"):
            await alerts.close()
        await pool.close()
        logger.info("Margin engine stopped.")


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
