"""Async web content collector using RSS feeds and article extraction."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from email.utils import parsedate_to_datetime

import feedparser
import httpx
import trafilatura

logger = logging.getLogger(__name__)


@dataclass
class Article:
    """An extracted web article."""

    title: str
    content: str
    url: str
    published_at: Optional[datetime] = None
    source_name: str = ""


class WebScraper:
    """Fetch RSS feeds and extract article text."""

    def __init__(self, request_delay: float = 1.0):
        """Initialize the web scraper.

        Args:
            request_delay: Seconds to wait between requests per domain.
        """
        self._request_delay = request_delay
        self._domain_timestamps: dict[str, float] = {}

    async def _rate_limit(self, url: str) -> None:
        """Enforce per-domain rate limiting."""
        from urllib.parse import urlparse

        domain = urlparse(url).netloc
        now = asyncio.get_event_loop().time()
        last = self._domain_timestamps.get(domain, 0)
        wait = self._request_delay - (now - last)
        if wait > 0:
            await asyncio.sleep(wait)
        self._domain_timestamps[domain] = asyncio.get_event_loop().time()

    async def fetch_source(
        self,
        source_name: str,
        source_config: dict,
    ) -> list[Article]:
        """Fetch articles from a single RSS source.

        Args:
            source_name: Identifier for this source.
            source_config: Dict with keys: url, language, max_articles.

        Returns:
            List of extracted Article objects.
        """
        feed_url = source_config["url"]
        max_articles = source_config.get("max_articles", 20)
        language = source_config.get("language", "en")

        logger.info("Fetching RSS feed: %s (%s)", source_name, feed_url)

        try:
            async with httpx.AsyncClient(
                timeout=30, follow_redirects=True
            ) as client:
                await self._rate_limit(feed_url)
                response = await client.get(feed_url)
                response.raise_for_status()
        except httpx.HTTPError:
            logger.error(
                "Failed to fetch feed %s", feed_url, exc_info=True
            )
            return []

        feed = await asyncio.to_thread(
            feedparser.parse, response.text
        )

        if feed.bozo and not feed.entries:
            logger.error(
                "Failed to parse feed %s: %s",
                feed_url,
                feed.bozo_exception,
            )
            return []

        entries = feed.entries[:max_articles]
        logger.info(
            "Found %d entries in %s, processing up to %d",
            len(feed.entries),
            source_name,
            max_articles,
        )

        articles = []
        for entry in entries:
            article = await self._extract_article(
                entry, source_name, language
            )
            if article:
                articles.append(article)

        logger.info(
            "Extracted %d articles from %s", len(articles), source_name
        )
        return articles

    async def _extract_article(
        self,
        entry: feedparser.FeedParserDict,
        source_name: str,
        language: str,
    ) -> Optional[Article]:
        """Extract article text from a feed entry.

        Args:
            entry: A feedparser entry.
            source_name: Name of the source feed.
            language: Language hint for trafilatura.

        Returns:
            Article object or None if extraction failed.
        """
        url = entry.get("link", "")
        title = entry.get("title", "")
        if not url:
            return None

        # Parse published date
        published_at = None
        published_str = entry.get("published") or entry.get("updated")
        if published_str:
            try:
                published_at = parsedate_to_datetime(published_str)
            except (ValueError, TypeError):
                pass

        # Try to get full text from the article page
        try:
            async with httpx.AsyncClient(
                timeout=30, follow_redirects=True
            ) as client:
                await self._rate_limit(url)
                response = await client.get(url)
                response.raise_for_status()
                html = response.text
        except httpx.HTTPError:
            logger.warning("Failed to fetch article: %s", url)
            # Fall back to feed summary
            content = entry.get("summary", "")
            if not content:
                return None
            return Article(
                title=title,
                content=content,
                url=url,
                published_at=published_at,
                source_name=source_name,
            )

        # Extract text with trafilatura
        content = await asyncio.to_thread(
            trafilatura.extract,
            html,
            target_language=language,
            include_comments=False,
            include_tables=False,
        )

        if not content:
            # Fall back to feed summary
            content = entry.get("summary", "")

        if not content:
            logger.debug("No content extracted for %s", url)
            return None

        return Article(
            title=title,
            content=content,
            url=url,
            published_at=published_at,
            source_name=source_name,
        )

    async def fetch_all_sources(
        self,
        sources: dict[str, dict],
    ) -> dict[str, list[Article]]:
        """Fetch articles from all configured web sources.

        Args:
            sources: Dict mapping source name to source config.

        Returns:
            Dict mapping source name to list of articles.
        """
        tasks = {
            name: self.fetch_source(name, config)
            for name, config in sources.items()
        }

        results = {}
        for name, coro in tasks.items():
            try:
                results[name] = await coro
            except Exception:
                logger.error(
                    "Failed to fetch source %s", name, exc_info=True
                )
                results[name] = []

        total = sum(len(articles) for articles in results.values())
        logger.info(
            "Fetched %d total articles from %d web sources",
            total,
            len(sources),
        )
        return results
