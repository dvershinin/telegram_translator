"""Tests for per-podcast source scoping in the digest collector."""

import asyncio
from pathlib import Path

from telegram_translator.config_manager import ConfigManager
from telegram_translator.digest import DigestPipeline


def _config_manager(tmp_path: Path) -> ConfigManager:
    """Build an isolated manager with two disjoint podcast source sets."""
    manager = ConfigManager.__new__(ConfigManager)
    manager.config_file = "<test>"
    manager.config = {
        "sources": {
            "telegram": {"news": {"prompt": "News"}},
            "web": {
                "general": {"url": "https://example.com/feed"},
                "articles": {
                    "type": "wordpress",
                    "url": "https://example.com/wp-json/wp/v2/posts",
                },
            },
        },
        "podcasts": {
            "daily": {"sources": ["news", "general"]},
            "articles": {"sources": ["articles"]},
        },
    }
    manager.app_name = "telegram_translator"
    manager.app_author = "telegram_translator"
    for attribute in (
        "data_dir",
        "config_dir",
        "sessions_dir",
        "logs_dir",
        "databases_dir",
        "podcasts_dir",
    ):
        setattr(manager, attribute, tmp_path)
    return manager


def test_selected_podcast_collects_only_its_sources(tmp_path):
    """A targeted collection must not fetch unrelated global sources."""
    pipeline = DigestPipeline(
        _config_manager(tmp_path), podcast_name="articles"
    )

    assert pipeline.sources_config == {
        "telegram": {},
        "web": {
            "articles": {
                "type": "wordpress",
                "url": "https://example.com/wp-json/wp/v2/posts",
            }
        },
    }


def test_unscoped_collection_keeps_all_global_sources(tmp_path):
    """The nightly all-podcast collection retains the shared source pool."""
    manager = _config_manager(tmp_path)
    pipeline = DigestPipeline(manager)

    assert pipeline.sources_config == manager.config["sources"]


def test_successful_summarize_retry_clears_stale_error(monkeypatch, tmp_path):
    """A recovered digest must not display its previous failure message."""

    class FakeSummarizer:
        def __init__(self, *args, **kwargs):
            pass

        async def select_content(self, items, selection_prompt):
            return items

        async def summarize_source(self, *args, **kwargs):
            return "Source summary"

        async def executive_summary(self, *args, **kwargs):
            return "Executive summary"

        async def generate_show_notes(self, *args, **kwargs):
            return '{"lead":"Lead","topics":[]}'

        async def generate_podcast_script(self, *args, **kwargs):
            return "Podcast script"

    monkeypatch.setattr(
        "telegram_translator.digest.Summarizer", FakeSummarizer
    )
    pipeline = DigestPipeline(
        _config_manager(tmp_path), podcast_name="articles"
    )
    date = pipeline._today()
    pipeline.store.store_content(
        source_name="articles",
        source_type="web",
        title="Article",
        content="Article body",
        url="https://example.com/article",
    )
    pipeline.store.create_digest(date, "articles")
    pipeline.store.update_digest(
        date,
        "articles",
        status="error",
        error_message="Earlier failure",
    )

    asyncio.run(pipeline.summarize(date))

    digest = pipeline.store.get_digest(date, "articles")
    assert digest.status == "summarized"
    assert digest.error_message == ""
