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
from pathlib import Path
from typing import Optional

import structlog

from alerts.telegram import TelegramAlerter
from config.runtime_config import runtime
from config.settings import Settings
from config.constants import FIVE_MIN_ENTRY_OFFSET
from data.aggregator import MarketAggregator
from data.feeds.binance_ws import BinanceWebSocketFeed
from data.feeds.chainlink_rpc import ChainlinkRPCFeed
from data.feeds.chainlink_feed import ChainlinkFeed
from data.feeds.tiingo_feed import TiingoFeed
from data.feeds.clob_feed import CLOBFeed
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
from persistence.tick_recorder import TickRecorder
from signals.arb_scanner import ArbScanner
from signals.cascade_detector import CascadeDetector
from signals.regime_classifier import RegimeClassifier
from signals.vpin import VPINCalculator
from strategies.sub_dollar_arb import SubDollarArbStrategy
from strategies.vpin_cascade import VPINCascadeStrategy
from strategies.five_min_vpin import FiveMinVPINStrategy
from strategies.timesfm_only import TimesFMOnlyStrategy
from strategies.timesfm_multi_entry import TimesFMMultiEntryStrategy
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
        self._timesfm_multi: Optional[TimesFMMultiEntryStrategy] = None

        log.info("orchestrator.init", paper_mode=settings.paper_mode)

        # ── Persistence ────────────────────────────────────────────────────────
        self._db = DBClient(settings=settings)
        # TickRecorder is wired to the pool after connect() in start()
        self._tick_recorder: Optional[TickRecorder] = None

        # ── Aggregator ─────────────────────────────────────────────────────────
        self._aggregator = MarketAggregator()

        # ── Alerts ─────────────────────────────────────────────────────────────
        self._alerter = TelegramAlerter(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            alerts_paper=settings.telegram_alerts_paper,
            alerts_live=settings.telegram_alerts_live,
            paper_mode=settings.paper_mode,
            anthropic_api_key=settings.anthropic_api_key,  # fix: pass key explicitly (pydantic doesn't inject into os.environ)
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
        self._alerter.set_location("MTL", self._settings.engine_version if hasattr(self._settings, "engine_version") else "v7.1")

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
        # Read from os.environ first, then .env file as fallback
        timesfm_enabled = os.environ.get("TIMESFM_ENABLED", "").lower() == "true"
        if not timesfm_enabled:
            # Fallback: read .env file directly if env var not set
            env_file = Path(__file__).parent.parent / ".env"
            if env_file.exists():
                with open(env_file) as f:
                    for line in f:
                        if line.startswith("TIMESFM_ENABLED="):
                            timesfm_enabled = line.split("=", 1)[1].strip().lower() == "true"
                            break
        
        timesfm_url = os.environ.get("TIMESFM_URL")
        if not timesfm_url:
            env_file = Path(__file__).parent.parent / ".env"
            if env_file.exists():
                with open(env_file) as f:
                    for line in f:
                        if line.startswith("TIMESFM_URL="):
                            timesfm_url = line.split("=", 1)[1].strip()
                            break
        timesfm_url = timesfm_url or "http://3.98.114.0:8080"
        
        timesfm_min_conf_str = os.environ.get("TIMESFM_MIN_CONFIDENCE")
        if not timesfm_min_conf_str:
            env_file = Path(__file__).parent.parent / ".env"
            if env_file.exists():
                with open(env_file) as f:
                    for line in f:
                        if line.startswith("TIMESFM_MIN_CONFIDENCE="):
                            timesfm_min_conf_str = line.split("=", 1)[1].strip()
                            break
        timesfm_min_conf = float(timesfm_min_conf_str or "0.30")

        if timesfm_enabled:
            self._timesfm_client = TimesFMClient(
                base_url=timesfm_url,
                timeout_seconds=10.0,
            )
            # v5.8: Only the CLIENT is created. No standalone strategies.
            # TimesFM is used ONLY as an agreement signal inside v5.7c.
            log.info(
                "orchestrator.timesfm_v58_mode",
                url=timesfm_url,
                min_confidence=timesfm_min_conf,
                mode="agreement_only",
                note="TimesFM used as v5.8 agreement signal, not standalone",
            )
        else:
            log.info("orchestrator.timesfm_v6_disabled")

        # v5.8: Inject TimesFM client into five_min_strategy (created before client was initialized)
        if self._timesfm_client and self._five_min_strategy:
            self._five_min_strategy._timesfm = self._timesfm_client
            log.info("orchestrator.timesfm_injected_into_five_min")

        # TickRecorder is not yet available at __init__ (pool not connected)
        # It is injected in start() after pool is live.

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

        # ── Chainlink Multi-Asset Feed (BTC/ETH/SOL/XRP, every 5s) ──────────
        # Pool not yet available at __init__ — injected in start() after connect()
        self._chainlink_multi_feed: Optional[ChainlinkFeed] = None
        # Instantiated in start() once DB pool is live

        # ── Tiingo Top-of-Book Feed (BTC/ETH/SOL/XRP, every 2s) ─────────────
        # Pool not yet available at __init__ — injected in start() after connect()
        self._tiingo_feed: Optional[TiingoFeed] = None
        self._clob_feed: Optional[CLOBFeed] = None
        # Instantiated in start() once DB pool is live

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
            # Wire DB to alerter for notification logging
            self._alerter.set_db_client(self._db)
        except Exception as exc:
            log.error("orchestrator.db_connect_failed", error=str(exc))
            raise

        # Ensure window_snapshots table exists (non-fatal if it fails)
        try:
            await self._db.ensure_window_tables()
        except Exception as exc:
            log.warning("orchestrator.ensure_window_tables_failed", error=str(exc))

        # ── TickRecorder: initialise now that pool is live ──────────────────
        try:
            self._tick_recorder = TickRecorder(pool=self._db._pool)
            await self._tick_recorder.ensure_tables()
            await self._tick_recorder.start()
            # Inject into five_min_strategy so it can record TimesFM forecasts
            if self._five_min_strategy:
                self._five_min_strategy._tick_recorder = self._tick_recorder
            log.info("orchestrator.tick_recorder_started")
        except Exception as exc:
            log.warning("orchestrator.tick_recorder_start_failed", error=str(exc))
            self._tick_recorder = None

        # ── Chainlink Multi-Asset Feed — initialise with live DB pool ────────
        try:
            _rpc_url = self._settings.polygon_rpc_url
            if _rpc_url and self._db._pool:
                self._chainlink_multi_feed = ChainlinkFeed(
                    rpc_url=_rpc_url,
                    pool=self._db._pool,
                )
                log.info("orchestrator.chainlink_multi_feed_ready", rpc=_rpc_url[:40])
            else:
                log.info(
                    "orchestrator.chainlink_multi_feed_disabled",
                    reason="no RPC URL or DB pool",
                )
        except Exception as exc:
            log.warning("orchestrator.chainlink_multi_feed_init_failed", error=str(exc))
            self._chainlink_multi_feed = None

        # ── Tiingo Feed — initialise with live DB pool ────────────────────────
        try:
            _tiingo_key = os.environ.get("TIINGO_API_KEY", "")
            if not _tiingo_key:
                # Fallback: read from .env file
                _env_file = Path(__file__).parent.parent / ".env"
                if _env_file.exists():
                    with open(_env_file) as _f:
                        for _line in _f:
                            if _line.startswith("TIINGO_API_KEY="):
                                _tiingo_key = _line.split("=", 1)[1].strip()
                                break
            if _tiingo_key and self._db._pool:
                self._tiingo_feed = TiingoFeed(
                    api_key=_tiingo_key,
                    pool=self._db._pool,
                )
                log.info("orchestrator.tiingo_feed_ready")
            else:
                log.info(
                    "orchestrator.tiingo_feed_disabled",
                    reason="no API key or DB pool",
                )
        except Exception as exc:
            log.warning("orchestrator.tiingo_feed_init_failed", error=str(exc))
            self._tiingo_feed = None

        # ── CLOB Book Feed (real Polymarket bid/ask, every 10s) ──────────────
        try:
            if self._order_manager and self._db and self._db._pool:
                _poly_client = getattr(self._order_manager, '_poly', None)
                _five_min_feed = getattr(self, '_strategy', None)
                if _poly_client:
                    self._clob_feed = CLOBFeed(
                        poly_client=_poly_client,
                        db_pool=self._db._pool,
                        polymarket_feed=self._five_min_feed,
                    )
                    log.info("orchestrator.clob_feed_ready")
                else:
                    log.info("orchestrator.clob_feed_disabled", reason="no poly client")
        except Exception as exc:
            log.warning("orchestrator.clob_feed_init_failed", error=str(exc))
            self._clob_feed = None

        # ── VPIN Warm Start: replay recent ticks to avoid cold-start ──────────
        try:
            if self._db and self._db._pool:
                ticks = await self._vpin_calc.warm_start(self._db._pool)
                if ticks > 0:
                    log.info("orchestrator.vpin_warm_start", ticks=ticks, vpin=f"{self._vpin_calc.current_vpin:.4f}")
        except Exception as exc:
            log.warning("orchestrator.vpin_warm_start_failed", error=str(exc))

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
        
        # Start multi-entry TimesFM strategy
        if self._timesfm_multi:
            self._timesfm_multi.set_aggregator(self._aggregator)
            await self._timesfm_multi.start()

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
        if self._chainlink_multi_feed:
            self._tasks.append(
                asyncio.create_task(
                    self._chainlink_multi_feed.start(), name="feed:chainlink_multi"
                )
            )
            log.info("orchestrator.chainlink_multi_feed_started")
        if self._tiingo_feed:
            self._tasks.append(
                asyncio.create_task(self._tiingo_feed.start(), name="feed:tiingo")
            )
            log.info("orchestrator.tiingo_feed_started")
        if self._clob_feed:
            self._tasks.append(
                asyncio.create_task(self._clob_feed.start(), name="feed:clob")
            )
            log.info("orchestrator.clob_feed_started")
        self._tasks.append(
            asyncio.create_task(self._polymarket_feed.start(), name="feed:polymarket")
        )

        # 5a. CoinGlass snapshot recorder (every 10s)
        if self._tick_recorder and self._cg_feeds:
            self._tasks.append(
                asyncio.create_task(
                    self._coinglass_snapshot_recorder_loop(),
                    name="tick_recorder:coinglass",
                )
            )

        # 5b. TimesFM 1-second forecast recorder
        if self._tick_recorder and self._timesfm_client:
            self._tasks.append(
                asyncio.create_task(
                    self._timesfm_forecast_recorder_loop(),
                    name="tick_recorder:timesfm_1s",
                )
            )

        # 5. Heartbeat task (every 10s)
        self._tasks.append(
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat")
        )

        # 6. Resolution polling task (every 5s)
        self._tasks.append(
            asyncio.create_task(self._resolution_loop(), name="resolution_poller")
        )

        # 6b. Polymarket reconciliation loop (every 5 min) — live mode only
        if not self._settings.paper_mode:
            self._tasks.append(
                asyncio.create_task(
                    self._polymarket_reconcile_loop(), name="polymarket_reconciler"
                )
            )

        # 7. Market state fan-out loop
        self._tasks.append(
            asyncio.create_task(self._market_state_loop(), name="market_state_loop")
        )

        # 7b. Manual trade queue poller (v5.8 dashboard live trades)
        self._tasks.append(
            asyncio.create_task(self._manual_trade_poller(), name="manual_trade_poller")
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
        if self._timesfm_multi:
            await self._timesfm_multi.stop()
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
        if self._chainlink_multi_feed:
            await self._chainlink_multi_feed.stop()
        if self._tiingo_feed:
            await self._tiingo_feed.stop()
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

        # Stop TickRecorder
        if self._tick_recorder:
            try:
                await self._tick_recorder.stop()
            except Exception as exc:
                log.warning("orchestrator.tick_recorder_stop_error", error=str(exc))

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

        # ── TickRecorder: buffer this tick (non-blocking) ─────────────────
        if self._tick_recorder:
            self._tick_recorder.record_binance_tick(
                trade, vpin=self._vpin_calc.current_vpin
            )

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
                        # Convert trade_time (datetime) to unix timestamp
                        ts = trade.trade_time.timestamp() if hasattr(trade.trade_time, 'timestamp') else time.time()
                        self._twap_tracker.add_tick("BTC", wts, float(trade.price), ts)
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

    async def _coinglass_snapshot_recorder_loop(self) -> None:
        """Every 10s: record CoinGlass snapshots for all assets to ticks_coinglass."""
        while not self._shutdown_event.is_set():
            try:
                for asset, feed in self._cg_feeds.items():
                    snap = feed.snapshot
                    if snap and snap.connected:
                        asyncio.create_task(
                            self._tick_recorder.record_coinglass_snapshot(asset, snap)
                        )
            except Exception as exc:
                log.debug("tick_recorder.coinglass_loop.error", error=str(exc))
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._shutdown_event.wait()),
                    timeout=10.0,
                )
                break
            except asyncio.TimeoutError:
                pass

    async def _timesfm_forecast_recorder_loop(self) -> None:
        """Every 1s: fetch TimesFM forecast with window-relative horizon and record."""
        import math
        while not self._shutdown_event.is_set():
            try:
                now = time.time()
                # Calculate current 5-min window context
                window_ts = int(now // 300) * 300  # current window open (aligned to 300s)
                window_close_ts = window_ts + 300
                seconds_to_close = max(1, int(window_close_ts - now))

                # Fetch with window-specific horizon
                forecast = await self._timesfm_client.get_forecast(
                    seconds_to_close=seconds_to_close,
                )
                if forecast and not forecast.error:
                    asyncio.create_task(
                        self._tick_recorder.record_timesfm_forecast(
                            forecast,
                            asset="BTC",
                            window_ts=window_ts,
                            window_close_ts=window_close_ts,
                            seconds_to_close=seconds_to_close,
                        )
                    )
            except Exception as exc:
                log.debug("tick_recorder.timesfm_1s_loop.error", error=str(exc))
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._shutdown_event.wait()),
                    timeout=1.0,
                )
                break
            except asyncio.TimeoutError:
                pass

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
        # ── TickRecorder: record Gamma prices on every window signal ─────────
        if self._tick_recorder and window.up_price is not None:
            asyncio.create_task(self._tick_recorder.record_gamma_price(window))

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
                # v5.8: No standalone multi-entry — TimesFM is agreement-only
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

            # ── Window-centric notification system (v7.2) ────────────────
            # Each window gets: OPEN card → T-240/180/120/90 snapshots →
            # T-60 trade decision → RESOLUTION with what-if table.
            # All messages tagged with window_id + location + version.
            if state_value == "ACTIVE" and window.open_price:
                import time as _time
                _elapsed = _time.time() - window.window_ts
                _remaining = window.duration_secs - _elapsed
                _wkey = f"{window.asset}-{window.window_ts}"
                _window_id = f"{window.asset}-{window.window_ts}"
                _tf = "15m" if window.duration_secs == 900 else "5m"

                # Track which stages we've sent for this window
                if not hasattr(self, '_countdown_sent'):
                    self._countdown_sent = {}
                if _wkey not in self._countdown_sent:
                    self._countdown_sent[_wkey] = set()
                    # WINDOW OPEN card — fires once at T-300 (window just opened)
                    if self._alerter and _remaining >= 270:
                        asyncio.create_task(self._alerter.send_window_open(
                            window_id=_window_id,
                            asset=window.asset,
                            timeframe=_tf,
                            open_price=window.open_price,
                            gamma_up=window.up_price or 0.50,
                            gamma_down=window.down_price or 0.50,
                        ))

                log.info("five_min.countdown_check", remaining=f"{_remaining:.1f}s",
                         sent=str(self._countdown_sent.get(_wkey, set())))

                # ── Helper: get full signal snapshot ────────────────────
                async def _get_full_snapshot(t_label: str, elapsed: int):
                    _vpin = self._five_min_strategy._vpin.current_vpin if self._five_min_strategy._vpin else 0.0
                    _btc = float(self._aggregator._state.btc_price) if self._aggregator._state.btc_price else 0.0
                    _d = (_btc - window.open_price) / window.open_price * 100 if window.open_price and _btc else 0.0
                    _regime = ("CASCADE" if _vpin >= 0.65 else "TRANSITION" if _vpin >= 0.55
                               else "NORMAL" if _vpin >= 0.45 else "CALM")
                    # TimesFM
                    _tsf_dir, _tsf_conf, _tsf_pred = None, 0.0, 0.0
                    if self._five_min_strategy._timesfm:
                        try:
                            _secs = max(1, int((window.window_ts + window.duration_secs) - _time.time()))
                            _tsf = await self._five_min_strategy._timesfm.get_forecast(
                                open_price=window.open_price, seconds_to_close=_secs)
                            if _tsf and not _tsf.error:
                                _tsf_dir = _tsf.direction
                                _tsf_conf = _tsf.confidence
                                _tsf_pred = _tsf.predicted_close
                        except Exception:
                            pass
                    # TWAP
                    _tw_dir, _tw_agree = None, 0
                    if self._twap_tracker:
                        _tw = self._twap_tracker.evaluate(
                            asset=window.asset, window_ts=window.window_ts,
                            current_price=_btc,
                            gamma_up_price=window.up_price, gamma_down_price=window.down_price,
                        )
                        if _tw:
                            _tw_dir = _tw.recommended_direction
                            _tw_agree = _tw.agreement_score
                    # CoinGlass
                    _cg_taker = 50.0
                    _cg_fund = 0.0
                    try:
                        _cg = self._cg_enhanced.snapshot if self._cg_enhanced else None
                        if _cg and _cg.connected:
                            _tot = _cg.taker_buy_volume_1m + _cg.taker_sell_volume_1m
                            if _tot > 0:
                                _cg_taker = _cg.taker_buy_volume_1m / _tot * 100
                            _cg_fund = _cg.funding_rate * 3 * 365
                    except Exception:
                        pass
                    # Collect price ticks from TWAP tracker
                    _ticks = []
                    try:
                        _tw_state = self._twap_tracker._windows.get(
                            f"{window.asset}-{window.window_ts}", None)
                        if _tw_state and hasattr(_tw_state, 'ticks'):
                            _ticks = [t[1] for t in _tw_state.ticks[-300:]]
                    except Exception:
                        _ticks = [window.open_price, _btc]
                    # AI commentary (non-blocking, best-effort)
                    _ai = None
                    if self._alerter._anthropic_api_key:
                        try:
                            _prompt = (
                                f"BTC 5m window {t_label}: "
                                f"VPIN {_vpin:.3f} ({_regime}), delta {_d:+.4f}%, "
                                f"TWAP {_tw_dir or '?'} {_tw_agree}/3, "
                                f"TimesFM {_tsf_dir or '?'} {_tsf_conf:.0%}, "
                                f"CG taker {_cg_taker:.0f}% buy, funding {_cg_fund:.0f}%/yr. "
                                f"1 sentence: what does this signal suggest about the next {int(_remaining):.0f}s?"
                            )
                            import aiohttp as _ah
                            async with _ah.ClientSession() as _sess:
                                async with _sess.post(
                                    "https://api.anthropic.com/v1/messages",
                                    json={"model": "claude-haiku-4-5", "max_tokens": 80,
                                          "messages": [{"role": "user", "content": _prompt}]},
                                    headers={"x-api-key": self._alerter._anthropic_api_key,
                                             "anthropic-version": "2023-06-01"},
                                    timeout=_ah.ClientTimeout(total=8),
                                ) as _r:
                                    if _r.status == 200:
                                        _d2 = await _r.json()
                                        _ai = _d2.get("content", [{}])[0].get("text", "").strip()
                        except Exception:
                            pass
                    return _vpin, _btc, _d, _regime, _tsf_dir, _tsf_conf, _tsf_pred, _tw_dir, _tw_agree, _cg_taker, _cg_fund, _ticks, _ai

                # Snapshot windows: T-240, T-210, T-180, T-150, T-120, T-90, T-70
                _snapshot_windows = [
                    ("T-240", 242, 220),
                    ("T-210", 212, 190),
                    ("T-180", 182, 160),
                    ("T-150", 152, 130),
                    ("T-120", 122, 100),
                    ("T-90",   92,  70),
                    ("T-70",   72,  55),
                ]
                for _t_lbl, _hi, _lo in _snapshot_windows:
                    if _remaining <= _hi and _remaining >= _lo and _t_lbl not in self._countdown_sent[_wkey]:
                        self._countdown_sent[_wkey].add(_t_lbl)
                        _elapsed_now = int(window.duration_secs - _remaining)
                        (_vpin, _btc, _d, _regime, _tsf_dir, _tsf_conf, _tsf_pred,
                         _tw_dir, _tw_agree, _cg_taker, _cg_fund, _ticks, _ai) = await _get_full_snapshot(_t_lbl, _elapsed_now)

                        # ── Fetch fresh Gamma prices for this stage ──────────
                        _snap_gamma_up = window.up_price or 0.50
                        _snap_gamma_down = window.down_price or 0.50
                        try:
                            _slug = f"{window.asset.lower()}-updown-{_tf}-{window.window_ts}"
                            import aiohttp as _ah_gamma
                            async with _ah_gamma.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as _gs:
                                async with _gs.get(
                                    f"https://gamma-api.polymarket.com/events?slug={_slug}",
                                    timeout=_ah_gamma.ClientTimeout(total=5),
                                ) as _gr:
                                    if _gr.status == 200:
                                        _gd = await _gr.json()
                                        if _gd and isinstance(_gd, list) and _gd[0].get("markets"):
                                            _gm = _gd[0]["markets"][0]
                                            _ba = _gm.get("bestAsk")
                                            if _ba is not None:
                                                _snap_gamma_up = round(float(_ba), 4)
                                                _snap_gamma_down = round(1.0 - _snap_gamma_up, 4)
                        except Exception:
                            pass

                        # ── Write snapshot to countdown_evaluations DB ───────
                        try:
                            _twap_agree_bool = (_tw_agree >= 2) if _tw_agree is not None else None
                            # v7.2: fetch multi-source prices for countdown record
                            _cl_price = None
                            _ti_price = None
                            if self._db:
                                try:
                                    _cl_price = await self._db.get_latest_chainlink_price(window.asset)
                                except Exception:
                                    pass
                                try:
                                    _ti_price = await self._db.get_latest_tiingo_price(window.asset)
                                except Exception:
                                    pass
                            _cl_str = f"{_cl_price:.2f}" if _cl_price else "N/A"
                            _ti_str = f"{_ti_price:.2f}" if _ti_price else "N/A"
                            _eval_notes = (
                                f"gamma_up={_snap_gamma_up:.4f},gamma_down={_snap_gamma_down:.4f},"
                                f"vpin={_vpin:.4f},delta_pct={_d:.4f},regime={_regime},"
                                f"tsf_dir={_tsf_dir},tsf_conf={_tsf_conf:.3f},"
                                f"twap_dir={_tw_dir},twap_agree={_tw_agree},"
                                f"btc={_btc:.2f},"
                                f"chainlink={_cl_str},"
                                f"tiingo={_ti_str}"
                            )
                            asyncio.create_task(self._db.write_countdown_evaluation({
                                "window_ts": window.window_ts,
                                "stage": _t_lbl,
                                "direction": _tsf_dir or ("UP" if _d > 0 else "DOWN"),
                                "confidence": _tsf_conf,
                                "agreement": _twap_agree_bool,
                                "action": "SNAPSHOT",
                                "notes": _eval_notes,
                                # v7.2: multi-source prices
                                "chainlink_price": _cl_price,
                                "tiingo_price": _ti_price,
                                "binance_price": _btc,
                            }))
                        except Exception:
                            pass

                        if self._alerter:
                            asyncio.create_task(self._alerter.send_window_snapshot(
                                window_id=_window_id,
                                t_label=_t_lbl,
                                elapsed_s=_elapsed_now,
                                price_ticks=_ticks or [window.open_price, _btc],
                                open_price=window.open_price,
                                current_price=_btc,
                                delta_pct=_d,
                                vpin=_vpin,
                                vpin_regime=_regime,
                                twap_direction=_tw_dir,
                                twap_agreement=_tw_agree,
                                timesfm_direction=_tsf_dir,
                                timesfm_confidence=_tsf_conf,
                                timesfm_predicted=_tsf_pred,
                                gamma_up=_snap_gamma_up,
                                gamma_down=_snap_gamma_down,
                                cg_taker_buy_pct=_cg_taker,
                                cg_funding_annual=_cg_fund,
                                stake_usd=4.0,
                                ai_commentary=_ai,
                            ))

                # Clean up old window tracking
                if len(self._countdown_sent) > 20:
                    _oldest = sorted(self._countdown_sent.keys())[:-10]
                    for k in _oldest:
                        del self._countdown_sent[k]

            # ── Multi-offset evaluation (v5.9) ───────────────────────────
            # CLOSING fires at each configured T-offset (e.g. T-90, T-60).
            # Strategy deduplicates: won't trade the same window twice.
            if state_value == "CLOSING":
                eval_offset = getattr(window, "eval_offset", None)
                window_key = f"{window.asset}-{window.window_ts}"
                log.info(
                    "five_min.closing_signal",
                    window_key=window_key,
                    eval_offset=eval_offset,
                )
                # G1 & G3: Queue window for staggered execution
                await self._execution_queue.put((window, self._aggregator))
            elif state_value != "ACTIVE":
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
        # ── TickRecorder: record Gamma prices on every 15m window signal ────
        if self._tick_recorder and window.up_price is not None:
            asyncio.create_task(self._tick_recorder.record_gamma_price(window))

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

                # v5.8: TimesFM checked inside v5.7c agreement (no standalone)
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
                        meta = order.metadata or {}
                        _window_id = f"{meta.get('asset', 'BTC')}-{int(meta.get('window_ts', 0))}"
                        _direction = "UP" if order.direction == "YES" else "DOWN"
                        _open_p = meta.get("window_open_price", 0) or 0
                        _close_p = float(self._order_manager._current_btc_price or 0)
                        _actual = "UP" if _close_p >= _open_p else "DOWN"
                        _delta = (_close_p - _open_p) / _open_p * 100 if _open_p else 0
                        _vpin = self._five_min_strategy._vpin.current_vpin if self._five_min_strategy and self._five_min_strategy._vpin else 0
                        # Win streak from risk manager
                        _rs = self._risk_manager.get_status()
                        _streak_w = _rs.get("win_streak", 0) if order.outcome == "WIN" else 0
                        _streak_l = _rs.get("loss_streak", 0) if order.outcome != "WIN" else 0
                        # Entry prices at each T- point (stored in window's countdown_sent data if available)
                        _entry_prices = meta.get("entry_prices_by_t", {})
                        if not _entry_prices:
                            _gamma_p = float(order.price or 0.50)
                            _entry_prices = {"T-60": _gamma_p}
                        await self._alerter.send_window_resolution(
                            window_id=_window_id,
                            asset=meta.get("asset", "BTC"),
                            timeframe=meta.get("timeframe", "5m"),
                            outcome=order.outcome or "UNKNOWN",
                            direction=_direction,
                            actual_direction=_actual,
                            entry_price=float(order.price or 0.50),
                            pnl_usd=order.pnl_usd or 0,
                            open_price=_open_p,
                            close_price=_close_p,
                            delta_pct=_delta,
                            vpin=_vpin,
                            regime=meta.get("regime", "UNKNOWN"),
                            entry_prices=_entry_prices,
                            stake_usd=order.stake_usd,
                            win_streak=_streak_w,
                            loss_streak=_streak_l,
                        )
                        
                        # Dual-AI outcome analysis with full window context (non-blocking)
                        if order.outcome in ("WIN", "LOSS") and self._alerter:
                            try:
                                _oid = order.order_id[:20]
                                _wid = _window_id
                                _dir = _direction
                                _ep = float(order.price or 0.50)
                                _oc = order.outcome
                                _pnl = order.pnl_usd or 0
                                _wd = {
                                    "vpin": _vpin,
                                    "delta_pct": _delta,
                                    "regime": meta.get("regime"),
                                    "open_price": _open_p,
                                    "close_price": _close_p,
                                    "timesfm_direction": meta.get("timesfm_direction"),
                                    "timesfm_confidence": meta.get("timesfm_confidence", 0),
                                    "twap_direction": meta.get("twap_direction"),
                                    "twap_agreement": meta.get("twap_agreement_score"),
                                    "gamma_up": meta.get("gamma_up_price"),
                                    "gamma_down": meta.get("gamma_down_price"),
                                    "cg_data": meta.get("cg_modifier_reason", ""),
                                }
                                async def _send_outcome_ai(_wid=_wid, _dir=_dir, _ep=_ep, _oc=_oc, _pnl=_pnl, _oid=_oid, _wd=_wd):
                                    try:
                                        result = await self._alerter.send_outcome_with_analysis(
                                            window_id=_wid, decision=_dir,
                                            entry_price=_ep, outcome=_oc, pnl_usd=_pnl,
                                            window_data=_wd,
                                        )
                                        log.debug("outcome_ai.sent", order_id=_oid, result_type=type(result).__name__)
                                    except Exception as exc:
                                        log.error("outcome_analysis_failed", order_id=_oid, error=str(exc)[:100])
                                asyncio.create_task(_send_outcome_ai())
                            except Exception as exc:
                                log.error("outcome_ai_spawn_failed", error=str(exc)[:100])
                        
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

                # ── Mode sync: read paper/live toggles from DB ──────────────
                # The frontend toggle updates system_state.paper_enabled/live_enabled
                # The engine picks it up here and switches mode at runtime
                try:
                    mode_row = await self._db.get_mode_toggles()
                    if mode_row is not None:
                        db_paper = mode_row.get("paper_enabled", True)
                        db_live = mode_row.get("live_enabled", False)
                        
                        # Determine target mode: if live_enabled and not paper_enabled → LIVE
                        # Otherwise → PAPER (safe default)
                        want_paper = not db_live or db_paper
                        current_paper = self._poly_client.paper_mode
                        
                        if want_paper != current_paper:
                            old_mode = "PAPER" if current_paper else "LIVE"
                            new_mode = "PAPER" if want_paper else "LIVE"
                            
                            log.warning(
                                "mode_switch.detected",
                                old_mode=old_mode,
                                new_mode=new_mode,
                                db_paper=db_paper,
                                db_live=db_live,
                            )
                            
                            # Switch poly client mode
                            self._poly_client.paper_mode = want_paper
                            self._settings.paper_mode = want_paper
                            
                            # Update risk manager
                            self._risk_manager._paper_mode = want_paper
                            
                            # Update alerter mode tag
                            if self._alerter:
                                self._alerter._paper_mode = want_paper
                            
                            # Telegram notification
                            if self._alerter:
                                mode_emoji = "📄 PAPER" if want_paper else "🔴 LIVE"
                                await self._alerter.send_system_alert(
                                    f"⚡ MODE SWITCH: {old_mode} → {new_mode}\n\n"
                                    f"Trading mode is now: {mode_emoji}\n"
                                    f"{'Simulated orders only' if want_paper else '⚠️ REAL MONEY — orders will be placed on Polymarket CLOB'}\n\n"
                                    f"Config: v7.1 | Gate: 0.45 | Cap: $0.70 | Bet: 10%",
                                    level="critical" if not want_paper else "info",
                                )
                            
                            log.warning(
                                "mode_switch.complete",
                                new_mode=new_mode,
                                paper_mode=want_paper,
                            )
                            
                            # Connect CLOB client if switching TO live
                            if not want_paper:
                                try:
                                    await self._poly_client.connect()
                                    log.info("mode_switch.clob_connected")
                                except Exception as exc:
                                    log.error("mode_switch.clob_connect_failed", error=str(exc)[:100])
                            
                            # Start redeemer if switching TO live
                            if not want_paper and self._redeemer:
                                try:
                                    self._redeemer._paper_mode = False
                                    await self._redeemer.connect()
                                    self._tasks.append(
                                        asyncio.create_task(
                                            self._redeemer_loop(), name="redeemer:sweep"
                                        )
                                    )
                                    log.info("orchestrator.redeemer_started_on_mode_switch")
                                except Exception as exc:
                                    log.error("orchestrator.redeemer_start_failed", error=str(exc))
                except Exception as exc:
                    log.debug("mode_sync.failed", error=str(exc)[:80])

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
                        wallet = _cached_wallet_balance or risk_status.get("current_bankroll", 0) or 0
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
                        # Use order manager for wins/losses (works for both paper and live)
                        real_wins = 0
                        real_losses = 0
                        open_positions_val = 0
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

                        mode_label = "📄 PAPER" if self._settings.paper_mode else "🔴 LIVE"

                        # Build CoinGlass block for sitrep
                        cg_block = ""
                        try:
                            cg_snapshot = self._cg_enhanced.snapshot if self._cg_enhanced is not None else None
                            cg_block = self._alerter.format_coinglass_block(cg_snapshot) + "\n"
                        except Exception:
                            pass

                        # ── P&L: use risk manager bankroll (synced from wallet in live) ──
                        baseline = self._settings.starting_bankroll
                        portfolio = bankroll + open_positions_val
                        real_pnl = daily_pnl  # Use today's tracked P&L, not lifetime
                        
                        # Live mode: wallet is the real USDC balance
                        if not self._settings.paper_mode and wallet and wallet > 0:
                            portfolio = wallet + open_positions_val

                        # ── Regime label (use VPIN thresholds from runtime) ──
                        vpin_regime = (
                            "CASCADE" if vpin >= runtime.vpin_cascade_direction_threshold
                            else "TRANSITION" if vpin >= runtime.vpin_informed_threshold
                            else "NORMAL" if vpin >= runtime.five_min_vpin_gate
                            else "CALM"
                        )

                        sitrep = (
                            f"📋 *5-MIN SITREP* ({status_emoji} {'KILLED' if killed else 'ACTIVE'}) {mode_label}\n"
                            f"\n"
                            + (
                                f"🏦 Cash: `${wallet:.2f}` USDC\n"
                                f"📊 Positions: `${open_positions_val:.2f}`\n"
                                f"💰 Portfolio: `${portfolio:.2f}`\n"
                                if not self._settings.paper_mode else
                                f"💰 Bankroll: `${bankroll:.2f}`\n"
                            ) +
                            f"📈 P&L: `${real_pnl:+.2f}` (from `${baseline:.0f}`)\n"
                            f"\n"
                            f"✅ Wins: `{real_wins}` | ❌ Losses: `{real_losses}`\n"
                            f"📉 Drawdown: `{drawdown:.1%}`\n"
                            f"\n"
                            f"🔬 VPIN: `{vpin:.4f}` | Regime: `{vpin_regime}`\n"
                            + cg_block +
                            f"🔗 Binance: `{'✅' if binance_ok else '❌'}` | BTC: `${self._order_manager._current_btc_price:,.2f}`\n"
                        )

                        await self._alerter.send_raw_message(sitrep)
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

    async def _polymarket_reconcile_loop(self) -> None:
        """Every 5 minutes: reconcile Polymarket activity with DB trades.

        Queries the Polymarket data API for recent user activity, compares with
        DB trades, and corrects any mismatches (wrong price, missing fills, etc).
        This runs on Montreal (where Polymarket is not geo-blocked).
        """
        import aiohttp
        from datetime import datetime, timezone

        RECONCILE_INTERVAL = 300  # 5 minutes
        POLYMARKET_ACTIVITY_URL = (
            "https://data-api.polymarket.com/activity"
            "?user=0x181d2ed714e0f7fe9c6e4f13711376edaab25e10&limit=20"
        )

        log.info("orchestrator.reconcile_loop.started")

        while not self._shutdown_event.is_set():
            try:
                async with aiohttp.ClientSession(
                    headers={"User-Agent": "Mozilla/5.0"}
                ) as session:
                    async with session.get(
                        POLYMARKET_ACTIVITY_URL,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status != 200:
                            log.warning(
                                "reconcile.api_error", status=resp.status
                            )
                            raise Exception(f"HTTP {resp.status}")
                        activity = await resp.json()

                if not self._db or not self._db._pool:
                    raise Exception("DB pool not available")

                # Pull recent trades from DB for comparison
                async with self._db._pool.acquire() as conn:
                    db_trades = await conn.fetch(
                        """
                        SELECT order_id, entry_price, status, outcome, metadata
                        FROM trades
                        WHERE created_at > NOW() - INTERVAL '2 hours'
                        AND is_live = TRUE
                        ORDER BY created_at DESC
                        LIMIT 50
                        """
                    )

                db_map = {row["order_id"]: row for row in db_trades}

                updates_made = 0
                for event in activity:
                    event_type = event.get("type")
                    slug = event.get("slug") or event.get("eventSlug", "")
                    price = event.get("price", 0)
                    size = event.get("size", 0)  # shares
                    usdc_size = event.get("usdcSize", 0)  # cost in USDC
                    side = event.get("side", "")
                    outcome_name = event.get("outcome", "")
                    tx_hash = event.get("transactionHash", "")

                    if event_type != "TRADE" or not slug:
                        continue

                    # Try to find matching DB trade by market slug
                    matched_row = None
                    matched_order_id = None
                    for oid, row in db_map.items():
                        meta = {}
                        try:
                            import json as _json
                            meta = _json.loads(row["metadata"]) if row["metadata"] else {}
                        except Exception:
                            pass
                        if meta.get("market_slug") == slug or slug in (meta.get("market_slug", "")):
                            matched_row = row
                            matched_order_id = oid
                            break

                    if matched_row is None:
                        continue

                    # Check if fill price is wrong
                    db_price = float(matched_row["entry_price"]) if matched_row["entry_price"] else None
                    actual_price = float(price) if price else None

                    needs_update = False
                    update_fields = {}

                    if (
                        actual_price
                        and db_price
                        and abs(actual_price - db_price) > 0.005  # >0.5¢ mismatch
                    ):
                        log.info(
                            "reconcile.price_mismatch",
                            order_id=matched_order_id[:20] if matched_order_id else "?",
                            db_price=f"${db_price:.4f}",
                            actual_price=f"${actual_price:.4f}",
                            slug=slug,
                        )
                        update_fields["entry_price"] = actual_price
                        needs_update = True

                    if needs_update and self._db._pool:
                        try:
                            set_clauses = ", ".join(
                                f"{k} = ${i+2}"
                                for i, k in enumerate(update_fields.keys())
                            )
                            values = list(update_fields.values())
                            async with self._db._pool.acquire() as conn:
                                await conn.execute(
                                    f"UPDATE trades SET {set_clauses} WHERE order_id = $1",
                                    matched_order_id,
                                    *values,
                                )
                            updates_made += 1
                            log.info(
                                "reconcile.updated",
                                order_id=matched_order_id[:20] if matched_order_id else "?",
                                fields=list(update_fields.keys()),
                            )
                        except Exception as upd_exc:
                            log.warning(
                                "reconcile.update_failed",
                                error=str(upd_exc)[:80],
                            )

                if updates_made > 0:
                    log.info(
                        "reconcile.complete",
                        updates=updates_made,
                        activity_events=len(activity),
                    )
                else:
                    log.debug("reconcile.no_mismatches", checked=len(activity))

            except Exception as exc:
                log.warning(
                    "orchestrator.reconcile_loop.error", error=str(exc)[:120]
                )

            try:
                await asyncio.wait_for(
                    asyncio.shield(self._shutdown_event.wait()),
                    timeout=float(RECONCILE_INTERVAL),
                )
                break
            except asyncio.TimeoutError:
                pass  # Normal — continue

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

                # Always notify on sweep results
                if self._alerter:
                    try:
                        redeemed = result.get("redeemed", 0)
                        failed = result.get("failed", 0)
                        total = result.get("total_positions", 0)
                        wins = result.get("wins", 0)
                        losses = result.get("losses", 0)
                        pnl = result.get("total_pnl", 0)
                        usdc = result.get("usdc_change", 0)
                        
                        if redeemed > 0 or failed > 0:
                            await self._alerter._send_with_id(
                                f"🔄 *REDEMPTION SWEEP* 🔴 LIVE\n\n"
                                f"Positions: `{total}` | Redeemed: `{redeemed}` | Failed: `{failed}`\n"
                                f"Wins: `{wins}` | Losses: `{losses}`\n"
                                f"P&L: `${pnl:+.2f}` | USDC change: `${usdc:+.2f}`\n"
                            )
                    except Exception:
                        pass

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

    async def _manual_trade_poller(self) -> None:
        """Poll DB for manual 'pending_live' trades from the dashboard and execute them.
        
        The hub API writes trades with status='pending_live' when Billy clicks
        the Live Trade button. This poller picks them up and submits FOK orders
        to Polymarket via the poly_client.
        """
        log.info("manual_trade_poller.started")
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(1)  # Poll every 1 second — trades are time-sensitive
                if not self._db or not self._poly_client:
                    continue
                
                pending = await self._db.poll_pending_live_trades()
                for trade in pending:
                    trade_id = trade["trade_id"]
                    direction_raw = trade["direction"]  # "UP" or "DOWN"
                    direction = "YES" if direction_raw == "UP" else "NO"
                    entry_price = trade["entry_price"]
                    stake = trade["stake_usd"] or 4.0
                    window_ts = trade["window_ts"]
                    asset = trade.get("asset", "BTC")
                    
                    log.info(
                        "manual_trade.executing",
                        trade_id=trade_id,
                        direction=direction,
                        entry_price=f"${entry_price:.4f}",
                        stake=f"${stake:.2f}",
                    )
                    
                    # Update status to 'executing'
                    await self._db.update_manual_trade_status(trade_id, "executing")
                    
                    try:
                        # Build market slug
                        tf = "5m"  # Manual trades default to 5m
                        market_slug = f"{asset.lower()}-updown-{tf}-{window_ts}"
                        
                        # Get token ID from recent windows
                        token_id = None
                        if self._five_min_strategy and hasattr(self._five_min_strategy, '_recent_windows'):
                            for w in reversed(self._five_min_strategy._recent_windows):
                                if w.window_ts == window_ts:
                                    token_id = w.up_token_id if direction == "YES" else w.down_token_id
                                    break
                        
                        if not token_id:
                            log.warning("manual_trade.no_token_id", trade_id=trade_id)
                            await self._db.update_manual_trade_status(trade_id, "failed_no_token")
                            continue
                        
                        if self._poly_client.paper_mode:
                            # Paper mode — simulate fill
                            clob_id = f"manual-paper-{trade_id[:12]}"
                            await self._db.update_manual_trade_status(trade_id, "open")
                            log.info("manual_trade.paper_filled", trade_id=trade_id, clob_id=clob_id)
                        else:
                            # Live — submit FOK to CLOB
                            from decimal import Decimal
                            clob_id = await self._poly_client.place_order(
                                market_slug=market_slug,
                                direction=direction,
                                price=Decimal(str(round(min(entry_price + 0.02, 0.65), 4))),  # Slight buffer above entry
                                stake_usd=stake,
                                token_id=token_id,
                            )
                            await self._db.update_manual_trade_status(trade_id, "open")
                            log.info("manual_trade.live_submitted", trade_id=trade_id, clob_id=str(clob_id)[:20])
                        
                        # Alert on Telegram
                        if self._alerter:
                            _mode = "📄 PAPER" if self._poly_client.paper_mode else "🔴 LIVE"
                            await self._alerter.send_system_alert(
                                f"👆 Manual Trade Executed ({_mode})\n"
                                f"Direction: {direction_raw}\n"
                                f"Entry: ${entry_price:.4f}\n"
                                f"Stake: ${stake:.2f}\n"
                                f"Trade ID: {trade_id[:16]}",
                                level="info",
                            )
                    
                    except Exception as exc:
                        log.error("manual_trade.execution_failed", trade_id=trade_id, error=str(exc))
                        await self._db.update_manual_trade_status(trade_id, f"failed: {str(exc)[:50]}")
                        
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("manual_trade_poller.error", error=str(exc))
                await asyncio.sleep(30)
        
        log.info("manual_trade_poller.stopped")

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
                        # G1: Stagger execution with jitter (reduced — FOK fills are instant)
                        stagger_delay = runtime.order_stagger_seconds
                        jitter = random.uniform(0.3, 1.0)  # 0.3-1s jitter (was 1-3s)
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
