"""Listener-facing podcast show-notes: parsing + deterministic rendering.

The LLM produces a structured JSON artifact (see schema in
``summarizer.generate_show_notes``). This module turns that artifact into
the Markdown body and the short description used by the Astro publisher.

Keeping the renderer here — pure functions, no IO — means the publisher
never touches free-form text from the LLM with regex hacks. The bug that
collapsed ``ИИ-кодинг`` to ``ИИкодинг`` (and similar) is structurally
impossible from this code path because no character-stripping happens.
"""

from __future__ import annotations

import json
import re


# Truncation cap matches what fits cleanly into OG/Twitter description meta
# tags without wrapping in the episode-list teaser on the Vaske site.
DEFAULT_DESCRIPTION_MAX = 260

# Sentence enders we accept as a clean break point when truncating.
_SENTENCE_ENDERS = (".", "!", "?", "…")


def parse_show_notes(raw: str) -> dict:
    """Parse the LLM's show-notes JSON and validate the shape.

    Args:
        raw: JSON string produced by ``Summarizer.generate_show_notes``.

    Returns:
        Dict with keys ``lead`` (str) and ``topics`` (list of
        ``{headline, paragraph, verdict}`` dicts).

    Raises:
        ValueError: If JSON is malformed or required fields are missing.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"show_notes is not valid JSON: {e}") from e

    if not isinstance(data, dict):
        raise ValueError("show_notes must be a JSON object")

    lead = data.get("lead")
    if not isinstance(lead, str):
        raise ValueError("show_notes.lead must be a string")

    topics = data.get("topics")
    if not isinstance(topics, list):
        raise ValueError("show_notes.topics must be a list")

    cleaned_topics = []
    for idx, topic in enumerate(topics):
        if not isinstance(topic, dict):
            raise ValueError(
                f"show_notes.topics[{idx}] must be an object"
            )
        for field in ("headline", "paragraph", "verdict"):
            value = topic.get(field)
            if not isinstance(value, str):
                raise ValueError(
                    f"show_notes.topics[{idx}].{field} must be a string"
                )
        cleaned_topics.append(
            {
                "headline": topic["headline"].strip(),
                "paragraph": topic["paragraph"].strip(),
                "verdict": topic["verdict"].strip(),
            }
        )

    return {"lead": lead.strip(), "topics": cleaned_topics}


def render_description(
    show_notes: dict, max_chars: int = DEFAULT_DESCRIPTION_MAX,
) -> str:
    """Render the YAML frontmatter ``description`` field.

    The description is derived from ``lead`` only — never from the body
    paragraphs — so production scaffolding from the executive prompt
    (e.g. ``Вот executive-обзор … Факты, оценки, углы атаки.``) cannot
    leak in. Hyphens and inter-word spacing are preserved verbatim;
    truncation breaks at the last sentence ender within ``max_chars``,
    falling back to a hard cut + ellipsis only if no break is available.

    Args:
        show_notes: Parsed show-notes dict (see ``parse_show_notes``).
        max_chars: Soft cap on output length, including ellipsis.

    Returns:
        Plain-text description suitable for YAML quoting.
    """
    lead = re.sub(r"\s+", " ", show_notes["lead"]).strip()
    if not lead:
        return ""
    if len(lead) <= max_chars:
        return lead

    head = lead[:max_chars]
    best_break = -1
    for ender in _SENTENCE_ENDERS:
        best_break = max(best_break, head.rfind(ender))
    if best_break >= max_chars // 2:
        return head[: best_break + 1].rstrip()

    return head.rstrip() + "…"


def render_body(show_notes: dict, verdict_label: str) -> str:
    """Render the Markdown body for the episode ``.md`` file.

    Composes deterministically — no regex, no character stripping. The
    body opens with the lead paragraph, then one block per topic:
    ``### {headline}`` / paragraph / ``**{verdict_label}:** {verdict}``.

    Args:
        show_notes: Parsed show-notes dict (see ``parse_show_notes``).
        verdict_label: Per-podcast label for the verdict line (e.g.
            ``"Вердикт Ваське"`` for vaske_daily, ``"Verdict"`` default).

    Returns:
        Markdown body string. No trailing newline; callers append one.
    """
    parts: list[str] = []
    lead = show_notes["lead"].strip()
    if lead:
        parts.append(lead)

    label = verdict_label.strip() or "Verdict"
    for topic in show_notes["topics"]:
        headline = topic["headline"].strip()
        paragraph = topic["paragraph"].strip()
        verdict = topic["verdict"].strip()
        block = [f"### {headline}", paragraph, f"**{label}:** {verdict}"]
        parts.append("\n\n".join(b for b in block if b))

    return "\n\n".join(parts)
