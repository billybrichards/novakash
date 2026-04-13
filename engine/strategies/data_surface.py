"""Data Surface Layer for Strategy Engine v2.

Assembles FullDataSurface from in-memory caches (zero I/O at decision time).
Background loop pre-fetches V4 snapshot every 2s using persistent HTTP session.

Audit: CA-08.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass(frozen=True)
class FullDataSurface:
    """Complete data surface available to all gates and strategies.

    Assembled by DataSurfaceManager. Read-only at decision time.
    All fields populated from in-memory caches -- zero I/O.
    """

    # Identity
    asset: str
    timescale: str
    window_ts: int
    eval_offset: Optional[int]
    assembled_at: float  # Unix epoch -- staleness check

    # Price (Binance WS, <100ms fresh)
    current_price: float
    open_price: float

    # Deltas (in-memory from feeds, <2s fresh)
    delta_binance: Optional[float]
    delta_tiingo: Optional[float]
    delta_chainlink: Optional[float]
    delta_pct: float  # Primary (source-selected)
    delta_source: str

    # VPIN + Regime (in-memory, <1s fresh)
    vpin: float
    regime: str  # CALM | NORMAL | TRANSITION | CASCADE

    # TWAP
    twap_delta: Optional[float]

    # V2 Predictions (from V4 snapshot cache, <5s fresh)
    v2_probability_up: Optional[float]
    v2_probability_raw: Optional[float]
    v2_quantiles_p10: Optional[float]
    v2_quantiles_p50: Optional[float]
    v2_quantiles_p90: Optional[float]

    # V3 Multi-Horizon Composites (9 timescales)
    v3_5m_composite: Optional[float]
    v3_15m_composite: Optional[float]
    v3_1h_composite: Optional[float]
    v3_4h_composite: Optional[float]
    v3_24h_composite: Optional[float]
    v3_48h_composite: Optional[float]
    v3_72h_composite: Optional[float]
    v3_1w_composite: Optional[float]
    v3_2w_composite: Optional[float]

    # V3 Sub-Signals
    v3_sub_elm: Optional[float]
    v3_sub_cascade: Optional[float]
    v3_sub_taker: Optional[float]
    v3_sub_oi: Optional[float]
    v3_sub_funding: Optional[float]
    v3_sub_vpin: Optional[float]
    v3_sub_momentum: Optional[float]

    # V4 Regime / HMM
    v4_regime: Optional[str]  # calm_trend | volatile_trend | chop | risk_off
    v4_regime_confidence: Optional[float]
    v4_regime_persistence: Optional[float]

    # V4 Macro
    v4_macro_bias: Optional[str]  # BULL | BEAR | NEUTRAL
    v4_macro_direction_gate: Optional[str]  # ALLOW_ALL | LONG_ONLY | SHORT_ONLY
    v4_macro_size_modifier: Optional[float]

    # V4 Consensus
    v4_consensus_safe_to_trade: Optional[bool]
    v4_consensus_agreement_score: Optional[float]
    v4_consensus_max_divergence_bps: Optional[float]

    # V4 Conviction
    v4_conviction: Optional[str]  # NONE | LOW | MEDIUM | HIGH
    v4_conviction_score: Optional[float]

    # V4 Polymarket Outcome (from timesfm-repo)
    poly_direction: Optional[str]  # UP | DOWN
    poly_trade_advised: Optional[bool]
    poly_confidence: Optional[float]
    poly_confidence_distance: Optional[float]
    poly_timing: Optional[str]  # early | optimal | late_window | expired
    poly_max_entry_price: Optional[float]
    poly_reason: Optional[str]

    # V4 Recommended Action
    v4_recommended_side: Optional[str]
    v4_recommended_collateral_pct: Optional[float]

    # V4 Sub-Signals
    v4_sub_signals: Optional[dict]

    # V4 Quantiles
    v4_quantiles: Optional[dict]

    # CLOB (in-memory from CLOBFeed, <2s fresh)
    clob_up_bid: Optional[float]
    clob_up_ask: Optional[float]
    clob_down_bid: Optional[float]
    clob_down_ask: Optional[float]
    clob_implied_up: Optional[float]

    # Gamma (refreshed per window)
    gamma_up_price: Optional[float]
    gamma_down_price: Optional[float]

    # CoinGlass (in-memory snapshot, <10s fresh)
    cg_oi_usd: Optional[float]
    cg_funding_rate: Optional[float]
    cg_taker_buy_vol: Optional[float]
    cg_taker_sell_vol: Optional[float]
    cg_liq_total: Optional[float]
    cg_liq_long: Optional[float]
    cg_liq_short: Optional[float]
    cg_long_short_ratio: Optional[float]

    # TimesFM Quantiles (from V4 snapshot, <5s fresh)
    timesfm_expected_move_bps: Optional[float]
    timesfm_vol_forecast_bps: Optional[float]

    # Window metadata
    hour_utc: Optional[int]
    seconds_to_close: Optional[int]


class DataSurfaceManager:
    """Keeps FullDataSurface fresh in memory. No blocking I/O at decision time.

    Background loop fetches V4 snapshot every 2s using a persistent
    aiohttp.ClientSession. Feed caches (Tiingo, Chainlink, CLOB) are
    read directly from the feed objects' in-memory attributes.
    """

    def __init__(
        self,
        *,
        v4_base_url: Optional[str] = None,
        tiingo_feed: Any = None,
        chainlink_feed: Any = None,
        clob_feed: Any = None,
        vpin_calculator: Any = None,
        cg_feeds: Optional[dict] = None,
        twap_tracker: Any = None,
        binance_state: Any = None,
    ):
        self._v4_url = v4_base_url or os.environ.get(
            "TIMESFM_URL", "http://localhost:8001"
        )
        self._tiingo = tiingo_feed
        self._chainlink = chainlink_feed
        self._clob = clob_feed
        self._vpin = vpin_calculator
        self._cg_feeds = cg_feeds or {}
        self._twap = twap_tracker
        self._binance_state = binance_state

        self._session = None  # aiohttp.ClientSession -- persistent
        self._cached_v4: Optional[dict] = None
        self._cached_v4_ts: float = 0.0
        self._refresh_interval = 2.0
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def set_feeds(
        self,
        *,
        tiingo_feed: Any = None,
        chainlink_feed: Any = None,
        clob_feed: Any = None,
        vpin_calculator: Any = None,
        cg_feeds: Optional[dict] = None,
        twap_tracker: Any = None,
        binance_state: Any = None,
    ) -> None:
        """Inject feed references after they are initialized.

        Called from orchestrator.start() where feeds are live, not __init__
        where they are still None.
        """
        if tiingo_feed is not None:
            self._tiingo = tiingo_feed
        if chainlink_feed is not None:
            self._chainlink = chainlink_feed
        if clob_feed is not None:
            self._clob = clob_feed
        if vpin_calculator is not None:
            self._vpin = vpin_calculator
        if cg_feeds is not None:
            self._cg_feeds = cg_feeds
        if twap_tracker is not None:
            self._twap = twap_tracker
        if binance_state is not None:
            self._binance_state = binance_state

    async def start(self) -> None:
        """Start background V4 pre-fetch loop."""
        import aiohttp

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        )
        self._running = True
        self._task = asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        """Stop background loop and close HTTP session."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()
            self._session = None

    async def _refresh_loop(self) -> None:
        """Fetch V4 snapshot every 2s using persistent HTTP session."""
        while self._running:
            try:
                await self._fetch_v4()
            except Exception:
                pass  # Non-fatal -- stale cache is better than no cache
            await asyncio.sleep(self._refresh_interval)

    async def _fetch_v4(self) -> None:
        """Fetch V4 snapshot and cache it in memory."""
        if not self._session:
            return
        url = f"{self._v4_url}/v4/snapshot"
        params = {"asset": "BTC", "timescale": "5m", "strategy": "polymarket_5m"}
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status == 200:
                    self._cached_v4 = await resp.json()
                    self._cached_v4_ts = time.time()
        except Exception:
            pass  # Keep stale cache

    def get_surface(
        self,
        window: Any,
        eval_offset: Optional[int],
    ) -> FullDataSurface:
        """Build FullDataSurface from cached data. ZERO I/O. <1ms.

        All reads from in-memory caches -- no DB queries, no HTTP calls.
        """
        asset = getattr(window, "asset", "BTC")
        window_ts = getattr(window, "window_ts", 0)
        open_price = getattr(window, "open_price", 0.0) or 0.0
        now = time.time()

        # Current price from Binance state
        btc_price = 0.0
        if self._binance_state:
            # Supports MarketAggregator (has get_state()) and MarketState directly
            _state_obj = self._binance_state
            if hasattr(self._binance_state, "get_state"):
                try:
                    _state_obj = self._binance_state.get_state()
                except Exception:
                    pass
            btc_price = float(getattr(_state_obj, "btc_price", 0) or 0)

        # Deltas from feed in-memory caches
        delta_tiingo = None
        delta_chainlink = None
        delta_binance = None
        delta_pct = 0.0
        delta_source = "unknown"

        if self._tiingo and open_price:
            ti_prices = getattr(self._tiingo, "latest_prices", {})
            ti_price = ti_prices.get(asset)
            if ti_price:
                delta_tiingo = (ti_price - open_price) / open_price

        if self._chainlink and open_price:
            cl_prices = getattr(self._chainlink, "latest_prices", {})
            cl_price = cl_prices.get(asset)
            if cl_price:
                delta_chainlink = (cl_price - open_price) / open_price

        if btc_price and open_price:
            delta_binance = (btc_price - open_price) / open_price

        # Select primary delta (priority: tiingo > chainlink > binance)
        for src, val in [
            ("tiingo_rest_candle", delta_tiingo),
            ("chainlink", delta_chainlink),
            ("binance", delta_binance),
        ]:
            if val is not None:
                delta_pct = val
                delta_source = src
                break

        # VPIN + Regime
        vpin_val = 0.0
        regime = "UNKNOWN"
        if self._vpin:
            vpin_val = getattr(self._vpin, "current_vpin", 0.0) or 0.0
            regime = getattr(self._vpin, "regime", "UNKNOWN") or "UNKNOWN"

        # TWAP
        twap_delta = None
        if self._twap:
            try:
                twap_result = self._twap.get_result(asset, window_ts)
                if twap_result:
                    twap_delta = getattr(twap_result, "delta_pct", None)
            except Exception:
                pass

        # CoinGlass
        cg = None
        cg_feed = self._cg_feeds.get(asset)
        if cg_feed:
            cg = getattr(cg_feed, "snapshot", None)

        # CLOB from feed in-memory cache
        clob_data = {}
        if self._clob:
            clob_data = getattr(self._clob, "latest_clob", {})

        # Gamma prices from window
        gamma_up = getattr(window, "up_price", None)
        gamma_down = getattr(window, "down_price", None)

        # V4 snapshot from cache
        v4 = self._cached_v4
        ts_data = {}
        if v4:
            ts_data = (v4.get("timescales") or {}).get("5m", {})

        poly = ts_data.get("polymarket_live_recommended_outcome") or {}
        rec = ts_data.get("recommended_action") or {}
        macro = v4.get("macro", {}) if v4 else {}
        consensus = v4.get("consensus", {}) if v4 else {}
        sub_signals = ts_data.get("sub_signals", {})
        quantiles = ts_data.get("quantiles_at_close") or ts_data.get("quantiles_full", {})

        # V3 composites from V4 snapshot
        v3 = (v4.get("timescales") or {}) if v4 else {}

        # Hour UTC
        hour_utc = None
        if window_ts:
            hour_utc = datetime.fromtimestamp(window_ts, tz=timezone.utc).hour

        # Seconds to close
        seconds_to_close = eval_offset

        p_up = ts_data.get("probability_up")
        p_up_float = float(p_up) if p_up is not None else None
        poly_confidence = poly.get("confidence")

        return FullDataSurface(
            # Identity
            asset=asset,
            timescale="5m",
            window_ts=window_ts,
            eval_offset=eval_offset,
            assembled_at=now,
            # Price
            current_price=btc_price,
            open_price=open_price,
            # Deltas
            delta_binance=delta_binance,
            delta_tiingo=delta_tiingo,
            delta_chainlink=delta_chainlink,
            delta_pct=delta_pct,
            delta_source=delta_source,
            # VPIN + Regime
            vpin=vpin_val,
            regime=regime,
            # TWAP
            twap_delta=twap_delta,
            # V2 Predictions
            v2_probability_up=p_up_float,
            v2_probability_raw=(
                float(ts_data["probability_raw"])
                if ts_data.get("probability_raw") is not None
                else None
            ),
            v2_quantiles_p10=quantiles.get("p10"),
            v2_quantiles_p50=quantiles.get("p50"),
            v2_quantiles_p90=quantiles.get("p90"),
            # V3 Composites
            v3_5m_composite=_v3_composite(v3, "5m"),
            v3_15m_composite=_v3_composite(v3, "15m"),
            v3_1h_composite=_v3_composite(v3, "1h"),
            v3_4h_composite=_v3_composite(v3, "4h"),
            v3_24h_composite=_v3_composite(v3, "24h"),
            v3_48h_composite=_v3_composite(v3, "48h"),
            v3_72h_composite=_v3_composite(v3, "72h"),
            v3_1w_composite=_v3_composite(v3, "1w"),
            v3_2w_composite=_v3_composite(v3, "2w"),
            # V3 Sub-Signals
            v3_sub_elm=sub_signals.get("elm"),
            v3_sub_cascade=sub_signals.get("cascade"),
            v3_sub_taker=sub_signals.get("taker"),
            v3_sub_oi=sub_signals.get("oi"),
            v3_sub_funding=sub_signals.get("funding"),
            v3_sub_vpin=sub_signals.get("vpin"),
            v3_sub_momentum=sub_signals.get("momentum"),
            # V4 Regime / HMM
            v4_regime=ts_data.get("regime"),
            v4_regime_confidence=(
                float(ts_data["regime_confidence"])
                if ts_data.get("regime_confidence") is not None
                else None
            ),
            v4_regime_persistence=(
                float(ts_data["regime_persistence"])
                if ts_data.get("regime_persistence") is not None
                else None
            ),
            # V4 Macro
            v4_macro_bias=macro.get("bias"),
            v4_macro_direction_gate=macro.get("direction_gate"),
            v4_macro_size_modifier=macro.get("size_modifier"),
            # V4 Consensus
            v4_consensus_safe_to_trade=consensus.get("safe_to_trade"),
            v4_consensus_agreement_score=consensus.get("agreement_score"),
            v4_consensus_max_divergence_bps=consensus.get("max_divergence_bps"),
            # V4 Conviction
            v4_conviction=ts_data.get("conviction"),
            v4_conviction_score=(
                float(ts_data["conviction_score"])
                if ts_data.get("conviction_score") is not None
                else None
            ),
            # Polymarket Outcome
            poly_direction=poly.get("direction"),
            poly_trade_advised=poly.get("trade_advised"),
            poly_confidence=float(poly_confidence) if poly_confidence is not None else None,
            poly_confidence_distance=poly.get("confidence_distance"),
            poly_timing=poly.get("timing"),
            poly_max_entry_price=poly.get("max_entry_price"),
            poly_reason=poly.get("reason"),
            # V4 Recommended Action
            v4_recommended_side=rec.get("side"),
            v4_recommended_collateral_pct=rec.get("collateral_pct"),
            # V4 Sub-Signals
            v4_sub_signals=sub_signals or None,
            # V4 Quantiles
            v4_quantiles=quantiles or None,
            # CLOB
            clob_up_bid=clob_data.get("clob_up_bid"),
            clob_up_ask=clob_data.get("clob_up_ask"),
            clob_down_bid=clob_data.get("clob_down_bid"),
            clob_down_ask=clob_data.get("clob_down_ask"),
            clob_implied_up=clob_data.get("clob_implied_up"),
            # Gamma
            gamma_up_price=gamma_up,
            gamma_down_price=gamma_down,
            # CoinGlass
            cg_oi_usd=getattr(cg, "oi_usd", None) if cg else None,
            cg_funding_rate=getattr(cg, "funding_rate", None) if cg else None,
            cg_taker_buy_vol=getattr(cg, "taker_buy_volume_1m", None) if cg else None,
            cg_taker_sell_vol=getattr(cg, "taker_sell_volume_1m", None) if cg else None,
            cg_liq_total=getattr(cg, "liq_total_usd_1m", None) if cg else None,
            cg_liq_long=getattr(cg, "liq_long_usd_1m", None) if cg else None,
            cg_liq_short=getattr(cg, "liq_short_usd_1m", None) if cg else None,
            cg_long_short_ratio=getattr(cg, "long_short_ratio", None) if cg else None,
            # TimesFM
            timesfm_expected_move_bps=quantiles.get("expected_move_bps"),
            timesfm_vol_forecast_bps=quantiles.get("vol_forecast_bps"),
            # Window metadata
            hour_utc=hour_utc,
            seconds_to_close=seconds_to_close,
        )


def _v3_composite(v3_timescales: dict, ts: str) -> Optional[float]:
    """Extract v3 composite for a timescale from V4 snapshot."""
    ts_block = v3_timescales.get(ts, {})
    if isinstance(ts_block, dict):
        val = ts_block.get("v3_composite") or ts_block.get("composite")
        if val is not None:
            return float(val)
    return None
