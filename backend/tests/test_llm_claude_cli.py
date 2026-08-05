"""ClaudeCliClient: the subscription-auth CLI backend for ctx.analyze().
Subprocess is faked; the envelope shapes mirror real captures (2026-08-05,
claude 2.1.220)."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.services import llm as llm_mod
from app.services.llm import ClaudeCliClient, LLMAnalyst


class FakeProc:
    def __init__(self, stdout: bytes, returncode: int = 0, stderr: bytes = b""):
        self._out = stdout
        self._err = stderr
        self.returncode = returncode
        self.stdin_received: bytes | None = None

    async def communicate(self, input: bytes | None = None):
        self.stdin_received = input
        return self._out, self._err


def envelope(result: str, ok: bool = True) -> bytes:
    return json.dumps({
        "is_error": not ok,
        "subtype": "success" if ok else "error_during_execution",
        "result": result,
        "total_cost_usd": 0.2,
    }).encode()


@pytest.fixture
def spawn(monkeypatch):
    """Patch subprocess creation; records argv, returns rig-configured proc."""
    calls: list[list[str]] = []
    holder = {"proc": FakeProc(envelope('{"direction":"bullish"}'))}

    async def fake_exec(*args, **kwargs):
        calls.append(list(args))
        return holder["proc"]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(llm_mod.shutil, "which", lambda _: "C:/fake/claude.exe")
    return SimpleNamespace(calls=calls, holder=holder)


async def create(client, text="AMD beat and guided up."):
    return await client.messages.create(
        model="claude-sonnet-5", max_tokens=2048,
        system="analysis component",
        output_config={"format": {"type": "json_schema",
                                  "schema": {"type": "object"}}},
        messages=[{"role": "user", "content": text}],
    )


@pytest.mark.asyncio
async def test_flags_prompt_and_result(spawn):
    client = ClaudeCliClient()
    resp = await create(client, text="press release text " * 500)

    argv = spawn.calls[0]
    assert argv[0].endswith("claude.exe") and "-p" in argv
    assert "--json-schema" in argv and "--output-format" in argv
    # Tool denial is non-negotiable for untrusted text.
    assert argv[argv.index("--allowedTools") + 1] == ""
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
    # Prompt travels on stdin, never argv (Windows command-line limits).
    assert b"press release text" in spawn.holder["proc"].stdin_received
    assert not any("press release" in a for a in argv)

    assert resp.stop_reason == "end_turn"
    assert json.loads(resp.content[0].text) == {"direction": "bullish"}


@pytest.mark.asyncio
async def test_nonzero_exit_raises(spawn):
    spawn.holder["proc"] = FakeProc(b"", returncode=1, stderr=b"auth expired")
    with pytest.raises(RuntimeError, match="auth expired"):
        await create(ClaudeCliClient())


@pytest.mark.asyncio
async def test_error_envelope_raises(spawn):
    spawn.holder["proc"] = FakeProc(envelope("rate limited", ok=False))
    with pytest.raises(RuntimeError, match="rate limited"):
        await create(ClaudeCliClient())


def test_backend_selection_and_configured(monkeypatch):
    cli_settings = SimpleNamespace(anthropic_api_key="", llm_backend="claude-cli")
    api_settings = SimpleNamespace(anthropic_api_key="", llm_backend="api")

    monkeypatch.setattr(llm_mod.shutil, "which", lambda _: "C:/fake/claude.exe")
    assert LLMAnalyst(cli_settings, store=None).configured is True
    assert isinstance(LLMAnalyst(cli_settings, store=None)._get_client(),
                      ClaudeCliClient)
    assert LLMAnalyst(api_settings, store=None).configured is False

    monkeypatch.setattr(llm_mod.shutil, "which", lambda _: None)
    assert LLMAnalyst(cli_settings, store=None).configured is False
