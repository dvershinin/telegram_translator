"""RSS 2.0 podcast feed generator with iTunes namespace support.

Generates feeds compliant with:
- RSS 2.0 spec (rssboard.org)
- Apple Podcasts RSS requirements (podcasters.apple.com/support/823)
- Atom self-link (PSP-1 best practice)
- content:encoded (Apple-supported namespace)
"""

import html as html_mod
import logging
import re
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.etree.ElementTree import (
    Element,
    ElementTree,
    SubElement,
    indent,
    register_namespace,
)

logger = logging.getLogger(__name__)

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"

register_namespace("itunes", ITUNES_NS)
register_namespace("atom", ATOM_NS)
register_namespace("content", CONTENT_NS)

# Marker prefix for elements whose text should be wrapped in CDATA.
# ElementTree escapes it as plain text; post-processing replaces with CDATA.
_CDATA_MARK = "___CDATA___"

# Apple Podcasts required show-level tags.
_REQUIRED_SHOW_TAGS = {
    "title",
    "description",
    "language",
    f"{{{ITUNES_NS}}}image",
    f"{{{ITUNES_NS}}}category",
    f"{{{ITUNES_NS}}}explicit",
}

# Apple Podcasts required episode-level tags.
_REQUIRED_EPISODE_TAGS = {
    "title",
    "enclosure",
    "guid",
}


def _markdown_to_html(text: str) -> str:
    """Convert basic Markdown to HTML for CDATA sections.

    Apple supports: <p>, <ul>, <ol>, <li>, <a>, <strong>.
    """
    lines = text.split("\n")
    html_parts: list[str] = []
    in_list = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            continue

        # Headings
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading_match:
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            level = len(heading_match.group(1))
            content = heading_match.group(2)
            content = re.sub(
                r"\*{1,3}(.+?)\*{1,3}", r"<strong>\1</strong>", content,
            )
            html_parts.append(f"<h{level}>{content}</h{level}>")
            continue

        # List items
        list_match = re.match(r"^[-*]\s+(.+)$", stripped)
        if list_match:
            if not in_list:
                html_parts.append("<ul>")
                in_list = True
            content = list_match.group(1)
            content = re.sub(
                r"\*{1,3}(.+?)\*{1,3}", r"<strong>\1</strong>", content,
            )
            html_parts.append(f"<li>{content}</li>")
            continue

        if in_list:
            html_parts.append("</ul>")
            in_list = False

        # Regular paragraph — convert bold
        para = re.sub(
            r"\*{1,3}(.+?)\*{1,3}", r"<strong>\1</strong>", stripped,
        )
        html_parts.append(f"<p>{para}</p>")

    if in_list:
        html_parts.append("</ul>")

    return "".join(html_parts)


def _inject_cdata(xml_text: str) -> str:
    """Replace CDATA marker with actual CDATA sections in serialized XML.

    ElementTree cannot produce CDATA natively, so we mark elements during
    tree construction and post-process the serialized output.

    Args:
        xml_text: Serialized XML string with escaped CDATA markers.

    Returns:
        XML string with proper CDATA sections.
    """
    escaped_mark = html_mod.escape(_CDATA_MARK)

    def _replace(m: re.Match) -> str:
        tag_open = m.group(1)
        escaped_html = m.group(2)
        tag_close = m.group(3)
        raw_html = html_mod.unescape(escaped_html)
        return f"{tag_open}<![CDATA[{raw_html}]]>{tag_close}"

    return re.sub(
        r"(<[^>]+>)" + re.escape(escaped_mark) + r"(.*?)(</[^>]+>)",
        _replace,
        xml_text,
        flags=re.DOTALL,
    )


