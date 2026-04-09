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

    # ── Signal source ──
    timesfm_ws_url: str = "ws://3.98.114.0:8080/v3/signal"
    signal_threshold: float = 0.3  # minimum composite score to trade

    # ── Sizing ──
    starting_capital: float = 500.0
    leverage: int = 5
    bet_fraction: float = 0.05  # 5% of balance per trade

    # ── Risk ──
    max_open_positions: int = 3
    max_exposure_pct: float = 0.60
    daily_loss_limit_pct: float = 0.10
    consecutive_loss_cooldown: int = 3
    cooldown_seconds: int = 600
    stop_loss_pct: float = 0.015  # 1.5%
    take_profit_pct: float = 0.03  # 3%
    trailing_stop_pct: float = 0.01  # 1%
    max_hold_seconds: int = 3600  # 1 hour
    signal_reversal_threshold: float = -0.2

    # ── Timescales to trade on ──
    trading_timescales: str = "5m,15m,1h"  # comma-separated

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
