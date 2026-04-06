"""
Engine Settings — Pydantic BaseSettings

All configuration is loaded from environment variables / .env file.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str = Field(..., description="Async PostgreSQL connection string")

    # Polymarket
    poly_private_key: str = Field(default="", description="Ethereum private key for Polymarket")
    poly_api_key: str = Field(default="", description="Polymarket CLOB API key")
    poly_api_secret: str = Field(default="", description="Polymarket CLOB API secret")
    poly_api_passphrase: str = Field(default="", description="Polymarket CLOB API passphrase")
    poly_funder_address: str = Field(default="", description="Polymarket funder wallet address")

    # Opinion
    opinion_api_key: str = Field(default="", description="Opinion exchange API key")
    opinion_wallet_key: str = Field(default="", description="Opinion wallet private key")

    # Binance (data only)
    binance_api_key: str = Field(default="", description="Binance API key")
    binance_api_secret: str = Field(default="", description="Binance API secret")

    # CoinGlass
    coinglass_api_key: str = Field(default="", description="CoinGlass API key")
    anthropic_api_key: str = Field(default="", description="Anthropic API key for Claude evaluator")

    # Polygon RPC
    polygon_rpc_url: str = Field(default="", description="Polygon RPC endpoint")

    # Telegram
    telegram_bot_token: str = Field(default="", description="Telegram bot token")
    telegram_chat_id: str = Field(default="", description="Telegram chat ID for alerts")
    telegram_alerts_paper: bool = Field(default=True, description="Send Telegram alerts for paper trades")
    telegram_alerts_live: bool = Field(default=False, description="Send Telegram alerts for live trades")

    # Risk / Trading
    starting_bankroll: float = Field(default=500.0, description="Starting bankroll in USD")
    paper_mode: bool = Field(default=True, description="Paper trading mode (no real orders)")
    paper_bankroll: float = Field(default=0.0, description="Paper bankroll override (0 = use starting_bankroll)")

    # Playwright / Gmail
    playwright_enabled: bool = Field(default=False, description="Enable Playwright browser automation")
    gmail_address: str = Field(default="", description="Gmail address for Polymarket login")
    gmail_app_password: str = Field(default="", description="Gmail app password for IMAP")

    # Polymarket token IDs for BTC markets (comma-separated)
    poly_btc_token_ids: str = Field(default="", description="Comma-separated Polymarket token IDs to watch")

    # Builder Relayer
    builder_key: str = Field(default="", description="Builder Relayer API key for redemptions")

    # 5-minute Polymarket trading settings
    five_min_enabled: bool = Field(default=False, description="Enable 5-minute Polymarket trading")
    five_min_assets: str = Field(default="BTC", description="Comma-separated assets for 5-min trading")
    five_min_mode: str = Field(default="safe", description="Trading mode: flat/safe/degen")

    # v6.0 TimesFM-only strategy
    timesfm_enabled: bool = Field(default=False, description="Enable v6.0 TimesFM-only strategy")
    timesfm_url: str = Field(default="http://3.98.114.0:8000", description="TimesFM forecast service URL")
    timesfm_min_confidence: float = Field(default=0.30, description="Minimum TimesFM confidence to trade")
    timesfm_assets: str = Field(default="BTC", description="Comma-separated assets for TimesFM strategy")

    # v7.2 Multi-source delta calculation
    delta_price_source: str = Field(
        default="chainlink",
        description="Price source for window delta: chainlink (default/oracle), binance (legacy), tiingo, or consensus (all must agree)",
    )


settings = Settings()
