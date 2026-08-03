from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# Anchor .env discovery to the source tree, not the process cwd. Launching
# uvicorn from the repo root vs backend/ must not silently drop the Alpaca
# keys (which would flip the whole app into the keyless fallback feed).
_BACKEND_DIR = Path(__file__).resolve().parent.parent  # .../backend
_ENV_FILES = (str(_BACKEND_DIR.parent / ".env"), str(_BACKEND_DIR / ".env"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILES, env_file_encoding="utf-8", extra="ignore"
    )

    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_paper: bool = True

    alpaca_stock_feed: Literal["iex", "sip"] = "iex"
    alpaca_option_feed: Literal["indicative", "opra"] = "indicative"

    database_url: str = "postgresql+asyncpg://trader:trader@localhost:5433/trader"
    redis_url: str = "redis://localhost:6380/0"

    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # HEADLESS=true boots the ENGINE ONLY: full execution stack (streams,
    # FSM, exit enforcer, reconcile, risk) plus the command/ops API, but no
    # UI-serving routes (chain, bars REST, browser WebSocket). This is the
    # future split seam: run the engine supervised on an always-on host and
    # point any UI at it — or at a separate UI-serving instance.
    headless: bool = False

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
