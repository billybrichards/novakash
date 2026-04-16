"""
5-Minute VPIN Strategy — Full-Window Continuous Monitor

Monitors the FULL 5-minute window from open to close.
Fires when composite signal confidence meets the time-tier threshold:
  - DECISIVE (any time): very high confidence → fire early, cheap tokens
  - HIGH (T-120s+): good confidence → fire with decent pricing
  - MODERATE (T-30s+): moderate confidence → fire with standard pricing
  - DEADLINE (T-5s): use best signal seen → fire regardless

Signal components:
  1. Window Delta (weight 5-7)  — primary
  2. VPIN (weight 2-3)          — informed flow detection
  3. Liquidation surge (wt 2)   — CoinGlass 1m (if available)
  4. Long/Short ratio (wt 1.5)  — CoinGlass 1m (if available)
  5. Funding rate (wt 1)        — CoinGlass (if available)
  6. OI delta (wt 1)            — CoinGlass 1m (if available)
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Callable, Awaitable, TYPE_CHECKING

if TYPE_CHECKING:
    from domain.ports import WindowStateRepository

import structlog

from config.constants import FIVE_MIN_ENTRY_OFFSET, FIVE_MIN_EVAL_OFFSETS
from config.runtime_config import runtime
from data.models import MarketState
from data.feeds.polymarket_5min import WindowInfo, WindowState
from execution.fok_ladder import FOKLadder, FOKResult
from execution.order_manager import Order, OrderManager, OrderStatus
from execution.polymarket_client import PolymarketClient
from execution.risk_manager import RiskManager
from signals.vpin import VPINCalculator
from signals.twap_delta import TWAPTracker, TWAPResult
from signals.timesfm_client import TimesFMClient, TimesFMForecast
from signals.window_evaluator import (
    WindowEvaluator,
    WindowState as EvalWindowState,
    WindowSignal,
)
from strategies.base import BaseStrategy

log = structlog.get_logger(__name__)


# ── v8.1 Dynamic entry caps by eval offset ──────────────────────────────────
# Max FOK price at each offset — set conservatively below breakeven WR.
# T-240 (89.5% WR) → cap $0.55 | T-180 (86.4%) → $0.60
# T-120 (84.3%) → $0.65        | T-60  (78.8%) → $0.73 (current)
# Feature-flagged via V2_EARLY_ENTRY_ENABLED env var.
_CAP_T240 = float(os.environ.get("V81_CAP_T240", "0.55"))
_CAP_T180 = float(os.environ.get("V81_CAP_T180", "0.60"))
_CAP_T120 = float(os.environ.get("V81_CAP_T120", "0.65"))
_CAP_T60 = float(os.environ.get("V81_CAP_T60", "0.73"))


def _get_v81_cap(offset: int) -> float:
    """Dynamic cap per eval offset — bands map to cap tiers.

    T-240..T-180: $0.55 — earliest entries, cheapest cap
    T-170..T-120: $0.60 — mid-early
    T-110..T-80:  $0.65 — mid-late
    T-70..T-60:   $0.73 — final offsets, max cap (most certainty)
    """
    if offset >= 180:
        return _CAP_T240  # T-240 to T-180: $0.55
    if offset >= 120:
        return _CAP_T180  # T-170 to T-120: $0.60
    if offset >= 80:
        return _CAP_T120  # T-110 to T-80:  $0.65
    return _CAP_T60  # T-70, T-60:     $0.73


# Legacy dict for backward compat
V81_ENTRY_CAPS: dict[int, float] = {
    240: _CAP_T240,
    180: _CAP_T180,
    120: _CAP_T120,
    60: _CAP_T60,
}


@dataclass
class FiveMinSignal:
    """Signal for 5-minute trading decision."""

    window: WindowInfo
    current_price: float
    current_vpin: float
    delta_pct: float
    confidence: str  # "HIGH", "MODERATE", "LOW", "DECISIVE"
    direction: str  # "UP" or "DOWN"
    cg_modifier: float = 0.0  # CoinGlass confidence modifier applied
    entry_reason: str = "v8_standard"  # v8.1: "v2.2_early_T240", "v8_standard", etc.
    v81_entry_cap: float = 0.73  # v8.1: dynamic FOK price cap for this offset


class FiveMinVPINStrategy(BaseStrategy):
    """
    5-Minute VPIN Strategy for Polymarket Up/Down markets.

    Waits for the T-10s signal from the market discovery feed, then evaluates
    the window delta combined with VPIN to make trading decisions.
    """

    def __init__(
        self,
        order_manager: OrderManager,
        risk_manager: RiskManager,
        poly_client: PolymarketClient,
        vpin_calculator: VPINCalculator,
        alerter=None,
        cg_enhanced=None,
        cg_feeds=None,
        claude_evaluator=None,
        db_client=None,
        on_window_signal: Optional[Callable[[WindowInfo], Awaitable[None]]] = None,
        geoblock_check_fn: Optional[Callable[[], bool]] = None,
        twap_tracker: Optional[TWAPTracker] = None,
        timesfm_client: Optional[TimesFMClient] = None,
        tiingo_adapter=None,  # CA-02: Optional TiingoRestAdapter (MarketFeedPort)
        window_state_repo: Optional[
            "WindowStateRepository"
        ] = None,  # CA-04: Phase 5 dual-write
        evaluate_use_case=None,  # CA-01 Phase 3: EvaluateWindowUseCase (flagged off)
    ) -> None:
        super().__init__(
            name="five_min_vpin",
            order_manager=order_manager,
            risk_manager=risk_manager,
        )
        self._poly = poly_client
        self._vpin = vpin_calculator
        self._alerter = alerter
        self._cg_enhanced = cg_enhanced  # CoinGlassEnhancedFeed (optional)
        self._cg_feeds = cg_feeds or {}
        self._claude_eval = claude_evaluator  # Claude Opus 4.6 evaluator  # Per-asset CG feeds: {"BTC": feed, "ETH": feed, ...}
        self._db = db_client  # DBClient for window snapshot persistence (optional)
        self._geoblock_check_fn = (
            geoblock_check_fn  # G6: Callable to check if geoblock is active
        )
        self._twap = twap_tracker  # v5.7: TWAP-delta direction tracker
        self._timesfm = timesfm_client  # v6.0 — DEPRECATED: use .timesfm_client / .set_timesfm_client()
        self._tiingo_adapter = (
            tiingo_adapter  # CA-02: TiingoRestAdapter (MarketFeedPort) for candle delta
        )
        self._window_state = (
            window_state_repo  # CA-04: Phase 5 WindowStateRepository (dual-write)
        )
        self._evaluate_uc = evaluate_use_case  # CA-01 Phase 3: EvaluateWindowUseCase
        self._timesfm_v2 = (
            None  # v8.1 — DEPRECATED: use .timesfm_v2_client / .set_timesfm_v2_client()
        )
        self._tick_recorder = None  # DEPRECATED: use .set_tick_recorder()
        self._evaluator = WindowEvaluator()

        # CRITICAL: DB-backed dedup — survives engine restarts.
        # Without this, restarts cause duplicate trades per window.
        # Bug discovered Apr 8: windows 1775683200 and 1775683800 got 2 trades each.
        self._traded_windows: set[str] = set()
        self._last_executed_window: Optional[str] = None  # kept for backward compat

        # Consolidated skip notification history: window_key → list of eval ticks
        # Instead of sending 19 individual skip alerts, we batch and send one summary.
        self._window_eval_history: dict[
            str, list
        ] = {}  # DEPRECATED: use .window_eval_history

        # Active window monitoring state (one per window)
        self._active_eval_states: dict[str, EvalWindowState] = {}

        # Window info buffer (populated by feed callbacks)
        self._pending_windows: list = []  # DEPRECATED: use .pending_windows / .append_pending_window()

        # ── v12: Strategy Port decisions (injected by orchestrator per-window) ──
        self._pending_strategy_decisions: Optional[list] = None

        # ── G4: Order rate limiter state ──────────────────────────────────────
        self._order_timestamps: list[float] = []  # timestamps of recent orders
        self._last_order_time: float = 0.0

        # ── G5: Circuit breaker state ─────────────────────────────────────────
        self._circuit_break_until: float = 0.0
        self._consecutive_errors: int = 0

        # Dedup cleanup counter (runs every ~300 market state ticks = ~5-10 min)
        self._dedup_cleanup_counter: int = 0

        self._log = log.bind(strategy="five_min_vpin")

        # Setup window signal callback
        if on_window_signal:
            self._on_window_signal = on_window_signal
        else:
            # Default: store window for evaluation
            self._on_window_signal = self._default_window_handler

    async def _default_window_handler(self, window: WindowInfo) -> None:
        """Default window signal handler - stores window for evaluation."""
        self._pending_windows.append(window)
        # Keep recent windows for token ID lookup
        if not hasattr(self, "_recent_windows"):
            self._recent_windows = []
        self._recent_windows.append(window)
        # Only keep last 10 windows
        if len(self._recent_windows) > 10:
            self._recent_windows = self._recent_windows[-10:]
        self._log.info(
            "window.signal.received",
            asset=window.asset,
            window_ts=window.window_ts,
            open_price=window.open_price,
            up_price=window.up_price,
        )

    # ─── Public API (used by orchestrator — replaces private field access) ───

    @property
    def traded_windows(self) -> set[str]:
        """Read-only view of traded window keys (DB-backed dedup set)."""
        return self._traded_windows

    @property
    def pending_windows(self) -> list:
        """Mutable reference to the pending window queue."""
        return self._pending_windows

    @property
    def recent_windows(self) -> list:
        """Mutable reference to the recent-windows ring buffer."""
        if not hasattr(self, "_recent_windows"):
            self._recent_windows = []
        return self._recent_windows

    @recent_windows.setter
    def recent_windows(self, value: list) -> None:
        self._recent_windows = value

    @property
    def vpin_calculator(self):
        """Public access to the VPIN calculator instance."""
        return self._vpin

    @property
    def current_vpin(self) -> float:
        """Convenience: current VPIN value (0.0 if calculator not set)."""
        return self._vpin.current_vpin if self._vpin else 0.0

    @property
    def timesfm_client(self):
        """Public access to the TimesFM v1 forecast client."""
        return self._timesfm

    @property
    def timesfm_v2_client(self):
        """Public access to the TimesFM v2.2 calibrated probability client."""
        return self._timesfm_v2

    @property
    def window_eval_history(self) -> dict[str, list]:
        """Public access to the per-window evaluation tick history."""
        return self._window_eval_history

    def set_timesfm_client(self, client) -> None:
        """Inject the TimesFM v1 forecast client (post-construction)."""
        self._timesfm = client

    def set_timesfm_v2_client(self, client) -> None:
        """Inject the TimesFM v2.2 calibrated probability client (post-construction)."""
        self._timesfm_v2 = client

    def set_tick_recorder(self, recorder) -> None:
        """Inject the TickRecorder (post-construction)."""
        self._tick_recorder = recorder

    async def evaluate_window(self, window, state) -> None:
        """Public entry point for window evaluation."""
        await self._evaluate_window(window, state)

    def append_pending_window(self, window) -> None:
        """Add a window to the pending evaluation queue."""
        self._pending_windows.append(window)

    def append_recent_window(self, window) -> None:
        """Add a window to the recent-windows ring buffer."""
        if not hasattr(self, "_recent_windows"):
            self._recent_windows = []
        self._recent_windows.append(window)

    def trim_recent_windows(self, max_size: int = 20) -> None:
        """Trim the recent-windows ring buffer to at most max_size entries."""
        if hasattr(self, "_recent_windows") and len(self._recent_windows) > max_size:
            self._recent_windows = self._recent_windows[-max_size:]

    async def on_window(self, window) -> None:
        """Hook for ProcessFiveMinWindowUseCase.
        Queue management is handled by the caller via append_pending_window /
        append_recent_window. This method is a no-op placeholder — concrete
        evaluation runs through the strategy registry (evaluate_all).
        """
        pass

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the strategy."""
        if not runtime.five_min_enabled:
            self._log.info("strategy.disabled", reason="runtime.five_min_enabled=false")
            return

        # ── Load recently traded windows from DB to survive restarts ─────
        if self._db:
            try:
                self._traded_windows = await self._db.load_recent_traded_windows(
                    hours=2
                )
                if self._traded_windows:
                    self._log.info(
                        "strategy.dedup_restored",
                        count=len(self._traded_windows),
                        windows=sorted(self._traded_windows),
                    )
            except Exception as exc:
                self._log.error("strategy.dedup_restore_failed", error=str(exc))

        self._running = True
        self._log.info("strategy.started")

    async def stop(self) -> None:
        """Stop the strategy."""
        self._running = False
        self._log.info("strategy.stopped")

    def _cleanup_old_traded_windows(self, max_age_seconds: int = 7200) -> None:
        """Remove traded window keys older than max_age_seconds (default 2h).

        Called periodically to prevent unbounded memory growth.
        Window keys are "{ASSET}-{unix_ts}", so we parse the timestamp suffix.
        """
        import time

        cutoff = int(time.time()) - max_age_seconds
        stale = {k for k in self._traded_windows if self._parse_window_ts(k) < cutoff}
        if stale:
            self._traded_windows -= stale
            self._log.debug(
                "dedup.cleanup", removed=len(stale), remaining=len(self._traded_windows)
            )

    @staticmethod
    def _parse_window_ts(key: str) -> int:
        """Extract the unix timestamp from a window key like 'BTC-1775683200'."""
        try:
            return int(key.rsplit("-", 1)[-1])
        except (ValueError, IndexError):
            return 0

    # ─── Market State Handler ─────────────────────────────────────────────────

    async def on_market_state(self, state: MarketState) -> None:
        """
        Called by the orchestrator on every market state update (~every 1-3s).

        REVERTED TO MORNING STRATEGY (2026-04-02):
        Continuous evaluator disabled — it was overconfident and unprofitable.
        Using legacy T-60s single-shot evaluation instead.
        Legacy path fires via _evaluate_window() called from orchestrator
        on each window signal.
        """
        if not self._running or not runtime.five_min_enabled:
            return

        # Periodic cleanup of old traded-window keys (every ~300 ticks ≈ 5-10 min)
        self._dedup_cleanup_counter += 1
        if self._dedup_cleanup_counter >= 300:
            self._dedup_cleanup_counter = 0
            self._cleanup_old_traded_windows()

        # Still register windows (needed for token ID lookup)
        if self._pending_windows:
            for window in self._pending_windows:
                window_key = f"{window.asset}-{window.window_ts}"
                if (
                    window_key not in self._active_eval_states
                    and window_key not in self._traded_windows
                ):
                    self._active_eval_states[window_key] = EvalWindowState(
                        window_ts=window.window_ts,
                        open_price=window.open_price or 0,
                    )
                    self._log.info(
                        "window.monitoring_started",
                        window_key=window_key,
                        open_price=window.open_price,
                    )
            self._pending_windows.clear()

        # ── CONTINUOUS EVALUATOR DISABLED ──────────────────────────────
        # Reverted to morning's T-60s single-shot strategy.
        # The continuous evaluator was overconfident and lost money.
        # Legacy _evaluate_window() is called by orchestrator on each
        # window signal (T-60s before close).
        #
        # Morning session: +$93 with simple delta + VPIN at T-60s
        # Afternoon with continuous evaluator: -$258
        # ──────────────────────────────────────────────────────────────
        pass

    # ─── Legacy Window Handler (still used by feed callback) ──────────────────

    async def _evaluate_window(self, window: WindowInfo, state: MarketState) -> None:
        """
        Evaluate a window at the T-offset signal.

        Multi-offset support (v5.9): fires at each configured FIVE_MIN_EVAL_OFFSETS
        (e.g. T-90s, then T-60s). Deduplicates: won't trade the same window twice,
        but will still record the window snapshot and send alerts for all offsets.
        """

        # ── CA-01 Phase 3: delegate to EvaluateWindowUseCase when flagged on ──
        if (
            os.environ.get("ENGINE_USE_CLEAN_EVALUATE_WINDOW", "true").lower() != "false"
            and self._evaluate_uc is not None
        ):
            await self._evaluate_uc.execute(window, state)
            return
        # -- End Phase 3 delegation --

        window_key = f"{window.asset}-{window.window_ts}"
        eval_offset = getattr(window, "eval_offset", None)

        # Already TRADED this window — skip evaluation entirely
        # CRITICAL: DB-backed dedup — survives engine restarts.
        if window_key in self._traded_windows:
            self._log.debug(
                "window.already_traded",
                window=window_key,
                eval_offset=eval_offset,
            )
            return

        # Get current price for this asset
        # BTC futures price from Binance futures websocket (for VPIN / fallback)
        # BTC spot price from Binance spot websocket (for delta — oracle-aligned)
        # Other assets: fetch spot price from Binance REST API
        if window.asset == "BTC":
            current_price = float(state.btc_price) if state.btc_price else None
            # Prefer spot price for delta_binance (Polymarket resolves via Chainlink
            # oracle on SPOT, not futures — futures has systematic basis skew)
            spot_price = (
                float(state.btc_spot_price) if state.btc_spot_price else current_price
            )
        else:
            current_price = await self._fetch_current_price(window.asset)
            spot_price = current_price

        if current_price is None:
            self._log.warning("evaluate.no_current_price", asset=window.asset)
            return

        # Get window open price
        open_price = window.open_price
        if open_price is None:
            self._log.warning("evaluate.no_open_price")
            return

        # ── Multi-source delta calculation (v8.0) ─────────────────────────
        # Feature flag: DELTA_PRICE_SOURCE (runtime_config.delta_price_source)
        # 'tiingo'    — Tiingo REST 5m candle open/close (oracle-aligned, default)
        # 'chainlink' — Chainlink price from DB (legacy)
        # 'binance'   — Binance websocket price (legacy baseline)
        # Tiingo REST candle gives a true 5m open→close delta aligned with the
        # Chainlink oracle settlement, achieving 96.9% accuracy vs Binance's 71.6%.
        from config.runtime_config import runtime as _rt_cfg

        _delta_source = _rt_cfg.delta_price_source  # env-only, not DB-synced

        # delta_binance uses SPOT price (oracle-aligned), not futures.
        # Futures aggTrades are still used for VPIN volume accumulation separately.
        binance_price = spot_price if spot_price is not None else current_price
        delta_binance = (binance_price - open_price) / open_price * 100

        # -- Tiingo 5m candle delta (v8.0, CA-02 adapter extraction) --------
        # Prefer injected TiingoRestAdapter if available; otherwise fall
        # back to inline HTTP with env-sourced API key (TIINGO_API_KEY).
        # NOTE: hardcoded API key removed -- security fix CA-02.
        _tiingo_open: Optional[float] = None
        _tiingo_close: Optional[float] = None
        delta_tiingo: Optional[float] = None
        _tiingo_candle_source = "none"

        if self._tiingo_adapter is not None:
            # -- Adapter path (preferred) ---------------------------------
            delta_tiingo = await self._tiingo_adapter.get_window_delta(
                asset=window.asset,
                window_ts=window.window_ts,
                open_price=open_price,
            )
            if delta_tiingo is not None:
                _tiingo_candle_source = "rest_candle"
        else:
            # -- Inline fallback (backward compat, reads key from env) -----
            _tiingo_api_key = os.environ.get("TIINGO_API_KEY", "")
            if _tiingo_api_key:
                _tiingo_asset_ticker = f"{window.asset.lower()}usd"
                _tiingo_window_start = datetime.fromtimestamp(
                    window.window_ts, tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                _tiingo_window_end = datetime.fromtimestamp(
                    window.window_ts + 300, tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                try:
                    import aiohttp as _aiohttp_tiingo

                    _tiingo_url = (
                        f"https://api.tiingo.com/tiingo/crypto/prices"
                        f"?tickers={_tiingo_asset_ticker}"
                        f"&startDate={_tiingo_window_start}"
                        f"&endDate={_tiingo_window_end}"
                        f"&resampleFreq=5min"
                        f"&token={_tiingo_api_key}"
                    )
                    async with _aiohttp_tiingo.ClientSession() as _ts:
                        async with _ts.get(
                            _tiingo_url,
                            timeout=_aiohttp_tiingo.ClientTimeout(total=3.0),
                        ) as _tr:
                            if _tr.status == 200:
                                _tiingo_data = await _tr.json()
                                if (
                                    _tiingo_data
                                    and isinstance(_tiingo_data, list)
                                    and len(_tiingo_data) > 0
                                ):
                                    _price_data = _tiingo_data[0].get("priceData", [])
                                    if _price_data and len(_price_data) > 0:
                                        _tiingo_open = (
                                            float(_price_data[0].get("open", 0) or 0)
                                            or None
                                        )
                                        _tiingo_close = (
                                            float(_price_data[-1].get("close", 0) or 0)
                                            or None
                                        )
                                        if (
                                            _tiingo_open
                                            and _tiingo_close
                                            and _tiingo_open > 0
                                        ):
                                            delta_tiingo = (
                                                (_tiingo_close - _tiingo_open)
                                                / _tiingo_open
                                                * 100
                                            )
                                            _tiingo_candle_source = "rest_candle"
                                            self._log.info(
                                                "tiingo.candle_fetched",
                                                asset=window.asset,
                                                open=f"${_tiingo_open:,.2f}",
                                                close=f"${_tiingo_close:,.2f}",
                                                delta=f"{delta_tiingo:+.4f}%",
                                                candles=len(_price_data),
                                            )
                except Exception as _te:
                    self._log.debug("tiingo.candle_fetch_failed", error=str(_te)[:80])
            else:
                self._log.debug(
                    "tiingo.no_api_key",
                    msg="TIINGO_API_KEY not set, skipping REST candle fetch",
                )

        # Tiingo DB fallback: use latest tick from ticks_tiingo if REST unavailable
        _tiingo_db_price: Optional[float] = None
        if delta_tiingo is None and self._db:
            try:
                _tiingo_db_price = await self._db.get_latest_tiingo_price(window.asset)
                if _tiingo_db_price:
                    _tiingo_open = open_price  # use window open as reference
                    _tiingo_close = _tiingo_db_price
                    delta_tiingo = (_tiingo_db_price - open_price) / open_price * 100
                    _tiingo_candle_source = "db_tick"
                    self._log.debug(
                        "tiingo.db_fallback",
                        asset=window.asset,
                        price=f"${_tiingo_db_price:,.2f}",
                        delta=f"{delta_tiingo:+.4f}%",
                    )
            except Exception:
                pass

        # ── Chainlink + Binance delta (always compute for comparison) ─────
        _chainlink_price: Optional[float] = None
        if self._db:
            try:
                _chainlink_price = await self._db.get_latest_chainlink_price(
                    window.asset
                )
            except Exception:
                pass

        delta_chainlink = (
            ((_chainlink_price - open_price) / open_price * 100)
            if _chainlink_price
            else None
        )

        # ── Direction consensus across all sources ─────────────────────────
        _dirs = []
        if delta_binance is not None:
            _dirs.append("UP" if delta_binance > 0 else "DOWN")
        if delta_chainlink is not None:
            _dirs.append("UP" if delta_chainlink > 0 else "DOWN")
        if delta_tiingo is not None:
            _dirs.append("UP" if delta_tiingo > 0 else "DOWN")

        if len(_dirs) >= 2:
            _up_count = _dirs.count("UP")
            _down_count = _dirs.count("DOWN")
            if _up_count == len(_dirs) or _down_count == len(_dirs):
                price_consensus = "AGREE"
            elif abs(_up_count - _down_count) >= 1 and len(_dirs) >= 3:
                price_consensus = "MIXED"
            elif len(_dirs) == 2 and _up_count != _down_count:
                price_consensus = "MIXED"
            else:
                price_consensus = "DISAGREE"
        else:
            price_consensus = "AGREE"  # Only one source — no conflict

        # ── Select primary delta based on DELTA_PRICE_SOURCE ──────────────
        # v9.0: For 5m Polymarket markets, Chainlink is the resolution oracle.
        # Default priority: chainlink → tiingo fallback → binance fallback
        # Config override via DELTA_PRICE_SOURCE still respected.
        if _delta_source == "chainlink" and delta_chainlink is not None:
            delta_pct = delta_chainlink
            _price_source_used = "chainlink"
        elif _delta_source == "chainlink" and delta_tiingo is not None:
            # Chainlink unavailable — fall back to tiingo
            delta_pct = delta_tiingo
            _price_source_used = f"tiingo_fallback_{_tiingo_candle_source}"
            self._log.info(
                "evaluate.chainlink_unavailable_tiingo_fallback",
                asset=window.asset,
            )
        elif _delta_source == "tiingo" and delta_tiingo is not None:
            delta_pct = delta_tiingo
            _price_source_used = f"tiingo_{_tiingo_candle_source}"
        elif _delta_source == "tiingo" and delta_chainlink is not None:
            # Tiingo unavailable — fall back to chainlink
            delta_pct = delta_chainlink
            _price_source_used = "chainlink_fallback"
            self._log.info(
                "evaluate.tiingo_unavailable_chainlink_fallback",
                asset=window.asset,
                tiingo_source=_tiingo_candle_source,
            )
        elif _delta_source == "consensus":
            if price_consensus != "AGREE":
                self._log.info(
                    "evaluate.consensus_skip",
                    asset=window.asset,
                    price_consensus=price_consensus,
                    delta_binance=f"{delta_binance:+.4f}%"
                    if delta_binance is not None
                    else "N/A",
                    delta_chainlink=f"{delta_chainlink:+.4f}%"
                    if delta_chainlink is not None
                    else "N/A",
                    delta_tiingo=f"{delta_tiingo:+.4f}%"
                    if delta_tiingo is not None
                    else "N/A",
                )
                return
            # delta_binance is now SPOT (via binance_spot_feed), not futures.
            # Chainlink preferred as oracle-aligned for 5m Polymarket.
            delta_pct = (
                delta_chainlink
                if delta_chainlink is not None
                else (delta_tiingo if delta_tiingo is not None else delta_binance)
            )
            _price_source_used = "consensus"
        else:
            # Fallback: chainlink → tiingo → binance (oracle-aligned first)
            if delta_chainlink is not None:
                delta_pct = delta_chainlink
                _price_source_used = "chainlink"
            elif delta_tiingo is not None:
                delta_pct = delta_tiingo
                _price_source_used = f"tiingo_{_tiingo_candle_source}"
            else:
                delta_pct = delta_binance
                _price_source_used = "binance"

        # Flag LOW confidence if primary source and Binance disagree on direction
        _price_confidence_flag = "OK"
        _primary_dir = "UP" if delta_pct > 0 else "DOWN"
        _bn_dir = "UP" if delta_binance > 0 else "DOWN"
        if _primary_dir != _bn_dir and _price_source_used not in (
            "binance",
            "binance_fallback",
        ):
            _price_confidence_flag = "LOW"
            self._log.warning(
                "evaluate.price_source_disagreement",
                asset=window.asset,
                primary_dir=_primary_dir,
                binance_dir=_bn_dir,
                delta_primary=f"{delta_pct:+.4f}%",
                delta_binance=f"{delta_binance:+.4f}%",
                price_source_used=_price_source_used,
            )

        self._log.info(
            "evaluate.multi_source_delta",
            asset=window.asset,
            source=_price_source_used,
            delta_pct=f"{delta_pct:+.4f}%",
            delta_binance=f"{delta_binance:+.4f}%",
            delta_chainlink=f"{delta_chainlink:+.4f}%"
            if delta_chainlink is not None
            else "N/A",
            delta_tiingo=f"{delta_tiingo:+.4f}%" if delta_tiingo is not None else "N/A",
            tiingo_open=f"${_tiingo_open:,.2f}" if _tiingo_open else "N/A",
            tiingo_close=f"${_tiingo_close:,.2f}" if _tiingo_close else "N/A",
            tiingo_source=_tiingo_candle_source,
            consensus=price_consensus,
            confidence_flag=_price_confidence_flag,
        )

        # Get current VPIN
        current_vpin = self._vpin.current_vpin

        # ── TWAP-Delta evaluation (v5.7) ─────────────────────────────────
        twap_result: Optional[TWAPResult] = None
        if self._twap:
            twap_result = self._twap.evaluate(
                asset=window.asset,
                window_ts=window.window_ts,
                current_price=current_price,
                gamma_up_price=window.up_price,
                gamma_down_price=window.down_price,
            )
            if twap_result:
                self._log.info(
                    "evaluate.twap_result",
                    asset=window.asset,
                    summary=twap_result.summary(),
                )
                # TWAP skip gate: if TWAP says skip (Gamma block, mixed signal, priced in)
                if twap_result.should_skip:
                    self._log.info(
                        "evaluate.twap_skip",
                        asset=window.asset,
                        reason=twap_result.skip_reason,
                        gamma_gate=twap_result.gamma_gate,
                        trend_pct=f"{twap_result.trend_pct:.2f}",
                    )
            # Cleanup window tracking data after evaluation
            self._twap.cleanup_window(window.asset, window.window_ts)

        # ── TimesFM forecast (v6.0 comparison data for alerts) ────────────
        # v8.0 Phase 3: Skip TimesFM fetch entirely when both timesfm_enabled
        # and timesfm_agreement_enabled are False — saves latency on every window.
        # If timesfm_enabled=True but timesfm_agreement_enabled=False, still fetch
        # for monitoring/recording but gate logic is skipped in _evaluate_signal.
        timesfm_forecast: Optional[TimesFMForecast] = None
        if self._timesfm and runtime.timesfm_enabled:
            try:
                # Calculate seconds until this window closes
                _window_close_ts = window.window_ts + window.duration_secs
                _seconds_to_close = max(1, int(_window_close_ts - time.time()))
                timesfm_forecast = await self._timesfm.get_forecast(
                    open_price=open_price,
                    seconds_to_close=_seconds_to_close,
                )
                if timesfm_forecast and not timesfm_forecast.error:
                    self._log.info(
                        "evaluate.timesfm_forecast",
                        asset=window.asset,
                        direction=timesfm_forecast.direction,
                        confidence=f"{timesfm_forecast.confidence:.2f}",
                        predicted_close=f"${timesfm_forecast.predicted_close:,.2f}",
                        gate_active=runtime.timesfm_agreement_enabled,
                    )
                    # ── TickRecorder: passive recording only ──────────────
                    if self._tick_recorder:
                        asyncio.create_task(
                            self._tick_recorder.record_timesfm_forecast(
                                timesfm_forecast,
                                asset=window.asset,
                                window_ts=int(window.window_ts),
                            )
                        )
            except Exception as exc:
                self._log.debug("evaluate.timesfm_fetch_failed", error=str(exc))

        # ── Determine regime early (needed by v9 gate logging) ──────────
        from config.runtime_config import runtime as _runtime

        if current_vpin >= _runtime.vpin_cascade_direction_threshold:
            _snap_regime = "CASCADE"
        elif current_vpin >= _runtime.vpin_informed_threshold:
            _snap_regime = "TRANSITION"
        elif current_vpin >= 0.45:
            _snap_regime = "NORMAL"
        else:
            _snap_regime = "CALM"

        # ── Default v9 variables (must exist even when v10 is active) ──────
        _v9_agreement = False
        _v9_source_agree = None
        _v9_direction_override = None

        # ── v10 DUNE-Gated Pipeline (when V10_DUNE_ENABLED=true) ─────────
        # Replaces v9's inline gates with clean composable pipeline.
        # Falls through to v9 inline gates when disabled.
        _v10_enabled = os.environ.get("V10_DUNE_ENABLED", "false").lower() == "true"
        if _v10_enabled:
            # CRITICAL: DB-backed dedup — survives engine restarts.
            # Without this, restarts cause duplicate trades per window.
            _window_key_v10 = f"{window.asset}-{window.window_ts}"
            if _window_key_v10 in self._traded_windows:
                return  # Already traded this window
            from signals.gates import (
                GateContext,
                GatePipeline,
                EvalOffsetBoundsGate,  # v10.6 DS-01 (default OFF via V10_6_ENABLED)
                SourceAgreementGate,
                DeltaMagnitudeGate,
                TakerFlowGate,
                CGConfirmationGate,
                DuneConfidenceGate,
                SpreadGate,
                DynamicCapGate,
                CoinGlassVetoGate,  # legacy fallback
            )
            from signals.v2_feature_body import build_v5_feature_body

            _cg = self._cg_enhanced.snapshot if self._cg_enhanced is not None else None
            # v10.3: pass Gamma prices for spread gate
            _gamma_up = getattr(self, "_gamma_up_price", None)
            _gamma_down = getattr(self, "_gamma_down_price", None)
            _dchain = delta_chainlink if "delta_chainlink" in locals() else None
            _dtii = delta_tiingo if "delta_tiingo" in locals() else None
            _dbin = delta_binance if "delta_binance" in locals() else None
            _tii_close = _tiingo_close if "_tiingo_close" in locals() else None
            _src_used = _price_source_used if "_price_source_used" in locals() else None
            _twap_d = None
            if "twap_result" in locals() and twap_result is not None:
                _twap_d = twap_result.twap_delta_pct

            # v11.1: Build the v5 push-mode feature body ONCE at context
            # construction time. The DuneConfidenceGate reads this body
            # directly via `ctx.v5_features`, guaranteeing the v10
            # pipeline call and the v8.1 early-entry call at line ~1478
            # use the same extraction logic. Gate booleans are NOT set
            # here because v10 pipeline gates have different semantics
            # than the v8.0 signal_evaluations columns the model was
            # trained on — leaving them None lets LightGBM use its
            # missing-default branch, matching training behaviour.
            _v5_body = build_v5_feature_body(
                eval_offset=getattr(window, "eval_offset", None),
                vpin=current_vpin,
                delta_pct=delta_pct,
                twap_delta=_twap_d,
                clob_up_price=_gamma_up,
                clob_down_price=_gamma_down,
                binance_price=current_price,
                tiingo_close=_tii_close,
                delta_binance=_dbin,
                delta_chainlink=_dchain,
                delta_tiingo=_dtii,
                regime=_snap_regime,
                delta_source=_src_used,
                # gate_* and prev_v2_probability_up stay None — they're
                # not yet resolved at this point in the function.
            )

            ctx = GateContext(
                delta_chainlink=_dchain,
                delta_tiingo=_dtii,
                delta_binance=_dbin,
                delta_pct=delta_pct,
                vpin=current_vpin,
                regime=_snap_regime,
                asset=window.asset,
                eval_offset=getattr(window, "eval_offset", None),
                window_ts=window.window_ts,
                cg_snapshot=_cg,
                gamma_up_price=_gamma_up,
                gamma_down_price=_gamma_down,
                # v11.1: new scalars for the DUNE gate's V5FeatureBody
                # fallback path (if `ctx.v5_features` is unset)
                twap_delta=_twap_d,
                tiingo_close=_tii_close,
                current_price=current_price,
                delta_source=_src_used,
                prev_v2_probability_up=None,  # v10 pipeline runs before any v2 query
                # v11.1: attached feature body — DuneConfidenceGate prefers
                # this over rebuilding from scalars
                v5_features=_v5_body,
            )
            # v10.5 + v10.6 DS-01: 8-gate decision surface pipeline
            # Order: EvalOffsetBounds → Agreement → DeltaMagnitude → TakerFlow → CGConfirm → DUNE → Spread → Cap
            #
            # G0 (EvalOffsetBoundsGate) is the v10.6 DS-01 gate — inserted
            # FIRST because it's the cheapest check in the pipeline (no
            # external calls, just two integer comparisons) AND it's a
            # hard-block that short-circuits the rest of the pipeline.
            # CRITICAL: defaults to OFF via V10_6_ENABLED=false. When
            # disabled, the gate is a pure no-op returning passed=True
            # unconditionally — the downstream 7-gate behaviour is
            # bit-for-bit identical to before this PR. Operator flips
            # V10_6_ENABLED=true on the host to turn hard-block on.
            pipeline = GatePipeline(
                [
                    EvalOffsetBoundsGate(),  # G0: v10.6 DS-01 (default OFF via V10_6_ENABLED)
                    SourceAgreementGate(),  # G1: CL+TI agree (94.7% WR)
                    DeltaMagnitudeGate(),  # G2: |delta| floor (v10.5 — blocks noise trades)
                    TakerFlowGate(),  # G3: CG taker hard gate + threshold modifier
                    CGConfirmationGate(),  # G4: CG 3-signal bonus (-0.02)
                    DuneConfidenceGate(
                        dune_client=self._timesfm_v2
                    ),  # G5: ELM + all modifiers
                    SpreadGate(),  # G6: Polymarket spread check
                    DynamicCapGate(),  # G7: cap = dune_p - 0.05, max $0.68
                ]
            )
            pipe_result = await pipeline.evaluate(ctx)
            if pipe_result.passed:
                direction = pipe_result.direction
                confidence = (
                    "HIGH"
                    if (
                        ctx.dune_probability_up
                        and max(ctx.dune_probability_up, 1 - ctx.dune_probability_up)
                        > 0.75
                    )
                    else "MODERATE"
                )
                _order_type = os.environ.get("ORDER_TYPE", "FAK").upper()
                # v10.3: include CG modifier info in entry reason for debugging
                _cg_tag = ""
                if ctx.cg_threshold_modifier != 0:
                    _cg_tag = f"_CG{ctx.cg_threshold_modifier:+.02f}"
                elif ctx.cg_bonus > 0:
                    _cg_tag = f"_CGB{ctx.cg_bonus:.02f}"
                signal = FiveMinSignal(
                    window=window,
                    current_price=current_price,
                    current_vpin=current_vpin,
                    delta_pct=delta_pct,
                    confidence=confidence,
                    direction=direction,
                    cg_modifier=ctx.cg_threshold_modifier,
                    entry_reason=f"v10_DUNE_{_snap_regime}_T{ctx.eval_offset}_{_order_type}{_cg_tag}",
                    v81_entry_cap=pipe_result.cap or 0.65,
                )
                # Mark window as traded (dedup for subsequent 2s evals + restart survival)
                self._traded_windows.add(_window_key_v10)
                self._last_executed_window = _window_key_v10  # backward compat
                # CA-04 Phase 5: dual-write to WindowStateRepository (alongside in-memory set)
                if self._window_state is not None:
                    try:
                        await self._window_state.mark_traded(_window_key_v10, "pending")
                    except Exception as _ws_exc:
                        self._log.warning(
                            "window_state.mark_traded_failed",
                            key=_window_key_v10,
                            error=str(_ws_exc)[:80],
                        )

                # v10.3: extract gate data for Telegram alerts + DB logging
                _v103_gate_data = {}
                for _gr in pipe_result.gate_results:
                    if _gr.gate_name == "dune_confidence":
                        _v103_gate_data["v103_dune_p"] = _gr.data.get("dune_p")
                        _v103_gate_data["v103_threshold"] = _gr.data.get("threshold")
                        _v103_gate_data["v103_threshold_base"] = (
                            self._regime_thresholds_cache.get(_snap_regime)
                            if hasattr(self, "_regime_thresholds_cache")
                            else None
                        )
                        _v103_gate_data["v103_down_penalty"] = _gr.data.get(
                            "down_penalty", 0
                        )
                        _v103_gate_data["v103_cg_modifier"] = _gr.data.get(
                            "cg_modifier", 0
                        )
                        _v103_gate_data["v103_cg_bonus"] = _gr.data.get("cg_bonus", 0)
                    elif _gr.gate_name == "taker_flow":
                        _v103_gate_data["v103_taker_status"] = (
                            "both_opposing"
                            if _gr.data.get("taker_opposing")
                            and _gr.data.get("smart_opposing")
                            else "opposing"
                            if _gr.data.get("taker_opposing")
                            else "aligned"
                            if _gr.data.get("taker_aligned")
                            else "neutral"
                        )
                        _v103_gate_data["v103_taker_buy_pct"] = _gr.data.get(
                            "buy_pct", 50
                        )
                        _v103_gate_data["v103_taker_sell_pct"] = 100 - _gr.data.get(
                            "buy_pct", 50
                        )
                    elif _gr.gate_name == "cg_confirmation":
                        _v103_gate_data["v103_cg_confirms"] = _gr.data.get(
                            "confirms", 0
                        )
                        _v103_gate_data["v103_cg_details"] = _gr.data.get("details", [])
                    elif _gr.gate_name == "spread_gate":
                        _v103_gate_data["v103_spread_pct"] = _gr.data.get("spread_pct")
                _v103_gate_data["v103_gate_results"] = [
                    {"name": g.gate_name, "passed": g.passed, "reason": g.reason[:60]}
                    for g in pipe_result.gate_results
                ]
                # Store on signal object so alert builder can access it
                signal._v10_gate_data = _v103_gate_data

                self._log.info(
                    "v10.trade",
                    direction=direction,
                    cap=f"${pipe_result.cap:.2f}" if pipe_result.cap else "?",
                    dune_p=f"{pipe_result.dune_p:.3f}" if pipe_result.dune_p else "N/A",
                    regime=_snap_regime,
                    offset=ctx.eval_offset,
                    cg_taker=_v103_gate_data.get("v103_taker_status", "?"),
                    cg_confirms=_v103_gate_data.get("v103_cg_confirms", 0),
                )

                # Write signal_evaluation with decision='TRADE' before executing
                if self._db:
                    try:
                        await self._db.write_signal_evaluation(
                            {
                                "window_ts": window.window_ts,
                                "asset": window.asset,
                                "timeframe": "5m",
                                "eval_offset": ctx.eval_offset,
                                "delta_chainlink": ctx.delta_chainlink,
                                "delta_tiingo": ctx.delta_tiingo,
                                "delta_binance": ctx.delta_binance,
                                "delta_pct": delta_pct,
                                "vpin": current_vpin,
                                "regime": _snap_regime,
                                "decision": "TRADE",
                                "gate_passed": True,
                                "gate_failed": None,
                                "v2_probability_up": ctx.dune_probability_up,
                                "v2_direction": direction,
                            }
                        )
                    except Exception as _sig_exc:
                        self._log.warning(
                            "db.v10_trade_signal_eval_failed", error=str(_sig_exc)[:100]
                        )

                # Write window_snapshot with TWAP + CG data before executing
                # (v10 returns early and would skip the v9 snapshot builder)
                if self._db:
                    try:
                        _cg = (
                            self._cg_enhanced.snapshot
                            if self._cg_enhanced is not None
                            else None
                        )
                        await self._db.write_window_snapshot(
                            {
                                "window_ts": window.window_ts,
                                "asset": window.asset,
                                "timeframe": "5m",
                                "open_price": open_price,
                                "close_price": current_price,
                                "delta_pct": delta_pct,
                                "delta_chainlink": ctx.delta_chainlink,
                                "delta_tiingo": ctx.delta_tiingo,
                                "delta_binance": ctx.delta_binance,
                                "vpin": current_vpin,
                                "regime": _snap_regime,
                                "btc_price": current_price,
                                "direction": direction,
                                "confidence": confidence,
                                "trade_placed": True,
                                "skip_reason": None,
                                "engine_version": "v10.3",
                                "eval_offset": ctx.eval_offset,
                                # TWAP data (captured before v10 pipeline)
                                "twap_delta_pct": twap_result.twap_delta_pct
                                if twap_result
                                else None,
                                "twap_direction": twap_result.twap_direction
                                if twap_result
                                else None,
                                "twap_agreement_score": twap_result.agreement_score
                                if twap_result
                                else None,
                                "twap_n_ticks": twap_result.n_ticks
                                if twap_result
                                else None,
                                # CoinGlass
                                "cg_connected": _cg.connected if _cg else False,
                                "cg_oi_usd": _cg.oi_usd if _cg else None,
                                "cg_oi_delta_pct": _cg.oi_delta_pct_1m if _cg else None,
                                "cg_long_pct": _cg.long_pct if _cg else None,
                                "cg_top_long_pct": _cg.top_position_long_pct
                                if _cg
                                else None,
                                "cg_top_short_pct": _cg.top_position_short_pct
                                if _cg
                                else None,
                                "cg_taker_buy_usd": _cg.taker_buy_volume_1m
                                if _cg
                                else None,
                                "cg_taker_sell_usd": _cg.taker_sell_volume_1m
                                if _cg
                                else None,
                                "cg_funding_rate": _cg.funding_rate if _cg else None,
                                # Gamma
                                "gamma_up_price": float(window.up_price)
                                if window.up_price
                                else None,
                                "gamma_down_price": float(window.down_price)
                                if window.down_price
                                else None,
                                # DUNE
                                "v2_probability_up": ctx.dune_probability_up,
                                "v2_direction": direction,
                            }
                        )
                    except Exception as _snap_exc:
                        self._log.warning(
                            "db.v10_trade_snapshot_failed", error=str(_snap_exc)[:80]
                        )

                # gate_audit writes retired — gate_check_traces is the successor

                return  # Done — one trade per window
            else:
                signal = None
                self._last_skip_reason = pipe_result.skip_reason or "v10 gate failed"
                if self._db:
                    try:
                        await self._db.write_signal_evaluation(
                            {
                                "window_ts": window.window_ts,
                                "asset": window.asset,
                                "timeframe": "5m",
                                "eval_offset": getattr(window, "eval_offset", None),
                                "delta_chainlink": ctx.delta_chainlink,
                                "delta_tiingo": ctx.delta_tiingo,
                                "delta_binance": ctx.delta_binance,
                                "delta_pct": delta_pct,
                                "vpin": current_vpin,
                                "regime": _snap_regime,
                                "decision": "SKIP",
                                "gate_passed": False,
                                "gate_failed": pipe_result.failed_gate or "unknown",
                                "v2_probability_up": ctx.dune_probability_up,
                            }
                        )
                    except Exception:
                        pass
                # gate_audit writes retired — gate_check_traces is the successor
                # v10 skip: don't fall through to v9 code — return after writing snapshot
                # (the window_snapshot write happens below, so we must NOT return here)
        else:
            # ── v9.0 Source Agreement Gate ────────────────────────────────────
            # When CL+TI agree on direction, WR = 94.7%. When they disagree, 9.1%.
            # This is the single most impactful filter. Feature-flagged for rollback.
            _v9_agreement = (
                os.environ.get("V9_SOURCE_AGREEMENT", "false").lower() == "true"
            )
            _v9_source_agree = None  # None=unknown, True=agree, False=disagree
            _v9_direction_override = None

            if delta_chainlink is not None and delta_tiingo is not None:
                _cl_dir = "UP" if delta_chainlink > 0 else "DOWN"
                _ti_dir = "UP" if delta_tiingo > 0 else "DOWN"
                _v9_source_agree = _cl_dir == _ti_dir

                if _v9_source_agree:
                    _v9_direction_override = (
                        _cl_dir  # Both agree → use shared direction
                    )
                    self._log.info(
                        "v9.source_agree",
                        cl=_cl_dir,
                        ti=_ti_dir,
                        delta_cl=f"{delta_chainlink:+.4f}%",
                        delta_ti=f"{delta_tiingo:+.4f}%",
                    )
                else:
                    self._log.info(
                        "v9.source_disagree",
                        cl=_cl_dir,
                        ti=_ti_dir,
                        delta_cl=f"{delta_chainlink:+.4f}%",
                        delta_ti=f"{delta_tiingo:+.4f}%",
                        gate_active=_v9_agreement,
                    )
                    if _v9_agreement:
                        self._last_skip_reason = (
                            f"v9: CL={_cl_dir} TI={_ti_dir} DISAGREE"
                        )
                        # Log to signal_evaluations before skipping
                        if self._db:
                            try:
                                await self._db.write_signal_evaluation(
                                    {
                                        "window_ts": window.window_ts,
                                        "asset": window.asset,
                                        "timeframe": "5m",
                                        "eval_offset": getattr(
                                            window, "eval_offset", None
                                        ),
                                        "delta_chainlink": delta_chainlink,
                                        "delta_tiingo": delta_tiingo,
                                        "delta_binance": delta_binance,
                                        "vpin": current_vpin,
                                        "regime": _snap_regime,
                                        "decision": "SKIP",
                                        "gate_passed": False,
                                        "gate_failed": "source_disagree",
                                    }
                                )
                            except Exception:
                                pass
                        # Telegram notification for source disagreement (once per window)
                        _disagree_key = f"{window.asset}-{window.window_ts}-disagree"
                        if not hasattr(self, "_v9_disagree_notified"):
                            self._v9_disagree_notified = set()
                        if (
                            self._alerter
                            and _disagree_key not in self._v9_disagree_notified
                        ):
                            self._v9_disagree_notified.add(_disagree_key)
                            # Clean old keys (>10min)
                            _now = time.time()
                            self._v9_disagree_notified = {
                                k
                                for k in self._v9_disagree_notified
                                if _now - int(k.rsplit("-", 1)[0].rsplit("-", 1)[-1])
                                < 600
                            }
                            _offset = getattr(window, "eval_offset", "?")

                            async def _send_disagree_alert():
                                try:
                                    await self._alerter.send_message(
                                        f"🔀 SOURCE DISAGREE — {window.asset} 5m\n"
                                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                        f"Chainlink: {_cl_dir} (Δ {delta_chainlink:+.4f}%)\n"
                                        f"Tiingo: {_ti_dir} (Δ {delta_tiingo:+.4f}%)\n"
                                        f"VPIN: {current_vpin:.3f} | {_snap_regime}\n"
                                        f"Offset: T-{_offset}\n"
                                        f"Action: ⏭ SKIP (9.1% WR when disagree)\n"
                                        f"\n📍 MTL  {window.asset}-{window.window_ts}  v9.0"
                                    )
                                except Exception:
                                    pass

                            asyncio.create_task(_send_disagree_alert())
                        signal = None
                    tf = "15m" if window.duration_secs == 900 else "5m"

        # ── v9.0 Dynamic Caps + Signal Evaluation ─────────────────────────
        # Skip entirely when v10 is active — v10 already decided TRADE or SKIP above.
        # Only run v9 caps + _evaluate_signal when v10 is disabled (legacy v9 path).
        _v9_caps = os.environ.get("V9_CAPS_ENABLED", "false").lower() == "true"
        _v9_cap = None
        _v9_tier = None
        _eval_offset = getattr(window, "eval_offset", None)

        if not _v10_enabled:
            if _v9_caps and _eval_offset is not None:
                _v9_cap_early = float(os.environ.get("V9_CAP_EARLY", "0.55"))
                _v9_cap_golden = float(os.environ.get("V9_CAP_GOLDEN", "0.65"))
                _v9_vpin_early = float(os.environ.get("V9_VPIN_EARLY", "0.65"))
                _v9_vpin_late = float(os.environ.get("V9_VPIN_LATE", "0.45"))

                if _eval_offset > 130:
                    # Early zone: CASCADE only (VPIN >= 0.65)
                    if current_vpin >= _v9_vpin_early:
                        _v9_cap = _v9_cap_early
                        _v9_tier = "EARLY_CASCADE"
                    else:
                        _v9_tier = "EARLY_SKIP"
                        if _v9_agreement:
                            # VPIN too low for early zone — skip regardless of agreement
                            self._last_skip_reason = f"v9: early offset T-{_eval_offset} VPIN {current_vpin:.2f} < {_v9_vpin_early}"
                            signal = None
                else:
                    # Golden zone (T-130..T-60): VPIN >= 0.45
                    if current_vpin >= _v9_vpin_late:
                        _v9_cap = _v9_cap_golden
                        _v9_tier = "GOLDEN"
                    else:
                        _v9_tier = "GOLDEN_SKIP"
                        if _v9_agreement:
                            self._last_skip_reason = f"v9: golden zone VPIN {current_vpin:.2f} < {_v9_vpin_late}"
                            signal = None

                self._log.info(
                    "v9.cap_tier",
                    tier=_v9_tier,
                    cap=f"${_v9_cap:.2f}" if _v9_cap else "SKIP",
                    offset=_eval_offset,
                    vpin=f"{current_vpin:.3f}",
                )

            # Evaluate signal (with TWAP and TimesFM — dead gates will be cleaned up)
            # Skip evaluation if v9.0 already decided to skip
            if not (
                (_v9_agreement and _v9_source_agree is False)
                or (_v9_caps and _v9_tier and "SKIP" in _v9_tier)
            ):
                signal = self._evaluate_signal(
                    window,
                    current_price,
                    current_vpin,
                    delta_pct,
                    twap_result=twap_result,
                    timesfm_forecast=timesfm_forecast,
                )
                # Override direction with v9.0 source agreement direction
                if signal and _v9_direction_override and _v9_agreement:
                    signal.direction = _v9_direction_override
                # Override cap with v9.0 tier cap and entry reason
                if signal and _v9_cap is not None:
                    signal.v81_entry_cap = _v9_cap
                    _order_type = os.environ.get("ORDER_TYPE", "FAK").upper()
                    signal.entry_reason = f"v9_{_v9_tier}_T{_eval_offset}_{_order_type}"
            else:
                signal = None

        tf = "15m" if window.duration_secs == 900 else "5m"

        # _snap_regime already computed above (before v9 gate block)

        # ── Capture CoinGlass snapshot at evaluation time ────────────────────
        # Per-asset CG feed (v5.4d) — fall back to BTC if asset feed unavailable

        cg = None

        if hasattr(self, "_cg_feeds") and self._cg_feeds:
            _asset_feed = self._cg_feeds.get(window.asset, self._cg_feeds.get("BTC"))

            if _asset_feed and _asset_feed.connected:
                cg = _asset_feed.snapshot

        elif self._cg_enhanced and self._cg_enhanced.connected:
            cg = self._cg_enhanced.snapshot

        # ── Build window snapshot dict ───────────────────────────────────────
        # TimesFM data for DB
        _tfm_direction = None
        _tfm_confidence = None
        _tfm_predicted_close = None
        _tfm_agreement = None
        if timesfm_forecast and not getattr(timesfm_forecast, "error", ""):
            _tfm_direction = getattr(timesfm_forecast, "direction", None)
            _tfm_confidence = getattr(timesfm_forecast, "confidence", None)
            _tfm_predicted_close = getattr(timesfm_forecast, "predicted_close", None)
            # Check agreement with v5.7c direction
            _implied_dir = "UP" if delta_pct > 0 else "DOWN"
            _v57c_dir = signal.direction if signal else _implied_dir
            _tfm_agreement = (_tfm_direction == _v57c_dir) if _tfm_direction else None

        window_snapshot = {
            "window_ts": window.window_ts,
            "asset": window.asset,
            "timeframe": tf,
            "open_price": open_price,
            "close_price": current_price,
            "delta_pct": delta_pct,  # Primary delta (chainlink if available, else binance)
            # Multi-source deltas (v7.2)
            "delta_chainlink": delta_chainlink,
            "delta_tiingo": delta_tiingo,
            "delta_binance": delta_binance,
            "price_consensus": price_consensus,
            "vpin": current_vpin,
            "regime": _snap_regime,
            "btc_price": current_price,
            # CoinGlass
            "cg_connected": cg.connected if cg else False,
            "cg_oi_usd": cg.oi_usd if cg else None,
            "cg_oi_delta_pct": cg.oi_delta_pct_1m if cg else None,
            "cg_liq_long_usd": cg.liq_long_usd_1m if cg else None,
            "cg_liq_short_usd": cg.liq_short_usd_1m if cg else None,
            "cg_liq_total_usd": cg.liq_total_usd_1m if cg else None,
            "cg_long_pct": cg.long_pct if cg else None,
            "cg_short_pct": cg.short_pct if cg else None,
            "cg_long_short_ratio": cg.long_short_ratio if cg else None,
            "cg_top_long_pct": cg.top_position_long_pct if cg else None,
            "cg_top_short_pct": cg.top_position_short_pct if cg else None,
            "cg_top_ratio": cg.top_position_ratio if cg else None,
            "cg_taker_buy_usd": cg.taker_buy_volume_1m if cg else None,
            "cg_taker_sell_usd": cg.taker_sell_volume_1m if cg else None,
            "cg_funding_rate": cg.funding_rate if cg else None,
            # Signal
            "direction": signal.direction
            if signal
            else ("UP" if delta_pct > 0 else "DOWN"),
            "confidence": signal.confidence if signal else None,
            "cg_modifier": signal.cg_modifier if signal else 0.0,
            "trade_placed": False,  # Updated to True downstream if order succeeds
            # TimesFM (v5.8)
            "timesfm_direction": _tfm_direction,
            "timesfm_confidence": _tfm_confidence,
            "timesfm_predicted_close": _tfm_predicted_close,
            "timesfm_agreement": _tfm_agreement,
            # TWAP data (v5.7)
            "twap_delta_pct": twap_result.twap_delta_pct if twap_result else None,
            "twap_direction": twap_result.twap_direction if twap_result else None,
            "twap_gamma_agree": twap_result.twap_gamma_agree if twap_result else None,
            "twap_agreement_score": twap_result.agreement_score
            if twap_result
            else None,
            "twap_confidence_boost": twap_result.confidence_boost
            if twap_result
            else None,
            "twap_n_ticks": twap_result.n_ticks if twap_result else None,
            "twap_stability": twap_result.twap_stability if twap_result else None,
            "twap_trend_pct": twap_result.trend_pct if twap_result else None,
            "twap_momentum_pct": twap_result.momentum_pct if twap_result else None,
            "twap_gamma_gate": twap_result.gamma_gate if twap_result else None,
            "twap_should_skip": twap_result.should_skip if twap_result else None,
            "twap_skip_reason": twap_result.skip_reason if twap_result else None,
            # v8.0 Phase 3: gate status flags (track when gates are off)
            "twap_override_active": runtime.twap_override_enabled,
            "twap_gamma_gate_active": runtime.twap_gamma_gate_enabled,
            "timesfm_gate_active": runtime.timesfm_agreement_enabled,
            # Gamma market prices (Polymarket token prices)
            "gamma_up_price": float(window.up_price)
            if window.up_price is not None
            else None,
            "gamma_down_price": float(window.down_price)
            if window.down_price is not None
            else None,
            "gamma_mid_price": (window.up_price + window.down_price) / 2
            if (window.up_price and window.down_price)
            else None,
            "gamma_spread": abs(window.up_price - window.down_price)
            if (window.up_price and window.down_price)
            else None,
            # Shadow trade fields (v5.8.1) — recorded for EVERY window with a signal direction.
            # Even gated/skipped windows get direction + entry price so what-if P&L
            # can be computed in the API without re-running the engine.
            # shadow_trade_direction: implied direction from delta (always set)
            # shadow_trade_entry_price: Gamma price for the implied direction at evaluation time
            "shadow_trade_direction": signal.direction
            if signal
            else ("UP" if delta_pct > 0 else "DOWN"),
            "shadow_trade_entry_price": (
                window.up_price
                if (signal.direction if signal else ("UP" if delta_pct > 0 else "DOWN"))
                == "UP"
                else window.down_price
            )
            if (window.up_price and window.down_price)
            else None,
            # skip_reason is set AFTER evaluation via _last_skip_reason (accurate)
            # This field is updated in the post-eval block below
            "skip_reason": None,
            "engine_version": "v8.0",
            # v8.0 delta source tracking
            "delta_source": _price_source_used,
            # Multi-source prices at evaluation time
            "chainlink_open": None,  # Populated async below
            "tiingo_open": None,  # Populated async below
            "tiingo_close": _tiingo_close,  # v8.0: REST candle close price
            # v7.1 retroactive: would this window pass v7.1 VPIN+delta gate?
            "v71_would_trade": (
                current_vpin >= 0.45
                and abs(delta_pct)
                >= (
                    0.01
                    if current_vpin >= _runtime.vpin_cascade_direction_threshold
                    else 0.02
                )
            ),
            "v71_skip_reason": (
                None
                if (
                    current_vpin >= 0.45
                    and abs(delta_pct)
                    >= (
                        0.01
                        if current_vpin >= _runtime.vpin_cascade_direction_threshold
                        else 0.02
                    )
                )
                else (
                    f"VPIN {current_vpin:.3f} < gate 0.45"
                    if current_vpin < 0.45
                    else f"delta {abs(delta_pct):.4f}% < {'0.01' if current_vpin >= _runtime.vpin_cascade_direction_threshold else '0.02'}%"
                )
            ),
            "v71_regime": (
                "CASCADE"
                if current_vpin >= 0.65
                else "TRANSITION"
                if current_vpin >= 0.55
                else "NORMAL"
            ),
        }

        # ── v8.1: Query v2.2 for early entry gate data (AFTER window_snapshot created) ───
        # This fetch populates window_snapshot with v2.2 data that will be written to DB.
        # Used for: (1) early entry gate at T>=120, (2) dashboard display, (3) analysis.
        #
        # Push-mode (Sequoia v5): we send every feature we have at this
        # point so the scorer can use its v5 booster directly instead
        # of pulling its own (v4-shaped, train-skewed) feature cache.
        # Gate results (_vpin_passed etc.) are NOT computed yet at this
        # point in the function — they're filled in further down — so
        # those stay None here. The decision-path fetch below (line
        # ~1478) sends the full 25-feature body once gates are resolved.
        _eval_offset = getattr(window, "eval_offset", None)
        if self._timesfm_v2 is not None and _eval_offset:
            try:
                from signals.v2_feature_body import build_v5_feature_body

                # Pre-eval diagnostic fetch: ~60% feature coverage by
                # design. The gate booleans haven't been computed yet at
                # this point in the function (that happens ~20 lines
                # below), and there is no prior v2 probability yet, so
                # those fields stay None. The decision-path fetch at
                # line ~1478 runs AFTER gates and uses the SAME builder
                # helper with the full 25 features. Both call sites go
                # through `build_v5_feature_body` so the extraction
                # logic is never duplicated or drifted.
                _pre_features = build_v5_feature_body(
                    eval_offset=float(_eval_offset),
                    vpin=current_vpin,
                    delta_pct=delta_pct,
                    twap_delta=(twap_result.twap_delta_pct if twap_result else None),
                    clob_up_price=window.up_price,
                    clob_down_price=window.down_price,
                    binance_price=current_price,
                    tiingo_close=_tiingo_close,
                    delta_binance=delta_binance,
                    delta_chainlink=delta_chainlink,
                    delta_tiingo=delta_tiingo,
                    regime=_snap_regime,
                    delta_source=_price_source_used,
                    # gate_*: not yet resolved at pre-eval time
                    # prev_v2_probability_up: no prior value at first-tick
                )
                _v2_pre = await self._timesfm_v2.score_with_features(
                    asset=window.asset,
                    seconds_to_close=_eval_offset,
                    features=_pre_features,
                )
                if _v2_pre and "probability_up" in _v2_pre:
                    window_snapshot["v2_probability_up"] = round(
                        float(_v2_pre["probability_up"]), 4
                    )
                    window_snapshot["v2_direction"] = (
                        "UP" if float(_v2_pre["probability_up"]) > 0.5 else "DOWN"
                    )
                    window_snapshot["v2_model_version"] = _v2_pre.get(
                        "model_version", ""
                    )
                    window_snapshot["eval_offset"] = _eval_offset
                    # v2.2: Store full timesfm quantile surface if available
                    _timesfm = _v2_pre.get("timesfm", {})
                    if _timesfm and _timesfm.get("quantiles"):
                        import json

                        window_snapshot["v2_quantiles"] = json.dumps(
                            _timesfm["quantiles"]
                        )
                    if _timesfm and _timesfm.get("quantiles_at_close"):
                        import json

                        window_snapshot["v2_quantiles_at_close"] = json.dumps(
                            _timesfm["quantiles_at_close"]
                        )
            except Exception as e:
                self._log.warning("v2.probability.fetch_failed", error_str=str(e)[:100])

        # ── v8.0: Compute gate results + confidence tier for notifications ────
        _vpin_passed = current_vpin >= _runtime.five_min_vpin_gate
        _delta_thresh = (
            _runtime.five_min_cascade_min_delta_pct
            if current_vpin >= _runtime.vpin_cascade_direction_threshold
            else _runtime.five_min_min_delta_pct
        )
        _delta_passed = (
            abs(delta_pct) >= _delta_thresh if delta_pct is not None else False
        )
        _actual_skip = getattr(self, "_last_skip_reason", "") or ""
        _cg_passed = not (
            "CG_VETO" in _actual_skip.upper() or "coinglass" in _actual_skip.lower()
        )
        _floor_passed = True
        _cap_passed = True

        # Check floor/cap from Gamma prices
        _entry_price = None
        _implied = (
            signal.direction
            if signal
            else ("UP" if delta_pct and delta_pct > 0 else "DOWN")
        )
        if window_snapshot.get("gamma_up_price") and window_snapshot.get(
            "gamma_down_price"
        ):
            _entry_price = (
                window_snapshot["gamma_up_price"]
                if _implied == "UP"
                else window_snapshot["gamma_down_price"]
            )
            if _entry_price < 0.30:
                _floor_passed = False
            elif _entry_price > 0.73:
                _cap_passed = False

        # Build gates_passed string and identify failed gate
        _gp_list = []
        _gf = None
        for _gname, _gpassed in [
            ("vpin", _vpin_passed),
            ("delta", _delta_passed),
            ("cg", _cg_passed),
            ("floor", _floor_passed),
            ("cap", _cap_passed),
        ]:
            if _gpassed:
                _gp_list.append(_gname)
            elif _gf is None:
                _gf = _gname

        # Confidence tier: based on VPIN strength + delta magnitude + source agreement
        _sources_agree = 0
        for _d in [delta_tiingo, delta_binance, delta_chainlink]:
            if _d is not None:
                if (_d > 0 and _implied == "UP") or (_d < 0 and _implied == "DOWN"):
                    _sources_agree += 1
        if _sources_agree >= 3 and current_vpin >= 0.65 and abs(delta_pct or 0) >= 0.05:
            _conf_tier = "DECISIVE"
        elif _sources_agree >= 2 and current_vpin >= 0.55:
            _conf_tier = "HIGH"
        elif _vpin_passed and _delta_passed:
            _conf_tier = "MODERATE"
        elif _vpin_passed:
            _conf_tier = "LOW"
        else:
            _conf_tier = "NONE"

        window_snapshot["gates_passed"] = ",".join(_gp_list)
        window_snapshot["gate_failed"] = _gf
        window_snapshot["confidence_tier"] = _conf_tier
        window_snapshot["_cap_passed"] = _cap_passed
        window_snapshot["_floor_passed"] = _floor_passed

        # ── Fetch fresh Gamma prices for ALL windows (not just traded ones) ────
        try:
            _slug = f"btc-updown-5m-{window.window_ts}"
            _fresh_up, _fresh_down, _src = await self._fetch_fresh_gamma_price(_slug)
            if _fresh_up is not None and _fresh_down is not None:
                window_snapshot["gamma_up_price"] = _fresh_up
                window_snapshot["gamma_down_price"] = _fresh_down
                self._log.debug(
                    "snapshot.gamma_fetched",
                    up=f"${_fresh_up:.3f}",
                    down=f"${_fresh_down:.3f}",
                )
        except Exception:
            pass

        # ── Populate chainlink_open / tiingo_open / CLOB prices ────────────────
        # _chainlink_price fetched earlier for delta calc.
        # _tiingo_open / _tiingo_close fetched from REST candle above.
        if _chainlink_price:
            window_snapshot["chainlink_open"] = _chainlink_price
        if _tiingo_open:
            window_snapshot["tiingo_open"] = _tiingo_open
        if _tiingo_close:
            window_snapshot["tiingo_close"] = _tiingo_close
        # CLOB real bid/ask from Polymarket order book
        if self._db:
            try:
                _clob = await self._db.get_latest_clob_prices(window.asset)
                if _clob:
                    window_snapshot["clob_up_bid"] = _clob.get("clob_up_bid")
                    window_snapshot["clob_up_ask"] = _clob.get("clob_up_ask")
                    window_snapshot["clob_down_bid"] = _clob.get("clob_down_bid")
                    window_snapshot["clob_down_ask"] = _clob.get("clob_down_ask")
            except Exception:
                pass

        # ── v8.0: Inject macro observer signal (display only, not gating) ────
        if self._db:
            try:
                _macro = await self._db.get_latest_macro_signal()
                if _macro:
                    window_snapshot["macro_bias"] = _macro["macro_bias"]
                    window_snapshot["macro_confidence"] = _macro["macro_confidence"]
                    window_snapshot["macro_gate"] = _macro.get("macro_gate", "")
                    window_snapshot["macro_reasoning"] = _macro.get(
                        "macro_reasoning", ""
                    )
            except Exception:
                pass

        # ── DB write (AWAIT so row exists before trade_placed update) ─────────
        if self._db is not None:
            try:
                await self._db.write_window_snapshot(window_snapshot)
                # v8.1: OAK (v2.2) fields are now included in the INSERT above
                # No separate UPDATE needed
            except Exception as exc:
                self._log.warning("db.snapshot_write_failed", error=str(exc)[:80])

        # ── Window prediction capture (v8.1.2) ───────────────────────────────
        # Record Tiingo + Chainlink close prices and predicted directions.
        # This runs on every window (trade or skip) for accuracy tracking.
        if self._db is not None:
            try:
                _sig_dir = (
                    signal.direction
                    if signal
                    else ("UP" if delta_pct and delta_pct > 0 else "DOWN")
                )
                _ti_dir = (
                    "UP"
                    if delta_tiingo and delta_tiingo > 0
                    else "DOWN"
                    if delta_tiingo
                    else None
                )
                _cl_dir = (
                    "UP"
                    if delta_chainlink and delta_chainlink > 0
                    else "DOWN"
                    if delta_chainlink
                    else None
                )
                _v2_dir_pred = window_snapshot.get("v2_direction")
                _v2_prob_pred = window_snapshot.get("v2_probability_up")
                _regime = _snap_regime
                _vpin_close = current_vpin

                asyncio.create_task(
                    self._db.write_window_prediction(
                        {
                            "window_ts": window.window_ts,
                            "asset": window.asset,
                            "timeframe": tf,
                            "tiingo_open": _tiingo_open,
                            "tiingo_close": _tiingo_close,
                            "chainlink_open": window_snapshot.get("chainlink_open"),
                            "chainlink_close": current_price,  # BTC price at eval time ≈ Chainlink
                            "tiingo_direction": _ti_dir,
                            "chainlink_direction": _cl_dir,
                            "our_signal_direction": _sig_dir,
                            "v2_direction": _v2_dir_pred,
                            "v2_probability": float(_v2_prob_pred)
                            if _v2_prob_pred
                            else None,
                            "vpin_at_close": _vpin_close,
                            "regime": _regime,
                            "trade_placed": signal is not None,
                            "our_direction": signal.direction if signal else None,
                            "our_entry_price": getattr(signal, "v81_entry_cap", None)
                            if signal
                            else None,
                            "bid_unfilled": False,  # Updated downstream if order expires unfilled
                            "skip_reason": self._last_skip_reason
                            if signal is None
                            else None,
                        }
                    )
                )
            except Exception:
                pass

        # ── Gate audit write (v8.0) — record pass/fail for every window ───────
        # Builds audit AFTER signal eval so gate_passed reflects actual decision.
        # signal is None → SKIP; signal is not None → TRADE.
        if self._db is not None:
            try:
                # Determine individual gate results for audit
                _vpin_gate_result = current_vpin >= _runtime.five_min_vpin_gate
                _delta_threshold = (
                    _runtime.five_min_cascade_min_delta_pct
                    if current_vpin >= _runtime.vpin_cascade_direction_threshold
                    else _runtime.five_min_min_delta_pct
                )
                _delta_gate_result = (
                    abs(delta_pct) >= _delta_threshold
                    if delta_pct is not None
                    else False
                )

                _gates_passed = []
                _gate_failed_name = None
                if _vpin_gate_result:
                    _gates_passed.append("vpin")
                else:
                    if not _gate_failed_name:
                        _gate_failed_name = "vpin"
                if _delta_gate_result:
                    _gates_passed.append("delta")
                else:
                    if not _gate_failed_name:
                        _gate_failed_name = "delta"

                # CG gate — inferred from skip reason if CG veto was triggered
                _cg_gate_passed = True
                _actual_skip_reason = getattr(self, "_last_skip_reason", "") or ""
                if (
                    "CG_VETO" in _actual_skip_reason.upper()
                    or "coinglass" in _actual_skip_reason.lower()
                ):
                    _cg_gate_passed = False
                    if not _gate_failed_name:
                        _gate_failed_name = "cg"
                if _cg_gate_passed:
                    _gates_passed.append("cg")

                # v8.0 Phase 3: TWAP gate audit — record even when disabled
                # twap_gate_would_block: what TWAP gate WOULD have done (regardless of flag)
                _twap_gate_would_block_audit = (
                    (
                        twap_result is not None
                        and twap_result.should_skip
                        and twap_result.n_ticks >= 5
                    )
                    if twap_result
                    else False
                )
                _twap_gate_blocked_actual = (
                    _twap_gate_would_block_audit and runtime.twap_gamma_gate_enabled
                )
                if not _twap_gate_blocked_actual and _twap_gate_would_block_audit:
                    # Would have blocked but gate is disabled — log shadow block
                    pass  # shadow block — recorded via gate_check_traces
                if not _twap_gate_blocked_actual:
                    _gates_passed.append("twap_gamma")
                else:
                    if not _gate_failed_name:
                        _gate_failed_name = "twap_gamma"

                # TimesFM gate audit — record even when disabled
                _timesfm_gate_blocked_actual = (
                    "timesfm" in _actual_skip_reason.lower()
                    and runtime.timesfm_agreement_enabled
                )
                _timesfm_would_block = "timesfm" in _actual_skip_reason.lower()
                if not _timesfm_gate_blocked_actual:
                    _gates_passed.append("timesfm")
                else:
                    if not _gate_failed_name:
                        _gate_failed_name = "timesfm"

                _all_passed = signal is not None
                # gate_audit writes retired — gate_check_traces is the successor
            except Exception as _gate_exc:
                self._log.warning(
                    "db.gate_signal_write_failed", error=str(_gate_exc)[:100]
                )

        # ── Comprehensive signal evaluation capture ──
        if self._db is not None:
            try:
                _clob_up_bid = window_snapshot.get("clob_up_bid")
                _clob_up_ask = window_snapshot.get("clob_up_ask")
                _clob_dn_bid = window_snapshot.get("clob_down_bid")
                _clob_dn_ask = window_snapshot.get("clob_down_ask")
                _clob_spread = (
                    (_clob_up_ask - _clob_up_bid)
                    if _clob_up_ask and _clob_up_bid
                    else None
                )
                _clob_mid = (
                    ((_clob_up_bid + _clob_up_ask) / 2)
                    if _clob_up_bid and _clob_up_ask
                    else None
                )

                asyncio.create_task(
                    self._db.write_signal_evaluation(
                        {
                            "window_ts": window.window_ts,
                            "asset": window.asset,
                            "timeframe": tf,
                            "eval_offset": eval_offset,
                            # Prices
                            "clob_up_bid": _clob_up_bid,
                            "clob_up_ask": _clob_up_ask,
                            "clob_down_bid": _clob_dn_bid,
                            "clob_down_ask": _clob_dn_ask,
                            "binance_price": window_snapshot.get("binance_price"),
                            "tiingo_open": _tiingo_open,
                            "tiingo_close": _tiingo_close,
                            "chainlink_price": window_snapshot.get("chainlink_open"),
                            # Deltas
                            "delta_pct": delta_pct,
                            "delta_tiingo": delta_tiingo,
                            "delta_binance": delta_binance,
                            "delta_chainlink": delta_chainlink,
                            "delta_source": _price_source_used,
                            # Market microstructure
                            "vpin": current_vpin,
                            "regime": _snap_regime,
                            "clob_spread": _clob_spread,
                            "clob_mid": _clob_mid,
                            # OAK/v2.2 full predictions
                            "v2_probability_up": window_snapshot.get(
                                "v2_probability_up"
                            ),
                            "v2_direction": window_snapshot.get("v2_direction"),
                            "v2_agrees": window_snapshot.get("v2_agrees"),
                            "v2_high_conf": window_snapshot.get("v2_direction")
                            is not None
                            and (
                                window_snapshot.get("v2_probability_up", 0) > 0.65
                                or window_snapshot.get("v2_probability_up", 1) < 0.35
                            ),
                            "v2_model_version": window_snapshot.get("v2_model_version"),
                            "v2_quantiles": window_snapshot.get("v2_quantiles"),
                            "v2_quantiles_at_close": window_snapshot.get(
                                "v2_quantiles_at_close"
                            ),
                            # Gates
                            "gate_vpin_passed": bool(_vpin_gate_result),
                            "gate_delta_passed": bool(_delta_gate_result),
                            "gate_cg_passed": _cg_gate_passed,
                            "gate_twap_passed": not _twap_gate_blocked_actual,
                            "gate_timesfm_passed": not _timesfm_gate_blocked_actual,
                            "gate_passed": _all_passed,
                            "gate_failed": _gate_failed_name,
                            "decision": "TRADE" if _all_passed else "SKIP",
                            # TWAP
                            "twap_delta": window_snapshot.get("twap_delta_pct"),
                            "twap_direction": window_snapshot.get("twap_direction"),
                            "twap_gamma_agree": window_snapshot.get("twap_gamma_agree"),
                        }
                    )
                )
            except Exception as _sig_exc:
                self._log.warning(
                    "db.signal_evaluation_write_failed", error=str(_sig_exc)[:100]
                )

        # ── v8.1 Early Entry Gate ────────────────────────────────────────────
        # At offsets >= 120s, require v2.2 HIGH CONF + v8 direction agreement.
        # If v2.2 disagrees or is low confidence → skip this offset, fall through
        # to the next one (T-180 → T-120 → T-60). At T-60 no v2.2 gate is applied.
        # Dynamic entry cap per offset: T-240=$0.55, T-180=$0.60, T-120=$0.65, T-60=$0.73
        _v81_active = False
        if eval_offset and signal is not None and self._timesfm_v2 is not None:
            _v81_cap = _get_v81_cap(eval_offset)
            _v81_active = True
            _v8_dir = signal.direction  # capture before any mutation
            try:
                from signals.v2_feature_body import (
                    build_v5_feature_body,
                    confidence_from_result,
                )

                # Build the full 25-feature push-mode body via the
                # single-source-of-truth helper. By this point gates
                # have been evaluated (above this block) so gate_*
                # booleans carry real state rather than None.
                #
                # The PRE-EVAL fetch at line ~1130 stored its P(UP) in
                # window_snapshot["v2_probability_up"]; we use that as
                # the v2_logit self-reference. First tick in a window
                # → None → scorer gets NaN → LightGBM missing-default.
                _decision_features = build_v5_feature_body(
                    eval_offset=float(eval_offset),
                    vpin=current_vpin,
                    delta_pct=delta_pct,
                    twap_delta=(twap_result.twap_delta_pct if twap_result else None),
                    clob_up_price=window.up_price,
                    clob_down_price=window.down_price,
                    binance_price=current_price,
                    chainlink_price=window_snapshot.get("chainlink_open"),
                    tiingo_close=_tiingo_close,
                    delta_binance=delta_binance,
                    delta_chainlink=delta_chainlink,
                    delta_tiingo=delta_tiingo,
                    gate_vpin_passed=_vpin_passed,
                    gate_delta_passed=_delta_passed,
                    gate_cg_passed=_cg_passed,
                    gate_twap_passed=not _twap_gate_blocked_actual,
                    gate_timesfm_passed=not _timesfm_gate_blocked_actual,
                    gate_passed=_all_passed,
                    regime=_snap_regime,
                    delta_source=_price_source_used,
                    prev_v2_probability_up=window_snapshot.get("v2_probability_up"),
                )
                _v2_result = await self._timesfm_v2.score_with_features(
                    asset=window.asset,
                    seconds_to_close=eval_offset,
                    features=_decision_features,
                )
                if not _v2_result or "probability_up" not in _v2_result:
                    raise RuntimeError(
                        f"v2.2 returned invalid response: {str(_v2_result)[:80]}"
                    )

                _v2_p = float(_v2_result["probability_up"])
                _v2_dir = "UP" if _v2_p > 0.5 else "DOWN"
                _v2_high = _v2_p > 0.65 or _v2_p < 0.35
                _v2_agrees = _v2_dir == _v8_dir

                # NOTE: dynamic confidence gating used to live here but
                # was removed in 1744fde — it had an asymmetric threshold
                # that silently blocked strong DOWN signals (161 in 48
                # minutes). Dynamic confidence gating now lives solely in
                # DuneConfidenceGate (engine/signals/gates.py), where it
                # composes cleanly with regime/offset/down/CG modifiers
                # and handles DOWN direction via dune_p = 1 - p_up. The
                # v11 confidence-extraction bug (reading timesfm.confidence
                # as P(UP) confidence) is fixed on the gates.py side of
                # this PR using confidence_from_result().
                # Store in snapshot for analysis
                window_snapshot["v2_probability_up"] = round(_v2_p, 4)
                window_snapshot["v2_direction"] = _v2_dir
                window_snapshot["v2_agrees"] = _v2_agrees
                window_snapshot["v2_model_version"] = _v2_result.get(
                    "model_version", ""
                )
                window_snapshot["eval_offset"] = eval_offset
                # v2.2: Store full quantile surface
                _timesfm = _v2_result.get("timesfm", {})
                if _timesfm and _timesfm.get("quantiles"):
                    import json

                    window_snapshot["v2_quantiles"] = json.dumps(_timesfm["quantiles"])
                if _timesfm and _timesfm.get("quantiles_at_close"):
                    import json

                    window_snapshot["v2_quantiles_at_close"] = json.dumps(
                        _timesfm["quantiles_at_close"]
                    )

                # Write snapshot immediately for this eval_offset
                if self._db is not None:
                    try:
                        await self._db.write_window_snapshot(window_snapshot)
                    except Exception as _snap_exc:
                        self._log.warning(
                            "db.v2_snapshot_write_failed",
                            error=str(_snap_exc)[:80],
                            offset=eval_offset,
                        )

                self._log.info(
                    "v81.early_gate",
                    offset=eval_offset,
                    v2_p=f"{_v2_p:.3f}",
                    v2_dir=_v2_dir,
                    v8_dir=_v8_dir,
                    agrees=_v2_agrees,
                    high_conf=_v2_high,
                    cap=_v81_cap,
                )

                # v9.0: When v9 caps are enabled and passed, bypass old v8.1 gates
                # v9.0 agreement + VPIN tier already handled direction + cap
                _v9_bypass = (
                    os.environ.get("V9_CAPS_ENABLED", "false").lower() == "true"
                    and "_v9_cap" in locals()
                    and _v9_cap is not None
                )
                if _v9_bypass:
                    self._log.info(
                        "v9.bypass_v8_gates",
                        offset=eval_offset,
                        v9_tier=_v9_tier,
                        v9_cap=f"${_v9_cap:.2f}",
                    )
                    signal.v81_entry_cap = _v9_cap
                    _order_type = os.environ.get("ORDER_TYPE", "FAK").upper()
                    signal.entry_reason = f"v9_{_v9_tier}_T{eval_offset}_{_order_type}"
                # Gate 1: v2.2 must be HIGH confidence (all offsets)
                # NOTE: dynamic confidence gating now lives in DuneConfidenceGate
                # (engine/signals/gates.py) from ee1b10f — see v11.0 changelog.
                elif not _v2_high:
                    signal = None
                    self._last_skip_reason = (
                        f"v2.2 LOW conf ({_v2_p:.2f}) at T-{eval_offset}"
                    )
                # Gate 2: v2.2 must agree with v8 direction (all offsets)
                elif not _v2_agrees:
                    signal = None
                    self._last_skip_reason = f"v2.2 DISAGREES (v2={_v2_dir} vs v8={_v8_dir}) at T-{eval_offset}"
                # Gate 3: Early offsets (≥120) also need CASCADE + strong delta for DECISIVE
                elif eval_offset >= 120:
                    _is_cascade = current_vpin >= 0.65
                    _is_strong_delta = abs(delta_pct) >= 0.05 if delta_pct else False
                    if not _is_cascade:
                        signal = None
                        self._last_skip_reason = f"v8.1: not CASCADE (VPIN {current_vpin:.3f} < 0.65) at T-{eval_offset}"
                    elif not _is_strong_delta:
                        signal = None
                        self._last_skip_reason = f"v8.1: delta too weak ({abs(delta_pct):.4f}% < 0.05%) at T-{eval_offset}"
                    else:
                        # Tight DECISIVE: v2.2 HIGH + agrees + CASCADE + delta≥5bp
                        signal.confidence = "DECISIVE"
                        signal.entry_reason = f"v2.2_early_T{eval_offset}"
                        signal.v81_entry_cap = _v81_cap
                else:
                    # Late offsets (<120): v2.2 HIGH + agrees is enough
                    # v8.1.2: ALL late offsets (<120) require TRANSITION+ (VPIN≥0.55)
                    # Apr 7 data: 4 NORMAL losses (T-70×3 + T-100×1), 1 NORMAL win
                    # 80% block accuracy on NORMAL regime at late offsets
                    if current_vpin < 0.55:
                        signal = None
                        self._last_skip_reason = f"v8.1.2: NORMAL at T-{eval_offset} (VPIN {current_vpin:.3f} < 0.55)"
                    else:
                        signal.entry_reason = f"v2.2_confirmed_T{eval_offset}"
                        signal.v81_entry_cap = _v81_cap
                    self._log.info(
                        "v81.early_entry_approved",
                        offset=eval_offset,
                        direction=_v8_dir,
                        cap=_v81_cap,
                        v2_p=f"{_v2_p:.3f}",
                    )
            except Exception as _v2_exc:
                # v2.2 service down — skip early entry, fall through to next offset
                signal = None
                self._last_skip_reason = (
                    f"v8.1: v2.2 unavailable at T-{eval_offset}: {str(_v2_exc)[:50]}"
                )
                self._log.warning(
                    "v81.v2_service_error",
                    offset=eval_offset,
                    error_str=str(_v2_exc)[:80],
                )

        if signal is None:
            # v7.1: Use the actual skip reason set at the point of rejection
            _skip_reason = getattr(self, "_last_skip_reason", "") or ""
            self._last_skip_reason = ""  # Reset after use
            if not _skip_reason:
                _skip_reason = f"Gates passed but signal None (VPIN {current_vpin:.3f}, delta {delta_pct:+.4f}%)"
            # Update snapshot with actual reason
            window_snapshot["skip_reason"] = _skip_reason
            if self._db:
                try:
                    asyncio.create_task(
                        self._db.update_window_skip_reason(
                            window.window_ts, window.asset, "5m", _skip_reason
                        )
                    )
                except Exception:
                    pass
            self._log.info(
                "evaluate.skip",
                asset=window.asset,
                window_ts=window.window_ts,
                delta_pct=f"{delta_pct:.4f}%",
                reason=_skip_reason[:80],
                entry=f"T-{FIVE_MIN_ENTRY_OFFSET}s",
            )
            # ── Consolidated skip history (replaces individual Telegram alerts) ──
            _window_key = f"{window.asset}-{window.window_ts}"
            if _window_key not in self._window_eval_history:
                self._window_eval_history[_window_key] = []
            _clob_ask_val = (
                window_snapshot.get("clob_up_ask")
                if delta_pct and delta_pct > 0
                else window_snapshot.get("clob_down_ask")
            )
            self._window_eval_history[_window_key].append(
                {
                    "offset": eval_offset,
                    "skip_reason": _skip_reason,
                    "vpin": current_vpin,
                    "delta_pct": delta_pct,
                    "v2_p": window_snapshot.get("v2_probability_up"),
                    "v2_dir": window_snapshot.get("v2_direction"),
                    "v2_agrees": window_snapshot.get("v2_agrees"),
                    "clob_ask": _clob_ask_val,
                    "confidence": window_snapshot.get("confidence_tier"),
                    "regime": _snap_regime,
                    # v9.0 fields
                    "cl_dir": _cl_dir if "_cl_dir" in locals() else None,
                    "ti_dir": _ti_dir if "_ti_dir" in locals() else None,
                    "delta_chainlink": delta_chainlink
                    if "delta_chainlink" in locals()
                    else None,
                    "delta_tiingo": delta_tiingo
                    if "delta_tiingo" in locals()
                    else None,
                    "v9_tier": _v9_tier if "_v9_tier" in locals() else None,
                    "v9_cap": _v9_cap if "_v9_cap" in locals() else None,
                }
            )
            # At the final offset (min of configured offsets), send consolidated summary
            _min_offset = min(FIVE_MIN_EVAL_OFFSETS) if FIVE_MIN_EVAL_OFFSETS else 60
            _is_final_offset = eval_offset is not None and eval_offset <= _min_offset
            # Skip old-system window summary when Strategy Engine v2 is active
            # (registry sends its own summary via _send_window_summary)
            _legacy_alerts_enabled = (
                os.environ.get("LEGACY_EXECUTION_DISABLED", "").lower() != "true"
            )
            if self._alerter and _is_final_offset and _legacy_alerts_enabled:
                try:
                    _hist = list(self._window_eval_history.get(_window_key, []))
                    # v12: Consume strategy port decisions for skip summary
                    _sp_decs = self._pending_strategy_decisions
                    self._pending_strategy_decisions = None

                    async def _send_summary_all_skip(
                        wk=_window_key, h=_hist, sp=_sp_decs
                    ):
                        try:
                            await self._alerter.send_window_summary(
                                window_id=wk,
                                eval_history=h,
                                traded=False,
                                strategy_decisions=sp,
                            )
                        except Exception as _se:
                            self._log.error(
                                "alert.window_summary_failed",
                                error=str(_se),
                                window_key=wk,
                            )

                    asyncio.create_task(_send_summary_all_skip())
                except Exception:
                    pass
            # Clean up stale history entries (older than 10 minutes)
            _now_ts = time.time()
            _stale_keys = []
            for _wk in list(self._window_eval_history.keys()):
                try:
                    _wts = int(_wk.split("-", 1)[1])
                    if _now_ts - _wts > 600:
                        _stale_keys.append(_wk)
                except Exception:
                    pass
            for _wk in _stale_keys:
                self._window_eval_history.pop(_wk, None)
            return

        # ── v8.0: Cap/Floor check from Gamma/CLOB prices ─────────────────────
        # If the token price is outside our bounds, skip before execution.
        # This catches the case where signal passes VPIN+delta gates but
        # the market price is too expensive (>$0.73) or too cheap (<$0.30).
        if not window_snapshot.get("_cap_passed", True) or not window_snapshot.get(
            "_floor_passed", True
        ):
            _implied = (
                signal.direction
                if signal
                else ("UP" if delta_pct and delta_pct > 0 else "DOWN")
            )
            _ep = (
                window_snapshot.get("gamma_up_price")
                if _implied == "UP"
                else window_snapshot.get("gamma_down_price")
            )
            _reason = (
                f"CAP: entry ${_ep:.3f} > $0.73"
                if not window_snapshot.get("_cap_passed", True)
                else f"FLOOR: entry ${_ep:.3f} < $0.30"
            )
            self._log.info(
                "evaluate.price_gate_block",
                direction=_implied,
                entry=f"${_ep:.3f}" if _ep else "?",
                reason=_reason,
            )
            self._last_skip_reason = _reason
            signal = None  # Force SKIP path

        if signal is not None:
            # Also check fresh CLOB price before committing to trade
            # v8.1: When FOK is enabled, only check floor (not cap) - FOK will ladder down
            if self._db:
                try:
                    _clob = await self._db.get_latest_clob_prices(window.asset)
                    if _clob:
                        _dir = signal.direction
                        _clob_ask = (
                            _clob.get("clob_up_ask")
                            if _dir == "UP"
                            else _clob.get("clob_down_ask")
                        )
                        # v8.1: Use dynamic cap from eval offset
                        _dynamic_cap = (
                            _get_v81_cap(eval_offset) if eval_offset else 0.73
                        )
                        # FOK-enabled: only block on floor (CLOB too cheap = bad value)
                        # GTC mode: block on both cap and floor
                        if runtime.fok_enabled:
                            # FOK mode: only check floor
                            if _clob_ask and _clob_ask < 0.30:
                                self._log.info(
                                    "evaluate.clob_floor_block",
                                    direction=_dir,
                                    clob_ask=f"${_clob_ask:.4f}",
                                )
                                self._last_skip_reason = (
                                    f"CLOB FLOOR: {_dir} ask ${_clob_ask:.3f} < $0.30"
                                )
                                signal = None
                        else:
                            # GTC mode: check both cap and floor
                            if _clob_ask and _clob_ask > _dynamic_cap:
                                self._log.info(
                                    "evaluate.clob_cap_block",
                                    direction=_dir,
                                    clob_ask=f"${_clob_ask:.4f}",
                                    cap=f"${_dynamic_cap:.2f}",
                                )
                                self._last_skip_reason = f"CLOB CAP: {_dir} ask ${_clob_ask:.3f} > ${_dynamic_cap:.2f}"
                                signal = None
                            elif _clob_ask and _clob_ask < 0.30:
                                self._log.info(
                                    "evaluate.clob_floor_block",
                                    direction=_dir,
                                    clob_ask=f"${_clob_ask:.4f}",
                                )
                                self._last_skip_reason = (
                                    f"CLOB FLOOR: {_dir} ask ${_clob_ask:.3f} < $0.30"
                                )
                                signal = None
                except Exception:
                    pass  # Don't block trade if DB read fails

        # If CLOB cap/floor blocked the trade, append to history and use SKIP path
        if signal is None:
            _skip_reason = getattr(self, "_last_skip_reason", "") or "CLOB price gate"
            self._last_skip_reason = ""
            # Append to consolidated eval history (same as gate-skip path)
            _window_key = f"{window.asset}-{window.window_ts}"
            if _window_key not in self._window_eval_history:
                self._window_eval_history[_window_key] = []
            _clob_ask_val2 = (
                window_snapshot.get("clob_up_ask")
                if delta_pct and delta_pct > 0
                else window_snapshot.get("clob_down_ask")
            )
            self._window_eval_history[_window_key].append(
                {
                    "offset": eval_offset,
                    "skip_reason": _skip_reason,
                    "vpin": current_vpin,
                    "delta_pct": delta_pct,
                    "v2_p": window_snapshot.get("v2_probability_up"),
                    "v2_dir": window_snapshot.get("v2_direction"),
                    "v2_agrees": window_snapshot.get("v2_agrees"),
                    "clob_ask": _clob_ask_val2,
                    "confidence": window_snapshot.get("confidence_tier"),
                    "regime": _snap_regime,
                    # v9.0 fields
                    "cl_dir": _cl_dir if "_cl_dir" in locals() else None,
                    "ti_dir": _ti_dir if "_ti_dir" in locals() else None,
                    "delta_chainlink": delta_chainlink
                    if "delta_chainlink" in locals()
                    else None,
                    "delta_tiingo": delta_tiingo
                    if "delta_tiingo" in locals()
                    else None,
                    "v9_tier": _v9_tier if "_v9_tier" in locals() else None,
                    "v9_cap": _v9_cap if "_v9_cap" in locals() else None,
                }
            )
            # At the final offset, send the consolidated summary
            _min_offset2 = min(FIVE_MIN_EVAL_OFFSETS) if FIVE_MIN_EVAL_OFFSETS else 60
            _is_final_offset2 = eval_offset is not None and eval_offset <= _min_offset2
            if self._alerter and _is_final_offset2 and _legacy_alerts_enabled:
                try:
                    _hist2 = list(self._window_eval_history.get(_window_key, []))

                    async def _send_clob_summary(wk=_window_key, h=_hist2):
                        try:
                            await self._alerter.send_window_summary(
                                window_id=wk,
                                eval_history=h,
                                traded=False,
                            )
                        except Exception as _se:
                            self._log.error(
                                "alert.window_summary_failed",
                                error=str(_se),
                                window_key=wk,
                            )

                    asyncio.create_task(_send_clob_summary())
                except Exception:
                    pass
            # Clean up stale history entries (older than 10 minutes)
            _now_ts2 = time.time()
            _stale_keys2 = []
            for _wk in list(self._window_eval_history.keys()):
                try:
                    _wts = int(_wk.split("-", 1)[1])
                    if _now_ts2 - _wts > 600:
                        _stale_keys2.append(_wk)
                except Exception:
                    pass
            for _wk in _stale_keys2:
                self._window_eval_history.pop(_wk, None)
            return

        # ── Send consolidated window summary if we have prior skip history ──────
        _trade_window_key = f"{window.asset}-{window.window_ts}"
        _legacy_trade_alerts = (
            os.environ.get("LEGACY_EXECUTION_DISABLED", "").lower() != "true"
        )
        if self._alerter and _trade_window_key in self._window_eval_history and _legacy_trade_alerts:
            try:
                _trade_hist = list(self._window_eval_history.get(_trade_window_key, []))
                _trade_offset_val = eval_offset

                async def _send_trade_summary(
                    wk=_trade_window_key, h=_trade_hist, to=_trade_offset_val
                ):
                    try:
                        await self._alerter.send_window_summary(
                            window_id=wk,
                            eval_history=h,
                            traded=True,
                            trade_offset=to,
                        )
                    except Exception as _se:
                        self._log.error(
                            "alert.window_summary_trade_failed",
                            error=str(_se),
                            window_key=wk,
                        )

                asyncio.create_task(_send_trade_summary())
            except Exception:
                pass
            # Remove from history since we've sent the summary
            self._window_eval_history.pop(_trade_window_key, None)

        # ── Send trade decision + dual-AI analysis (non-blocking) ──────────────
        if self._alerter:

            async def _send_trade_alert():
                try:
                    window_id = f"{window.asset}-{window.window_ts}"
                    # v10.3: extract gate result data for Telegram alerts
                    _v103_data = getattr(signal, "_v10_gate_data", {}) or {}
                    if not _v103_data:
                        # Fallback: try window_snapshot
                        _v103_data = {
                            k: v
                            for k, v in window_snapshot.items()
                            if k.startswith("v103_")
                        }

                    signal_dict = {
                        "direction": signal.direction,
                        "delta_pct": delta_pct,
                        "vpin": current_vpin,
                        "regime": _snap_regime,
                        # v8.0 fields
                        "delta_source": window_snapshot.get("delta_source", "?"),
                        "delta_tiingo": window_snapshot.get("delta_tiingo"),
                        "delta_binance": window_snapshot.get("delta_binance"),
                        "delta_chainlink": window_snapshot.get("delta_chainlink"),
                        "tiingo_close": window_snapshot.get("tiingo_close"),
                        "chainlink_price": window_snapshot.get("chainlink_open"),
                        "binance_price": window_snapshot.get("btc_price"),
                        "gates_passed": window_snapshot.get("gates_passed", ""),
                        "gate_failed": window_snapshot.get("gate_failed"),
                        "confidence_tier": window_snapshot.get("confidence_tier", "?"),
                        "macro_bias": window_snapshot.get("macro_bias", "N/A"),
                        "macro_confidence": window_snapshot.get("macro_confidence", ""),
                        "macro_gate": window_snapshot.get("macro_gate", ""),
                        "clob_up_ask": window_snapshot.get("clob_up_ask"),
                        "clob_down_ask": window_snapshot.get("clob_down_ask"),
                        # v8.1 early entry fields
                        "v2_probability_up": window_snapshot.get("v2_probability_up"),
                        "v2_direction": window_snapshot.get("v2_direction"),
                        "v2_agrees": window_snapshot.get("v2_agrees"),
                        "entry_reason": getattr(signal, "entry_reason", "v8_standard"),
                        "eval_offset": eval_offset,
                        "v81_entry_cap": getattr(signal, "v81_entry_cap", None),
                        # v10.3 gate pipeline data
                        **_v103_data,
                    }
                    reason = f"VPIN {current_vpin:.3f} ({_snap_regime}), delta {delta_pct:+.4f}%"

                    # v12: Grab strategy port decisions if available
                    _sp_decisions = self._pending_strategy_decisions
                    self._pending_strategy_decisions = None  # consume once

                    # Send decision only if old system is primary executor
                    # (Strategy Engine v2 sends its own alerts when LEGACY_EXECUTION_DISABLED=true)
                    if os.environ.get("LEGACY_EXECUTION_DISABLED", "").lower() == "true":
                        return  # v2 registry handles alerts
                    await self._alerter.send_trade_decision_detailed(
                        window_id=window_id,
                        signal=signal_dict,
                        decision="TRADE",
                        reason=reason,
                        gamma_up=window_snapshot.get("gamma_up_price"),
                        gamma_down=window_snapshot.get("gamma_down_price"),
                        strategy_decisions=_sp_decisions,
                    )
                except Exception as alert_exc:
                    self._log.error(
                        "alert.trade_decision_failed",
                        error=str(alert_exc),
                        window_ts=window.window_ts,
                    )

            asyncio.create_task(_send_trade_alert())

        # Claude AI evaluation (non-blocking, 1min timeout)
        # Fetches FRESH Gamma price before evaluation so Claude sees real-time data
        if self._claude_eval and signal:
            try:
                _cg_dict = {}
                if cg:
                    _cg_dict = {
                        "oi_usd": cg.oi_usd,
                        "oi_delta_pct": cg.oi_delta_pct_1m,
                        "long_pct": cg.long_pct,
                        "short_pct": cg.short_pct,
                        "top_short_pct": cg.top_position_short_pct,
                        "funding_rate": cg.funding_rate,
                        "taker_buy": cg.taker_buy_volume_1m,
                        "taker_sell": cg.taker_sell_volume_1m,
                    }

                # v8.0: Use actual entry price from window snapshot, not fresh Gamma
                # Fresh Gamma moves after trade placement and misleads the evaluator
                _eval_entry = (
                    window_snapshot.get("gamma_up_price")
                    if signal.direction == "UP"
                    else window_snapshot.get("gamma_down_price")
                )
                _eval_entry = _eval_entry or (
                    window.up_price
                    if signal.direction == "UP"
                    else (window.down_price or 0.50)
                )

                asyncio.create_task(
                    self._claude_eval.evaluate_trade_decision(
                        asset=window.asset,
                        timeframe=tf,
                        direction=signal.direction,
                        confidence=signal.confidence,
                        delta_pct=signal.delta_pct,
                        vpin=signal.current_vpin,
                        regime=_snap_regime,
                        cg_snapshot=_cg_dict,
                        token_price=float(_eval_entry),
                        gamma_bestask=float(_eval_entry),
                        window_open_price=open_price,
                        current_price=current_price,
                        trade_placed=True,
                        price_source="SNAPSHOT",
                    )
                )
            except Exception:
                pass

        # Track executed window (DB-backed dedup — survives restarts)
        self._traded_windows.add(window_key)
        self._last_executed_window = window_key  # backward compat
        # CA-04 Phase 5: dual-write to WindowStateRepository (alongside in-memory set)
        if self._window_state is not None:
            try:
                await self._window_state.mark_traded(window_key, "pending")
            except Exception as _ws_exc:
                self._log.warning(
                    "window_state.mark_traded_failed",
                    key=window_key,
                    error=str(_ws_exc)[:80],
                )

    def _evaluate_signal(
        self,
        window: WindowInfo,
        current_price: float,
        current_vpin: float,
        delta_pct: float,
        twap_result: Optional[TWAPResult] = None,
        timesfm_forecast=None,
    ) -> Optional[FiveMinSignal]:
        """
        Evaluate trading signal based on delta, VPIN, and market regime.

        REGIME-AWARE DIRECTION (v4):
        ─────────────────────────────────────────────────────────
        VPIN >= 0.65 (cascade/trend):  MOMENTUM  — ride the trend
        VPIN  < 0.55 (normal/ranging): CONTRARIAN — mean-reversion
        VPIN 0.55–0.65 (transition):   CONTRARIAN with higher delta bar
        ─────────────────────────────────────────────────────────

        Evidence:
        - De Nicola (2021): 5-min BTC autocorrelation = -0.1016 (mean-reversion)
        - 4-day backtest: 55-59% contrarian WR in normal VPIN regime
        - Live data (Apr 3): 83% momentum WR when VPIN > 0.75 (cascade)

        Returns None if no trade should be placed.
        """
        # VPIN gate — core thesis: no informed flow = no trade
        if current_vpin < runtime.five_min_vpin_gate:
            self._log.info(
                "evaluate.vpin_below_gate",
                vpin=f"{current_vpin:.3f}",
                gate=f"{runtime.five_min_vpin_gate:.3f}",
            )
            self._last_skip_reason = (
                f"VPIN {current_vpin:.3f} < gate {runtime.five_min_vpin_gate}"
            )
            return None

        # TWAP Gamma Gate REMOVED in v10 cleanup (was feature-flagged OFF)

        # ── REGIME-AWARE DIRECTION (v4.1) ──────────────────────────
        # Cascade/trend regime: VPIN >= 0.65 → MOMENTUM + lower delta bar
        # Transition zone:      VPIN 0.55-0.65 → CONTRARIAN + higher delta bar
        # Normal/ranging:       VPIN < 0.55 → CONTRARIAN + standard delta bar
        #
        # KEY INSIGHT (Billy): In cascade, VPIN IS the signal. A smaller
        # delta still means something when informed flow is extreme.
        # Like how a small temperature rise matters more in a patient
        # who's already septic vs a healthy one.

        if current_vpin >= runtime.vpin_cascade_direction_threshold:
            # CASCADE/TREND: ride the momentum, VPIN-scaled delta bar
            # The higher the VPIN, the less delta we need — VPIN IS the signal
            # VPIN 0.65-0.75: use configured cascade min delta (0.03%)
            # VPIN 0.75-0.85: halve it (0.015%)
            # VPIN 0.85+:     near-zero (0.005%) — mega cascade, just go
            base_min = runtime.five_min_cascade_min_delta_pct
            if current_vpin >= 0.85:
                min_delta = 0.005
            elif current_vpin >= 0.75:
                # Linear scale from base_min down to base_min/2
                t = (current_vpin - 0.75) / 0.10
                min_delta = base_min * (1.0 - 0.5 * t)
            else:
                min_delta = base_min
            if abs(delta_pct) < min_delta:
                self._last_skip_reason = f"CASCADE: delta {abs(delta_pct):.4f}% < scaled threshold {min_delta:.4f}% (VPIN {current_vpin:.3f})"
                return None
            direction = "UP" if delta_pct > 0 else "DOWN"
            regime = "CASCADE"
            self._log.debug(
                "evaluate.cascade_delta_bar",
                vpin=f"{current_vpin:.3f}",
                min_delta=f"{min_delta:.4f}%",
                actual_delta=f"{abs(delta_pct):.4f}%",
                scaled="mega"
                if current_vpin >= 0.85
                else ("high" if current_vpin >= 0.75 else "base"),
            )
        elif current_vpin >= runtime.vpin_informed_threshold:
            # TRANSITION: v5.1 ALL MOMENTUM (contrarian = coin flip per 30-day data)
            if abs(delta_pct) < runtime.five_min_min_delta_pct:
                self._last_skip_reason = f"TRANSITION: delta {abs(delta_pct):.4f}% < threshold {runtime.five_min_min_delta_pct:.4f}% (VPIN {current_vpin:.3f})"
                return None
            direction = "UP" if delta_pct > 0 else "DOWN"
            regime = "TRANSITION"
        else:
            # NORMAL: v5.1 ALL MOMENTUM (contrarian = coin flip per 30-day data)
            if abs(delta_pct) < runtime.five_min_min_delta_pct:
                self._last_skip_reason = f"NORMAL: delta {abs(delta_pct):.4f}% < threshold {runtime.five_min_min_delta_pct:.4f}% (VPIN {current_vpin:.3f})"
                return None
            direction = "UP" if delta_pct > 0 else "DOWN"
            regime = "NORMAL"

        # TWAP Direction Override REMOVED in v10 cleanup (was feature-flagged OFF)

        confidence = self._calculate_confidence(delta_pct, current_vpin, direction)

        # ── CoinGlass Confirmation Layer ───────────────────────────────────
        # CoinGlass is a CONFIRMING signal only. VPIN + delta are primary.
        # If CG agrees → can lift LOW to MODERATE, boost MODERATE to HIGH.
        # If CG strongly disagrees → reduce confidence or skip.
        cg_confidence_modifier = 0.0
        cg_log_parts: list[str] = []

        cg = self._cg_enhanced.snapshot if self._cg_enhanced is not None else None

        if cg is not None and cg.connected:
            # ── v5.1 DATA-DRIVEN CG MODIFIERS ─────────────────────────────
            # 30-day backtest (8,640 windows) proved:
            #   - L/S ratio: coin flip at ANY threshold (49-51%)
            #   - Crowd positioning: coin flip
            #   - Smart money divergence: insufficient samples
            #   - Taker aggression: not validated within-window
            #   - Funding rate: coin flip
            #   - OI delta >0.10%: +2.9% WR lift (ONLY signal that works)
            #
            # ZEROED: liq, crowd, smart money, taker, funding
            # KEPT: OI delta as sole confirmer
            # ALL data still logged + saved for future analysis

            # OI Delta confirmation — the only CG signal backed by data
            # Rising OI >0.10% confirms real position changes backing the move
            if abs(cg.oi_delta_pct_1m) > 0.001:  # >0.10% OI change
                cg_confidence_modifier += 0.10
                cg_log_parts.append(
                    f"oi_delta_confirms(+0.10, OI_Δ={cg.oi_delta_pct_1m:.3f}%)"
                )

            # ── CoinGlass VETO SYSTEM v7.1 ────────────────────────────────
            # Fires when 2+ signals strongly oppose our direction.
            # (Was 3+ in v5.4d — too loose. Apr 5 trade slipped through with only
            #  1 trigger despite 3 independent bullish signals against a DOWN bet.)
            #
            # Changes from v5.4d → v7.1:
            #   1. Veto threshold: 3+ → 2+  (was too forgiving)
            #   2. Smart money threshold: 55% → 52%  (catch near-majority divergence)
            #   3. Taker threshold: 65% → 60%  (catch clear directional flow)
            #   4. Funding bug fixed: was only checking negative funding for DOWN veto.
            #      Strongly POSITIVE funding (longs paying huge premium) is bullish —
            #      it should also veto a DOWN bet. Threshold: >100% annualised.
            #   5. New: CASCADE + taker divergence. If VPIN ≥ 0.65 (high informed flow)
            #      but taker flow opposes direction >55%, that's the worst case — VPIN
            #      detected real flow but takers are telling us which WAY it goes.
            #
            # Replay of Apr 5 DOWN trade (LOSS):
            #   taker_buying=66.2% > 60%         → veto +1
            #   smart_money_long=54% > 52%        → veto +1
            #   CASCADE_taker_divergence           → veto +1
            #   Total: 3 → TRADE WOULD HAVE BEEN BLOCKED ✓

            _veto_count = 0
            _veto_reasons = []

            # 1. Smart money opposing: top traders >52% on the other side (was 55%)
            if direction == "UP" and cg.top_position_short_pct > 52:
                _veto_count += 1
                _veto_reasons.append(
                    f"smart_money_short={cg.top_position_short_pct:.0f}%"
                )
            elif direction == "DOWN" and cg.top_position_long_pct > 52:
                _veto_count += 1
                _veto_reasons.append(
                    f"smart_money_long={cg.top_position_long_pct:.0f}%"
                )

            # 2. Funding opposing — FIXED: check both directions properly
            # For DOWN bet: both strong positive AND strong negative funding can be signals
            # Strong POSITIVE funding = longs paying big premium = bullish conviction against DOWN
            # Strong NEGATIVE funding = not relevant for DOWN veto (shorts paying = bearish = confirms DOWN)
            _funding_annual = cg.funding_rate * 3 * 365  # 8h rate → annualised
            if (
                direction == "UP" and cg.funding_rate > 0.0005
            ):  # longs paying → bearish → against UP
                _veto_count += 1
                _veto_reasons.append(f"funding_against_up={_funding_annual:.0f}%/yr")
            elif (
                direction == "DOWN" and _funding_annual > 1.0
            ):  # >100%/yr longs paying → bullish → against DOWN
                _veto_count += 1
                _veto_reasons.append(
                    f"funding_bullish_vs_down={_funding_annual:.0f}%/yr"
                )
            elif (
                direction == "DOWN" and cg.funding_rate < -0.0005
            ):  # shorts paying heavily → bearish → confirms DOWN (no veto)
                pass  # This confirms our DOWN bet, not opposes it

            # 3. Crowd overleveraged in opposing direction (unchanged, >60%)
            if direction == "UP" and cg.long_pct > 60:
                _veto_count += 1
                _veto_reasons.append(f"crowd_overleveraged_long={cg.long_pct:.0f}%")
            elif direction == "DOWN" and cg.short_pct > 60:
                _veto_count += 1
                _veto_reasons.append(f"crowd_overleveraged_short={cg.short_pct:.0f}%")

            # 4. Taker volume opposing (was >65%, now >60%)
            _taker_total = cg.taker_buy_volume_1m + cg.taker_sell_volume_1m
            if _taker_total > 0:
                _sell_pct = cg.taker_sell_volume_1m / _taker_total * 100
                _buy_pct = 100 - _sell_pct
                if direction == "UP" and _sell_pct > 60:
                    _veto_count += 1
                    _veto_reasons.append(f"taker_selling={_sell_pct:.0f}%")
                elif direction == "DOWN" and _buy_pct > 60:
                    _veto_count += 1
                    _veto_reasons.append(f"taker_buying={_buy_pct:.0f}%")

            # 5. NEW: CASCADE + taker divergence (worst case — VPIN says "big player"
            #    but takers say "they're going the other way")
            if _taker_total > 0:
                if current_vpin >= 0.65:
                    if direction == "UP" and _sell_pct > 55:
                        _veto_count += 1
                        _veto_reasons.append(
                            f"cascade_taker_divergence: vpin={current_vpin:.2f} but sell={_sell_pct:.0f}%"
                        )
                    elif direction == "DOWN" and _buy_pct > 55:
                        _veto_count += 1
                        _veto_reasons.append(
                            f"cascade_taker_divergence: vpin={current_vpin:.2f} but buy={_buy_pct:.0f}%"
                        )

            # VETO: 3+ signals opposing = block (restored from v5.4d)
            if _veto_count >= 3:
                self._log.warning(
                    "evaluate.cg_veto",
                    direction=direction,
                    veto_count=_veto_count,
                    reasons=", ".join(_veto_reasons),
                    asset=window.asset,
                )
                self._last_skip_reason = (
                    f"CG VETO ({_veto_count} signals): {', '.join(_veto_reasons)}"
                )
                return None

            # Log "would have blocked" for tracking — even when not vetoing
            if _veto_count == 1:
                self._log.info(
                    "evaluate.cg_would_warn",
                    direction=direction,
                    veto_count=_veto_count,
                    reasons=", ".join(_veto_reasons),
                    asset=window.asset,
                )

        # Clamp modifier to [-0.5, +0.5]
        cg_confidence_modifier = max(-0.5, min(0.5, cg_confidence_modifier))

        self._log.debug(
            "evaluate.coinglass_signal",
            regime=regime,
            direction=direction,
            cg_modifier=f"{cg_confidence_modifier:+.2f}",
            cg_connected=cg is not None and cg.connected if cg else False,
            contributions=", ".join(cg_log_parts) if cg_log_parts else "none",
            liq_long=f"${cg.liq_long_usd_1m:,.0f}" if cg else "n/a",
            liq_short=f"${cg.liq_short_usd_1m:,.0f}" if cg else "n/a",
            long_pct=f"{cg.long_pct:.1f}%" if cg else "n/a",
            top_short_pct=f"{cg.top_position_short_pct:.1f}%" if cg else "n/a",
            taker_sell_pct=f"{cg.taker_sell_volume_1m / (cg.taker_buy_volume_1m + cg.taker_sell_volume_1m) * 100:.1f}%"
            if (cg and (cg.taker_buy_volume_1m + cg.taker_sell_volume_1m) > 0)
            else "n/a",
            funding=f"{cg.funding_rate * 100:.4f}%" if cg else "n/a",
        )

        # Apply CoinGlass modifier to lift/suppress confidence
        # Modifier > 0.2: can lift LOW → MODERATE
        # Modifier < -0.2: suppress MODERATE → LOW (skip), or HIGH → MODERATE (skip)
        if cg_confidence_modifier >= 0.2 and confidence == "LOW":
            self._log.info(
                "evaluate.cg_lift",
                from_confidence="LOW",
                to_confidence="MODERATE",
                modifier=f"{cg_confidence_modifier:+.2f}",
                contributions=", ".join(cg_log_parts),
            )
            confidence = "MODERATE"
        elif cg_confidence_modifier <= -0.2 and confidence == "MODERATE":
            self._log.info(
                "evaluate.cg_suppress",
                from_confidence="MODERATE",
                to_confidence="LOW",
                modifier=f"{cg_confidence_modifier:+.2f}",
                contributions=", ".join(cg_log_parts),
            )
            confidence = "LOW"
        elif cg_confidence_modifier <= -0.35 and confidence == "HIGH":
            self._log.info(
                "evaluate.cg_suppress",
                from_confidence="HIGH",
                to_confidence="MODERATE",
                modifier=f"{cg_confidence_modifier:+.2f}",
                contributions=", ".join(cg_log_parts),
            )
            confidence = "MODERATE"

        # TWAP Confidence Adjustment REMOVED in v10 cleanup (was feature-flagged OFF)

        # Block NONE and LOW confidence — only trade MODERATE or HIGH
        if confidence in ("NONE", "LOW"):
            return None

        # TimesFM Agreement REMOVED in v10 cleanup (47.8% accuracy, worse than coin flip)
        timesfm_agreement = None

        self._log.info(
            "evaluate.regime_signal",
            regime=regime,
            vpin=f"{current_vpin:.3f}",
            delta=f"{delta_pct:+.4f}%",
            direction=direction,
            confidence=confidence,
            cg_modifier=f"{cg_confidence_modifier:+.2f}",
            timesfm_agreement=timesfm_agreement,
        )

        return FiveMinSignal(
            window=window,
            current_price=current_price,
            current_vpin=current_vpin,
            delta_pct=delta_pct,
            confidence=confidence,
            direction=direction,
            cg_modifier=cg_confidence_modifier,
        )

    def _calculate_confidence(
        self,
        delta_pct: float,
        current_vpin: float,
        direction: str,
    ) -> str:
        """
        Calculate confidence level based on delta and VPIN.

        - If VPIN >= 0.30 AND delta direction aligns → HIGH
        - If delta > 0.10% → HIGH
        - If delta > 0.02% → MODERATE
        - If delta > 0.005% → LOW
        """
        abs_delta = abs(delta_pct)

        # High confidence: strong delta alone is enough
        if abs_delta > 0.10:
            return "HIGH"

        # Moderate confidence: decent delta
        if abs_delta > 0.02:
            return "MODERATE"

        # Low confidence: small but non-trivial delta
        if abs_delta > 0.005:
            return "LOW"

        return "NONE"

    # ─── Guardrail Helpers ────────────────────────────────────────────────────
    # NOTE: _execute_from_signal() deleted in v10 cleanup (was 430 lines, never called)
    #       Trade execution moved to use-case layer in Phase 2 clean-arch.
    #       See git history for removed code if needed.

    def _check_rate_limit(self) -> tuple[bool, str]:
        """
        G4: Check order rate limiting.
        Returns (allowed, reason). reason is empty string when allowed.
        """
        now = time.time()

        # Purge timestamps older than 1 hour
        cutoff = now - 3600.0
        self._order_timestamps = [ts for ts in self._order_timestamps if ts > cutoff]

        # Check minimum interval between orders
        if self._last_order_time > 0:
            elapsed = now - self._last_order_time
            if elapsed < runtime.min_order_interval_seconds:
                return False, (
                    f"rate_limit.too_fast: {elapsed:.1f}s since last order "
                    f"(min {runtime.min_order_interval_seconds:.0f}s)"
                )

        # Check hourly cap
        if len(self._order_timestamps) >= runtime.max_orders_per_hour:
            return False, (
                f"rate_limit.hourly_cap: {len(self._order_timestamps)} orders "
                f"in last hour (max {runtime.max_orders_per_hour})"
            )

        return True, ""

    def _record_order_placed(self) -> None:
        """G4: Record that an order was placed now."""
        now = time.time()
        self._order_timestamps.append(now)
        self._last_order_time = now

    def _check_circuit_breaker(self) -> tuple[bool, str]:
        """
        G5: Check if the circuit breaker is active.
        Returns (allowed, reason).
        """
        now = time.time()
        if self._circuit_break_until > now:
            remaining = self._circuit_break_until - now
            return False, f"circuit_breaker.active: {remaining:.0f}s remaining"
        return True, ""

    def _on_order_error(self, error: Exception) -> None:
        """G5: Handle an order error — activate circuit breaker if needed."""
        now = time.time()
        error_str = str(error)

        # Check for 4xx errors
        is_4xx = any(str(code) in error_str for code in range(400, 500))
        is_error = True  # Any exception counts as consecutive error

        if is_4xx:
            self._circuit_break_until = now + 60  # 60 seconds (was 15 min)
            self._consecutive_errors = 0
            self._log.error(
                "guardrail.circuit_breaker.4xx",
                error=error_str[:200],
                break_until=self._circuit_break_until,
                break_minutes=15,
            )
        elif is_error:
            self._consecutive_errors += 1
            if self._consecutive_errors >= 3:
                self._circuit_break_until = now + 180  # 3 minutes (was 1 hour)
                self._log.error(
                    "guardrail.circuit_breaker.consecutive",
                    consecutive_errors=self._consecutive_errors,
                    break_until=self._circuit_break_until,
                    break_minutes=60,
                )
            else:
                self._log.warning(
                    "guardrail.circuit_breaker.error_count",
                    consecutive_errors=self._consecutive_errors,
                    breaks_at=3,
                )

    def _on_order_success(self) -> None:
        """G5: Reset consecutive error counter on successful order."""
        self._consecutive_errors = 0

    async def evaluate(self, state: MarketState) -> Optional[dict]:
        """Evaluate market state for trading signals."""
        # This strategy uses window signals, not continuous evaluation
        return None

    async def execute(self, state: MarketState, signal: dict) -> Optional[Order]:
        """Execute a trading signal."""
        # This strategy handles execution internally
        return None
