"""LLM-based summarization for the digest pipeline."""

import logging
import os
from collections import defaultdict
from typing import Optional

import openai

from telegram_translator.content_store import ContentItem

logger = logging.getLogger(__name__)

# Fallback prompts when not configured
_DEFAULT_EXECUTIVE_PROMPT = (
    "You are a professional news analyst creating a daily executive briefing. "
    "Synthesize the source summaries into a cohesive overview of today's most "
    "important developments. Focus on what changed, what matters, and what "
    "to watch next."
)

_DEFAULT_PODCAST_PROMPT = (
    "Convert this executive summary into a natural monologue script for a "
    "daily news podcast. Write as if speaking to the listener directly. "
    "Use smooth transitions between topics. Open with a greeting and date, "
    "close with a brief sign-off. Target ~1500 words (roughly 10 minutes)."
)


class Summarizer:
    """Generate summaries and podcast scripts using an LLM."""

    def __init__(
        self,
        config: dict,
        title: str = "",
        host_name: str = "",
    ):
        """Initialize the summarizer.

        Args:
            config: Podcast config dict with keys: model,
                executive_prompt, podcast_prompt.
            title: Podcast title for template substitution.
            host_name: Host name for template substitution.

        Raises:
            ValueError: If no API key is available.
        """
        self.model = config.get("model", "gpt-4o")
        self.title = title
        self.host_name = host_name
        self.executive_prompt = config.get(
            "executive_prompt", _DEFAULT_EXECUTIVE_PROMPT
        )
        self.podcast_prompt = config.get(
            "podcast_prompt", _DEFAULT_PODCAST_PROMPT
        )

        api_key = os.getenv("OPENAI_API_KEY") or config.get("api_key")
        if not api_key:
            raise ValueError(
                "OpenAI API key required for summarization. "
                "Set OPENAI_API_KEY or add api_key to digest config."
            )

        self.client = openai.AsyncOpenAI(api_key=api_key)

    async def _chat(
        self,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.5,
    ) -> str:
        """Send a chat completion request.

        Args:
            system: System prompt.
            user: User message.
            max_tokens: Max tokens in the response.
            temperature: Sampling temperature.

        Returns:
            The assistant's response text.
        """
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return response.choices[0].message.content.strip()

    async def summarize_source(
        self,
        items: list[ContentItem],
        source_name: str,
        source_prompt: Optional[str] = None,
    ) -> str:
        """Generate a summary for a single source's content.

        Args:
            items: Content items from this source.
            source_name: Name of the source.
            source_prompt: Custom summarization instructions for this source.

        Returns:
            Summary text.
        """
        if not items:
            return ""

        # Build the content block
        content_parts = []
        for item in items:
            header = item.title or "(no title)"
            content_parts.append(f"### {header}\n{item.content}")

        all_content = "\n\n---\n\n".join(content_parts)

        # Truncate if too long (leave room for prompts)
        if len(all_content) > 80_000:
            all_content = all_content[:80_000] + "\n\n[... truncated]"

        system = (
            f"You are summarizing content from the source '{source_name}'. "
            f"{source_prompt or ''} "
            "Produce a concise but thorough summary of the key developments. "
            "Use bullet points for individual items. "
            "Write in English."
        )

        user = (
            f"Here are today's items from {source_name}:\n\n{all_content}"
        )

        logger.info(
            "Summarizing %d items from %s (%d chars)",
            len(items),
            source_name,
            len(all_content),
        )

        return await self._chat(system, user)

    async def executive_summary(
        self,
        source_summaries: dict[str, str],
        prompt: Optional[str] = None,
    ) -> str:
        """Generate a cross-source executive summary.

        Args:
            source_summaries: Dict mapping source name to its summary.
            prompt: Custom executive summary prompt override.

        Returns:
            Executive summary text.
        """
        if not source_summaries:
            return "No content available for summary."

        parts = []
        for source, summary in source_summaries.items():
            parts.append(f"## {source}\n\n{summary}")

        all_summaries = "\n\n---\n\n".join(parts)

        system = prompt or self.executive_prompt

        user = (
            "Here are the individual source summaries for today:\n\n"
            f"{all_summaries}"
        )

        logger.info(
            "Generating executive summary from %d sources",
            len(source_summaries),
        )

        return await self._chat(system, user, max_tokens=4096)

    async def generate_podcast_script(
        self,
        executive_summary: str,
        date: str,
        prompt: Optional[str] = None,
    ) -> str:
        """Generate a podcast monologue script from the executive summary.

        Template variables ``{title}``, ``{host_name}``, and ``{date}``
        in the prompt are substituted with the configured values.

        Args:
            executive_summary: The executive summary text.
            date: Date string (YYYY-MM-DD) for the greeting.
            prompt: Custom podcast prompt override.

        Returns:
            Podcast script text.
        """
        raw_prompt = prompt or self.podcast_prompt
        template_vars = defaultdict(
            str,
            title=self.title,
            host_name=self.host_name,
            date=date,
        )
        system = raw_prompt.format_map(template_vars)

        user = (
            f"Date: {date}\n\n"
            f"Executive summary to convert into a podcast script:\n\n"
            f"{executive_summary}"
        )

        logger.info("Generating podcast script for %s", date)

        return await self._chat(
            system, user, max_tokens=8192, temperature=0.7
        )