def _validate_feed(rss: Element):
    """Validate the feed against Apple Podcasts requirements.

    Args:
        rss: Root RSS element.

    Raises:
        ValueError: If required tags are missing.
    """
    channel = rss.find("channel")
    if channel is None:
        raise ValueError("Feed missing <channel> element")

    errors: list[str] = []

    # Show-level required tags
    for tag in _REQUIRED_SHOW_TAGS:
        el = channel.find(tag)
        if el is None:
            errors.append(f"Show missing required tag: <{tag}>")
        elif tag == f"{{{ITUNES_NS}}}image" and not el.get("href"):
            errors.append("Show <itunes:image> missing href attribute")
        elif tag == f"{{{ITUNES_NS}}}category" and not el.get("text"):
            errors.append("Show <itunes:category> missing text attribute")

    # Episode-level required tags
    for i, item in enumerate(channel.findall("item")):
        ep_title = (
            item.findtext("title") or f"episode #{i + 1}"
        )
        for tag in _REQUIRED_EPISODE_TAGS:
            el = item.find(tag)
            if el is None:
                errors.append(
                    f"Episode '{ep_title}' missing required tag: <{tag}>"
                )

        # Enclosure must have url, length, type
        enc = item.find("enclosure")
        if enc is not None:
            for attr in ("url", "length", "type"):
                if not enc.get(attr):
                    errors.append(
                        f"Episode '{ep_title}' <enclosure> "
                        f"missing '{attr}' attribute"
                    )

    if errors:
        raise ValueError(
            "Feed validation failed:\n  " + "\n  ".join(errors)
        )

    logger.info("Feed validation passed")


