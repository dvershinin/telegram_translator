"""Tests for token-only DeepSeek billing telemetry."""

import json
from types import SimpleNamespace

from telegram_translator.llm_usage import (
    USAGE_MARKER,
    deepseek_usage_event,
    record_deepseek_usage,
)


def _response(**overrides):
    values = {
        "id": "req-ttr-1",
        "created": 1786096800,
        "model": "deepseek-v4-flash",
        "usage": SimpleNamespace(
            prompt_tokens=11,
            prompt_cache_hit_tokens=7,
            prompt_cache_miss_tokens=4,
            completion_tokens=3,
        ),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_record_deepseek_usage_writes_content_free_jsonl(monkeypatch, tmp_path):
    path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("LLM_USAGE_LOG", str(path))
    assert record_deepseek_usage(
        _response(),
        requested_model="deepseek-v4-flash",
        project="telegram-translator",
        callsite="test",
    )
    raw = path.read_text()
    assert raw.startswith(USAGE_MARKER)
    payload = json.loads(raw.removeprefix(USAGE_MARKER))
    assert payload["usage"]["prompt_cache_hit_tokens"] == 7
    assert "content" not in raw


def test_usage_event_rejects_missing_exact_cache_split():
    response = _response(
        usage=SimpleNamespace(
            prompt_tokens=11,
            prompt_cache_hit_tokens=None,
            prompt_cache_miss_tokens=4,
            completion_tokens=3,
        )
    )
    assert (
        deepseek_usage_event(
            response, requested_model="deepseek-v4-flash", project="p", callsite="c"
        )
        is None
    )


def test_usage_event_ignores_non_deepseek_response():
    assert (
        deepseek_usage_event(
            _response(model="gpt-5-mini"),
            requested_model="gpt-5-mini",
            project="p",
            callsite="c",
        )
        is None
    )
