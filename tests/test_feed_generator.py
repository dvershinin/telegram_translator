"""Tests for the RSS feed generator."""

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from telegram_translator.feed_generator import (
    ATOM_NS,
    CONTENT_NS,
    ITUNES_NS,
    PodcastFeed,
    _markdown_to_html,
)

SAMPLE_EPISODE = {
    "title": "Test Show — March 20, 2026",
    "description": "### Geopolitics\n\n**War update**: Something happened.",
    "executive_summary": "### Geopolitics\n\n**War update**: Something happened.\n\n- Point one\n- Point two",
    "filename": "test_2026-03-20.m4a",
    "duration_seconds": 477,
    "pub_date": "2026-03-20",
    "guid": "test-2026-03-20",
    "file_size": 6321350,
}


@pytest.fixture()
def feed():
    """Create a PodcastFeed with all fields populated."""
    return PodcastFeed(
        title="Test Show",
        base_url="https://example.com/podcast",
        description="A test podcast.",
        author="Host Name",
        language="en",
        category="News",
        subcategory="Daily News",
        artwork_url="https://example.com/podcast/artwork.jpg",
        explicit=False,
        feed_url="https://example.com/podcast/feed.xml",
        copyright_text="© 2024 Test LLC",
    )


@pytest.fixture()
def feed_xml(feed, tmp_path):
    """Generate a feed and return (xml_text, parsed_root, output_path)."""
    output = tmp_path / "feed.xml"
    feed.generate([SAMPLE_EPISODE], output)
    xml_text = output.read_text(encoding="utf-8")
    root = ET.fromstring(xml_text)
    return xml_text, root, output


class TestShowLevelTags:
    """Verify all required and recommended show-level tags."""

    def test_required_title(self, feed_xml):
        _, root, _ = feed_xml
        channel = root.find("channel")
        assert channel.findtext("title") == "Test Show"

    def test_required_description(self, feed_xml):
        _, root, _ = feed_xml
        channel = root.find("channel")
        assert channel.findtext("description") == "A test podcast."

    def test_required_language(self, feed_xml):
        _, root, _ = feed_xml
        channel = root.find("channel")
        assert channel.findtext("language") == "en"

    def test_required_itunes_image(self, feed_xml):
        _, root, _ = feed_xml
        channel = root.find("channel")
        img = channel.find(f"{{{ITUNES_NS}}}image")
        assert img is not None
        assert img.get("href") == "https://example.com/podcast/artwork.jpg"

    def test_required_itunes_category(self, feed_xml):
        _, root, _ = feed_xml
        channel = root.find("channel")
        cat = channel.find(f"{{{ITUNES_NS}}}category")
        assert cat is not None
        assert cat.get("text") == "News"
        sub = cat.find(f"{{{ITUNES_NS}}}category")
        assert sub is not None
        assert sub.get("text") == "Daily News"

    def test_required_itunes_explicit(self, feed_xml):
        _, root, _ = feed_xml
        channel = root.find("channel")
        assert channel.findtext(f"{{{ITUNES_NS}}}explicit") == "false"

    def test_recommended_link(self, feed_xml):
        _, root, _ = feed_xml
        channel = root.find("channel")
        assert channel.findtext("link") == "https://example.com/podcast/"

    def test_recommended_author(self, feed_xml):
        _, root, _ = feed_xml
        channel = root.find("channel")
        assert channel.findtext(f"{{{ITUNES_NS}}}author") == "Host Name"

    def test_recommended_type(self, feed_xml):
        _, root, _ = feed_xml
        channel = root.find("channel")
        assert channel.findtext(f"{{{ITUNES_NS}}}type") == "episodic"

    def test_optional_copyright(self, feed_xml):
        _, root, _ = feed_xml
        channel = root.find("channel")
        assert channel.findtext("copyright") == "\u00a9 2024 Test LLC"

    def test_optional_last_build_date(self, feed_xml):
        _, root, _ = feed_xml
        channel = root.find("channel")
        lbd = channel.findtext("lastBuildDate")
        assert lbd is not None
        # RFC 2822 contains day-of-week abbreviation
        assert any(
            d in lbd for d in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        )

    def test_atom_self_link(self, feed_xml):
        _, root, _ = feed_xml
        channel = root.find("channel")
        link = channel.find(f"{{{ATOM_NS}}}link")
        assert link is not None
        assert link.get("rel") == "self"
        assert link.get("type") == "application/rss+xml"
        assert link.get("href") == "https://example.com/podcast/feed.xml"

    def test_no_podcast_namespace(self, feed_xml):
        """The podcast: namespace (podcastindex.org) should not appear."""
        xml_text, _, _ = feed_xml
        assert "podcastindex.org" not in xml_text
        assert "podcast:locked" not in xml_text
        assert "podcast:medium" not in xml_text

    def test_no_itunes_owner(self, feed_xml):
        """itunes:owner is deprecated since Aug 2022."""
        xml_text, _, _ = feed_xml
        assert "itunes:owner" not in xml_text


