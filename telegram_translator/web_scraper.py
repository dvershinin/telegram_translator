"""Async web content collector using RSS feeds and article extraction."""

import asyncio
import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from email.utils import parsedate_to_datetime

import feedparser
import httpx
import trafilatura

logger = logging.getLogger(__name__)


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _plain_text(value: str) -> str:
    """Convert a small WordPress HTML fragment to readable plain text."""
    extracted = trafilatura.extract(
        value,
        include_comments=False,
        include_tables=False,
    )
    if extracted:
        return extracted.strip()
    return re.sub(r"\s+", " ", html.unescape(_HTML_TAG_RE.sub(" ", value))).strip()


def _normalized_title(value: str) -> str:
    """Normalize a rendered title for cross-post-type matching."""
    return re.sub(r"[^a-z0-9]+", " ", _plain_text(value).lower()).strip()


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
        if source_config.get("type") == "wordpress":
            return await self._fetch_wordpress_source(source_name, source_config)

        feed_url = source_config["url"]
        max_articles = source_config.get("max_articles", 20)
        language = source_config.get("language", "en")

        logger.info("Fetching RSS feed: %s (%s)", source_name, feed_url)

        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                await self._rate_limit(feed_url)
                response = await client.get(feed_url)
                response.raise_for_status()
        except httpx.HTTPError:
            logger.error("Failed to fetch feed %s", feed_url, exc_info=True)
            return []

        feed = await asyncio.to_thread(feedparser.parse, response.text)

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
            article = await self._extract_article(entry, source_name, language)
            if article:
                articles.append(article)

        logger.info("Extracted %d articles from %s", len(articles), source_name)
        return articles

    async def _fetch_wordpress_source(
        self,
        source_name: str,
        source_config: dict,
    ) -> list[Article]:
        """Fetch unpublished article candidates from WordPress REST.

        Existing podcast episodes are the durable deduplication ledger. This
        makes the collector safe after a local database reset and lets it walk
        the full article archive one episode at a time without loading the
        entire archive into the LLM context.

        Args:
            source_name: Identifier for this source.
            source_config: WordPress source configuration. ``url`` is the
                posts collection endpoint; ``episode_url`` defaults to the
                sibling podcast collection endpoint.

        Returns:
            Up to ``max_articles`` article candidates, newest first.
        """
        posts_url = source_config["url"]
        episode_url = source_config.get("episode_url")
        if not episode_url:
            marker = "/wp-json/wp/v2/"
            if marker not in posts_url:
                raise ValueError(
                    "WordPress source URL must contain '/wp-json/wp/v2/' "
                    "when episode_url is omitted"
                )
            episode_url = posts_url.split(marker, 1)[0] + marker + "podcast"

        max_articles = int(source_config.get("max_articles", 1))
        if max_articles < 1:
            return []

        series_id = source_config.get("podcast_series_id")
        page_size = min(100, max(1, int(source_config.get("page_size", 100))))

        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": "GetPageSpeed-Podcast/1.0"},
        ) as client:
            episode_slugs, episode_titles = await self._wordpress_episode_ledger(
                client, episode_url, series_id, page_size
            )

            articles: list[Article] = []
            page = 1
            while len(articles) < max_articles:
                params = {
                    "page": page,
                    "per_page": page_size,
                    "orderby": "date",
                    "order": "desc",
                    "status": "publish",
                    "_fields": "id,date_gmt,link,slug,title,content,excerpt",
                }
                response = await client.get(posts_url, params=params)
                if response.status_code == 400 and page > 1:
                    break
                response.raise_for_status()
                posts = response.json()
                if not posts:
                    break

                for post in posts:
                    slug = str(post.get("slug", "")).strip()
                    title_html = str((post.get("title") or {}).get("rendered", ""))
                    title = _plain_text(title_html)
                    if not slug or not title:
                        continue
                    if (
                        f"{slug}-audio" in episode_slugs
                        or _normalized_title(title) in episode_titles
                    ):
                        continue

                    content_html = str((post.get("content") or {}).get("rendered", ""))
                    content = _plain_text(content_html)
                    if not content:
                        content = _plain_text(
                            str((post.get("excerpt") or {}).get("rendered", ""))
                        )
                    url = str(post.get("link", "")).strip()
                    if not content or not url:
                        continue

                    published_at = None
                    date_gmt = post.get("date_gmt")
                    if date_gmt:
                        try:
                            published_at = datetime.fromisoformat(
                                str(date_gmt).replace("Z", "+00:00")
                            )
                            if published_at.tzinfo is None:
                                published_at = published_at.replace(tzinfo=timezone.utc)
                        except ValueError:
                            logger.warning(
                                "Invalid WordPress date for post %s: %s",
                                post.get("id"),
                                date_gmt,
                            )

                    articles.append(
                        Article(
                            title=title,
                            content=content,
                            url=url,
                            published_at=published_at,
                            source_name=source_name,
                        )
                    )
                    if len(articles) >= max_articles:
                        break

                total_pages = int(response.headers.get("X-WP-TotalPages", "1"))
                if page >= total_pages:
                    break
                page += 1

        logger.info(
            "Selected %d WordPress article(s) not yet represented in %s",
            len(articles),
            episode_url,
        )
        return articles

    async def _wordpress_episode_ledger(
        self,
        client: httpx.AsyncClient,
        episode_url: str,
        series_id: int | str | None,
        page_size: int,
    ) -> tuple[set[str], set[str]]:
        """Return existing episode slugs and normalized titles."""
        slugs: set[str] = set()
        titles: set[str] = set()
        page = 1

        while True:
            params: dict[str, object] = {
                "page": page,
                "per_page": page_size,
                "orderby": "date",
                "order": "desc",
                "status": "publish",
                "_fields": "slug,title",
            }
            if series_id:
                params["series"] = series_id

            response = await client.get(episode_url, params=params)
            if response.status_code == 400 and page > 1:
                break
            response.raise_for_status()
            episodes = response.json()
            if not episodes:
                break

            for episode in episodes:
                slug = str(episode.get("slug", "")).strip()
                if slug:
                    slugs.add(slug)
                title = str((episode.get("title") or {}).get("rendered", ""))
                normalized = _normalized_title(title)
                if normalized:
                    titles.add(normalized)

            total_pages = int(response.headers.get("X-WP-TotalPages", "1"))
            if page >= total_pages:
                break
            page += 1

        return slugs, titles

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
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
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
            name: self.fetch_source(name, config) for name, config in sources.items()
        }

        results = {}
        for name, coro in tasks.items():
            try:
                results[name] = await coro
            except Exception:
                logger.error("Failed to fetch source %s", name, exc_info=True)
                results[name] = []

        total = sum(len(articles) for articles in results.values())
        logger.info(
            "Fetched %d total articles from %d web sources",
            total,
            len(sources),
        )
        return results
