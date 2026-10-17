"""Tests for scored selection and cross-source story grouping."""

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from telegram_translator.config_manager import ConfigManager
from telegram_translator.content_store import ContentItem, ContentStore
from telegram_translator.digest import DigestPipeline
from telegram_translator.summarizer import (
    ScoredItem,
    Story,
    Summarizer,
    rank_stories,
)


def _set_writer_role(monkeypatch):
    """Configure a hermetic WRITER role (never depend on ~/.secrets)."""
    monkeypatch.setenv("LLM_WRITER_MODEL", "test-writer-model")
    monkeypatch.setenv("LLM_WRITER_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_WRITER_API_KEY", "test-key")
    monkeypatch.delenv("LLM_WRITER_THINKING", raising=False)


def _item(source: str, title: str, item_id: int | None = None) -> ContentItem:
    """Build an in-memory content item."""
    return ContentItem(
        source_name=source,
        source_type="web",
        title=title,
        content=f"{title} body",
        content_hash=f"{source}:{title}",
        id=item_id,
    )


class FakeLLM:
    """Scripted structured-output provider keyed by schema name."""

    def __init__(self, scores=None, groups=None):
        self.scores = scores or {}
        self.groups = groups
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(self, system, user, schema_name, schema, **kwargs):
        self.calls.append((schema_name, system, user))
        if schema_name == "article_scores":
            source = user.split("Articles from ", 1)[1].split(":", 1)[0]
            return json.dumps({"items": self.scores[source]})
        if schema_name == "story_groups":
            return json.dumps({"stories": self.groups})
        if schema_name == "show_notes":
            return json.dumps({"lead": "Lead", "topics": []})
        return json.dumps({"sections": [{"topic": None, "text": "Hello"}]})


def _summarizer(monkeypatch, fake: FakeLLM, store=None) -> Summarizer:
    """Build a summarizer whose structured calls hit ``fake``."""
    _set_writer_role(monkeypatch)
    summarizer = Summarizer({"llm_role": "writer"}, store=store, podcast_name="daily")
    monkeypatch.setattr(summarizer, "_structured_chat", fake)
    return summarizer


def _scored(source: str, title: str, score: int) -> ScoredItem:
    """Build a scored in-memory content item."""
    return ScoredItem(item=_item(source, title), score=score)


# --- scoring -----------------------------------------------------------------


def test_unscored_and_out_of_range_entries(monkeypatch):
    """Missing items score 0; bad indices and repeats are ignored; clamped."""
    items = [_item("a", "one"), _item("a", "two"), _item("a", "three")]
    fake = FakeLLM(
        scores={
            "a": [
                {"n": 1, "score": 14, "reason": "huge"},
                {"n": 1, "score": 2, "reason": "repeat ignored"},
                {"n": 9, "score": 9, "reason": "out of range"},
                {"n": 3, "score": -4, "reason": "negative"},
            ]
        }
    )
    scored = asyncio.run(_summarizer(monkeypatch, fake).score_content(items, "x"))

    assert [s.item for s in scored] == items
    assert [s.score for s in scored] == [10, 0, 0]
    assert scored[1].reason == "not scored"


def test_scoring_runs_once_per_source(monkeypatch):
    """Per-source batching keeps small sources from being drowned out."""
    items = [_item("a", "one"), _item("b", "two"), _item("a", "three")]
    fake = FakeLLM(
        scores={
            "a": [
                {"n": 1, "score": 5, "reason": ""},
                {"n": 2, "score": 6, "reason": ""},
            ],
            "b": [{"n": 1, "score": 7, "reason": ""}],
        }
    )
    scored = asyncio.run(_summarizer(monkeypatch, fake).score_content(items, "x"))

    assert [c[0] for c in fake.calls] == ["article_scores", "article_scores"]
    assert [s.score for s in scored] == [5, 7, 6]


def test_threshold_is_inclusive(monkeypatch):
    """An item scoring exactly the threshold survives."""
    items = [_item("a", "edge"), _item("a", "below")]
    fake = FakeLLM(
        scores={
            "a": [
                {"n": 1, "score": 6, "reason": ""},
                {"n": 2, "score": 5, "reason": ""},
            ]
        }
    )
    selection = asyncio.run(
        _summarizer(monkeypatch, fake).select_top_stories(
            items, "x", threshold=6, max_stories=5
        )
    )

    assert selection.items == [items[0]]
    # One survivor: grouping needs no LLM call.
    assert [c[0] for c in fake.calls] == ["article_scores"]


def test_invalid_json_fails_loudly(monkeypatch):
    """A garbage payload raises instead of silently selecting nothing."""

    async def garbage(*args, **kwargs):
        return "not json"

    _set_writer_role(monkeypatch)
    summarizer = Summarizer({"llm_role": "writer"}, podcast_name="daily")
    monkeypatch.setattr(summarizer, "_structured_chat", garbage)

    with pytest.raises(ValueError, match="invalid JSON"):
        asyncio.run(summarizer.score_content([_item("a", "one")], "x"))