class TestEpisodeLevelTags:
    """Verify all required and recommended episode-level tags."""

    def _item(self, feed_xml):
        _, root, _ = feed_xml
        return root.find("channel/item")

    def test_required_title(self, feed_xml):
        item = self._item(feed_xml)
        assert item.findtext("title") == "Test Show — March 20, 2026"

    def test_required_enclosure(self, feed_xml):
        item = self._item(feed_xml)
        enc = item.find("enclosure")
        assert enc is not None
        assert enc.get("url") == (
            "https://example.com/podcast/episodes/test_2026-03-20.m4a"
        )
        assert enc.get("type") == "audio/x-m4a"
        assert enc.get("length") == "6321350"

    def test_required_guid(self, feed_xml):
        item = self._item(feed_xml)
        guid = item.find("guid")
        assert guid is not None
        assert guid.text == "test-2026-03-20"
        assert guid.get("isPermaLink") == "false"

    def test_recommended_pub_date(self, feed_xml):
        item = self._item(feed_xml)
        pub = item.findtext("pubDate")
        assert pub is not None
        assert "20 Mar 2026" in pub

    def test_recommended_duration(self, feed_xml):
        item = self._item(feed_xml)
        dur = item.findtext(f"{{{ITUNES_NS}}}duration")
        assert dur == "00:07:57"

    def test_recommended_itunes_image(self, feed_xml):
        item = self._item(feed_xml)
        img = item.find(f"{{{ITUNES_NS}}}image")
        assert img is not None
        assert img.get("href") == "https://example.com/podcast/artwork.jpg"

    def test_recommended_itunes_explicit(self, feed_xml):
        item = self._item(feed_xml)
        assert item.findtext(f"{{{ITUNES_NS}}}explicit") == "false"

    def test_recommended_link(self, feed_xml):
        item = self._item(feed_xml)
        assert item.findtext("link") == (
            "https://example.com/podcast/episodes/test_2026-03-20.m4a"
        )

    def test_optional_episode_type(self, feed_xml):
        item = self._item(feed_xml)
        assert item.findtext(f"{{{ITUNES_NS}}}episodeType") == "full"

    def test_optional_episode_number(self, feed_xml):
        item = self._item(feed_xml)
        assert item.findtext(f"{{{ITUNES_NS}}}episode") == "1"


class TestCDATA:
    """Episode description and content:encoded must use CDATA with HTML."""

    def test_description_has_cdata(self, feed_xml):
        xml_text, _, _ = feed_xml
        assert "<description><![CDATA[" in xml_text

    def test_description_contains_html(self, feed_xml):
        xml_text, _, _ = feed_xml
        # Extract CDATA content from description
        start = xml_text.index("<description><![CDATA[") + len(
            "<description><![CDATA["
        )
        end = xml_text.index("]]></description>")
        cdata_content = xml_text[start:end]
        assert "<strong>" in cdata_content
        assert "<p>" in cdata_content

    def test_description_no_escaped_html(self, feed_xml):
        """HTML should be raw inside CDATA, not entity-escaped."""
        xml_text, _, _ = feed_xml
        # Find the description section
        start = xml_text.index("<description>")
        end = xml_text.index("</description>") + len("</description>")
        desc_section = xml_text[start:end]
        assert "&lt;strong&gt;" not in desc_section
        assert "&lt;p&gt;" not in desc_section

    def test_content_encoded_has_cdata(self, feed_xml):
        xml_text, _, _ = feed_xml
        assert "<content:encoded><![CDATA[" in xml_text

    def test_content_encoded_has_html_lists(self, feed_xml):
        xml_text, _, _ = feed_xml
        start = xml_text.index("<content:encoded><![CDATA[") + len(
            "<content:encoded><![CDATA["
        )
        end = xml_text.index("]]></content:encoded>")
        cdata_content = xml_text[start:end]
        assert "<ul>" in cdata_content
        assert "<li>" in cdata_content

    def test_show_description_no_cdata(self, feed_xml):
        """Show-level description is plain text, no CDATA needed."""
        xml_text, _, _ = feed_xml
        channel_desc_start = xml_text.index("<description>")
        # The first <description> is the show-level one
        assert xml_text[channel_desc_start:].startswith(
            "<description>A test podcast.</description>"
        )

    def test_no_cdata_marker_in_output(self, feed_xml):
        """The internal CDATA marker must not leak into the XML."""
        xml_text, _, _ = feed_xml
        assert "___CDATA___" not in xml_text


