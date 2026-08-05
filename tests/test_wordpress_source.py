"""Tests for WordPress article backlog collection."""

import asyncio

import httpx

from telegram_translator.web_scraper import WebScraper


def test_wordpress_source_skips_existing_episode(monkeypatch):
    """The collector returns the newest article without a matching episode."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/podcast"):
            return httpx.Response(
                200,
                headers={"X-WP-TotalPages": "1"},
                json=[
                    {
                        "slug": "already-covered-audio",
                        "title": {"rendered": "Already Covered"},
                    }
                ],
            )
        return httpx.Response(
            200,
            headers={"X-WP-TotalPages": "1"},
            json=[
                {
                    "id": 10,
                    "date_gmt": "2026-08-05T12:00:00",
                    "link": "https://www.getpagespeed.com/already-covered",
                    "slug": "already-covered",
                    "title": {"rendered": "Already Covered"},
                    "content": {"rendered": "<p>Old body.</p>"},
                    "excerpt": {"rendered": ""},
                },
                {
                    "id": 9,
                    "date_gmt": "2026-08-04T12:00:00",
                    "link": "https://www.getpagespeed.com/new-article",
                    "slug": "new-article",
                    "title": {"rendered": "New &amp; Useful Article"},
                    "content": {"rendered": "<h2>Fast NGINX</h2><p>Use a cache.</p>"},
                    "excerpt": {"rendered": ""},
                },
            ],
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        return real_client(transport=transport)

    monkeypatch.setattr(
        "telegram_translator.web_scraper.httpx.AsyncClient",
        client_factory,
    )

    articles = asyncio.run(
        WebScraper(request_delay=0).fetch_source(
            "getpagespeed_articles",
            {
                "type": "wordpress",
                "url": "https://www.getpagespeed.com/wp-json/wp/v2/posts",
                "podcast_series_id": 456,
                "max_articles": 1,
            },
        )
    )

    assert len(articles) == 1
    assert articles[0].title == "New & Useful Article"
    assert "Fast NGINX" in articles[0].content
    assert articles[0].url.endswith("/new-article")
    assert articles[0].published_at.isoformat() == "2026-08-04T12:00:00+00:00"


def test_wordpress_source_title_match_covers_legacy_episode(monkeypatch):
    """Historical episodes without the -audio slug are matched by title."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/podcast"):
            body = [
                {
                    "slug": "legacy-custom-slug",
                    "title": {"rendered": "NGINX Security Headers module"},
                }
            ]
        else:
            body = [
                {
                    "id": 1,
                    "date_gmt": "2024-12-19T10:33:46",
                    "link": (
                        "https://www.getpagespeed.com/security/"
                        "nginx-security-headers-module"
                    ),
                    "slug": "nginx-security-headers-module",
                    "title": {"rendered": "NGINX Security Headers module"},
                    "content": {"rendered": "<p>Covered already.</p>"},
                    "excerpt": {"rendered": ""},
                }
            ]
        return httpx.Response(200, headers={"X-WP-TotalPages": "1"}, json=body)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "telegram_translator.web_scraper.httpx.AsyncClient",
        lambda *args, **kwargs: real_client(transport=transport),
    )

    articles = asyncio.run(
        WebScraper(request_delay=0).fetch_source(
            "gps",
            {
                "type": "wordpress",
                "url": "https://www.getpagespeed.com/wp-json/wp/v2/posts",
                "max_articles": 1,
            },
        )
    )
    assert articles == []
