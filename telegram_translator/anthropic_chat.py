"""Native Anthropic Messages API path for env-resolved LLM roles.

A role whose model is a Claude model is served through the official
``anthropic`` SDK rather than an OpenAI-compatible shim, so the digest pipeline
gets Claude's own structured outputs and refusal handling. Everything else
about the role (model, base URL, key) still comes from the environment.

Claude's current models differ from the OpenAI chat-completions surface in ways
that would otherwise surface as 400s or silent truncation:

* Sampling parameters (``temperature``) are rejected, so they are never sent.
* Thinking cannot be disabled on Claude Opus 5.5 and counts against
  ``max_tokens``. A ceiling sized for visible text alone (the selection call
  asks for 1024) would truncate the answer, so a floor is applied.
* Effort is the depth control. It comes from ``LLM_<ROLE>_EFFORT`` so it can be
  tuned per role without a code change; unset leaves the model default.
* A safety-classifier decline is an HTTP 200 with ``stop_reason="refusal"``.
  Server-side fallbacks (``fallbacks="default"``) retry it on the model
  Anthropic recommends for that category before it ever reaches us.
"""

from __future__ import annotations

import os
from typing import Any

import anthropic

from telegram_translator.llm_env import LLMRole

#: Minimum ``max_tokens`` for Claude requests. Thinking shares this budget, and
#: 16000 is the largest value the SDK accepts without streaming.
MAX_TOKENS_FLOOR = 16000

#: Beta header that gates the ``fallbacks="default"`` scalar form.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def is_anthropic(model: object) -> bool:
    """Report whether a model identifier is a Claude model."""
    return str(model or "").casefold().startswith("claude")


def role_effort(role_name: str) -> str | None:
    """Return the configured effort for a role, or None to use the default.

    Raises:
        RuntimeError: If ``LLM_<ROLE>_EFFORT`` is set to an unknown level.
    """
    name = f"LLM_{role_name.upper()}_EFFORT"
    value = (os.environ.get(name) or "").strip().casefold()
    if not value:
        return None
    if value not in _EFFORT_LEVELS:
        raise RuntimeError(
            f"{name}={value!r} is not one of {', '.join(_EFFORT_LEVELS)}."
        )
    return value


def make_client(role: LLMRole) -> anthropic.AsyncAnthropic:
    """Build an async Anthropic client bound to the role's key and base URL."""
    return anthropic.AsyncAnthropic(api_key=role.api_key, base_url=role.base_url)


def _request(
    role: LLMRole,
    system: str,
    user: str,
    max_tokens: int,
    effort: str | None,
    output_format: dict | None = None,
) -> dict[str, Any]:
    output_config: dict[str, Any] = {}
    if effort:
        output_config["effort"] = effort
    if output_format:
        output_config["format"] = output_format
    request: dict[str, Any] = dict(
        model=role.model,
        max_tokens=max(max_tokens, MAX_TOKENS_FLOOR),
        system=system,
        messages=[{"role": "user", "content": user}],
        betas=[FALLBACK_BETA],
        fallbacks="default",
    )
    if output_config:
        request["output_config"] = output_config
    return request


def _text(response: Any, label: str) -> str:
    """Return the response's visible text, failing loudly on a bad stop."""
    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None)
        raise ValueError(f"{label}: Claude declined the request (category={category})")
    if response.stop_reason == "max_tokens":
        raise ValueError(f"{label}: Claude hit max_tokens before finishing")
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise ValueError(f"{label}: Claude returned no text")
    return text


async def complete_text(
    client: anthropic.AsyncAnthropic,
    role: LLMRole,
    system: str,
    user: str,
    max_tokens: int,
    effort: str | None = None,
    label: str = "chat",
) -> str:
    """Run one system+user turn and return the plain-text answer."""
    response = await client.beta.messages.create(
        **_request(role, system, user, max_tokens, effort)
    )
    return _text(response, label)


async def complete_json(
    client: anthropic.AsyncAnthropic,
    role: LLMRole,
    system: str,
    user: str,
    schema: dict,
    max_tokens: int,
    effort: str | None = None,
    label: str = "structured",
) -> str:
    """Run one turn constrained to ``schema`` and return the JSON string."""
    response = await client.beta.messages.create(
        **_request(
            role,
            system,
            user,
            max_tokens,
            effort,
            output_format={"type": "json_schema", "schema": schema},
        )
    )
    return _text(response, label)
