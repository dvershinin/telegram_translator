"""Tests for the idempotent WordPress podcast publisher."""

import asyncio
import json
import wave
from pathlib import Path

import httpx

from telegram_translator.content_store import ContentStore
from telegram_translator.wordpress_publisher import (
    WordPressPodcastPublisher,
    _duration_text,
    _filesize_text,
)


def _write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b"\0\0" * 240)


def _store_with_digest(tmp_path: Path):
    store = ContentStore(tmp_path / "content.db")
    assert store.store_content(
        source_name="getpagespeed_articles",
        source_type="web",
        title="Tune NGINX Like a Pro",
        content="Article body",
        url="https://www.getpagespeed.com/tune-nginx",
    )
    item = store.get_content_since(__import__("datetime").datetime(1970, 1, 1))[0]
    store.create_digest("2026-08-06", "scalable_stories")
    store.update_digest(
        "2026-08-06",
        "scalable_stories",
        executive_summary="A practical NGINX tuning walkthrough.",
        selected_item_ids=json.dumps([item.id]),
        audio_path=str(tmp_path / "episode.wav"),
    )
    return store, store.get_digest("2026-08-06", "scalable_stories")


def _config(tmp_path: Path) -> dict:
    return {
        "name": "scalable_stories",
        "title": "Scalable Stories",
        "host_name": "GetPageSpeed",
        "publish": {
            "base_url": "https://www.getpagespeed.com",
            "publish_dir": str(tmp_path / "publish"),
            "username_env": "GPS_WP_USER",
            "application_password_env": "GPS_WP_APP_PASSWORD",
            "post_type": "podcast",
            "series_id": 456,
            "post_status": "publish",
        },
    }


def test_publisher_draft_upload_finalize(monkeypatch, tmp_path):
    """A new episode is drafted, attached, finalized, and verified."""
    store, digest = _store_with_digest(tmp_path)
    wav_path = tmp_path / "episode.wav"
    _write_wav(wav_path)
    requests: list[tuple[str, str, dict]] = []

    def fake_encode(source, destination, bitrate, metadata):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"m4a-audio")
        return destination, 367.4

    def handler(request: httpx.Request) -> httpx.Response:
        payload = (
            json.loads(request.content)
            if request.content
            and (request.headers.get("content-type", "").startswith("application/json"))
            else {}
        )
        requests.append((request.method, request.url.path, payload))

        if request.method == "GET" and request.url.path.endswith("/podcast"):
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path.endswith("/podcast"):
            assert payload["status"] == "draft"
            return httpx.Response(201, json={"id": 99, "status": "draft", "meta": {}})
        if request.method == "GET" and request.url.path.endswith("/media"):
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path.endswith("/media"):
            assert request.url.params["post"] == "99"
            return httpx.Response(
                201,
                json={
                    "id": 100,
                    "source_url": (
                        "https://www.getpagespeed.com/wp-content/uploads/"
                        "2026/08/tune-nginx-audio.m4a"
                    ),
                },
            )
        if request.method == "POST" and request.url.path.endswith("/podcast/99"):
            assert payload["series"] == [456]
            assert payload["meta"]["duration"] == "00:06:07"
            assert payload["meta"]["filesize_raw"] == "9"
            assert "Read the full article" in payload["content"]
            return httpx.Response(
                200,
                json={
                    "id": 99,
                    "status": "publish",
                    "meta": {
                        "audio_file": (
                            "https://www.getpagespeed.com/wp-content/uploads/"
                            "2026/08/tune-nginx-audio.m4a"
                        )
                    },
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "telegram_translator.wordpress_publisher.encode_m4a", fake_encode
    )
    monkeypatch.setattr(
        "telegram_translator.wordpress_publisher.httpx.AsyncClient",
        lambda *args, **kwargs: real_client(transport=transport),
    )
    monkeypatch.setenv("GPS_WP_USER", "danila")
    monkeypatch.setenv("GPS_WP_APP_PASSWORD", "test-app-password")

    result = asyncio.run(
        WordPressPodcastPublisher(_config(tmp_path), store).publish(
            "scalable_stories", "2026-08-06", digest, wav_path
        )
    )

    assert Path(result).name == "tune-nginx-audio.m4a"
    assert sum(path.endswith("/media") for _, path, _ in requests) == 2
    updated = store.get_digest("2026-08-06", "scalable_stories")
    assert updated.status == "published"
    assert updated.duration_seconds == 367.4


def test_publisher_reuses_existing_audio(monkeypatch, tmp_path):
    """A retry does not upload a second media item."""
    store, digest = _store_with_digest(tmp_path)
    wav_path = tmp_path / "episode.wav"
    _write_wav(wav_path)
    media_posts = 0

    def fake_encode(source, destination, bitrate, metadata):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"audio")
        return destination, 60.0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal media_posts
        audio_url = "https://www.getpagespeed.com/audio/existing.m4a"
        if request.method == "GET" and request.url.path.endswith("/podcast"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 99,
                        "status": "draft",
                        "meta": {"audio_file": audio_url},
                    }
                ],
            )
        if request.method == "POST" and request.url.path.endswith("/media"):
            media_posts += 1
        if request.method == "POST" and request.url.path.endswith("/podcast/99"):
            return httpx.Response(
                200,
                json={
                    "id": 99,
                    "status": "publish",
                    "meta": {"audio_file": audio_url},
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "telegram_translator.wordpress_publisher.encode_m4a", fake_encode
    )
    monkeypatch.setattr(
        "telegram_translator.wordpress_publisher.httpx.AsyncClient",
        lambda *args, **kwargs: real_client(transport=transport),
    )
    monkeypatch.setenv("GPS_WP_USER", "danila")
    monkeypatch.setenv("GPS_WP_APP_PASSWORD", "test-app-password")

    asyncio.run(
        WordPressPodcastPublisher(_config(tmp_path), store).publish(
            "scalable_stories", "2026-08-06", digest, wav_path
        )
    )
    assert media_posts == 0


def test_metadata_formatters():
    assert _duration_text(367.4) == "00:06:07"
    assert _filesize_text(1024 * 1024) == "1.00MB"
