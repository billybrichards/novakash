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
    # ``v2_probability_up`` is the "effective" P(UP) the engine consumes: if
    # the forecaster shipped novakash-timesfm PR #107 and env flag
    # ``V2_PROB_USE_CALIBRATED`` is true, it is the isotonic-calibrated value;
    # otherwise it falls through to the uncalibrated ``probability_up`` key.
    # ``v2_probability_up_raw`` / ``v2_probability_up_calibrated`` /
    # ``isotonic_version`` are kept purely for logging + diagnostics; they
    # do NOT feed gates or blends.
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

    # Isotonic calibration (novakash-timesfm PR #107) — diagnostics only.
    # ``v2_probability_up`` above is whichever value the adapter selected
    # (see `V2_PROB_USE_CALIBRATED`). These three fields preserve the raw
    # and calibrated originals plus the isotonic version tag for logging.
    # All default to None so older V4 snapshots / forecaster versions
    # pre-PR-107 keep working unchanged.
    v2_probability_up_raw: Optional[float] = None
    v2_probability_up_calibrated: Optional[float] = None
    isotonic_version: Optional[str] = None

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

    # ── Cedar LGB candidate fields (2026-04-21 shadow A/B) ────────────────
    # Populated from /v2/probability/cedar (Montreal ML box). BTC-only
    # initially. When the cedar endpoint is unreachable, these stay None
    # and cedar-sourced strategies (v8_champion_cedar*) SKIP with
    # ``cedar_source_unavailable`` — prod v8_champion is unaffected.
    #
    # Contract mirror of the prod probability block: probability_up_cedar
    # is the ensemble blended prob (equivalent of surface.poly_confidence
    # on the prod side); probability_lgb_cedar is the LGB direction head;
    # probability_classifier_cedar is the TimesFM Path1 classifier head.
    # v4_regime_cedar is optional — present when the cedar snapshot ships
    # its own regime label, absent when the caller should reuse the prod
    # ``v4_regime``. Strategies decide how to consume; the v8_champion
    # hook only swaps the probability stack based on
    # ``gate_params.v2_probability_source``.
    probability_up_cedar: Optional[float] = None
    probability_lgb_cedar: Optional[float] = None
    probability_classifier_cedar: Optional[float] = None
    v4_regime_cedar: Optional[str] = None

    # v10 LGB shadow model — served alongside prod v5 via probability_lgb_v10
    # on /v4/snapshot. The engine's v10_lgb_only strategy reads this field.
    probability_lgb_v10: Optional[float] = None

    # v12 LGB combo model — served alongside prod v5 via probability_lgb_v12
    # on /v4/snapshot. The engine's v12_lgb_combo strategy reads this field.
    probability_lgb_v12: Optional[float] = None

    # v9.1 LGB retrain — priceToBeat-aligned, served alongside v9 PROD via
    # probability_lgb_v9_1 on /v4/snapshot when timesfm V9_1_ENABLED=true.
    # The engine's v9_1_lgb_only (PR #466) and v12_lgb_combo (post-2026-05-02
    # swap) strategies read this field. Hub note #313 + docs/v9_1_PROVENANCE.md.
    probability_lgb_v9_1: Optional[float] = None

    # ── Audit #374 — Chainlink delta-source freshness (2026-05-06) ────────
    # Seconds since the last on-chain Chainlink Aggregator V3 round update for
    # the surface's asset. Populated from ChainlinkFeed.latest_updated_at[asset]
    # at surface-assembly time. None when the feed is offline or hasn't yet
    # observed a round for this asset. Strategies use this to fail closed when
    # the on-chain oracle is stale (the off-chain CG/Tiingo deltas will keep
    # reading fine, masking a directional signal that has actually rotted).
    delta_chainlink_age_seconds: Optional[int] = None

    # ── Audit #373 — CoinGlass directional flow proxy (2026-05-06) ────────
    # Directional ratio derived from CoinGlass taker buy vs taker sell volumes:
    #   delta_coinglass = (taker_buy_vol - taker_sell_vol)
    #                     / max(taker_buy_vol + taker_sell_vol, 1e-9)
    # > 0 = net taker BUY pressure → UP; < 0 = net SELL pressure → DOWN.
    # Used as a 4th vote in SourceAgreementGate when min_sources >= 3.
    # Falls back to None when CG snapshot is missing — SourceAgreementGate then
    # degrades to legacy 3-source (chainlink + tiingo + binance) behaviour.
    delta_coinglass: Optional[float] = None


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
        v4_fallback_url: Optional[str] = None,
        tiingo_feed: Any = None,
        chainlink_feed: Any = None,
        clob_feed: Any = None,
        vpin_calculator: Any = None,
        cg_feeds: Optional[dict] = None,
        twap_tracker: Any = None,
        binance_state: Any = None,
        active_assets: Optional[list[str]] = None,
    ):
        self._v4_url = v4_base_url or os.environ.get(
            "TIMESFM_URL", "http://localhost:8001"
        )
        # Optional fallback host used when the primary /v4/snapshot fetch
        # fails (non-200, timeout, transport error). Classifier handoff
        # (hub note #226 + #228, 2026-04-24): primary TIMESFM_URL is
        # flipped to the classifier box so the engine can read
        # ``probability_classifier`` (pc) off every snapshot for shadow
        # logging. TIMESFM_FALLBACK_URL points back at the pre-flip
        # primary so a classifier-box outage doesn't blind the engine.
        # ``None`` / empty-string disables the fallback.
        self._v4_fallback_url = (
            v4_fallback_url
            or os.environ.get("TIMESFM_FALLBACK_URL")
            or None
        )
        if self._v4_fallback_url == "":
            self._v4_fallback_url = None
        # Per-asset fetch telemetry. ``_last_fetch_latency_ms`` is the
        # wall-clock duration of the most recent successful /v4/snapshot
        # response; surfaces read this into metadata as
        # ``pc_fetch_latency_ms`` for burn-in p95 analysis (note #226
        # promotion checklist). ``_last_fetch_source`` tracks whether the
        # cache was refreshed from primary or fallback so ops can grep
        # for "engine fell back to pre-flip box" incidents.
        self._last_fetch_latency_ms: dict[str, float] = {}
        self._last_fetch_source: dict[str, str] = {}
        self._tiingo = tiingo_feed
        self._chainlink = chainlink_feed
        self._clob = clob_feed
        self._vpin = vpin_calculator
        self._cg_feeds = cg_feeds or {}
        self._twap = twap_tracker
        self._binance_state = binance_state

        self._session = None  # aiohttp.ClientSession -- persistent
        # Per-asset v4 snapshot cache (audit #267).
        # Historical bug: a single-slot cache would hold ONE asset's payload
        # (always BTC in practice). A non-BTC window then read BTC data into
        # its surface — silent corruption. These dicts are keyed by UPPER
        # asset (BTC/ETH/SOL/XRP) so ETH windows read ETH snapshots etc.
        # Empty dicts mean "no cache yet" — get_surface falls through to
        # poly=None, forcing strategies into their classifier-only / no_poly
        # paths rather than reading stale neighbour data.
        self._cached_v4: dict[str, dict] = {}
        self._cached_v4_ts: dict[str, float] = {}
        # Assets that the refresh loop polls /v4/snapshot for. Defaults to
        # BTC only for backward-compat with single-asset callers/tests. The
        # orchestrator overrides this via set_active_assets() once it knows
        # the feed asset list.
        self._active_assets: list[str] = [
            a.upper() for a in (active_assets or ["BTC"])
        ]
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

        # ── Cedar LGB candidate cache (shadow A/B, 2026-04-21) ───────────
        # Per-asset cache slot for /v2/probability/cedar. Mirrors the v4
        # cache shape exactly (same {asset: dict} + ts pattern introduced
        # by the per-asset refactor). Cedar endpoint is served by the ML
        # box alongside the prod /v2/probability route. Missing endpoint
        # or non-200s leave the cache at its defaulted-empty state — the
        # surface builder then emits cedar fields as None and cedar
        # strategies SKIP gracefully (``cedar_source_unavailable``).
        #
        # BTC-only until the ML box ships multi-asset cedar. ETH/SOL/XRP
        # in _active_assets are skipped by _fetch_cedar_asset.
        self._cached_cedar: dict[str, dict] = {}
        self._cached_cedar_ts: dict[str, float] = {}
        self._cedar_url = os.environ.get(
            "CEDAR_PROBABILITY_URL",
            "http://16.52.14.182:8080/v2/probability/cedar",
        )
        # Sticky disable: once we see a hard negative (404/405/501, DNS
        # error, repeated timeout) we stop hammering the endpoint for the
        # rest of this process's lifetime. Resets on engine restart.
        self._cedar_disabled: bool = False

        # ── Cold-start warmup cache (2026-04-26) ──────────────────────────
        # Per-asset cache of the most-recent ``signal_evaluations`` row read
        # from the DB at startup by ``warmup_from_db``. Used as a *fallback*
        # in ``get_surface`` for fields whose live source has not populated
        # yet (Tiingo + Chainlink delta caches typically take 5–6 minutes
        # after a restart to fan out through the data-surface pipeline).
        # Live feed values ALWAYS win over the warmup cache — the warmup is
        # only consulted when the live read returned None/empty/zero.
        #
        # Warmup values are stamped with the source row's ``window_ts`` so
        # rows older than ``_warmup_max_age_s`` are ignored at read time
        # (don't seed strategies with hour-old data after a long downtime).
        self._warmup_cache: dict[str, dict] = {}
        self._warmup_max_age_s: float = 15 * 60  # 15 minutes

    def set_active_assets(self, assets: list[str]) -> None:
        """Set which assets the refresh loop polls /v4/snapshot for.

        Call after the feed list is known (typically from
        FIFTEEN_MIN_ASSETS) so ETH/SOL/XRP snapshots start populating
        alongside BTC. Case-insensitive — stored as upper.
        """
        if not assets:
            return
        self._active_assets = [a.upper() for a in assets]

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

    async def warmup_from_db(self, db_pool: Any) -> None:
        """Seed the in-memory surface from the most recent signal_evaluations row.

        After every engine restart, the Tiingo/Chainlink delta caches +
        VPIN + CLOB take ~5 minutes to fan out through the data-surface
        pipeline, even though the underlying feeds start polling within
        seconds. During that window strategies SKIP every eval with
        ``feature_stale: chainlink,tiingo missing at eval`` and
        ``source_agreement: chainlink,tiingo missing``.

        Meanwhile the DB has a perfectly-good ``signal_evaluations`` row
        from the previous engine instance, written ≤2s before the
        previous shutdown. Those values are stale by 30–300s but FAR
        more useful than None — strategies can at least evaluate, and
        live ticks overwrite the warmup within seconds anyway (live
        always wins; see ``get_surface``).

        Per active asset, this method:
          • SELECTs the most recent row (BTC + asset filter, last 15min)
          • Stores it in ``_warmup_cache`` keyed by asset
          • Logs ``data_surface.warmup_seeded`` with source ts + age
          • Logs ``data_surface.warmup_no_recent_row`` when the row is
            absent or older than the freshness window — strategies will
            keep skipping until live ticks arrive (existing behaviour).

        Fail-open: any DB error is caught and swallowed. Engine startup
        must NOT block on this query — it is purely opportunistic.
        """
        if db_pool is None:
            log.info(
                "data_surface.warmup_skipped",
                reason="db_pool is None",
            )
            return
        try:
            now = time.time()
            for asset in list(self._active_assets):
                try:
                    async with db_pool.acquire() as conn:
                        row = await conn.fetchrow(
                            """
                            SELECT
                                window_ts, asset, timeframe, eval_offset,
                                clob_up_bid, clob_up_ask,
                                clob_down_bid, clob_down_ask,
                                binance_price, chainlink_price, tiingo_close,
                                delta_pct, delta_tiingo, delta_binance,
                                delta_chainlink, delta_source,
                                vpin, regime,
                                v2_probability_up, v2_model_version,
                                evaluated_at
                            FROM signal_evaluations
                            WHERE asset = $1
                              AND evaluated_at > NOW() - INTERVAL '15 minutes'
                            ORDER BY evaluated_at DESC
                            LIMIT 1
                            """,
                            asset,
                        )
                except Exception as exc:
                    log.warning(
                        "data_surface.warmup_query_error",
                        asset=asset,
                        error=str(exc)[:200],
                    )
                    continue
                if row is None:
                    log.info(
                        "data_surface.warmup_no_recent_row",
                        asset=asset,
                        reason="no signal_evaluations row in last 15 minutes",
                    )
                    continue
                # Convert asyncpg.Record → dict for stable downstream access.
                seeded = {k: row[k] for k in row.keys()}
                self._warmup_cache[asset] = seeded
                evaluated_at = seeded.get("evaluated_at")
                age_s: Optional[float] = None
                if evaluated_at is not None:
                    try:
                        age_s = max(0.0, now - evaluated_at.timestamp())
                    except Exception:
                        age_s = None
                log.info(
                    "data_surface.warmup_seeded",
                    asset=asset,
                    window_ts=int(seeded.get("window_ts") or 0),
                    eval_offset=seeded.get("eval_offset"),
                    age_seconds=round(age_s, 1) if age_s is not None else None,
                    delta_source=seeded.get("delta_source"),
                    regime=seeded.get("regime"),
                )
        except Exception as exc:
            log.warning(
                "data_surface.warmup_unexpected_error",
                error=str(exc)[:200],
                reason="non-fatal; engine startup continues without seed",
            )

    def _get_warmup(self, asset: str) -> Optional[dict]:
        """Return the warmup row for ``asset`` if still within the freshness
        window, else None. Used by ``get_surface`` as a per-field fallback."""
        seeded = self._warmup_cache.get(asset)
        if not seeded:
            return None
        evaluated_at = seeded.get("evaluated_at")
        if evaluated_at is None:
            return None
        try:
            age_s = time.time() - evaluated_at.timestamp()
        except Exception:
            return None
        if age_s > self._warmup_max_age_s:
            return None
        return seeded

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
        # Cold-start fix: eagerly fetch /v4/snapshot so v4_regime is
        # populated from tick zero — otherwise strategies SKIP for 5-15
        # minutes until the first Polymarket window closes and the eval
        # loop fires.
        try:
            await self._fetch_v4()
            # Log what we got so ops can verify cold-start priming worked.
            for asset_key in list(self._active_assets):
                v4 = self._cached_v4.get(asset_key)
                if v4:
                    ts5 = (v4.get("timescales") or {}).get("5m", {})
                    log.info(
                        "data_surface.startup_v4_fetch",
                        asset=asset_key,
                        regime=ts5.get("regime"),
                        probability_lgb=ts5.get("probability_lgb"),
                        probability_classifier=ts5.get("probability_classifier"),
                        poly_timing=(
                            (ts5.get("polymarket_live_recommended_outcome") or {})
                            .get("timing")
                        ),
                        cache_populated=True,
                    )
                else:
                    log.warning(
                        "data_surface.startup_v4_fetch",
                        asset=asset_key,
                        cache_populated=False,
                        reason="fetch returned no usable data; strategies will skip until next refresh",
                    )
        except Exception as exc:
            log.warning(
                "data_surface.startup_v4_fetch_failed",
                error=str(exc)[:200],
                reason="non-fatal; background refresh loop will retry",
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
        """Fetch V4 snapshot for every active asset and cache per-asset.

        Per-asset cache (audit #267): BTC has a full LGB+classifier stack
        and a populated polymarket outcome block; ETH/SOL/XRP (2026-04-20)
        serve classifier-only snapshots with ``status=no_model`` and an
        empty poly block. We must:

          1. keep strict poly validation for BTC (still primary/LIVE),
          2. accept ``no_model`` / empty-poly payloads for non-BTC assets
             as long as the classifier head is present, so v7_15m_sniper
             can still read probability_classifier,
          3. key the cache by asset — not a single slot.

        Alerting tracks BTC only (it is the LIVE asset). Non-BTC failures
        are logged but do NOT fire Telegram to avoid noise.
        """
        if not self._session:
            return
        for asset in list(self._active_assets):
            await self._fetch_v4_asset(asset)
        # Parallel cedar fetch (BTC-only, shadow A/B). Isolated in its
        # own try/except so any cedar failure leaves prod v4 cache
        # untouched and strategies consuming prod probabilities continue
        # normally. Non-fatal — cedar strategies will just SKIP until the
        # ML box responds.
        if not self._cedar_disabled:
            try:
                await self._fetch_cedar_asset("BTC")
            except Exception as exc:
                log.warning(
                    "data_surface.cedar_fetch_unexpected_error",
                    error=str(exc)[:200],
                )

    async def _fetch_v4_asset(self, asset: str) -> None:
        """Fetch /v4/snapshot for a single asset and update its cache slot.

        Classifier-box shadow read (note #226): the primary host is
        ``TIMESFM_URL`` (now pointed at the classifier box so the
        snapshot includes ``probability_classifier``). If that fetch
        fails, we transparently retry against ``TIMESFM_FALLBACK_URL``
        (the pre-flip primary, LGB-only). The fallback snapshot is
        accepted and cached exactly like the primary so trade decisions
        keep flowing — the only cost is ``pc`` is missing from that
        tick, which strategies handle via the existing None-fallback.
        """
        ok = await self._try_fetch_snapshot(asset, self._v4_url, source="primary")
        if ok:
            return
        if self._v4_fallback_url:
            log.warning(
                "data_surface.v4_fallback_attempt",
                asset=asset,
                fallback_url=self._v4_fallback_url,
            )
            ok = await self._try_fetch_snapshot(
                asset, self._v4_fallback_url, source="fallback"
            )
            if ok:
                return

        # Both primary and fallback failed — reuse existing alert path.
        cached_ts = self._cached_v4_ts.get(asset, 0.0)
        age = time.time() - cached_ts if cached_ts else None
        if asset.upper() == "BTC":
            self._maybe_fire_degraded_alert(age)

    async def _try_fetch_snapshot(
        self, asset: str, base_url: str, *, source: str
    ) -> bool:
        """Attempt one /v4/snapshot fetch against ``base_url``.

        Returns True if the cache was updated. Latency is recorded in
        ``_last_fetch_latency_ms`` on success so ``get_surface`` can
        stamp it on the decision metadata for burn-in p95 analysis.
        """
        url = f"{base_url}/v4/snapshot"
        params = {"asset": asset, "timescales": "5m,15m", "strategy": "polymarket_5m"}
        is_primary = asset.upper() == "BTC"
        cached_ts = self._cached_v4_ts.get(asset, 0.0)
        age = time.time() - cached_ts if cached_ts else None
        t0 = time.monotonic()
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    log_fn = log.error if (
                        is_primary and age is not None and age > 30
                        and source == "primary"
                    ) else log.warning
                    log_fn(
                        "data_surface.v4_fetch_http_error",
                        asset=asset,
                        status=resp.status,
                        source=source,
                        cache_age_s=round(age, 1) if age is not None else None,
                    )
                    return False
                body = await resp.json()
                # BTC: full stack required — reject empty poly block (PR #47
                # feature-pipeline freeze class bug).
                # Non-BTC: accept classifier-only (no_model) payloads. Still
                # require SOME content so a totally empty body doesn't get
                # cached.
                if is_primary:
                    ts5 = (body.get("timescales") or {}).get("5m", {})
                    poly = ts5.get("polymarket_live_recommended_outcome") or {}
                    if not poly or poly.get("timing") is None:
                        log_fn = log.error if (
                            age is not None and age > 30
                            and source == "primary"
                        ) else log.warning
                        log_fn(
                            "data_surface.v4_empty_polymarket",
                            asset=asset,
                            poly_keys=len(poly),
                            source=source,
                            cache_age_s=round(age, 1) if age is not None else None,
                        )
                        return False
                else:
                    # Non-BTC: accept no_model payloads. Sanity check: body
                    # must have a timescales block (otherwise it's an error
                    # page, not a valid snapshot).
                    if not isinstance(body.get("timescales"), dict):
                        log.warning(
                            "data_surface.v4_missing_timescales",
                            asset=asset,
                            source=source,
                            status=body.get("status"),
                        )
                        return False
                latency_ms = (time.monotonic() - t0) * 1000.0
                self._cached_v4[asset] = body
                self._cached_v4_ts[asset] = time.time()
                self._last_fetch_latency_ms[asset] = latency_ms
                self._last_fetch_source[asset] = source
                # BTC recovery alert — we were degraded and now we're not.
                if is_primary and self._degraded_since is not None and self._alert_cb is not None:
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
                return True
        except asyncio.TimeoutError:
            log_fn = log.error if (
                is_primary and age is not None and age > 30
                and source == "primary"
            ) else log.warning
            log_fn(
                "data_surface.v4_fetch_timeout",
                asset=asset,
                source=source,
                cache_age_s=round(age, 1) if age is not None else None,
            )
        except Exception as exc:
            log_fn = log.error if (
                is_primary and age is not None and age > 30
                and source == "primary"
            ) else log.warning
            log_fn(
                "data_surface.v4_fetch_error",
                asset=asset,
                error=str(exc)[:200],
                source=source,
                cache_age_s=round(age, 1) if age is not None else None,
            )
        return False

    async def _fetch_cedar_asset(self, asset: str) -> None:
        """Fetch /v2/probability/cedar for an asset and cache the payload.

        Shadow A/B path — the ML box exposes a parallel endpoint to
        /v2/probability that runs the cedar LGB candidate alongside the
        prod model. Shape is assumed to mirror the existing probability
        payload: ``{"probability_up": float, "probability_lgb": float,
        "probability_classifier": float, "regime": str?}`` plus any
        metadata the ML box includes.

        This method is deliberately permissive about missing keys — when
        cedar returns 200 but with a partial payload, we cache whatever
        arrived and let ``get_surface`` read None for absent fields.

        Non-2xx responses or connection errors DO NOT propagate — they
        set ``_cedar_disabled=True`` (hard negatives) or simply log
        (transient). Prod v4 cache is never touched.
        """
        if not self._session:
            return
        asset_key = asset.upper()
        url = self._cedar_url
        params = {"asset": asset_key}
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status in (404, 405, 501):
                    # Hard negative — endpoint not deployed yet. Go quiet
                    # until restart so we don't spam logs.
                    log.info(
                        "data_surface.cedar_endpoint_unavailable",
                        asset=asset_key,
                        status=resp.status,
                        url=url,
                    )
                    self._cedar_disabled = True
                    return
                if resp.status != 200:
                    log.warning(
                        "data_surface.cedar_fetch_http_error",
                        asset=asset_key,
                        status=resp.status,
                    )
                    return
                body = await resp.json()
                if not isinstance(body, dict):
                    log.warning(
                        "data_surface.cedar_bad_payload",
                        asset=asset_key,
                        body_type=type(body).__name__,
                    )
                    return
                self._cached_cedar[asset_key] = body
                self._cached_cedar_ts[asset_key] = time.time()
        except asyncio.TimeoutError:
            log.warning("data_surface.cedar_fetch_timeout", asset=asset_key)
        except Exception as exc:
            log.warning(
                "data_surface.cedar_fetch_error",
                asset=asset_key,
                error=str(exc)[:200],
            )

    def get_fetch_telemetry(self, asset: str) -> dict:
        """Return last-fetch telemetry for ``asset``.

        Used by the registry decision-writer to stamp
        ``pc_fetch_latency_ms`` + ``pc_fetch_source`` onto
        ``strategy_decisions.metadata_json`` for the burn-in analysis
        (note #226 promotion checklist item 6: latency p95 < 500 ms).
        Returns ``{}`` if no successful fetch has happened yet for this
        asset — consumers should treat missing keys as "no data".
        """
        key = asset.upper() if asset else "BTC"
        out: dict = {}
        lat = self._last_fetch_latency_ms.get(key)
        if lat is not None:
            out["pc_fetch_latency_ms"] = round(lat, 2)
        src = self._last_fetch_source.get(key)
        if src is not None:
            out["pc_fetch_source"] = src
        return out

    def _maybe_fire_degraded_alert(self, age: Optional[float]) -> None:
        """Schedule a BTC /v4/snapshot degraded Telegram alert if threshold crossed.

        Idempotent via ``_degraded_since``. Awaits the alert callback on the
        caller's task so the log order stays consistent.
        """
        if age is None or age <= self._alert_stale_threshold_s:
            return
        if self._degraded_since is not None:
            return
        self._degraded_since = time.time() - age
        if self._alert_cb is None:
            return
        # Fire alert on current task — caller is already inside an async
        # coroutine (either _fetch_v4_asset or _fetch_v4 prime path).
        async def _fire() -> None:
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
        try:
            asyncio.create_task(_fire())
        except RuntimeError:
            # No running loop (test context) — skip firing.
            pass

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

        # Audit #374: also capture Chainlink delta-source freshness at this
        # asset. ChainlinkFeed populates `latest_updated_at[asset]` (epoch
        # seconds) from the on-chain Aggregator V3 round.updatedAt every poll.
        # When the feed is offline / hasn't yet seen a round → None.
        delta_chainlink_age_seconds: Optional[int] = None
        if self._chainlink and open_price:
            cl_prices = getattr(self._chainlink, "latest_prices", {})
            cl_price = cl_prices.get(asset)
            if cl_price:
                delta_chainlink = (cl_price - open_price) / open_price
            cl_updated_at_map = getattr(
                self._chainlink, "latest_updated_at", {}
            )
            cl_updated_at = cl_updated_at_map.get(asset)
            if cl_updated_at:
                try:
                    age = int(now - float(cl_updated_at))
                    if age >= 0:
                        delta_chainlink_age_seconds = age
                except (TypeError, ValueError):
                    delta_chainlink_age_seconds = None

        if btc_price and open_price:
            delta_binance = (btc_price - open_price) / open_price

        # ── Cold-start warmup fallback for deltas (2026-04-26) ────────────
        # If a delta source is still None (feeds haven't fanned-out yet
        # post-restart), fall back to the value seeded from the most
        # recent signal_evaluations row. Live values ALWAYS win — this
        # only triggers when the live read came back None.
        _warmup = self._get_warmup(asset)
        if _warmup is not None:
            if delta_chainlink is None and _warmup.get("delta_chainlink") is not None:
                try:
                    delta_chainlink = float(_warmup["delta_chainlink"])
                except (TypeError, ValueError):
                    pass
            if delta_tiingo is None and _warmup.get("delta_tiingo") is not None:
                try:
                    delta_tiingo = float(_warmup["delta_tiingo"])
                except (TypeError, ValueError):
                    pass
            if delta_binance is None and _warmup.get("delta_binance") is not None:
                try:
                    delta_binance = float(_warmup["delta_binance"])
                except (TypeError, ValueError):
                    pass

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
        # Cold-start warmup fallback: VPIN bucket calculator needs ~50
        # buckets ($2.5M of trades) to produce a valid value, which can
        # take 2–4 minutes after a restart. If we have no live value,
        # reuse the most recent DB row so regime classification works
        # immediately. Live values overwrite within seconds.
        if vpin_val == 0.0 and regime == "UNKNOWN" and _warmup is not None:
            warm_vpin = _warmup.get("vpin")
            warm_regime = _warmup.get("regime")
            if warm_vpin is not None:
                try:
                    vpin_val = float(warm_vpin)
                except (TypeError, ValueError):
                    pass
            if warm_regime:
                regime = str(warm_regime)

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

        # Audit #373: derive a 4th directional vote from CoinGlass taker flow.
        # +1 = net taker BUY pressure (UP); -1 = net SELL (DOWN). Normalised
        # to [-1, 1]. None when CG snapshot is missing — SourceAgreementGate
        # then degrades to 3-source (chainlink + tiingo + binance) cleanly.
        delta_coinglass: Optional[float] = None
        if cg is not None:
            try:
                tb = getattr(cg, "taker_buy_volume_1m", None)
                ts_v = getattr(cg, "taker_sell_volume_1m", None)
                if tb is not None and ts_v is not None:
                    tb = float(tb)
                    ts_v = float(ts_v)
                    denom = tb + ts_v
                    if denom > 0.0:
                        delta_coinglass = (tb - ts_v) / denom
            except (TypeError, ValueError):
                delta_coinglass = None

        # CLOB from feed in-memory cache
        clob_data = {}
        if self._clob:
            clob_data = getattr(self._clob, "latest_clob", {})
        # Cold-start warmup fallback: CLOB orderbook poller cycles every
        # 10s, so a fresh restart leaves clob_data empty for the first
        # tick or two. Backfill from the warmup row if no live data yet.
        if not clob_data and _warmup is not None:
            for live_key, warm_key in (
                ("clob_up_bid", "clob_up_bid"),
                ("clob_up_ask", "clob_up_ask"),
                ("clob_down_bid", "clob_down_bid"),
                ("clob_down_ask", "clob_down_ask"),
            ):
                v = _warmup.get(warm_key)
                if v is not None:
                    try:
                        clob_data[live_key] = float(v)
                    except (TypeError, ValueError):
                        pass

        # Gamma prices from window
        gamma_up = getattr(window, "up_price", None)
        gamma_down = getattr(window, "down_price", None)

        # V4 snapshot from per-asset cache — reject cache older than 60s.
        # A stale cache causes strategies to evaluate against frozen poly
        # data, which is what produced the 2026-04-16 "timing=early"
        # silent-skip incident (TimesFM /v4/snapshot hung; engine kept
        # serving stale cache for an hour). Treating stale cache as
        # absent forces strategies into `timing="unknown"` rather than
        # acting on stale "early"/"optimal" labels.
        #
        # Audit #267: cache is keyed by asset so ETH/SOL/XRP windows
        # read their own snapshots instead of silently inheriting BTC
        # payloads. An asset with no cache entry yet falls through to
        # poly=None (and classifier=None) — strategies then hit their
        # staleness / no_eval branches, which is the correct behaviour.
        timeframe = getattr(window, "timeframe", "5m")
        asset_key = asset.upper() if asset else "BTC"
        v4 = self._cached_v4.get(asset_key)
        asset_ts = self._cached_v4_ts.get(asset_key, 0.0)
        if v4 and asset_ts:
            age = time.time() - asset_ts
            if age > 60:
                log.error(
                    "data_surface.v4_cache_stale",
                    asset=asset_key,
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

        # ── Cedar shadow probabilities (2026-04-21 A/B) ──────────────────
        # Per-asset read from _cached_cedar. Stale (>60s) treated as
        # absent so cedar strategies SKIP on stale rather than firing
        # against frozen data. BTC-only today; non-BTC windows simply
        # read None and, for GHOST cedar strategies, SKIP cleanly.
        cedar_p_up: Optional[float] = None
        cedar_p_lgb: Optional[float] = None
        cedar_p_classifier: Optional[float] = None
        cedar_regime: Optional[str] = None
        cedar_payload = self._cached_cedar.get(asset_key)
        cedar_ts = self._cached_cedar_ts.get(asset_key, 0.0)
        if cedar_payload and cedar_ts and (time.time() - cedar_ts) <= 60:
            try:
                raw_up = cedar_payload.get("probability_up")
                if raw_up is not None:
                    cedar_p_up = float(raw_up)
                raw_lgb = cedar_payload.get("probability_lgb")
                if raw_lgb is not None:
                    cedar_p_lgb = float(raw_lgb)
                raw_cls = cedar_payload.get("probability_classifier")
                if raw_cls is not None:
                    cedar_p_classifier = float(raw_cls)
                raw_regime = cedar_payload.get("regime")
                if raw_regime is not None:
                    cedar_regime = str(raw_regime)
            except (TypeError, ValueError) as exc:
                log.warning(
                    "data_surface.cedar_payload_coerce_error",
                    error=str(exc)[:200],
                )

        # ── Isotonic-calibrated P(UP) passthrough (novakash-timesfm PR #107) ──
        # The V4 snapshot may include the new additive fields
        # ``probability_up_calibrated`` + ``isotonic_version`` alongside the
        # unchanged ``probability_up`` / ``probability_raw``. If it also
        # carries a top-level ``probability_up_effective`` (set by the
        # forecaster or by ``TimesFMV2Client`` when this dict is the raw
        # scorer payload), we trust that as the already-selected value.
        # Otherwise re-apply the env-flag decision here so surfaces fed
        # directly off the V4 snapshot still benefit from the calibration
        # choice. See ``engine/adapters/prediction/timesfm_v2.py``.
        p_up_raw = ts_data.get("probability_up")
        p_up_cal = ts_data.get("probability_up_calibrated")
        iso_version = ts_data.get("isotonic_version")
        p_up_eff = ts_data.get("probability_up_effective")
        if p_up_eff is None:
            use_cal = os.environ.get(
                "V2_PROB_USE_CALIBRATED", "true"
            ).strip().lower() in ("1", "true", "yes", "on")
            p_up_eff = p_up_cal if (use_cal and p_up_cal is not None) else p_up_raw
        p_up_float = float(p_up_eff) if p_up_eff is not None else None
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
            classifier_inferred_at = payload_ts or (asset_ts or None)

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
            # Isotonic calibration passthrough (audit: novakash-timesfm #107)
            v2_probability_up_raw=(
                float(p_up_raw) if p_up_raw is not None else None
            ),
            v2_probability_up_calibrated=(
                float(p_up_cal) if p_up_cal is not None else None
            ),
            isotonic_version=(str(iso_version) if iso_version is not None else None),
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
            probability_lgb_v10=(
                float(ts_data["probability_lgb_v10"])
                if ts_data.get("probability_lgb_v10") is not None
                else None
            ),
            probability_lgb_v12=(
                float(ts_data["probability_lgb_v12"])
                if ts_data.get("probability_lgb_v12") is not None
                else None
            ),
            probability_lgb_v9_1=(
                float(ts_data["probability_lgb_v9_1"])
                if ts_data.get("probability_lgb_v9_1") is not None
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
            # Cedar shadow A/B passthrough (2026-04-21)
            probability_up_cedar=cedar_p_up,
            probability_lgb_cedar=cedar_p_lgb,
            probability_classifier_cedar=cedar_p_classifier,
            v4_regime_cedar=cedar_regime,
            # Audit #373 / #374 (2026-05-06)
            delta_chainlink_age_seconds=delta_chainlink_age_seconds,
            delta_coinglass=delta_coinglass,
        )


def _v3_composite(v3_timescales: dict, ts: str) -> Optional[float]:
    """Extract v3 composite for a timescale from V4 snapshot."""
    ts_block = v3_timescales.get(ts, {})
    if isinstance(ts_block, dict):
        val = ts_block.get("v3_composite") or ts_block.get("composite")
        if val is not None:
            return float(val)
    return None
