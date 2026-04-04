"""
v6.0 — TimesFM-Only Strategy

Uses TimesFM forecast as the SOLE indicator for trade direction.
No VPIN gating, no CoinGlass modifiers, no delta thresholds.
Pure ML forecast → direction → trade.

PURPOSE: Overnight paper test to measure TimesFM directional accuracy
in isolation, so we can compare it against the existing v5.7c
multi-indicator strategy.

Decision logic:
  1. Get TimesFM forecast (direction + confidence)
  2. If confidence >= min_confidence → trade in that direction
  3. Record everything: TimesFM prediction, actual outcome, TWAP/Gamma/point
     data (for comparison, NOT for decision-making)

Spread/Liquidity tracking:
  - Fetches real Gamma API orderbook data at evaluation time
  - Records best bid/ask, spread, and mid price
  - Paper fills use REAL mid price (not fixed 65¢)
  - This gives us honest fill price estimates
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import structlog

from config.constants import FIVE_MIN_ENTRY_OFFSET
from config.runtime_config import runtime
from data.models import MarketState
from data.feeds.polymarket_5min import WindowInfo
from execution.order_manager import Order, OrderManager, OrderStatus
from execution.polymarket_client import PolymarketClient
from execution.risk_manager import RiskManager
from signals.timesfm_client import TimesFMClient, TimesFMForecast
from signals.twap_delta import TWAPTracker, TWAPResult
from strategies.base import BaseStrategy

log = structlog.get_logger(__name__)


@dataclass
class TimesFMSignal:
    """Signal produced by the TimesFM-only strategy."""
    window: WindowInfo
    direction: str          # "UP" or "DOWN"
    confidence: float       # TimesFM confidence (0-1)
    predicted_close: float  # TimesFM predicted price
    delta_vs_open_pct: float  # TimesFM predicted delta from window open
    spread: float           # TimesFM quantile spread (uncertainty)
    current_price: float    # Actual BTC price at eval time

    # Comparison data (logged, NOT used for decision)
    twap_direction: str = ""
    point_direction: str = ""
    gamma_direction: str = ""
    agreement_with_twap: bool = False
    agreement_with_point: bool = False
    agreement_with_gamma: bool = False

    # Spread/liquidity data
    market_best_bid: float = 0.0
    market_best_ask: float = 0.0
    market_spread: float = 0.0
    market_mid_price: float = 0.0


class TimesFMOnlyStrategy(BaseStrategy):
    """
    v6.0: TimesFM forecast as sole trade indicator.

    Paper-only overnight test to measure:
    1. TimesFM directional accuracy (UP/DOWN correct rate)
    2. How it compares to TWAP, point delta, Gamma consensus
    3. Real spread/liquidity on Polymarket UpDown tokens
    """

    def __init__(
        self,
        order_manager: OrderManager,
        risk_manager: RiskManager,
        poly_client: PolymarketClient,
        timesfm_client: TimesFMClient,
        alerter=None,
        db_client=None,
        twap_tracker: Optional[TWAPTracker] = None,
        min_confidence: float = 0.30,
    ) -> None:
        super().__init__(
            name="timesfm_only_v6",
            order_manager=order_manager,
            risk_manager=risk_manager,
        )
        self._poly = poly_client
        self._timesfm = timesfm_client
        self._alerter = alerter
        self._db = db_client
        self._twap = twap_tracker
        self._min_confidence = min_confidence

        self._last_executed_window: Optional[str] = None
        self._recent_windows: list = []
        self._pending_windows: list = []

        self._log = log.bind(strategy="timesfm_only_v6")

    async def start(self) -> None:
        """Start the strategy."""
        self._running = True

        # Health check TimesFM service
        health = await self._timesfm.health_check()
        if health.get("status") == "ok":
            self._log.info(
                "strategy.started",
                timesfm_status="ok",
                model_loaded=health.get("model_loaded"),
                buffer_size=health.get("buffer_size"),
            )
        else:
            self._log.warning(
                "strategy.started_without_timesfm",
                timesfm_status=health,
            )

    async def stop(self) -> None:
        self._running = False
        self._log.info("strategy.stopped")

    async def on_market_state(self, state: MarketState) -> None:
        """Called on every market state update. v6.0 doesn't use continuous eval."""
        if not self._running:
            return
        # Just register windows for token ID lookup
        if self._pending_windows:
            self._pending_windows.clear()

    async def evaluate_window(self, window: WindowInfo, state: MarketState) -> None:
        """
        Evaluate a window using ONLY TimesFM forecast.

        Called by orchestrator at T-60s (CLOSING state).
        """
        window_key = f"{window.asset}-{window.window_ts}"
        if self._last_executed_window == window_key:
            return

        # Get current price
        if window.asset == "BTC":
            current_price = float(state.btc_price) if state.btc_price else None
        else:
            current_price = await self._fetch_current_price(window.asset)

        if current_price is None or window.open_price is None:
            self._log.warning("evaluate.no_price", asset=window.asset)
            return

        open_price = window.open_price
        point_delta_pct = (current_price - open_price) / open_price * 100

        # ── 1. GET TIMESFM FORECAST (THE SOLE INDICATOR) ─────────────
        forecast = await self._timesfm.get_forecast(open_price=open_price)

        if forecast.error:
            self._log.warning(
                "evaluate.timesfm_error",
                asset=window.asset,
                error=forecast.error,
            )
            # Send alert about the error, but don't trade
            if self._alerter:
                await self._send_window_report(
                    window, open_price, current_price, point_delta_pct,
                    forecast=forecast, twap_result=None, signal=None,
                    skip_reason=f"TimesFM error: {forecast.error}",
                    spread_data=None,
                )
            return

        # ── 2. GET COMPARISON DATA (for analysis, NOT decision-making) ───
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
                self._twap.cleanup_window(window.asset, window.window_ts)

        # ── 3. GET REAL SPREAD/LIQUIDITY DATA ────────────────────────
        spread_data = await self._fetch_spread_data(window)

        # ── 4. DECISION: TimesFM confidence gate (ONLY gate) ─────────
        timesfm_direction = forecast.direction  # "UP" or "DOWN"
        timesfm_confidence = forecast.confidence

        # Comparison flags (logged, not used for decision)
        point_direction = "UP" if point_delta_pct > 0 else "DOWN"
        twap_direction = twap_result.twap_direction if twap_result else ""
        gamma_direction = twap_result.gamma_direction if twap_result else ""

        if timesfm_confidence < self._min_confidence:
            self._log.info(
                "evaluate.skip_low_confidence",
                asset=window.asset,
                timesfm_direction=timesfm_direction,
                timesfm_confidence=f"{timesfm_confidence:.2f}",
                min_confidence=f"{self._min_confidence:.2f}",
            )
            if self._alerter:
                await self._send_window_report(
                    window, open_price, current_price, point_delta_pct,
                    forecast=forecast, twap_result=twap_result, signal=None,
                    skip_reason=f"TimesFM confidence {timesfm_confidence:.2f} < {self._min_confidence:.2f}",
                    spread_data=spread_data,
                )
            await self._write_snapshot(
                window, open_price, current_price, point_delta_pct,
                forecast, twap_result, spread_data, trade_placed=False,
                skip_reason=f"Low confidence: {timesfm_confidence:.2f}",
            )
            return

        # ── 5. BUILD SIGNAL ──────────────────────────────────────────
        signal = TimesFMSignal(
            window=window,
            direction=timesfm_direction,
            confidence=timesfm_confidence,
            predicted_close=forecast.predicted_close,
            delta_vs_open_pct=forecast.delta_vs_open_pct,
            spread=forecast.spread,
            current_price=current_price,
            twap_direction=twap_direction,
            point_direction=point_direction,
            gamma_direction=gamma_direction,
            agreement_with_twap=timesfm_direction == twap_direction if twap_direction else False,
            agreement_with_point=timesfm_direction == point_direction,
            agreement_with_gamma=timesfm_direction == gamma_direction if gamma_direction else False,
            market_best_bid=spread_data.get("best_bid", 0) if spread_data else 0,
            market_best_ask=spread_data.get("best_ask", 0) if spread_data else 0,
            market_spread=spread_data.get("spread", 0) if spread_data else 0,
            market_mid_price=spread_data.get("mid_price", 0) if spread_data else 0,
        )

        self._log.info(
            "evaluate.timesfm_signal",
            asset=window.asset,
            direction=signal.direction,
            confidence=f"{signal.confidence:.2f}",
            predicted_close=f"${signal.predicted_close:,.2f}",
            agrees_twap=signal.agreement_with_twap,
            agrees_point=signal.agreement_with_point,
            agrees_gamma=signal.agreement_with_gamma,
            market_spread=f"{signal.market_spread:.4f}" if signal.market_spread else "n/a",
        )

        # ── 6. EXECUTE TRADE ─────────────────────────────────────────
        await self._execute_trade(state, signal, spread_data)

        # ── 7. SEND ALERTS + DB SNAPSHOT ─────────────────────────────
        if self._alerter:
            await self._send_window_report(
                window, open_price, current_price, point_delta_pct,
                forecast=forecast, twap_result=twap_result, signal=signal,
                skip_reason=None, spread_data=spread_data,
            )

        await self._write_snapshot(
            window, open_price, current_price, point_delta_pct,
            forecast, twap_result, spread_data, trade_placed=True,
        )

        self._last_executed_window = window_key

    async def _execute_trade(
        self, state: MarketState, signal: TimesFMSignal, spread_data: Optional[dict]
    ) -> None:
        """Execute a paper trade based on TimesFM signal."""
        window = signal.window

        # Direction → token
        if signal.direction == "UP":
            direction = "YES"
            token_id = window.up_token_id
        else:
            direction = "NO"
            token_id = window.down_token_id

        if token_id is None:
            # Try recent windows for token IDs
            for w in reversed(self._recent_windows):
                if w.asset == window.asset:
                    tid = w.up_token_id if direction == "YES" else w.down_token_id
                    if tid:
                        token_id = tid
                        break

        if token_id is None:
            self._log.warning("execute.no_token_id", direction=signal.direction)
            return

        # ── REALISTIC FILL PRICE ──────────────────────────────────────
        # Use REAL market mid price instead of fixed 65¢
        # This is the key improvement for honest paper testing
        if spread_data and spread_data.get("mid_price", 0) > 0:
            # Use actual mid price from the orderbook
            fill_price = spread_data["mid_price"]
            if direction == "NO":
                fill_price = 1.0 - fill_price  # Complement for NO token
            price_source = "market_mid"
        elif direction == "YES" and window.up_price:
            fill_price = window.up_price
            price_source = "gamma_up"
        elif direction == "NO" and window.down_price:
            fill_price = window.down_price
            price_source = "gamma_down"
        else:
            fill_price = 0.50
            price_source = "fallback_50c"

        # Cap at 65¢ for 5m, 70¢ for 15m (same as v5.7c)
        tf = "15m" if window.duration_secs == 900 else "5m"
        max_price = 0.70 if tf == "15m" else 0.65

        if fill_price > max_price:
            self._log.info(
                "execute.price_above_cap",
                fill_price=f"${fill_price:.4f}",
                cap=f"${max_price:.2f}",
                source=price_source,
            )
            return

        if fill_price < 0.20:
            fill_price = 0.50  # Safety floor
            price_source = "floor_50c"

        price = Decimal(str(round(fill_price, 4)))

        # Calculate stake
        stake = self._calculate_stake(signal.confidence, float(price))

        # Risk check
        approved, reason = await self._check_risk(stake)
        if not approved:
            self._log.info("execute.risk_blocked", reason=reason)
            return

        # Place order (paper mode)
        market_slug = f"{window.asset.lower()}-updown-{tf}-{window.window_ts}"

        try:
            clob_order_id = await self._poly.place_order(
                market_slug=market_slug,
                direction=direction,
                price=price,
                stake_usd=stake,
                token_id=token_id,
            )
        except Exception as exc:
            self._log.error("execute.order_failed", error=str(exc))
            return

        order_id = clob_order_id if not self._poly.paper_mode else f"v6-{uuid.uuid4().hex[:12]}"

        fee_mult = 0.072
        fee_usd = fee_mult * float(price) * (1.0 - float(price)) * stake

        order = Order(
            order_id=order_id,
            strategy=self.name,
            venue="polymarket",
            direction=direction,
            price=str(price),
            stake_usd=stake,
            fee_usd=fee_usd,
            status=OrderStatus.OPEN,
            btc_entry_price=signal.current_price,
            window_seconds=window.duration_secs,
            market_id=market_slug,
            metadata={
                "window_ts": window.window_ts,
                "window_open_price": window.open_price,
                "strategy_version": "v6.0_timesfm_only",
                "timesfm_direction": signal.direction,
                "timesfm_confidence": signal.confidence,
                "timesfm_predicted_close": signal.predicted_close,
                "timesfm_delta_vs_open": signal.delta_vs_open_pct,
                "timesfm_spread": signal.spread,
                "point_delta_pct": (signal.current_price - window.open_price) / window.open_price * 100,
                "twap_direction": signal.twap_direction,
                "point_direction": signal.point_direction,
                "gamma_direction": signal.gamma_direction,
                "agrees_twap": signal.agreement_with_twap,
                "agrees_point": signal.agreement_with_point,
                "agrees_gamma": signal.agreement_with_gamma,
                "fill_price": float(price),
                "fill_price_source": price_source,
                "market_spread": signal.market_spread,
                "market_best_bid": signal.market_best_bid,
                "market_best_ask": signal.market_best_ask,
                "token_id": token_id,
                "timeframe": tf,
                "market_slug": market_slug,
            },
        )

        await self._om.register_order(order)

        self._log.info(
            "trade.executed",
            order_id=order.order_id[:20],
            direction=direction,
            confidence=f"{signal.confidence:.2f}",
            price=str(price),
            price_source=price_source,
            stake=f"${stake:.2f}",
            predicted_close=f"${signal.predicted_close:,.2f}",
        )

        if self._alerter:
            try:
                asyncio.create_task(self._alerter.send_entry_alert(order))
            except Exception:
                pass

    def _calculate_stake(self, confidence: float, token_price: float = 0.50) -> float:
        """Calculate stake for TimesFM strategy. Same scaling as v5.7c."""
        status = self._rm.get_status()
        bankroll = status["current_bankroll"]
        base_stake = bankroll * runtime.bet_fraction

        tp = max(0.30, min(0.65, token_price))
        price_multiplier = (1.0 - tp) / 0.50
        price_multiplier = max(0.5, min(1.5, price_multiplier))

        adjusted_stake = base_stake * price_multiplier
        max_stake = bankroll * runtime.bet_fraction
        adjusted_stake = min(adjusted_stake, max_stake * 0.95)

        hard_max = runtime.max_position_usd
        if adjusted_stake > hard_max:
            adjusted_stake = hard_max * 0.95

        return round(adjusted_stake, 2)

    async def _fetch_spread_data(self, window: WindowInfo) -> Optional[dict]:
        """
        Fetch REAL orderbook spread/liquidity from Gamma API.

        Returns dict with best_bid, best_ask, spread, mid_price, depth
        or None if unavailable.
        """
        try:
            import aiohttp as _aiohttp

            tf = "15m" if window.duration_secs == 900 else "5m"
            slug = f"{window.asset.lower()}-updown-{tf}-{window.window_ts}"

            async with _aiohttp.ClientSession(
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=_aiohttp.ClientTimeout(total=5),
            ) as session:
                url = f"https://gamma-api.polymarket.com/events?slug={slug}"
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return None

                    data = await resp.json()
                    if not data or not isinstance(data, list) or not data[0].get("markets"):
                        return None

                    mkt = data[0]["markets"][0]
                    best_bid = float(mkt.get("bestBid", 0))
                    best_ask = float(mkt.get("bestAsk", 0))

                    if best_bid > 0 and best_ask > 0:
                        spread = best_ask - best_bid
                        mid_price = (best_bid + best_ask) / 2
                    elif best_ask > 0:
                        spread = 0.0
                        mid_price = best_ask
                    else:
                        return None

                    # Try to get orderbook depth
                    volume = float(mkt.get("volume", 0))
                    liquidity = float(mkt.get("liquidityNum", 0))

                    return {
                        "best_bid": best_bid,
                        "best_ask": best_ask,
                        "spread": round(spread, 6),
                        "mid_price": round(mid_price, 4),
                        "volume": volume,
                        "liquidity": liquidity,
                        "slug": slug,
                    }

        except Exception as exc:
            self._log.debug("spread_data.fetch_failed", error=str(exc))
            return None

    async def _fetch_current_price(self, asset: str) -> Optional[float]:
        """Fetch current spot price from Binance for non-BTC assets."""
        symbols = {
            "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
            "XRP": "XRPUSDT", "DOGE": "DOGEUSDT",
        }
        symbol = symbols.get(asset.upper())
        if not symbol:
            return None
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    data = await resp.json()
                    return float(data["price"])
        except Exception:
            return None

    async def _send_window_report(
        self,
        window: WindowInfo,
        open_price: float,
        close_price: float,
        point_delta_pct: float,
        forecast: TimesFMForecast,
        twap_result: Optional[TWAPResult],
        signal: Optional[TimesFMSignal],
        skip_reason: Optional[str],
        spread_data: Optional[dict],
    ) -> None:
        """Send a v6.0 Telegram window report with TimesFM analysis."""
        try:
            await self._alerter.send_timesfm_window_report(
                window_ts=window.window_ts,
                asset=window.asset,
                timeframe="15m" if window.duration_secs == 900 else "5m",
                open_price=open_price,
                close_price=close_price,
                delta_pct=point_delta_pct,
                forecast=forecast,
                twap_result=twap_result,
                trade_placed=signal is not None,
                skip_reason=skip_reason,
                spread_data=spread_data,
            )
        except Exception as exc:
            self._log.warning("window_report.send_failed", error=str(exc))

    async def _write_snapshot(
        self,
        window: WindowInfo,
        open_price: float,
        close_price: float,
        point_delta_pct: float,
        forecast: TimesFMForecast,
        twap_result: Optional[TWAPResult],
        spread_data: Optional[dict],
        trade_placed: bool,
        skip_reason: str = "",
    ) -> None:
        """Write window snapshot to DB for later analysis."""
        if self._db is None:
            return

        tf = "15m" if window.duration_secs == 900 else "5m"

        snapshot = {
            "window_ts": window.window_ts,
            "asset": window.asset,
            "timeframe": tf,
            "open_price": open_price,
            "close_price": close_price,
            "delta_pct": point_delta_pct,
            "vpin": 0.0,  # v6.0 doesn't use VPIN
            "regime": "TIMESFM_ONLY",
            "btc_price": close_price,
            # TimesFM data
            "timesfm_direction": forecast.direction,
            "timesfm_confidence": forecast.confidence,
            "timesfm_predicted_close": forecast.predicted_close,
            "timesfm_delta_vs_open": forecast.delta_vs_open_pct,
            "timesfm_spread": forecast.spread,
            "timesfm_p10": forecast.p10,
            "timesfm_p50": forecast.p50,
            "timesfm_p90": forecast.p90,
            # TWAP comparison data
            "twap_delta_pct": twap_result.twap_delta_pct if twap_result else None,
            "twap_direction": twap_result.twap_direction if twap_result else None,
            "twap_gamma_agree": twap_result.twap_gamma_agree if twap_result else None,
            "twap_agreement_score": twap_result.agreement_score if twap_result else None,
            "twap_confidence_boost": twap_result.confidence_boost if twap_result else None,
            "twap_n_ticks": twap_result.n_ticks if twap_result else None,
            "twap_stability": twap_result.twap_stability if twap_result else None,
            # Spread/liquidity
            "market_best_bid": spread_data.get("best_bid") if spread_data else None,
            "market_best_ask": spread_data.get("best_ask") if spread_data else None,
            "market_spread": spread_data.get("spread") if spread_data else None,
            "market_mid_price": spread_data.get("mid_price") if spread_data else None,
            "market_volume": spread_data.get("volume") if spread_data else None,
            "market_liquidity": spread_data.get("liquidity") if spread_data else None,
            # Signal
            "direction": forecast.direction if forecast.direction else ("UP" if point_delta_pct > 0 else "DOWN"),
            "confidence": forecast.confidence,
            "trade_placed": trade_placed,
            "skip_reason": skip_reason if not trade_placed else None,
            # Comparison flags
            "cg_connected": False,
            "cg_modifier": 0.0,
        }

        try:
            asyncio.create_task(self._db.write_window_snapshot(snapshot))
        except Exception:
            pass

    # ─── Base interface ───────────────────────────────────────────────────

    async def evaluate(self, state: MarketState) -> Optional[dict]:
        return None

    async def execute(self, state: MarketState, signal: dict) -> Optional[Order]:
        return None
