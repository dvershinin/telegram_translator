"""LLM-based summarization for the digest pipeline."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional

import openai

from telegram_translator import anthropic_chat
from telegram_translator.content_store import ContentItem
from telegram_translator.llm_env import (
    DEFAULT_ROLE,
    completion_kwargs,
    is_deepseek,
    require_role,
    strict_schema_base_url,
    thinking_extra_body,
)
from telegram_translator.llm_usage import record_deepseek_usage

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


# Structured-output schemas. Shared by the Claude `output_config.format` path,
# the OpenAI `json_schema` path and the DeepSeek strict tool-call path, so every
# provider is held to the exact same contract instead of a prose description.
PODCAST_SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": ["string", "null"],
                        "description": "Topic name for a new major topic, or null for intro/outro.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Spoken content only — no markdown or formatting.",
                    },
                },
                "required": ["topic", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["sections"],
    "additionalProperties": False,
}

SHOW_NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "lead": {
            "type": "string",
            "description": "Short 1-2 sentence teaser for the episode.",
        },
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "headline": {
                        "type": "string",
                        "description": "Plain-text headline. No markdown.",
                    },
                    "paragraph": {
                        "type": "string",
                        "description": "Factual paragraph in the host's voice.",
                    },
                    "verdict": {
                        "type": "string",
                        "description": "Single-line verdict in the host's voice.",
                    },
                },
                "required": ["headline", "paragraph", "verdict"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["lead", "topics"],
    "additionalProperties": False,
}


# Scored selection (opt-in per podcast via `selection_scoring`). Adapted from
# Horizon's tech-news rubric: the podcast's selection_prompt says WHAT belongs,
# this rubric says how much it matters, so a threshold and a story cap can pick
# the strongest stories instead of passing every on-topic item through.
_SCORING_RUBRIC = (
    "Score every article from 0 to 10 for how much it deserves airtime in "
    "today's episode:\n"
    "- 9-10: Major developments: wars escalating or ending, landmark "
    "agreements, breakthroughs, industry-changing announcements.\n"
    "- 7-8: Important developments worth prompt attention, with concrete "
    "facts and clear consequences.\n"
    "- 5-6: Interesting but incremental or non-urgent.\n"
    "- 3-4: Routine updates, shallow coverage, promotion-dominated.\n"
    "- 0-2: Outside the selection criteria, noise, spam, or clickbait.\n"
    "Judge the substance, not the headline: do not reward exaggerated "
    "headlines or popularity alone. Articles may be in any language "
    "(Russian, Ukrainian, English, etc.); score all of them by topic and "
    "substance regardless of language. Score every numbered article "
    "exactly once. Keep each reason under 12 words."
)

SCORING_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {
                        "type": "integer",
                        "description": "The article's catalog number.",
                    },
                    "score": {
                        "type": "integer",
                        "description": "Airtime score from 0 to 10.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why, in under 12 words.",
                    },
                },
                "required": ["n", "score", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

STORY_GROUPING_SCHEMA = {
    "type": "object",
    "properties": {
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Short neutral name of the story.",
                    },
                    "items": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Catalog numbers covering this story.",
                    },
                },
                "required": ["label", "items"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["stories"],
    "additionalProperties": False,
}

_STORY_GROUPING_SYSTEM = (
    "You group news articles by the real-world story they cover. Articles "
    "from different outlets, possibly in different languages, about the same "
    "event or development belong to one story. Articles about merely related "
    "topics (two separate events in the same war, two different AI model "
    "releases) are different stories. Put every numbered article in exactly "
    "one story; an article with no counterpart is a story of its own. Give "
    "each story a short neutral label in English that names no outlet."
)


@dataclass
class ScoredItem:
    """A content item with its airtime score."""

    item: ContentItem
    score: int
    reason: str = ""


@dataclass
class Story:
    """Items from one or more sources that cover the same real-world story."""

    label: str
    members: list[ScoredItem] = field(default_factory=list)

    @property
    def score(self) -> int:
        """Return the best member score."""
        return max(member.score for member in self.members)

    @property
    def sources(self) -> list[str]:
        """Return distinct source names in first-seen order."""
        return list(dict.fromkeys(m.item.source_name for m in self.members))


@dataclass
class StorySelection:
    """Outcome of scored selection: kept items, kept stories, and a report."""

    items: list[ContentItem]
    stories: list[Story]
    report: dict


def rank_stories(stories: list[Story], max_stories: int) -> list[Story]:
    """Order stories by importance and keep the top ``max_stories``.

    Best member score ranks first. Coverage by more distinct sources breaks
    ties (corroboration), then the number of articles. The sort is stable, so
    remaining ties keep the grouping order.

    Args:
        stories: Candidate stories; every story has at least one member.
        max_stories: Maximum number of stories to keep.

    Returns:
        The kept stories, most important first.
    """
    ranked = sorted(
        stories,
        key=lambda story: (story.score, len(story.sources), len(story.members)),
        reverse=True,
    )
    return ranked[:max_stories]


def _parse_json_payload(payload: str, label: str) -> dict:
    """Parse a structured-output payload, failing loudly on garbage."""
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label}: provider returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label}: provider returned a non-object payload")
    return parsed


def _catalog(items: list[ContentItem], preview_chars: int) -> str:
    """Render a 1-based numbered catalog of titles plus content previews."""
    lines = []
    for i, item in enumerate(items, 1):
        title = item.title or "(no title)"
        preview = item.content[:preview_chars].replace("\n", " ")
        lines.append(f"{i}. {title} — {preview}")
    return "\n".join(lines)


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
        role_name = config.get("llm_role") or DEFAULT_ROLE
        self.role = require_role(role_name)
        self.uses_deepseek = is_deepseek(self.role.model)
        self.uses_anthropic = anthropic_chat.is_anthropic(self.role.model)
        self.effort = (
            anthropic_chat.role_effort(role_name) if self.uses_anthropic else None
        )
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
        self.podcast_prompt = config.get("podcast_prompt", _DEFAULT_PODCAST_PROMPT)
        self.show_notes_prompt = config.get(
            "show_notes_prompt", _DEFAULT_SHOW_NOTES_PROMPT
        )

        self.api_base = self.role.base_url
        # Claude roles go through the native Anthropic SDK; see anthropic_chat.
        self.anthropic_client = (
            anthropic_chat.make_client(self.role) if self.uses_anthropic else None
        )
        self.client = openai.AsyncOpenAI(
            api_key=self.role.api_key,
            base_url=self.role.base_url,
        )

        # Structured output goes through a second client: DeepSeek only enforces
        # `strict: true` on its /beta endpoint, and a client's base_url is fixed
        # at construction. For non-DeepSeek providers this is the same URL, so
        # the strict path can always use it.
        strict_base = strict_schema_base_url(self.role.base_url)
        self.strict_client = (
            self.client
            if strict_base == self.role.base_url
            else openai.AsyncOpenAI(api_key=self.role.api_key, base_url=strict_base)
        )

    @staticmethod
    def _date_for_prompt(date: str) -> str:
        """Render a digest date so the model never has to guess the weekday.

        Hosts naturally open with "It's Thursday, August 7th". Given only
        ``2026-08-07`` a model will invent the day name, and on 2026-08-07 one
        did: it said Thursday for a Friday. The weekday is stated explicitly so
        the script cannot be wrong about it. The ISO form is kept alongside so
        anything that reads the date out of the prompt still finds it, and the
        weekday is given in English because every prompt already instructs the
        model to match the briefing's language.

        Args:
            date: Digest date in ``YYYY-MM-DD`` form.

        Returns:
            ``"2026-08-07 (Friday)"``, or the input unchanged if unparseable.
        """
        try:
            parsed = datetime.strptime(date, "%Y-%m-%d")
        except (TypeError, ValueError):
            return date
        return f"{date} ({parsed.strftime('%A')})"

    async def _structured_chat(
        self,
        system: str,
        user: str,
        schema_name: str,
        schema: dict,
        max_tokens: int,
        temperature: float,
        model: str,
    ) -> str:
        """Request JSON constrained by ``schema`` and return it as a string.

        Every provider is held to the same schema, by different mechanisms:

        * Claude takes ``output_config.format`` natively (``anthropic_chat``).
        * OpenAI accepts ``response_format: {"type": "json_schema", ...}``.
        * DeepSeek rejects that outright ("This response_format type is
          unavailable now") on both ``/v1`` and ``/beta``. Its only enforced
          route is a **strict tool call**, and only on ``/beta`` — ``/v1``
          silently ignores ``strict``. So the schema is presented as a forced
          single-function tool and the arguments are the payload.

        This replaces the previous DeepSeek path, which asked for
        ``{"type": "json_object"}`` plus a prose description of the shape. That
        mode is documented to "occasionally return empty content", which is
        exactly the failure already seen elsewhere in the fleet.

        Args:
            system: System prompt.
            user: User message.
            schema_name: Name for the schema/function.
            schema: JSON Schema the response must satisfy.
            max_tokens: Output-token ceiling.
            temperature: Sampling temperature.
            model: Model identifier.

        Returns:
            The JSON payload as a string.

        Raises:
            ValueError: If the provider returns no usable structured payload.
        """
        if self.anthropic_client is not None:
            return await anthropic_chat.complete_json(
                self.anthropic_client,
                self.role,
                system,
                user,
                schema,
                max_tokens=max_tokens,
                effort=self.effort,
                label=schema_name,
            )

        shared = dict(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            extra_body=thinking_extra_body(self.role),
            **completion_kwargs(
                model, max_output_tokens=max_tokens, temperature=temperature
            ),
        )

        if self.uses_deepseek:
            response = await self.strict_client.chat.completions.create(
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": schema_name,
                            "description": f"Return the {schema_name} payload.",
                            "strict": True,
                            "parameters": schema,
                        },
                    }
                ],
                tool_choice={
                    "type": "function",
                    "function": {"name": schema_name},
                },
                **shared,
            )
            record_deepseek_usage(
                response,
                requested_model=model,
                project="telegram-translator",
                callsite=f"digest.structured.{schema_name}",
            )
            calls = response.choices[0].message.tool_calls
            if not calls:
                raise ValueError(
                    f"{schema_name}: provider returned no tool call; "
                    "cannot recover a structured payload"
                )
            return calls[0].function.arguments.strip()

        response = await self.client.chat.completions.create(
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
            **shared,
        )
        record_deepseek_usage(
            response,
            requested_model=model,
            project="telegram-translator",
            callsite=f"digest.structured.{schema_name}",
        )
        content = response.choices[0].message.content
        if not content or not content.strip():
            raise ValueError(f"{schema_name}: provider returned empty content")
        return content.strip()

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
                logger.info("Cache hit for %s, skipping API call", stage)
                return cached

        stage = cache_key.split(":")[0] if cache_key else "uncached"
        if self.anthropic_client is not None:
            result = await anthropic_chat.complete_text(
                self.anthropic_client,
                self.role,
                system,
                user,
                max_tokens=max_tokens,
                effort=self.effort,
                label=stage,
            )
            if cache_key and self.store:
                self.store.set_llm_cache(cache_key, stage, result, use_model)
            return result

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
        record_deepseek_usage(
            response,
            requested_model=use_model,
            project="telegram-translator",
            callsite=f"digest.{stage}",
        )
        result = response.choices[0].message.content.strip()

        # Store in cache
        if cache_key and self.store:
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
                source_items,
                selection_prompt,
                source_name,
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
        catalog = _catalog(items, 400)

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
            "selection",
            system,
            user,
            self.selection_model,
            extra=source_name,
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
                "No articles selected from %s",
                source_name,
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
            len(selected),
            len(items),
            source_name,
        )
        return selected

    async def _cached_structured(
        self,
        stage: str,
        system: str,
        user: str,
        schema_name: str,
        schema: dict,
        max_tokens: int,
        extra: str = "",
    ) -> dict:
        """Run a cached structured call and return the parsed object.

        Args:
            stage: Cache stage name.
            system: System prompt.
            user: User message.
            schema_name: Name for the schema/function.
            schema: JSON Schema the response must satisfy.
            max_tokens: Output-token ceiling.
            extra: Optional extra cache-key component.

        Returns:
            The parsed JSON object.

        Raises:
            ValueError: If the payload is not a JSON object.
        """
        model = self.selection_model
        cache_key = self._make_cache_key(stage, system, user, model, extra=extra)
        if cache_key and self.store:
            cached = self.store.get_llm_cache(cache_key)
            if cached is not None:
                logger.info("Cache hit for %s, skipping API call", stage)
                return _parse_json_payload(cached, schema_name)

        payload = await self._structured_chat(
            system,
            user,
            schema_name,
            schema,
            max_tokens=max_tokens,
            temperature=0.0,
            model=model,
        )
        # Parse before caching so a malformed payload is never replayed.
        parsed = _parse_json_payload(payload, schema_name)
        if cache_key and self.store:
            self.store.set_llm_cache(cache_key, stage, payload, model)
        return parsed

    async def score_content(
        self,
        items: list[ContentItem],
        selection_prompt: str,
    ) -> list[ScoredItem]:
        """Score every item 0-10 for airtime, one call per source.

        Per-source batching keeps small sources from being drowned out in a
        large combined catalog, as in ``select_content``.

        Args:
            items: Collected content items.
            selection_prompt: The podcast's inclusion criteria.

        Returns:
            One ScoredItem per input item, in input order. Items the model
            did not score get 0.
        """
        by_source: dict[str, list[ContentItem]] = {}
        for item in items:
            by_source.setdefault(item.source_name, []).append(item)

        system = f"Selection criteria: {selection_prompt}\n\n{_SCORING_RUBRIC}"
        scored: dict[int, ScoredItem] = {}
        for source_name, source_items in by_source.items():
            user = f"Articles from {source_name}:\n{_catalog(source_items, 400)}"
            logger.info(
                "Scoring %s: %d candidates, model=%s",
                source_name,
                len(source_items),
                self.selection_model,
            )
            parsed = await self._cached_structured(
                "scoring",
                system,
                user,
                "article_scores",
                SCORING_SCHEMA,
                max_tokens=8192,
                extra=source_name,
            )
            results: dict[int, ScoredItem] = {}
            for entry in parsed.get("items", []):
                n = entry.get("n")
                if not isinstance(n, int) or not 1 <= n <= len(source_items):
                    continue
                if n in results:
                    continue
                raw_score = entry.get("score")
                score = raw_score if isinstance(raw_score, int) else 0
                results[n] = ScoredItem(
                    item=source_items[n - 1],
                    score=min(max(score, 0), 10),
                    reason=str(entry.get("reason", "")),
                )
            for n, item in enumerate(source_items, 1):
                scored[id(item)] = results.get(n) or ScoredItem(
                    item=item, score=0, reason="not scored"
                )

        return [scored[id(item)] for item in items]

    async def group_stories(self, scored: list[ScoredItem]) -> list[Story]:
        """Group scored items covering the same real-world story.

        One call over all sources, so the same event reported by several
        outlets collapses into one story.

        Args:
            scored: Items that passed the score threshold.

        Returns:
            Stories covering every input item exactly once. An item the model
            did not place becomes its own story; an item placed twice stays in
            the first story that claimed it.
        """
        if len(scored) <= 1:
            return [
                Story(label=member.item.title or "(no title)", members=[member])
                for member in scored
            ]

        lines = []
        for i, member in enumerate(scored, 1):
            item = member.item
            title = item.title or "(no title)"
            preview = item.content[:300].replace("\n", " ")
            lines.append(f"{i}. ({item.source_name}) {title} — {preview}")
        user = "Articles:\n" + "\n".join(lines)

        logger.info("Grouping %d scored articles into stories", len(scored))
        parsed = await self._cached_structured(
            "stories",
            _STORY_GROUPING_SYSTEM,
            user,
            "story_groups",
            STORY_GROUPING_SCHEMA,
            max_tokens=8192,
        )

        placed: set[int] = set()
        stories: list[Story] = []
        for group in parsed.get("stories", []):
            members = []
            for n in group.get("items", []):
                if isinstance(n, int) and 1 <= n <= len(scored) and n not in placed:
                    placed.add(n)
                    members.append(scored[n - 1])
            if members:
                label = str(group.get("label") or "").strip()
                stories.append(
                    Story(label=label or members[0].item.title or "", members=members)
                )
        for n, member in enumerate(scored, 1):
            if n not in placed:
                stories.append(
                    Story(label=member.item.title or "(no title)", members=[member])
                )
        return stories

    async def select_top_stories(
        self,
        items: list[ContentItem],
        selection_prompt: str,
        threshold: float,
        max_stories: int,
    ) -> StorySelection:
        """Score, threshold, group across sources, rank, and cap.

        Args:
            items: Collected content items.
            selection_prompt: The podcast's inclusion criteria.
            threshold: Minimum score (inclusive) an item needs to survive.
            max_stories: Maximum number of stories to keep.

        Returns:
            The kept items (in input order), the kept stories (most important
            first), and a JSON-serializable report for tuning.
        """
        if not items:
            return StorySelection(items=[], stories=[], report={})

        scored = await self.score_content(items, selection_prompt)
        passed = [member for member in scored if member.score >= threshold]
        stories = await self.group_stories(passed)
        kept = rank_stories(stories, max_stories)

        kept_ids = {id(member.item) for story in kept for member in story.members}
        kept_items = [item for item in items if id(item) in kept_ids]
        kept_story_ids = {id(story) for story in kept}

        logger.info(
            "Scored selection: scored %d, passed %d, stories %d, kept %d "
            "stories / %d items",
            len(scored),
            len(passed),
            len(stories),
            len(kept),
            len(kept_items),
        )

        report = {
            "threshold": threshold,
            "max_stories": max_stories,
            "items": [
                {
                    "id": member.item.id,
                    "source": member.item.source_name,
                    "title": member.item.title or "",
                    "score": member.score,
                    "reason": member.reason,
                }
                for member in scored
            ],
            "stories": [
                {
                    "label": story.label,
                    "score": story.score,
                    "sources": story.sources,
                    "item_ids": [member.item.id for member in story.members],
                    "kept": id(story) in kept_story_ids,
                }
                for story in rank_stories(stories, len(stories))
            ],
        }
        return StorySelection(items=kept_items, stories=kept, report=report)

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
            "source_summary",
            system,
            user,
            self.summarization_model,
            extra=source_name,
        )
        return await self._chat(
            system,
            user,
            model=self.summarization_model,
            cache_key=cache_key,
        )

    async def executive_summary(
        self,
        source_summaries: dict[str, str],
        prompt: Optional[str] = None,
        source_biases: Optional[dict[str, str]] = None,
        prior_context: Optional[str] = None,
        story_ranking: Optional[list[Story]] = None,
    ) -> str:
        """Generate a cross-source executive summary.

        Args:
            source_summaries: Dict mapping source name to its summary.
            prompt: Custom executive summary prompt override.
            source_biases: Dict mapping source name to bias description.
            prior_context: Summary of recent prior episodes so the LLM
                can focus on what is new rather than re-explaining.
            story_ranking: Kept stories, most important first, from scored
                selection. Rendered with the anonymized source labels.

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
            f"## {alias_for[name]}\n\n{summary}" for name, summary in ordered_sources
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

        user = f"Here are the individual source summaries for today:\n\n{all_summaries}"

        if story_ranking:
            ranking_lines = []
            for rank, story in enumerate(story_ranking, 1):
                labels = [alias_for[s] for s in story.sources if s in alias_for]
                covered = f"; covered by {', '.join(labels)}" if labels else ""
                ranking_lines.append(
                    f"{rank}. {story.label} (importance {story.score}/10{covered})"
                )
            user += (
                "\n\n---\n\nSTORY RANKING (most important first). A story "
                "covered by several sources is one event: merge those reports "
                "into one item. Lead with higher-ranked stories.\n"
                + "\n".join(ranking_lines)
            )

        if prior_context:
            user += f"\n\n---\n\n{prior_context}"

        logger.info(
            "Generating executive summary from %d sources, model=%s",
            len(source_summaries),
            self.executive_model,
        )

        cache_key = self._make_cache_key(
            "executive",
            system,
            user,
            self.executive_model,
        )
        return await self._chat(
            system,
            user,
            max_tokens=4096,
            model=self.executive_model,
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
        # No shape description here: the schema is enforced by the provider (a
        # strict tool call on DeepSeek, json_schema on OpenAI). Telling the model
        # to emit "a JSON object and nothing else" would actively fight the tool
        # call it is supposed to produce.
        template_vars = defaultdict(
            str,
            title=self.title,
            host_name=self.host_name,
            date=self._date_for_prompt(date),
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
            "script",
            system,
            user,
            use_model,
        )

        # Check cache
        if cache_key and self.store:
            cached = self.store.get_llm_cache(cache_key)
            if cached is not None:
                logger.info("Cache hit for script, skipping API call")
                return cached

        result = await self._structured_chat(
            system,
            user,
            "podcast_script",
            PODCAST_SCRIPT_SCHEMA,
            max_tokens=8192,
            temperature=0.7,
            model=use_model,
        )

        # Store in cache
        if cache_key and self.store:
            self.store.set_llm_cache(
                cache_key,
                "script",
                result,
                use_model,
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
        Shape is enforced by ``SHOW_NOTES_SCHEMA`` through ``_structured_chat``,
        which picks the right mechanism per provider: a strict tool call on
        DeepSeek's ``/beta`` endpoint, ``json_schema`` on OpenAI.

        Args:
            executive_summary: The executive briefing text.
            date: Date string (YYYY-MM-DD) for template substitution.
            prompt: Optional per-podcast prompt override; falls back to
                ``self.show_notes_prompt``.

        Returns:
            JSON string with the validated show-notes shape.
        """
        # Shape is enforced by SHOW_NOTES_SCHEMA via the provider, not described
        # in prose. See _structured_chat.
        raw_prompt = prompt or self.show_notes_prompt
        template_vars = defaultdict(
            str,
            title=self.title,
            host_name=self.host_name,
            date=self._date_for_prompt(date),
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
            "show_notes",
            system,
            user,
            use_model,
        )

        if cache_key and self.store:
            cached = self.store.get_llm_cache(cache_key)
            if cached is not None:
                logger.info("Cache hit for show_notes, skipping API call")
                return cached

        result = await self._structured_chat(
            system,
            user,
            "show_notes",
            SHOW_NOTES_SCHEMA,
            max_tokens=4096,
            temperature=0.5,
            model=use_model,
        )

        if cache_key and self.store:
            self.store.set_llm_cache(
                cache_key,
                "show_notes",
                result,
                use_model,
            )

        return result
