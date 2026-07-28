from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_paper: bool = True

    alpaca_stock_feed: Literal["iex", "sip"] = "iex"
    alpaca_option_feed: Literal["indicative", "opra"] = "indicative"

    database_url: str = "postgresql+asyncpg://trader:trader@localhost:5433/trader"
    redis_url: str = "redis://localhost:6380/0"

    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # Risk defaults; live values persisted in app_settings table.
    default_max_loss_pct: float = 0.02
    default_daily_loss_pct: float = 0.06
    default_stop_loss_pct: float = 0.50
    default_take_profit_pct: float = 1.00

    # Bar cache retention: 10 trading days of 1-minute bars per symbol.
    bar_cache_days: int = 10

    def validate_paper_lock(self) -> None:
        # v1 is hard-locked to paper trading. Refuse to boot otherwise.
        if not self.alpaca_paper:
            raise RuntimeError(
                "ALPACA_PAPER=false is not supported in this build. "
                "Live trading is disabled until the system is validated on paper."
            )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_paper_lock()
    return settings
