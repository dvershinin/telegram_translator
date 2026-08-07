"""Tests for structured podcast script parsing and legacy fallback."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from telegram_translator.content_store import ContentItem
from telegram_translator.podcast_generator import (
    parse_structured_sections,
    sections_to_readable,
    split_script_by_topics,
)
from telegram_translator.show_notes import (
    parse_show_notes,
    render_body,
    render_description,
)
from telegram_translator.summarizer import Summarizer


def _set_writer_role(monkeypatch):
    """Configure a hermetic WRITER role.

    Tests must never depend on the developer's ~/.secrets being sourced, so
    every Summarizer construction supplies its own role variables.
    """
    monkeypatch.setenv("LLM_WRITER_MODEL", "test-writer-model")
    monkeypatch.setenv("LLM_WRITER_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_WRITER_API_KEY", "test-key")
    monkeypatch.delenv("LLM_WRITER_THINKING", raising=False)


def _make_script(sections: list[dict]) -> str:
    """Build a JSON script string from section dicts."""
    return json.dumps({"sections": sections})


class TestParseStructuredSections:
    """Tests for parse_structured_sections()."""

    def test_basic_sections(self):
        """Sections with topics produce correct boundaries."""
        script = _make_script([
            {"topic": None, "text": "Hello and welcome."},
            {"topic": "War", "text": "Fighting continued today."},
            {"topic": "Economy", "text": "Markets rose sharply."},
            {"topic": None, "text": "That wraps up today."},
        ])
        segments, boundaries = parse_structured_sections(script)
        assert len(segments) == 4
        assert boundaries == {1, 2}

    def test_topic_text_excluded_from_segments(self):
        """Topic names never appear in the segment text sent to TTS."""
        script = _make_script([
            {"topic": None, "text": "Welcome."},
            {"topic": "Ukraine Conflict", "text": "Intense fighting."},
        ])
        segments, _ = parse_structured_sections(script)
        for seg in segments:
            assert "Ukraine Conflict" not in seg
            assert "**" not in seg

    def test_empty_text_skipped(self):
        """Sections with empty text are skipped entirely."""
        script = _make_script([
            {"topic": None, "text": "Hello."},
            {"topic": "Empty", "text": ""},
            {"topic": "Real", "text": "Content here."},
        ])
        segments, boundaries = parse_structured_sections(script)
        assert len(segments) == 2
        assert segments == ["Hello.", "Content here."]
        # "Real" topic boundary is at index 1 (after "Hello.")
        assert boundaries == {1}

    def test_long_section_splits(self):
        """Long section text is split into multiple TTS segments."""
        long_text = ". ".join(["This is a sentence"] * 50) + "."
        script = _make_script([
            {"topic": None, "text": "Intro."},
            {"topic": "Big Topic", "text": long_text},
        ])
        segments, boundaries = parse_structured_sections(
            script, max_chars=200,
        )
        assert len(segments) > 2
        # Topic boundary marks the first segment of "Big Topic"
        assert 1 in boundaries

    def test_no_topics_no_boundaries(self):
        """Script with all null topics produces no boundaries."""
        script = _make_script([
            {"topic": None, "text": "Just talking."},
            {"topic": None, "text": "Still talking."},
        ])
        segments, boundaries = parse_structured_sections(script)
        assert len(segments) == 2
        assert boundaries == set()

    def test_single_section(self):
        """Single section works without errors."""
        script = _make_script([
            {"topic": None, "text": "One section only."},
        ])
        segments, boundaries = parse_structured_sections(script)
        assert segments == ["One section only."]
        assert boundaries == set()


class TestSectionsToReadable:
    """Tests for sections_to_readable()."""

    def test_readable_output(self):
        """Readable text includes topic headers in brackets."""
        script = _make_script([
            {"topic": None, "text": "Hello everyone."},
            {"topic": "War", "text": "Fighting today."},
            {"topic": None, "text": "Goodbye."},
        ])
        result = sections_to_readable(script)
        assert "[War]" in result
        assert "Hello everyone." in result
        assert "Fighting today." in result
        assert "Goodbye." in result
        # No markdown formatting
        assert "**" not in result


class TestLegacyFallback:
    """Verify split_script_by_topics() still works for old scripts."""

    def test_markdown_headers_detected(self):
        """Legacy ** headers produce topic boundaries."""
        script = (
            "Hello and welcome to today's briefing.\n\n"
            "**Ukraine Conflict**\n"
            "Fighting continued in the east.\n\n"
            "**Economy**\n"
            "Markets saw gains today.\n\n"
            "That wraps up today's show."
        )
        segments, boundaries = split_script_by_topics(script)
        assert len(segments) >= 3
        assert len(boundaries) >= 1

    def test_no_headers_no_boundaries(self):
        """Plain text without ** produces a single block."""
        script = "Just a simple script with no topic headers at all."
        segments, boundaries = split_script_by_topics(script)
        assert len(segments) == 1
        assert boundaries == set()


def _make_show_notes(
    lead: str, topics: list[dict],
) -> dict:
    """Build a parsed show-notes dict for renderer tests."""
    return {"lead": lead, "topics": topics}


_VASKE_VERDICT = "Вердикт Ваське"

# Host-direction labels that must never reach the public episode page.
# Listed here so the test failure pinpoints exactly which label leaked
# without the test itself becoming a hardcoded brand filter — these
# are the upstream prompt scaffolding strings, not source channels.
_SCAFFOLDING_MARKERS = (
    "Executive-обзор",
    "Тема:",
    "Что случилось:",
    "Угол для ведущего:",
    "Вопрос к ведущему:",
    "Скажу так:",
    "Почему это важно",
)


class TestShowNotesRendering:
    """Renderer tests for telegram_translator.show_notes."""

    def test_description_preserves_hyphens(self):
        """Hyphenated words round-trip verbatim into the description.

        The pre-fix regex stripped hyphens, turning `ИИ-кодинг` into
        `ИИкодинг`. The renderer reads from `lead` directly and must
        never strip any character.
        """
        sn = _make_show_notes(
            "Сегодня про ИИ-кодинг, open-source модели и AI-экономику.",
            [],
        )
        desc = render_description(sn)
        assert "ИИ-кодинг" in desc
        assert "open-source" in desc
        assert "AI-экономику" in desc
        # The collapsed forms produced by the old bug:
        assert "ИИкодинг" not in desc
        assert "opensource" not in desc

    def test_description_no_boilerplate(self):
        """Description pulls only from `lead` — not from topic bodies.

        Even if a paragraph contains upstream-prompt boilerplate
        (the host-brief artifact bled through historically), it must
        never appear in the description because the renderer never
        reads `topics` when building it.
        """
        sn = _make_show_notes(
            "Ясный тизер для слушателя.",
            [
                {
                    "headline": "Headline",
                    "paragraph": (
                        "Вот executive-обзор для сегодняшнего выпуска. "
                        "Факты, оценки, углы атаки."
                    ),
                    "verdict": "Verdict.",
                }
            ],
        )
        desc = render_description(sn)
        assert "executive-обзор" not in desc
        assert "Факты, оценки, углы атаки" not in desc
        assert desc == "Ясный тизер для слушателя."

    def test_description_caps_at_sentence_boundary(self):
        """Long leads truncate to ≤max_chars on a sentence boundary."""
        long_lead = (
            "Первое предложение про новости. "
            "Второе предложение про ещё что-то. "
            "Третье предложение, тоже важное. "
            + ("Дополнительный текст. " * 30)
        )
        desc = render_description(long_lead := _make_show_notes(
            long_lead, [],
        ))
        assert len(desc) <= 260
        assert desc[-1] in (".", "!", "?", "…")

    def test_description_hard_cut_when_no_sentence_boundary(self):
        """Lead without sentence enders falls back to hard cut + ellipsis."""
        sn = _make_show_notes("слово " * 200, [])
        desc = render_description(sn, max_chars=80)
        assert desc.endswith("…")
        assert len(desc) <= 81  # 80 chars + the ellipsis

    def test_body_has_headlines_and_verdict(self):
        """Each topic renders as ### headline + paragraph + verdict line."""
        sn = _make_show_notes(
            "Lead text.",
            [
                {"headline": "Тема А",
                 "paragraph": "Параграф А.",
                 "verdict": "Коротко А."},
                {"headline": "Тема Б",
                 "paragraph": "Параграф Б.",
                 "verdict": "Коротко Б."},
                {"headline": "Тема В",
                 "paragraph": "Параграф В.",
                 "verdict": "Коротко В."},
            ],
        )
        body = render_body(sn, _VASKE_VERDICT)
        assert body.count("### ") == 3
        assert body.count(f"**{_VASKE_VERDICT}:**") == 3
        for headline in ("Тема А", "Тема Б", "Тема В"):
            assert f"### {headline}" in body
        for verdict in ("Коротко А.", "Коротко Б.", "Коротко В."):
            assert f"**{_VASKE_VERDICT}:** {verdict}" in body

    def test_body_no_scaffolding(self):
        """Body composed from explicit fields cannot contain host-brief labels.

        Structural guarantee: the renderer interpolates only the three
        named fields per topic, so unless those fields themselves carry
        the markers (which the prompt forbids), the body is clean.
        """
        sn = _make_show_notes(
            "Lead.",
            [
                {"headline": "Headline",
                 "paragraph": "Plain factual paragraph.",
                 "verdict": "Short verdict."},
            ],
        )
        body = render_body(sn, _VASKE_VERDICT)
        for marker in _SCAFFOLDING_MARKERS:
            assert marker not in body, (
                f"scaffolding marker leaked into body: {marker!r}"
            )

    def test_body_no_channel_attribution_in_render(self):
        """Renderer never emits source-channel attribution from clean input."""
        sn = _make_show_notes(
            "Lead.",
            [
                {"headline": "Headline",
                 "paragraph": "Factual paragraph in the host's voice.",
                 "verdict": "Short verdict."},
            ],
        )
        body = render_body(sn, _VASKE_VERDICT)
        # These channel-attribution patterns appeared in the broken
        # published episodes; the renderer must never introduce them.
        for marker in ("Naebnet", "Канал приводит", "Канал добавляет"):
            assert marker not in body
        # Render output also must not contain the literal word "Source"
        # with a number — that's the LLM's internal anonymized label,
        # which the show-notes prompt is told to drop.
        assert "Source 1" not in body
        assert "Source 2" not in body

    def test_parse_show_notes_rejects_missing_fields(self):
        """parse_show_notes raises ValueError on bad shape."""
        with pytest.raises(ValueError):
            parse_show_notes('{"lead": "x"}')  # missing topics
        with pytest.raises(ValueError):
            parse_show_notes(
                '{"lead": "x", "topics": [{"headline": "h"}]}'
            )
        with pytest.raises(ValueError):
            parse_show_notes("not json at all")

    def test_parse_show_notes_strips_whitespace(self):
        """Valid JSON parses to a normalized dict."""
        raw = json.dumps({
            "lead": "  Lead.  ",
            "topics": [
                {"headline": "  H  ",
                 "paragraph": "  P  ",
                 "verdict": "  V  "},
            ],
        })
        result = parse_show_notes(raw)
        assert result["lead"] == "Lead."
        assert result["topics"][0] == {
            "headline": "H", "paragraph": "P", "verdict": "V",
        }


