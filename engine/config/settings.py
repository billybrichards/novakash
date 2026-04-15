"""
Engine Settings — Pydantic BaseSettings

All configuration is loaded from environment variables / .env file.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Database
    database_url: str = Field(..., description="Async PostgreSQL connection string")

    # Polymarket
    poly_private_key: str = Field(
        default="", description="Ethereum private key for Polymarket"
    )
    poly_api_key: str = Field(default="", description="Polymarket CLOB API key")
    poly_api_secret: str = Field(default="", description="Polymarket CLOB API secret")
    poly_api_passphrase: str = Field(
        default="", description="Polymarket CLOB API passphrase"
    )
    poly_funder_address: str = Field(
        default="", description="Polymarket funder wallet address"
    )

    # Opinion
    opinion_api_key: str = Field(default="", description="Opinion exchange API key")
    opinion_wallet_key: str = Field(
        default="", description="Opinion wallet private key"
    )

    # Binance (data only)
    binance_api_key: str = Field(default="", description="Binance API key")
    binance_api_secret: str = Field(default="", description="Binance API secret")

    # CoinGlass
    coinglass_api_key: str = Field(default="", description="CoinGlass API key")
    openrouter_api_key: str = Field(
        default="", description="OpenRouter API key for AI summaries/evaluators"
    )
    openrouter_model: str = Field(
        default="qwen/qwen-2.5-7b-instruct",
        description="OpenRouter model for summaries/evaluators",
    )

    # Tiingo (CA-02: extracted from hardcoded literal in five_min_vpin.py)
    tiingo_api_key: str = Field(
        default="", description="Tiingo API key for crypto candle data"
    )

    # Polygon RPC
    polygon_rpc_url: str = Field(default="", description="Polygon RPC endpoint")

    # Telegram
    telegram_bot_token: str = Field(default="", description="Telegram bot token")
    telegram_chat_id: str = Field(default="", description="Telegram chat ID for alerts")
    telegram_alerts_paper: bool = Field(
        default=True, description="Send Telegram alerts for paper trades"
    )
    telegram_alerts_live: bool = Field(
        default=False, description="Send Telegram alerts for live trades"
    )

    # Risk / Trading
    starting_bankroll: float = Field(
        default=500.0, description="Starting bankroll in USD"
    )
    paper_mode: bool = Field(
        default=True, description="Paper trading mode (no real orders)"
    )
    paper_bankroll: float = Field(
        default=0.0, description="Paper bankroll override (0 = use starting_bankroll)"
    )

    # Playwright / Gmail
    playwright_enabled: bool = Field(
        default=False, description="Enable Playwright browser automation"
    )
    gmail_address: str = Field(
        default="", description="Gmail address for Polymarket login"
    )
    gmail_app_password: str = Field(
        default="", description="Gmail app password for IMAP"
    )

    # Polymarket token IDs for BTC markets (comma-separated)
    poly_btc_token_ids: str = Field(
        default="", description="Comma-separated Polymarket token IDs to watch"
    )

    # Builder Relayer
    builder_key: str = Field(
        default="", description="Builder Relayer API key for redemptions"
    )

    # 5-minute Polymarket trading settings
    five_min_enabled: bool = Field(
        default=False, description="Enable 5-minute Polymarket trading"
    )
    five_min_assets: str = Field(
        default="BTC", description="Comma-separated assets for 5-min trading"
    )
    five_min_mode: str = Field(
        default="safe", description="Trading mode: flat/safe/degen"
    )

    # v6.0 TimesFM-only strategy
    timesfm_enabled: bool = Field(
        default=False, description="Enable v6.0 TimesFM-only strategy"
    )
    timesfm_url: str = Field(
        default="http://3.98.114.0:8000", description="TimesFM forecast service URL"
    )
    timesfm_min_confidence: float = Field(
        default=0.30, description="Minimum TimesFM confidence to trade"
    )
    timesfm_assets: str = Field(
        default="BTC", description="Comma-separated assets for TimesFM strategy"
    )

    # v7.2 Multi-source delta calculation
    delta_price_source: str = Field(
        default="chainlink",
        description="Price source for window delta: chainlink (default/oracle), binance (legacy), tiingo, or consensus (all must agree)",
    )




class TestSettings(Settings):
    """Test defaults — NEVER instantiate in production paths.

    Subclasses Settings so every required field gets a safe default.
    Imported only by the test suite (tests/conftest.py and fixtures/).
    """
    database_url: str = "sqlite+aiosqlite:///:memory:"
    poly_private_key: str = "test"
    poly_api_key: str = "test"
    poly_api_secret: str = "test"
    poly_api_passphrase: str = "test"
    poly_funder_address: str = "0x0000000000000000000000000000000000000000"
    opinion_api_key: str = "test"
    opinion_wallet_key: str = "test"
    binance_api_key: str = "test"
    binance_api_secret: str = "test"
    coinglass_api_key: str = "test"
    openrouter_api_key: str = "test"
    tiingo_api_key: str = "test"
    polygon_rpc_url: str = "https://test.invalid"
    telegram_bot_token: str = "test"
    telegram_chat_id: str = "0"
    starting_bankroll: float = 500.0
    paper_mode: bool = True


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the singleton Settings instance, loading lazily."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def _reset_settings_for_tests() -> None:
    """Test helper — clears the cached singleton. Not for prod use."""
    global _settings
    _settings = None


class _LazySettingsProxy:
    """Backwards-compat shim for `from config.settings import settings`.

    Forwards attribute access to the lazily-loaded real Settings object.
    Existing call sites that do `settings.database_url` keep working without
    needing the refactor in Phase 1 Task 1.3 — that task is cleanup.
    """
    def __getattr__(self, name: str):
        return getattr(get_settings(), name)

    def __setattr__(self, name: str, value) -> None:
        setattr(get_settings(), name, value)


settings = _LazySettingsProxy()  # type: ignore[assignment]
