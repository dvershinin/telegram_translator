"""Tests for ContentStore exclude_used / exclude_podcast filtering."""

from datetime import datetime, timedelta, timezone

import pytest

from telegram_translator.content_store import ContentStore


@pytest.fixture
def store(tmp_path):
    """Create a ContentStore backed by a temporary database."""
    return ContentStore(tmp_path / "test.db")


def _insert_items(store, count=3, source="lenta"):
    """Insert test content items and return their IDs."""
    ids = []
    for i in range(count):
        store.store_content(
            source_name=source,
            source_type="web",
            content=f"article content {source} {i}",
            title=f"Article {i}",
        )
    # Retrieve IDs by querying back
    since = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    items = store.get_content_since(since, source_name=source)
    return [item.id for item in items]


class TestExcludeUsedGlobal:
    """Backward compat: exclude_used without exclude_podcast is global."""

    def test_excludes_items_used_by_any_podcast(self, store):
        ids = _insert_items(store, count=3)
        store.mark_items_used("2026-03-22", "podcast_a", [ids[0]])

        since = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        result = store.get_content_since(since, exclude_used=True)

        result_ids = {item.id for item in result}
        assert ids[0] not in result_ids
        assert ids[1] in result_ids
        assert ids[2] in result_ids


class TestExcludeUsedScopedToPodcast:
    """Items used by podcast A should still be visible to podcast B."""

    def test_items_visible_to_other_podcast(self, store):
        ids = _insert_items(store, count=3)
        store.mark_items_used("2026-03-22", "podcast_a", [ids[0], ids[1]])

        since = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        result = store.get_content_since(
            since, exclude_used=True, exclude_podcast="podcast_b",
        )

        result_ids = {item.id for item in result}
        # All items visible because podcast_b hasn't used any
        assert ids[0] in result_ids
        assert ids[1] in result_ids
        assert ids[2] in result_ids

    def test_with_source_names_filter(self, store):
        lenta_ids = _insert_items(store, count=2, source="lenta")
        vc_ids = _insert_items(store, count=2, source="vc_ru")
        store.mark_items_used("2026-03-22", "crosswire", lenta_ids)

        since = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        result = store.get_content_since(
            since,
            source_names=["lenta", "vc_ru"],
            exclude_used=True,
            exclude_podcast="the_stack",
        )

        result_ids = {item.id for item in result}
        # the_stack sees all items — crosswire's usage doesn't affect it
        for item_id in lenta_ids + vc_ids:
            assert item_id in result_ids


class TestExcludeUsedSamePodcast:
    """Items used by podcast A should be excluded from podcast A."""

    def test_excludes_own_items(self, store):
        ids = _insert_items(store, count=3)
        store.mark_items_used("2026-03-22", "podcast_a", [ids[0]])

        since = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        result = store.get_content_since(
            since, exclude_used=True, exclude_podcast="podcast_a",
        )

        result_ids = {item.id for item in result}
        assert ids[0] not in result_ids
        assert ids[1] in result_ids
        assert ids[2] in result_ids


class TestExcludeUsedNoItemsUsed:
    """When nothing is marked used, all items are returned."""

    def test_all_returned(self, store):
        ids = _insert_items(store, count=3)

        since = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        result = store.get_content_since(
            since, exclude_used=True, exclude_podcast="podcast_a",
        )

        assert len(result) == 3
        result_ids = {item.id for item in result}
        for item_id in ids:
            assert item_id in result_ids
