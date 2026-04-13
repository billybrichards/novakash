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
from evaluation.post_resolution_evaluator import PostResolutionEvaluator
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
        self._execution_queue: asyncio.Queue = (
            asyncio.Queue()
        )  # Pending window evaluations
        self._geoblock_active: bool = False  # G6: Geoblock flag

        # ── LT-04: manual trade fast path (hybrid LISTEN/NOTIFY + poll) ──────
        # When the hub INSERTs a row into manual_trades with
        # status='pending_live', it also emits
        #   SELECT pg_notify('manual_trade_pending', trade_id)
        # which wakes this event. The poll loop uses asyncio.wait_for on
        # this event with a 1s timeout as a safety-net fall-through, so
        # latency drops from ~1s (worst-case tick wait) to ~tens of
        # milliseconds (LISTEN propagation + engine execute) on the
        # happy path.
        self._manual_trade_notify_event: asyncio.Event = asyncio.Event()

        # ── Dedup: track conditions resolved by OrderManager callback ─────
        # When _on_order_resolution fires, we add the condition_id here.
        # _position_monitor_loop skips any condition_id already in this set.
        self._resolved_by_order_manager: set = set()

        # ── v12: Cache latest strategy port result for sitrep + alerts ─────
        self._last_sp_result = None  # EvaluateStrategiesResult from last window

        # ── CLOB Reconciler (v10.2: definitive source of truth) ─────
        self._reconciler = None
        self._reconcile_uc = None  # ReconcilePositionsUseCase — wired in start()
        self._window_state_repo = None  # PgWindowRepository — wired in start()
        self._trade_repo_adapter = None  # PgTradeRepository — wired in start()

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
        effective_bankroll = (
            settings.paper_bankroll
            if settings.paper_mode and settings.paper_bankroll > 0
            else settings.starting_bankroll
        )

        self._risk_manager = RiskManager(
            order_manager=self._order_manager,
            starting_bankroll=effective_bankroll,
            paper_mode=settings.paper_mode,
        )

        # Wire alerter references now that risk_manager and poly_client exist
        self._alerter.set_risk_manager(self._risk_manager)
        self._alerter.set_poly_client(self._poly_client)
        self._alerter.set_location("MTL", "v11.2")

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
            log.info(
                "orchestrator.coinglass_multi_asset", assets=list(self._cg_feeds.keys())
            )

        # ── Claude Opus 4.6 AI Evaluator ─────────────────────────────────────
        self._claude_evaluator = None
        if settings.anthropic_api_key:
            self._claude_evaluator = ClaudeEvaluator(
                api_key=settings.anthropic_api_key,
                alerter=self._alerter,
                db_client=self._db,
            )
            log.info("orchestrator.claude_evaluator_enabled")

        # ── Post-Resolution AI Evaluator (Sonnet, runs after shadow resolution) ─
        self._post_resolution_evaluator = None
        if settings.anthropic_api_key:
            self._post_resolution_evaluator = PostResolutionEvaluator(
                api_key=settings.anthropic_api_key,
                db_client=self._db,
                alerter=self._alerter,
            )
            log.info("orchestrator.post_resolution_evaluator_enabled")

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
                            timesfm_enabled = (
                                line.split("=", 1)[1].strip().lower() == "true"
                            )
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
            self._five_min_strategy.set_timesfm_client(self._timesfm_client)
            log.info("orchestrator.timesfm_injected_into_five_min")

        # v8.1: Inject TimesFM v2.2 client for early entry (calibrated probability)
        _v2_enabled = os.environ.get("V2_EARLY_ENTRY_ENABLED", "true").lower() == "true"
        if _v2_enabled and self._five_min_strategy:
            from signals.timesfm_v2_client import TimesFMV2Client

            _v2_url = os.environ.get("TIMESFM_V2_URL", "http://3.98.114.0:8080")
            self._five_min_strategy.set_timesfm_v2_client(
                TimesFMV2Client(base_url=_v2_url)
            )
            log.info("orchestrator.v2_early_entry_enabled", url=_v2_url)
        else:
            log.info("orchestrator.v2_early_entry_disabled")

        # ── SP-04: Multi-Strategy Port (behind ENGINE_USE_STRATEGY_PORT flag) ──
        # Priority: runtime (DB-synced) > env var > code default.
        # runtime.use_strategy_port is initialised from env in RuntimeConfig.__init__,
        # so at __init__ time this is effectively env-var driven (DB sync hasn't run yet).
        self._use_strategy_port = runtime.use_strategy_port
        self._evaluate_strategies_uc = None
        if self._use_strategy_port:
            from domain.value_objects import StrategyRegistration
            from use_cases.evaluate_strategies import EvaluateStrategiesUseCase
            from adapters.strategies.v10_gate_strategy import V10GateStrategy

            # runtime.v10_gate_mode / v4_fusion_mode are hot-reloaded each sync(),
            # but StrategyRegistration is structural so we read at init time.
            # The per-window evaluate() path in EvaluateStrategiesUseCase re-checks
            # the live registration mode via runtime on each call.
            v10_mode = runtime.v10_gate_mode
            v10_reg = StrategyRegistration(
                strategy_id="v10_gate",
                mode=v10_mode,
                enabled=True,
                priority=1,
            )
            v10_strat = V10GateStrategy(dune_client=self._timesfm_client)

            strategy_pairs = [(v10_reg, v10_strat)]

            # V4 Fusion (optional, GHOST by default)
            v4_enabled = runtime.v4_fusion_enabled
            v4_snapshot_port = None
            if v4_enabled:
                from adapters.strategies.v4_fusion_strategy import V4FusionStrategy
                from adapters.v4_snapshot_http import V4SnapshotHttpAdapter

                v4_mode = runtime.v4_fusion_mode
                v4_reg = StrategyRegistration(
                    strategy_id="v4_fusion",
                    mode=v4_mode,
                    enabled=True,
                    priority=2,
                )
                v4_strat = V4FusionStrategy()
                strategy_pairs.append((v4_reg, v4_strat))
                v4_snapshot_port = V4SnapshotHttpAdapter()

            # V4 DOWN-Only: DOWN filter + CLOB sizing (SIG-03/SIG-04).
            # Independent of v4_fusion_enabled — registers its own snapshot port
            # if V4 Fusion isn't enabled. Both share the same port if it exists.
            if runtime.v4_down_only_enabled:
                from adapters.strategies.v4_down_only_strategy import V4DownOnlyStrategy

                if v4_snapshot_port is None:
                    from adapters.v4_snapshot_http import V4SnapshotHttpAdapter

                    v4_snapshot_port = V4SnapshotHttpAdapter()
                v4_down_mode = runtime.v4_down_only_mode
                v4_down_reg = StrategyRegistration(
                    strategy_id="v4_down_only",
                    mode=v4_down_mode,
                    enabled=True,
                    priority=3,
                )
                strategy_pairs.append((v4_down_reg, V4DownOnlyStrategy()))
            elif not v4_enabled:
                log.warning(
                    "orchestrator.v4_down_only_disabled",
                    hint="set V4_DOWN_ONLY_ENABLED=true in DB config or .env to enable primary DOWN strategy",
                )

            # V4 Asian UP: UP-only, Asian session (23:00-02:59 UTC), medium conviction.
            # Discovered 2026-04-12: 81-99% WR (5,543 samples, dist 0.15-0.20, hrs 23,0,1,2).
            # Safe to run simultaneously with v4_down_only — they're direction-exclusive
            # (UP vs DOWN) so they never both fire in the same window.
            if runtime.v4_up_asian_enabled:
                from adapters.strategies.v4_up_asian_strategy import V4UpAsianStrategy

                if v4_snapshot_port is None:
                    from adapters.v4_snapshot_http import V4SnapshotHttpAdapter

                    v4_snapshot_port = V4SnapshotHttpAdapter()
                v4_up_mode = runtime.v4_up_asian_mode
                v4_up_reg = StrategyRegistration(
                    strategy_id="v4_up_asian",
                    mode=v4_up_mode,
                    enabled=True,
                    priority=4,
                )
                strategy_pairs.append((v4_up_reg, V4UpAsianStrategy()))

            from adapters.persistence.pg_strategy_decisions import (
                PgStrategyDecisionRepository,
            )

            # Pass the db_client — the repo extracts the pool lazily via _get_pool()
            # so it works even though the pool isn't connected at __init__ time
            _decision_repo = PgStrategyDecisionRepository(db_client=self._db)

            self._evaluate_strategies_uc = EvaluateStrategiesUseCase(
                strategies=strategy_pairs,
                decision_repo=_decision_repo,
                v4_snapshot_port=v4_snapshot_port,
                vpin_calculator=self._vpin_calc,
                cg_feeds=self._cg_feeds,
                twap_tracker=self._twap_tracker,
                db_client=self._db,
            )
            log.info(
                "orchestrator.strategy_port_enabled",
                strategies=[f"{r.strategy_id}({r.mode})" for r, _ in strategy_pairs],
            )
        else:
            log.info("orchestrator.strategy_port_disabled")

        # ── Strategy Engine v2: Config-first registry (behind feature flag) ──
        # When enabled, runs the new YAML-config-based registry in parallel
        # with the existing EvaluateStrategiesUseCase for decision comparison.
        self._strategy_registry = None
        self._use_strategy_registry = (
            os.environ.get("ENGINE_USE_STRATEGY_REGISTRY", "false").lower() == "true"
        )
        if self._use_strategy_registry:
            try:
                from strategies.data_surface import DataSurfaceManager
                from strategies.registry import StrategyRegistry

                config_dir = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "configs"
                )
                self._data_surface_mgr = DataSurfaceManager(
                    v4_base_url=os.environ.get("TIMESFM_URL", "http://localhost:8001"),
                    tiingo_feed=getattr(self, "_tiingo_feed", None),
                    chainlink_feed=getattr(self, "_chainlink_feed", None),
                    clob_feed=getattr(self, "_clob_feed", None),
                    vpin_calculator=self._vpin_calc,
                    cg_feeds=self._cg_feeds,
                    twap_tracker=self._twap_tracker,
                    binance_state=self._aggregator
                    if hasattr(self, "_aggregator")
                    else None,
                )
                # Wire ExecuteTradeUseCase if execution is enabled
                _execute_uc = None
                if os.environ.get("ENGINE_REGISTRY_EXECUTE", "false").lower() == "true":
                    try:
                        from use_cases.execute_trade import ExecuteTradeUseCase
                        from adapters.execution.paper_executor import PaperExecutor
                        from adapters.execution.fak_ladder_executor import (
                            FAKLadderExecutor,
                        )
                        from adapters.execution.trade_recorder import (
                            DBTradeRecorder as TradeRecorder,
                        )

                        _paper = os.environ.get("PAPER_MODE", "true").lower() == "true"
                        _executor = (
                            PaperExecutor()
                            if _paper
                            else FAKLadderExecutor(
                                poly_client=self._poly_client,
                            )
                        )
                        _recorder = (
                            TradeRecorder(
                                db_client=self._db,
                                order_manager=self._order_manager,
                            )
                            if self._db
                            else None
                        )

                        from adapters.clock.system_clock import SystemClock

                        _execute_uc = ExecuteTradeUseCase(
                            polymarket=self._poly_client,
                            order_executor=_executor,
                            risk_manager=self._risk_manager,
                            window_state=getattr(self, "_window_state_repo", None),
                            alerter=self._alerter,
                            trade_recorder=_recorder,
                            clock=SystemClock(),
                            paper_mode=_paper,
                        )
                        log.info(
                            "orchestrator.execute_trade_uc_wired", paper_mode=_paper
                        )
                    except Exception as exc:
                        log.warning(
                            "orchestrator.execute_trade_uc_error", error=str(exc)[:200]
                        )

                # Wire decision repo for per-eval strategy_decisions writes
                _decision_repo = None
                try:
                    from adapters.persistence.pg_strategy_decisions import (
                        PgStrategyDecisionRepository,
                    )

                    _decision_repo = PgStrategyDecisionRepository(
                        db_client=self._db,
                    )
                except Exception as exc:
                    log.warning(
                        "orchestrator.decision_repo_init_error",
                        error=str(exc)[:200],
                    )

                self._strategy_registry = StrategyRegistry(
                    config_dir,
                    self._data_surface_mgr,
                    execute_trade_uc=_execute_uc,
                    alerter=self._alerter,
                    decision_repo=_decision_repo,
                )
                self._strategy_registry.load_all()
                log.info(
                    "orchestrator.strategy_registry_enabled",
                    strategies=self._strategy_registry.strategy_names,
                )
            except Exception as exc:
                log.error(
                    "orchestrator.strategy_registry_init_error", error=str(exc)[:200]
                )
                self._strategy_registry = None

        # TickRecorder is not yet available at __init__ (pool not connected)
        # It is injected in start() after pool is live.

        # 15-minute Polymarket strategy (uses same strategy, different feed)
        self._fifteen_min_feed = None
        fifteen_min_enabled = (
            os.environ.get("FIFTEEN_MIN_ENABLED", "false").lower() == "true"
        )
        fifteen_min_assets = os.environ.get("FIFTEEN_MIN_ASSETS", "BTC,ETH,SOL").split(
            ","
        )
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
        # Futures feed: aggTrades → VPIN calculator, forceOrder → cascade detector
        self._binance_feed = BinanceWebSocketFeed(
            symbol="btcusdt",
            venue="futures",
            on_trade=self._on_binance_trade,
            on_liquidation=self._aggregator.on_liquidation,
        )
        # Spot feed: aggTrades → btc_spot_price for oracle-aligned delta calculation
        # (Polymarket resolves via Chainlink oracle on SPOT, not futures)
        self._binance_spot_feed = BinanceWebSocketFeed(
            symbol="btcusdt",
            venue="spot",
            on_trade=self._on_binance_spot_trade,
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
            tid.strip() for tid in settings.poly_btc_token_ids.split(",") if tid.strip()
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

        # ── Reconcile UC: wire after pool is live ──────────────────────────────
        try:
            from use_cases.reconcile_positions import ReconcilePositionsUseCase
            from adapters.persistence.pg_trade_repo import PgTradeRepository
            from adapters.persistence.pg_window_repo import PgWindowRepository
            from adapters.clock.system_clock import SystemClock

            self._window_state_repo = PgWindowRepository(self._db._pool)
            self._trade_repo_adapter = PgTradeRepository(self._db._pool)
            self._reconcile_uc = ReconcilePositionsUseCase(
                trade_repo=self._trade_repo_adapter,
                window_state=self._window_state_repo,
                alerts=self._alerter,
                clock=SystemClock(),
            )
            log.info("orchestrator.reconcile_uc_wired")
        except Exception as exc:
            log.warning("orchestrator.reconcile_uc_failed", error=str(exc)[:200])
            self._reconcile_uc = None

        # ── TickRecorder: initialise now that pool is live ──────────────────
        try:
            self._tick_recorder = TickRecorder(pool=self._db._pool)
            await self._tick_recorder.ensure_tables()
            await self._tick_recorder.start()
            # Inject into five_min_strategy so it can record TimesFM forecasts
            if self._five_min_strategy:
                self._five_min_strategy.set_tick_recorder(self._tick_recorder)
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
                _poly_client = getattr(self._order_manager, "_poly_client", None)
                _five_min_feed = getattr(self, "_strategy", None)
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
                    log.info(
                        "orchestrator.vpin_warm_start",
                        ticks=ticks,
                        vpin=f"{self._vpin_calc.current_vpin:.4f}",
                    )
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
                asyncio.create_task(
                    self._fifteen_min_feed.start(), name="feed:fifteen_min"
                )
            )

        # 4. Start feed tasks
        self._tasks.append(
            asyncio.create_task(self._binance_feed.start(), name="feed:binance_futures")
        )
        self._tasks.append(
            asyncio.create_task(
                self._binance_spot_feed.start(), name="feed:binance_spot"
            )
        )
        if self._coinglass_feed:
            self._tasks.append(
                asyncio.create_task(self._coinglass_feed.start(), name="feed:coinglass")
            )
        if self._cg_feeds:
            for _sym, _cgf in self._cg_feeds.items():
                self._tasks.append(
                    asyncio.create_task(
                        self._start_cg_staggered(_cgf, _sym),
                        name=f"feed:cg_{_sym.lower()}",
                    )
                )
        elif self._cg_enhanced:
            self._tasks.append(
                asyncio.create_task(
                    self._cg_enhanced.start(), name="feed:coinglass_enhanced"
                )
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

        # 5c. Prediction recorder (all 4 assets, every 30s)
        if self._five_min_strategy and self._five_min_strategy.timesfm_v2_client:
            from data.feeds.prediction_recorder import PredictionRecorder

            _prediction_recorder = PredictionRecorder(
                elm_client=self._five_min_strategy.timesfm_v2_client,
                db_pool=self._db._pool if self._db else None,
                shutdown_event=self._shutdown_event,
            )
            self._tasks.append(
                asyncio.create_task(
                    _prediction_recorder.run(), name="prediction_recorder"
                )
            )
            log.info(
                "orchestrator.prediction_recorder_started",
                assets=["BTC", "ETH", "SOL", "XRP"],
            )

        # 5d. Polymarket trade history reconciler (every 5 min)
        if self._poly_client and self._db and self._db._pool:
            from reconciliation.poly_trade_history import PolyTradeHistoryReconciler

            _poly_hist = PolyTradeHistoryReconciler(
                poly_client=self._poly_client,
                db_pool=self._db._pool,
                alerter=self._alerter,
                shutdown_event=self._shutdown_event,
            )
            self._tasks.append(
                asyncio.create_task(_poly_hist.run(), name="poly_trade_history")
            )
            log.info("orchestrator.poly_trade_history_started")

        # 5e. v11: poly_fills reconciler — authoritative source-of-truth
        # sync from Polymarket data-api. Runs every 5 minutes, append-only,
        # enriches trade_bible with condition_id / market_slug / fill linkage.
        # This is the GROUND TRUTH table for post-hoc P&L analysis.
        if self._db and self._db._pool and self._settings.poly_funder_address:
            from reconciliation.poly_fills_reconciler import PolyFillsReconciler

            self._poly_fills_reconciler = PolyFillsReconciler(
                pool=self._db._pool,
                funder_address=self._settings.poly_funder_address,
            )
            self._tasks.append(
                asyncio.create_task(
                    self._poly_fills_loop(), name="poly_fills_reconciler"
                )
            )
            log.info(
                "orchestrator.poly_fills_reconciler_started",
                funder=self._settings.poly_funder_address,
            )

        # 5. Heartbeat task (every 10s)
        self._tasks.append(
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat")
        )

        # 6. Resolution polling task (every 5s)
        self._tasks.append(
            asyncio.create_task(self._resolution_loop(), name="resolution_poller")
        )

        # 6b. Shadow trade resolution loop (every 30s) — evaluates skipped windows
        self._tasks.append(
            asyncio.create_task(
                self._shadow_resolution_loop(), name="shadow_resolution"
            )
        )

        # 6c. Ensure shadow columns exist in window_snapshots
        try:
            await self._db.ensure_shadow_columns()
        except Exception as exc:
            log.warning("orchestrator.ensure_shadow_columns_failed", error=str(exc))

        # 6c2. Ensure post-resolution analysis table exists
        try:
            await self._db.ensure_post_resolution_table()
        except Exception as exc:
            log.warning(
                "orchestrator.ensure_post_resolution_table_failed", error=str(exc)
            )

        try:
            await self._db.ensure_window_predictions_table()
        except Exception as exc:
            log.warning(
                "orchestrator.ensure_window_predictions_table_failed", error=str(exc)
            )

        # 6d. Ensure v8.0 columns exist in trades table
        try:
            await self._db.ensure_v8_trade_columns()
        except Exception as exc:
            log.warning("orchestrator.ensure_v8_trade_columns_failed", error=str(exc))

        # 6d2. POLY-SOT: ensure manual_trades has the source-of-truth columns.
        # Hub also ensures these on its own startup, but the engine restart
        # cycle is independent and the SOT reconciler loop will fail loudly
        # if it tries to write a column that doesn't exist yet.
        try:
            await self._db.ensure_manual_trades_sot_columns()
        except Exception as exc:
            log.warning(
                "orchestrator.ensure_manual_trades_sot_columns_failed", error=str(exc)
            )

        # 6d3. POLY-SOT-b: same for the `trades` table — automatic engine
        # trades now get the same SOT treatment as operator manual trades.
        try:
            await self._db.ensure_trades_sot_columns()
        except Exception as exc:
            log.warning("orchestrator.ensure_trades_sot_columns_failed", error=str(exc))

        # 6f. Recover open trades from previous sessions (startup trade recovery)
        try:
            recovered = await self._order_manager.recover_open_trades(self._db)
            log.info("orchestrator.trades_recovered", count=recovered)
            if recovered > 0:
                await self._alerter.send_raw_message(
                    f"♻️ *Trade Recovery*\nRecovered `{recovered}` open trade(s) from previous session.\n"
                    f"Oracle polling will resolve them automatically."
                )
        except Exception as exc:
            log.warning("orchestrator.trade_recovery_failed", error=str(exc))

        # 6e. CLOB Reconciler (v10.2) or legacy reconciliation loop
        _use_reconciler = os.environ.get("RECONCILER_ENABLED", "true").lower() == "true"
        if not self._settings.paper_mode and _use_reconciler:
            try:
                from reconciliation.reconciler import CLOBReconciler

                self._reconciler = CLOBReconciler(
                    poly_client=self._poly_client,
                    db_pool=self._db._pool,
                    alerter=self._alerter,
                    shutdown_event=self._shutdown_event,
                )
                await self._reconciler.start()
                log.info("orchestrator.clob_reconciler_started")
            except Exception as exc:
                log.error("orchestrator.clob_reconciler_failed", error=str(exc))
                self._reconciler = None
        elif not self._settings.paper_mode:
            # Legacy: old 5-min reconcile loop (fallback when RECONCILER_ENABLED=false)
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

        # 7c. POLY-SOT reconciler loop (always-on, runs in both paper and
        # live mode so paper trades exercise the same code path that live
        # trades will). Cadence is conservative — 2 minutes is enough to
        # catch the failure mode the user flagged (engine claims executed
        # but Polymarket has no record) without hammering the CLOB.
        self._tasks.append(
            asyncio.create_task(self._sot_reconciler_loop(), name="sot_reconciler")
        )

        # 8. Builder Relayer redeemer (live mode only)
        if not self._settings.paper_mode:
            try:
                await self._redeemer.connect()
                self._tasks.append(
                    asyncio.create_task(self._redeemer_loop(), name="redeemer:sweep")
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

        if not self._settings.paper_mode and not _use_reconciler:
            # Legacy position monitor (disabled when CLOB reconciler is active)
            self._tasks.append(
                asyncio.create_task(
                    self._position_monitor_loop(), name="position_monitor"
                )
            )

        # ── G1 & G3: Staggered execution + single best signal loop ──────────────
        self._tasks.append(
            asyncio.create_task(
                self._staggered_execution_loop(), name="staggered_execution"
            )
        )

        # Register OS signal handlers
        loop = asyncio.get_running_loop()
        for sig in (_signal.SIGINT, _signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._handle_os_signal)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass

        # Start Strategy Engine v2 DataSurfaceManager background loop
        # Inject live feed references NOW (they were None at __init__ time)
        if self._strategy_registry and hasattr(self, "_data_surface_mgr"):
            try:
                self._data_surface_mgr.set_feeds(
                    tiingo_feed=getattr(self, "_tiingo_feed", None),
                    chainlink_feed=getattr(self, "_chainlink_feed", None),
                    clob_feed=getattr(self, "_clob_feed", None),
                    vpin_calculator=self._vpin_calc,
                    cg_feeds=self._cg_feeds,
                    twap_tracker=self._twap_tracker,
                    binance_state=self._aggregator
                    if hasattr(self, "_aggregator")
                    else None,
                )
                await self._data_surface_mgr.start()
                log.info("orchestrator.data_surface_manager_started")
            except Exception as exc:
                log.warning(
                    "orchestrator.data_surface_start_error", error=str(exc)[:200]
                )

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

        # Stop CLOB Reconciler
        if self._reconciler:
            try:
                await self._reconciler.stop()
            except Exception as exc:
                log.warning("orchestrator.reconciler_stop_error", error=str(exc))

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
        await self._binance_spot_feed.stop()
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
                        ts = (
                            trade.trade_time.timestamp()
                            if hasattr(trade.trade_time, "timestamp")
                            else time.time()
                        )
                        self._twap_tracker.add_tick("BTC", wts, float(trade.price), ts)
                    except (ValueError, TypeError):
                        pass

    async def _on_binance_spot_trade(self, trade: AggTrade) -> None:
        """Binance SPOT aggTrade → aggregator (btc_spot_price for delta)."""
        await self._aggregator.on_spot_trade(trade)

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
                window_ts = (
                    int(now // 300) * 300
                )  # current window open (aligned to 300s)
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
                log.error(
                    "orchestrator.cascade_update_from_vpin_failed", error=str(exc)
                )

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
        window_state = getattr(window, "state", None)
        state_value = (
            window_state.value
            if hasattr(window_state, "value")
            else str(window_state)
            if window_state
            else "NO_STATE"
        )
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
            self._five_min_strategy.append_pending_window(window)
            self._five_min_strategy.append_recent_window(window)

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
                    _up_ratio = (
                        window.up_price / (window.up_price + window.down_price)
                        if (window.up_price + window.down_price) > 0
                        else 0.5
                    )
                    # Map token ratio to price delta: ratio 0.55 ≈ +0.05% delta
                    _implied_delta = (_up_ratio - 0.5) * 0.002  # Scale factor
                    _proxy_price = window.open_price * (1 + _implied_delta)
                self._twap_tracker.add_tick(
                    window.asset, window.window_ts, _proxy_price
                )

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
                if not hasattr(self, "_countdown_sent"):
                    self._countdown_sent = {}
                if _wkey not in self._countdown_sent:
                    self._countdown_sent[_wkey] = set()
                    # WINDOW OPEN card — fires once at T-300 (window just opened)
                    if self._alerter and _remaining >= 270:
                        asyncio.create_task(
                            self._alerter.send_window_open(
                                window_id=_window_id,
                                asset=window.asset,
                                timeframe=_tf,
                                open_price=window.open_price,
                                gamma_up=window.up_price or 0.50,
                                gamma_down=window.down_price or 0.50,
                            )
                        )

                log.info(
                    "five_min.countdown_check",
                    remaining=f"{_remaining:.1f}s",
                    sent=str(self._countdown_sent.get(_wkey, set())),
                )

                # ── Helper: get full signal snapshot ────────────────────
                async def _get_full_snapshot(t_label: str, elapsed: int):
                    _vpin = self._five_min_strategy.current_vpin
                    _btc = (
                        float(self._aggregator._state.btc_price)
                        if self._aggregator._state.btc_price
                        else 0.0
                    )
                    _d = (
                        (_btc - window.open_price) / window.open_price * 100
                        if window.open_price and _btc
                        else 0.0
                    )
                    _regime = (
                        "CASCADE"
                        if _vpin >= 0.65
                        else "TRANSITION"
                        if _vpin >= 0.55
                        else "NORMAL"
                        if _vpin >= 0.45
                        else "CALM"
                    )
                    # TimesFM
                    _tsf_dir, _tsf_conf, _tsf_pred = None, 0.0, 0.0
                    if self._five_min_strategy.timesfm_client:
                        try:
                            _secs = max(
                                1,
                                int(
                                    (window.window_ts + window.duration_secs)
                                    - _time.time()
                                ),
                            )
                            _tsf = await self._five_min_strategy.timesfm_client.get_forecast(
                                open_price=window.open_price, seconds_to_close=_secs
                            )
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
                            asset=window.asset,
                            window_ts=window.window_ts,
                            current_price=_btc,
                            gamma_up_price=window.up_price,
                            gamma_down_price=window.down_price,
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
                            f"{window.asset}-{window.window_ts}", None
                        )
                        if _tw_state and hasattr(_tw_state, "ticks"):
                            _ticks = [t[1] for t in _tw_state.ticks[-300:]]
                    except Exception:
                        _ticks = [window.open_price, _btc]
                    # AI commentary (non-blocking, best-effort)
                    _ai = None
                    if self._alerter._anthropic_api_key:
                        try:
                            # v8.0: Tiingo-based prompt, Sonnet model, adequate tokens
                            _dir_word = "UP" if _d > 0 else "DOWN"
                            _prompt = (
                                f"BTC 5m window at {t_label} ({int(_remaining)}s left). "
                                f"Tiingo delta: {_d:+.4f}% → {_dir_word}. "
                                f"VPIN: {_vpin:.3f} ({_regime}). "
                                f"CG: taker {_cg_taker:.0f}% buy, funding {_cg_fund:.0f}%/yr. "
                                f"CLOB: UP ${_snap_gamma_up:.2f} / DOWN ${_snap_gamma_down:.2f}. "
                                f"1 sentence: signal strength and key risk."
                            )
                            import aiohttp as _ah

                            async with _ah.ClientSession() as _sess:
                                async with _sess.post(
                                    "https://api.anthropic.com/v1/messages",
                                    json={
                                        "model": "claude-sonnet-4-6",
                                        "max_tokens": 150,
                                        "system": "You are a crypto trading analyst for Polymarket 5-min prediction markets. Be concise. 1-2 sentences max.",
                                        "messages": [
                                            {"role": "user", "content": _prompt}
                                        ],
                                    },
                                    headers={
                                        "x-api-key": self._alerter._anthropic_api_key,
                                        "anthropic-version": "2023-06-01",
                                    },
                                    timeout=_ah.ClientTimeout(total=8),
                                ) as _r:
                                    if _r.status == 200:
                                        _d2 = await _r.json()
                                        _ai = (
                                            _d2.get("content", [{}])[0]
                                            .get("text", "")
                                            .strip()
                                        )
                        except Exception:
                            pass
                    return (
                        _vpin,
                        _btc,
                        _d,
                        _regime,
                        _tsf_dir,
                        _tsf_conf,
                        _tsf_pred,
                        _tw_dir,
                        _tw_agree,
                        _cg_taker,
                        _cg_fund,
                        _ticks,
                        _ai,
                    )

                # Snapshot windows: T-240, T-210, T-180, T-150, T-120, T-90, T-70
                _snapshot_windows = [
                    ("T-240", 242, 220),
                    ("T-210", 212, 190),
                    ("T-180", 182, 160),
                    ("T-150", 152, 130),
                    ("T-120", 122, 100),
                    ("T-90", 92, 70),
                    ("T-70", 72, 55),
                ]
                for _t_lbl, _hi, _lo in _snapshot_windows:
                    if (
                        _remaining <= _hi
                        and _remaining >= _lo
                        and _t_lbl not in self._countdown_sent[_wkey]
                    ):
                        self._countdown_sent[_wkey].add(_t_lbl)
                        _elapsed_now = int(window.duration_secs - _remaining)
                        (
                            _vpin,
                            _btc,
                            _d,
                            _regime,
                            _tsf_dir,
                            _tsf_conf,
                            _tsf_pred,
                            _tw_dir,
                            _tw_agree,
                            _cg_taker,
                            _cg_fund,
                            _ticks,
                            _ai,
                        ) = await _get_full_snapshot(_t_lbl, _elapsed_now)

                        # ── Fetch fresh Gamma prices for this stage ──────────
                        _snap_gamma_up = window.up_price or 0.50
                        _snap_gamma_down = window.down_price or 0.50
                        try:
                            _slug = f"{window.asset.lower()}-updown-{_tf}-{window.window_ts}"
                            import aiohttp as _ah_gamma

                            async with _ah_gamma.ClientSession(
                                headers={"User-Agent": "Mozilla/5.0"}
                            ) as _gs:
                                async with _gs.get(
                                    f"https://gamma-api.polymarket.com/events?slug={_slug}",
                                    timeout=_ah_gamma.ClientTimeout(total=5),
                                ) as _gr:
                                    if _gr.status == 200:
                                        _gd = await _gr.json()
                                        if (
                                            _gd
                                            and isinstance(_gd, list)
                                            and _gd[0].get("markets")
                                        ):
                                            _gm = _gd[0]["markets"][0]
                                            _ba = _gm.get("bestAsk")
                                            if _ba is not None:
                                                _snap_gamma_up = round(float(_ba), 4)
                                                _snap_gamma_down = round(
                                                    1.0 - _snap_gamma_up, 4
                                                )
                        except Exception:
                            pass

                        # ── Write snapshot to countdown_evaluations DB ───────
                        try:
                            _twap_agree_bool = (
                                (_tw_agree >= 2) if _tw_agree is not None else None
                            )
                            # v7.2: fetch multi-source prices for countdown record
                            _cl_price = None
                            _ti_price = None
                            if self._db:
                                try:
                                    _cl_price = (
                                        await self._db.get_latest_chainlink_price(
                                            window.asset
                                        )
                                    )
                                except Exception:
                                    pass
                                try:
                                    _ti_price = await self._db.get_latest_tiingo_price(
                                        window.asset
                                    )
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
                            asyncio.create_task(
                                self._db.write_countdown_evaluation(
                                    {
                                        "window_ts": window.window_ts,
                                        "stage": _t_lbl,
                                        "direction": _tsf_dir
                                        or ("UP" if _d > 0 else "DOWN"),
                                        "confidence": _tsf_conf,
                                        "agreement": _twap_agree_bool,
                                        "action": "SNAPSHOT",
                                        "notes": _eval_notes,
                                        # v7.2: multi-source prices
                                        "chainlink_price": _cl_price,
                                        "tiingo_price": _ti_price,
                                        "binance_price": _btc,
                                    }
                                )
                            )
                        except Exception:
                            pass

                        if self._alerter:
                            asyncio.create_task(
                                self._alerter.send_window_snapshot(
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
                                )
                            )

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
                # v8.0: Direct evaluation — no staggered queue delay.
                # Staggered loop was for multi-asset batching (BTC+ETH+SOL).
                # BTC-only: evaluate immediately for fastest FOK execution.
                try:
                    state = await self._aggregator.get_state()

                    # Strategy Engine v2: evaluate + execute for LIVE strategies
                    if self._strategy_registry:
                        try:
                            # Build WindowMarket from window's token IDs if available
                            _v2_window_market = None
                            if getattr(window, "up_token_id", None) and getattr(
                                window, "down_token_id", None
                            ):
                                from domain.value_objects import WindowMarket

                                _v2_window_market = WindowMarket(
                                    condition_id=f"{window.asset}-{window.window_ts}",
                                    up_token_id=window.up_token_id,
                                    down_token_id=window.down_token_id,
                                    market_slug=f"{window.asset.lower()}-updown-5m-{window.window_ts}",
                                )

                            v2_decisions = await self._strategy_registry.evaluate_all(
                                window,
                                state,
                                window_market=_v2_window_market,
                                current_btc_price=float(
                                    getattr(state, "btc_price", 0) or 0
                                ),
                                open_price=float(getattr(window, "open_price", 0) or 0),
                            )
                            for d in v2_decisions:
                                log.info(
                                    "strategy_registry_v2.decision",
                                    strategy=d.strategy_id,
                                    action=d.action,
                                    direction=d.direction,
                                    skip_reason=d.skip_reason,
                                )
                        except Exception as exc:
                            log.warning(
                                "strategy_registry_v2.eval_error", error=str(exc)[:200]
                            )

                    if self._use_strategy_port and self._evaluate_strategies_uc:
                        # SP-04: Multi-strategy path
                        result = await self._evaluate_strategies_uc.execute(
                            window, state
                        )
                        # v12: Cache for sitrep + pass decisions to alerter
                        self._last_sp_result = result
                        # Build enriched decision dicts with mode info for Telegram
                        _sp_for_alert = []
                        _regs = {
                            r.strategy_id: r
                            for r, _ in self._evaluate_strategies_uc._strategies
                        }
                        for _d in result.all_decisions:
                            _reg = _regs.get(_d.strategy_id)
                            _sp_for_alert.append(
                                {
                                    "strategy_id": _d.strategy_id,
                                    "mode": _reg.mode if _reg else "?",
                                    "action": _d.action,
                                    "direction": _d.direction,
                                    "confidence": _d.confidence,
                                    "skip_reason": _d.skip_reason,
                                    "entry_cap": _d.entry_cap,
                                }
                            )
                        # Inject strategy decisions into five_min_strategy for
                        # Telegram alerts (send_trade_decision_detailed / send_window_summary)
                        if self._five_min_strategy:
                            self._five_min_strategy._pending_strategy_decisions = (
                                _sp_for_alert
                            )
                        if (
                            result.live_decision
                            and result.live_decision.action == "TRADE"
                        ):
                            live = result.live_decision
                            log.info(
                                "strategy_port.live_trade",
                                strategy=live.strategy_id,
                                direction=live.direction,
                                entry_cap=live.entry_cap,
                            )
                            # Pass the port's decision directly to execution.
                            # We set _sp_trade_decision on five_min_strategy so
                            # _evaluate_window() sees it and skips re-evaluation,
                            # going straight to _execute_trade().
                            if self._five_min_strategy:
                                self._five_min_strategy._sp_trade_decision = {
                                    "direction": live.direction,
                                    "entry_cap": live.entry_cap or 0.65,
                                    "confidence_score": live.confidence_score,
                                    "strategy_id": live.strategy_id,
                                }
                            await self._five_min_strategy.evaluate_window(window, state)
                        else:
                            log.info(
                                "strategy_port.no_live_trade",
                                decisions=len(result.all_decisions),
                            )
                            # Write window_snapshot so close_price is tracked for analysis.
                            # Without this, strategy_port skips evaluate_window entirely on
                            # no-trade ticks, leaving window_snapshots empty.
                            _ctx = result.context
                            if self._db and _ctx:
                                asyncio.create_task(
                                    self._db.write_window_snapshot(
                                        {
                                            "window_ts": getattr(
                                                window, "window_ts", 0
                                            ),
                                            "asset": getattr(window, "asset", "BTC"),
                                            "timeframe": "5m",
                                            "open_price": getattr(
                                                window, "open_price", 0
                                            ),
                                            "close_price": _ctx.current_price or 0,
                                            "btc_price": _ctx.current_price or 0,
                                            "eval_offset": getattr(
                                                window, "eval_offset", None
                                            ),
                                            "vpin": _ctx.vpin,
                                            "regime": _ctx.regime,
                                            "delta_pct": _ctx.delta_pct,
                                            "delta_source": _ctx.delta_source,
                                            "v2_probability_up": (
                                                _ctx.v4_snapshot.probability_up
                                                if _ctx.v4_snapshot
                                                else None
                                            ),
                                            "v2_direction": (
                                                "DOWN"
                                                if (
                                                    _ctx.v4_snapshot
                                                    and (
                                                        _ctx.v4_snapshot.probability_up
                                                        or 0.5
                                                    )
                                                    < 0.5
                                                )
                                                else "UP"
                                            )
                                            if _ctx.v4_snapshot
                                            else None,
                                        }
                                    )
                                )
                            # v12: Send strategy comparison alert ONCE per window at window close.
                            # Only fire when eval_offset is low (window ending) to avoid
                            # spamming Telegram on every 2-second evaluation tick.
                            _eval_offset = getattr(window, "eval_offset", 300) or 300
                            _is_closing = (
                                _eval_offset <= 30
                            )  # last 30 seconds of window
                            if self._alerter and _sp_for_alert and _is_closing:
                                _window_key = f"{window.asset}-{window.window_ts}"
                                _ctx = result.context
                                _skip_reasons = "; ".join(
                                    f"{d['strategy_id']}: {d.get('skip_reason', '?')[:50]}"
                                    for d in _sp_for_alert
                                    if d.get("action") != "TRADE"
                                )
                                try:
                                    await self._alerter.send_window_summary(
                                        window_id=_window_key,
                                        eval_history=[
                                            {
                                                "offset": _eval_offset,
                                                "skip_reason": _skip_reasons[:120],
                                                "vpin": _ctx.vpin if _ctx else 0,
                                                "delta_pct": _ctx.delta_pct
                                                if _ctx
                                                else 0,
                                                "regime": _ctx.regime if _ctx else "?",
                                            }
                                        ],
                                        traded=False,
                                        strategy_decisions=_sp_for_alert,
                                    )
                                except Exception as _se:
                                    log.warning(
                                        "strategy_port.skip_alert_failed",
                                        error=str(_se)[:100],
                                    )
                    else:
                        # Legacy path
                        await self._five_min_strategy.evaluate_window(window, state)
                except Exception as exc:
                    log.warning(
                        "five_min.direct_eval_error",
                        asset=window.asset,
                        error=str(exc)[:200],
                    )
            elif state_value != "ACTIVE":
                log.info(
                    "five_min.skip_evaluation",
                    reason="not_CLOSING_state",
                    state=state_value,
                )
            self._five_min_strategy.trim_recent_windows(20)

    async def _on_fifteen_min_window(self, window) -> None:
        """Handle 15-minute window signal — same strategy, different timeframe.

        G1 & G3: Collect windows for staggered, single-best-signal execution.
        """
        window_state = getattr(window, "state", None)
        state_value = (
            window_state.value
            if hasattr(window_state, "value")
            else str(window_state)
            if window_state
            else "NO_STATE"
        )
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
            self._five_min_strategy.append_pending_window(window)
            self._five_min_strategy.append_recent_window(window)

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
                    _up_ratio = (
                        window.up_price / (window.up_price + window.down_price)
                        if (window.up_price + window.down_price) > 0
                        else 0.5
                    )
                    _implied_delta = (_up_ratio - 0.5) * 0.002
                    _proxy_price = window.open_price * (1 + _implied_delta)
                self._twap_tracker.add_tick(
                    window.asset, window.window_ts, _proxy_price
                )

            # ONLY evaluate at T-60s (CLOSING state), NOT at window open
            if state_value == "CLOSING":
                # G1 & G3: Queue window for staggered execution instead of immediate eval
                await self._execution_queue.put((window, self._aggregator))

                # v5.8: TimesFM checked inside v5.7c agreement (no standalone)
            else:
                log.info(
                    "fifteen_min.skip_evaluation",
                    reason="not_CLOSING_state",
                    state=state_value,
                )
            self._five_min_strategy.trim_recent_windows(20)

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
                    self._timesfm_strategy._recent_windows = (
                        self._timesfm_strategy._recent_windows[-20:]
                    )
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

            # Track this resolution so _position_monitor_loop skips duplicate notification
            meta_pre = order.metadata or {}
            _resolved_token = meta_pre.get("token_id")
            if _resolved_token:
                self._resolved_by_order_manager.add(_resolved_token)
                log.info(
                    "resolution.dedup_tracked",
                    token_id=_resolved_token[:20] + "..."
                    if len(_resolved_token) > 20
                    else _resolved_token,
                )

            # Update risk manager with PnL (bankroll, daily PnL, drawdown, consecutive losses)
            async def _record_and_alert():
                try:
                    await self._risk_manager.record_outcome(order.pnl_usd)
                    log.info(
                        "resolution.pnl_recorded",
                        order_id=order.order_id[:20],
                        pnl=f"${order.pnl_usd:.2f}",
                    )
                except Exception as exc:
                    log.error("resolution.pnl_record_failed", error=str(exc))

                if filled or is_paper_mode:
                    try:
                        meta = order.metadata or {}
                        _window_id = f"{meta.get('asset', 'BTC')}-{int(meta.get('window_ts', 0))}"
                        _direction = "UP" if order.direction == "YES" else "DOWN"
                        _open_p = meta.get("window_open_price", 0) or 0
                        _close_p = float(self._order_manager._current_btc_price or 0)
                        # Determine actual direction from oracle outcome, not live price
                        if order.outcome == "WIN":
                            _actual = (
                                _direction  # We won = oracle agreed with our direction
                            )
                        else:
                            _actual = (
                                "DOWN" if _direction == "UP" else "UP"
                            )  # We lost = oracle went opposite
                        _delta = (_close_p - _open_p) / _open_p * 100 if _open_p else 0
                        _vpin = (
                            self._five_min_strategy.current_vpin
                            if self._five_min_strategy
                            else 0
                        )
                        # Win streak from risk manager
                        _rs = self._risk_manager.get_status()
                        _streak_w = (
                            _rs.get("win_streak", 0) if order.outcome == "WIN" else 0
                        )
                        _streak_l = (
                            _rs.get("loss_streak", 0) if order.outcome != "WIN" else 0
                        )
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
                            entry_reason=meta.get("entry_reason"),
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
                                    "timesfm_confidence": meta.get(
                                        "timesfm_confidence", 0
                                    ),
                                    "twap_direction": meta.get("twap_direction"),
                                    "twap_agreement": meta.get("twap_agreement_score"),
                                    "gamma_up": meta.get("gamma_up_price"),
                                    "gamma_down": meta.get("gamma_down_price"),
                                    "cg_data": meta.get("cg_modifier_reason", ""),
                                    # v8.0 fields
                                    "delta_source": meta.get("delta_source", "?"),
                                    "delta_pct": meta.get("delta_pct", 0),
                                    "actual_direction": _oc,  # Oracle resolved direction
                                }

                                async def _send_outcome_ai(
                                    _wid=_wid,
                                    _dir=_dir,
                                    _ep=_ep,
                                    _oc=_oc,
                                    _pnl=_pnl,
                                    _oid=_oid,
                                    _wd=_wd,
                                ):
                                    try:
                                        result = await self._alerter.send_outcome_with_analysis(
                                            window_id=_wid,
                                            decision=_dir,
                                            entry_price=_ep,
                                            outcome=_oc,
                                            pnl_usd=_pnl,
                                            window_data=_wd,
                                        )
                                        log.debug(
                                            "outcome_ai.sent",
                                            order_id=_oid,
                                            result_type=type(result).__name__,
                                        )
                                    except Exception as exc:
                                        log.error(
                                            "outcome_analysis_failed",
                                            order_id=_oid,
                                            error=str(exc)[:100],
                                        )

                                asyncio.create_task(_send_outcome_ai())
                            except Exception as exc:
                                log.error(
                                    "outcome_ai_spawn_failed", error=str(exc)[:100]
                                )

                        log.info("resolution.alert_sent", order_id=order.order_id[:20])
                    except Exception as exc:
                        log.error(
                            "resolution.alert_failed",
                            order_id=order.order_id[:20],
                            error=str(exc),
                        )

            asyncio.create_task(_record_and_alert())

    # ─── Background Tasks ─────────────────────────────────────────────────────

    async def _poly_fills_loop(self) -> None:
        """v11: Periodic poly_fills reconciliation from Polymarket data-api.

        Runs every 5 minutes (configurable via POLY_FILLS_SYNC_INTERVAL_S).
        Appends new fills to poly_fills, links orphans to trade_bible,
        enriches trade_bible.condition_id + market_slug. Idempotent and
        safe to run repeatedly.

        If the reconciler isn't initialized (e.g. no db pool), this loop
        exits immediately.
        """
        interval = float(os.environ.get("POLY_FILLS_SYNC_INTERVAL_S", "300"))
        lookback_hours = float(os.environ.get("POLY_FILLS_LOOKBACK_HOURS", "2"))

        if not getattr(self, "_poly_fills_reconciler", None):
            log.info("poly_fills_loop.disabled_no_reconciler")
            return

        # Initial delay so we don't hammer the data-api on startup
        await asyncio.sleep(30)

        while not self._shutdown_event.is_set():
            try:
                result = await self._poly_fills_reconciler.sync(hours=lookback_hours)
                if result.get("inserted", 0) or result.get("linked", 0):
                    log.info("poly_fills_loop.sync_result", **result)
            except Exception as exc:
                log.warning("poly_fills_loop.sync_failed", error=str(exc)[:200])

            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

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
                            _cached_wallet_balance = (
                                await self._poly_client.get_balance()
                            )
                            await self._risk_manager.sync_bankroll(
                                _cached_wallet_balance
                            )
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

                            if not want_paper:
                                _live_gate = (
                                    os.environ.get("LIVE_TRADING_ENABLED", "")
                                    .strip()
                                    .lower()
                                )
                                if _live_gate != "true":
                                    try:
                                        _env_file = os.path.join(
                                            os.path.dirname(os.path.dirname(__file__)),
                                            ".env",
                                        )
                                        with open(
                                            _env_file, "r", encoding="utf-8"
                                        ) as fh:
                                            for line in fh:
                                                if line.startswith(
                                                    "LIVE_TRADING_ENABLED="
                                                ):
                                                    _live_gate = (
                                                        line.split("=", 1)[1]
                                                        .strip()
                                                        .lower()
                                                    )
                                                    break
                                    except Exception:
                                        pass
                                if _live_gate != "true":
                                    log.error(
                                        "mode_switch.live_gate_disabled",
                                        requested_mode=new_mode,
                                    )
                                    if self._alerter:
                                        await self._alerter.send_system_alert(
                                            "LIVE mode requested but LIVE_TRADING_ENABLED is not set. "
                                            "Keeping engine in PAPER mode.",
                                            level="critical",
                                        )
                                    continue

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
                            if not want_paper:
                                try:
                                    _live_wallet = await self._poly_client.get_balance()
                                    await self._risk_manager.rebaseline_live_bankroll(
                                        _live_wallet
                                    )
                                except Exception as exc:
                                    log.error(
                                        "mode_switch.live_risk_rebaseline_failed",
                                        error=str(exc)[:200],
                                    )

                            # Keep registry execution aligned with runtime mode.
                            if (
                                self._strategy_registry
                                and getattr(
                                    self._strategy_registry, "_execute_uc", None
                                )
                                is not None
                            ):
                                try:
                                    from adapters.execution.paper_executor import (
                                        PaperExecutor,
                                    )
                                    from adapters.execution.fak_ladder_executor import (
                                        FAKLadderExecutor,
                                    )

                                    _execute_uc = self._strategy_registry._execute_uc
                                    _execute_uc._paper_mode = want_paper
                                    _execute_uc._executor = (
                                        PaperExecutor()
                                        if want_paper
                                        else FAKLadderExecutor(
                                            poly_client=self._poly_client
                                        )
                                    )
                                    log.info(
                                        "mode_switch.execute_uc_rewired",
                                        paper_mode=want_paper,
                                        executor=(
                                            "PaperExecutor"
                                            if want_paper
                                            else "FAKLadderExecutor"
                                        ),
                                    )
                                except Exception as exc:
                                    log.error(
                                        "mode_switch.execute_uc_rewire_failed",
                                        error=str(exc)[:200],
                                    )

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
                                    log.error(
                                        "mode_switch.clob_connect_failed",
                                        error=str(exc)[:100],
                                    )

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
                                    log.info(
                                        "orchestrator.redeemer_started_on_mode_switch"
                                    )
                                except Exception as exc:
                                    log.error(
                                        "orchestrator.redeemer_start_failed",
                                        error=str(exc),
                                    )
                except Exception as exc:
                    log.debug("mode_sync.failed", error=str(exc)[:80])

                await self._db.update_feed_status(
                    # Both futures + spot feeds must be connected for "binance" healthy
                    binance=self._binance_feed.connected
                    and self._binance_spot_feed.connected,
                    coinglass=self._coinglass_feed.connected
                    if self._coinglass_feed
                    else False,
                    chainlink=self._chainlink_feed.connected
                    if self._chainlink_feed
                    else False,
                    polymarket=self._polymarket_feed.connected,
                    opinion=self._opinion_client.connected,
                )

                # Update venue connectivity in risk manager
                await self._risk_manager.update_venue_status(
                    polymarket=self._polymarket_feed.connected,
                    opinion=self._opinion_client.connected,
                )

                # ── 5-Minute Sitrep to Telegram ──────────────────────────
                # Skip old SITREP when Strategy Engine v2 is active
                _v2_sitrep_active = (
                    os.environ.get("LEGACY_EXECUTION_DISABLED", "").lower() == "true"
                )
                _sitrep_counter += 1
                if (
                    _sitrep_counter >= 30 and not _v2_sitrep_active
                ):  # 30 × 10s = 5 minutes
                    _sitrep_counter = 0
                    try:
                        om_total = self._order_manager.total_orders
                        om_resolved = self._order_manager.resolved_orders
                        om_open = len(open_orders)
                        wallet = (
                            _cached_wallet_balance
                            or risk_status.get("current_bankroll", 0)
                            or 0
                        )
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

                        # Fetch position outcomes from DB (survives restarts)
                        real_wins = 0
                        real_losses = 0
                        open_positions_val = 0
                        try:
                            if self._db._pool:
                                async with self._db._pool.acquire() as conn:
                                    # Use trade_bible as source of truth (includes reconciler-resolved orphans)
                                    # v12: Include paper trades when in paper mode (was filtering them out)
                                    _live_filter = (
                                        "AND is_live = true"
                                        if not self._settings.paper_mode
                                        else ""
                                    )
                                    row = await conn.fetchrow(
                                        "SELECT "
                                        "  COUNT(*) FILTER (WHERE trade_outcome='WIN') as w, "
                                        "  COUNT(*) FILTER (WHERE trade_outcome='LOSS') as l "
                                        f"FROM trade_bible WHERE 1=1 {_live_filter} "
                                        "AND resolved_at > DATE_TRUNC('day', NOW())"
                                    )
                                    if row:
                                        real_wins = int(row["w"] or 0)
                                        real_losses = int(row["l"] or 0)
                            # Open positions from order manager
                            for oid, o in self._order_manager._orders.items():
                                if o.status.value in ("OPEN", "FILLED"):
                                    open_positions_val += o.stake_usd
                        except Exception:
                            pass

                        mode_label = (
                            "📄 PAPER" if self._settings.paper_mode else "🔴 LIVE"
                        )

                        # Build CoinGlass block for sitrep
                        cg_block = ""
                        try:
                            cg_snapshot = (
                                self._cg_enhanced.snapshot
                                if self._cg_enhanced is not None
                                else None
                            )
                            cg_block = (
                                self._alerter.format_coinglass_block(cg_snapshot) + "\n"
                            )
                        except Exception:
                            pass

                        # ── P&L: use risk manager bankroll (synced from wallet in live) ──
                        baseline = self._settings.starting_bankroll
                        portfolio = bankroll + open_positions_val
                        real_pnl = daily_pnl  # Use today's tracked P&L, not lifetime

                        # Live mode: wallet is the real USDC balance
                        if not self._settings.paper_mode and wallet and wallet > 0:
                            portfolio = wallet + open_positions_val

                        # ── Regime label (prefer HMM from V4 snapshot when available) ──
                        vpin_regime = (
                            "CASCADE"
                            if vpin >= runtime.vpin_cascade_direction_threshold
                            else "TRANSITION"
                            if vpin >= runtime.vpin_informed_threshold
                            else "NORMAL"
                            if vpin >= runtime.five_min_vpin_gate
                            else "CALM"
                        )
                        # v12: HMM regime from latest strategy port context
                        _hmm_regime = None
                        _hmm_confidence = None
                        if self._last_sp_result and self._last_sp_result.context:
                            _v4s = getattr(
                                self._last_sp_result.context, "v4_snapshot", None
                            )
                            if _v4s:
                                _hmm_regime = getattr(_v4s, "regime", None)
                                _hmm_confidence = getattr(
                                    _v4s, "regime_confidence", None
                                )

                        # v8.1: Enhanced SITREP with recent trades + pending positions
                        _recent_block = ""
                        _pending_block = ""
                        _pnl_from_wallet = 0.0
                        try:
                            if self._db._pool:
                                async with self._db._pool.acquire() as conn:
                                    # Recent 5 trades
                                    _recent = await conn.fetch(
                                        """SELECT direction, outcome, status,
                                           metadata->>'entry_reason' as reason,
                                           metadata->>'v81_entry_cap' as cap,
                                           metadata->>'window_ts' as wts,
                                           created_at AT TIME ZONE 'UTC' as placed,
                                           ROUND(stake_usd::numeric, 2) as stake,
                                           ROUND(pnl_usd::numeric, 2) as pnl
                                        FROM trades WHERE created_at > NOW() - INTERVAL '2 hours'
                                        ORDER BY created_at DESC LIMIT 5"""
                                    )
                                    if _recent:
                                        _lines = []
                                        for r in _recent:
                                            _dir = (
                                                "⬆️" if r["direction"] == "YES" else "⬇️"
                                            )
                                            _out = {"WIN": "✅", "LOSS": "❌"}.get(
                                                r["outcome"] or "", "⏳"
                                            )
                                            _cap = (
                                                f"${float(r['cap']):.2f}"
                                                if r["cap"]
                                                else "?"
                                            )
                                            _rsn = (r["reason"] or "?")[-25:]
                                            # Window end time as ID (e.g. "14:10 BTC")
                                            _wid = ""
                                            try:
                                                from datetime import datetime, timezone

                                                _wts = (
                                                    int(r["wts"]) + 300
                                                )  # window_ts + 5min = close time
                                                _wid = datetime.fromtimestamp(
                                                    _wts, tz=timezone.utc
                                                ).strftime("%H:%M")
                                            except Exception:
                                                try:
                                                    _wid = r["placed"].strftime("%H:%M")
                                                except Exception:
                                                    pass
                                            if r["outcome"]:
                                                _p = float(r["pnl"] or 0)
                                                _pstr = f"`{'+' if _p >= 0 else ''}${_p:.2f}`"
                                            elif r["status"] == "EXPIRED":
                                                _pstr = "unfilled"
                                                _out = "⏭"
                                            elif r["status"] == "SKIPPED":
                                                _pstr = f"skip: {(r.get('skip_reason') or '?')[:30]}"
                                                _out = "🚫"
                                            else:
                                                _pstr = "⏳open"
                                            _lines.append(
                                                f"{_out}{_dir} `{_wid}` {_cap} {_rsn} {_pstr}"
                                            )
                                        _recent_block = (
                                            "\n📝 *Recent trades:*\n"
                                            + "\n".join(_lines)
                                            + "\n"
                                        )

                                    # Recent strategy decisions — show BOTH LIVE and GHOST
                                    # per window so operator sees V4 (paper) vs V10 (ghost) side-by-side
                                    _sp_rows = await conn.fetch(
                                        """SELECT DISTINCT ON (window_ts, strategy_id)
                                               strategy_id, mode, action, direction,
                                               skip_reason, eval_offset, window_ts,
                                               metadata_json::jsonb->'_ctx'->>'v4_regime' AS regime
                                           FROM strategy_decisions
                                           WHERE window_ts > EXTRACT(EPOCH FROM NOW() - INTERVAL '15 minutes')::bigint
                                           ORDER BY window_ts DESC, strategy_id, eval_offset ASC
                                           LIMIT 10"""
                                    )
                                    if _sp_rows:
                                        from datetime import datetime, timezone as _tz

                                        _by_window: dict = {}
                                        for _r in _sp_rows:
                                            _wts = _r["window_ts"]
                                            if _wts not in _by_window:
                                                _by_window[_wts] = {}
                                            _by_window[_wts][_r["strategy_id"]] = _r

                                        _slines = []
                                        for _wts, _strats in sorted(
                                            _by_window.items(), reverse=True
                                        )[:3]:
                                            try:
                                                _wid = datetime.fromtimestamp(
                                                    int(_wts) + 300, tz=_tz.utc
                                                ).strftime("%H:%M")
                                            except Exception:
                                                _wid = "?"
                                            _parts = []
                                            for _sid, _sr in sorted(_strats.items()):
                                                _label = (
                                                    "📄"
                                                    if _sr["mode"] == "LIVE"
                                                    else "👻"
                                                )
                                                _action = (
                                                    "✅"
                                                    if _sr["action"] == "TRADE"
                                                    else "🚫"
                                                )
                                                _dir = (
                                                    "⬆️"
                                                    if _sr.get("direction") == "UP"
                                                    else (
                                                        "⬇️"
                                                        if _sr.get("direction")
                                                        == "DOWN"
                                                        else ""
                                                    )
                                                )
                                                _reason = (
                                                    _sr.get("skip_reason") or "?"
                                                )[:40]
                                                _regime = (_sr.get("regime") or "")[:8]
                                                _parts.append(
                                                    f"{_label}{_action}{_dir} {_sid[:4]}: {_reason}"
                                                    + (
                                                        f" [{_regime}]"
                                                        if _regime
                                                        else ""
                                                    )
                                                )
                                            _slines.append(
                                                f"`{_wid}` " + " | ".join(_parts)
                                            )

                                        _recent_block += (
                                            "📝 *Recent decisions (📄=live paper, 👻=ghost):*\n"
                                            + "\n".join(_slines)
                                            + "\n"
                                        )
                                    else:
                                        # Fall back to legacy window_snapshots skip display
                                        _skips = await conn.fetch(
                                            """SELECT DISTINCT ON (window_ts) direction, skip_reason,
                                               window_ts, ROUND(vpin::numeric, 3) as vpin
                                            FROM window_snapshots
                                            WHERE trade_placed = false AND skip_reason IS NOT NULL
                                              AND window_ts > EXTRACT(EPOCH FROM NOW() - INTERVAL '15 minutes')
                                            ORDER BY window_ts DESC, id DESC
                                            LIMIT 3"""
                                        )
                                        if _skips:
                                            _slines = []
                                            for s in _skips:
                                                _dir = (
                                                    "⬆️"
                                                    if s["direction"] == "UP"
                                                    else "⬇️"
                                                )
                                                try:
                                                    from datetime import (
                                                        datetime,
                                                        timezone as _tz2,
                                                    )

                                                    _wts = int(s["window_ts"]) + 300
                                                    _wid = datetime.fromtimestamp(
                                                        _wts, tz=_tz2.utc
                                                    ).strftime("%H:%M")
                                                except Exception:
                                                    _wid = "?"
                                                _sr = (s["skip_reason"] or "?")[:45]
                                                _slines.append(
                                                    f"🚫{_dir} `{_wid}` {_sr}"
                                                )
                                            _recent_block += (
                                                "📝 *Recent skips:*\n"
                                                + "\n".join(_slines)
                                                + "\n"
                                            )

                                    # ── Recent wins and losses from trade_bible (source of truth) ──
                                    #
                                    # Filter by resolution_source so we don't
                                    # display startup-backfilled (stale) trades
                                    # mixed with fresh fills. Values set by
                                    # engine/reconciliation/reconciler.py:
                                    #   'trigger'          — live-engine resolution via DB trigger
                                    #                        (the current-session decision path)
                                    #   'orphan_resolved'  — CLOB reconciler found a fill the engine
                                    #                        didn't locally track, but still in-session
                                    #   'backfill'         — reconciler startup backfill (pre-restart,
                                    #                        stale — exclude from "recent" display)
                                    #   'trades_table'     — historical populate_trade_bible.sql batch
                                    #   NULL               — legacy rows without source tagging
                                    #
                                    # Show 'trigger' and 'orphan_resolved' (both
                                    # are current-session); optionally mark
                                    # orphan-resolved lines with a ⚙ glyph so
                                    # the operator can tell them apart.
                                    _wl_block = ""
                                    try:
                                        _wins = await conn.fetch(
                                            """SELECT direction, ROUND(pnl_usd::numeric, 2) as pnl,
                                               entry_reason as reason, resolved_at,
                                               COALESCE(resolution_source, '') as source
                                            FROM trade_bible WHERE trade_outcome = 'WIN' AND is_live = true
                                              AND resolved_at > NOW() - INTERVAL '6 hours'
                                              AND COALESCE(resolution_source, '') IN ('trigger', 'orphan_resolved', '')
                                            ORDER BY resolved_at DESC LIMIT 3"""
                                        )
                                        _losses = await conn.fetch(
                                            """SELECT direction, ROUND(pnl_usd::numeric, 2) as pnl,
                                               entry_reason as reason, resolved_at,
                                               COALESCE(resolution_source, '') as source
                                            FROM trade_bible WHERE trade_outcome = 'LOSS' AND is_live = true
                                              AND resolved_at > NOW() - INTERVAL '6 hours'
                                              AND COALESCE(resolution_source, '') IN ('trigger', 'orphan_resolved', '')
                                            ORDER BY resolved_at DESC LIMIT 3"""
                                        )

                                        def _label_prefix(source: str) -> str:
                                            """Visual marker so operators can tell at a glance
                                            whether a line came from a live-engine resolution or
                                            from the orphan reconciler catching up on a missing fill."""
                                            return (
                                                "⚙"
                                                if source == "orphan_resolved"
                                                else ""
                                            )

                                        if _wins:
                                            _wlines = []
                                            for w in _wins:
                                                _d = (
                                                    "⬆️"
                                                    if w["direction"] == "YES"
                                                    else "⬇️"
                                                )
                                                _t = ""
                                                try:
                                                    _t = w["resolved_at"].strftime(
                                                        "%H:%M"
                                                    )
                                                except Exception:
                                                    pass
                                                _r = (w["reason"] or "?")[-20:]
                                                _mark = _label_prefix(w["source"])
                                                _wlines.append(
                                                    f"✅{_d}{_mark} `{_t}` `+${float(w['pnl']):.2f}` {_r}"
                                                )
                                            _wl_block += (
                                                "🏆 *Recent wins:*\n"
                                                + "\n".join(_wlines)
                                                + "\n"
                                            )
                                        if _losses:
                                            _llines = []
                                            for l in _losses:
                                                _d = (
                                                    "⬆️"
                                                    if l["direction"] == "YES"
                                                    else "⬇️"
                                                )
                                                _t = ""
                                                try:
                                                    _t = l["resolved_at"].strftime(
                                                        "%H:%M"
                                                    )
                                                except Exception:
                                                    pass
                                                _r = (l["reason"] or "?")[-20:]
                                                _mark = _label_prefix(l["source"])
                                                _llines.append(
                                                    f"❌{_d}{_mark} `{_t}` `-${abs(float(l['pnl'])):.2f}` {_r}"
                                                )
                                            _wl_block += (
                                                "💀 *Recent losses:*\n"
                                                + "\n".join(_llines)
                                                + "\n"
                                            )
                                    except Exception:
                                        pass
                                    _recent_block += _wl_block

                                    # Pending positions (OPEN/FILLED not yet resolved)
                                    _pending = await conn.fetch(
                                        """SELECT direction, metadata->>'v81_entry_cap' as cap,
                                           metadata->>'entry_reason' as reason,
                                           metadata->>'window_ts' as wts,
                                           ROUND(stake_usd::numeric, 2) as stake,
                                           metadata->>'market_slug' as slug
                                        FROM trades WHERE status IN ('OPEN', 'FILLED')
                                        AND created_at > NOW() - INTERVAL '1 hour'
                                        ORDER BY created_at"""
                                    )
                                    if _pending:
                                        _plines = []
                                        _total_risk = 0
                                        _total_upside = 0
                                        for p in _pending:
                                            _dir = (
                                                "⬆️" if p["direction"] == "YES" else "⬇️"
                                            )
                                            _cap = float(p["cap"]) if p["cap"] else 0.73
                                            _stk = float(p["stake"])
                                            _win_est = _stk * (1 - _cap) / _cap
                                            _total_risk += _stk
                                            _total_upside += _win_est
                                            _wid = ""
                                            try:
                                                from datetime import datetime, timezone

                                                _wts = int(p["wts"]) + 300
                                                _wid = datetime.fromtimestamp(
                                                    _wts, tz=timezone.utc
                                                ).strftime("%H:%M")
                                            except Exception:
                                                pass
                                            _plines.append(
                                                f"{_dir} `{_wid} BTC` ${_cap:.2f} risk `${_stk:.2f}` → win `+${_win_est:.2f}`"
                                            )
                                        _pending_block = (
                                            f"\n⏳ *Pending ({len(_pending)}):*\n"
                                            + "\n".join(_plines)
                                            + f"\nIf all win: `+${_total_upside:.2f}` | If all lose: `-${_total_risk:.2f}`\n"
                                        )

                                    # Real P&L from wallet
                                    _pnl_from_wallet = wallet - baseline
                        except Exception:
                            pass

                        _wr = (
                            (real_wins / (real_wins + real_losses) * 100)
                            if (real_wins + real_losses) > 0
                            else 0
                        )

                        # v12: Build HMM regime line
                        _hmm_line = ""
                        if _hmm_regime:
                            _hmm_display = _hmm_regime.replace("_", " ").title()
                            _hmm_conf_s = (
                                f" `{_hmm_confidence:.0%}`"
                                if _hmm_confidence is not None
                                else ""
                            )
                            _hmm_line = f"🧠 HMM: `{_hmm_display}`{_hmm_conf_s}\n"

                        # v12: Build feed health line
                        _feeds = []
                        try:
                            _feeds.append(
                                f"BN-F:{'✓' if self._binance_feed.connected else '✗'}"
                            )
                            _feeds.append(
                                f"BN-S:{'✓' if self._binance_spot_feed.connected else '✗'}"
                            )
                            if self._chainlink_feed:
                                _feeds.append(
                                    f"CL:{'✓' if self._chainlink_feed.connected else '✗'}"
                                )
                            if self._tiingo_feed:
                                _feeds.append(
                                    f"TI:{'✓' if self._tiingo_feed.connected else '✗'}"
                                )
                            if self._coinglass_feed:
                                _feeds.append(
                                    f"CG:{'✓' if self._coinglass_feed.connected else '✗'}"
                                )
                            _feeds.append(
                                f"PM:{'✓' if self._polymarket_feed.connected else '✗'}"
                            )
                            if self._clob_feed:
                                _feeds.append(
                                    f"CLOB:{'✓' if self._clob_feed.connected else '✗'}"
                                )
                        except Exception:
                            pass
                        _feed_line = (
                            f"📡 Feeds: `{' '.join(_feeds)}`\n" if _feeds else ""
                        )

                        # v12: Build strategy port summary
                        _sp_block = ""
                        _v4_mode_label = ""
                        if self._last_sp_result and self._last_sp_result.all_decisions:
                            try:
                                _regs_map = (
                                    {
                                        r.strategy_id: r
                                        for r, _ in self._evaluate_strategies_uc._strategies
                                    }
                                    if self._evaluate_strategies_uc
                                    else {}
                                )
                                # Build V4 mode label for header (LIVE vs GHOST)
                                _v4_reg = _regs_map.get("v4_fusion")
                                if _v4_reg:
                                    _v4_mode_label = (
                                        " | V4:🎯LIVE"
                                        if _v4_reg.mode == "LIVE"
                                        else " | V4:👻GHOST"
                                    )
                                _sp_lines = []
                                for _sd in self._last_sp_result.all_decisions:
                                    _sid = getattr(_sd, "strategy_id", "?")
                                    _action = getattr(_sd, "action", "?")
                                    _dir = getattr(_sd, "direction", None)
                                    _skip = getattr(_sd, "skip_reason", None)
                                    _mode = (
                                        _regs_map[_sid].mode
                                        if _sid in _regs_map
                                        else "?"
                                    )
                                    _icon = "🎯" if _mode == "LIVE" else "👻"
                                    if _action == "TRADE":
                                        _sp_lines.append(
                                            f"{_icon}`{_sid}`: TRADE `{_dir}`"
                                        )
                                    elif _action == "SKIP":
                                        _sp_lines.append(
                                            f"{_icon}`{_sid}`: SKIP _{(_skip or '?')[:40]}_"
                                        )
                                    else:
                                        _sp_lines.append(
                                            f"{_icon}`{_sid}`: `{_action}`"
                                        )
                                _sp_block = (
                                    "🔬 *Last window:* " + " | ".join(_sp_lines) + "\n"
                                )
                            except Exception:
                                pass

                        sitrep = (
                            f"📋 *5-MIN SITREP* ({status_emoji} {'KILLED' if killed else 'ACTIVE'}) {mode_label}{_v4_mode_label}\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            + (
                                f"🏦 Wallet: `${wallet:.2f}` USDC _(CLOB verified)_\n"
                                f"📈 P&L: `${_pnl_from_wallet:+.2f}` from `${baseline:.0f}` start\n"
                                if not self._settings.paper_mode
                                else f"💰 Bankroll: `${bankroll:.2f}`\n"
                            )
                            + f"\n"
                            f"📊 *24h Record:* `{real_wins}W/{real_losses}L` (`{_wr:.0f}%` WR)\n"
                            + _recent_block
                            + _pending_block
                            + f"\n"
                            f"🔬 VPIN: `{vpin:.4f}` | `{vpin_regime}`\n"
                            + _hmm_line
                            + _feed_line
                            + _sp_block
                            + cg_block
                            + f"BTC: `${self._order_manager._current_btc_price:,.2f}`\n"
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
                            log.warning("reconcile.api_error", status=resp.status)
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

                            meta = (
                                _json.loads(row["metadata"]) if row["metadata"] else {}
                            )
                        except Exception:
                            pass
                        if meta.get("market_slug") == slug or slug in (
                            meta.get("market_slug", "")
                        ):
                            matched_row = row
                            matched_order_id = oid
                            break

                    if matched_row is None:
                        continue

                    # Check if fill price is wrong
                    db_price = (
                        float(matched_row["entry_price"])
                        if matched_row["entry_price"]
                        else None
                    )
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
                                f"{k} = ${i + 2}"
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
                                order_id=matched_order_id[:20]
                                if matched_order_id
                                else "?",
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
                log.warning("orchestrator.reconcile_loop.error", error=str(exc)[:120])

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
                    msg = f"Redeemed {result['redeemed']} position(s) via Playwright"
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

    def _on_manual_trade_notify(
        self,
        conn,
        pid: int,
        channel: str,
        payload: str,
    ) -> None:
        """LT-04: asyncpg callback fired when the hub emits pg_notify
        on 'manual_trade_pending'. The callback runs on the LISTEN
        connection's read loop and must be non-blocking — we just set
        an asyncio.Event that the poll loop awaits.

        The payload is the trade_id that the hub just INSERTed, but we
        don't actually need to parse it: the poll loop re-fetches all
        rows with status='pending_live' every time the event fires, so
        a lost or misrouted NOTIFY still falls through to the poll-based
        safety net.
        """
        try:
            self._manual_trade_notify_event.set()
            log.debug(
                "manual_trade.notify_received",
                channel=channel,
                payload=(payload[:32] if payload else ""),
            )
        except Exception as exc:
            # Never let a logging / event error kill the LISTEN connection.
            log.warning("manual_trade.notify_callback_error", error=str(exc))

    async def _manual_trade_poller(self) -> None:
        """Poll DB for manual 'pending_live' trades from the dashboard and execute them.

        The hub API writes trades with status='pending_live' when Billy clicks
        the Live Trade button. This poller picks them up and submits FOK orders
        to Polymarket via the poly_client.

        LT-04: hybrid LISTEN/NOTIFY + poll fast path.
          1. On startup, subscribe to the 'manual_trade_pending' channel
             via a dedicated asyncpg connection. The hub emits
             pg_notify('manual_trade_pending', trade_id) after the INSERT
             commit in v58_monitor.py::post_manual_trade.
          2. The main loop awaits self._manual_trade_notify_event with
             a 1s timeout. On NOTIFY the event fires immediately
             (latency ~tens of ms); on timeout we fall through to the
             periodic poll as the safety net.
          3. If the LISTEN connection dies, ensure_listening re-opens
             it on the next tick. Meanwhile the 1s poll still picks up
             trades — zero regression vs the pre-LT-04 behavior.
        """
        log.info("manual_trade_poller.started")

        # LT-04: subscribe to the notify channel. Failure here is
        # non-fatal — the 1s fall-through poll still works.
        try:
            from persistence.db_client import MANUAL_TRADE_NOTIFY_CHANNEL

            await self._db.ensure_listening(
                MANUAL_TRADE_NOTIFY_CHANNEL,
                self._on_manual_trade_notify,
            )
        except Exception as exc:
            log.warning(
                "manual_trade_poller.initial_listen_failed",
                error=str(exc)[:200],
            )

        while not self._shutdown_event.is_set():
            try:
                # LT-04: event-driven wait with 1s fall-through.
                # - NOTIFY fires → event is set → wait_for returns
                #   immediately (latency ~10-50ms from hub commit to
                #   engine wakeup on same-region DB).
                # - No NOTIFY within 1s → asyncio.TimeoutError → we
                #   fall through to the periodic poll as the safety net.
                try:
                    await asyncio.wait_for(
                        self._manual_trade_notify_event.wait(),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    pass
                finally:
                    self._manual_trade_notify_event.clear()

                if not self._db or not self._poly_client:
                    continue

                # LT-04: re-establish the LISTEN connection if it died.
                # ensure_listening is a no-op if already connected.
                try:
                    from persistence.db_client import MANUAL_TRADE_NOTIFY_CHANNEL

                    await self._db.ensure_listening(
                        MANUAL_TRADE_NOTIFY_CHANNEL,
                        self._on_manual_trade_notify,
                    )
                except Exception:
                    # Safety-net poll still fires below.
                    pass

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

                        # LT-02: Get CLOB token ID for the requested direction.
                        #
                        # Primary source: FiveMinVPINStrategy._recent_windows
                        # in-memory ring buffer. Fast but volatile — empty
                        # right after engine startup, and stale windows age
                        # out of the buffer within minutes.
                        #
                        # Fallback source: market_data table, written by the
                        # data-collector service on Montreal. Persistent
                        # across restarts, has per-window up_token_id and
                        # down_token_id. This is the fix for the silent
                        # "failed_no_token" failure the user reported —
                        # previously the engine had no fallback and every
                        # manual trade against a stale window died here
                        # with no Telegram alert.
                        token_id = None
                        token_source = None
                        if self._five_min_strategy:
                            for w in reversed(self._five_min_strategy.recent_windows):
                                if w.window_ts == window_ts:
                                    token_id = (
                                        w.up_token_id
                                        if direction == "YES"
                                        else w.down_token_id
                                    )
                                    if token_id:
                                        token_source = "recent_windows"
                                    break

                        # Fallback: query market_data by (asset, window_ts)
                        if not token_id:
                            log.info(
                                "manual_trade.ring_buffer_miss_fetching_from_db",
                                trade_id=trade_id,
                                window_ts=window_ts,
                                asset=asset,
                                direction=direction,
                            )
                            md_row = await self._db.get_token_ids_from_market_data(
                                asset=asset,
                                window_ts=window_ts,
                                timeframe=tf,
                            )
                            if md_row:
                                token_id = (
                                    md_row["up_token_id"]
                                    if direction == "YES"
                                    else md_row["down_token_id"]
                                )
                                if token_id:
                                    token_source = "market_data_db"
                                    log.info(
                                        "manual_trade.token_id_from_db",
                                        trade_id=trade_id,
                                        token_id_prefix=token_id[:20]
                                        if len(token_id) > 20
                                        else token_id,
                                    )

                        if not token_id:
                            log.warning(
                                "manual_trade.no_token_id",
                                trade_id=trade_id,
                                window_ts=window_ts,
                                asset=asset,
                                direction=direction,
                                tried_sources="recent_windows,market_data_db",
                            )
                            await self._db.update_manual_trade_status(
                                trade_id, "failed_no_token"
                            )
                            # LT-02: alert on Telegram so the operator knows
                            # the trade didn't land — previously this failed
                            # silently and the user thought the button did
                            # nothing at all.
                            if self._alerter:
                                try:
                                    await self._alerter.send_system_alert(
                                        f"⚠️ Manual Trade FAILED\n\n"
                                        f"Trade ID: `{trade_id[:16]}`\n"
                                        f"Direction: {direction} ({asset} 5m)\n"
                                        f"Reason: no CLOB token_id found for window_ts={window_ts}\n"
                                        f"Tried: ring_buffer + market_data_db\n\n"
                                        f"The window may be too stale (aged out of the ring buffer) "
                                        f"or data-collector hasn't written it yet. Try a fresh window.",
                                        level="warning",
                                    )
                                except Exception:
                                    pass  # don't let Telegram break the loop
                            continue

                        log.info(
                            "manual_trade.token_id_resolved",
                            trade_id=trade_id,
                            source=token_source,
                        )

                        if self._poly_client.paper_mode:
                            # Paper mode — simulate fill
                            clob_id = f"manual-paper-{trade_id[:12]}"
                            # POLY-SOT: persist the synthetic clob_id even in
                            # paper mode so the reconciler exercises the same
                            # code path and stamps the row `agrees`. The
                            # PolymarketClient's get_order_status_sot()
                            # recognises `manual-paper-*` IDs as synthetic
                            # paper fills and returns a filled OrderStatus.
                            await self._db.update_manual_trade_status(
                                trade_id,
                                "open",
                                clob_order_id=clob_id,
                            )
                            log.info(
                                "manual_trade.paper_filled",
                                trade_id=trade_id,
                                clob_id=clob_id,
                            )
                        else:
                            # Live — submit FOK to CLOB
                            from decimal import Decimal

                            clob_id = await self._poly_client.place_order(
                                market_slug=market_slug,
                                direction=direction,
                                price=Decimal(
                                    str(round(min(entry_price + 0.02, 0.65), 4))
                                ),  # Slight buffer above entry
                                stake_usd=stake,
                                token_id=token_id,
                            )
                            # POLY-SOT: persist the CLOB order ID so the SOT
                            # reconciler loop can verify the trade actually
                            # landed on Polymarket. If clob_id is None or
                            # empty (place_order returned silently), the
                            # reconciler will catch the gap on its next
                            # pass and tag the row engine_optimistic.
                            await self._db.update_manual_trade_status(
                                trade_id,
                                "open",
                                clob_order_id=str(clob_id) if clob_id else None,
                            )
                            log.info(
                                "manual_trade.live_submitted",
                                trade_id=trade_id,
                                clob_id=str(clob_id)[:20],
                            )

                        # Alert on Telegram
                        if self._alerter:
                            _mode = (
                                "📄 PAPER"
                                if self._poly_client.paper_mode
                                else "🔴 LIVE"
                            )
                            await self._alerter.send_system_alert(
                                f"👆 Manual Trade Executed ({_mode})\n"
                                f"Direction: {direction_raw}\n"
                                f"Entry: ${entry_price:.4f}\n"
                                f"Stake: ${stake:.2f}\n"
                                f"Trade ID: {trade_id[:16]}",
                                level="info",
                            )

                    except Exception as exc:
                        log.error(
                            "manual_trade.execution_failed",
                            trade_id=trade_id,
                            error=str(exc),
                        )
                        await self._db.update_manual_trade_status(
                            trade_id, f"failed: {str(exc)[:50]}"
                        )

            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("manual_trade_poller.error", error=str(exc))
                await asyncio.sleep(30)

        log.info("manual_trade_poller.stopped")

    async def _sot_reconciler_loop(self) -> None:
        """POLY-SOT — every 2 minutes, verify manual_trades AND trades rows against Polymarket.

        This loop is the safety net for both write paths into the database:
          * ``manual_trades`` — written by ``_manual_trade_poller`` after the
            operator clicks Execute.
          * ``trades`` — written by the engine's automatic strategies after
            ``_poly_client.place_order(...)`` returns.

        Both paths suffer from the same failure mode: the engine writes
        status='open' / 'FILLED' immediately after place_order() returns,
        without re-querying Polymarket to confirm the order actually landed.
        If place_order() times out, retries, or partially executes, the
        engine DB would happily claim success.

        The SOT loop closes that gap on BOTH tables: it iterates rows older
        than 30 seconds, calls polymarket_client.get_order_status_sot(), and
        stamps each row with the authoritative polymarket_confirmed_* fields
        plus a sot_reconciliation_state. On `engine_optimistic` or `diverged`
        rows it fires a Telegram alert.

        POLY-SOT-b: the same pass now walks both tables in sequence so the
        operator gets a unified view of "everything the engine touched
        Polymarket about" without spawning a second asyncio task. The
        Telegram dedupe key is namespaced by table so manual_trades #42 and
        trades #42 are treated as independent.

        Always-on: runs in both paper and live mode. In paper mode the
        reconciler stamps every paper trade `agrees` because PolymarketClient
        recognises the synthetic `manual-paper-*` order IDs and returns a
        filled OrderStatus. This means the dashboard "agrees" chip is
        meaningful even on the paper engine.

        Cadence: 2 minutes is a tradeoff between latency-to-detect and CLOB
        rate-limit pressure. Configurable via env var SOT_RECONCILER_INTERVAL.
        """
        # CLOBReconciler may be None when paper_mode is on (the existing
        # reconciler is live-only) — but we still want the SOT loop running
        # in paper mode. Construct a thin SOT-only reconciler in that case
        # so the loop body can stay the same.
        from reconciliation.reconciler import CLOBReconciler

        sot_reconciler = self._reconciler
        if sot_reconciler is None:
            try:
                sot_reconciler = CLOBReconciler(
                    poly_client=self._poly_client,
                    db_pool=self._db._pool if self._db else None,
                    alerter=self._alerter,
                    shutdown_event=self._shutdown_event,
                )
            except Exception as exc:
                log.error("sot_reconciler_loop.init_failed", error=str(exc))
                return

        try:
            interval_s = float(os.environ.get("SOT_RECONCILER_INTERVAL", "120"))
        except (TypeError, ValueError):
            interval_s = 120.0
        if interval_s < 10:
            interval_s = 10  # safety floor

        log.info("sot_reconciler_loop.started", interval_seconds=interval_s)

        while not self._shutdown_event.is_set():
            # POLY-SOT-b: walk both tables in the same pass — manual_trades
            # for operator trades, then `trades` for automatic engine trades.
            # Single-task design (rather than two parallel loops) keeps the
            # asyncio surface area smaller and ensures the two passes don't
            # race against each other on shared CLOB rate limits.
            try:
                manual_summary = await sot_reconciler.reconcile_manual_trades_sot(
                    limit=100
                )
                if manual_summary.checked > 0:
                    log.info(
                        "sot_reconciler_loop.manual_pass_complete",
                        checked=manual_summary.checked,
                        agrees=manual_summary.agrees,
                        unreconciled=manual_summary.unreconciled,
                        engine_optimistic=manual_summary.engine_optimistic,
                        polymarket_only=manual_summary.polymarket_only,
                        diverged=manual_summary.diverged,
                        alerts=manual_summary.alerts_fired,
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("sot_reconciler_loop.manual_pass_error", error=str(exc)[:200])

            try:
                trades_summary = await sot_reconciler.reconcile_trades_sot(limit=100)
                if trades_summary.checked > 0:
                    log.info(
                        "sot_reconciler_loop.trades_pass_complete",
                        checked=trades_summary.checked,
                        agrees=trades_summary.agrees,
                        unreconciled=trades_summary.unreconciled,
                        engine_optimistic=trades_summary.engine_optimistic,
                        polymarket_only=trades_summary.polymarket_only,
                        diverged=trades_summary.diverged,
                        alerts=trades_summary.alerts_fired,
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("sot_reconciler_loop.trades_pass_error", error=str(exc)[:200])

            # Paper + live resolution via ReconcilePositionsUseCase
            if getattr(self, "_reconcile_uc", None):
                try:
                    positions = []
                    _use_live_uc = (
                        os.environ.get("ENGINE_USE_RECONCILE_UC", "false").lower()
                        == "true"
                    )
                    if _use_live_uc and not self._settings.paper_mode:
                        try:
                            from domain.value_objects import PositionOutcome

                            raw = await self._poly_client.get_position_outcomes()
                            positions = [
                                PositionOutcome(
                                    condition_id=cid,
                                    token_id=str(
                                        data.get("asset", "") or data.get("tokenId", "")
                                    ),
                                    outcome=data["outcome"],
                                    size=float(data.get("size", 0)),
                                    avg_price=float(data.get("avgPrice", 0)),
                                    cost=float(data.get("cost", 0)),
                                    value=float(data.get("value", 0)),
                                    pnl_raw=float(data.get("pnl", 0)),
                                )
                                for cid, data in (raw or {}).items()
                                if data.get("outcome") in ("WIN", "LOSS")
                            ]
                        except Exception as exc:
                            log.warning(
                                "reconcile_uc.positions_fetch_failed",
                                error=str(exc)[:100],
                            )

                    result = await self._reconcile_uc.execute(positions)
                    if (
                        result.paper_resolved
                        or result.live_resolved
                        or result.windows_labeled
                    ):
                        log.info(
                            "reconcile_uc.complete",
                            live_resolved=result.live_resolved,
                            paper_resolved=result.paper_resolved,
                            paper_skipped=result.paper_skipped,
                            errors=result.errors,
                            windows_labeled=result.windows_labeled,
                        )
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    log.error("reconcile_uc.loop_error", error=str(exc)[:200])

            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=interval_s)
                break  # shutdown signalled
            except asyncio.TimeoutError:
                pass  # normal — continue to next pass

        log.info("sot_reconciler_loop.stopped")

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
                    log.info(
                        "position_monitor.started",
                        known_resolved=len(_resolved_conditions),
                    )
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

                        # v9.0: Link resolution back to trades table
                        # v10.1: Match by token_id in metadata (precise) instead of fuzzy stake matching
                        _matched_trade_id = None
                        _matched_reason = None
                        _matched_token_id = None
                        try:
                            if self._db._pool:
                                async with self._db._pool.acquire() as _conn:
                                    # Primary: match by token_id in metadata (exact match)
                                    _match = await _conn.fetchrow(
                                        """SELECT id, metadata->>'entry_reason' as reason,
                                           metadata->>'v81_entry_cap' as cap,
                                           metadata->>'token_id' as token_id
                                        FROM trades
                                        WHERE status IN ('OPEN', 'FILLED', 'EXPIRED')
                                          AND is_live = true
                                          AND metadata->>'token_id' IS NOT NULL
                                        ORDER BY created_at DESC LIMIT 5""",
                                    )
                                    # If no token_id match found, fall back to cost-based matching
                                    if not _match:
                                        _match = await _conn.fetchrow(
                                            """SELECT id, metadata->>'entry_reason' as reason,
                                               metadata->>'v81_entry_cap' as cap,
                                               metadata->>'token_id' as token_id
                                            FROM trades
                                            WHERE status IN ('OPEN', 'FILLED', 'EXPIRED')
                                              AND is_live = true
                                              AND ABS(CAST(stake_usd AS numeric) - $1) < 0.5
                                            ORDER BY created_at DESC LIMIT 1""",
                                            cost,
                                        )
                                    if _match:
                                        _matched_trade_id = _match["id"]
                                        _matched_reason = _match["reason"]
                                        _matched_token_id = _match["token_id"]
                                        # Update trade with resolution
                                        _status = (
                                            "RESOLVED_WIN"
                                            if outcome == "WIN"
                                            else "RESOLVED_LOSS"
                                        )
                                        await _conn.execute(
                                            """UPDATE trades SET outcome = $1, pnl_usd = $2,
                                               resolved_at = NOW(), status = $3
                                            WHERE id = $4 AND outcome IS NULL""",
                                            outcome,
                                            pnl if outcome == "WIN" else -cost,
                                            _status,
                                            _matched_trade_id,
                                        )
                                        log.info(
                                            "position_monitor.trade_linked",
                                            trade_id=_matched_trade_id,
                                            reason=_matched_reason,
                                            token_id=(_matched_token_id or "?")[:20],
                                            outcome=outcome,
                                        )
                        except Exception as _link_exc:
                            log.debug(
                                "position_monitor.trade_link_failed",
                                error=str(_link_exc)[:100],
                            )

                        # Dedup: skip notification if _on_order_resolution already sent it
                        if (
                            _matched_token_id
                            and _matched_token_id in self._resolved_by_order_manager
                        ):
                            log.info(
                                "position_monitor.skip_duplicate",
                                condition_id=cid[:20] + "...",
                                token_id=_matched_token_id[:20] + "...",
                                reason="already_notified_by_order_manager",
                            )
                            continue

                        # Try to find matching trade in DB for signal details
                        # v10.1: Use matched trade ID from token_id match (precise) instead of fuzzy cost match
                        trade_info = ""
                        try:
                            if self._db._pool:
                                async with self._db._pool.acquire() as conn:
                                    if _matched_trade_id:
                                        row = await conn.fetchrow(
                                            """SELECT metadata, created_at FROM trades WHERE id = $1""",
                                            _matched_trade_id,
                                        )
                                    else:
                                        row = await conn.fetchrow(
                                            """SELECT metadata, created_at FROM trades
                                               WHERE mode = 'live' AND stake_usd BETWEEN $1 AND $2
                                               ORDER BY created_at DESC LIMIT 1""",
                                            cost * 0.8,
                                            cost * 1.2,
                                        )
                                    if row and row["metadata"]:
                                        import json as _json

                                        meta = (
                                            _json.loads(row["metadata"])
                                            if isinstance(row["metadata"], str)
                                            else row["metadata"]
                                        )
                                        tier = meta.get("tier", "")
                                        conf = meta.get("confidence", "")
                                        delta = meta.get("delta_pct", "")
                                        entry_reason = meta.get("entry_reason", "")
                                        if tier or conf:
                                            conf_str = (
                                                f"{int(conf * 100)}%"
                                                if isinstance(conf, float)
                                                else str(conf)
                                            )
                                            trade_info = (
                                                f"\n📊 *Signal*\n"
                                                f"Tier: `{tier}`\n"
                                                f"Confidence: `{conf_str}`\n"
                                                f"Delta: `{delta}`\n"
                                            )
                                            if entry_reason:
                                                trade_info += (
                                                    f"Entry: `{entry_reason[:60]}`\n"
                                                )
                        except Exception:
                            pass

                        # Get wallet balance
                        try:
                            wallet = await self._poly_client.get_balance()
                            wallet_str = f"\n🏦 Wallet: `${wallet:.2f}` USDC"
                        except Exception:
                            wallet_str = ""

                        # v8.1: Show our engine's trade data from DB (not Polymarket aggregate)
                        _our_shares = ""
                        _our_fill = ""
                        _our_pnl = pnl_str
                        _our_cost = f"${cost:.2f}"
                        try:
                            if self._db._pool:
                                async with self._db._pool.acquire() as conn:
                                    _our_row = await conn.fetchrow(
                                        """SELECT metadata, stake_usd, pnl_usd, outcome
                                           FROM trades WHERE outcome IS NOT NULL
                                           AND created_at > NOW() - INTERVAL '15 minutes'
                                           ORDER BY created_at DESC LIMIT 1"""
                                    )
                                    if _our_row:
                                        import json as _json2

                                        _m = (
                                            _json2.loads(_our_row["metadata"])
                                            if isinstance(_our_row["metadata"], str)
                                            else _our_row["metadata"]
                                        )
                                        _our_shares = (
                                            f"Shares: `{_m.get('size_matched', '?')}`\n"
                                        )
                                        _fp = _m.get("actual_fill_price")
                                        _our_fill = (
                                            f"Fill: `${float(_fp):.4f}`\n"
                                            if _fp
                                            else f"Entry: `${avg_price:.4f}`\n"
                                        )
                                        _our_cost = (
                                            f"${float(_our_row['stake_usd']):.2f}"
                                        )
                                        if _our_row["pnl_usd"]:
                                            _p = float(_our_row["pnl_usd"])
                                            _our_pnl = (
                                                f"+${_p:.2f}"
                                                if _p >= 0
                                                else f"-${abs(_p):.2f}"
                                            )
                        except Exception:
                            pass

                        # v9.0: Show entry reason in resolution notification
                        _reason_line = ""
                        if _matched_reason:
                            _reason_line = f"Entry: `{_matched_reason}`\n"
                        elif trade_info:
                            pass  # trade_info already has entry reason from fuzzy match

                        msg = (
                            f"{emoji} *{outcome} — BTC* (💰 LIVE)\n"
                            f"🕐 `{now_str}`\n"
                            f"\n"
                            f"📊 *Result*\n"
                            f"{_our_shares}"
                            f"{_our_fill}"
                            f"Cost: `{_our_cost}`\n"
                            f"Payout: `${value:.2f}`\n"
                            f"PnL: `{_our_pnl}`\n"
                            f"{_reason_line}"
                            f"{trade_info}"
                            f"{wallet_str}"
                        )

                        # Use send_raw_message — send_system_alert wraps text in backticks
                        # which breaks multi-line markdown formatting.
                        await self._alerter.send_raw_message(msg)
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

    # ── Shadow Trade Resolution Loop ─────────────────────────────────────────

    async def _shadow_resolution_loop(self) -> None:
        """
        Every 30s: resolve oracle outcomes for skipped (shadow) windows.

        For each recent skipped window (trade_placed=FALSE) with a shadow signal
        that hasn't been oracle-resolved yet, query the Polymarket Gamma API to
        check whether the market settled UP or DOWN. Then:
          - Compute shadow P&L (what we would have won/lost)
          - Write oracle_outcome, shadow_pnl, shadow_would_win to DB
          - Send Telegram notification

        Rate limiting: batch all windows, one API call per window, 0.5s delay between.
        Resolution typically happens ~4 min after window close (oracle lag).
        """
        import aiohttp as _aiohttp

        POLL_INTERVAL = 30  # seconds between sweeps
        API_DELAY = 0.5  # seconds between Gamma API calls (rate limit)
        STAKE_USD = 5.0  # shadow stake for P&L calculation
        FEE_MULT = 0.98  # 2% fee on winnings

        log.info("shadow_resolution_loop.started")

        while not self._shutdown_event.is_set():
            try:
                # Fetch unresolved skipped windows from last 10 min
                unresolved = await self._db.get_unresolved_shadow_windows(
                    minutes_back=10
                )

                if unresolved:
                    log.debug(
                        "shadow_resolution.checking",
                        count=len(unresolved),
                    )

                for row in unresolved:
                    if self._shutdown_event.is_set():
                        break

                    window_ts = row["window_ts"]
                    asset = row.get("asset", "BTC")
                    timeframe = row.get("timeframe", "5m")
                    shadow_dir = row.get("shadow_trade_direction")
                    entry_price = row.get("shadow_trade_entry_price")
                    skip_reason = row.get("skip_reason") or "unknown"
                    confidence_raw = row.get("confidence")

                    # Map confidence float → tier label
                    confidence_tier = "LOW"
                    if confidence_raw is not None:
                        if confidence_raw >= 0.80:
                            confidence_tier = "HIGH"
                        elif confidence_raw >= 0.60:
                            confidence_tier = "MODERATE"

                    if not shadow_dir or entry_price is None:
                        continue

                    # Build Gamma API slug: btc-updown-5m-{window_ts}
                    slug = f"{asset.lower()}-updown-{timeframe}-{window_ts}"
                    url = f"https://gamma-api.polymarket.com/events?slug={slug}"

                    try:
                        async with _aiohttp.ClientSession(
                            headers={"User-Agent": "Mozilla/5.0"}
                        ) as session:
                            async with session.get(
                                url,
                                timeout=_aiohttp.ClientTimeout(total=10),
                            ) as resp:
                                if resp.status != 200:
                                    log.debug(
                                        "shadow_resolution.api_error",
                                        slug=slug,
                                        status=resp.status,
                                    )
                                    await asyncio.sleep(API_DELAY)
                                    continue

                                data = await resp.json()
                    except Exception as api_exc:
                        log.debug(
                            "shadow_resolution.api_exception",
                            slug=slug,
                            error=str(api_exc)[:80],
                        )
                        await asyncio.sleep(API_DELAY)
                        continue

                    # Parse response
                    try:
                        if (
                            not data
                            or not isinstance(data, list)
                            or not data[0].get("markets")
                        ):
                            # Market not found yet — skip until next sweep
                            await asyncio.sleep(API_DELAY)
                            continue

                        market = data[0]["markets"][0]

                        # Check if resolved — Gamma may not set resolved=true
                        # for 5-min markets, so also check outcomePrices directly
                        if not market.get("resolved") and not any(
                            str(p) in ("0", "1", "1.0", "0.0")
                            for p in market.get("outcomePrices", [])
                        ):
                            # Not settled yet — oracle hasn't closed
                            await asyncio.sleep(API_DELAY)
                            continue

                        # outcomePrices: [1, 0] = UP won, [0, 1] = DOWN won
                        outcome_prices = market.get("outcomePrices", [])
                        if len(outcome_prices) < 2:
                            await asyncio.sleep(API_DELAY)
                            continue

                        # Polymarket UP is outcomes[0], DOWN is outcomes[1]
                        try:
                            up_price_final = float(outcome_prices[0])
                            down_price_final = float(outcome_prices[1])
                        except (ValueError, TypeError):
                            await asyncio.sleep(API_DELAY)
                            continue

                        # Determine oracle winner
                        if up_price_final >= 0.99:
                            oracle_direction = "UP"
                        elif down_price_final >= 0.99:
                            oracle_direction = "DOWN"
                        else:
                            # Not yet fully settled (prices not at 0/1)
                            await asyncio.sleep(API_DELAY)
                            continue

                        # Compute shadow P&L
                        shadow_would_win = shadow_dir == oracle_direction
                        if shadow_would_win:
                            # Win: (1 - entry_price) * stake * fee_mult
                            shadow_pnl = (
                                (1.0 - float(entry_price)) * STAKE_USD * FEE_MULT
                            )
                        else:
                            # Loss: -entry_price * stake
                            shadow_pnl = -float(entry_price) * STAKE_USD

                        # Persist to DB
                        await self._db.update_shadow_resolution(
                            window_ts=window_ts,
                            asset=asset,
                            timeframe=timeframe,
                            oracle_outcome=oracle_direction,
                            shadow_pnl=round(shadow_pnl, 2),
                            shadow_would_win=shadow_would_win,
                        )
                        # v8.1.2: Also update window_predictions with oracle result
                        try:
                            await self._db.update_window_prediction_outcome(
                                window_ts, asset, oracle_direction
                            )
                        except Exception:
                            pass

                        # Send Telegram notification
                        window_id = f"{asset}-{window_ts}"
                        await self._alerter.send_shadow_resolution(
                            window_id=window_id,
                            direction=shadow_dir,
                            entry_price=float(entry_price),
                            oracle_direction=oracle_direction,
                            shadow_pnl=round(shadow_pnl, 2),
                            skip_reason=skip_reason,
                            confidence_tier=confidence_tier,
                        )

                        log.info(
                            "shadow_resolution.resolved",
                            window_ts=window_ts,
                            asset=asset,
                            shadow_dir=shadow_dir,
                            oracle_direction=oracle_direction,
                            shadow_would_win=shadow_would_win,
                            shadow_pnl=f"{shadow_pnl:+.2f}",
                        )

                        # ── Post-Resolution AI Analysis ───────────────────────
                        # Run Sonnet analysis of ALL eval ticks for this window.
                        # Only for recent windows (last 15 min), rate-limited to
                        # 1 analysis per 60 seconds to avoid API spam.
                        if self._post_resolution_evaluator:
                            try:
                                # Fetch eval ticks from gate_audit / in-memory history
                                _eval_ticks = []
                                # Try in-memory window_eval_history first (most complete)
                                if self._five_min_strategy:
                                    _wkey = f"{asset}-{window_ts}"
                                    _eval_ticks = list(
                                        self._five_min_strategy.window_eval_history.get(
                                            _wkey, []
                                        )
                                    )
                                # Fall back to DB gate_audit table
                                if not _eval_ticks:
                                    _eval_ticks = (
                                        await self._db.get_eval_ticks_for_window(
                                            window_ts=window_ts,
                                            asset=asset,
                                            timeframe=timeframe,
                                        )
                                    )
                                # Schedule analysis as a background task (non-blocking)
                                asyncio.create_task(
                                    self._post_resolution_evaluator.analyse_window(
                                        window_ts=window_ts,
                                        asset=asset,
                                        timeframe=timeframe,
                                        oracle_direction=oracle_direction,
                                        eval_ticks=_eval_ticks or None,
                                    )
                                )
                                log.debug(
                                    "shadow_resolution.post_eval_scheduled",
                                    window_ts=window_ts,
                                    n_ticks=len(_eval_ticks),
                                )
                            except Exception as _pe:
                                log.warning(
                                    "shadow_resolution.post_eval_error",
                                    error=str(_pe)[:100],
                                )

                    except Exception as parse_exc:
                        log.warning(
                            "shadow_resolution.parse_error",
                            slug=slug,
                            error=str(parse_exc)[:120],
                        )

                    await asyncio.sleep(API_DELAY)

            except Exception as exc:
                log.error("shadow_resolution_loop.error", error=str(exc)[:120])

            # v8.1.2: Also resolve ALL window_predictions without oracle_winner
            # This catches windows that had no shadow signal (e.g. CALM regime skips)
            try:
                if self._db._pool:
                    import time as _time

                    _cutoff = int(_time.time()) - 1800  # last 30 min
                    async with self._db._pool.acquire() as _conn:
                        _unresolved = await _conn.fetch(
                            """
                            SELECT window_ts, asset FROM window_predictions
                            WHERE oracle_winner IS NULL AND window_ts > $1 AND window_ts < $2
                            LIMIT 5
                        """,
                            _cutoff,
                            int(_time.time()) - 60,
                        )  # at least 60s old

                    for _row in _unresolved:
                        _wts = _row["window_ts"]
                        _asset = _row["asset"]
                        _slug = f"{_asset.lower()}-updown-5m-{_wts}"
                        try:
                            import aiohttp as _aio2

                            async with _aio2.ClientSession(
                                headers={"User-Agent": "Mozilla/5.0"}
                            ) as _s:
                                async with _s.get(
                                    f"https://gamma-api.polymarket.com/events?slug={_slug}",
                                    timeout=_aio2.ClientTimeout(total=10),
                                ) as _r:
                                    if _r.status == 200:
                                        _d = await _r.json()
                                        if (
                                            _d
                                            and isinstance(_d, list)
                                            and _d[0].get("markets")
                                        ):
                                            _m = _d[0]["markets"][0]
                                            if _m.get("resolved") or any(
                                                str(p) in ("0", "1", "1.0", "0.0")
                                                for p in _m.get("outcomePrices", [])
                                            ):
                                                _op = _m.get("outcomePrices", [])
                                                if len(_op) >= 2:
                                                    _up = float(_op[0])
                                                    _dn = float(_op[1])
                                                    if _up >= 0.99:
                                                        _winner = "UP"
                                                    elif _dn >= 0.99:
                                                        _winner = "DOWN"
                                                    else:
                                                        continue
                                                    # Update both tables
                                                    await self._db.update_window_prediction_outcome(
                                                        _wts, _asset, _winner
                                                    )
                                                    # Also update window_snapshots.poly_winner
                                                    try:
                                                        async with (
                                                            self._db._pool.acquire() as _c2
                                                        ):
                                                            await _c2.execute(
                                                                "UPDATE window_snapshots SET poly_winner=$1 "
                                                                "WHERE window_ts=$2 AND asset=$3 AND poly_winner IS NULL",
                                                                _winner.capitalize(),
                                                                _wts,
                                                                _asset,
                                                            )
                                                    except Exception:
                                                        pass
                                                    log.info(
                                                        "prediction_resolution.resolved",
                                                        window_ts=_wts,
                                                        winner=_winner,
                                                    )
                            await asyncio.sleep(0.5)
                        except Exception:
                            pass
            except Exception as _pred_exc:
                log.debug("prediction_resolution.error", error=str(_pred_exc)[:100])

            # Wait 30s between sweeps
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._shutdown_event.wait()),
                    timeout=float(POLL_INTERVAL),
                )
                break
            except asyncio.TimeoutError:
                pass  # Normal — continue loop

        log.info("shadow_resolution_loop.stopped")

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
                            current_price = (
                                float(state.btc_price) if state.btc_price else None
                            )
                            if current_price is None or w.open_price is None:
                                continue
                            delta_pct = (
                                (current_price - w.open_price) / w.open_price * 100
                            )
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
                        await self._five_min_strategy.evaluate_window(w, state)
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
