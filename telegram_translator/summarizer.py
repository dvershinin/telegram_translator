"""LLM-based summarization for the digest pipeline."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Optional

import openai

from telegram_translator.content_store import ContentItem
from telegram_translator.llm_env import (
    DEFAULT_ROLE,
    completion_kwargs,
    is_deepseek,
    require_role,
    thinking_extra_body,
)

if TYPE_CHECKING:
    from telegram_translator.content_store import ContentStore

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

_DEFAULT_SHOW_NOTES_PROMPT = (
    "You are producing listener-facing show notes for a daily podcast — "
    "the polished text readers see on the show's website. "
    "Read the executive briefing and produce clean, factual notes."
    "\n\nMatch the language of the briefing (Russian briefing → Russian "
    "notes; English briefing → English notes). Speak in the host's own "
    "voice — informed, conversational, matter-of-fact."
    "\n\nHard rules:"
    "\n- Never name or hint at any source channel or publication. "
    "If a source is mentioned in the briefing, drop the attribution."
    "\n- Do NOT use host-direction scaffolding: no 'Executive-обзор', "
    "'Тема:', 'Что случилось:', 'Почему это важно:', "
    "'Угол для ведущего:', 'Вопрос к ведущему:', 'Скажу так:', "
    "'What happened:', 'Why it matters:', 'Angle for host:'. These are "
    "internal labels — never surface them."
    "\n- Headlines are plain text only — no '#' characters, no '**' "
    "markdown, no leading numbering."
    "\n- The lead is a 1-2 sentence teaser for the entire episode, not a "
    "preamble like 'Here is today's briefing.'"
)

_FACTUAL_ACCURACY_GUARDRAIL = (
    "\n\nFACTUAL ACCURACY: Use your general knowledge to avoid "
    "mischaracterizing well-known existing products, services, or "
    'organizations as "new," "upcoming," or "launching." When a source '
    "describes advertising, marketing campaigns, or increased public "
    "visibility for something, do not assume it is a new product launch — "
    "it is likely a campaign for an existing offering. Only call something "
    '"new" if the source explicitly states it was just created or '
    "announced for the first time."
)


class Summarizer:
    """Generate summaries and podcast scripts using an LLM."""

    def __init__(
        self,
        config: dict,
        title: str = "",
        host_name: str = "",
        store: ContentStore | None = None,
        podcast_name: str = "",
        no_cache: bool = False,
    ):
        """Initialize the summarizer.

        Args:
            config: Podcast config dict with keys: model,
                selection_model, summarization_model, executive_model,
                script_model, executive_prompt, podcast_prompt.
            title: Podcast title for template substitution.
            host_name: Host name for template substitution.
            store: Optional ContentStore for LLM response caching.
            podcast_name: Podcast identifier for cache key construction.
            no_cache: If True, bypass the LLM cache entirely.

        Raises:
            ValueError: If no API key is available.
        """
        # The model is NOT read from config: the podcast names an LLM role and
        # the role resolves from the environment. See telegram_translator.llm_env.
        self.role = require_role(config.get("llm_role") or DEFAULT_ROLE)
        self.uses_deepseek = is_deepseek(self.role.model)
        self.default_model = self.role.model
        self.selection_model = self.role.model
        self.summarization_model = self.role.model
        self.executive_model = self.role.model
        self.script_model = self.role.model

        self.title = title
        self.host_name = host_name
        self.store = store
        self.podcast_name = podcast_name
        self.no_cache = no_cache
        self.executive_prompt = config.get(
            "executive_prompt", _DEFAULT_EXECUTIVE_PROMPT
        )
        self.podcast_prompt = config.get(
            "podcast_prompt", _DEFAULT_PODCAST_PROMPT
        )
        self.show_notes_prompt = config.get(
            "show_notes_prompt", _DEFAULT_SHOW_NOTES_PROMPT
        )

        self.api_base = self.role.base_url
        self.client = openai.AsyncOpenAI(
            api_key=self.role.api_key,
            base_url=self.role.base_url,
        )

    def _make_cache_key(
        self,
        stage: str,
        system: str,
        user: str,
        model: str,
        extra: str = "",
    ) -> str | None:
        """Compute a cache key for an LLM call.

        Args:
            stage: Pipeline stage name.
            system: System prompt.
            user: User message.
            model: Model identifier.
            extra: Optional extra key component (e.g. source name).

        Returns:
            Cache key string, or None if caching is disabled.
        """
        if self.no_cache or not self.store:
            return None
        payload = f"{system}\n{user}\n{model}"
        h = hashlib.sha256(payload.encode()).hexdigest()
        parts = [stage, self.podcast_name]
        if extra:
            parts.append(extra)
        parts.append(h)
        return ":".join(parts)

    async def _chat(
        self,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.5,
        model: str | None = None,
        cache_key: str | None = None,
    ) -> str:
        """Send a chat completion request.

        Args:
            system: System prompt.
            user: User message.
            max_tokens: Max tokens in the response.
            temperature: Sampling temperature.
            model: Model to use. Defaults to self.default_model.
            cache_key: Optional cache key. If set and cache hits, skips
                the API call.

        Returns:
            The assistant's response text.
        """
        use_model = model or self.default_model

        # Check cache
        if cache_key and self.store:
            cached = self.store.get_llm_cache(cache_key)
            if cached is not None:
                stage = cache_key.split(":")[0]
                logger.info(
                    "Cache hit for %s, skipping API call", stage
                )
                return cached

        response = await self.client.chat.completions.create(
            model=use_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            extra_body=thinking_extra_body(self.role),
            **completion_kwargs(
                use_model,
                max_output_tokens=max_tokens,
                temperature=temperature,
            ),
        )
        result = response.choices[0].message.content.strip()

        # Store in cache
        if cache_key and self.store:
            stage = cache_key.split(":")[0]
            self.store.set_llm_cache(cache_key, stage, result, use_model)

        return result

    async def select_content(
        self,
        items: list[ContentItem],
        selection_prompt: str,
    ) -> list[ContentItem]:
        """Filter content items by relevance using an LLM.

        Runs selection per source to avoid small sources being
        drowned out in a large combined catalog.

        Args:
            items: All collected content items.
            selection_prompt: Criteria describing which articles to include.

        Returns:
            Filtered list of relevant ContentItem objects.
        """
        if not items:
            return []

        # Group items by source
        by_source: dict[str, list[ContentItem]] = {}
        for item in items:
            by_source.setdefault(item.source_name, []).append(item)

        all_selected: list[ContentItem] = []
        for source_name, source_items in by_source.items():
            selected = await self._select_batch(
                source_items, selection_prompt, source_name,
            )
            all_selected.extend(selected)

        logger.info(
            "Content selection total: %d/%d items from %d sources",
            len(all_selected),
            len(items),
            len(by_source),
        )
        return all_selected

    async def _select_batch(
        self,
        items: list[ContentItem],
        selection_prompt: str,
        source_name: str,
    ) -> list[ContentItem]:
        """Run selection on a single batch of items.

        Args:
            items: Content items to evaluate.
            selection_prompt: Criteria for inclusion.
            source_name: Source name (for logging/caching).

        Returns:
            Filtered list of matching ContentItem objects.
        """
        catalog_lines = []
        for i, item in enumerate(items, 1):
            title = item.title or "(no title)"
            preview = item.content[:400].replace("\n", " ")
            catalog_lines.append(
                f"{i}. {title} — {preview}"
            )
        catalog = "\n".join(catalog_lines)

        system = (
            "You are a content curator. Given a selection criteria and a "
            "numbered list of articles, return ONLY the numbers of articles "
            "that match the criteria. Output one number per line, nothing "
            "else. If none match, output NONE.\n\n"
            "Articles may be in any language (Russian, Ukrainian, "
            "English, etc.). Evaluate ALL articles by their topic "
            "regardless of language."
        )
        user = (
            f"Selection criteria: {selection_prompt}\n\n"
            f"Articles from {source_name}:\n{catalog}"
        )

        logger.info(
            "Selecting from %s: %d candidates, model=%s",
            source_name,
            len(items),
            self.selection_model,
        )

        cache_key = self._make_cache_key(
            "selection", system, user,
            self.selection_model, extra=source_name,
        )
        response = await self._chat(
            system,
            user,
            max_tokens=1024,
            temperature=0.0,
            model=self.selection_model,
            cache_key=cache_key,
        )

        if response.strip().upper() == "NONE":
            logger.info(
                "No articles selected from %s", source_name,
            )
            return []

        selected_indices = set()
        for token in re.findall(r"\d+", response):
            idx = int(token)
            if 1 <= idx <= len(items):
                selected_indices.add(idx)

        selected = [items[i - 1] for i in sorted(selected_indices)]
        logger.info(
            "Selected %d/%d from %s",
            len(selected), len(items), source_name,
        )
        return selected

    async def summarize_source(
        self,
        items: list[ContentItem],
        source_name: str,
        source_prompt: Optional[str] = None,
        source_bias: Optional[str] = None,
    ) -> str:
        """Generate a summary for a single source's content.

        Args:
            items: Content items from this source.
            source_name: Name of the source.
            source_prompt: Custom summarization instructions for this source.
            source_bias: Editorial bias description for this source.

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

        bias_context = ""
        if source_bias:
            bias_context = (
                f"\n\nEDITORIAL BIAS NOTE about this source: {source_bias} "
                "Account for this bias when summarizing — present facts "
                "neutrally, flag claims that may reflect editorial slant "
                "rather than verified reality, and avoid parroting "
                "propaganda framing."
            )

        system = (
            "You are summarizing today's news items. "
            f"{source_prompt or ''} "
            "Produce a concise but thorough summary of the key developments. "
            "Use bullet points for individual items. "
            "Write in English. "
            "Never name or hint at the source publication or channel in "
            "your output — speak in the host's own voice. "
            f"{bias_context}{_FACTUAL_ACCURACY_GUARDRAIL}"
        )

        user = f"Here are today's items:\n\n{all_content}"

        logger.info(
            "Summarizing %d items from %s (%d chars), model=%s",
            len(items),
            source_name,
            len(all_content),
            self.summarization_model,
        )

        cache_key = self._make_cache_key(
            "source_summary", system, user,
            self.summarization_model, extra=source_name,
        )
        return await self._chat(
            system, user, model=self.summarization_model,
            cache_key=cache_key,
        )

    async def executive_summary(
        self,
        source_summaries: dict[str, str],
        prompt: Optional[str] = None,
        source_biases: Optional[dict[str, str]] = None,
        prior_context: Optional[str] = None,
    ) -> str:
        """Generate a cross-source executive summary.

        Args:
            source_summaries: Dict mapping source name to its summary.
            prompt: Custom executive summary prompt override.
            source_biases: Dict mapping source name to bias description.
            prior_context: Summary of recent prior episodes so the LLM
                can focus on what is new rather than re-explaining.

        Returns:
            Executive summary text.
        """
        if not source_summaries:
            return "No content available for summary."

        # Source names are deliberately anonymized to `Source 1`,
        # `Source 2`, ... before being shown to the LLM. This is the
        # root-cause fix for source-channel name leaks into the
        # executive summary (and downstream show notes / podcast
        # script). The LLM never sees the brand name and so cannot
        # echo it. Per-source bias keeps the same anonymized labels so
        # the cross-referencing prompt still works.
        ordered_sources = list(source_summaries.items())
        alias_for = {
            name: f"Source {idx + 1}"
            for idx, (name, _summary) in enumerate(ordered_sources)
        }
        parts = [
            f"## {alias_for[name]}\n\n{summary}"
            for name, summary in ordered_sources
        ]
        all_summaries = "\n\n---\n\n".join(parts)

        bias_block = ""
        if source_biases:
            bias_lines = [
                f"- {alias_for.get(name, name)}: {bias}"
                for name, bias in source_biases.items()
                if bias and name in alias_for
            ]
            if bias_lines:
                bias_block = (
                    "\n\nSOURCE BIAS CONTEXT (use for cross-referencing):\n"
                    + "\n".join(bias_lines)
                    + "\n\nWhen sources report the same event differently, "
                    "note the discrepancy and present a balanced view. "
                    "Do not adopt any single source's framing as truth. "
                    "Never name a source channel or publication in your "
                    "output — speak in the host's own voice."
                )

        guardrail = (
            "\n\nIMPORTANT: Only cover topics that have actual content "
            "in the source summaries. Do NOT invent categories or mention "
            "that a topic area had no developments. If a topic has no "
            "content, simply omit it."
            "\n\nAlso, use your general knowledge to verify claims about "
            "product or service launches. Do not describe well-known "
            'existing products as "new" or "upcoming" just because sources '
            "mention advertising or marketing activity for them."
        )
        system = (prompt or self.executive_prompt) + bias_block + guardrail

        user = (
            "Here are the individual source summaries for today:\n\n"
            f"{all_summaries}"
        )

        if prior_context:
            user += f"\n\n---\n\n{prior_context}"

        logger.info(
            "Generating executive summary from %d sources, model=%s",
            len(source_summaries),
            self.executive_model,
        )

        cache_key = self._make_cache_key(
            "executive", system, user, self.executive_model,
        )
        return await self._chat(
            system, user, max_tokens=4096, model=self.executive_model,
            cache_key=cache_key,
        )

    async def generate_podcast_script(
        self,
        executive_summary: str,
        date: str,
        prompt: Optional[str] = None,
    ) -> str:
        """Generate a podcast monologue script from the executive summary.

        Uses OpenAI structured output to return JSON with sections, each
        containing a topic name (or null for intro/outro) and spoken text.
        This keeps topic headers out of the TTS pipeline entirely.

        Template variables ``{title}``, ``{host_name}``, and ``{date}``
        in the prompt are substituted with the configured values.

        Args:
            executive_summary: The executive summary text.
            date: Date string (YYYY-MM-DD) for the greeting.
            prompt: Custom podcast prompt override.

        Returns:
            JSON string with structured sections.
        """
        raw_prompt = prompt or self.podcast_prompt
        raw_prompt += (
            "\n\nNever mention that a topic had no news or was quiet. "
            "Only discuss topics present in the summary. "
            "Return the script as structured sections. Each section has a "
            "'topic' field (the topic name string when starting a new major "
            "topic, or null for intro/outro) and a 'text' field (the spoken "
            "content only — no markdown, no formatting, no asterisks). "
            "Don't set a topic for the opening greeting or closing sign-off."
        )
        if self.uses_deepseek:
            raw_prompt += (
                "\n\nReturn a JSON object with this exact shape and nothing "
                'else: {{"sections": [{{"topic": string|null, "text": '
                'string}}, ...]}}. No prose before or after the JSON.'
            )
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

        logger.info(
            "Generating podcast script for %s, model=%s",
            date,
            self.script_model,
        )

        use_model = self.script_model

        cache_key = self._make_cache_key(
            "script", system, user, use_model,
        )

        # Check cache
        if cache_key and self.store:
            cached = self.store.get_llm_cache(cache_key)
            if cached is not None:
                logger.info("Cache hit for script, skipping API call")
                return cached

        if self.uses_deepseek:
            response_format = {"type": "json_object"}
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "podcast_script",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "sections": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "topic": {
                                            "type": ["string", "null"],
                                            "description": (
                                                "Topic name for a new major "
                                                "topic, or null for "
                                                "intro/outro."
                                            ),
                                        },
                                        "text": {
                                            "type": "string",
                                            "description": (
                                                "Spoken content only — no "
                                                "markdown or formatting."
                                            ),
                                        },
                                    },
                                    "required": ["topic", "text"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["sections"],
                        "additionalProperties": False,
                    },
                },
            }

        response = await self.client.chat.completions.create(
            model=use_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=8192,
            temperature=0.7,
            response_format=response_format,
        )
        result = response.choices[0].message.content.strip()

        # Store in cache
        if cache_key and self.store:
            self.store.set_llm_cache(
                cache_key, "script", result, use_model,
            )

        return result

    async def generate_show_notes(
        self,
        executive_summary: str,
        date: str,
        prompt: Optional[str] = None,
    ) -> str:
        """Generate listener-facing show notes from the executive summary.

        Returns a JSON string with the shape consumed by
        ``telegram_translator.show_notes``::

            {"lead": str,
             "topics": [{"headline": str,
                         "paragraph": str,
                         "verdict": str}, ...]}

        This is a separate artifact from the executive summary (host
        brief, internal scaffolding) and the podcast script (audio
        monologue, may keep edgy host voice). The show notes are the
        polished, clean text that lands on the public episode page.
        Branches on ``self.uses_deepseek`` the same way ``generate_podcast_script``
        does, because DeepSeek supports ``json_object`` but not strict
        ``json_schema``.

        Args:
            executive_summary: The executive briefing text.
            date: Date string (YYYY-MM-DD) for template substitution.
            prompt: Optional per-podcast prompt override; falls back to
                ``self.show_notes_prompt``.

        Returns:
            JSON string with the validated show-notes shape.
        """
        raw_prompt = prompt or self.show_notes_prompt
        if self.uses_deepseek:
            raw_prompt += (
                "\n\nReturn a JSON object with this exact shape and "
                'nothing else: {{"lead": string, "topics": '
                '[{{"headline": string, "paragraph": string, "verdict": '
                "string}}, ...]}}. No prose before or after the JSON."
            )
        template_vars = defaultdict(
            str,
            title=self.title,
            host_name=self.host_name,
            date=date,
        )
        system = raw_prompt.format_map(template_vars)

        user = (
            f"Date: {date}\n\n"
            "Executive briefing to turn into listener-facing show "
            f"notes:\n\n{executive_summary}"
        )

        logger.info(
            "Generating show notes for %s, model=%s",
            date,
            self.script_model,
        )

        use_model = self.script_model

        cache_key = self._make_cache_key(
            "show_notes", system, user, use_model,
        )

        if cache_key and self.store:
            cached = self.store.get_llm_cache(cache_key)
            if cached is not None:
                logger.info("Cache hit for show_notes, skipping API call")
                return cached

        if self.uses_deepseek:
            response_format = {"type": "json_object"}
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "show_notes",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "lead": {
                                "type": "string",
                                "description": (
                                    "Short 1-2 sentence teaser for the "
                                    "episode."
                                ),
                            },
                            "topics": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "headline": {
                                            "type": "string",
                                            "description": (
                                                "Plain-text headline. "
                                                "No markdown."
                                            ),
                                        },
                                        "paragraph": {
                                            "type": "string",
                                            "description": (
                                                "Factual paragraph in "
                                                "the host's voice."
                                            ),
                                        },
                                        "verdict": {
                                            "type": "string",
                                            "description": (
                                                "Single-line verdict in "
                                                "the host's voice."
                                            ),
                                        },
                                    },
                                    "required": [
                                        "headline", "paragraph", "verdict",
                                    ],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["lead", "topics"],
                        "additionalProperties": False,
                    },
                },
            }

        response = await self.client.chat.completions.create(
            model=use_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=4096,
            temperature=0.5,
            response_format=response_format,
        )
        result = response.choices[0].message.content.strip()

        if cache_key and self.store:
            self.store.set_llm_cache(
                cache_key, "show_notes", result, use_model,
            )

        return result
