"""Tests for structured podcast script parsing and legacy fallback."""

import json

from telegram_translator.podcast_generator import (
    parse_structured_sections,
    sections_to_readable,
    split_script_by_topics,
)


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
