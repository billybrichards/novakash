"""
Margin engine settings — loaded from environment variables.

Pydantic BaseSettings reads from env vars and .env files automatically.
All secrets come from the environment; no hardcoded values.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings


class MarginSettings(BaseSettings):
    """Configuration for the Binance margin engine."""

    # ── Exchange ──
    binance_api_key: str = ""
    binance_private_key_path: str = "/opt/margin-engine/.keys/binance_ed25519.pem"
    paper_mode: bool = True  # default to paper for safety
    # Paper exchange fee per side. Binance spot margin default is around
    # 0.001 (0.1%, 20 bps RT). Maker tier is slightly lower (~0.0008 = 16 bps
    # RT). Set this to model the execution regime you expect in live mode.
    paper_fee_rate: float = 0.001
    paper_spread_bps: float = 2.0

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

    @property
    def trading_timescale_list(self) -> list[str]:
        return [t.strip() for t in self.trading_timescales.split(",") if t.strip()]
