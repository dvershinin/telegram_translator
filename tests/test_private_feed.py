"""Regression tests for token-protected private static podcast feeds."""

import logging
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

from click.testing import CliRunner
import pytest

from telegram_translator.cli import cli
from telegram_translator.content_store import ContentStore
from telegram_translator.feed_generator import ATOM_NS, ITUNES_NS
from telegram_translator.private_feed import read_private_feed_token
from telegram_translator.publisher import PodcastPublisher

_TOKEN = "A_secure-private-feed-token_1234567890"


def _credential(tmp_path: Path, token: str = _TOKEN) -> Path:
    """Create a safely permissioned private-feed credential file."""
    credential_dir = tmp_path / "credentials"
    credential_dir.mkdir(mode=0o700)
    credential_dir.chmod(0o700)
    token_file = credential_dir / "morning-token"
    token_file.write_text(token + "\n", encoding="ascii")
    token_file.chmod(0o600)
    return token_file


def _private_config(tmp_path: Path, token_file: Path) -> dict:
    """Build one resolved private podcast config."""
    return {
        "name": "morning",
        "title": "Morning Brief",
        "host_name": "Daniel",
        "language": "en",
        "destination_name": "private_morning",
        "destination_type": "static",
        "slug": "",
        "publish": {
            "base_url": "https://podcasts.example.com/morning-brief",
            "publish_dir": str(tmp_path / "publish"),
            "private_token_file": str(token_file),
            "show_description": "Private morning brief",
            "show_category": "News",
        },
    }


def test_private_feed_protects_every_generated_url_and_skips_html(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Put the token on every RSS asset URL without creating private HTML."""
    token_file = _credential(tmp_path)
    config = _private_config(tmp_path, token_file)
    store = ContentStore(tmp_path / "content.db")
    m4a_path = tmp_path / "publish" / "episodes" / "morning_2026-09-10.m4a"
    m4a_path.parent.mkdir(parents=True)
    m4a_path.write_bytes(b"audio")
    store.create_digest("2026-09-10", "morning")
    store.update_digest(
        "2026-09-10",
        "morning",
        executive_summary="Exact private script",
        podcast_script="Exact private script",
        m4a_path=str(m4a_path),
        duration_seconds=30,
        published_at="2026-09-10T00:00:00+00:00",
    )
    stale_index = tmp_path / "publish" / "index.html"
    stale_index.write_text("stale public page", encoding="utf-8")

    caplog.set_level(logging.INFO)
    feed_path = PodcastPublisher(config, store).rebuild_feed("morning")

    root = ElementTree.parse(feed_path).getroot()
    channel = root.find("channel")
    item = channel.find("item")
    urls = [
        channel.findtext("link"),
        channel.find(f"{{{ATOM_NS}}}link").get("href"),
        channel.find(f"{{{ITUNES_NS}}}image").get("href"),
        item.find("enclosure").get("url"),
        item.find(f"{{{ITUNES_NS}}}image").get("href"),
        item.findtext("link"),
    ]
    assert all(parse_qs(urlsplit(url).query) == {"token": [_TOKEN]} for url in urls)
    assert all(_TOKEN not in urlsplit(url).path for url in urls)
    assert not stale_index.exists()
    assert _TOKEN not in caplog.text


def test_private_feed_missing_credential_fails_without_public_fallback(
    tmp_path: Path,
) -> None:
    """Fail a private feed rebuild when its credential is unavailable."""
    token_file = tmp_path / "credentials" / "missing"
    token_file.parent.mkdir(mode=0o700)
    token_file.parent.chmod(0o700)
    config = _private_config(tmp_path, token_file)

    with pytest.raises(RuntimeError, match="unavailable or unsafe"):
        PodcastPublisher(config, ContentStore(tmp_path / "content.db")).rebuild_feed(
            "morning"
        )
    assert not (tmp_path / "publish" / "feed.xml").exists()


@pytest.mark.parametrize("unsafe", ["file_mode", "directory_mode", "symlink"])
def test_private_token_file_rejects_unsafe_files(
    tmp_path: Path,
    unsafe: str,
) -> None:
    """Reject permissive modes and symbolic-link credentials."""
    token_file = _credential(tmp_path)
    if unsafe == "file_mode":
        token_file.chmod(0o644)
    elif unsafe == "directory_mode":
        token_file.parent.chmod(0o755)
    else:
        target = token_file
        link = token_file.parent / "linked-token"
        link.symlink_to(target)
        token_file = link

    with pytest.raises(RuntimeError, match="unavailable or unsafe"):
        read_private_feed_token(str(token_file))


def test_private_destination_never_builds_public_root_index(tmp_path: Path) -> None:
    """Skip the public root index for every private static destination."""
    destination = {
        "type": "static",
        "publish_dir": str(tmp_path / "publish"),
        "private_token_file": str(tmp_path / "token"),
    }
    stale_index = tmp_path / "publish" / "index.html"
    stale_index.parent.mkdir()
    stale_index.write_text("stale public page", encoding="utf-8")
    result = PodcastPublisher.build_static_site_index(
        "private_morning",
        destination,
        [{"name": "morning", "slug": ""}],
        ContentStore(tmp_path / "content.db"),
    )
    assert result is None
    assert not stale_index.exists()


def test_digest_publish_exits_nonzero_when_destination_sync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Surface a failed destination sync as a nonzero CLI result."""
    podcast = {
        "name": "morning",
        "destination_name": "private_morning",
        "destination_type": "static",
        "slug": "",
        "publish": {},
    }
    destination = {
        "name": "private_morning",
        "type": "static",
        "publish_dir": str(tmp_path / "publish"),
        "sync_command": "false",
    }

    class ConfigStub:
        """Provide isolated publish configuration."""

        def get_database_path(self, database_name: str) -> str:
            """Return an isolated content database."""
            return str(tmp_path / database_name)

        def resolve_podcast_configs(self) -> dict:
            """Return the only podcast."""
            return {"morning": podcast}

        def resolve_destinations(self) -> dict:
            """Return its destination."""
            return {"private_morning": destination}

    async def fake_publish(self, podcast_name: str, date: str | None = None) -> str:
        return str(tmp_path / "episode.m4a")

    monkeypatch.setattr("telegram_translator.cli.ConfigManager", ConfigStub)
    monkeypatch.setattr(PodcastPublisher, "publish", fake_publish)
    monkeypatch.setattr(PodcastPublisher, "build_static_site_index", lambda *args: None)
    monkeypatch.setattr(PodcastPublisher, "run_destination_sync", lambda *args: False)

    result = CliRunner().invoke(
        cli,
        ["digest", "publish", "--podcast", "morning", "--date", "2026-09-10"],
    )

    assert result.exit_code != 0
    assert "Sync failed: private_morning" in result.output