class PodcastFeed:
    """Generate an RSS 2.0 podcast feed with iTunes extensions."""

    def __init__(
        self,
        title: str,
        base_url: str,
        description: str = "",
        author: str = "",
        language: str = "en",
        category: str = "News",
        subcategory: str = "",
        artwork_url: str = "",
        explicit: bool = False,
        feed_url: str = "",
        copyright_text: str = "",
        owner_name: str = "",
        owner_email: str = "",
    ):
        """Initialize the feed generator.

        Args:
            title: Show title.
            base_url: Base URL where episodes are hosted.
            description: Show description (up to 4000 chars for Apple).
            author: Show author / host name.
            language: ISO 639-1 language code.
            category: iTunes top-level category.
            subcategory: iTunes subcategory.
            artwork_url: URL to show artwork (1400-3000px JPEG/PNG).
            explicit: Whether the show contains explicit content.
            feed_url: Canonical feed URL for atom:link rel="self".
            copyright_text: Copyright notice text.
            owner_name: Show owner name for itunes:owner.
            owner_email: Show owner email for itunes:owner.
        """
        self.title = title
        self.base_url = base_url.rstrip("/")
        self.description = description
        self.author = author
        self.language = language
        self.category = category
        self.subcategory = subcategory
        self.artwork_url = artwork_url or f"{self.base_url}/artwork.jpg"
        self.explicit = explicit
        self.feed_url = feed_url or f"{self.base_url}/feed.xml"
        self.copyright_text = copyright_text
        self.owner_name = owner_name
        self.owner_email = owner_email

    def generate(
        self,
        episodes: list[dict],
        output_path: Path,
    ) -> Path:
        """Build, validate, and write the RSS feed XML.

        Args:
            episodes: List of episode dicts with keys: title, description,
                filename, duration_seconds, pub_date, guid, file_size.
                Optional: episode_number, executive_summary.
            output_path: Where to write the feed.xml.

        Returns:
            The output path.

        Raises:
            ValueError: If the generated feed fails validation.
        """
        rss = Element("rss", {"version": "2.0"})
        channel = SubElement(rss, "channel")

        # --- Show-level required tags ---
        SubElement(channel, "title").text = self.title
        SubElement(channel, "link").text = self.base_url + "/"
        SubElement(channel, "description").text = self.description
        SubElement(channel, "language").text = self.language

        img = SubElement(channel, f"{{{ITUNES_NS}}}image")
        img.set("href", self.artwork_url)

        cat = SubElement(channel, f"{{{ITUNES_NS}}}category")
        cat.set("text", self.category)
        if self.subcategory:
            sub = SubElement(cat, f"{{{ITUNES_NS}}}category")
            sub.set("text", self.subcategory)

        SubElement(channel, f"{{{ITUNES_NS}}}explicit").text = (
            "true" if self.explicit else "false"
        )

        # --- Show-level recommended/optional tags ---
        SubElement(
            channel, f"{{{ITUNES_NS}}}author"
        ).text = self.author
        SubElement(
            channel, f"{{{ITUNES_NS}}}type"
        ).text = "episodic"
        SubElement(channel, "lastBuildDate").text = format_datetime(
            datetime.now(tz=timezone.utc)
        )
        if self.copyright_text:
            SubElement(channel, "copyright").text = self.copyright_text

        if self.owner_email:
            owner = SubElement(channel, f"{{{ITUNES_NS}}}owner")
            if self.owner_name:
                SubElement(owner, f"{{{ITUNES_NS}}}name").text = self.owner_name
            SubElement(owner, f"{{{ITUNES_NS}}}email").text = self.owner_email

        # Atom self-link (PSP-1 best practice, harmless for Apple)
        SubElement(
            channel,
            f"{{{ATOM_NS}}}link",
            {
                "href": self.feed_url,
                "rel": "self",
                "type": "application/rss+xml",
            },
        )

        # --- Episodes (newest first) ---
        sorted_episodes = sorted(
            episodes,
            key=lambda e: e.get("pub_date", ""),
            reverse=True,
        )
        total = len(sorted_episodes)
        for i, ep in enumerate(sorted_episodes):
            if "episode_number" not in ep:
                ep = {**ep, "episode_number": total - i}
            self._add_episode(channel, ep)

        # Validate before writing
        _validate_feed(rss)

        tree = ElementTree(rss)
        indent(tree, space="  ")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tree.write(
            str(output_path),
            encoding="utf-8",
            xml_declaration=True,
        )

        # Post-process: replace CDATA markers with actual CDATA sections
        xml_text = output_path.read_text(encoding="utf-8")
        xml_text = _inject_cdata(xml_text)
        output_path.write_text(xml_text, encoding="utf-8")

        logger.info(
            "Feed written: %s (%d episodes)", output_path, len(episodes)
        )
        return output_path

    def _add_episode(self, channel: Element, ep: dict):
        """Add a single episode item to the channel.

        Args:
            channel: The RSS channel element.
            ep: Episode dict.
        """
        item = SubElement(channel, "item")

        # --- Episode required tags ---
        SubElement(item, "title").text = ep.get("title", "Untitled")

        filename = ep.get("filename", "")
        if filename:
            enclosure = SubElement(item, "enclosure")
            enclosure.set(
                "url", f"{self.base_url}/episodes/{filename}"
            )
            enclosure.set("type", "audio/x-m4a")
            enclosure.set("length", str(ep.get("file_size", 0)))

        guid_el = SubElement(item, "guid")
        guid_el.text = ep.get("guid", "")
        guid_el.set("isPermaLink", "false")

        # --- Episode recommended tags ---
        pub_date = ep.get("pub_date")
        if pub_date:
            if isinstance(pub_date, str):
                dt = datetime.strptime(
                    pub_date, "%Y-%m-%d"
                ).replace(hour=12, tzinfo=timezone.utc)
            else:
                dt = pub_date
            SubElement(item, "pubDate").text = format_datetime(dt)

        # Episode description: HTML in CDATA (per Apple spec)
        ep_html = ""
        executive_summary = ep.get("executive_summary", "")
        if executive_summary:
            ep_html = _markdown_to_html(executive_summary)
        description = ep.get("description", "")
        if ep_html:
            SubElement(item, "description").text = (
                _CDATA_MARK + ep_html
            )
        elif description:
            SubElement(item, "description").text = description

        duration = ep.get("duration_seconds", 0)
        if duration:
            minutes, seconds = divmod(int(duration), 60)
            hours, minutes = divmod(minutes, 60)
            SubElement(
                item, f"{{{ITUNES_NS}}}duration"
            ).text = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

        ep_img = SubElement(item, f"{{{ITUNES_NS}}}image")
        ep_img.set("href", self.artwork_url)

        SubElement(item, f"{{{ITUNES_NS}}}explicit").text = (
            "true" if self.explicit else "false"
        )

        if filename:
            SubElement(
                item, "link"
            ).text = f"{self.base_url}/episodes/{filename}"

        # --- Episode optional tags ---
        SubElement(
            item, f"{{{ITUNES_NS}}}episodeType"
        ).text = "full"

        episode_number = ep.get("episode_number")
        if episode_number:
            SubElement(
                item, f"{{{ITUNES_NS}}}episode"
            ).text = str(episode_number)

        # HTML show notes (also CDATA)
        if ep_html:
            content_el = SubElement(
                item, f"{{{CONTENT_NS}}}encoded"
            )
            content_el.text = _CDATA_MARK + ep_html