class TestSourceLeakPrevention:
    """The executive prompt must never see source channel names."""

    def test_executive_summary_anonymizes_sources(self, monkeypatch):
        """Source-channel names are aliased to `Source N` before the LLM.

        Root-cause fix for `Naebnet` / `Канал приводит` leaks in
        published show notes: the LLM literally cannot echo a brand
        name it was never shown.
        """
        _set_writer_role(monkeypatch)
        summarizer = Summarizer(
            {"llm_role": "writer"},
            title="Test",
            host_name="Test Host",
            podcast_name="test",
        )

        captured = {}

        async def fake_chat(system, user, **kwargs):
            captured["system"] = system
            captured["user"] = user
            return "fake summary"

        with patch.object(
            summarizer, "_chat", side_effect=fake_chat,
        ):
            asyncio.run(summarizer.executive_summary(
                {
                    "naebnet": "Per-source summary one.",
                    "another_channel": "Per-source summary two.",
                },
                source_biases={
                    "naebnet": "edgy Russian commentary",
                },
            ))

        assert "naebnet" not in captured["user"]
        assert "another_channel" not in captured["user"]
        assert "naebnet" not in captured["system"]
        # The anonymized labels are what the LLM does see.
        assert "Source 1" in captured["user"]
        assert "Source 2" in captured["user"]
        # And the bias context references the alias, not the brand.
        assert "Source 1: edgy Russian commentary" in captured["system"]

    def test_summarize_source_omits_source_name(self, monkeypatch):
        """summarize_source prompt no longer carries the brand name."""
        _set_writer_role(monkeypatch)
        summarizer = Summarizer(
            {"llm_role": "writer"},
            podcast_name="test",
        )

        captured = {}

        async def fake_chat(system, user, **kwargs):
            captured["system"] = system
            captured["user"] = user
            return "fake source summary"

        items = [
            ContentItem(
                source_name="naebnet",
                source_type="telegram",
                title="t1",
                content="body 1",
                content_hash="h1",
            )
        ]
        with patch.object(
            summarizer, "_chat", new=AsyncMock(side_effect=fake_chat),
        ):
            asyncio.run(summarizer.summarize_source(items, "naebnet"))

        assert "naebnet" not in captured["system"]
        assert "naebnet" not in captured["user"]
