"""Configuração lida do ficheiro `.env` via Pydantic Settings."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Modo de operação ---
    live_mode: bool = Field(default=False, description="false = dry-run; true = ordens reais")

    # --- Polygon / Polymarket ---
    polygon_private_key: SecretStr
    polygon_chain_id: int = 137
    polygon_rpc_url: str = "https://polygon-rpc.com"

    clob_api_url: str = "https://clob.polymarket.com"
    polymarket_data_api_url: str = "https://data-api.polymarket.com"
    polymarket_gamma_api_url: str = "https://gamma-api.polymarket.com"

    clob_api_key: SecretStr | None = None
    clob_api_secret: SecretStr | None = None
    clob_api_passphrase: SecretStr | None = None
    proxy_wallet_address: str | None = None

    # --- Telegram ---
    telegram_bot_token: SecretStr
    telegram_chat_id: str

    # --- Persistência ---
    database_url: str = "sqlite+aiosqlite:///./data/polymarket_bot.db"

    # --- Logging ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "text"] = "json"

    # --- Universo de wallets ---
    wallet_discovery_top_n: int = 150
    scoring_window_weeks: int = 4

    @property
    def is_dry_run(self) -> bool:
        return not self.live_mode

    @property
    def has_clob_credentials(self) -> bool:
        return all([self.clob_api_key, self.clob_api_secret, self.clob_api_passphrase])

    @property
    def data_dir(self) -> Path:
        path = Path("data")
        path.mkdir(exist_ok=True)
        return path


@lru_cache
def get_settings() -> Settings:
    """Carrega settings uma única vez por processo."""
    return Settings()  # type: ignore[call-arg]
