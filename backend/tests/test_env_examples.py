"""The two env examples are the whole environment of a server on the box.
Settings has extra="ignore", so a misspelled key would silently become a
missing setting: every key must be a Settings field or an Alpaca key pair,
and each example must boot in the mode it claims."""

import re
from pathlib import Path

import pytest
from dotenv import dotenv_values

from app.config import Settings

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
KEY_PATTERN = re.compile(r"^ALPACA_(API|SECRET)_KEY$|^ALPACA_ACCOUNT_.+_(API|SECRET)_KEY$")
PLACEHOLDER = re.compile(r"^(PK|AK)?\.\.\.$|^\.\.\.$|^$")


def _keys(path: Path) -> dict[str, str]:
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


@pytest.mark.parametrize("example", ["live/live.env.example", "paper/paper.env.example"])
def test_every_key_is_a_setting_or_an_alpaca_key(example):
    fields = {name.upper() for name in Settings.model_fields}
    for key in _keys(DEPLOY / example):
        assert key in fields or KEY_PATTERN.match(key), f"{example}: {key} is not a Settings field"


def _boot(example: str, monkeypatch) -> Settings:
    for key, value in _keys(DEPLOY / example).items():
        monkeypatch.setenv(key, "" if PLACEHOLDER.match(value) else value)
    settings = Settings(_env_file=None)
    settings.validate_paper_lock()
    return settings


def test_live_example_boots_live_manual(monkeypatch):
    s = _boot("live/live.env.example", monkeypatch)
    assert s.trading_mode == "live_manual" and s.alpaca_paper is False
    assert s.strategies_enabled is False and s.live_account_name == "live_roth"
    assert s.database_url.endswith("/trader_live") and s.redis_url.endswith("/1")


def test_paper_example_boots_paper(monkeypatch):
    s = _boot("paper/paper.env.example", monkeypatch)
    assert s.trading_mode == "paper" and s.alpaca_paper is True and s.strategies_enabled is True
    assert s.database_url.endswith(":5432/trader") and s.redis_url.endswith(":6379/0")
    assert s.llm_backend == "claude-cli" and s.headless is False
