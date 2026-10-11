"""Tests for the native Anthropic path of env-resolved LLM roles."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from telegram_translator import anthropic_chat
from telegram_translator.anthropic_chat import (
    FALLBACK_BETA,
    MAX_TOKENS_FLOOR,
    complete_json,
    complete_text,
    is_anthropic,
    role_effort,
)
from telegram_translator.llm_env import LLMRole
from telegram_translator.summarizer import PODCAST_SCRIPT_SCHEMA, Summarizer

ROLE = LLMRole("claude-test-model", "https://anthropic.invalid", "test-key")


def _response(*blocks, stop_reason="end_turn", stop_details=None):
    return SimpleNamespace(
        content=list(blocks), stop_reason=stop_reason, stop_details=stop_details
    )


def _block(kind, text=""):
    return SimpleNamespace(type=kind, text=text)


def _client(response):
    create = AsyncMock(return_value=response)
    client = SimpleNamespace(
        beta=SimpleNamespace(messages=SimpleNamespace(create=create))
    )
    return client, create


def test_is_anthropic_matches_claude_models_only():
    assert is_anthropic("claude-anything")
    assert is_anthropic("Claude-Upper")
    assert not is_anthropic("deepseek-v4-flash")
    assert not is_anthropic("gpt-5.6-luna")
    assert not is_anthropic(None)


def test_role_effort_reads_env(monkeypatch):
    monkeypatch.setenv("LLM_PODCAST_EFFORT", " Medium ")
    assert role_effort("podcast") == "medium"


def test_role_effort_unset_is_none(monkeypatch):
    monkeypatch.delenv("LLM_PODCAST_EFFORT", raising=False)
    assert role_effort("podcast") is None


def test_role_effort_rejects_unknown_level(monkeypatch):
    monkeypatch.setenv("LLM_PODCAST_EFFORT", "turbo")
    with pytest.raises(RuntimeError, match="LLM_PODCAST_EFFORT"):
        role_effort("podcast")


def test_complete_text_request_shape():
    client, create = _client(_response(_block("thinking"), _block("text", " hi ")))

    result = asyncio.run(
        complete_text(client, ROLE, "sys", "usr", max_tokens=1024, effort="low")
    )

    assert result == "hi"
    kwargs = create.await_args.kwargs
    assert kwargs["model"] == "claude-test-model"
    assert kwargs["system"] == "sys"
    assert kwargs["messages"] == [{"role": "user", "content": "usr"}]
    # Thinking shares max_tokens, so a small text ceiling is raised to the floor.
    assert kwargs["max_tokens"] == MAX_TOKENS_FLOOR
    assert kwargs["output_config"] == {"effort": "low"}
    assert kwargs["fallbacks"] == "default"
    assert kwargs["betas"] == [FALLBACK_BETA]
    # Current Claude models reject sampling parameters.
    assert "temperature" not in kwargs


def test_complete_text_keeps_larger_ceiling_and_omits_unset_effort():
    client, create = _client(_response(_block("text", "ok")))

    asyncio.run(complete_text(client, ROLE, "s", "u", max_tokens=MAX_TOKENS_FLOOR + 1))

    kwargs = create.await_args.kwargs
    assert kwargs["max_tokens"] == MAX_TOKENS_FLOOR + 1
    assert "output_config" not in kwargs


def test_complete_json_sends_schema():
    payload = json.dumps({"sections": []})
    client, create = _client(_response(_block("text", payload)))

    result = asyncio.run(
        complete_json(client, ROLE, "s", "u", PODCAST_SCRIPT_SCHEMA, max_tokens=8192)
    )

    assert json.loads(result) == {"sections": []}
    fmt = create.await_args.kwargs["output_config"]["format"]
    assert fmt == {"type": "json_schema", "schema": PODCAST_SCRIPT_SCHEMA}


@pytest.mark.parametrize(
    "response, message",
    [
        (
            _response(
                stop_reason="refusal",
                stop_details=SimpleNamespace(category="general_harms"),
            ),
            "declined.*general_harms",
        ),
        (_response(_block("text", "cut"), stop_reason="max_tokens"), "max_tokens"),
        (_response(_block("thinking")), "no text"),
    ],
)
def test_bad_stops_fail_loudly(response, message):
    client, _ = _client(response)
    with pytest.raises(ValueError, match=message):
        asyncio.run(complete_text(client, ROLE, "s", "u", max_tokens=10))


def _claude_summarizer(monkeypatch, effort=None):
    monkeypatch.setenv("LLM_PODCAST_MODEL", "claude-test-model")
    monkeypatch.setenv("LLM_PODCAST_BASE_URL", "https://anthropic.invalid")
    monkeypatch.setenv("LLM_PODCAST_API_KEY", "test-key")
    if effort:
        monkeypatch.setenv("LLM_PODCAST_EFFORT", effort)
    else:
        monkeypatch.delenv("LLM_PODCAST_EFFORT", raising=False)
    return Summarizer(
        {"llm_role": "podcast"}, title="T", host_name="H", podcast_name="test"
    )


def test_summarizer_routes_claude_role_through_anthropic(monkeypatch):
    summarizer = _claude_summarizer(monkeypatch, effort="high")
    assert summarizer.uses_anthropic
    assert summarizer.effort == "high"

    text = AsyncMock(return_value="claude says")
    monkeypatch.setattr(anthropic_chat, "complete_text", text)
    summarizer.client.chat.completions.create = AsyncMock(
        side_effect=AssertionError("OpenAI client must not be used for Claude")
    )

    assert asyncio.run(summarizer._chat("sys", "usr", max_tokens=1024)) == "claude says"
    assert text.await_args.kwargs["effort"] == "high"


def test_summarizer_structured_claude_path(monkeypatch):
    summarizer = _claude_summarizer(monkeypatch)
    structured = AsyncMock(return_value='{"sections": []}')
    monkeypatch.setattr(anthropic_chat, "complete_json", structured)

    result = asyncio.run(
        summarizer._structured_chat(
            "sys",
            "usr",
            "podcast_script",
            PODCAST_SCRIPT_SCHEMA,
            max_tokens=8192,
            temperature=0.7,
            model=summarizer.script_model,
        )
    )

    assert result == '{"sections": []}'
    assert structured.await_args.args[4] is PODCAST_SCRIPT_SCHEMA


def test_non_claude_role_has_no_anthropic_client(monkeypatch):
    monkeypatch.setenv("LLM_WRITER_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("LLM_WRITER_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_WRITER_API_KEY", "test-key")
    summarizer = Summarizer({"llm_role": "writer"}, title="T", host_name="H")
    assert summarizer.anthropic_client is None
    assert summarizer.effort is None
