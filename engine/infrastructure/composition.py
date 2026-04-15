"""
CompositionRoot — owns component creation and wiring.

Extracted from Orchestrator.__init__ during Phase 5 refactor. Callbacks that
referenced Orchestrator methods (self._on_*) are now None; EngineRuntime
patches them post-construction.
"""

from __future__ import annotations

import asyncio
import os
import signal as _signal  # noqa: F401
import time  # noqa: F401
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
    AggTrade,  # noqa: F401
    ArbOpportunity,  # noqa: F401
    CascadeSignal,  # noqa: F401
    LiquidationVolume,  # noqa: F401
    OpenInterestSnapshot,  # noqa: F401
    PolymarketOrderBook,  # noqa: F401
    VPINSignal,  # noqa: F401
)
from execution.opinion_client import OpinionClient
from execution.order_manager import OrderManager
from execution.polymarket_client import PolymarketClient
from execution.risk_manager import RiskManager
from persistence.db_client import DBClient, DBClientLegacyShim
from persistence.tick_recorder import TickRecorder
from signals.arb_scanner import ArbScanner
from signals.cascade_detector import CascadeDetector
from signals.regime_classifier import RegimeClassifier
from signals.vpin import VPINCalculator
from strategies.five_min_vpin import FiveMinVPINStrategy
from signals.twap_delta import TWAPTracker
from signals.timesfm_client import TimesFMClient

log = structlog.get_logger(__name__)


