"""
Margin engine settings — loaded from environment variables.

Pydantic BaseSettings reads from env vars and .env files automatically.
All secrets come from the environment; no hardcoded values.
"""

from __future__ import annotations

from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings

# Supported execution venues. Keeping this as a module-level tuple rather
# than an Enum so operators can see the set from reading the file and the
# validator can produce a readable error message.
SUPPORTED_VENUES: tuple[str, ...] = ("binance", "hyperliquid")


class MarginSettings(BaseSettings):
    """Configuration for the margin engine."""

    # ── Exchange ──
    binance_api_key: str = ""
    binance_private_key_path: str = "/opt/margin-engine/.keys/binance_ed25519.pem"
    paper_mode: bool = True  # default to paper for safety

    # Which execution venue we model (paper) or trade on (live). Orthogonal to
    # paper_mode — see the 2x2 matrix in main.py. Only "binance" has a live
    # adapter today; "hyperliquid" is paper-only until the signing layer lands.
    #
    # DQ-06: defaults to "hyperliquid" because the paper+binance branch in
    # main.py constructs PaperExchangeAdapter WITHOUT a price_getter, so
    # _last_price stays stuck at the 80000.0 class default and every
    # paper fill prices against a frozen $80k constant — producing garbage
    # validation PnL. The paper+hyperliquid branch correctly wires
    # HyperliquidPriceFeed as the price source. User confirmed 2026-04-11
    # that the paper venue should be hyperliquid. The CI deploy workflow
    # also explicitly templates MARGIN_EXCHANGE_VENUE=hyperliquid on every
    # deploy so the host .env stays aligned even if it drifts.
    exchange_venue: str = "hyperliquid"

    # ── Paper exchange fee calibration ──
    # Binance spot margin default ≈ 0.001 per side (0.1%, 20 bps RT).
    # Maker tier is slightly lower (~0.0008 = 16 bps RT).
    paper_fee_rate: float = 0.001
    paper_spread_bps: float = 2.0

    # Hyperliquid BTC-PERP ≈ 4.5 bps taker per side = 9 bps RT, 1 bp spread.
    # These are the venue-aware defaults the wiring in main.py applies when
    # exchange_venue=hyperliquid and neither *_override below is set.
    hyperliquid_paper_fee_rate: float = 0.00045
    hyperliquid_paper_spread_bps: float = 1.0

    # Explicit operator overrides — if set, these win over any venue-aware
    # default, letting you force a specific fee profile without editing code.
    # Useful for A/B testing "what if HL raised taker to 10 bps" scenarios.
    paper_fee_rate_override: Optional[float] = None
    paper_spread_bps_override: Optional[float] = None

    # ── Hyperliquid price feed ──
    hyperliquid_info_url: str = "https://api.hyperliquid.xyz/info"
    hyperliquid_asset: str = "BTC"
    hyperliquid_poll_interval_s: float = 2.0
    hyperliquid_price_freshness_s: float = 15.0

    # ── v4 snapshot integration (dark deploy in PR A, behavior in PR B) ──
    # The /v4/snapshot endpoint on the timesfm service fuses per-timescale
    # probability, TimesFM quantiles, regime classification, 6-source price
    # consensus, Claude macro bias, macro-event calendar, and cascade FSM
    # state into one atomic read. PR A scaffolds the adapter and observes
    # what v4 would filter; PR B consumes the gates in entry + continuation.
    #
    # `engine_use_v4_actions` starts OFF so merge of PR A is pure
    # telemetry — use cases keep running on the legacy /v2/probability path
    # regardless of this setting until PR B wires them to consume v4.

    v4_snapshot_url: str = "http://16.52.14.182:8080"
    engine_use_v4_actions: bool = False
    v4_primary_timescale: str = "15m"
    v4_timescales: str = "5m,15m,1h,4h"  # CSV; used for snapshot request
    v4_strategy: str = "fee_aware_15m"
    v4_poll_interval_s: float = 2.0
    v4_freshness_s: float = 10.0

    # Thresholds consumed by PR B code paths. Declared here so env files
    # are ready before PR B lands — no settings churn in the second PR.
    v4_entry_edge: float = 0.10  # min |p - 0.5| for entry
    v4_continuation_min_conviction: float = 0.10  # looser than entry (user's choice)
    v4_continuation_max: Optional[int] = 7  # max 7 continuations per position
    v4_min_expected_move_bps: float = 15.0  # Hyperliquid-calibrated fee wall
    v4_allow_mean_reverting: bool = False  # opt-in per strategy
    v4_event_exit_seconds: int = 120  # force exit within 2 min of HIGH/EXTREME

    # ── Fee-aware continuation (NEW) ──
    fee_aware_continuation_enabled: bool = False
    fee_aware_partial_tp_threshold: float = 0.5
    fee_aware_partial_tp_size: float = 0.5
    continuation_alignment_enabled: bool = False
    continuation_min_timescales: int = 2
    continuation_hold_extension_max: float = 2.0
    continuation_conviction_min: float = 0.10
    continuation_regime_bonus: bool = True
    max_partial_closes: int = 3
    partial_close_cooldown_s: float = 300.0

    # ── Phase A (2026-04-11): macro gate demotion ──
    # 24h audit of margin engine paper-mode behaviour showed Qwen's BEAR
    # calls at 20-30% directional hit rate across 15m/1h/4h horizons —
    # actively anti-predictive. Full audit in docs/MACRO_AUDIT_2026-04-11.md.
    #
    # `v4_macro_mode` controls how the macro direction_gate is applied at
    # entry AND continuation:
    #   "veto"     — hard-skip entries and force-close existing positions
    #                when the gate opposes the side. Only ever fires when
    #                macro.confidence >= v4_macro_hard_veto_confidence_floor.
    #                Previous behaviour.
    #   "advisory" — default. Log the conflict but do NOT skip/force-close.
    #                At entry, apply v4_macro_advisory_size_mult_on_conflict
    #                as a haircut on preliminary_collateral so conflicting
    #                macro reduces position size instead of blocking it.
    #
    # Below the confidence floor the macro gate is a no-op in both modes.
    # The floor exists so a flat NEUTRAL/0 fallback row (macro observer
    # down, ticker hiccup) cannot accidentally scale down every entry.
    v4_macro_mode: str = "advisory"
    v4_macro_hard_veto_confidence_floor: int = 80
    v4_macro_advisory_size_mult_on_conflict: float = 0.75

    # Optional experimental override: allow entries when regime=NO_EDGE if
    # TimesFM's quantile-derived expected move clears a stronger bar. The
    # 2026-04-11 audit found a 74-sample bucket (NO_EDGE + BEAR + exp_move>3)
    # with 100% directional hit rate and +22.8 bps average actual move —
    # suspiciously clean and needs a 7-day replay before activation. This
    # flag ships the code in Phase A but leaves it OFF (None) by default.
    # Flip via MARGIN_V4_ALLOW_NO_EDGE_IF_EXP_MOVE_BPS_GTE=3.0 after replay.
    v4_allow_no_edge_if_exp_move_bps_gte: Optional[float] = None

    # DQ-07: defensive mark_divergence gate. When v4.last_price (which is
    # Binance spot from the assembler) diverges from the exchange's actual
    # mark price by more than this many basis points, the gate rejects the
    # trade with reason "mark_divergence". Catches stale spot ticks,
    # Hyperliquid basis spikes, and cross-region latency. Default 0.0 = no-op
    # (gate returns passed unconditionally), so the merge is zero-behavior-
    # change in production. Operator enables by setting
    # MARGIN_V4_MAX_MARK_DIVERGENCE_BPS=20 in /opt/margin-engine/.env.
    v4_max_mark_divergence_bps: float = 0.0

    # ── Regime-adaptive strategy (ME-STRAT-04) ──
    # Feature flag to enable regime-based strategy selection.
    # When True, routes to different strategies based on V4 regime classification.
    # Default False for safe rollout.
    regime_adaptive_enabled: bool = False

    # Trend strategy defaults (for TRENDING_UP/TRENDING_DOWN regimes)
    regime_trend_min_prob: float = 0.55
    regime_trend_size_mult: float = 1.2
    regime_trend_stop_bps: int = 150
    regime_trend_tp_bps: int = 200
    regime_trend_hold_minutes: int = 60
    regime_trend_min_expected_move_bps: float = 30.0

    # Mean-reversion strategy defaults (for MEAN_REVERTING regime)
    regime_mr_entry_threshold: float = 0.70
    regime_mr_size_mult: float = 0.8
    regime_mr_stop_bps: int = 80
    regime_mr_tp_bps: int = 50
    regime_mr_hold_minutes: int = 15
    regime_mr_min_fade_conviction: float = 0.55

    # No-trade strategy (for CHOPPY/NO_EDGE regimes)
    regime_no_trade_allow: bool = False
    regime_no_trade_size_mult: float = 0.1

    # ── ME-STRAT-05: Cascade Fade Strategy ──
    # Feature flag to enable cascade fade strategy
    cascade_fade_enabled: bool = False

    # Minimum cascade strength to consider fading
    cascade_min_strength: float = 0.5

    # Position sizing for cascade fades (half size due to higher risk)
    cascade_fade_size_mult: float = 0.5

    # Stop loss and take profit (basis points) - wider stops for cascades
    cascade_fade_stop_bps: int = 300  # 3% stop
    cascade_fade_tp_bps: int = 100  # 1% target

    # Holding period and cooldown
    cascade_fade_hold_minutes: int = 10  # Very short hold
    cascade_cooldown_seconds: int = 900  # 15 min cooldown after cascade

    @property
    def v4_timescales_tuple(self) -> tuple[str, ...]:
        """CSV 'v4_timescales' split into a tuple for adapter consumption."""
        return tuple(s.strip() for s in self.v4_timescales.split(",") if s.strip())

    # ── Signal sources ──
    # v3 composite: still polled for REGIME FILTER (soft, logged only) and
    # for passive signal recording. Direction comes from the ML endpoint.
    timesfm_ws_url: str = "ws://16.52.14.182:8080/v3/signal"
    # Magnitude of 1h composite required to allow a trade. Market too quiet
    # → skip. This is NOT a directional threshold — sign is not used.
    # NOTE: start at 0.0 (effectively OFF). Backtest on 114 historical
    # candidates showed avg |composite_1h|=0.273, so gating at 0.5 blocks
    # 80%+ of trades. Soften once we have more regime-diverse data.
    regime_threshold: float = 0.0
    regime_timescale: str = "1h"
    # Legacy field kept for backward compatibility with existing env vars.
    # Unused by v2 strategy.
    signal_threshold: float = 0.50

    # v2 ML probability endpoint (HTTP poller, not WebSocket)
    probability_http_url: str = "http://16.52.14.182:8080"
    probability_asset: str = "BTC"
    probability_timescale: str = "15m"
    probability_seconds_to_close: int = 480  # sweet spot per edge analysis
    probability_poll_interval_s: float = 30.0
    probability_freshness_s: float = 120.0
    # |p_up - 0.5| >= this → trade. 0.20 means p>0.70 or p<0.30.
    probability_min_conviction: float = 0.20

    # ── Sizing ──
    # Paper mode → can size larger than live-ready defaults to generate
    # meaningful P&L signal while we validate the new strategy.
    starting_capital: float = 1000.0  # paper mode starting bankroll
    leverage: int = 3  # down from 5 while validating
    bet_fraction: float = 0.02  # 2% per trade, down from 5%

    # ── Risk ──
    max_open_positions: int = 1  # one at a time — clean attribution