def test_scoring_cache_hit_skips_provider(monkeypatch, tmp_path):
    """A second identical run is served from the LLM cache."""
    store = ContentStore(tmp_path / "content.db")
    items = [_item("a", "one")]
    fake = FakeLLM(scores={"a": [{"n": 1, "score": 8, "reason": "big"}]})
    summarizer = _summarizer(monkeypatch, fake, store=store)

    first = asyncio.run(summarizer.score_content(items, "x"))
    second = asyncio.run(summarizer.score_content(items, "x"))

    assert len(fake.calls) == 1
    assert first[0].score == second[0].score == 8


# --- grouping ----------------------------------------------------------------


def test_grouping_places_every_item_exactly_once(monkeypatch):
    """Unplaced items become singletons; double placement keeps the first."""
    scored = [_scored("a", "one", 7), _scored("b", "two", 7), _scored("c", "three", 7)]
    fake = FakeLLM(
        groups=[
            {"label": "Story X", "items": [1, 2]},
            {"label": "Story Y", "items": [2, 99]},
        ]
    )
    stories = asyncio.run(_summarizer(monkeypatch, fake).group_stories(scored))

    assert [(s.label, [m.item.title for m in s.members]) for s in stories] == [
        ("Story X", ["one", "two"]),
        ("three", ["three"]),
    ]


# --- ranking -----------------------------------------------------------------


def test_rank_by_score_then_corroboration_then_count():
    """Best score wins; more distinct sources break ties; then article count."""
    lone = Story("lone", [_scored("a", "1", 8)])
    same_source_pair = Story("pair", [_scored("a", "2", 8), _scored("a", "3", 6)])
    corroborated = Story("corr", [_scored("a", "4", 8), _scored("b", "5", 5)])
    top = Story("top", [_scored("c", "6", 9)])

    ranked = rank_stories([lone, same_source_pair, corroborated, top], max_stories=3)

    assert [s.label for s in ranked] == ["top", "corr", "pair"]


# --- executive story map -----------------------------------------------------


def test_story_ranking_uses_anonymized_sources(monkeypatch):
    """The story map never shows the LLM a source's real name."""
    _set_writer_role(monkeypatch)
    summarizer = Summarizer({"llm_role": "writer"}, podcast_name="daily")
    captured = {}

    async def fake_chat(system, user, **kwargs):
        captured["user"] = user
        return "exec"

    monkeypatch.setattr(summarizer, "_chat", fake_chat)
    story = Story(
        "Summit talks", [_scored("naebnet", "t", 9), _scored("lenta", "u", 7)]
    )
    asyncio.run(
        summarizer.executive_summary(
            {"naebnet": "s1", "lenta": "s2"}, story_ranking=[story]
        )
    )

    assert "1. Summit talks (importance 9/10; covered by Source 1, Source 2)" in (
        captured["user"]
    )
    assert "naebnet" not in captured["user"]
    assert "lenta" not in captured["user"]


# --- pipeline integration ----------------------------------------------------


