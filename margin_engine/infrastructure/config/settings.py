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

    v4_snapshot_url: str = "http://3.98.114.0:8080"
    engine_use_v4_actions: bool = False
    v4_primary_timescale: str = "15m"
    v4_timescales: str = "5m,15m,1h,4h"   # CSV; used for snapshot request
    v4_strategy: str = "fee_aware_15m"
    v4_poll_interval_s: float = 2.0
    v4_freshness_s: float = 10.0

    # Thresholds consumed by PR B code paths. Declared here so env files
    # are ready before PR B lands — no settings churn in the second PR.
    v4_entry_edge: float = 0.10                      # min |p - 0.5| for entry
    v4_continuation_min_conviction: float = 0.10     # looser than entry (user's choice)
    v4_continuation_max: Optional[int] = None        # None = uncapped (user's choice)
    v4_min_expected_move_bps: float = 15.0           # Hyperliquid-calibrated fee wall
    v4_allow_mean_reverting: bool = False            # opt-in per strategy
    v4_event_exit_seconds: int = 120                 # force exit within 2 min of HIGH/EXTREME

    @property
    def v4_timescales_tuple(self) -> tuple[str, ...]:
        """CSV 'v4_timescales' split into a tuple for adapter consumption."""
        return tuple(s.strip() for s in self.v4_timescales.split(",") if s.strip())

    # ── Signal sources ──
    # v3 composite: still polled for REGIME FILTER (soft, logged only) and
    # for passive signal recording. Direction comes from the ML endpoint.
    timesfm_ws_url: str = "ws://3.98.114.0:8080/v3/signal"
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
    probability_http_url: str = "http://3.98.114.0:8080"
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
    starting_capital: float = 500.0
    leverage: int = 3                # down from 5 while validating
    bet_fraction: float = 0.02       # 2% per trade, down from 5%

    # ── Risk ──
    max_open_positions: int = 1      # one at a time — clean attribution
    max_exposure_pct: float = 0.20   # down from 0.60
    daily_loss_limit_pct: float = 0.10
    consecutive_loss_cooldown: int = 3
    cooldown_seconds: int = 600
    stop_loss_pct: float = 0.006     # 0.6% (3x fee cost, was 1.5%)
    take_profit_pct: float = 0.005   # 0.5% (2.7x fee cost, was 3%)
    trailing_stop_pct: float = 0.003 # 0.3% (was 1%)
    max_hold_seconds: int = 900      # 15 min (window close, was 1 hour)
    # v2: signal_reversal_threshold removed from active code path but
    # retained here so existing .env files don't fail on startup.
    signal_reversal_threshold: float = -10.0

    # ── Timescales to trade on ──
    # v2 trades only the 15m window (the one with proven edge) via the
    # probability endpoint. The v3 composite timescales are still
    # retained for regime filtering.
    trading_timescales: str = "15m"

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
