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
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Awaitable, Optional, Dict, List
import httpx
import structlog

log = structlog.get_logger(__name__)


class WindowState(Enum):
    """Lifecycle state for a 5-minute trading window."""
    WAITING = "WAITING"       # Window not yet active
    ACTIVE = "ACTIVE"         # Window is live, trading allowed
    CLOSING = "CLOSING"       # T-10s, signal to evaluate
    RESOLVED = "RESOLVED"     # Window closed and resolved


@dataclass
class WindowInfo:
    """Information about a 5-minute trading window."""
    window_ts: int                    # Unix timestamp of window start
    asset: str                         # e.g. "BTC", "ETH"
    duration_secs: int                 # 300 for 5m, 900 for 15m
    state: WindowState = WindowState.WAITING
    open_price: Optional[float] = None # Opening price (Chainlink oracle)
    current_price: Optional[float] = None  # Current price
    up_token_id: Optional[str] = None  # "Up" outcome token ID
    down_token_id: Optional[str] = None  # "Down" outcome token ID
    up_price: Optional[float] = None   # Current Up token price
    down_price: Optional[float] = None  # Current Down token price


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
        on_window_signal: Optional[Callable[[WindowInfo], Awaitable[None]]] = None,
        on_window_state_change: Optional[Callable[[str, WindowState, WindowState], Awaitable[None]]] = None,
        paper_mode: bool = True,
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
        """
        self._assets = assets or ["BTC"]
        self._duration_secs = duration_secs
        self._signal_offset = signal_offset
        self._on_window_signal = on_window_signal
        self._on_window_state_change = on_window_state_change
        self._paper_mode = paper_mode
        
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
                await self._emit_state_change(window.asset, window.window_ts, window.state)
        
        elif window.state == WindowState.ACTIVE:
            # Check for T-10s signal
            if remaining <= self._signal_offset and remaining > 0:
                window.state = WindowState.CLOSING
                await self._emit_state_change(window.asset, window.window_ts, window.state)
                await self._emit_window_signal(window)
            
            # Window expired
            elif remaining <= 0:
                window.state = WindowState.RESOLVED
                await self._emit_state_change(window.asset, window.window_ts, window.state)

    async def _emit_state_change(self, asset: str, window_ts: int, new_state: WindowState) -> None:
        """Emit window state change callback."""
        if self._on_window_state_change:
            try:
                await self._on_window_state_change(f"{asset}-{window_ts}", new_state)
            except Exception as exc:
                self._log.error("state_change_callback_error", error=str(exc))

    async def _emit_window_signal(self, window: WindowInfo) -> None:
        """Emit T-10s signal to strategy."""
        if self._on_window_signal:
            try:
                await self._on_window_signal(window)
                self._log.info(
                    "window.signal",
                    asset=window.asset,
                    window_ts=window.window_ts,
                    open_price=window.open_price,
                    up_price=window.up_price,
                    down_price=window.down_price,
                )
            except Exception as exc:
                self._log.error("window_signal_callback_error", error=str(exc))

    # ─── Market Data Fetching ─────────────────────────────────────────────────

    async def _fetch_market_data(self, window: WindowInfo) -> None:
        """Fetch market data from Polymarket Gamma API."""
        if self._paper_mode:
            await self._fetch_paper_data(window)
            return
        
        try:
            await self._fetch_live_data(window)
        except Exception as exc:
            self._log.error("market_fetch_failed", error=str(exc))
            # Fall back to paper data
            await self._fetch_paper_data(window)

    async def _fetch_live_data(self, window: WindowInfo) -> None:
        """Fetch live market data from Gamma API."""
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
            
            # Extract market info
            window.up_token_id = event.get("up_token_id")
            window.down_token_id = event.get("down_token_id")
            
            # Extract prices (these would come from order book)
            # For now, we'll use a simple approximation
            self._log.info("market.fetched", slug=slug, data_keys=list(event.keys())[:5])
            
        except Exception as exc:
            self._log.error("gamma_api_error", error=str(exc))

    async def _fetch_paper_data(self, window: WindowInfo) -> None:
        """Generate paper market data for testing."""
        import random
        
        # Simulate a random open price (±5% around 50/50)
        base_price = 0.50
        noise = random.uniform(-0.05, 0.05)
        
        window.up_price = max(0.01, min(0.99, base_price + noise))
        window.down_price = 1.0 - window.up_price
        
        # Generate fake token IDs
        window.up_token_id = f"paper-up-{window.asset}-{window.window_ts}"
        window.down_token_id = f"paper-down-{window.asset}-{window.window_ts}"
        
        # Simulate open price (for delta calculation)
        # In reality, this would come from the oracle at window open
        window.open_price = 45000.0 + random.uniform(-500, 500)
        
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
        return f"{window.asset.lower()}-updown-5m-{window.window_ts}"

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
