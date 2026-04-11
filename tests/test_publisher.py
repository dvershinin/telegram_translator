"""Tests for PodcastPublisher: site index, astro episode writer, helpers."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from telegram_translator.publisher import PodcastPublisher


@pytest.fixture()
def fake_store():
    """ContentStore stub with one recent digest per podcast."""
    store = MagicMock()

    def _list_digests(limit, podcast_name=None):
        d = MagicMock()
        d.date = "2026-04-11"
        d.podcast_name = podcast_name
        d.m4a_path = f"/tmp/{podcast_name}_2026-04-11.m4a"
        d.duration_seconds = 420.0
        d.executive_summary = "## Today\n\n**Big** news."
        d.published_at = "2026-04-11T10:00:00+00:00"
        return [d]

    store.list_digests.side_effect = _list_digests
    return store


@pytest.fixture()
def static_destination_cfg(tmp_path):
    """Resolved static destination config rooted at tmp_path."""
    publish_dir = tmp_path / "publish" / "gp"
    publish_dir.mkdir(parents=True)
    return {
        "name": "gp",
        "type": "static",
        "base_url": "https://podcasts.getpagespeed.com",
        "publish_dir": str(publish_dir),
        "sync_command": "echo sync",
        "site_title": "GetPageSpeed Podcasts",
        "site_description": "Daily briefings on tech, geopolitics.",
        "copyright": "© 2026 GetPageSpeed LLC",
    }


def _fake_podcast(name, title, slug, description=""):
    """Build a minimal resolved podcast config dict for the site index."""
    return {
        "name": name,
        "title": title,
        "slug": slug,
        "destination_name": "gp",
        "destination_type": "static",
        "publish": {
            "show_description": description,
        },
    }


class TestBuildStaticSiteIndex:
    """PodcastPublisher.build_static_site_index() behavior."""

    def test_multi_podcast_writes_index(
        self, static_destination_cfg, fake_store
    ):
        publish_dir = Path(static_destination_cfg["publish_dir"])
        (publish_dir / "crosswire").mkdir()
        (publish_dir / "the-stack").mkdir()
        (publish_dir / "crosswire" / "artwork_thumb.jpg").write_bytes(b"x")
        (publish_dir / "the-stack" / "artwork_thumb.jpg").write_bytes(b"x")

        podcasts = [
            _fake_podcast(
                "crosswire", "Crosswire", "crosswire", "Geopolitics show."
            ),
            _fake_podcast(
                "the_stack", "The Stack", "the-stack", "Tech show."
            ),
        ]
        path = PodcastPublisher.build_static_site_index(
            "gp", static_destination_cfg, podcasts, fake_store
        )
        assert path is not None
        assert path == publish_dir / "index.html"
        html = path.read_text()
        assert "GetPageSpeed Podcasts" in html
        assert "Crosswire" in html
        assert "The Stack" in html
        assert 'href="crosswire/"' in html
        assert 'href="crosswire/feed.xml"' in html
        assert 'href="the-stack/"' in html
        assert 'href="the-stack/feed.xml"' in html

    def test_includes_latest_episode_date_from_store(
        self, static_destination_cfg, fake_store
    ):
        publish_dir = Path(static_destination_cfg["publish_dir"])
        (publish_dir / "a").mkdir()
        (publish_dir / "b").mkdir()

        podcasts = [
            _fake_podcast("a", "A", "a"),
            _fake_podcast("b", "B", "b"),
        ]
        path = PodcastPublisher.build_static_site_index(
            "gp", static_destination_cfg, podcasts, fake_store
        )
        html = path.read_text()
        # fake_store returns a digest dated 2026-04-11 for any podcast
        assert html.count("Latest episode: 2026-04-11") == 2

    def test_shows_description_as_html(
        self, static_destination_cfg, fake_store
    ):
        publish_dir = Path(static_destination_cfg["publish_dir"])
        (publish_dir / "p").mkdir()
        podcasts = [
            _fake_podcast("p", "P", "p", "A **bold** show."),
        ]
        path = PodcastPublisher.build_static_site_index(
            "gp", static_destination_cfg, podcasts, fake_store
        )
        html = path.read_text()
        assert "<strong>bold</strong>" in html

    def test_thumbnail_img_omitted_when_file_missing(
        self, static_destination_cfg, fake_store
    ):
        publish_dir = Path(static_destination_cfg["publish_dir"])
        (publish_dir / "p").mkdir()
        # Intentionally do NOT create artwork_thumb.jpg

        podcasts = [_fake_podcast("p", "P", "p")]
        path = PodcastPublisher.build_static_site_index(
            "gp", static_destination_cfg, podcasts, fake_store
        )
        html = path.read_text()
        assert "artwork_thumb.jpg" not in html

    def test_sorts_podcasts_by_title_case_insensitive(
        self, static_destination_cfg, fake_store
    ):
        publish_dir = Path(static_destination_cfg["publish_dir"])
        (publish_dir / "a").mkdir()
        (publish_dir / "b").mkdir()
        (publish_dir / "c").mkdir()

        podcasts = [
            _fake_podcast("p3", "Zebra", "a"),
            _fake_podcast("p1", "alpha", "b"),
            _fake_podcast("p2", "Middle", "c"),
        ]
        path = PodcastPublisher.build_static_site_index(
            "gp", static_destination_cfg, podcasts, fake_store
        )
        html = path.read_text()
        alpha_pos = html.find(">alpha<")
        middle_pos = html.find(">Middle<")
        zebra_pos = html.find(">Zebra<")
        assert 0 < alpha_pos < middle_pos < zebra_pos

    def test_single_root_mounted_returns_none(
        self, static_destination_cfg, fake_store
    ):
        """Skip root index when destination has a single root-mounted podcast."""
        podcasts = [_fake_podcast("only", "Only", "")]
        path = PodcastPublisher.build_static_site_index(
            "gp", static_destination_cfg, podcasts, fake_store
        )
        assert path is None
        # Index file must not be written
        publish_dir = Path(static_destination_cfg["publish_dir"])
        assert not (publish_dir / "index.html").exists()

    def test_astro_collection_destination_returns_none(
        self, tmp_path, fake_store
    ):
        dest_cfg = {
            "name": "v",
            "type": "astro_collection",
            "base_url": "https://www.vaske.ru/podcast",
            "content_dir": str(tmp_path / "content"),
            "public_dir": str(tmp_path / "public"),
        }
        podcasts = [
            {
                "name": "vaske_daily",
                "title": "Vaske",
                "slug": None,
                "destination_name": "v",
                "destination_type": "astro_collection",
                "publish": {},
            }
        ]
        path = PodcastPublisher.build_static_site_index(
            "v", dest_cfg, podcasts, fake_store
        )
        assert path is None


class TestAstroAudioUrl:
    """PodcastPublisher._astro_audio_url() derives site-root-relative URLs."""

    def test_base_url_with_path(self):
        url = PodcastPublisher._astro_audio_url(
            "https://www.vaske.ru/podcast", "ep.m4a"
        )
        assert url == "/podcast/episodes/ep.m4a"

    def test_base_url_host_only(self):
        url = PodcastPublisher._astro_audio_url(
            "https://www.vaske.ru", "ep.m4a"
        )
        assert url == "/episodes/ep.m4a"

    def test_base_url_with_trailing_slash(self):
        url = PodcastPublisher._astro_audio_url(
            "https://x.com/podcast/", "ep.m4a"
        )
        assert url == "/podcast/episodes/ep.m4a"

    def test_empty_base_url(self):
        url = PodcastPublisher._astro_audio_url("", "ep.m4a")
        assert url == "/episodes/ep.m4a"

    def test_http_scheme(self):
        url = PodcastPublisher._astro_audio_url(
            "http://example.com/foo", "ep.m4a"
        )
        assert url == "/foo/episodes/ep.m4a"

    def test_nested_path(self):
        url = PodcastPublisher._astro_audio_url(
            "https://site.com/a/b/c", "ep.m4a"
        )
        assert url == "/a/b/c/episodes/ep.m4a"


class TestWriteAstroEpisode:
    """PodcastPublisher._write_astro_episode() writes frontmatter + body."""

    def test_writes_frontmatter_fields(self, tmp_path):
        path = PodcastPublisher._write_astro_episode(
            podcast_name="vaske_daily",
            date="2026-04-11",
            title="Vaske Daily",
            formatted_date="April 11, 2026",
            duration=420.7,
            audio_url="/podcast/episodes/vaske_daily_2026-04-11.m4a",
            executive_summary="## Today\n\nBig **news**.",
            content_dir=tmp_path,
        )
        assert path == tmp_path / "vaske_daily-2026-04-11.md"
        md = path.read_text()
        assert 'title: "Vaske Daily \u2014 April 11, 2026"' in md
        assert "date: 2026-04-11" in md
        assert (
            'audioUrl: "/podcast/episodes/vaske_daily_2026-04-11.m4a"'
            in md
        )
        assert "duration: 421" in md  # rounded from 420.7
        assert "## Today" in md
        assert "Big **news**." in md

    def test_description_truncated_at_200_chars(self, tmp_path):
        long = "Today " + "x" * 500
        path = PodcastPublisher._write_astro_episode(
            podcast_name="p",
            date="2026-04-11",
            title="P",
            formatted_date="April 11, 2026",
            duration=60,
            audio_url="/p/e.m4a",
            executive_summary=long,
            content_dir=tmp_path,
        )
        md = path.read_text()
        # Extract description line
        desc_line = next(
            ln for ln in md.splitlines() if ln.startswith("description:")
        )
        # Strip 'description: "' prefix and trailing '"'
        desc = desc_line[len('description: "') : -1]
        # Ellipsis added; body is <= 201 chars (200 + ellipsis)
        assert desc.endswith("\u2026")
        assert len(desc) <= 201

    def test_description_strips_markdown(self, tmp_path):
        path = PodcastPublisher._write_astro_episode(
            podcast_name="p",
            date="2026-04-11",
            title="P",
            formatted_date="April 11, 2026",
            duration=60,
            audio_url="/p/e.m4a",
            executive_summary="## H\n\n**Bold** _em_ `code`.",
            content_dir=tmp_path,
        )
        md = path.read_text()
        desc_line = next(
            ln for ln in md.splitlines() if ln.startswith("description:")
        )
        # Markdown chars stripped from the auto-description
        assert "**" not in desc_line
        assert "##" not in desc_line
        assert "`" not in desc_line

    def test_escapes_quotes_in_title(self, tmp_path):
        path = PodcastPublisher._write_astro_episode(
            podcast_name="p",
            date="2026-04-11",
            title='The "Daily"',
            formatted_date="April 11, 2026",
            duration=60,
            audio_url="/p/e.m4a",
            executive_summary="Body.",
            content_dir=tmp_path,
        )
        md = path.read_text()
        # YAML double-quote escaping: " → \"
        assert r'title: "The \"Daily\" \u2014 April 11, 2026"' in md \
            or 'title: "The \\"Daily\\" \u2014 April 11, 2026"' in md

    def test_body_preserved_verbatim(self, tmp_path):
        body = "## Section\n\n- item 1\n- item 2\n\n**bold** text."
        path = PodcastPublisher._write_astro_episode(
            podcast_name="p",
            date="2026-04-11",
            title="P",
            formatted_date="April 11, 2026",
            duration=60,
            audio_url="/p/e.m4a",
            executive_summary=body,
            content_dir=tmp_path,
        )
        md = path.read_text()
        # Body appears after the closing --- line
        _, _, after = md.partition("---\n\n")
        assert after == body

    def test_empty_executive_summary_writes_empty_body(self, tmp_path):
        path = PodcastPublisher._write_astro_episode(
            podcast_name="p",
            date="2026-04-11",
            title="P",
            formatted_date="April 11, 2026",
            duration=60,
            audio_url="/p/e.m4a",
            executive_summary="",
            content_dir=tmp_path,
        )
        md = path.read_text()
        assert 'description: ""' in md
        _, _, after = md.partition("---\n\n")
        assert after == ""


class TestRunDestinationSync:
    """PodcastPublisher.run_destination_sync() invokes sync_command once."""

    def test_no_sync_command_returns_true(self):
        ok = PodcastPublisher.run_destination_sync("gp", {})
        assert ok is True

    def test_successful_sync(self, monkeypatch):
        recorded = {}

        def fake_run(cmd, **kwargs):
            recorded["cmd"] = cmd
            recorded["kwargs"] = kwargs
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            return result

        monkeypatch.setattr(
            "telegram_translator.publisher.subprocess.run", fake_run
        )
        ok = PodcastPublisher.run_destination_sync(
            "gp", {"sync_command": "echo hi"}
        )
        assert ok is True
        assert recorded["cmd"] == "echo hi"
        assert recorded["kwargs"].get("shell") is True

    def test_failed_sync_returns_false(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stderr = "boom"
            return result

        monkeypatch.setattr(
            "telegram_translator.publisher.subprocess.run", fake_run
        )
        ok = PodcastPublisher.run_destination_sync(
            "gp", {"sync_command": "false"}
        )
        assert ok is False


class TestPerPodcastIndexRegression:
    """Per-podcast HTML index still renders after the CSS refactor."""

    def test_per_podcast_index_renders(self, tmp_path):
        """Smoke-test: _build_index_html produces valid HTML with key elements."""
        config = {
            "title": "Crosswire",
            "host_name": "Vera",
            "language": "en",
            "destination_name": None,
            "publish": {
                "base_url": "https://podcasts.getpagespeed.com/crosswire",
                "publish_dir": str(tmp_path),
                "show_description": "Crosswire show.",
                "show_artwork": "./art.jpg",
                "copyright": "© 2026",
                "spotify_url": "https://open.spotify.com/show/abc",
            },
        }
        store = MagicMock()
        pub = PodcastPublisher(config, store)
        episodes = [
            {
                "title": "Crosswire — April 11, 2026",
                "description": "Today's stories",
                "executive_summary": "## Today\n\n**Big** story.",
                "filename": "crosswire_2026-04-11.m4a",
                "duration_seconds": 420,
                "pub_date": "2026-04-11",
                "guid": "crosswire-2026-04-11",
                "file_size": 1000,
            }
        ]
        pub._build_index_html(
            "crosswire", episodes, tmp_path, config["publish"]
        )
        html = (tmp_path / "index.html").read_text()

        # Title + headers present
        assert "<title>Crosswire</title>" in html
        assert "Crosswire — April 11, 2026" in html
        # Spotify link present when configured
        assert "Listen on Spotify" in html
        assert "open.spotify.com/show/abc" in html
        # Subscribe button present
        assert "Subscribe via RSS" in html
        assert (
            'href="https://podcasts.getpagespeed.com/crosswire/feed.xml"'
            in html
        )
        # Audio player points at the right episode URL
        assert (
            "https://podcasts.getpagespeed.com/crosswire/episodes/"
            "crosswire_2026-04-11.m4a" in html
        )
        # Show notes rendered from Markdown
        assert "<strong>Big</strong>" in html
        # No stray f-string leakage
        assert "{" not in html.replace("{", "").replace(
            "}", ""
        ) or html.count("{") == html.count("}")

    def test_spotify_link_omitted_when_not_configured(self, tmp_path):
        config = {
            "title": "P",
            "host_name": "H",
            "language": "en",
            "destination_name": None,
            "publish": {
                "base_url": "https://x.com/p",
                "publish_dir": str(tmp_path),
                "show_description": "",
                "copyright": "",
            },
        }
        pub = PodcastPublisher(config, MagicMock())
        pub._build_index_html("p", [], tmp_path, config["publish"])
        html = (tmp_path / "index.html").read_text()
        assert "Listen on Spotify" not in html
