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
# polymarket_rtds_chainlink kept in repo for reference but no longer used —
# the WebSocket connected + subscribed but received zero messages, timing out
# every 30s in an endless reconnect loop. Replaced by HTML scrape below.
from data.feeds.polymarket_html_pricetobeat import PolymarketHTMLPriceToBeatFeed
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

        # ── TG Narrative V2 (Phase A-K refactor) ───────────────────────────────
        # Always constructed so callers have a stable reference; activation is
        # gated by ``settings.tg_narrative_v2_enabled`` inside the publisher.
        # See plans/serialized-drifting-clover.md.
        self._narrative_v2_enabled: bool = bool(
            getattr(settings, "tg_narrative_v2_enabled", False)
        )
        self._owner_eoa_addresses: frozenset[str] = frozenset(
            a.strip().lower()
            for a in (getattr(settings, "owner_eoa_addresses", "") or "").split(",")
            if a.strip()
        )
        self._alert_renderer = None        # type: ignore[var-annotated]
        self._alert_sender = None          # type: ignore[var-annotated]
        self._shadow_decision_repo = None  # type: ignore[var-annotated]
        self._tally_repo = None            # type: ignore[var-annotated]
        self._onchain_query = None         # type: ignore[var-annotated]
        self._publish_alert = None         # type: ignore[var-annotated]
        self._init_narrative_v2()

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

        # ── Rolling-WR auto-pause monitor (audits #379 + #385) ───────────────
        # Built before OrderManager so we can pass it as a constructor arg.
        # The concrete repo is passed the DBClient shim; it extracts the
        # asyncpg pool lazily so it survives DB reconnects.
        from adapters.persistence.pg_cell_pause_repo import PgCellPauseRepo
        from services.rolling_wr_monitor import RollingWRMonitor
        self._cell_pause_repo = PgCellPauseRepo(db_client=self._db)
        self._rolling_wr_monitor = RollingWRMonitor(
            repo=self._cell_pause_repo,
            alerter=self._alerter,
        )
        # In-memory snapshot of active cell pauses (refreshed every 30s by
        # EngineRuntime). The set holds frozensets of (strategy_id, direction,
        # t_band, regime, session) so the CellPauseGate lookup is O(1) and
        # zero-I/O at decision time.
        self._active_pause_snapshot: set[tuple] = set()

        def _cell_pause_lookup(
            strategy_id: str,
            direction: str,
            t_band: str,
            regime: Optional[str],
            session: Optional[str],
        ) -> Optional[str]:
            """Sync gate lookup — consults in-memory snapshot."""
            key = (strategy_id, direction, t_band, regime, session)
            if key in self._active_pause_snapshot:
                return f"cell auto-paused by rolling-WR monitor"
            return None

        self._cell_pause_lookup = _cell_pause_lookup

        # ── Order & Risk Management ────────────────────────────────────────────
        self._order_manager = OrderManager(
            db=self._db,
            bankroll=settings.starting_bankroll,
            paper_mode=settings.paper_mode,
            on_resolution=None,
            poly_client=self._poly_client,
            rolling_wr_monitor=self._rolling_wr_monitor,
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

        # ── On-Chain Redemption Pipeline ─────────────────────────────────────
        # Primary: direct on-chain scan + factory.proxy() redeem.
        # Fallback: Builder Relayer API (kept for backwards compat).
        from execution.redeemer import PositionRedeemer
        from adapters.persistence.pg_redeem_attempts import (
            PgRedeemAttemptsRepository,
        )
        from adapters.persistence.pg_trade_repo import PgTradeRepository

        # Attempts repo backs the "skip condition_ids with >=3 failed
        # attempts in 24h" gate in redeem_position(). Passing the DBClient
        # lets the repo pull the shared pool lazily — survives DB reconnects.
        redeem_attempts_repo = PgRedeemAttemptsRepository(db_client=self._db)

        # Trades repo writes redemption state back onto the canonical
        # trades table so audits, P&L dashboards, and future sweeps can
        # distinguish truly-settled positions from stale WIN rows.
        redeem_trades_repo: Optional[PgTradeRepository] = None
        db_pool = getattr(self._db, "_pool", None) if self._db is not None else None
        if db_pool is not None:
            redeem_trades_repo = PgTradeRepository(db_pool)

        # On-chain scanner + transport: wired when we have the private key
        # and at least one RPC URL. The scanner needs 2+ RPCs for consensus;
        # we build the list from POLYGON_RPC_URL (primary) plus any
        # POLYGON_RPC_URL_2 / POLYGON_RPC_URL_3 env vars (fallbacks).
        _onchain_scanner = None
        _onchain_transport = None
        if settings.polygon_rpc_url and settings.poly_private_key and settings.poly_funder_address:
            rpc_urls = [settings.polygon_rpc_url]
            for suffix in ("_2", "_3", "_BACKUP"):
                extra = os.environ.get(f"POLYGON_RPC_URL{suffix}", "")
                if extra:
                    rpc_urls.append(extra)
            # Free public Polygon RPC as last-resort second endpoint
            if len(rpc_urls) < 2:
                rpc_urls.append("https://polygon-rpc.com")

            try:
                from execution.onchain_scanner import OnChainPositionScanner
                _onchain_scanner = OnChainPositionScanner(
                    rpc_urls=rpc_urls,
                    proxy_address=settings.poly_funder_address,
                )
            except Exception as exc:
                log.warning("composition.onchain_scanner_init_failed", error=str(exc)[:200])

            try:
                from execution.onchain_transport import OnChainRedemptionTransport
                _onchain_transport = OnChainRedemptionTransport(
                    rpc_url=settings.polygon_rpc_url,
                    private_key=settings.poly_private_key,
                    proxy_address=settings.poly_funder_address,
                )
            except Exception as exc:
                log.warning("composition.onchain_transport_init_failed", error=str(exc)[:200])

        self._redeemer = PositionRedeemer(
            rpc_url=settings.polygon_rpc_url,
            private_key=settings.poly_private_key,
            proxy_address=settings.poly_funder_address,
            paper_mode=settings.paper_mode,
            builder_key=settings.builder_key or os.environ.get("BUILDER_KEY", ""),
            attempts_repo=redeem_attempts_repo,
            trades_repo=redeem_trades_repo,
            onchain_scanner=_onchain_scanner,
            onchain_transport=_onchain_transport,
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

        # ── Polymarket HTML priceToBeat Feed ─────────────────────────────────
        # Anonymous HTTP scrape of polymarket.com/event/<slug> — extracts the
        # EXACT priceToBeat Polymarket displays in their UI from the Next.js
        # __NEXT_DATA__ JSON blob. Replaces the broken RTDS WebSocket feed
        # (which connected + subscribed but received zero messages, timing out
        # every 30s in an endless reconnect loop). Eliminates train/serve skew
        # vs Polymarket UI for WindowInfo.open_price.
        # Legacy attribute name `_rtds_feed` retained = None so existing
        # `getattr(root, "_rtds_feed", None)` references in runtime.py keep
        # working without modification.
        self._rtds_feed = None
        try:
            self._html_ptb_feed = PolymarketHTMLPriceToBeatFeed()
            log.info("orchestrator.html_ptb_feed_instantiated")
        except Exception as exc:
            log.warning("orchestrator.html_ptb_feed_init_failed", error=str(exc))
            self._html_ptb_feed = None

        # 5-minute Polymarket strategy (optional)
        self._five_min_strategy = None
        if settings.five_min_enabled:
            self._five_min_feed = Polymarket5MinFeed(
                assets=settings.five_min_assets.split(","),
                signal_offset=FIVE_MIN_ENTRY_OFFSET,
                on_window_signal=None,
                paper_mode=settings.paper_mode,
                html_ptb_feed=self._html_ptb_feed,
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
        # Enable logic (audit #397):
        #   1. Resolve TIMESFM_URL from env (or .env file fallback).
        #   2. If TIMESFM_URL is set, enable by default — URL presence signals intent.
        #      TIMESFM_ENABLED=false can still explicitly disable.
        #   3. If TIMESFM_URL is absent, skip cleanly with an error-level log.
        # No legacy IP fallback — if the URL is missing the service is unconfigured.

        # Resolve URL (env > .env file; no hardcoded fallback)
        timesfm_url = os.environ.get("TIMESFM_URL", "").strip()
        if not timesfm_url:
            env_file = Path(__file__).parent.parent / ".env"
            if env_file.exists():
                with open(env_file) as f:
                    for line in f:
                        if line.startswith("TIMESFM_URL="):
                            timesfm_url = line.split("=", 1)[1].strip()
                            break

        # Resolve explicit enable/disable override (env > .env file)
        _timesfm_enabled_raw = os.environ.get("TIMESFM_ENABLED", "").strip()
        if not _timesfm_enabled_raw:
            env_file = Path(__file__).parent.parent / ".env"
            if env_file.exists():
                with open(env_file) as f:
                    for line in f:
                        if line.startswith("TIMESFM_ENABLED="):
                            _timesfm_enabled_raw = line.split("=", 1)[1].strip()
                            break

        # Decision: URL present → enabled by default unless explicitly set to false
        if _timesfm_enabled_raw.lower() == "false":
            timesfm_enabled = False
        elif _timesfm_enabled_raw.lower() == "true":
            timesfm_enabled = True
        else:
            # No explicit override — enable iff URL is configured
            timesfm_enabled = bool(timesfm_url)

        timesfm_min_conf_str = os.environ.get("TIMESFM_MIN_CONFIDENCE", "").strip()
        if not timesfm_min_conf_str:
            env_file = Path(__file__).parent.parent / ".env"
            if env_file.exists():
                with open(env_file) as f:
                    for line in f:
                        if line.startswith("TIMESFM_MIN_CONFIDENCE="):
                            timesfm_min_conf_str = line.split("=", 1)[1].strip()
                            break
        timesfm_min_conf = float(timesfm_min_conf_str or "0.30")

        if timesfm_enabled and not timesfm_url:
            # TIMESFM_ENABLED=true but no URL — warn and skip cleanly
            log.error(
                "orchestrator.timesfm_url_missing_skip",
                reason="TIMESFM_ENABLED=true but TIMESFM_URL is not set — TimesFM disabled",
            )
            timesfm_enabled = False

        if timesfm_enabled:
            self._timesfm_client = TimesFMClient(
                base_url=timesfm_url,
                timeout_seconds=10.0,
            )
            # Only the CLIENT is created here. No standalone strategies.
            # TimesFM is used as an agreement signal inside the strategy gates
            # AND as the 1Hz ticks_timesfm writer (audit #397).
            log.info(
                "orchestrator.timesfm_v6_enabled",
                url=timesfm_url,
                min_confidence=timesfm_min_conf,
                mode="agreement_signal_and_tick_writer",
            )
        else:
            log.info(
                "orchestrator.timesfm_v6_disabled",
                url_configured=bool(timesfm_url),
                explicit_override=_timesfm_enabled_raw or None,
            )

        # v5.8: Inject TimesFM client into five_min_strategy (created before client was initialized)
        if self._timesfm_client and self._five_min_strategy:
            self._five_min_strategy.set_timesfm_client(self._timesfm_client)
            log.info("orchestrator.timesfm_injected_into_five_min")

        # v8.1: Inject TimesFM v2.2 client for early entry (calibrated probability)
        _v2_enabled = os.environ.get("V2_EARLY_ENTRY_ENABLED", "true").lower() == "true"
        if _v2_enabled and self._five_min_strategy:
            from signals.timesfm_v2_client import TimesFMV2Client

            _v2_url = os.environ.get("TIMESFM_V2_URL") or timesfm_url or "http://3.96.151.28:8080"
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
            # Audit #255 F5 — expose for the runtime-level trade recorder wiring.
            self._strategy_decision_repo = _decision_repo

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
                from execution.position_monitor import PositionMonitor

                config_dir = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "strategies", "configs",
                )
                # Assets the data-surface refresh loop polls /v4/snapshot
                # for. Union of FIVE_MIN_ASSETS + FIFTEEN_MIN_ASSETS env
                # vars so every active window has a per-asset cache slot
                # and strategies never silently read another asset's data.
                #
                # Read env directly — the `fifteen_min_assets` local below
                # is defined AFTER this block, so referencing it here
                # raised NameError on startup, which killed the whole
                # strategy-registry init (v6_sniper LIVE went silent with
                # `strategy_registry_init_error`). Always read env twice
                # so this block stays independent of execution order.
                _surface_assets: list[str] = []
                # 2026-05-23: default mirrors settings.five_min_assets so the
                # data-surface cache covers every active asset when env is
                # missing. See settings.py:five_min_assets for the gap that
                # blocked v9.5 XRP probabilities all day.
                _five_min_assets = os.environ.get(
                    "FIVE_MIN_ASSETS", "BTC,ETH,SOL,XRP"
                ).split(",")
                _fifteen_min_assets_env = os.environ.get(
                    "FIFTEEN_MIN_ASSETS", "BTC"
                ).split(",")
                for _a in _five_min_assets + _fifteen_min_assets_env:
                    _a_up = (_a or "").strip().upper()
                    if _a_up and _a_up not in _surface_assets:
                        _surface_assets.append(_a_up)
                if not _surface_assets:
                    _surface_assets = ["BTC"]
                self._data_surface_mgr = DataSurfaceManager(
                    v4_base_url=os.environ.get("TIMESFM_URL", "http://localhost:8001"),
                    v4_fallback_url=os.environ.get("TIMESFM_FALLBACK_URL") or None,
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
                    active_assets=_surface_assets,
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
                    # Audit #255 F5 — expose for the runtime TradeRecorder path.
                    # Only set when the earlier EvaluateStrategiesUseCase branch
                    # hasn't already wired it.
                    if getattr(self, "_strategy_decision_repo", None) is None:
                        self._strategy_decision_repo = _decision_repo
                    _trace_repo = PgWindowTraceRepository(
                        db_client=self._db,
                    )
                except Exception as exc:
                    log.warning(
                        "orchestrator.decision_repo_init_error",
                        error=str(exc)[:200],
                    )

                _position_monitor = PositionMonitor(
                    alerter=self._alerter,
                    poly_client=getattr(self, '_poly_client', None),
                    # NOTE: at composition time _pool is None (db.connect()
                    # runs later in start()). Pass the db client so the
                    # monitor can resolve the live pool lazily at write time.
                    db_pool=getattr(self._db, '_pool', None),
                    db_client=self._db,
                )

                self._strategy_registry = StrategyRegistry(
                    config_dir,
                    self._data_surface_mgr,
                    execute_trade_uc=None,  # DEFERRED to start()
                    alerter=self._alerter,
                    decision_repo=_decision_repo,
                    trace_repo=_trace_repo,
                    # v4.4.0: registry upserts v3/v4 surface fields into
                    # window_snapshots on every window eval so SQL analysis
                    # works as first-class columns (not JSONB extraction).
                    db=self._db,
                    position_monitor=_position_monitor,
                )
                self._strategy_registry.load_all()
                # Sync paper_mode from settings — registry defaults to True
                # ("safe") and is only flipped via set_paper_mode() during
                # mode-switch events. On a clean LIVE startup, no switch fires
                # so the registry would stay paper forever and execute_uc
                # never runs. Audit #318 — this was blocking v9_lgb_only fills.
                self._strategy_registry.set_paper_mode(settings.paper_mode)
                # Wire cell-pause lookup into any CellPauseGate gates loaded
                # from YAML. The in-memory snapshot is refreshed every 30s
                # by EngineRuntime.refresh_cell_pause_snapshot().
                if hasattr(self._strategy_registry, "set_cell_pause_lookup"):
                    self._strategy_registry.set_cell_pause_lookup(
                        self._cell_pause_lookup
                    )
                log.info(
                    "orchestrator.strategy_registry_enabled",
                    strategies=self._strategy_registry.strategy_names,
                    paper_mode=settings.paper_mode,
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
                html_ptb_feed=self._html_ptb_feed,
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

        # ── Binance Depth Feed (BTC/ETH/XRP/…, depth20@100ms, flush 1s) ─────
        # PR follow-up to #582: feeds the 4 binance_depth_imbalance_* +
        # binance_spread_pct features in V5FeatureBody. Disabled by
        # default (BINANCE_DEPTH_ENABLED=1 to enable) to keep the
        # initial rollout opt-in until on-box performance is verified.
        # Multi-asset via FIVE_MIN_ASSETS.
        self._binance_depth_feed = None  # instantiated in start()

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

        # ── B1/B2/B3/B4: Manual Trade Use Case + Poller ───────────────────────
        # Wired here so EngineRuntime can pull the poller out and spawn it
        # as an asyncio.Task alongside the main strategy loop.
        self._execute_manual_trade_uc = self._build_execute_manual_trade_uc(settings)
        self._manual_trade_poller = self._build_manual_trade_poller()

    def _build_execute_manual_trade_uc(self, settings):
        """Construct ExecuteManualTradeUseCase with all dependencies wired.

        B4 deliverable — single wiring point. EngineRuntime reads this from
        ``root._execute_manual_trade_uc`` rather than building its own.
        """
        from adapters.alert.manual_trade_alerter import ManualTradeAlerter
        from adapters.clock.system_clock import SystemClock
        from use_cases.execute_manual_trade import ExecuteManualTradeUseCase

        # B3: Use a dedicated chat_id for manual-trade alerts when configured.
        override_chat_id = getattr(settings, "manual_trade_telegram_chat_id", "") or ""
        manual_alerter = ManualTradeAlerter(
            alerter=self._alerter,
            override_chat_id=override_chat_id or None,
        )
        log.info(
            "composition.manual_trade_alerter",
            has_override_chat=bool(override_chat_id),
        )

        # PolymarketClientPort: the existing PolymarketClient implements
        # poll_pending_trades / place_order / get_window_market / get_book
        # via the port interface (duck-typed).  ManualTradeRepository is also
        # duck-typed via the DBClient shim which already has update_status /
        # get_token_ids_from_market_data.
        #
        # WindowStateRepository: the use case uses it only for token-id ring-buffer
        # fallback in the original design; the current implementation delegates to
        # the PolymarketClientPort.get_window_market() and ManualTradeRepository.
        # We pass None here — _resolve_token_id() only calls window_state when the
        # primary miss — and the existing DBClient fallback path still works via
        # manual_trade_repo.get_token_ids().
        return ExecuteManualTradeUseCase(
            polymarket=self._poly_client,
            manual_trade_repo=self._db,  # DBClientLegacyShim implements the repo interface
            window_state=None,            # not required by current implementation
            alerts=manual_alerter,
            clock=SystemClock(),
            risk_manager=self._risk_manager,
            paper_mode=settings.paper_mode,
        )

    def _build_manual_trade_poller(self):
        """Construct ManualTradePoller pointing at the pre-built use case.

        B1 deliverable.
        """
        from tasks.manual_trade_poller import ManualTradePoller

        return ManualTradePoller(
            db=self._db,
            use_case=self._execute_manual_trade_uc,
        )

    # =================================================================
    # TG Narrative V2 wiring (Phase E)
    # See plans/serialized-drifting-clover.md.
    # =================================================================

    def _init_narrative_v2(self) -> None:
        """Construct the Phase A-K narrative pipeline.

        Always builds the wiring; the flag ``tg_narrative_v2_enabled`` only
        affects whether call sites *route through* the new publisher.

        Shadow + tally repos use PG if ``self._db`` is available (the
        ``shadow_decisions`` migration landed 2026-04-17); otherwise fall
        back to in-memory so unit tests / cold starts still boot.
        Polygonscan gateway not yet implemented → in-memory default tags
        wallet-balance changes with no matching tx as DRIFT (TACTICAL),
        which is the safer default.
        """
        from adapters.alert.telegram_renderer import TelegramRenderer
        from adapters.alert.telegram_sender import TelegramSender
        from adapters.onchain.in_memory_onchain import InMemoryOnChainQuery
        from adapters.persistence.in_memory_shadow_decision_repo import (
            InMemoryShadowDecisionRepository,
        )
        from adapters.persistence.in_memory_tally_repo import InMemoryTallyRepo
        from adapters.persistence.pg_shadow_decision_repo import (
            PgShadowDecisionRepository,
        )
        from adapters.persistence.pg_tally_repo import PgTallyRepo
        from use_cases.alerts import PublishAlertUseCase

        self._alert_renderer = TelegramRenderer()
        self._alert_sender = TelegramSender(self._alerter)

        db_client = getattr(self, "_db", None)
        if db_client is not None:
            self._shadow_decision_repo = PgShadowDecisionRepository(
                db_client=db_client
            )
            self._tally_repo = PgTallyRepo(db_client=db_client)
        else:
            self._shadow_decision_repo = InMemoryShadowDecisionRepository()
            self._tally_repo = InMemoryTallyRepo()

        self._onchain_query = InMemoryOnChainQuery()
        self._publish_alert = PublishAlertUseCase(
            renderer=self._alert_renderer,
            alerter=self._alert_sender,
        )

        # Phase G.1 dual-fire hook: TelegramAlerter's legacy send_strategy_trade_alert
        # also emits via the new pipeline when flag is on.
        from adapters.clock.system_clock import SystemClock
        try:
            self._alerter.set_narrative_v2(
                enabled=self._narrative_v2_enabled,
                publish_uc=self._publish_alert,
                clock=SystemClock(),
                tallies=self._tally_repo,
                shadow_repo=self._shadow_decision_repo,
                onchain=self._onchain_query,
                owner_eoas=self._owner_eoa_addresses,
                poly_contracts=frozenset(),  # populated in Phase I hardening
                redeemer_addr=None,
            )
        except Exception as exc:
            # Never fail composition for a dual-fire hook.
            log = structlog.get_logger(__name__)
            log.warning(
                "composition.narrative_v2_hook_failed",
                error=str(exc)[:200],
            )

    # -- Accessors for EngineRuntime + use-case builders -------------------

    @property
    def publish_alert(self):
        """Phase A-K narrative publisher. None-safe: always non-None."""
        return self._publish_alert

    @property
    def alert_renderer(self):
        return self._alert_renderer

    @property
    def alert_sender(self):
        return self._alert_sender

    @property
    def shadow_decision_repo(self):
        return self._shadow_decision_repo

    @property
    def tally_repo(self):
        return self._tally_repo

    @property
    def onchain_query(self):
        return self._onchain_query

    @property
    def narrative_v2_enabled(self) -> bool:
        """Feature flag — True if call sites should route through v2."""
        return self._narrative_v2_enabled

    @property
    def owner_eoa_addresses(self) -> frozenset[str]:
        """Lowercased allowlist for wallet-delta classifier."""
        return self._owner_eoa_addresses

    async def refresh_cell_pause_snapshot(self) -> None:
        """Refresh the in-memory set of active cell pauses from the DB.

        Called every 30s by EngineRuntime so the CellPauseGate has an
        up-to-date view without hitting the DB on every evaluation tick.
        Fire-and-forget: silently swallows errors so a DB hiccup doesn't
        break the evaluation loop.
        """
        try:
            rows = await self._cell_pause_repo.list_active_pauses()
            snapshot: set[tuple] = set()
            for row in rows:
                key = (
                    row.get("strategy_id"),
                    row.get("direction"),
                    row.get("t_band"),
                    row.get("regime"),
                    row.get("session"),
                )
                snapshot.add(key)
            self._active_pause_snapshot = snapshot
        except Exception as exc:
            log.warning(
                "composition.cell_pause_snapshot_refresh_failed",
                error=str(exc)[:200],
            )
