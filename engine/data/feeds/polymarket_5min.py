"""
Polymarket 5-Minute Market Discovery

Auto-discovers and subscribes to BTC Up/Down 5-minute markets.
Calculates current window timestamp, fetches market from gamma API,
and provides token IDs for trading.

Markets available:
- btc-updown-5m-{ts}
- eth-updown-5m-{ts}
- sol-updown-5m-{ts}
- doge-updown-5m-{ts}
- bnb-updown-5m-{ts}
- xrp-updown-5m-{ts}

Also 15-minute versions: btc-updown-15m-{ts}, etc.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Awaitable, Optional, Dict, List, Deque
import httpx
import structlog

log = structlog.get_logger(__name__)


# Concurrency cap for fire-and-forget window-signal callbacks. Without this,
# the feed loop dispatches a fresh callback every ~2s and they pile up
# unboundedly when the strategy is slow (e.g. waiting on a saturated DB pool).
# Tunable via FIVE_MIN_SIGNAL_MAX_INFLIGHT (default 4).
try:
    _SIGNAL_MAX_INFLIGHT = max(int(os.environ.get("FIVE_MIN_SIGNAL_MAX_INFLIGHT", "4")), 1)
except (TypeError, ValueError):
    _SIGNAL_MAX_INFLIGHT = 4


class WindowState(Enum):
    """Lifecycle state for a 5-minute trading window."""

    WAITING = "WAITING"  # Window not yet active
    ACTIVE = "ACTIVE"  # Window is live, trading allowed
    CLOSING = "CLOSING"  # T-10s, signal to evaluate
    RESOLVED = "RESOLVED"  # Window closed and resolved


@dataclass
class WindowInfo:
    """Information about a 5-minute trading window."""

    window_ts: int  # Unix timestamp of window start
    asset: str  # e.g. "BTC", "ETH"
    duration_secs: int  # 300 for 5m, 900 for 15m
    state: WindowState = WindowState.WAITING
    open_price: Optional[float] = None  # Opening price (Polymarket priceToBeat, Chainlink fallback)
    current_price: Optional[float] = None  # Current price
    up_token_id: Optional[str] = None  # "Up" outcome token ID
    down_token_id: Optional[str] = None  # "Down" outcome token ID
    up_price: Optional[float] = None  # Current Up token price
    down_price: Optional[float] = None  # Current Down token price
    price_source: str = "unknown"  # "gamma_api", "synthetic", "stale_gamma"
    eval_offset: Optional[int] = None  # Current T-minus evaluation offset, if any
    gamma_price_to_beat: Optional[float] = None  # Polymarket-published reference price (canonical)
    open_price_source: str = "unknown"  # "polymarket_priceToBeat", "chainlink_polygon", "binance_fallback"
    _gamma_metadata_synced: bool = False  # True once eventMetadata.priceToBeat captured
    _gamma_resync_attempts: int = 0  # # of post-open Gamma re-polls attempted

    @property
    def timeframe(self) -> str:
        """Semantic timeframe derived from duration_secs."""
        return "15m" if self.duration_secs >= 900 else "5m"


class Polymarket5MinFeed:
    """
    Auto-discovers and tracks 5-minute Polymarket Up/Down markets.

    Calculates the current window timestamp aligned to 300-second intervals,
    fetches market data from the Gamma API, and provides token IDs for trading.

    Emits signals to the strategy at T-10 seconds (290s into window) for
    optimal entry timing.

    Attributes:
        on_window_signal: Callback invoked when T-10s signal is ready.
        on_window_state_change: Callback invoked when window state changes.
    """

    # Available assets for 5-minute markets
    SUPPORTED_ASSETS = ["BTC", "ETH", "SOL", "DOGE", "BNB", "XRP"]

    # Default window duration (5 minutes in seconds)
    DEFAULT_DURATION = 300

    # Signal offset - trigger strategy evaluation at T-10s
    SIGNAL_OFFSET = 10  # seconds before window close

    def __init__(
        self,
        assets: List[str] = None,
        duration_secs: int = 300,
        signal_offset: int = 10,
        eval_offsets: Optional[List[int]] = None,
        on_window_signal: Optional[Callable[[WindowInfo], Awaitable[None]]] = None,
        on_window_state_change: Optional[
            Callable[[str, WindowState, WindowState], Awaitable[None]]
        ] = None,
        paper_mode: bool = True,
        chainlink_feed: Optional[object] = None,
        rtds_feed: Optional[object] = None,
        html_ptb_feed: Optional[object] = None,
    ) -> None:
        """
        Initialize the 5-minute market feed.

        Args:
            assets: List of assets to track (default: ["BTC"])
            duration_secs: Window duration in seconds (default: 300 for 5m)
            signal_offset: Seconds before close to signal strategy (default: 10)
            on_window_signal: Async callback when T-10s signal ready
            on_window_state_change: Async callback on state change
            paper_mode: If True, simulate market data
            chainlink_feed: ChainlinkFeed instance with latest_prices dict
                for oracle-aligned open price (fallback source)
            rtds_feed: (DEPRECATED) Legacy RTDS WebSocket feed — broken
                upstream (zero messages received). Kept for backwards-compat
                but no longer queried; ``html_ptb_feed`` is the new primary.
            html_ptb_feed: PolymarketHTMLPriceToBeatFeed instance — scrapes
                the public Polymarket event page to read the canonical
                ``priceToBeat`` Polymarket displays in their UI. Replaces
                ``rtds_feed`` as the PRIMARY open-price source.
        """
        self._assets = assets or ["BTC"]
        self._duration_secs = duration_secs
        self._signal_offset = signal_offset
        self._eval_offsets_override = eval_offsets  # if set, use instead of FIVE_MIN_EVAL_OFFSETS
        self._on_window_signal = on_window_signal
        self._on_window_state_change = on_window_state_change
        self._paper_mode = paper_mode
        self._chainlink_feed = chainlink_feed
        self._rtds_feed = rtds_feed  # legacy, unused
        self._html_ptb_feed = html_ptb_feed

        # Track windows by asset -> window_ts -> WindowInfo
        self._windows: Dict[str, Dict[int, WindowInfo]] = {
            asset: {} for asset in self._assets
        }

        # Current active window per asset
        self._current_windows: Dict[str, int] = {asset: None for asset in self._assets}

        # HTTP client for Gamma API
        self._http_client: Optional[httpx.AsyncClient] = None

        # Background task handle
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Bounded inflight queue for fire-and-forget window-signal callbacks.
        # When the strategy is slow (e.g. saturated DB pool), callbacks pile up
        # and exhaust shared resources. Cap concurrency at _SIGNAL_MAX_INFLIGHT;
        # if exceeded, drop the OLDEST pending callback so the newest signal
        # (most actionable) still runs.
        self._inflight_signals: Deque[asyncio.Task] = deque()
        self._inflight_max = _SIGNAL_MAX_INFLIGHT

        self._log = log.bind(component="Polymarket5MinFeed", assets=self._assets)
        self._log.info(
            "initialised",
            duration_secs=duration_secs,
            signal_offset=signal_offset,
            paper_mode=paper_mode,
        )

    # ─── Public Properties ────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        """True if the feed is actively running."""
        return self._running

    def get_current_window(self, asset: str = "BTC") -> Optional[WindowInfo]:
        """Get the current active window for an asset."""
        if asset not in self._current_windows:
            return None
        window_ts = self._current_windows[asset]
        if window_ts is None:
            return None
        return self._windows[asset].get(window_ts)

    def get_window_open_price(self, asset: str = "BTC") -> Optional[float]:
        """Get the open price for the current window."""
        window = self.get_current_window(asset)
        return window.open_price if window else None

    def get_window_prices(self, asset: str = "BTC") -> Optional[dict]:
        """Get current Up/Down prices for the current window."""
        window = self.get_current_window(asset)
        if not window:
            return None
        return {
            "up": window.up_price,
            "down": window.down_price,
            "up_token_id": window.up_token_id,
            "down_token_id": window.down_token_id,
        }

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the market discovery loop."""
        self._running = True
        self._http_client = httpx.AsyncClient(timeout=10.0)

        self._log.info("feed.started")

        while self._running:
            try:
                await self._process_loop()
                await asyncio.sleep(1.0)  # Check every second
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log.error("feed.loop_error", error=str(exc))
                await asyncio.sleep(5.0)  # Back off on error

    async def stop(self) -> None:
        """Stop the market discovery loop."""
        self._running = False

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        self._log.info("feed.stopped")

    # ─── Main Loop ────────────────────────────────────────────────────────────

    async def _process_loop(self) -> None:
        """Main processing loop - updates all tracked windows."""
        now = time.time()

        for asset in self._assets:
            # Calculate current window timestamp
            window_ts = self._calculate_window_ts(now, asset)

            # Check if we need to switch to a new window
            current_ts = self._current_windows[asset]
            if current_ts != window_ts:
                await self._handle_window_change(asset, window_ts)

            # Get or update window info
            window = self._windows[asset].get(window_ts)
            if window:
                await self._update_window(window, now)

    def _calculate_window_ts(self, now: float, asset: str) -> int:
        """Calculate the current window timestamp aligned to duration."""
        return (int(now) // self._duration_secs) * self._duration_secs

    async def _handle_window_change(self, asset: str, new_ts: int) -> None:
        """Handle transition to a new window."""
        old_ts = self._current_windows[asset]
        old_window = self._windows[asset].get(old_ts) if old_ts else None

        self._log.info(
            "window.change",
            asset=asset,
            old_ts=old_ts,
            new_ts=new_ts,
        )

        # Mark old window as resolved if it exists
        if old_window:
            old_window.state = WindowState.RESOLVED
            await self._emit_state_change(asset, old_ts, old_window.state)

        # Create new window
        new_window = WindowInfo(
            window_ts=new_ts,
            asset=asset,
            duration_secs=self._duration_secs,
            state=WindowState.WAITING,
        )
        self._windows[asset][new_ts] = new_window
        self._current_windows[asset] = new_ts

        # Fetch market data
        await self._fetch_market_data(new_window)

        # Emit state change
        await self._emit_state_change(asset, new_ts, new_window.state)

    async def _update_window(self, window: WindowInfo, now: float) -> None:
        """Update window state based on elapsed time."""
        elapsed = int(now) - window.window_ts
        remaining = window.duration_secs - elapsed

        # State machine transitions
        if window.state == WindowState.WAITING:
            if elapsed >= 0:
                window.state = WindowState.ACTIVE
                await self._emit_state_change(
                    window.asset, window.window_ts, window.state
                )
                # Emit window signal at OPEN so strategy can start monitoring
                await self._emit_window_signal(window)

        elif window.state in (WindowState.ACTIVE, WindowState.CLOSING):
            # ── Sync open_price from Polymarket priceToBeat (canonical) ───────
            # Polymarket samples Chainlink Data Streams at each window boundary.
            # That sample becomes available via Gamma's eventMetadata as
            # finalPrice[N-1] = priceToBeat[N] ~50-90s after window N-1 closes
            # (i.e. ~50-90s into window N). Poll the PREVIOUS window's Gamma
            # event at +60s, +90s, +120s, +180s elapsed-into-window until we
            # capture it. This produces an open_price that matches the UI
            # "Price To Beat" exactly, replacing the Chainlink Polygon
            # approximation we used at window creation (off by $3-30/window).
            if not window._gamma_metadata_synced:
                resync_offsets = [60, 90, 120, 180, 240, 270]
                for _idx, _ofs in enumerate(resync_offsets):
                    if (
                        elapsed >= _ofs
                        and window._gamma_resync_attempts < (_idx + 1)
                    ):
                        window._gamma_resync_attempts = _idx + 1
                        try:
                            await self._sync_open_price_from_prev_window_final(window)
                        except Exception as exc:
                            self._log.warning(
                                "open_price.resync_failed",
                                window_ts=window.window_ts,
                                elapsed=elapsed,
                                error=str(exc),
                            )
                        break  # one resync per tick

            # ── Countdown re-emissions at T-180, T-120, T-90 ─────────────
            # Re-emit window signal at countdown milestones so orchestrator can send alerts
            if window.state == WindowState.ACTIVE:
                _countdown_milestones = [180, 120, 90]
                if not hasattr(window, "_countdown_emitted"):
                    window._countdown_emitted = set()
                for _ms in _countdown_milestones:
                    if remaining <= _ms and _ms not in window._countdown_emitted:
                        window._countdown_emitted.add(_ms)
                        await self._emit_window_signal(window)
                        break  # Only one per tick

            # ── Multi-offset evaluation signals ──────────────────────────
            # Emit CLOSING signal at each configured eval offset (T-90, T-60, etc.)
            # Allows strategy to evaluate (and optionally trade) at multiple points.
            # Each offset fires exactly once per window.
            if self._eval_offsets_override is not None:
                _eval_offsets = self._eval_offsets_override
            else:
                try:
                    from config.constants import FIVE_MIN_EVAL_OFFSETS as _eval_offsets
                except ImportError:
                    _eval_offsets = [self._signal_offset]

            if not hasattr(window, "_eval_offsets_emitted"):
                window._eval_offsets_emitted = set()

            for _offset in _eval_offsets:
                if remaining <= _offset and _offset not in window._eval_offsets_emitted:
                    window._eval_offsets_emitted.add(_offset)
                    # Tag the window with which offset fired so strategy knows
                    window.eval_offset = _offset
                    window.state = WindowState.CLOSING
                    await self._emit_state_change(
                        window.asset, window.window_ts, window.state
                    )
                    await self._emit_window_signal(window)
                    # Don't break — check remaining offsets in same tick
                    # so T-60 retry fires even if T-70 and T-60 both became
                    # eligible between ticks

            # Window expired
            if remaining <= 0:
                window.state = WindowState.RESOLVED
                await self._emit_state_change(
                    window.asset, window.window_ts, window.state
                )

    async def _emit_state_change(
        self, asset: str, window_ts: int, new_state: WindowState
    ) -> None:
        """Emit window state change callback."""
        if self._on_window_state_change:
            try:
                await self._on_window_state_change(f"{asset}-{window_ts}", new_state)
            except Exception as exc:
                self._log.error("state_change_callback_error", error=str(exc))

    async def _emit_window_signal(self, window: WindowInfo) -> None:
        """Emit T-10s signal to strategy.

        Fires the strategy callback as a background task so the feed loop
        is never blocked on slow strategy work. To prevent unbounded pile-up
        when the strategy is slow (e.g. DB pool saturated), we cap concurrent
        in-flight callbacks at self._inflight_max. If at the cap, the OLDEST
        pending task is cancelled and dropped — newer windows are more
        actionable than stale ones.
        """
        if self._on_window_signal:
            try:
                window_snapshot = replace(window)

                # Reap any completed tasks before checking the cap.
                self._inflight_signals = deque(
                    t for t in self._inflight_signals if not t.done()
                )

                # Enforce concurrency cap: drop oldest if we're at the limit.
                while len(self._inflight_signals) >= self._inflight_max:
                    oldest = self._inflight_signals.popleft()
                    if not oldest.done():
                        oldest.cancel()
                        self._log.warning(
                            "window_signal_dropped_oldest",
                            inflight=len(self._inflight_signals) + 1,
                            cap=self._inflight_max,
                        )

                task = asyncio.create_task(self._on_window_signal(window_snapshot))
                self._inflight_signals.append(task)

                def _log_callback_error(done_task: asyncio.Task) -> None:
                    if done_task.cancelled():
                        return
                    exc = done_task.exception()
                    if exc is not None:
                        self._log.error("window_signal_callback_error", error=str(exc))

                task.add_done_callback(_log_callback_error)
                self._log.info(
                    "window.signal",
                    asset=window.asset,
                    window_ts=window.window_ts,
                    open_price=window.open_price,
                    up_price=window.up_price,
                    down_price=window.down_price,
                    inflight=len(self._inflight_signals),
                )
            except Exception as exc:
                self._log.error("window_signal_dispatch_error", error=str(exc))

    # ─── Market Data Fetching ─────────────────────────────────────────────────

    async def _fetch_market_data(self, window: WindowInfo) -> None:
        """Fetch market data from Polymarket Gamma API.

        ALWAYS tries to fetch real Gamma API prices first — even in paper mode.
        This ensures paper P&L uses realistic token costs ($0.50-0.55) instead
        of the synthetic delta-based model ($0.70-0.97) which massively
        overstates token costs and makes paper results unrealistically negative.

        Falls back to paper data generation only if Gamma API fails.
        """
        try:
            await self._fetch_live_data(window)
        except Exception as exc:
            self._log.warning("market_fetch_failed_falling_back", error=str(exc))

        # Paper mode: set open price from Binance + generate paper token IDs
        if self._paper_mode:
            await self._fetch_paper_data(window)
        else:
            # Live mode: still need open price from Binance for delta calculation
            if window.open_price is None:
                await self._fetch_open_price(window)

    async def _fetch_live_data(self, window: WindowInfo) -> None:
        """Fetch live market data from Gamma API.

        The Gamma API returns a list of events. Each event has a ``markets``
        array; each market has a ``clobTokenIds`` array where:
          - index 0 → YES / Up token ID
          - index 1 → NO  / Down token ID

        Example abbreviated response::

            [
              {
                "slug": "btc-updown-5m-...",
                "markets": [
                  {
                    "clobTokenIds": ["<yes_token_id>", "<no_token_id>"],
                    "bestAsk": "0.52",
                    "bestBid": "0.48"
                  }
                ]
              }
            ]
        """
        slug = self._build_slug(window)

        if not self._http_client:
            return

        try:
            # Fetch event from Gamma API
            resp = await self._http_client.get(
                "https://gamma-api.polymarket.com/events",
                params={"slug": slug},
            )
            resp.raise_for_status()

            data = resp.json()

            if not data or not isinstance(data, list) or len(data) == 0:
                self._log.warning("market.not_found", slug=slug)
                return

            event = data[0]

            # ── Capture priceToBeat (canonical Polymarket reference) if present ──
            # Polymarket publishes `eventMetadata.priceToBeat` for THIS window, but
            # only AFTER it closes (~50-90s after close). For an actively-trading
            # window, this field is None. The companion routine
            # `_sync_open_price_from_prev_window_final` polls the PREVIOUS window's
            # `finalPrice` (which Polymarket guarantees equals THIS window's
            # `priceToBeat`) — that becomes available shortly after the previous
            # window closes, i.e. within the first ~60-90s of THIS window. So this
            # block here just opportunistically captures the value if Gamma has
            # back-filled it (e.g. on a late strategy fire or post-resolution).
            event_meta = event.get("eventMetadata") or {}
            ptb_raw = event_meta.get("priceToBeat") if isinstance(event_meta, dict) else None
            if ptb_raw is not None:
                try:
                    ptb_val = float(ptb_raw)
                    if ptb_val > 0:
                        window.gamma_price_to_beat = ptb_val
                        window._gamma_metadata_synced = True
                        # Authoritative — override any prior Chainlink-derived open_price
                        prev_open = window.open_price
                        prev_source = window.open_price_source
                        window.open_price = ptb_val
                        window.open_price_source = "polymarket_priceToBeat"
                        # Unconditional probe — fires on EVERY priceToBeat capture,
                        # regardless of whether prev_open existed. Used to prove the
                        # priceToBeat → open_price plumbing is actually executing.
                        self._log.info(
                            "open_price.priceToBeat_captured_in_fetch_live",
                            window_ts=window.window_ts,
                            asset=window.asset,
                            priceToBeat=ptb_val,
                            prev_open=prev_open,
                            prev_source=prev_source,
                        )
                        if prev_open is not None and abs(prev_open - ptb_val) > 0.01:
                            self._log.info(
                                "open_price.priceToBeat_override",
                                window_ts=window.window_ts,
                                asset=window.asset,
                                prev_open=prev_open,
                                prev_source=prev_source,
                                new_open=ptb_val,
                                delta=round(prev_open - ptb_val, 4),
                            )
                except (TypeError, ValueError):
                    pass

            # The event may carry markets as a nested list
            markets = event.get("markets", [])
            if not markets:
                self._log.warning(
                    "market.no_markets_in_event",
                    slug=slug,
                    event_keys=list(event.keys()),
                )
                return

            # Up/Down markets: first market in the list (there should only be one
            # for 5-minute binary events, but be defensive)
            market = markets[0]

            raw_token_ids = market.get("clobTokenIds") or []
            # Gamma API returns clobTokenIds as a JSON string, not a list
            if isinstance(raw_token_ids, str):
                import json as _json

                try:
                    raw_token_ids = _json.loads(raw_token_ids)
                except (ValueError, TypeError):
                    raw_token_ids = []
            clob_token_ids: list = raw_token_ids

            if len(clob_token_ids) >= 2:
                window.up_token_id = str(clob_token_ids[0])  # YES / Up
                window.down_token_id = str(clob_token_ids[1])  # NO  / Down
            elif len(clob_token_ids) == 1:
                window.up_token_id = str(clob_token_ids[0])
                self._log.warning("market.only_one_token_id", slug=slug)
            else:
                self._log.warning(
                    "market.no_token_ids", slug=slug, market_keys=list(market.keys())
                )
                return

            # Extract best-ask prices if available
            try:
                best_ask = market.get("bestAsk") or market.get("best_ask")
                if best_ask is not None:
                    window.up_price = float(best_ask)
                    window.down_price = round(1.0 - window.up_price, 4)
                    window.price_source = "gamma_api"
            except (TypeError, ValueError):
                pass  # prices will stay None; strategy will handle

            self._log.info(
                "market.fetched",
                slug=slug,
                up_token_id=window.up_token_id[:20] + "..."
                if window.up_token_id and len(window.up_token_id) > 20
                else window.up_token_id,
                down_token_id=window.down_token_id[:20] + "..."
                if window.down_token_id and len(window.down_token_id) > 20
                else window.down_token_id,
                up_price=window.up_price,
                down_price=window.down_price,
            )

        except Exception as exc:
            self._log.error("gamma_api_error", error=str(exc))

    async def _fetch_open_price(self, window: WindowInfo) -> None:
        """Fetch the window open price.

        Priority order:
          0. Polymarket public event HTML (canonical — the EXACT priceToBeat
             value Polymarket displays in their UI, parsed out of the
             Next.js ``__NEXT_DATA__`` JSON blob. Available within ~1-3s of
             the window boundary. Replaces the broken RTDS WebSocket feed.)
          1. Polymarket eventMetadata.priceToBeat (Gamma — same value, but
             only published a few seconds after window open).
          2. Chainlink Polygon in-memory cache (approximation; ~$3-30 skew vs
             Polymarket's Chainlink Data Streams sample. Used pre-open or if
             HTML feed is unavailable.)
          3. Binance REST (fallback).

        Polymarket 5m markets resolve via Chainlink Data Streams (off-chain,
        low-latency feed) — NOT the on-chain Aggregator V3 contract on Polygon.
        The two diverge by tens of dollars per window. The HTML page exposes
        the EXACT priceToBeat Polymarket already wrote, eliminating that skew
        without needing to subscribe to the underlying stream ourselves.
        """
        # ── PRIMARY: Polymarket HTML priceToBeat scrape ──
        if self._html_ptb_feed is not None:
            try:
                # Map window duration → timeframe label expected by URL.
                tf = "5m" if self._duration_secs == 300 else (
                    "15m" if self._duration_secs == 900 else f"{self._duration_secs // 60}m"
                )
                ptb_val = await self._html_ptb_feed.get_price_to_beat(
                    window.asset, tf, window.window_ts
                )
            except Exception as exc:
                self._log.warning("html_ptb.get_open_price_error", error=str(exc)[:200])
                ptb_val = None
            if ptb_val and ptb_val > 0:
                window.open_price = float(ptb_val)
                # Use canonical "polymarket_priceToBeat" so downstream
                # evaluate_strategies.py:571 picks it up into V5FeatureBody.
                # The HTML method is recorded in the from_polymarket_html log.
                window.open_price_source = "polymarket_priceToBeat"
                self._log.info(
                    "open_price.from_polymarket_html",
                    asset=window.asset,
                    window_ts=window.window_ts,
                    price=ptb_val,
                )
                self._log.info(
                    "live.open_price_fetched",
                    asset=window.asset,
                    price=window.open_price,
                    source="polymarket_priceToBeat",
                    method="html",
                )
                return

        # ── FALLBACK 1: Polymarket priceToBeat already captured (Gamma) ──
        if window.gamma_price_to_beat and window.gamma_price_to_beat > 0:
            window.open_price = float(window.gamma_price_to_beat)
            window.open_price_source = "polymarket_priceToBeat"
            self._log.info(
                "live.open_price_fetched",
                asset=window.asset,
                price=window.open_price,
                source="polymarket_priceToBeat",
            )
            return

        # ── FALLBACK 2: Chainlink Polygon (approximation; will be overridden
        #               by _fetch_live_data once eventMetadata publishes) ──
        if self._chainlink_feed:
            cl_prices = getattr(self._chainlink_feed, "latest_prices", {})
            cl_price = cl_prices.get(window.asset)
            if cl_price and cl_price > 0:
                window.open_price = float(cl_price)
                window.open_price_source = "chainlink_polygon_pending"
                self._log.info(
                    "live.open_price_fetched",
                    asset=window.asset,
                    price=window.open_price,
                    source="chainlink_polygon_pending",
                )
                return

        # ── FALLBACK: Binance REST ──
        self._log.warning(
            "live.open_price_chainlink_unavailable_falling_back_to_binance",
            asset=window.asset,
            chainlink_feed_present=self._chainlink_feed is not None,
        )
        asset_symbols = {
            "BTC": "BTCUSDT",
            "ETH": "ETHUSDT",
            "SOL": "SOLUSDT",
            "DOGE": "DOGEUSDT",
            "XRP": "XRPUSDT",
            "BNB": "BNBUSDT",
            "HYPE": "HYPEUSDT",
        }
        symbol = asset_symbols.get(window.asset, f"{window.asset}USDT")

        urls = [
            f"https://data-api.binance.vision/api/v3/ticker/price?symbol={symbol}",
            f"https://api1.binance.com/api/v3/ticker/price?symbol={symbol}",
            f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}",
            f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}",
        ]

        import aiohttp

        headers = {"User-Agent": "Mozilla/5.0 NovakashEngine/1.0"}
        for url in urls:
            try:
                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                        if isinstance(data, dict) and "price" in data:
                            window.open_price = float(data["price"])
                            window.open_price_source = "binance_fallback"
                            self._log.info(
                                "live.open_price_fetched",
                                asset=window.asset,
                                price=window.open_price,
                                source=f"binance_fallback_{url.split('/')[2]}",
                            )
                            return
            except Exception:
                continue

        self._log.warning("live.open_price_all_failed", asset=window.asset)

    async def _sync_open_price_from_prev_window_final(
        self, window: WindowInfo
    ) -> bool:
        """Fetch the previous window's `finalPrice` from Gamma and use it as the
        canonical open_price for `window`.

        Polymarket samples Chainlink Data Streams (off-chain low-latency feed at
        data.chain.link/streams/btc-usd) once at every 5-minute window boundary.
        That single sample serves as both `finalPrice[N-1]` and `priceToBeat[N]`.
        Verified empirically across 5 sequential windows (2026-05-01 22:00-22:25
        UTC) with 100% match.

        Polymarket publishes `eventMetadata.finalPrice` ~50-90s after window N-1
        closes, which is ~50-90s into window N. Calling this method during
        window N's ACTIVE state once the previous window has resolved yields
        the canonical reference price (matches the UI "Price To Beat" exactly,
        modulo float precision).

        Returns True if the open_price was successfully overridden from Gamma.
        """
        if window._gamma_metadata_synced:
            return True
        if not self._http_client:
            return False

        # ── PRIMARY PATH: HTML scrape for canonical priceToBeat ──
        # Tries the same source as _fetch_open_price's PRIMARY path. The HTML
        # feed has its own permanent cache so a successful first fetch is free
        # on subsequent calls. The reason we re-attempt HERE (in the timed
        # resync loop, fired at offsets 60/90/120/180/240/270s) is that the
        # past-results array on Polymarket's page only includes window N once
        # ~5-30s after N opens — the at-window-open call from _fetch_open_price
        # may race with that publication and miss it.
        if self._html_ptb_feed is not None:
            try:
                tf = "5m" if self._duration_secs == 300 else (
                    "15m" if self._duration_secs == 900 else f"{self._duration_secs // 60}m"
                )
                ptb_html = await self._html_ptb_feed.get_price_to_beat(
                    window.asset, tf, window.window_ts
                )
            except Exception as exc:
                self._log.warning(
                    "open_price.html_resync_failed",
                    window_ts=window.window_ts,
                    error=str(exc)[:200],
                )
                ptb_html = None
            if ptb_html and ptb_html > 0:
                prev_open = window.open_price
                prev_source = window.open_price_source
                window.gamma_price_to_beat = ptb_html  # share field for downstream
                window.open_price = float(ptb_html)
                # Use canonical "polymarket_priceToBeat" so evaluate_strategies.py
                # picks it up into V5FeatureBody.polymarket_price_to_beat.
                window.open_price_source = "polymarket_priceToBeat"
                window._gamma_metadata_synced = True
                self._log.info(
                    "open_price.priceToBeat_synced_from_html",
                    window_ts=window.window_ts,
                    asset=window.asset,
                    prev_open=prev_open,
                    prev_source=prev_source,
                    new_open=ptb_html,
                )
                return True

        # ── SECONDARY PATH: query CURRENT window's slug for priceToBeat ──
        # Polymarket publishes eventMetadata.priceToBeat[N] much faster than
        # eventMetadata.finalPrice[N-1] (the prev-window fallback below). Both
        # values are identical (same Chainlink Streams sample at window boundary)
        # but priceToBeat publishes within ~60-180s of window N opening, whereas
        # finalPrice is empirically >270s post-close in many cases.
        # PR #464's resync was failing silently because finalPrice wasn't there
        # by the [60,90,120,180,240,270] retry deadlines.
        cur_slug = (
            f"{window.asset.lower()}-updown-"
            f"{'15m' if window.duration_secs == 900 else '5m'}-{window.window_ts}"
        )
        try:
            resp = await self._http_client.get(
                "https://gamma-api.polymarket.com/events",
                params={"slug": cur_slug},
                timeout=5.0,
            )
            resp.raise_for_status()
            data = resp.json()
            if data and isinstance(data, list):
                cur_event = data[0]
                cur_meta = cur_event.get("eventMetadata") or {}
                if isinstance(cur_meta, dict):
                    ptb_raw = cur_meta.get("priceToBeat")
                    if ptb_raw is not None:
                        ptb_val = float(ptb_raw)
                        if ptb_val > 0:
                            prev_open = window.open_price
                            prev_source = window.open_price_source
                            window.gamma_price_to_beat = ptb_val
                            window.open_price = ptb_val
                            window.open_price_source = "polymarket_priceToBeat"
                            window._gamma_metadata_synced = True
                            self._log.info(
                                "open_price.priceToBeat_synced_from_current_window",
                                window_ts=window.window_ts,
                                asset=window.asset,
                                prev_open=prev_open,
                                prev_source=prev_source,
                                new_open=ptb_val,
                            )
                            return True
        except Exception as exc:
            self._log.warning(
                "open_price.current_window_sync_failed",
                window_ts=window.window_ts,
                slug=cur_slug,
                error=str(exc)[:200],
            )

        # ── FALLBACK: query PREV window's slug for finalPrice (legacy path) ──
        prev_ts = window.window_ts - window.duration_secs
        prev_slug = (
            f"{window.asset.lower()}-updown-"
            f"{'15m' if window.duration_secs == 900 else '5m'}-{prev_ts}"
        )
        try:
            resp = await self._http_client.get(
                "https://gamma-api.polymarket.com/events",
                params={"slug": prev_slug},
                timeout=5.0,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data or not isinstance(data, list):
                return False
            prev_event = data[0]
            prev_meta = prev_event.get("eventMetadata") or {}
            if not isinstance(prev_meta, dict):
                return False
            final_raw = prev_meta.get("finalPrice")
            if final_raw is None:
                return False
            final_val = float(final_raw)
            if final_val <= 0:
                return False

            prev_open = window.open_price
            prev_source = window.open_price_source
            window.gamma_price_to_beat = final_val
            window.open_price = final_val
            window.open_price_source = "polymarket_priceToBeat"
            window._gamma_metadata_synced = True

            if prev_open is not None and abs(prev_open - final_val) > 0.01:
                self._log.info(
                    "open_price.priceToBeat_synced_from_prev_window",
                    window_ts=window.window_ts,
                    asset=window.asset,
                    prev_open=prev_open,
                    prev_source=prev_source,
                    new_open=final_val,
                    delta=round(prev_open - final_val, 4),
                    prev_window_ts=prev_ts,
                )
            else:
                self._log.info(
                    "open_price.priceToBeat_synced_from_prev_window",
                    window_ts=window.window_ts,
                    asset=window.asset,
                    new_open=final_val,
                    prev_window_ts=prev_ts,
                )
            return True
        except Exception as exc:
            self._log.info(
                "open_price.prev_window_sync_failed",
                window_ts=window.window_ts,
                prev_slug=prev_slug,
                error=str(exc)[:200],
            )
            return False

    async def _fetch_paper_data(self, window: WindowInfo) -> None:
        """Set paper token IDs and open price. Preserves real Gamma prices if already fetched."""
        import random

        # Only set prices if Gamma API didn't provide them
        if window.up_price is None:
            base_price = 0.50
            noise = random.uniform(-0.02, 0.02)
            window.up_price = max(0.01, min(0.99, base_price + noise))
            window.down_price = 1.0 - window.up_price
            window.price_source = "synthetic"

        # Only set token IDs if Gamma API didn't provide them
        if window.up_token_id is None:
            window.up_token_id = f"paper-up-{window.asset}-{window.window_ts}"
        if window.down_token_id is None:
            window.down_token_id = f"paper-down-{window.asset}-{window.window_ts}"

        # Fetch open price: Chainlink oracle (primary) -> Binance REST (fallback)
        if window.open_price is None:
            # Try Chainlink first (oracle-aligned with Polymarket resolution)
            if self._chainlink_feed:
                cl_prices = getattr(self._chainlink_feed, "latest_prices", {})
                cl_price = cl_prices.get(window.asset)
                if cl_price and cl_price > 0:
                    window.open_price = float(cl_price)
                    self._log.debug(
                        "paper.open_price_from_chainlink",
                        asset=window.asset,
                        price=window.open_price,
                    )

        # Fallback to Binance REST if Chainlink unavailable
        asset_symbols = {
            "BTC": "BTCUSDT",
            "ETH": "ETHUSDT",
            "SOL": "SOLUSDT",
            "DOGE": "DOGEUSDT",
            "XRP": "XRPUSDT",
            "BNB": "BNBUSDT",
            "HYPE": "HYPEUSDT",
        }
        symbol = asset_symbols.get(window.asset, f"{window.asset}USDT")

        if window.open_price is None:
            try:
                import aiohttp

                async with aiohttp.ClientSession() as session:
                    url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        data = await resp.json()
                        window.open_price = float(data["price"])
                        self._log.debug(
                            "paper.open_price_from_binance_fallback",
                            asset=window.asset,
                            price=window.open_price,
                        )
            except Exception:
                # Fallback estimates
                fallbacks = {
                    "BTC": 68500,
                    "ETH": 1850,
                    "SOL": 130,
                    "DOGE": 0.17,
                    "XRP": 0.60,
                }
                window.open_price = fallbacks.get(window.asset, 100.0) + random.uniform(
                    -5, 5
                )

        self._log.debug(
            "paper.market_data",
            asset=window.asset,
            window_ts=window.window_ts,
            up_price=window.up_price,
            down_price=window.down_price,
            open_price=window.open_price,
        )

    def _build_slug(self, window: WindowInfo) -> str:
        """Build the market slug for the window."""
        tf = "15m" if window.duration_secs == 900 else "5m"
        return f"{window.asset.lower()}-updown-{tf}-{window.window_ts}"

    # ─── Utility Methods ──────────────────────────────────────────────────────

    def get_next_window_ts(self, asset: str = "BTC") -> int:
        """Get the next window timestamp."""
        now = time.time()
        current_ts = self._calculate_window_ts(now, asset)
        return current_ts + self._duration_secs

    def get_time_until_signal(self, asset: str = "BTC") -> Optional[float]:
        """Get seconds until the T-10s signal for current window."""
        window = self.get_current_window(asset)
        if not window:
            return None

        now = time.time()
        elapsed = int(now) - window.window_ts
        signal_at = window.duration_secs - self._signal_offset

        remaining = signal_at - elapsed
        return max(0.0, remaining)

    def get_time_until_close(self, asset: str = "BTC") -> Optional[float]:
        """Get seconds until window close."""
        window = self.get_current_window(asset)
        if not window:
            return None

        now = time.time()
        elapsed = int(now) - window.window_ts
        return max(0.0, window.duration_secs - elapsed)
