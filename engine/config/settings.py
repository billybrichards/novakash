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

    # Polygon RPC
    polygon_rpc_url: str = Field(default="", description="Polygon RPC endpoint")

    # Telegram
    telegram_bot_token: str = Field(default="", description="Telegram bot token")
    telegram_chat_id: str = Field(default="", description="Telegram chat ID for alerts")

    # Risk / Trading
    starting_bankroll: float = Field(default=500.0, description="Starting bankroll in USD")
    paper_mode: bool = Field(default=True, description="Paper trading mode (no real orders)")

    # Polymarket token IDs for BTC markets (comma-separated)
    poly_btc_token_ids: str = Field(default="", description="Comma-separated Polymarket token IDs to watch")


settings = Settings()
