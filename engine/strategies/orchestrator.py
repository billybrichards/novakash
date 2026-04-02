"""
Orchestrator — Engine Central Coordinator

The Orchestrator owns the full lifecycle of every component in the engine.
It wires all feeds, signals, and strategies together and manages:
  - Component creation from Settings
  - Feed task management
  - Signal callback wiring (feeds → aggregator → signals → strategies)
  - Heartbeat (every 10s)
  - Resolution polling (every 5s)
  - Market state fan-out loop
  - Graceful shutdown

All components are created internally; the caller simply does:
    orch = Orchestrator(settings=settings)
    await orch.run()
"""

from __future__ import annotations

import asyncio
import os
import signal as _signal
from typing import Optional

import structlog

from alerts.telegram import TelegramAlerter
from config.runtime_config import runtime
from config.settings import Settings
from config.constants import FIVE_MIN_ENTRY_OFFSET
from data.aggregator import MarketAggregator
from data.feeds.binance_ws import BinanceWebSocketFeed
from data.feeds.chainlink_rpc import ChainlinkRPCFeed
from data.feeds.coinglass_api import CoinGlassAPIFeed
from data.feeds.polymarket_ws import PolymarketWebSocketFeed
from data.feeds.polymarket_5min import Polymarket5MinFeed
from data.models import (
    AggTrade,
    ArbOpportunity,
    CascadeSignal,
    LiquidationVolume,
    OpenInterestSnapshot,
    PolymarketOrderBook,
    VPINSignal,
)
from execution.opinion_client import OpinionClient
from execution.order_manager import OrderManager
from execution.polymarket_client import PolymarketClient
from execution.risk_manager import RiskManager
from persistence.db_client import DBClient
from signals.arb_scanner import ArbScanner
from signals.cascade_detector import CascadeDetector
from signals.regime_classifier import RegimeClassifier
from signals.vpin import VPINCalculator
from strategies.sub_dollar_arb import SubDollarArbStrategy
from strategies.vpin_cascade import VPINCascadeStrategy
from strategies.five_min_vpin import FiveMinVPINStrategy

log = structlog.get_logger(__name__)