def _config_manager(tmp_path: Path, podcast: dict) -> ConfigManager:
    """Build an isolated manager with one podcast over two sources."""
    manager = ConfigManager.__new__(ConfigManager)
    manager.config_file = "<test>"
    manager.config = {
        "sources": {
            "telegram": {"news": {"prompt": "News"}},
            "web": {"general": {"url": "https://example.com/feed"}},
        },
        "podcasts": {"daily": {"llm_role": "writer", **podcast}},
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


def _run_pipeline(monkeypatch, tmp_path, podcast: dict, fake: FakeLLM):
    """Run summarize over four fixed items and capture executive prompts."""
    _set_writer_role(monkeypatch)
    exec_users = []

    async def fake_chat(self, system, user, **kwargs):
        if "STORY RANKING" in user or "individual source summaries" in user:
            exec_users.append(user)
        return "Summary"

    monkeypatch.setattr(
        Summarizer, "_structured_chat", lambda self, *a, **k: fake(*a, **k)
    )
    monkeypatch.setattr(Summarizer, "_chat", fake_chat)
    pipeline = DigestPipeline(_config_manager(tmp_path, podcast), podcast_name="daily")
    date = pipeline._today()
    for source, title in (
        ("general", "Summit report"),
        ("general", "Celebrity gossip"),
        ("news", "Summit opens"),
        ("news", "Quake hits"),
    ):
        pipeline.store.store_content(
            source_name=source, source_type="web", title=title, content=title
        )
    asyncio.run(pipeline.summarize(date, no_cache=True))
    return pipeline, date, exec_users


def test_pipeline_keeps_top_story_and_persists_report(monkeypatch, tmp_path):
    """Scored selection marks only kept-story items and stores a report."""
    fake = FakeLLM(
        scores={
            # Items arrive ordered by source name, so general precedes news.
            # general catalog: 1 Summit report, 2 Celebrity gossip
            "general": [
                {"n": 1, "score": 7, "reason": "summit"},
                {"n": 2, "score": 1, "reason": "gossip"},
            ],
            # news catalog: 1 Summit opens, 2 Quake hits
            "news": [
                {"n": 1, "score": 9, "reason": "summit"},
                {"n": 2, "score": 9, "reason": "quake"},
            ],
        },
        # Survivors in input order: Summit report, Summit opens, Quake hits.
        groups=[
            {"label": "Summit", "items": [1, 2]},
            {"label": "Quake", "items": [3]},
        ],
    )
    pipeline, date, exec_users = _run_pipeline(
        monkeypatch,
        tmp_path,
        {
            "sources": ["news", "general"],
            "selection_prompt": "World news",
            "selection_scoring": {"threshold": 6, "max_stories": 1},
        },
        fake,
    )

    digest = pipeline.store.get_digest(date, "daily")
    titles = {
        item.id: item.title
        for item in pipeline.store.get_content_since(
            pipeline._since(date), source_names=["news", "general"]
        )
    }
    kept = sorted(titles[i] for i in json.loads(digest.selected_item_ids))
    assert kept == ["Summit opens", "Summit report"]

    report = json.loads(digest.selection_report)
    assert report["threshold"] == 6.0
    assert [(s["label"], s["kept"]) for s in report["stories"]] == [
        ("Summit", True),
        ("Quake", False),
    ]
    assert len(report["items"]) == 4
    assert "STORY RANKING" in exec_users[0]
    assert "covered by Source 1, Source 2" in exec_users[0]


def test_pipeline_without_scoring_uses_yes_no_selection(monkeypatch, tmp_path):
    """Podcasts without selection_scoring keep the legacy selection path."""
    fake = FakeLLM()

    async def fake_chat(self, system, user, **kwargs):
        if "return ONLY the numbers" in system:
            return "1"
        return "Summary"

    _set_writer_role(monkeypatch)
    monkeypatch.setattr(
        Summarizer, "_structured_chat", lambda self, *a, **k: fake(*a, **k)
    )
    monkeypatch.setattr(Summarizer, "_chat", fake_chat)
    pipeline = DigestPipeline(
        _config_manager(
            tmp_path, {"sources": ["general"], "selection_prompt": "Anything"}
        ),
        podcast_name="daily",
    )
    date = pipeline._today()
    pipeline.store.store_content(
        source_name="general", source_type="web", title="Only", content="Only"
    )
    asyncio.run(pipeline.summarize(date, no_cache=True))

    digest = pipeline.store.get_digest(date, "daily")
    assert len(json.loads(digest.selected_item_ids)) == 1
    assert digest.selection_report == ""
    assert not [c for c in fake.calls if c[0] in ("article_scores", "story_groups")]


# --- config + migration ------------------------------------------------------


@pytest.mark.parametrize(
    "scoring, message",
    [
        ({"threshold": 11, "max_stories": 5}, "threshold"),
        ({"threshold": True, "max_stories": 5}, "threshold"),
        ({"threshold": 6, "max_stories": 0}, "max_stories"),
        ({"threshold": 6, "max_stories": 2.5}, "max_stories"),
        ({"threshold": 6, "max_stories": 5, "cap": 3}, "unknown keys"),
        ("yes", "mapping"),
    ],
)
def test_invalid_selection_scoring_is_rejected(tmp_path, scoring, message):
    """Malformed selection_scoring blocks fail at config load."""
    manager = _config_manager(
        tmp_path,
        {
            "sources": ["general"],
            "selection_prompt": "x",
            "selection_scoring": scoring,
        },
    )
    with pytest.raises(ValueError, match=message):
        manager.resolve_podcast_configs()


def test_selection_scoring_requires_selection_prompt(tmp_path):
    """Scoring without inclusion criteria is a config error."""
    manager = _config_manager(
        tmp_path,
        {
            "sources": ["general"],
            "selection_scoring": {"threshold": 6, "max_stories": 5},
        },
    )
    with pytest.raises(ValueError, match="requires a selection_prompt"):
        manager.resolve_podcast_configs()


def test_valid_selection_scoring_resolves(tmp_path):
    """A valid block resolves with a float threshold."""
    manager = _config_manager(
        tmp_path,
        {
            "sources": ["general"],
            "selection_prompt": "x",
            "selection_scoring": {"threshold": 6, "max_stories": 8},
        },
    )
    resolved = manager.resolve_podcast_configs()["daily"]
    assert resolved["selection_scoring"] == {"threshold": 6.0, "max_stories": 8}


def test_migration_adds_selection_report_column(tmp_path):
    """Older databases gain the selection_report column on open."""
    db = tmp_path / "content.db"
    ContentStore(db)
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE digests DROP COLUMN selection_report")
    ContentStore(db)
    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(digests)")}
    assert "selection_report" in columns
