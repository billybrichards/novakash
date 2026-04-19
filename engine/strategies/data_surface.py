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

import structlog

log = structlog.get_logger(__name__)


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

    # Audit #121 Path 1 — TimesFM ensemble fields (timesfm-repo commit f62e9d8).
    # All None when V5_ENSEMBLE_PATH1_ENABLED is off on the timesfm side, OR
    # when classifier head fails to load (then mode == "fallback_lgb_only").
    # Cross-repo contract keys — do NOT rename without coordinated PR.
    probability_lgb: Optional[float]
    probability_classifier: Optional[float]
    ensemble_config: Optional[dict]

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

    # ── Per-field inference timestamps ───────────────────────────────────
    # Wall-clock moment the probability_classifier (TimesFM Path1) value
    # was produced upstream. This is NOT the surface-assembly time (see
    # ``assembled_at``) — it is the closest available timestamp for the
    # classifier inference itself. Populated from the v4 snapshot's
    # top-level ``ts`` field when present, with a fallback to the engine's
    # v4-cache-write time (``self._cached_v4_ts``). None when the cached
    # snapshot has no timestamp AND the cache hasn't been primed yet.
    #
    # Freshness gates in individual strategies (e.g. v6_sniper's path1
    # freshness check) should prefer this over ``assembled_at`` because
    # a single cached v4 payload feeds many successive surfaces across a
    # 5-minute window — ``assembled_at`` drifts past the gate threshold
    # while the upstream classifier is still healthy, firing false
    # "no_eval_blocked" skips. This field tracks the real inference age.
    probability_classifier_inferred_at: Optional[float] = None


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
        # TimesFM health alerting — optional callback set via set_alerter().
        # Called once when cache first crosses staleness threshold and again
        # when it recovers. Prevents alert spam while ensuring Daisy knows
        # TimesFM is degraded (2026-04-16 incident: silent degradation for
        # 1h+ because no Telegram fires when /v4/snapshot goes bad).
        self._alert_cb = None
        self._degraded_since: Optional[float] = None
        self._alert_stale_threshold_s = 45.0  # alert after 45s stale

    def set_alerter(self, alert_cb: Any) -> None:
        """Register a coroutine(text, level) for TimesFM health alerts.

        ``alert_cb`` must be awaitable: ``async def cb(text: str, level: str)``.
        Typical wiring: ``cb = TelegramAlerter.send_system_alert``.
        Optional — if unset, staleness only goes to structlog error.
        """
        self._alert_cb = alert_cb

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

        # /v4/snapshot p99 observed 2026-04-16: 4-6s under normal CPU,
        # 10-16s under TimesFM CPU pressure (281% sustained). A 5s
        # timeout counted every slow response as a fetch failure →
        # cache staleness crossed 45s threshold every few minutes →
        # Telegram flap cycle (degraded for 51-259s, recovered for
        # 30-60s, repeat). 15s accommodates the observed p99 while
        # still being well below the 45s stale-alert threshold, so
        # genuine TimesFM outages still fire the alert loudly.
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        )

        # Prime the snapshot cache before the registry starts evaluating.
        try:
            await self._fetch_v4()
        except Exception:
            pass

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
        """Fetch V4 snapshot and cache it in memory.

        Observability: every failure is logged with a reason. When the cache
        goes stale (>30s since last successful fetch), escalates to ERROR so
        it's visible in alerting. Empty polymarket block is a distinct failure
        mode from transport timeout — log it separately so the TimesFM
        feature-pipeline freeze (PR #47 class) gets caught early.
        """
        if not self._session:
            return
        url = f"{self._v4_url}/v4/snapshot"
        params = {"asset": "BTC", "timescales": "5m,15m", "strategy": "polymarket_5m"}
        age = time.time() - self._cached_v4_ts if self._cached_v4_ts else None
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    log_fn = log.error if age is not None and age > 30 else log.warning
                    log_fn(
                        "data_surface.v4_fetch_http_error",
                        status=resp.status,
                        cache_age_s=round(age, 1) if age is not None else None,
                    )
                    return
                body = await resp.json()
                # Validate downstream critical fields BEFORE replacing cache.
                # TimesFM can serve 200 OK with an empty polymarket block when
                # its v2 feature pipeline freezes (see PR #47 class bug) —
                # treat that as a fetch failure, not a success, to avoid
                # feeding stale cache to strategies via a poisoned surface.
                ts5 = (body.get("timescales") or {}).get("5m", {})
                poly = ts5.get("polymarket_live_recommended_outcome") or {}
                if not poly or poly.get("timing") is None:
                    log_fn = log.error if age is not None and age > 30 else log.warning
                    log_fn(
                        "data_surface.v4_empty_polymarket",
                        poly_keys=len(poly),
                        cache_age_s=round(age, 1) if age is not None else None,
                    )
                    return
                self._cached_v4 = body
                self._cached_v4_ts = time.time()
                # RECOVERY alert — we were degraded and now we're not.
                if self._degraded_since is not None and self._alert_cb is not None:
                    down_s = int(time.time() - self._degraded_since)
                    try:
                        await self._alert_cb(
                            f"TimesFM /v4/snapshot recovered after {down_s}s down — "
                            f"poly data flowing again.",
                            "info",
                        )
                    except Exception as exc:
                        log.warning("data_surface.recovery_alert_failed", error=str(exc)[:200])
                    self._degraded_since = None
                return
        except asyncio.TimeoutError:
            log_fn = log.error if age is not None and age > 30 else log.warning
            log_fn(
                "data_surface.v4_fetch_timeout",
                cache_age_s=round(age, 1) if age is not None else None,
            )
        except Exception as exc:
            log_fn = log.error if age is not None and age > 30 else log.warning
            log_fn(
                "data_surface.v4_fetch_error",
                error=str(exc)[:200],
                cache_age_s=round(age, 1) if age is not None else None,
            )

        # Any fall-through to here = this tick did not replace the cache.
        # If the cache age now crosses the alert threshold and we have not
        # already alerted, fire a Telegram alert so Daisy sees the outage
        # even though strategies continue to skip silently.
        if age is not None and age > self._alert_stale_threshold_s:
            if self._degraded_since is None:
                self._degraded_since = time.time() - age
                if self._alert_cb is not None:
                    try:
                        await self._alert_cb(
                            f"TimesFM /v4/snapshot degraded — cache stale "
                            f"{int(age)}s. Strategies will skip until recovery.",
                            "error",
                        )
                    except Exception as exc:
                        log.warning(
                            "data_surface.degraded_alert_failed",
                            error=str(exc)[:200],
                        )

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
            # Supports MarketAggregator (async get_state()) and MarketState directly.
            # Use the live in-memory _state when the aggregator object is injected here,
            # because get_surface() is intentionally synchronous.
            _state_obj = self._binance_state
            if hasattr(self._binance_state, "_state"):
                _state_obj = getattr(self._binance_state, "_state", _state_obj)
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

        # Select primary delta
        # For 5m Polymarket markets: chainlink first (it IS the resolution oracle)
        # For other timescales: tiingo first (higher update frequency)
        timeframe = getattr(window, "timeframe", "5m")
        _duration = getattr(window, "duration_secs", 300)
        _is_5m = timeframe == "5m" or _duration == 300
        if _is_5m:
            _delta_priority = [
                ("chainlink", delta_chainlink),
                ("tiingo_rest_candle", delta_tiingo),
                ("binance", delta_binance),
            ]
        else:
            _delta_priority = [
                ("tiingo_rest_candle", delta_tiingo),
                ("chainlink", delta_chainlink),
                ("binance", delta_binance),
            ]
        for src, val in _delta_priority:
            if val is not None:
                delta_pct = val
                delta_source = src
                break

        # VPIN + Regime
        vpin_val = 0.0
        regime = "UNKNOWN"
        if self._vpin:
            vpin_val = getattr(self._vpin, "current_vpin", 0.0) or 0.0
            if vpin_val >= 0.65:
                regime = "CASCADE"
            elif vpin_val >= 0.55:
                regime = "TRANSITION"
            elif vpin_val >= 0.45:
                regime = "NORMAL"
            else:
                regime = "CALM"

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

        # V4 snapshot from cache — reject cache older than 60s.
        # A stale cache causes strategies to evaluate against frozen poly
        # data, which is what produced the 2026-04-16 "timing=early"
        # silent-skip incident (TimesFM /v4/snapshot hung; engine kept
        # serving stale cache for an hour). Treating stale cache as
        # absent forces strategies into `timing="unknown"` rather than
        # acting on stale "early"/"optimal" labels.
        timeframe = getattr(window, "timeframe", "5m")
        v4 = self._cached_v4
        if v4 and self._cached_v4_ts:
            age = time.time() - self._cached_v4_ts
            if age > 60:
                log.error(
                    "data_surface.v4_cache_stale",
                    cache_age_s=round(age, 1),
                    reason="stale cache rejected; strategies will see poly=None",
                )
                v4 = None
        ts_data = {}
        if v4:
            ts_data = (v4.get("timescales") or {}).get(timeframe, {})

        poly = ts_data.get("polymarket_live_recommended_outcome") or {}
        rec = ts_data.get("recommended_action") or {}
        # Read per-timescale macro (fallback to top-level for backward compat)
        macro = ts_data.get("macro", {}) or (v4.get("macro", {}) if v4 else {})
        consensus = v4.get("consensus", {}) if v4 else {}
        sub_signals = ts_data.get("sub_signals", {})
        quantiles = ts_data.get("quantiles_at_close") or ts_data.get(
            "quantiles_full", {}
        )

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

        # Per-field inference timestamp for the Path1 classifier. Prefer the
        # v4 payload's top-level ``ts`` (closest to actual inference time),
        # then the engine's v4-cache-write time (``_cached_v4_ts``) which
        # lags inference by the round-trip latency (~100-500ms). Leave as
        # None when the classifier itself is absent — freshness gates read
        # this together with ``probability_classifier`` to decide skips.
        p_classifier_raw = ts_data.get("probability_classifier")
        if p_classifier_raw is None:
            classifier_inferred_at: Optional[float] = None
        else:
            payload_ts = None
            if v4:
                try:
                    _raw_ts = v4.get("ts")
                    if _raw_ts:
                        payload_ts = float(_raw_ts)
                except (TypeError, ValueError):
                    payload_ts = None
            classifier_inferred_at = payload_ts or (self._cached_v4_ts or None)

        return FullDataSurface(
            # Identity
            asset=asset,
            timescale=timeframe,
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
            # Path 1 ensemble (audit #121) — all None when ensemble disabled
            # on timesfm side. ensemble_config carries mode + weights metadata.
            probability_lgb=(
                float(ts_data["probability_lgb"])
                if ts_data.get("probability_lgb") is not None
                else None
            ),
            probability_classifier=(
                float(ts_data["probability_classifier"])
                if ts_data.get("probability_classifier") is not None
                else None
            ),
            ensemble_config=ts_data.get("ensemble_config"),
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
            poly_confidence=float(poly_confidence)
            if poly_confidence is not None
            else None,
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
            # Per-field inference timestamp for Path1 classifier
            probability_classifier_inferred_at=classifier_inferred_at,
        )


def _v3_composite(v3_timescales: dict, ts: str) -> Optional[float]:
    """Extract v3 composite for a timescale from V4 snapshot."""
    ts_block = v3_timescales.get(ts, {})
    if isinstance(ts_block, dict):
        val = ts_block.get("v3_composite") or ts_block.get("composite")
        if val is not None:
            return float(val)
    return None