class CompositionRoot:
    """
    Construction-only container.

    Creates every engine component from Settings but does NOT wire runtime
    callbacks (EngineRuntime does that) and does NOT start anything.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

        # ── G1 & G3: Staggered execution + single best signal ─────────────────
        # (runtime-only state lives on EngineRuntime, not here)

        # ── CLOB Reconciler (v10.2: definitive source of truth) ─────
        self._reconciler = None

        # ── TWAP Tracker (v5.7: time-weighted delta for direction) ─────────
        self._twap_tracker = TWAPTracker(max_windows=50)

        # ── TimesFM Client (v6.0, initialized early for FiveMinVPIN alerts) ──
        self._timesfm_client: Optional[TimesFMClient] = None

        log.info("orchestrator.init", paper_mode=settings.paper_mode)

        # ── Persistence ────────────────────────────────────────────────────────
        self._db = DBClientLegacyShim(DBClient(settings=settings))
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
            openrouter_api_key=settings.openrouter_api_key,
        )

        # ── Signal Processors ──────────────────────────────────────────────────
        # VPIN (callback patched by EngineRuntime)
        self._vpin_calc = VPINCalculator(
            on_signal=None,
        )

        # Cascade Detector (callback patched by EngineRuntime)
        self._cascade = CascadeDetector(
            on_signal=None,
        )

        # Arb Scanner (callback patched by EngineRuntime)
        self._arb_scanner = ArbScanner(
            fee_mult=0.072,  # POLYMARKET_CRYPTO_FEE_MULT
            on_opportunities=None,
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
            on_resolution=None,
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
        from adapters.persistence.pg_redeem_attempts import (
            PgRedeemAttemptsRepository,
        )

        # Attempts repo backs the "skip condition_ids with >=3 failed
        # attempts in 24h" gate in redeem_position(). Passing the DBClient
        # lets the repo pull the shared pool lazily — survives DB reconnects.
        redeem_attempts_repo = PgRedeemAttemptsRepository(db_client=self._db)

        self._redeemer = PositionRedeemer(
            rpc_url=settings.polygon_rpc_url,
            private_key=settings.poly_private_key,
            proxy_address=settings.poly_funder_address,
            paper_mode=settings.paper_mode,
            builder_key=settings.builder_key or os.environ.get("BUILDER_KEY", ""),
            attempts_repo=redeem_attempts_repo,
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
        if settings.openrouter_api_key:
            self._claude_evaluator = ClaudeEvaluator(
                api_key=settings.openrouter_api_key,
                model=settings.openrouter_model,
                alerter=self._alerter,
                db_client=self._db,
            )
            log.info("orchestrator.claude_evaluator_enabled")

        # ── Post-Resolution AI Evaluator (Sonnet, runs after shadow resolution) ─
        self._post_resolution_evaluator = None
        if settings.openrouter_api_key:
            self._post_resolution_evaluator = PostResolutionEvaluator(
                api_key=settings.openrouter_api_key,
                model=settings.openrouter_model,
                db_client=self._db,
                alerter=self._alerter,
            )
            log.info("orchestrator.post_resolution_evaluator_enabled")

        # ── Strategies ─────────────────────────────────────────────────────────
        # Legacy arb/cascade/timesfm strategies retired — registry handles all execution.

        # 5-minute Polymarket strategy (optional)
        self._five_min_strategy = None
        if settings.five_min_enabled:
            self._five_min_feed = Polymarket5MinFeed(
                assets=settings.five_min_assets.split(","),
                signal_offset=FIVE_MIN_ENTRY_OFFSET,
                on_window_signal=None,
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
                geoblock_check_fn=lambda: False,  # G6: patched by EngineRuntime
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
        timesfm_url = timesfm_url or "http://16.52.14.182:8080"

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

            _v2_url = os.environ.get("TIMESFM_V2_URL", "http://16.52.14.182:8080")
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

        # ── ProcessFiveMinWindowUseCase ────────────────────────────────────────
        from use_cases.process_five_min_window import ProcessFiveMinWindowUseCase
        self._process_window_uc = ProcessFiveMinWindowUseCase(
            strategy=self._five_min_strategy,
            shadow_strategies=[],
        )

        # ── Strategy Engine v2: Config-first registry (behind feature flag) ──
        # When enabled, runs the new YAML-config-based registry in parallel
        # with the existing EvaluateStrategiesUseCase for decision comparison.
        self._strategy_registry = None
        self._use_strategy_registry = (
            os.environ.get("ENGINE_USE_STRATEGY_REGISTRY", "false").lower() == "true"
        )
        if self._use_strategy_registry:
            # Create the registry, but defer wiring the ExecuteTradeUseCase until
            # after all its dependencies (DB, repos) are live.
            try:
                from strategies.data_surface import DataSurfaceManager
                from strategies.registry import StrategyRegistry

                config_dir = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "strategies", "configs",
                )
                self._data_surface_mgr = DataSurfaceManager(
                    v4_base_url=os.environ.get("TIMESFM_URL", "http://localhost:8001"),
                    tiingo_feed=getattr(self, "_tiingo_feed", None),
                    chainlink_feed=getattr(self, "_chainlink_multi_feed", None)
                    or getattr(self, "_chainlink_feed", None),
                    clob_feed=getattr(self, "_clob_feed", None),
                    vpin_calculator=self._vpin_calc,
                    cg_feeds=self._cg_feeds,
                    twap_tracker=self._twap_tracker,
                    binance_state=self._aggregator
                    if hasattr(self, "_aggregator")
                    else None,
                )

                # Wire decision repo for per-eval strategy_decisions writes
                _decision_repo = None
                _trace_repo = None
                try:
                    from adapters.persistence.pg_strategy_decisions import (
                        PgStrategyDecisionRepository,
                    )
                    from adapters.persistence.pg_window_trace_repo import (
                        PgWindowTraceRepository,
                    )

                    _decision_repo = PgStrategyDecisionRepository(
                        db_client=self._db,
                    )
                    _trace_repo = PgWindowTraceRepository(
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
                    execute_trade_uc=None,  # DEFERRED to start()
                    alerter=self._alerter,
                    decision_repo=_decision_repo,
                    trace_repo=_trace_repo,
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
            # 15m eval offsets: T-600 down to T-60 every 2s
            # Covers [270,450] timing gates (v15m_down_only etc) that 5m offsets miss
            _fifteen_eval_offsets = list(range(600, 59, -2))
            self._fifteen_min_feed = Polymarket5MinFeed(
                assets=fifteen_min_assets,
                duration_secs=900,  # 15 minutes
                signal_offset=FIVE_MIN_ENTRY_OFFSET,
                eval_offsets=_fifteen_eval_offsets,
                on_window_signal=None,
                paper_mode=settings.paper_mode,
            )
            log.info("orchestrator.fifteen_min_enabled", assets=fifteen_min_assets)

        # ── Feeds (wired after all components exist) ────────────────────────────
        # Futures feed: aggTrades → VPIN calculator, forceOrder → cascade detector
        self._binance_feed = BinanceWebSocketFeed(
            symbol="btcusdt",
            venue="futures",
            on_trade=None,
            on_liquidation=self._aggregator.on_liquidation,
        )
        # Spot feed: aggTrades → btc_spot_price for oracle-aligned delta calculation
        # (Polymarket resolves via Chainlink oracle on SPOT, not futures)
        self._binance_spot_feed = BinanceWebSocketFeed(
            symbol="btcusdt",
            venue="spot",
            on_trade=None,
        )
        # Optional feeds — only create if API keys are configured
        self._coinglass_feed = None
        if settings.coinglass_api_key:
            self._coinglass_feed = CoinGlassAPIFeed(
                api_key=settings.coinglass_api_key,
                symbol="BTC",
                on_oi=None,
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
            on_book=None,
        )

        # ── Heartbeat use cases ────────────────────────────────────────────────
        from use_cases.publish_heartbeat import PublishHeartbeatUseCase
        from adapters.persistence.pg_system_repo import PgSystemRepository
        from adapters.clock.system_clock import SystemClock
        from adapters.engine_state_reader import EngineStateReaderAdapter
        from use_cases.run_heartbeat_tick import RunHeartbeatTickUseCase

        self._pg_system_repo = PgSystemRepository(pool=None)  # pool injected in start()
        self._engine_state_reader = EngineStateReaderAdapter(settings=settings)
        self._publish_heartbeat_uc = PublishHeartbeatUseCase(
            risk_manager=self._risk_manager,
            system_state_repo=self._pg_system_repo,
            alerts=self._alerter,
            clock=SystemClock(),
            engine_state=self._engine_state_reader,
            sitrep_interval=9999,  # runtime's rich sitrep handles Telegram; this UC owns DB write only
        )
        self._run_heartbeat_tick_uc = RunHeartbeatTickUseCase(
            publish_heartbeat_uc=self._publish_heartbeat_uc,
            engine_state_reader=self._engine_state_reader,
            aggregator=self._aggregator,
            risk_manager=self._risk_manager,
            order_manager=self._order_manager,
            poly_client=self._poly_client,
            settings=settings,
        )
