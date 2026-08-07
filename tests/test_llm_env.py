"""Tests for environment-driven LLM role resolution."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from telegram_translator.llm_env import (
    DEFAULT_ROLE,
    LLMRole,
    ROLE_VARIABLE_SUFFIXES,
    completion_kwargs,
    is_deepseek,
    pins_default_temperature,
    require_role,
    thinking_extra_body,
)

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "telegram_translator"

# Source files that construct LLM requests.
LLM_SOURCE_FILES = ("summarizer.py", "translation_manager.py", "config_manager.py")

HARDCODED_MODEL_PATTERN = re.compile(
    r"""['"](?:gpt-[0-9]|deepseek-(?:chat|reasoner|v)|claude-|gemini-)[^'"]*['"]""",
    re.IGNORECASE,
)


def _set_role(monkeypatch, role: str, **overrides):
    defaults = {
        "MODEL": "test-model-x",
        "BASE_URL": "https://example.invalid/v1",
        "API_KEY": "test-key",
    }
    defaults.update(overrides)
    for suffix, value in defaults.items():
        name = f"LLM_{role.upper()}_{suffix}"
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def test_require_role_resolves_full_triple(monkeypatch):
    _set_role(monkeypatch, "writer")
    monkeypatch.delenv("LLM_WRITER_THINKING", raising=False)

    role = require_role("writer")

    assert role == LLMRole(
        model="test-model-x",
        base_url="https://example.invalid/v1",
        api_key="test-key",
        thinking=None,
    )


def test_require_role_reads_optional_thinking(monkeypatch):
    _set_role(monkeypatch, "fast")
    monkeypatch.setenv("LLM_FAST_THINKING", "enabled")

    assert require_role("fast").thinking == "enabled"


@pytest.mark.parametrize("suffix", ROLE_VARIABLE_SUFFIXES)
def test_require_role_fails_loud_on_each_missing_variable(monkeypatch, suffix):
    _set_role(monkeypatch, "writer", **{suffix: None})

    with pytest.raises(RuntimeError) as excinfo:
        require_role("writer")

    assert f"LLM_WRITER_{suffix}" in str(excinfo.value)
    assert "~/.secrets" in str(excinfo.value)


def test_require_role_treats_blank_as_missing(monkeypatch):
    _set_role(monkeypatch, "fast", MODEL="   ")

    with pytest.raises(RuntimeError, match="LLM_FAST_MODEL"):
        require_role("fast")


def test_default_role_is_writer():
    """Podcasts that name no role must land on long-form prose, not a guess."""
    assert DEFAULT_ROLE == "writer"


@pytest.mark.parametrize(
    "model,expected",
    [
        ("deepseek-v4-flash", True),
        ("deepseek-v4-pro", True),
        ("gpt-4o", False),
        ("gpt-5.6-luna", False),
        ("", False),
        (None, False),
    ],
)
def test_is_deepseek(model, expected):
    assert is_deepseek(model) is expected


def test_thinking_defaults_to_disabled_for_deepseek():
    """An omitted `thinking` field means ENABLED upstream - never omit it."""
    role = LLMRole("deepseek-v4-flash", "https://x/v1", "k", thinking=None)

    assert thinking_extra_body(role) == {"thinking": {"type": "disabled"}}


def test_thinking_honours_role_configuration():
    role = LLMRole("deepseek-v4-flash", "https://x/v1", "k", thinking="enabled")

    assert thinking_extra_body(role) == {"thinking": {"type": "enabled"}}


def test_thinking_is_not_sent_to_non_deepseek_providers():
    role = LLMRole("gpt-5.6-luna", "https://x/v1", "k", thinking="enabled")

    assert thinking_extra_body(role) is None


def test_thinking_does_not_mutate_caller_mapping():
    role = LLMRole("deepseek-v4-flash", "https://x/v1", "k")
    original = {"foo": 1}

    thinking_extra_body(role, original)

    assert original == {"foo": 1}


@pytest.mark.parametrize(
    "model,pinned",
    [("gpt-5.6-luna", True), ("gpt-5", True), ("gpt-4o", False), ("deepseek-v4-flash", False)],
)
def test_pins_default_temperature(model, pinned):
    assert pins_default_temperature(model) is pinned


def test_completion_kwargs_uses_max_completion_tokens():
    kwargs = completion_kwargs("deepseek-v4-flash", max_output_tokens=512)

    assert kwargs["max_completion_tokens"] == 512
    assert "max_tokens" not in kwargs


def test_completion_kwargs_drops_temperature_for_gpt5_family():
    """Repointing a role at GPT-5 must not start returning HTTP 400."""
    assert completion_kwargs("gpt-5.6-luna", 512, temperature=0.5) == {
        "max_completion_tokens": 512
    }


def test_completion_kwargs_keeps_temperature_elsewhere():
    assert completion_kwargs("deepseek-v4-flash", 512, temperature=0.5) == {
        "max_completion_tokens": 512,
        "temperature": 0.5,
    }


@pytest.mark.parametrize("filename", LLM_SOURCE_FILES)
def test_no_hardcoded_model_identifiers_in_source(filename):
    """Ratchet: model IDs live in the environment, never in source.

    vaske_daily silently rode the retired `deepseek-chat` alias for weeks
    because the model was a config default baked into source. Keep it
    impossible to reintroduce.
    """
    text = (PACKAGE_DIR / filename).read_text(encoding="utf-8")
    offenders = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        code = line.split("#", 1)[0]
        for match in HARDCODED_MODEL_PATTERN.finditer(code):
            offenders.append(f"{filename}:{lineno}: {match.group(0)}")

    assert not offenders, (
        "Hardcoded model identifier(s) found. Resolve a role via "
        "llm_env.require_role() instead:\n  " + "\n  ".join(offenders)
    )


class TestDateForPrompt:
    """The script must never guess the weekday.

    On 2026-08-07 a generated crosswire script opened with "It's Thursday,
    August 7th, 2026". That date is a Friday. The prompt had only the ISO
    date, so the model invented the day name.
    """

    def test_appends_the_real_weekday(self):
        from telegram_translator.summarizer import Summarizer

        assert Summarizer._date_for_prompt("2026-08-07") == "2026-08-07 (Friday)"

    @pytest.mark.parametrize(
        "date,weekday",
        [
            ("2026-08-03", "Monday"),
            ("2026-08-08", "Saturday"),
            ("2026-08-09", "Sunday"),
            ("2026-01-01", "Thursday"),
        ],
    )
    def test_weekday_matches_the_calendar(self, date, weekday):
        from telegram_translator.summarizer import Summarizer

        assert Summarizer._date_for_prompt(date) == f"{date} ({weekday})"

    @pytest.mark.parametrize("bad", ["", "not-a-date", "07/08/2026", None])
    def test_unparseable_input_passes_through(self, bad):
        from telegram_translator.summarizer import Summarizer

        assert Summarizer._date_for_prompt(bad) == bad
