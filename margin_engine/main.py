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

    # ── Repository ──
    from margin_engine.adapters.persistence.pg_repository import PgPositionRepository
    repo = PgPositionRepository(pool)
    await repo.ensure_table()

    # ── Exchange adapter ──
    if settings.paper_mode:
        from margin_engine.adapters.exchange.paper import PaperExchangeAdapter
        exchange = PaperExchangeAdapter(starting_balance=settings.starting_capital)
        logger.info("Using PAPER exchange adapter")
    else:
        from margin_engine.adapters.exchange.binance_margin import BinanceMarginAdapter
        exchange = BinanceMarginAdapter(
            api_key=settings.binance_api_key,
            private_key_path=settings.binance_private_key_path,
        )
        logger.info("Using LIVE Binance margin adapter")

    # ── Signal adapter ──
    from margin_engine.adapters.signal.ws_signal import WsSignalAdapter
    signal_adapter = WsSignalAdapter(url=settings.timesfm_ws_url)
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

    # ── Use Cases ──
    from margin_engine.use_cases.open_position import OpenPositionUseCase
    from margin_engine.use_cases.manage_positions import ManagePositionsUseCase

    open_uc = OpenPositionUseCase(
        exchange=exchange,
        portfolio=portfolio,
        repository=repo,
        alerts=alerts,
        signal_threshold=settings.signal_threshold,
        bet_fraction=settings.bet_fraction,
        stop_loss_pct=settings.stop_loss_pct,
        take_profit_pct=settings.take_profit_pct,
    )

    manage_uc = ManagePositionsUseCase(
        exchange=exchange,
        portfolio=portfolio,
        repository=repo,
        alerts=alerts,
        signal_port=signal_adapter,
        trailing_stop_pct=settings.trailing_stop_pct,
        signal_reversal_threshold=settings.signal_reversal_threshold,
    )

    # ── Status HTTP server (for dashboard proxy) ──
    from margin_engine.infrastructure.status_server import StatusServer
    status_server = StatusServer(portfolio, exchange, port=settings.status_port)
    await status_server.start()

    # ── Graceful shutdown ──
    shutdown_event = asyncio.Event()

    def _signal_handler(signum, frame):
        logger.info("Shutdown signal received (%s)", signum)
        shutdown_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # ── Main trading loop ──
    logger.info(
        "Margin engine ready — trading on timescales: %s, threshold: %.2f",
        settings.trading_timescales, settings.signal_threshold,
    )

    try:
        while not shutdown_event.is_set():
            try:
                # 1. Check for new entry signals
                for timescale in settings.trading_timescale_list:
                    sig = await signal_adapter.get_latest_signal(timescale)
                    if sig is not None:
                        await open_uc.execute(sig)

                # 2. Manage existing positions
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
        await signal_adapter.disconnect()
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