max_exposure_pct: float = 0.80   # 80% max exposure per position
    daily_loss_limit_pct: float = 0.10
    consecutive_loss_cooldown: int = 3
    cooldown_seconds: int = 600
    stop_loss_pct: float = 0.006  # 0.6% (3x fee cost, was 1.5%)
    take_profit_pct: float = 0.005  # 0.5% (2.7x fee cost, was 3%)
    trailing_stop_pct: float = 0.003  # 0.3% (was 1%)
    max_hold_seconds: int = 900  # 15 min (window close, was 1 hour)
    # v2: signal_reversal_threshold removed from active code path but
    # retained here so existing .env files don't fail on startup.
    signal_reversal_threshold: float = -10.0

    # ── Timescales to trade on ──
    # DEPRECATED: v2/v3 legacy field, no longer used in V4 fusion path.
    # V4 uses v4_timescales (5m,15m,1h,4h) for multi-timescale consensus.
    # Kept for .env compatibility only.
    trading_timescales: str = "15m"  # DEPRECATED — not used in V4

    # ── Database ──
    database_url: str = ""

    # ── Telegram ──
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_enabled: bool = True

    # ── Status server ──
    status_port: int = 8090

    # ── Loop cadence ──
    tick_interval_s: float = 2.0  # position management check interval

    model_config = {"env_prefix": "MARGIN_", "env_file": ".env", "extra": "ignore"}

    @field_validator("exchange_venue", mode="before")
    @classmethod
    def _normalize_venue(cls, v: object) -> str:
        """
        Coerce venue to lowercase and reject unknown values.

        Runs in mode='before' so the lowercased string is what pydantic
        stores on the model — downstream code can do exact-match checks
        like `venue == "binance"` without worrying about casing.
        """
        if not isinstance(v, str):
            raise ValueError(f"exchange_venue must be a string, got {type(v).__name__}")
        normalized = v.strip().lower()
        if normalized not in SUPPORTED_VENUES:
            raise ValueError(
                f"exchange_venue={v!r} is not supported. "
                f"Valid values: {', '.join(SUPPORTED_VENUES)}"
            )
        return normalized

    @property
    def trading_timescale_list(self) -> list[str]:
        return [t.strip() for t in self.trading_timescales.split(",") if t.strip()]

    @property
    def effective_paper_fee_rate(self) -> float:
        """
        Resolve the effective fee rate for paper mode based on venue + overrides.

        Precedence:
          1. paper_fee_rate_override (if set, always wins)
          2. venue-specific default (hyperliquid → hyperliquid_paper_fee_rate)
          3. generic paper_fee_rate (Binance-calibrated default)
        """
        if self.paper_fee_rate_override is not None:
            return self.paper_fee_rate_override
        if self.exchange_venue == "hyperliquid":
            return self.hyperliquid_paper_fee_rate
        return self.paper_fee_rate

    @property
    def effective_paper_spread_bps(self) -> float:
        """Same resolution order as effective_paper_fee_rate."""
        if self.paper_spread_bps_override is not None:
            return self.paper_spread_bps_override
        if self.exchange_venue == "hyperliquid":
            return self.hyperliquid_paper_spread_bps
        return self.paper_spread_bps