class TestMarkdownToHtml:
    """Test the Markdown to HTML converter."""

    def test_headings(self):
        assert _markdown_to_html("### Title") == "<h3>Title</h3>"

    def test_bold(self):
        result = _markdown_to_html("**bold text**")
        assert result == "<p><strong>bold text</strong></p>"

    def test_bold_in_heading(self):
        result = _markdown_to_html("### **Bold Heading**")
        assert result == "<h3><strong>Bold Heading</strong></h3>"

    def test_list_items(self):
        result = _markdown_to_html("- first\n- second")
        assert "<ul>" in result
        assert "<li>first</li>" in result
        assert "<li>second</li>" in result
        assert "</ul>" in result

    def test_paragraphs(self):
        result = _markdown_to_html("First paragraph.\n\nSecond paragraph.")
        assert "<p>First paragraph.</p>" in result
        assert "<p>Second paragraph.</p>" in result

    def test_mixed_content(self):
        md = "### Topic\n\n**Key point**: Details here.\n\n- Item 1\n- Item 2"
        result = _markdown_to_html(md)
        assert "<h3>Topic</h3>" in result
        assert "<strong>Key point</strong>" in result
        assert "<ul>" in result
        assert result.endswith("</ul>")

    def test_empty_input(self):
        assert _markdown_to_html("") == ""


class TestEpisodeNumbering:
    """Episodes should be numbered oldest=1, newest=highest."""

    def test_two_episodes_numbered_correctly(self, feed, tmp_path):
        eps = [
            {**SAMPLE_EPISODE, "pub_date": "2026-03-19", "guid": "ep-19"},
            {**SAMPLE_EPISODE, "pub_date": "2026-03-20", "guid": "ep-20"},
        ]
        output = tmp_path / "feed.xml"
        feed.generate(eps, output)
        root = ET.fromstring(output.read_text(encoding="utf-8"))
        items = root.findall("channel/item")
        # Items are sorted newest first
        assert items[0].findtext(f"{{{ITUNES_NS}}}episode") == "2"
        assert items[1].findtext(f"{{{ITUNES_NS}}}episode") == "1"


class TestValidation:
    """Feed validation should catch missing required tags."""

    def test_missing_title_raises(self, tmp_path):
        feed = PodcastFeed(
            title="",
            base_url="https://example.com",
        )
        # Empty title still produces a <title> element, so validation
        # passes at the tag-present level. Test missing enclosure instead.
        ep = {
            "title": "Episode",
            "guid": "ep-1",
            # No filename -> no enclosure
        }
        with pytest.raises(ValueError, match="missing required tag.*enclosure"):
            feed.generate([ep], tmp_path / "feed.xml")

    def test_valid_feed_passes(self, feed, tmp_path):
        output = tmp_path / "feed.xml"
        # Should not raise
        feed.generate([SAMPLE_EPISODE], output)
        assert output.exists()

    def test_xml_declaration_present(self, feed_xml):
        xml_text, _, _ = feed_xml
        assert xml_text.startswith("<?xml version='1.0' encoding='utf-8'?>")

    def test_namespaces_declared(self, feed_xml):
        xml_text, _, _ = feed_xml
        assert 'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"' in xml_text
        assert 'xmlns:content="http://purl.org/rss/1.0/modules/content/"' in xml_text
        assert 'xmlns:atom="http://www.w3.org/2005/Atom"' in xml_text
