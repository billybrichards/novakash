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
import time
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
from data.feeds.coinglass_enhanced import CoinGlassEnhancedFeed
from evaluation.claude_evaluator import ClaudeEvaluator
from data.feeds.polymarket_ws import PolymarketWebSocketFeed
from data.feeds.polymarket_5min import Polymarket5MinFeed
from polymarket_browser.service import PlaywrightService
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
from strategies.timesfm_only import TimesFMOnlyStrategy
from signals.twap_delta import TWAPTracker
from signals.timesfm_client import TimesFMClient

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

        # ── G1 & G3: Staggered execution + single best signal ─────────────────
        self._execution_queue: asyncio.Queue = asyncio.Queue()  # Pending window evaluations
        self._geoblock_active: bool = False  # G6: Geoblock flag

        # ── TWAP Tracker (v5.7: time-weighted delta for direction) ─────────
        self._twap_tracker = TWAPTracker(max_windows=50)

        # ── TimesFM Client (v6.0, initialized early for FiveMinVPIN alerts) ──
        self._timesfm_client: Optional[TimesFMClient] = None
        self._timesfm_strategy: Optional[TimesFMOnlyStrategy] = None

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
        
        # Determine effective starting bankroll (paper override if set)
        effective_bankroll = settings.paper_bankroll if settings.paper_mode and settings.paper_bankroll > 0 else settings.starting_bankroll
        
        self._risk_manager = RiskManager(
            order_manager=self._order_manager,
            starting_bankroll=effective_bankroll,
            paper_mode=settings.paper_mode,
        )

        # Wire alerter references now that risk_manager and poly_client exist
        self._alerter.set_risk_manager(self._risk_manager)
        self._alerter.set_poly_client(self._poly_client)

        # ── Builder Relayer Redeemer ──────────────────────────────────────────
        from execution.redeemer import PositionRedeemer
        self._redeemer = PositionRedeemer(
            rpc_url=settings.polygon_rpc_url,
            private_key=settings.poly_private_key,
            proxy_address=settings.poly_funder_address,
            paper_mode=settings.paper_mode,
            builder_key=settings.builder_key or os.environ.get("BUILDER_KEY", ""),
        )

        # ── Playwright browser automation (replaces on-chain redeemer) ────────
        self._playwright: PlaywrightService | None = None
        if settings.playwright_enabled:
            self._playwright = PlaywrightService(
                gmail_address=settings.gmail_address,
                gmail_app_password=settings.gmail_app_password,
                headless=True,
            )

        # ── CoinGlass Enhanced Feeds (per-asset, 10s poll each) ────────────────
        # BTC primary + ETH/SOL/XRP with staggered polls to stay within 300 req/min
        self._cg_enhanced: Optional[CoinGlassEnhancedFeed] = None
        self._cg_feeds: dict[str, CoinGlassEnhancedFeed] = {}
        if settings.coinglass_api_key:
            for _cg_sym in ("BTC", "ETH", "SOL", "XRP"):
                _feed = CoinGlassEnhancedFeed(
                    api_key=settings.coinglass_api_key,
                    symbol=_cg_sym,
                )
                self._cg_feeds[_cg_sym] = _feed
                if _cg_sym == "BTC":
                    self._cg_enhanced = _feed  # backward compat
            log.info("orchestrator.coinglass_multi_asset", assets=list(self._cg_feeds.keys()))

        # ── Claude Opus 4.6 AI Evaluator ─────────────────────────────────────
        self._claude_evaluator = None
        if settings.anthropic_api_key:
            self._claude_evaluator = ClaudeEvaluator(
                api_key=settings.anthropic_api_key,
                alerter=self._alerter,
                db_client=self._db,
            )
            log.info("orchestrator.claude_evaluator_enabled")

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
                cg_enhanced=self._cg_enhanced,
                cg_feeds=self._cg_feeds,
                claude_evaluator=self._claude_evaluator,
                db_client=self._db,
                geoblock_check_fn=lambda: self._geoblock_active,  # G6
                twap_tracker=self._twap_tracker,  # v5.7: TWAP direction
                timesfm_client=self._timesfm_client,  # v6.0: TimesFM for comparison alerts
            )
            log.info("orchestrator.five_min_enabled", assets=settings.five_min_assets)
        else:
            self._five_min_feed = None
            log.info("orchestrator.five_min_disabled")

        # ── v6.0 TimesFM-Only Strategy ──────────────────────────────────────
        timesfm_enabled = os.environ.get("TIMESFM_ENABLED", "false").lower() == "true"
        timesfm_url = os.environ.get("TIMESFM_URL", "http://16.52.148.255:8000")
        timesfm_min_conf = float(os.environ.get("TIMESFM_MIN_CONFIDENCE", "0.30"))

        if timesfm_enabled:
            self._timesfm_client = TimesFMClient(
                base_url=timesfm_url,
                timeout_seconds=10.0,
            )
            self._timesfm_strategy = TimesFMOnlyStrategy(
                order_manager=self._order_manager,
                risk_manager=self._risk_manager,
                poly_client=self._poly_client,
                timesfm_client=self._timesfm_client,
                alerter=self._alerter,
                db_client=self._db,
                twap_tracker=self._twap_tracker,
                min_confidence=timesfm_min_conf,
            )
            log.info(
                "orchestrator.timesfm_v6_enabled",
                url=timesfm_url,
                min_confidence=timesfm_min_conf,
            )
        else:
            log.info("orchestrator.timesfm_v6_disabled")

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
        1. Geoblock check (live mode only) — G6
        2. Connect DB
        3. Connect exchange clients
        4. Start strategies
        5. Start feed tasks
        6. Start heartbeat task
        7. Start resolution polling task
        8. Start market state fan-out loop
        """
        log.info("orchestrator.starting")

        # ── G6: Geoblock check (live mode only) ────────────────────────────────
        if not self._settings.paper_mode:
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        "https://polymarket.com/api/geoblock",
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        data = await resp.json()
                        if data.get("blocked"):
                            self._geoblock_active = True
                            country = data.get("country", "UNKNOWN")
                            ip = data.get("ip", "UNKNOWN")
                            log.error(
                                "guardrail.geoblock.blocked",
                                country=country,
                                ip=ip,
                            )
                            await self._alerter.send_system_alert(
                                f"🚨 GEOBLOCK: Trading blocked from {country} ({ip}). "
                                f"Engine will NOT place live orders.",
                                level="critical",
                            )
            except Exception as exc:
                # Geoblock check failed — log but don't block startup
                log.warning("guardrail.geoblock.check_failed", error=str(exc))
        
        # ── Startup continues ──────────────────────────────────────────────────

        # 1. Connect DB
        try:
            await self._db.connect()
        except Exception as exc:
            log.error("orchestrator.db_connect_failed", error=str(exc))
            raise

        # Ensure window_snapshots table exists (non-fatal if it fails)
        try:
            await self._db.ensure_window_tables()
        except Exception as exc:
            log.warning("orchestrator.ensure_window_tables_failed", error=str(exc))

        # 2. Connect exchange clients
        try:
            await self._poly_client.connect()
        except Exception as exc:
            log.warning("orchestrator.poly_connect_failed", error=str(exc))

        try:
            await self._opinion_client.connect()
        except Exception as exc:
            log.warning("orchestrator.opinion_connect_failed", error=str(exc))

        # 3. Set paper bankroll if configured (before strategies start)
        if self._settings.paper_mode and self._settings.paper_bankroll > 0:
            await self._risk_manager.set_paper_bankroll(self._settings.paper_bankroll)

        # 3. Start strategies
        await self._arb_strategy.start()
        await self._cascade_strategy.start()
        
        # Start 5-min feed and strategy if enabled
        if self._five_min_feed and self._five_min_strategy:
            await self._five_min_strategy.start()
            self._tasks.append(
                asyncio.create_task(self._five_min_feed.start(), name="feed:five_min")
            )

        # Start v6.0 TimesFM-only strategy if enabled
        if self._timesfm_strategy:
            await self._timesfm_strategy.start()

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
        if self._cg_feeds:
            for _sym, _cgf in self._cg_feeds.items():
                self._tasks.append(
                    asyncio.create_task(self._start_cg_staggered(_cgf, _sym), name=f"feed:cg_{_sym.lower()}")
                )
        elif self._cg_enhanced:
            self._tasks.append(
                asyncio.create_task(self._cg_enhanced.start(), name="feed:coinglass_enhanced")
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

        # 8. Builder Relayer redeemer (live mode only)
        if not self._settings.paper_mode:
            try:
                await self._redeemer.connect()
                self._tasks.append(
                    asyncio.create_task(
                        self._redeemer_loop(), name="redeemer:sweep"
                    )
                )
                log.info("orchestrator.redeemer_started")
            except Exception as e:
                log.error("orchestrator.redeemer_start_failed", error=str(e))

        # 9. Playwright automation (balance / screenshot only — redeem handled by redeemer)
        if self._playwright:
            try:
                await self._playwright.start()
                await self._db.ensure_playwright_tables()
                self._tasks.append(
                    asyncio.create_task(
                        self._playwright_balance_loop(), name="playwright:balance"
                    )
                )
                self._tasks.append(
                    asyncio.create_task(
                        self._playwright_screenshot_loop(), name="playwright:screenshot"
                    )
                )
                log.info("orchestrator.playwright_started")
            except Exception as e:
                log.error("orchestrator.playwright_start_failed", error=str(e))

        if not self._settings.paper_mode:
            self._tasks.append(
                asyncio.create_task(self._position_monitor_loop(), name="position_monitor")
            )

        # ── G1 & G3: Staggered execution + single best signal loop ──────────────
        self._tasks.append(
            asyncio.create_task(self._staggered_execution_loop(), name="staggered_execution")
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

        # Stop Playwright browser
        if self._playwright:
            await self._playwright.stop()

        # Stop strategies
        await self._arb_strategy.stop()
        await self._cascade_strategy.stop()
        
        # Stop v6.0 TimesFM strategy
        if self._timesfm_strategy:
            await self._timesfm_strategy.stop()
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
        """Binance aggTrade → aggregator + VPIN calculator + regime classifier + TWAP."""
        # Update aggregator (BTC price, price history)
        await self._aggregator.on_agg_trade(trade)

        # Update order manager BTC price for paper resolution
        self._order_manager.update_btc_price(trade.price)

        # Feed VPIN calculator (triggers on_vpin_signal when bucket fills)
        await self._vpin_calc.on_trade(trade)

        # Update regime classifier
        self._regime.on_price(float(trade.price))

        # Feed TWAP tracker — add tick to all active BTC windows
        # For non-BTC assets, ticks are added via _fetch_current_price in strategy
        for wkey in self._twap_tracker.active_windows:
            if wkey.startswith("BTC-"):
                parts = wkey.split("-", 1)
                if len(parts) == 2:
                    try:
                        wts = int(parts[1])
                        self._twap_tracker.add_tick("BTC", wts, float(trade.price), float(trade.timestamp) / 1000 if trade.timestamp > 1e12 else float(trade.timestamp))
                    except (ValueError, TypeError):
                        pass

    async def _start_cg_staggered(self, feed: CoinGlassEnhancedFeed, symbol: str):
        """Start CG feed with stagger to spread API load."""
        import random
        _delay = {"BTC": 0, "ETH": 2, "SOL": 4, "XRP": 6}.get(symbol, 0)
        _delay += random.uniform(0, 1)
        await asyncio.sleep(_delay)
        log.info("coinglass.starting_feed", symbol=symbol, delay=f"{_delay:.1f}s")
        await feed.start()

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
        
        G1 & G3: Collect windows for staggered, single-best-signal execution.
        Logs for observability AND forwards to the strategy for evaluation.
        """
        window_state = getattr(window, 'state', None)
        state_value = window_state.value if hasattr(window_state, 'value') else str(window_state) if window_state else 'NO_STATE'
        log.info(
            "five_min.window_signal",
            asset=window.asset,
            window_ts=window.window_ts,
            open_price=window.open_price,
            up_price=window.up_price,
            down_price=window.down_price,
            state=state_value,
        )
        # Forward to strategy — store for token ID lookup
        if self._five_min_strategy:
            self._five_min_strategy._pending_windows.append(window)
            if not hasattr(self._five_min_strategy, '_recent_windows'):
                self._five_min_strategy._recent_windows = []
            self._five_min_strategy._recent_windows.append(window)

            # TWAP: Start tracking on ACTIVE, add price ticks on every signal
            if state_value == "ACTIVE" and window.open_price:
                self._twap_tracker.start_window(
                    asset=window.asset,
                    window_ts=window.window_ts,
                    open_price=window.open_price,
                    duration_s=300.0,
                )
            # Feed non-BTC prices to TWAP from window signals (these arrive every ~1s)
            if window.open_price and window.asset != "BTC":
                # Use up_price as a proxy for current market price direction
                # up_price > 0.50 means market thinks UP, which implies price rose
                _proxy_price = window.open_price  # Fallback
                if window.up_price and window.down_price:
                    # Infer approximate current price from token prices
                    # This is an approximation — better than no ticks at all
                    _up_ratio = window.up_price / (window.up_price + window.down_price) if (window.up_price + window.down_price) > 0 else 0.5
                    # Map token ratio to price delta: ratio 0.55 ≈ +0.05% delta
                    _implied_delta = (_up_ratio - 0.5) * 0.002  # Scale factor
                    _proxy_price = window.open_price * (1 + _implied_delta)
                self._twap_tracker.add_tick(window.asset, window.window_ts, _proxy_price)

            # ONLY evaluate at T-60s (CLOSING state), NOT at window open
            # Window open signal is just for token ID registration
            if state_value == "CLOSING":
                # G1 & G3: Queue window for staggered execution instead of immediate eval
                await self._execution_queue.put((window, self._aggregator))

                # v6.0: Also evaluate with TimesFM-only strategy (parallel, independent)
                if self._timesfm_strategy:
                    asyncio.create_task(self._evaluate_timesfm_window(window))
            else:
                log.info("five_min.skip_evaluation", reason="not_CLOSING_state", state=state_value)
            if len(self._five_min_strategy._recent_windows) > 20:
                self._five_min_strategy._recent_windows = self._five_min_strategy._recent_windows[-20:]

    async def _on_fifteen_min_window(self, window) -> None:
        """Handle 15-minute window signal — same strategy, different timeframe.
        
        G1 & G3: Collect windows for staggered, single-best-signal execution.
        """
        window_state = getattr(window, 'state', None)
        state_value = window_state.value if hasattr(window_state, 'value') else str(window_state) if window_state else 'NO_STATE'
        log.info(
            "fifteen_min.window_signal",
            asset=window.asset,
            window_ts=window.window_ts,
            open_price=window.open_price,
            up_price=window.up_price,
            down_price=window.down_price,
            duration=900,
            state=state_value,
        )
        # Reuse the same 5-min strategy for evaluation + token ID lookup
        if self._five_min_strategy:
            self._five_min_strategy._pending_windows.append(window)
            if not hasattr(self._five_min_strategy, '_recent_windows'):
                self._five_min_strategy._recent_windows = []
            self._five_min_strategy._recent_windows.append(window)

            # TWAP: Start tracking on ACTIVE for 15-min windows
            if state_value == "ACTIVE" and window.open_price:
                self._twap_tracker.start_window(
                    asset=window.asset,
                    window_ts=window.window_ts,
                    open_price=window.open_price,
                    duration_s=900.0,
                )
            # Feed non-BTC prices to TWAP from window signals
            if window.open_price and window.asset != "BTC":
                _proxy_price = window.open_price
                if window.up_price and window.down_price:
                    _up_ratio = window.up_price / (window.up_price + window.down_price) if (window.up_price + window.down_price) > 0 else 0.5
                    _implied_delta = (_up_ratio - 0.5) * 0.002
                    _proxy_price = window.open_price * (1 + _implied_delta)
                self._twap_tracker.add_tick(window.asset, window.window_ts, _proxy_price)

            # ONLY evaluate at T-60s (CLOSING state), NOT at window open
            if state_value == "CLOSING":
                # G1 & G3: Queue window for staggered execution instead of immediate eval
                await self._execution_queue.put((window, self._aggregator))

                # v6.0: Also evaluate with TimesFM-only strategy
                if self._timesfm_strategy:
                    asyncio.create_task(self._evaluate_timesfm_window(window))
            else:
                log.info("fifteen_min.skip_evaluation", reason="not_CLOSING_state", state=state_value)
            if len(self._five_min_strategy._recent_windows) > 20:
                self._five_min_strategy._recent_windows = self._five_min_strategy._recent_windows[-20:]

    # ─── v6.0 TimesFM Window Evaluation ──────────────────────────────────────

    async def _evaluate_timesfm_window(self, window) -> None:
        """
        Evaluate a window with the v6.0 TimesFM-only strategy.
        Runs in parallel with v5.7c — independent paper trades.
        """
        try:
            state = await self._aggregator.get_state()
            # Register window for token ID lookup in TimesFM strategy
            if self._timesfm_strategy:
                self._timesfm_strategy._recent_windows.append(window)
                if len(self._timesfm_strategy._recent_windows) > 20:
                    self._timesfm_strategy._recent_windows = self._timesfm_strategy._recent_windows[-20:]
                await self._timesfm_strategy.evaluate_window(window, state)
        except Exception as exc:
            log.warning("timesfm.evaluate_error", asset=window.asset, error=str(exc))

    # ─── Order Resolution Callback ────────────────────────────────────────────

    def _on_order_resolution(self, order) -> None:
        """Called by OrderManager when an order resolves. Update risk manager + send Telegram alert."""
        if order.pnl_usd is not None:
            filled = order.metadata.get("filled", False) if order.metadata else False
            is_paper_mode = self._settings.paper_mode
            
            log.info(
                "resolution.callback",
                order_id=order.order_id[:20],
                outcome=order.outcome,
                pnl=f"${order.pnl_usd:.2f}",
                filled=filled,
                paper=is_paper_mode,
                will_alert=filled or is_paper_mode,
            )
            
            # Update risk manager with PnL (bankroll, daily PnL, drawdown, consecutive losses)
            async def _record_and_alert():
                try:
                    await self._risk_manager.record_outcome(order.pnl_usd)
                    log.info("resolution.pnl_recorded", order_id=order.order_id[:20], pnl=f"${order.pnl_usd:.2f}")
                except Exception as exc:
                    log.error("resolution.pnl_record_failed", error=str(exc))
                
                if filled or is_paper_mode:
                    try:
                        cg_snap = self._cg_enhanced.snapshot if self._cg_enhanced else None
                        await self._alerter.send_trade_alert(order, cg_snapshot=cg_snap)
                        log.info("resolution.alert_sent", order_id=order.order_id[:20])
                    except Exception as exc:
                        log.error("resolution.alert_failed", order_id=order.order_id[:20], error=str(exc))
            
            asyncio.create_task(_record_and_alert())

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
                    if not self._settings.paper_mode:
                        # Live mode: sync from real Polymarket wallet (cash only)
                        try:
                            _cached_wallet_balance = await self._poly_client.get_balance()
                            await self._risk_manager.sync_bankroll(_cached_wallet_balance)
                        except Exception as exc:
                            log.debug("heartbeat.wallet_balance_error", error=str(exc))
                    else:
                        # Paper mode: use risk manager's tracked bankroll for sitrep
                        _cached_wallet_balance = risk_status.get("current_bankroll", 0)

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
                        try:
                            vpin = state.vpin.value if state.vpin else 0
                        except Exception:
                            vpin = 0
                        try:
                            regime = self._regime.current_regime
                        except Exception:
                            regime = "UNKNOWN"
                        try:
                            binance_ok = self._binance_feed.connected
                        except Exception:
                            binance_ok = False

                        daily_sign = "+" if daily_pnl >= 0 else ""
                        status_emoji = "🛑" if killed else "🟢"

                        # Fetch position outcomes
                        real_wins = 0
                        real_losses = 0
                        open_positions_val = 0

                        if not self._settings.paper_mode:
                            # Live mode: fetch real Polymarket position outcomes
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
                        else:
                            # Paper mode: use internal order manager counts
                            try:
                                for oid, o in self._order_manager._orders.items():
                                    if o.outcome == "WIN":
                                        real_wins += 1
                                    elif o.outcome == "LOSS":
                                        real_losses += 1
                                    elif o.status.value in ("OPEN", "FILLED"):
                                        open_positions_val += o.stake_usd
                            except Exception:
                                pass

                        portfolio = wallet + open_positions_val
                        # Use starting bankroll as baseline (not hardcoded $208.98)
                        baseline = runtime.starting_bankroll if self._settings.paper_mode else 208.98
                        real_pnl = portfolio - baseline
                        mode_label = "📄 PAPER" if self._settings.paper_mode else "💰 LIVE"

                        # Build CoinGlass block for sitrep
                        cg_block = ""
                        try:
                            cg_snapshot = self._cg_enhanced.snapshot if self._cg_enhanced is not None else None
                            cg_block = self._alerter.format_coinglass_block(cg_snapshot) + "\n"
                        except Exception:
                            pass

                        sitrep = (
                            f"📋 *5-MIN SITREP* ({status_emoji} {'KILLED' if killed else 'ACTIVE'}) {mode_label}\n"
                            f"\n"
                            f"🏦 Cash: `${wallet:.2f}`{' USDC' if not self._settings.paper_mode else ''}\n"
                            f"📊 Positions: `${open_positions_val:.2f}`\n"
                            f"💰 Portfolio: `${portfolio:.2f}`\n"
                            f"📈 P&L: `${real_pnl:+.2f}` (from ${baseline:.0f})\n"
                            f"\n"
                            f"✅ Wins: `{real_wins}` | ❌ Losses: `{real_losses}`\n"
                            f"📉 Drawdown: `{drawdown:.1%}`\n"
                            f"\n"
                            f"🔬 VPIN: `{vpin:.4f}` | Vol: `{regime}` | Trade: `{'CASCADE' if vpin >= 0.65 else ('TRANSITION' if vpin >= 0.55 else 'NORMAL' if vpin >= 0.45 else 'CALM')}`\n"
                            + cg_block +
                            f"🔗 Binance: `{'✅' if binance_ok else '❌'}` | BTC: `${self._order_manager._current_btc_price:,.2f}`\n"
                        )

                        await self._alerter.send_system_alert(sitrep, level="info")
                        log.info("sitrep.sent")
                    except Exception as exc:
                        log.warning("sitrep.failed", error=str(exc))

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

    # ── Builder Relayer Redeemer Loop ───────────────────────────────────────

    async def _redeemer_loop(self) -> None:
        """Auto-redeem settled positions every 5 minutes via Builder Relayer."""
        REDEEM_INTERVAL = 300
        while not self._shutdown_event.is_set():
            try:
                # Check for manual redeem request from Hub
                try:
                    if await self._db.check_redeem_requested():
                        log.info("orchestrator.manual_redeem_triggered")
                except Exception:
                    pass

                result = await self._redeemer.redeem_all()

                if result.get("redeemed", 0) > 0:
                    try:
                        await self._db.write_redeem_event(result)
                    except Exception:
                        pass
                    try:
                        await self._alerter.send_redeem_alert(result)
                    except Exception:
                        pass

            except Exception as e:
                log.error("orchestrator.redeemer_loop.error", error=str(e))

            try:
                await asyncio.wait_for(
                    asyncio.shield(self._shutdown_event.wait()),
                    timeout=REDEEM_INTERVAL,
                )
                break
            except asyncio.TimeoutError:
                pass

    # ── Playwright Loops ────────────────────────────────────────────────────

    async def _playwright_redeem_loop(self) -> None:
        """Auto-redeem settled positions every 5 minutes via Playwright."""
        REDEEM_INTERVAL = 300
        while not self._shutdown_event.is_set():
            try:
                # Check for manual redeem request from Hub
                if await self._db.check_redeem_requested():
                    log.info("orchestrator.manual_redeem_triggered")

                result = await self._playwright.redeem_all()

                if result["redeemed"] > 0:
                    await self._db.write_redeem_event(result)
                    msg = (
                        f"Redeemed {result['redeemed']} position(s) "
                        f"via Playwright"
                    )
                    if result["failed"] > 0:
                        msg += f" ({result['failed']} failed)"
                    await self._alerter.send_system_alert(msg, level="info")

            except Exception as e:
                log.error("orchestrator.playwright_redeem.error", error=str(e))

            try:
                await asyncio.wait_for(
                    asyncio.shield(self._shutdown_event.wait()),
                    timeout=REDEEM_INTERVAL,
                )
                break
            except asyncio.TimeoutError:
                pass

    async def _playwright_balance_loop(self) -> None:
        """Poll account balance every 60 seconds via Playwright."""
        BALANCE_INTERVAL = 60
        while not self._shutdown_event.is_set():
            try:
                balance = await self._playwright.get_portfolio_balance()
                positions = await self._playwright.get_positions()
                redeemable = await self._playwright.get_redeemable()
                history = await self._playwright.get_order_history(limit=50)

                await self._db.update_playwright_state(
                    logged_in=self._playwright._logged_in,
                    browser_alive=self._playwright._browser_alive,
                    usdc_balance=balance.get("usdc", 0.0),
                    positions_value=balance.get("positions_value", 0.0),
                    positions_json=positions,
                    redeemable_json=redeemable,
                    history_json=history,
                )

            except Exception as e:
                log.error("orchestrator.playwright_balance.error", error=str(e))

            try:
                await asyncio.wait_for(
                    asyncio.shield(self._shutdown_event.wait()),
                    timeout=BALANCE_INTERVAL,
                )
                break
            except asyncio.TimeoutError:
                pass

    async def _playwright_screenshot_loop(self) -> None:
        """Capture browser screenshot every 30 seconds."""
        SCREENSHOT_INTERVAL = 30
        while not self._shutdown_event.is_set():
            try:
                screenshot = await self._playwright.screenshot()
                if screenshot:
                    await self._db.update_playwright_state(
                        logged_in=self._playwright._logged_in,
                        browser_alive=self._playwright._browser_alive,
                        usdc_balance=0,
                        positions_value=0,
                        screenshot_png=screenshot,
                    )
            except Exception as e:
                log.error("orchestrator.playwright_screenshot.error", error=str(e))

            try:
                await asyncio.wait_for(
                    asyncio.shield(self._shutdown_event.wait()),
                    timeout=SCREENSHOT_INTERVAL,
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

    async def _position_monitor_loop(self) -> None:
        """Every 30s: check Polymarket positions API for resolved trades.
        
        SOURCE OF TRUTH for WIN/LOSS — uses Polymarket's oracle, not internal logic.
        Only alerts on NEW resolutions (ignores positions resolved before engine started).
        """
        POLL_INTERVAL = 30
        _resolved_conditions: set = set()
        _first_run = True
        _start_time = time.time()
        
        while not self._shutdown_event.is_set():
            try:
                outcomes = await self._poly_client.get_position_outcomes()
                
                if _first_run:
                    # On first run, mark ALL currently resolved positions as "known"
                    # so we don't spam alerts for old historical positions
                    for cid, data in outcomes.items():
                        if data["outcome"] != "OPEN":
                            _resolved_conditions.add(cid)
                    _first_run = False
                    log.info("position_monitor.started", known_resolved=len(_resolved_conditions))
                    # Continue to next poll — don't alert on existing positions
                else:
                    for cid, data in outcomes.items():
                        if cid in _resolved_conditions:
                            continue
                        
                        outcome = data["outcome"]
                        if outcome == "OPEN":
                            continue
                        
                        # NEW resolution detected
                        _resolved_conditions.add(cid)
                        
                        size = data["size"]
                        avg_price = data["avgPrice"]
                        cost = data["cost"]
                        value = data["value"]
                        pnl = data["pnl"]
                        
                        from datetime import datetime, timezone
                        now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
                        
                        if outcome == "WIN":
                            emoji = "✅"
                            pnl_str = f"+${pnl:.2f}"
                        else:
                            emoji = "❌"
                            pnl_str = f"-${cost:.2f}"
                        
                        # Try to find matching trade in DB for signal details
                        trade_info = ""
                        try:
                            if self._db._pool:
                                async with self._db._pool.acquire() as conn:
                                    row = await conn.fetchrow(
                                        """SELECT metadata, created_at FROM trades 
                                           WHERE mode = 'live' AND stake_usd BETWEEN $1 AND $2
                                           ORDER BY created_at DESC LIMIT 1""",
                                        cost * 0.8, cost * 1.2,
                                    )
                                    if row and row["metadata"]:
                                        import json as _json
                                        meta = _json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
                                        tier = meta.get("tier", "")
                                        conf = meta.get("confidence", "")
                                        delta = meta.get("delta_pct", "")
                                        entry_reason = meta.get("entry_reason", "")
                                        if tier or conf:
                                            conf_str = f"{int(conf*100)}%" if isinstance(conf, float) else str(conf)
                                            trade_info = (
                                                f"\n📊 *Signal*\n"
                                                f"Tier: `{tier}`\n"
                                                f"Confidence: `{conf_str}`\n"
                                                f"Delta: `{delta}`\n"
                                            )
                                            if entry_reason:
                                                trade_info += f"Entry: `{entry_reason[:60]}`\n"
                        except Exception:
                            pass
                        
                        # Get wallet balance
                        try:
                            wallet = await self._poly_client.get_balance()
                            wallet_str = f"\n🏦 Wallet: `${wallet:.2f}` USDC"
                        except Exception:
                            wallet_str = ""
                        
                        msg = (
                            f"{emoji} *{outcome} — BTC* (💰 LIVE)\n"
                            f"🕐 `{now_str}`\n"
                            f"\n"
                            f"📊 *Result (from Polymarket)*\n"
                            f"Shares: `{size:.1f}`\n"
                            f"Entry: `${avg_price:.4f}`\n"
                            f"Cost: `${cost:.2f}`\n"
                            f"Payout: `${value:.2f}`\n"
                            f"PnL: `{pnl_str}`\n"
                            f"{trade_info}"
                            f"{wallet_str}"
                        )
                        
                        await self._alerter.send_system_alert(msg, level="info")
                        log.info(
                            "position_monitor.resolved",
                            condition_id=cid[:20] + "...",
                            outcome=outcome,
                            pnl=pnl,
                        )
                
            except Exception as exc:
                log.debug("position_monitor.error", error=str(exc))
            
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._shutdown_event.wait()),
                    timeout=POLL_INTERVAL,
                )
                break
            except asyncio.TimeoutError:
                pass

    # ── G1 & G3: Staggered Execution + Single Best Signal ────────────────────

    async def _staggered_execution_loop(self) -> None:
        """
        G1 & G3: Process queued window signals with staggered execution.
        
        - Collects all pending windows for the same time period
        - If G3 enabled (single_best_signal), picks only the highest-scoring asset
        - If G3 disabled, keeps all assets
        - Executes them sequentially with configurable gaps (G1)
        
        Gap: ORDER_STAGGER_SECONDS (default 5) + 1-3s random jitter
        """
        import random
        
        while not self._shutdown_event.is_set():
            try:
                # Wait for first window, with timeout to avoid hanging
                try:
                    window, agg_ref = await asyncio.wait_for(
                        self._execution_queue.get(),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    # No pending windows — continue loop
                    continue
                
                # Collect all windows for the same period (within 2 seconds)
                windows_batch = [(window, agg_ref)]
                batch_start_time = time.time()
                
                while time.time() - batch_start_time < 2.0:
                    try:
                        next_window, next_agg_ref = await asyncio.wait_for(
                            self._execution_queue.get(),
                            timeout=0.5,
                        )
                        windows_batch.append((next_window, next_agg_ref))
                    except asyncio.TimeoutError:
                        break
                
                # G3: Single best signal mode — pick only the top-scoring window
                if runtime.single_best_signal and len(windows_batch) > 1:
                    # Score each window: abs(delta_pct) * current_vpin
                    scored_windows = []
                    for w, agg_ref in windows_batch:
                        try:
                            state = await agg_ref.get_state()
                            current_price = float(state.btc_price) if state.btc_price else None
                            if current_price is None or w.open_price is None:
                                continue
                            delta_pct = (current_price - w.open_price) / w.open_price * 100
                            current_vpin = self._vpin_calc.current_vpin
                            score = abs(delta_pct) * current_vpin
                            scored_windows.append((score, w, agg_ref))
                        except Exception:
                            continue
                    
                    if scored_windows:
                        # Sort by score descending, pick top
                        scored_windows.sort(key=lambda x: x[0], reverse=True)
                        top_score, top_window, top_agg_ref = scored_windows[0]
                        windows_batch = [(top_window, top_agg_ref)]
                        log.info(
                            "guardrail.single_best_signal",
                            selected_asset=top_window.asset,
                            score=f"{top_score:.4f}",
                            skipped=len(scored_windows) - 1,
                        )
                
                # Execute windows sequentially with staggered gaps
                for idx, (w, agg_ref) in enumerate(windows_batch):
                    if self._shutdown_event.is_set():
                        break
                    
                    if idx > 0:
                        # G1: Stagger execution with jitter
                        stagger_delay = runtime.order_stagger_seconds
                        jitter = random.uniform(1.0, 3.0)  # 1-3s random jitter
                        total_delay = stagger_delay + jitter
                        log.info(
                            "guardrail.staggered_execution.wait",
                            asset=w.asset,
                            window_ts=w.window_ts,
                            delay=f"{total_delay:.2f}s",
                            base=f"{stagger_delay:.1f}s",
                            jitter=f"{jitter:.2f}s",
                        )
                        await asyncio.sleep(total_delay)
                    
                    # Evaluate and execute
                    try:
                        state = await agg_ref.get_state()
                        await self._five_min_strategy._evaluate_window(w, state)
                        log.info(
                            "guardrail.staggered_execution.evaluated",
                            asset=w.asset,
                            window_ts=w.window_ts,
                            position=f"{idx + 1}/{len(windows_batch)}",
                        )
                    except Exception as exc:
                        log.warning(
                            "guardrail.staggered_execution.eval_error",
                            asset=w.asset,
                            error=str(exc),
                        )
            
            except Exception as exc:
                log.error("orchestrator.staggered_execution_error", error=str(exc))
            
            # Small yield to prevent busy loop
            await asyncio.sleep(0.1)