class Orchestrator:
    """
    Central coordinator for the Novakash trading engine.

    Owns all component creation, wiring, task management, and shutdown.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._shutdown_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

        log.info("orchestrator.init", paper_mode=settings.paper_mode)

        # ── Persistence ────────────────────────────────────────────────────────
        self._db = DBClient(settings=settings)

        # ── Aggregator ─────────────────────────────────────────────────────────
        self._aggregator = MarketAggregator()

        # ── Alerts ─────────────────────────────────────────────────────────────
        self._alerter = TelegramAlerter(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            alerts_paper=settings.telegram_alerts_paper,
            alerts_live=settings.telegram_alerts_live,
            paper_mode=settings.paper_mode,
        )

        # ── Signal Processors ──────────────────────────────────────────────────
        # VPIN (wired to on_vpin_signal after creation)
        self._vpin_calc = VPINCalculator(
            on_signal=self._on_vpin_signal,
        )

        # Cascade Detector (wired to on_cascade_signal)
        self._cascade = CascadeDetector(
            on_signal=self._on_cascade_signal,
        )

        # Arb Scanner
        self._arb_scanner = ArbScanner(
            fee_mult=0.072,  # POLYMARKET_CRYPTO_FEE_MULT
            on_opportunities=self._on_arb_opportunities,
        )

        # Regime Classifier (stateless utility, no callbacks needed)
        self._regime = RegimeClassifier()

        # ── Execution Clients ──────────────────────────────────────────────────
        self._poly_client = PolymarketClient(
            private_key=settings.poly_private_key,
            api_key=settings.poly_api_key,
            api_secret=settings.poly_api_secret,
            api_passphrase=settings.poly_api_passphrase,
            funder_address=settings.poly_funder_address,
            paper_mode=settings.paper_mode,
        )
        self._opinion_client = OpinionClient(
            api_key=settings.opinion_api_key,
            wallet_key=settings.opinion_wallet_key,
            paper_mode=settings.paper_mode,
        )

        # ── Order & Risk Management ────────────────────────────────────────────
        self._order_manager = OrderManager(
            db=self._db,
            bankroll=settings.starting_bankroll,
            paper_mode=settings.paper_mode,
            on_resolution=self._on_order_resolution,
            poly_client=self._poly_client,
        )
        self._risk_manager = RiskManager(
            order_manager=self._order_manager,
            starting_bankroll=settings.starting_bankroll,
            paper_mode=settings.paper_mode,
        )

        # Wire alerter references now that risk_manager and poly_client exist
        self._alerter.set_risk_manager(self._risk_manager)
        self._alerter.set_poly_client(self._poly_client)

        # ── Strategies ─────────────────────────────────────────────────────────
        self._arb_strategy = SubDollarArbStrategy(
            order_manager=self._order_manager,
            risk_manager=self._risk_manager,
            poly_client=self._poly_client,
        )
        self._cascade_strategy = VPINCascadeStrategy(
            order_manager=self._order_manager,
            risk_manager=self._risk_manager,
            poly_client=self._poly_client,
            opinion_client=self._opinion_client,
        )
        
        # 5-minute Polymarket strategy (optional)
        self._five_min_strategy = None
        if settings.five_min_enabled:
            self._five_min_feed = Polymarket5MinFeed(
                assets=settings.five_min_assets.split(","),
                signal_offset=FIVE_MIN_ENTRY_OFFSET,
                on_window_signal=self._on_five_min_window,
                paper_mode=settings.paper_mode,
            )
            self._five_min_strategy = FiveMinVPINStrategy(
                order_manager=self._order_manager,
                risk_manager=self._risk_manager,
                poly_client=self._poly_client,
                vpin_calculator=self._vpin_calc,
                alerter=self._alerter,
            )
            log.info("orchestrator.five_min_enabled", assets=settings.five_min_assets)
        else:
            self._five_min_feed = None
            log.info("orchestrator.five_min_disabled")

        # 15-minute Polymarket strategy (uses same strategy, different feed)
        self._fifteen_min_feed = None
        fifteen_min_enabled = os.environ.get("FIFTEEN_MIN_ENABLED", "false").lower() == "true"
        fifteen_min_assets = os.environ.get("FIFTEEN_MIN_ASSETS", "BTC,ETH,SOL").split(",")
        if fifteen_min_enabled:
            self._fifteen_min_feed = Polymarket5MinFeed(
                assets=fifteen_min_assets,
                duration_secs=900,  # 15 minutes
                signal_offset=FIVE_MIN_ENTRY_OFFSET,  # Same entry offset (T-60s)
                on_window_signal=self._on_fifteen_min_window,
                paper_mode=settings.paper_mode,
            )
            log.info("orchestrator.fifteen_min_enabled", assets=fifteen_min_assets)

        # ── Feeds (wired after all components exist) ────────────────────────────
        self._binance_feed = BinanceWebSocketFeed(
            symbol="btcusdt",
            on_trade=self._on_binance_trade,
            on_liquidation=self._aggregator.on_liquidation,
        )
        # Optional feeds — only create if API keys are configured
        self._coinglass_feed = None
        if settings.coinglass_api_key:
            self._coinglass_feed = CoinGlassAPIFeed(
                api_key=settings.coinglass_api_key,
                symbol="BTC",
                on_oi=self._on_oi_update,
                on_liq=self._aggregator.on_liquidation_volume,
            )
            log.info("orchestrator.coinglass_enabled")
        else:
            log.info("orchestrator.coinglass_disabled", reason="no API key set")

        self._chainlink_feed = None
        if settings.polygon_rpc_url:
            self._chainlink_feed = ChainlinkRPCFeed(
                rpc_url=settings.polygon_rpc_url,
                on_price=self._aggregator.on_chainlink_price,
            )
            log.info("orchestrator.chainlink_enabled")
        else:
            log.info("orchestrator.chainlink_disabled", reason="no RPC URL set")

        # Polymarket token IDs from settings
        token_ids = [
            tid.strip()
            for tid in settings.poly_btc_token_ids.split(",")
            if tid.strip()
        ]
        self._polymarket_feed = PolymarketWebSocketFeed(
            token_ids=token_ids,
            on_book=self._on_polymarket_book,
        )

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Initialise all components, wire callbacks, and start all tasks.

        Order:
        1. Connect DB
        2. Connect exchange clients
        3. Start strategies
        4. Start feed tasks
        5. Start heartbeat task
        6. Start resolution polling task
        7. Start market state fan-out loop
        """
        log.info("orchestrator.starting")

        # 1. Connect DB
        try:
            await self._db.connect()
        except Exception as exc:
            log.error("orchestrator.db_connect_failed", error=str(exc))
            raise

        # 2. Connect exchange clients
        try:
            await self._poly_client.connect()
        except Exception as exc:
            log.warning("orchestrator.poly_connect_failed", error=str(exc))

        try:
            await self._opinion_client.connect()
        except Exception as exc:
            log.warning("orchestrator.opinion_connect_failed", error=str(exc))

        # 3. Start strategies
        await self._arb_strategy.start()
        await self._cascade_strategy.start()
        
        # Start 5-min feed and strategy if enabled
        if self._five_min_feed and self._five_min_strategy:
            await self._five_min_strategy.start()
            self._tasks.append(
                asyncio.create_task(self._five_min_feed.start(), name="feed:five_min")
            )

        if self._fifteen_min_feed:
            self._tasks.append(
                asyncio.create_task(self._fifteen_min_feed.start(), name="feed:fifteen_min")
            )

        # 4. Start feed tasks
        self._tasks.append(
            asyncio.create_task(self._binance_feed.start(), name="feed:binance")
        )
        if self._coinglass_feed:
            self._tasks.append(
                asyncio.create_task(self._coinglass_feed.start(), name="feed:coinglass")
            )
        if self._chainlink_feed:
            self._tasks.append(
                asyncio.create_task(self._chainlink_feed.start(), name="feed:chainlink")
            )
        self._tasks.append(
            asyncio.create_task(self._polymarket_feed.start(), name="feed:polymarket")
        )

        # 5. Heartbeat task (every 10s)
        self._tasks.append(
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat")
        )

        # 6. Resolution polling task (every 5s)
        self._tasks.append(
            asyncio.create_task(self._resolution_loop(), name="resolution_poller")
        )

        # 7. Market state fan-out loop
        self._tasks.append(
            asyncio.create_task(self._market_state_loop(), name="market_state_loop")
        )

        # Register OS signal handlers
        loop = asyncio.get_running_loop()
        for sig in (_signal.SIGINT, _signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._handle_os_signal)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass

        await self._alerter.send_system_alert("Engine started", level="info")
        log.info("orchestrator.started", tasks=len(self._tasks))

    async def run(self) -> None:
        """Start the engine and wait for shutdown."""
        await self.start()
        await self._shutdown_event.wait()
        await self.stop()

    async def stop(self) -> None:
        """Graceful shutdown of all components."""
        log.info("orchestrator.stopping")

        # Stop strategies
        await self._arb_strategy.stop()
        await self._cascade_strategy.stop()
        
        # Stop 5-min strategy and feed
        if self._five_min_strategy:
            await self._five_min_strategy.stop()
        if self._five_min_feed:
            await self._five_min_feed.stop()
        if self._fifteen_min_feed:
            await self._fifteen_min_feed.stop()

        # Stop feeds
        await self._binance_feed.stop()
        if self._coinglass_feed:
            await self._coinglass_feed.stop()
        if self._chainlink_feed:
            await self._chainlink_feed.stop()
        await self._polymarket_feed.stop()

        # Cancel all tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        # Disconnect clients
        try:
            await self._opinion_client.disconnect()
        except Exception as exc:
            log.warning("orchestrator.opinion_disconnect_error", error=str(exc))

        # Close DB
        try:
            await self._db.close()
        except Exception as exc:
            log.warning("orchestrator.db_close_error", error=str(exc))

        await self._alerter.send_system_alert("Engine stopped", level="warning")
        log.info("orchestrator.stopped")

    # ─── OS Signal Handler ────────────────────────────────────────────────────

    def _handle_os_signal(self) -> None:
        """Trigger graceful shutdown on SIGINT/SIGTERM."""
        log.info("orchestrator.shutdown_signal_received")
        self._shutdown_event.set()

    # ─── Feed Callback Wiring ─────────────────────────────────────────────────

    async def _on_binance_trade(self, trade: AggTrade) -> None:
        """Binance aggTrade → aggregator + VPIN calculator + regime classifier."""
        # Update aggregator (BTC price, price history)
        await self._aggregator.on_agg_trade(trade)

        # Update order manager BTC price for paper resolution
        self._order_manager.update_btc_price(trade.price)

        # Feed VPIN calculator (triggers on_vpin_signal when bucket fills)
        await self._vpin_calc.on_trade(trade)

        # Update regime classifier
        self._regime.on_price(float(trade.price))

    async def _on_oi_update(self, oi: OpenInterestSnapshot) -> None:
        """CoinGlass OI → aggregator + cascade detector update."""
        await self._aggregator.on_open_interest(oi)

        # Get current state to drive cascade detector
        state = await self._aggregator.get_state()
        vpin_value = state.vpin.value if state.vpin else 0.0
        liq_5m = float(state.liq_volume_5m_usd or 0)

        if state.btc_price and state.btc_price_5m_ago:
            try:
                await self._cascade.update(
                    vpin=vpin_value,
                    oi_delta_pct=oi.open_interest_delta_pct,
                    liq_volume_5m=liq_5m,
                    btc_price=float(state.btc_price),
                    btc_price_5m_ago=float(state.btc_price_5m_ago),
                )
            except Exception as exc:
                log.error("orchestrator.cascade_update_failed", error=str(exc))

    async def _on_polymarket_book(self, book: PolymarketOrderBook) -> None:
        """Polymarket order book → aggregator + arb scanner.
        
        The book now contains both YES and NO sides (NO derived from YES complement).
        Feed the complete book to the arb scanner for scanning.
        """
        await self._aggregator.on_polymarket_book(book)

        # Feed arb scanner with the complete book (both sides now populated)
        try:
            # Pass the book with side="YES" to maintain compatibility,
            # but the book now contains both YES and NO data
            await self._arb_scanner.on_book(book, side="YES")
        except Exception as exc:
            log.error("orchestrator.arb_scanner_failed", error=str(exc))

    # ─── Signal Callbacks ─────────────────────────────────────────────────────

    async def _on_vpin_signal(self, signal: VPINSignal) -> None:
        """VPIN signal → aggregator + DB persistence + cascade detector update."""
        # Update aggregator
        await self._aggregator.on_vpin_signal(signal)

        # Persist to DB
        try:
            await self._db.write_signal(
                signal_type="vpin",
                value=signal.value,
                metadata=signal.model_dump(mode="json"),
            )
        except Exception as exc:
            log.error("orchestrator.db_vpin_write_failed", error=str(exc))

        # Drive cascade detector with latest state
        state = await self._aggregator.get_state()
        if state.btc_price and state.btc_price_5m_ago:
            try:
                await self._cascade.update(
                    vpin=signal.value,
                    oi_delta_pct=state.oi_delta_pct or 0.0,
                    liq_volume_5m=float(state.liq_volume_5m_usd or 0),
                    btc_price=float(state.btc_price),
                    btc_price_5m_ago=float(state.btc_price_5m_ago),
                )
            except Exception as exc:
                log.error("orchestrator.cascade_update_from_vpin_failed", error=str(exc))

    async def _on_cascade_signal(self, signal: CascadeSignal) -> None:
        """Cascade FSM signal → aggregator + DB persistence + alert."""
        # Update aggregator
        await self._aggregator.on_cascade_signal(signal)

        # Persist to DB
        try:
            await self._db.write_signal(
                signal_type="cascade",
                value=1.0,
                metadata=signal.model_dump(mode="json"),
            )
        except Exception as exc:
            log.error("orchestrator.db_cascade_write_failed", error=str(exc))

        # Send Telegram alert
        await self._alerter.send_cascade_alert(signal)

    async def _on_arb_opportunities(self, opps: list[ArbOpportunity]) -> None:
        """Arb opportunities → aggregator + DB persistence."""
        await self._aggregator.on_arb_opportunities(opps)

        # Persist best opportunity if present
        if opps:
            best = max(opps, key=lambda o: float(o.net_spread))
            try:
                await self._db.write_signal(
                    signal_type="arb",
                    value=float(best.net_spread),
                    metadata={
                        "market_slug": best.market_slug,
                        "yes_price": str(best.yes_price),
                        "no_price": str(best.no_price),
                        "combined_price": str(best.combined_price),
                        "net_spread": str(best.net_spread),
                    },
                )
            except Exception as exc:
                log.error("orchestrator.db_arb_write_failed", error=str(exc))

    # ─── 5-Minute Feed Callbacks ──────────────────────────────────────────────

    async def _on_five_min_window(self, window) -> None:
        """Handle 5-minute window signal from the feed.
        
        Logs for observability AND forwards to the strategy for evaluation.
        """
        log.info(
            "five_min.window_signal",
            asset=window.asset,
            window_ts=window.window_ts,
            open_price=window.open_price,
            up_price=window.up_price,
            down_price=window.down_price,
        )
        # Forward to strategy — queues window so next on_market_state evaluates it
        if self._five_min_strategy:
            self._five_min_strategy._pending_windows.append(window)

    async def _on_fifteen_min_window(self, window) -> None:
        """Handle 15-minute window signal — same strategy, different timeframe."""
        log.info(
            "fifteen_min.window_signal",
            asset=window.asset,
            window_ts=window.window_ts,
            open_price=window.open_price,
            up_price=window.up_price,
            down_price=window.down_price,
            duration=900,
        )
        # Reuse the same 5-min strategy for evaluation
        if self._five_min_strategy:
            self._five_min_strategy._pending_windows.append(window)

    # ─── Order Resolution Callback ────────────────────────────────────────────

    def _on_order_resolution(self, order) -> None:
        """Called by OrderManager when an order resolves. Notify risk manager.
        
        Only sends Telegram alerts for orders that actually filled on the CLOB
        (metadata['filled'] == True). Unfilled/expired orders are silently
        recorded — no misleading WIN/LOSS notifications.
        """
        if order.pnl_usd is not None:
            # Resolution alerts — labelled as ESTIMATE
            # Internal resolution may not match Polymarket oracle.
            # Wallet balance in the alert lets Billy cross-check.
            filled = order.metadata.get("filled", False) if order.metadata else False
            if filled:
                asyncio.create_task(self._alerter.send_trade_alert(order))

    # ─── Background Tasks ─────────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Every 10s: update system state and feed connection flags in DB."""
        # Wallet balance refresh counter (fetch every 6th heartbeat = ~60s)
        _wallet_check_counter = 0
        _cached_wallet_balance: float | None = None
        # Sitrep counter (send every 30th heartbeat = ~5 minutes)
        _sitrep_counter = 0
        _sitrep_trades_total = 0
        _sitrep_trades_filled = 0

        while not self._shutdown_event.is_set():
            try:
                # ── Sync runtime config from DB (trading_configs table) ────
                # Pulls the active config for current mode every heartbeat.
                # DB values overlay env var defaults — hot reload without restart.
                if self._db._pool:
                    await runtime.sync(
                        self._db._pool,
                        paper_mode=self._settings.paper_mode,
                    )

                risk_status = self._risk_manager.get_status()
                state = await self._aggregator.get_state()

                open_orders = await self._order_manager.get_open_orders()

                # Fetch Polymarket wallet balance periodically (~every 60s)
                _wallet_check_counter += 1
                if _wallet_check_counter >= 6:
                    _wallet_check_counter = 0
                    try:
                        _cached_wallet_balance = await self._poly_client.get_balance()
                        # Sync risk manager bankroll from portfolio value (cash + positions)
                        # This prevents bankroll shrinking due to unredeemed wins
                        try:
                            portfolio_value = await self._poly_client.get_portfolio_value()
                            await self._risk_manager.sync_bankroll(portfolio_value)
                        except Exception:
                            # Fallback to cash-only if portfolio fetch fails
                            if _cached_wallet_balance is not None:
                                await self._risk_manager.sync_bankroll(_cached_wallet_balance)
                    except Exception as exc:
                        log.debug("heartbeat.wallet_balance_error", error=str(exc))

                # Build config snapshot with wallet + risk extras + runtime config
                config_snapshot = {
                    "wallet_balance_usdc": _cached_wallet_balance,
                    "daily_pnl": risk_status.get("daily_pnl", 0),
                    "consecutive_losses": risk_status.get("consecutive_losses", 0),
                    "paper_mode": risk_status.get("paper_mode", True),
                    "kill_switch_active": risk_status.get("kill_switch_active", False),
                    "runtime_config": runtime.snapshot(),
                }

                await self._db.update_system_state(
                    engine_status="running",
                    current_balance=risk_status["current_bankroll"],
                    peak_balance=risk_status["peak_bankroll"],
                    current_drawdown_pct=risk_status["drawdown_pct"],
                    last_vpin=state.vpin.value if state.vpin else None,
                    last_cascade_state=state.cascade.state if state.cascade else None,
                    active_positions=len(open_orders),
                    config=config_snapshot,
                )

                await self._db.update_feed_status(
                    binance=self._binance_feed.connected,
                    coinglass=self._coinglass_feed.connected if self._coinglass_feed else False,
                    chainlink=self._chainlink_feed.connected if self._chainlink_feed else False,
                    polymarket=self._polymarket_feed.connected,
                    opinion=self._opinion_client.connected,
                )

                # Update venue connectivity in risk manager
                await self._risk_manager.update_venue_status(
                    polymarket=self._polymarket_feed.connected,
                    opinion=self._opinion_client.connected,
                )

                # ── 5-Minute Sitrep to Telegram ──────────────────────────
                _sitrep_counter += 1
                if _sitrep_counter >= 30:  # 30 × 10s = 5 minutes
                    _sitrep_counter = 0
                    try:
                        om_total = self._order_manager.total_orders
                        om_resolved = self._order_manager.resolved_orders
                        om_open = len(open_orders)
                        wallet = _cached_wallet_balance or 0
                        bankroll = risk_status.get("current_bankroll", 0)
                        daily_pnl = risk_status.get("daily_pnl", 0)
                        drawdown = risk_status.get("drawdown_pct", 0)
                        killed = risk_status.get("is_killed", False)
                        vpin = state.vpin.value if state.vpin else 0
                        regime = "TRENDING" if state.regime and state.regime.regime == "TRENDING" else "LOW_VOL"
                        binance_ok = self._binance_feed.connected

                        daily_sign = "+" if daily_pnl >= 0 else ""
                        status_emoji = "🛑" if killed else "🟢"

                        # Fetch REAL position outcomes from Polymarket
                        real_wins = 0
                        real_losses = 0
                        open_positions_val = 0
                        try:
                            pos_outcomes = await self._poly_client.get_position_outcomes()
                            for cid, data in pos_outcomes.items():
                                if data["outcome"] == "WIN":
                                    real_wins += 1
                                elif data["outcome"] == "LOSS":
                                    real_losses += 1
                                else:
                                    open_positions_val += data["value"]
                        except Exception:
                            pass

                        portfolio = wallet + open_positions_val
                        real_pnl = portfolio - 208.98  # from deposits

                        sitrep = (
                            f"📋 *5-MIN SITREP* ({status_emoji} {'KILLED' if killed else 'ACTIVE'})\n"
                            f"\n"
                            f"🏦 Cash: `${wallet:.2f}` USDC\n"
                            f"📊 Positions: `${open_positions_val:.2f}`\n"
                            f"💰 Portfolio: `${portfolio:.2f}`\n"
                            f"📈 Real P&L: `${real_pnl:+.2f}` (from $209 deposit)\n"
                            f"\n"
                            f"✅ Poly Wins: `{real_wins}` | ❌ Losses: `{real_losses}`\n"
                            f"📉 Drawdown: `{drawdown:.1%}`\n"
                            f"\n"
                            f"🔬 VPIN: `{vpin:.4f}` | Regime: `{regime}`\n"
                            f"🔗 Binance: `{'✅' if binance_ok else '❌'}` | BTC: `${self._order_manager._current_btc_price:,.2f}`\n"
                        )

                        await self._alerter.send_system_alert(sitrep, level="info")
                    except Exception as exc:
                        log.debug("sitrep.failed", error=str(exc))

            except Exception as exc:
                log.error("orchestrator.heartbeat_error", error=str(exc))

            try:
                await asyncio.wait_for(
                    asyncio.shield(self._shutdown_event.wait()),
                    timeout=10.0,
                )
                break  # Shutdown event set
            except asyncio.TimeoutError:
                pass  # Normal — continue heartbeat

    async def _resolution_loop(self) -> None:
        """Every 5s: poll order resolutions (paper mode simulation)."""
        while not self._shutdown_event.is_set():
            try:
                await self._order_manager.poll_resolutions()
            except Exception as exc:
                log.error("orchestrator.resolution_poll_error", error=str(exc))

            try:
                await asyncio.wait_for(
                    asyncio.shield(self._shutdown_event.wait()),
                    timeout=5.0,
                )
                break
            except asyncio.TimeoutError:
                pass

    async def _market_state_loop(self) -> None:
        """
        Consume aggregator.stream() and fan out each MarketState to all strategies.

        Strategies run concurrently per state update.
        """
        strategies = [self._arb_strategy, self._cascade_strategy]
        
        # Add 5-min strategy if enabled
        if self._five_min_strategy:
            strategies.append(self._five_min_strategy)

        try:
            async for state in self._aggregator.stream():
                if self._shutdown_event.is_set():
                    break

                # Fan out to all strategies concurrently
                results = await asyncio.gather(
                    *[strategy.on_market_state(state) for strategy in strategies],
                    return_exceptions=True,
                )

                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        log.error(
                            "orchestrator.strategy_error",
                            strategy=strategies[i].name,
                            error=str(result),
                        )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.error("orchestrator.market_state_loop_error", error=str(exc))
