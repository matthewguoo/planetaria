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
    # Which named paper account the keys above currently represent; stamped
    # onto plans. Set at boot by AccountService.apply() — pydantic requires
    # the field declared for that assignment to exist.
    alpaca_account_name: str = "default"

    # TRADING_MODE=live_manual boots the ISOLATED LIVE SERVER: real-money
    # endpoint, manual UI entries only. The strategy plane (runner, feeds,
    # breaker loop) is never constructed in that process — a live order can
    # only originate from a human ticket, while the exit enforcer still runs
    # protective TP/SL/time stops. validate_paper_lock() holds the boot
    # invariants for both modes.
    trading_mode: Literal["paper", "live_manual"] = "paper"
    # The one env-pinned live account (e.g. live_roth). No DB selection and
    # no fallback in live mode: if these keys are missing, boot dies rather
    # than proceeding on whatever keys are lying around in settings.
    live_account_name: str = ""

    alpaca_stock_feed: Literal["iex", "sip"] = "iex"
    alpaca_option_feed: Literal["indicative", "opra"] = "indicative"

    database_url: str = "postgresql+asyncpg://trader:trader@localhost:5433/trader"
    redis_url: str = "redis://localhost:6380/0"

    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # SQLITE_FALLBACK=true (the dev default) lets a boot with Postgres down
    # continue on ./trader.db so exit enforcement keeps working on a laptop.
    # A server unit sets it false: an empty fallback store means zero
    # monitors on real positions while /api/health still answers, so a
    # Postgres outage must be a boot failure there. Live forces it off.
    sqlite_fallback: bool = True

    # KEYLESS=true boots with NO broker keys whatever .env holds: the UI
    # preview knob (scripts/preview-ui.ps1). A second keyed engine on a
    # box that already runs one is a second exit enforcer on the same
    # account, and PowerShell cannot export an empty variable to blank the
    # keys from outside — so the refusal lives here, in the process.
    keyless: bool = False

    # Bar cache retention: 10 trading days of 1-minute bars per symbol.
    bar_cache_days: int = 10

    # STRATEGIES_ENABLED=false is the boot-time kill switch for the whole
    # strategy runtime: instances stay in the DB but nothing spawns.
    strategies_enabled: bool = True

    # LLM analysis gateway (ctx.analyze). Absent key = analyze() raises a
    # clean AnalysisError; nothing else in the engine depends on it.
    # LLM_BACKEND=claude-cli runs analyses through the local Claude Code CLI
    # on subscription auth instead of the API (see llm.ClaudeCliClient for
    # the measured latency/quota tradeoffs).
    anthropic_api_key: str = ""
    llm_backend: Literal["api", "claude-cli"] = "api"
    llm_model: str = "claude-opus-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"

    # Earnings data plane (Phase 10). EDGAR requires a real User-Agent with a
    # contact address (SEC fair-access rules); the calendar feed only starts
    # when a Finnhub key is present (free tier: finnhub.io/register).
    edgar_user_agent: str = "planetaria/0.1 (contact: matthewguo.x86@gmail.com)"
    finnhub_api_key: str = ""

    def validate_paper_lock(self) -> None:
        if self.keyless:
            if self.trading_mode != "paper":
                raise RuntimeError("KEYLESS=true is a paper-server preview knob only")
            self.alpaca_api_key = ""
            self.alpaca_secret_key = ""
        if self.trading_mode == "paper":
            # The paper server is hard-locked to paper trading, exactly as
            # v1 always was. Refuse to boot otherwise.
            if not self.alpaca_paper:
                raise RuntimeError(
                    "ALPACA_PAPER=false is not supported on the paper server. "
                    "Live trading runs only as the isolated TRADING_MODE="
                    "live_manual instance (see live.ps1)."
                )
            return
        # live_manual: the isolated manual-only live server. Every invariant
        # here is a boot refusal, not a warning — a misconfigured live
        # process must die, never degrade.
        if self.strategies_enabled:
            raise RuntimeError(
                "live_manual requires STRATEGIES_ENABLED=false — automation "
                "never runs in the live process."
            )
        if not self.live_account_name.startswith("live_"):
            raise RuntimeError(
                "live_manual requires LIVE_ACCOUNT_NAME=live_<name> matching "
                "an ALPACA_ACCOUNT_LIVE_<NAME>_API_KEY pair in .env."
            )
        for field in ("database_url", "redis_url"):
            if getattr(self, field) == Settings.model_fields[field].default:
                raise RuntimeError(
                    f"live_manual requires an explicit {field.upper()} distinct "
                    "from the paper server's default — the two enforcers must "
                    "never share state."
                )
        # Derived, never trusted from env: the live server talks to the live
        # endpoint by construction, and ALPACA_PAPER is ignored entirely.
        self.alpaca_paper = False
        # Likewise derived: a live enforcer that silently opened an empty
        # SQLite store would run zero monitors on real positions.
        self.sqlite_fallback = False


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_paper_lock()
    return settings
