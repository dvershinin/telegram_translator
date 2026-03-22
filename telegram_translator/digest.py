"""Digest pipeline orchestrator."""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram_translator.config_manager import ConfigManager
from telegram_translator.content_store import ContentStore
from telegram_translator.podcast_generator import PodcastGenerator
from telegram_translator.summarizer import Summarizer
from telegram_translator.web_scraper import WebScraper

logger = logging.getLogger(__name__)


class DigestPipeline:
    """Orchestrate the daily digest: collect, summarize, podcast."""

    def __init__(
        self,
        config_manager: ConfigManager,
        podcast_name: str | None = None,
    ):
        """Initialize the pipeline.

        Args:
            config_manager: Application config manager.
            podcast_name: Run only this podcast. Runs all if None.

        Raises:
            ValueError: If the given podcast_name is not configured.
        """
        self.config_manager = config_manager
        config = config_manager.config

        db_path = config_manager.get_database_path("content_store.db")
        self.store = ContentStore(db_path)

        self.sources_config = config.get("sources", {})

        all_podcasts = config_manager.resolve_podcast_configs()
        if podcast_name:
            if podcast_name not in all_podcasts:
                available = list(all_podcasts.keys())
                raise ValueError(
                    f"Unknown podcast '{podcast_name}'. "
                    f"Available: {available}"
                )
            self.podcast_configs = {podcast_name: all_podcasts[podcast_name]}
        else:
            self.podcast_configs = all_podcasts

    def _today(self) -> str:
        """Return today's date as YYYY-MM-DD in local time."""
        return datetime.now().strftime("%Y-%m-%d")

    def _since(self, date: str) -> datetime:
        """Return the content cutoff datetime.

        For today's date, returns 24 hours ago to capture content
        across timezone boundaries. For past dates, returns the
        start of that day in UTC.
        """
        if date == self._today():
            return datetime.now(tz=timezone.utc) - timedelta(hours=24)
        return datetime.strptime(date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )

    async def collect(self, date: str | None = None) -> int:
        """Collect content from all configured sources.

        Args:
            date: Target date (YYYY-MM-DD). Defaults to today.

        Returns:
            Number of new items stored.
        """
        date = date or self._today()
        since = self._since(date)
        total_new = 0

        telegram_sources = self.sources_config.get("telegram", {})
        if telegram_sources:
            total_new += await self._collect_telegram(
                telegram_sources, since
            )

        web_sources = self.sources_config.get("web", {})
        if web_sources:
            total_new += await self._collect_web(web_sources)

        logger.info("Collection complete: %d new items stored", total_new)
        return total_new

    async def _collect_telegram(
        self,
        sources: dict,
        since: datetime,
    ) -> int:
        """Fetch Telegram channel history and store content."""
        from telethon import TelegramClient

        credentials = self.config_manager.get_telegram_credentials()
        client = TelegramClient(
            credentials["session_name"],
            credentials["api_id"],
            credentials["api_hash"],
        )

        new_count = 0
        try:
            await client.connect()
            if not await client.is_user_authorized():
                logger.warning(
                    "Telegram session not authorized — "
                    "Telegram sources will be skipped. To fix, run:\n"
                    "  source ~/.secrets && telegram-translator start\n"
                    "Enter the phone code, then Ctrl+C once the bot "
                    "starts. After that, digest collect will work."
                )
                return 0
            logger.info("Telegram client connected for digest collection")

            translation_mgr = None
            translation_config = self.config_manager.get_translation_config()
            if translation_config.get("provider") == "openai":
                from telegram_translator.translation_manager import (
                    TranslationManager,
                )
                try:
                    translation_mgr = TranslationManager(translation_config)
                except ValueError:
                    logger.warning(
                        "Translation unavailable, storing original text"
                    )

            for channel_name, channel_config in sources.items():
                try:
                    new_count += await self._collect_channel(
                        client,
                        channel_name,
                        channel_config,
                        since,
                        translation_mgr,
                    )
                except Exception:
                    logger.error(
                        "Failed to collect from Telegram channel %s",
                        channel_name,
                        exc_info=True,
                    )

        finally:
            await client.disconnect()

        return new_count

    async def _collect_channel(
        self,
        client,
        channel_name: str,
        channel_config: dict,
        since: datetime,
        translation_mgr=None,
    ) -> int:
        """Collect messages from a single Telegram channel."""
        logger.info("Collecting Telegram channel: %s", channel_name)
        new_count = 0

        async for message in client.iter_messages(
            channel_name, offset_date=since, reverse=True
        ):
            text = message.message
            if not text:
                continue

            content = text
            if translation_mgr:
                try:
                    translated = await translation_mgr.translate(text)
                    if translated and translated != text:
                        content = translated
                except Exception:
                    logger.warning(
                        "Translation failed for message %d in %s",
                        message.id,
                        channel_name,
                        exc_info=True,
                    )

            inserted = self.store.store_content(
                source_name=channel_name,
                source_type="telegram",
                content=content,
                message_id=message.id,
                published_at=message.date,
            )
            if inserted:
                new_count += 1

        logger.info(
            "Collected %d new messages from %s", new_count, channel_name
        )
        return new_count

    async def _collect_web(self, sources: dict) -> int:
        """Fetch web articles and store content."""
        scraper = WebScraper()
        all_articles = await scraper.fetch_all_sources(sources)

        new_count = 0
        for source_name, articles in all_articles.items():
            for article in articles:
                inserted = self.store.store_content(
                    source_name=source_name,
                    source_type="web",
                    content=article.content,
                    title=article.title,
                    url=article.url,
                    published_at=article.published_at,
                )
                if inserted:
                    new_count += 1

        logger.info("Collected %d new web articles", new_count)
        return new_count

    async def summarize(
        self,
        date: str | None = None,
        no_cache: bool = False,
    ) -> dict:
        """Generate summaries for each podcast from collected content.

        Args:
            date: Target date (YYYY-MM-DD). Defaults to today.
            no_cache: If True, bypass the LLM cache for this run.

        Returns:
            Dict mapping podcast name to result dict with keys:
            source_summaries, executive_summary, podcast_script.
        """
        date = date or self._today()
        since = self._since(date)
        results = {}

        for podcast_name, pcfg in self.podcast_configs.items():
            logger.info("Summarizing for podcast: %s", podcast_name)

            digest = self.store.create_digest(date, podcast_name)
            self.store.update_digest(
                date, podcast_name, status="summarizing"
            )

            try:
                summarizer = Summarizer(
                    pcfg,
                    title=pcfg.get("title", ""),
                    host_name=pcfg.get("host_name", ""),
                    store=self.store,
                    podcast_name=podcast_name,
                    no_cache=no_cache,
                )

                # Filter to this podcast's sources
                source_filter = pcfg.get("source_names") or None
                source_names = self.store.get_source_names(
                    since, source_filter=source_filter
                )

                if not source_names:
                    msg = f"No content found for podcast '{podcast_name}'"
                    logger.warning(msg)
                    self.store.update_digest(
                        date, podcast_name,
                        status="error", error_message=msg,
                    )
                    results[podcast_name] = {
                        "source_summaries": {},
                        "executive_summary": "",
                        "podcast_script": "",
                    }
                    continue

                # Content selection filter — narrow items before
                # summarization so each podcast only sees relevant
                # articles.
                selection_prompt = pcfg.get("selection_prompt", "")
                filtered_items_by_source = None
                if selection_prompt:
                    all_items = self.store.get_content_since(
                        since, source_names=source_filter,
                        exclude_used=True,
                        exclude_podcast=podcast_name,
                    )
                    all_items = await summarizer.select_content(
                        all_items, selection_prompt,
                    )
                    # Track selected item IDs for publish-time marking
                    selected_ids = [
                        item.id for item in all_items if item.id is not None
                    ]
                    self.store.update_digest(
                        date, podcast_name,
                        selected_item_ids=json.dumps(selected_ids),
                    )

                    # Re-derive source_names from filtered items
                    filtered_items_by_source = {}
                    for item in all_items:
                        filtered_items_by_source.setdefault(
                            item.source_name, []
                        ).append(item)
                    source_names = sorted(filtered_items_by_source.keys())

                    if not source_names:
                        msg = (
                            f"No content survived selection for "
                            f"podcast '{podcast_name}'"
                        )
                        logger.warning(msg)
                        self.store.update_digest(
                            date, podcast_name,
                            status="error", error_message=msg,
                        )
                        results[podcast_name] = {
                            "source_summaries": {},
                            "executive_summary": "",
                            "podcast_script": "",
                        }
                        continue

                # Resolve per-source prompts
                all_sources = {
                    **pcfg.get("sources", {}).get("telegram", {}),
                    **pcfg.get("sources", {}).get("web", {}),
                }

                source_summaries = {}
                source_biases = {}
                for source_name in source_names:
                    if filtered_items_by_source is not None:
                        items = filtered_items_by_source.get(
                            source_name, []
                        )
                    else:
                        items = self.store.get_content_since(
                            since, source_name=source_name
                        )
                    if not items:
                        continue

                    source_cfg = all_sources.get(source_name, {})
                    source_prompt = source_cfg.get("prompt", "")
                    source_bias = source_cfg.get("bias", "")

                    if source_bias:
                        source_biases[source_name] = source_bias

                    summary = await summarizer.summarize_source(
                        items, source_name, source_prompt, source_bias
                    )
                    if summary:
                        source_summaries[source_name] = summary

                # Inject prior episode context for dedup
                prior = self.store.get_recent_summaries(
                    podcast_name, date, limit=3,
                )
                prior_context = None
                if prior:
                    parts = []
                    for pdate, psummary in prior:
                        parts.append(
                            f"[{pdate}]:\n{psummary[:2000]}"
                        )
                    prior_context = (
                        "PREVIOUSLY COVERED (recent episodes — "
                        "focus on what is NEW today, don't "
                        "re-explain these stories from scratch):"
                        "\n\n" + "\n\n".join(parts)
                    )

                executive = await summarizer.executive_summary(
                    source_summaries,
                    pcfg.get("executive_prompt") or None,
                    source_biases=source_biases or None,
                    prior_context=prior_context,
                )

                script = await summarizer.generate_podcast_script(
                    executive,
                    date,
                    pcfg.get("podcast_prompt") or None,
                )

                self.store.update_digest(
                    date,
                    podcast_name,
                    source_summaries=source_summaries,
                    executive_summary=executive,
                    podcast_script=script,
                    status="summarized",
                )

                results[podcast_name] = {
                    "source_summaries": source_summaries,
                    "executive_summary": executive,
                    "podcast_script": script,
                }
                logger.info(
                    "Summarization complete for %s/%s", podcast_name, date
                )

            except Exception as e:
                self.store.update_digest(
                    date, podcast_name,
                    status="error", error_message=str(e),
                )
                logger.error(
                    "Summarization failed for %s/%s",
                    podcast_name, date,
                    exc_info=True,
                )
                raise

        return results

    async def podcast(
        self,
        date: str | None = None,
        no_cache: bool = False,
    ) -> dict:
        """Generate podcast audio for each podcast.

        Args:
            date: Target date (YYYY-MM-DD). Defaults to today.
            no_cache: If True, bypass the TTS segment cache.

        Returns:
            Dict mapping podcast name to audio file path string.
        """
        date = date or self._today()
        results = {}

        tts_cache_dir = Path(".cache/tts")

        for podcast_name, pcfg in self.podcast_configs.items():
            digest = self.store.get_digest(date, podcast_name)

            if not digest or not digest.podcast_script:
                raise RuntimeError(
                    f"No podcast script found for {podcast_name}/{date}. "
                    "Run 'digest summarize' first."
                )

            self.store.update_digest(
                date, podcast_name, status="generating"
            )

            try:
                generator = PodcastGenerator(
                    pcfg,
                    tts_cache_dir=tts_cache_dir,
                    no_cache=no_cache,
                )
                audio_path = await generator.generate_podcast(
                    digest.podcast_script, date
                )

                self.store.update_digest(
                    date,
                    podcast_name,
                    audio_path=str(audio_path),
                    status="complete",
                    completed_at=datetime.now(
                        tz=timezone.utc
                    ).isoformat(),
                )

                results[podcast_name] = str(audio_path)
                logger.info(
                    "Podcast complete for %s: %s", podcast_name, audio_path
                )

            except Exception as e:
                self.store.update_digest(
                    date, podcast_name,
                    status="error", error_message=str(e),
                )
                logger.error(
                    "Podcast generation failed for %s",
                    podcast_name,
                    exc_info=True,
                )
                raise

        return results

    async def run(
        self,
        date: str | None = None,
        no_cache: bool = False,
    ) -> dict:
        """Run the full digest pipeline.

        Args:
            date: Target date (YYYY-MM-DD). Defaults to today.
            no_cache: If True, bypass LLM and TTS caches.

        Returns:
            Dict mapping podcast name to result dict.
        """
        date = date or self._today()
        logger.info("Starting full digest pipeline for %s", date)

        new_items = await self.collect(date)
        logger.info("Collected %d new items", new_items)

        results = await self.summarize(date, no_cache=no_cache)

        for podcast_name in self.podcast_configs:
            try:
                audio_path = await self.podcast(date, no_cache=no_cache)
                if podcast_name in audio_path:
                    results.setdefault(podcast_name, {})
                    results[podcast_name]["audio_path"] = audio_path[
                        podcast_name
                    ]
            except RuntimeError:
                logger.warning(
                    "Podcast generation skipped for %s (Voicebox unavailable)",
                    podcast_name,
                    exc_info=True,
                )
                results.setdefault(podcast_name, {})
                results[podcast_name]["audio_path"] = ""

        logger.info("Digest pipeline complete for %s", date)
        return results
