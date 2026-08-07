"""Environment-driven LLM model and provider selection.

No model identifier is hardcoded in this package. Each podcast names an LLM
*role* in its config (``llm_role: writer``), and the role's model, base URL, and
API key come from the environment. Switching model or provider is therefore a
change to the ``LLM model routing`` block in ``~/.secrets`` and nothing else.

``scripts/daily_podcasts.sh`` sources ``~/.secrets`` before running the
pipeline, so the cron job sees these variables.

Resolution fails loudly. A missing or empty variable raises rather than falling
back to a default, because a silent default is how a pipeline ends up quietly
paying 2024 prices on a 2026 workload.
"""

from __future__ import annotations

import os
from typing import Any, Mapping, NamedTuple, Optional

ROLE_VARIABLE_SUFFIXES = ("MODEL", "BASE_URL", "API_KEY")

#: Role used when a podcast config does not name one.
DEFAULT_ROLE = "writer"


class LLMRole(NamedTuple):
    """One resolved LLM role.

    Attributes:
        model: Provider model identifier, e.g. ``"deepseek-v4-flash"``.
        base_url: OpenAI-compatible API base URL.
        api_key: API key for that base URL.
        thinking: Explicit DeepSeek thinking intent, ``"enabled"`` or
            ``"disabled"``. ``None`` leaves the request unshaped, which is only
            correct for non-DeepSeek providers.
    """

    model: str
    base_url: str
    api_key: str
    thinking: Optional[str] = None


def require_role(role: str) -> LLMRole:
    """Resolve one LLM role from the environment.

    Args:
        role: Role name such as ``"fast"`` or ``"writer"``. Case-insensitive.

    Returns:
        The resolved model, base URL, API key, and thinking intent.

    Raises:
        RuntimeError: If any of the role's required variables is unset or empty.
    """
    prefix = f"LLM_{role.upper()}_"
    names = [prefix + suffix for suffix in ROLE_VARIABLE_SUFFIXES]
    values = [(os.environ.get(name) or "").strip() for name in names]
    missing = [name for name, value in zip(names, values) if not value]
    if missing:
        raise RuntimeError(
            f"LLM role {role.upper()!r} is not configured: "
            f"{', '.join(missing)} unset or empty. Define the role in the "
            f"'LLM model routing' block of ~/.secrets, then `source ~/.secrets`."
        )
    thinking = (os.environ.get(prefix + "THINKING") or "").strip() or None
    return LLMRole(*values, thinking=thinking)


def is_deepseek(model: object) -> bool:
    """Report whether a model identifier belongs to DeepSeek."""
    return str(model or "").casefold().startswith("deepseek")


def thinking_extra_body(
    role: LLMRole,
    extra_body: Optional[Mapping[str, Any]] = None,
) -> Optional[dict]:
    """Return ``extra_body`` carrying an explicit DeepSeek thinking intent.

    DeepSeek V4 treats an omitted ``thinking`` field as thinking *enabled*, so a
    request that says nothing silently becomes a slower, costlier
    hidden-reasoning call whose ``content`` can come back empty. Every DeepSeek
    request must state its intent.

    Args:
        role: The resolved role whose model and thinking intent apply.
        extra_body: Existing extra body, if any.

    Returns:
        For non-DeepSeek models, ``extra_body`` unchanged. For DeepSeek models, a
        copy carrying a ``thinking`` entry: the caller's own value when present,
        otherwise the role's configured intent, otherwise disabled.
    """
    if not is_deepseek(role.model):
        return dict(extra_body) if extra_body is not None else None
    body = dict(extra_body or {})
    body.setdefault("thinking", {"type": role.thinking or "disabled"})
    return body


def strict_schema_base_url(base_url: str) -> str:
    """Return the base URL that actually enforces strict tool schemas.

    DeepSeek only honours ``strict: true`` on a function schema when the request
    goes to its ``/beta`` endpoint. Characterised live 2026-08-07: a schema that
    violates strict-mode rules (a property missing from ``required``) is
    **rejected** on ``/beta`` with "Required properties must match all
    properties in the object", but **silently accepted** on ``/v1``, which
    returns unconstrained output. Sending ``strict`` to ``/v1`` therefore buys
    nothing while looking like a guarantee.

    Note this is about *tool* schemas. ``response_format: {"type":
    "json_schema"}`` is rejected on both endpoints with "This response_format
    type is unavailable now", so strict tool calling is the only route to an
    enforced schema on DeepSeek.

    Args:
        base_url: The role's configured base URL.

    Returns:
        The ``/beta`` variant for DeepSeek hosts, otherwise ``base_url``.
    """
    if "deepseek" not in base_url.casefold():
        return base_url
    trimmed = base_url.rstrip("/")
    for suffix in ("/v1", "/beta"):
        if trimmed.endswith(suffix):
            trimmed = trimmed[: -len(suffix)]
            break
    return f"{trimmed}/beta"


def pins_default_temperature(model: object) -> bool:
    """Report whether a model rejects any explicit ``temperature``.

    The OpenAI GPT-5 family accepts only the default temperature and returns
    HTTP 400 for anything else (characterised live 2026-08-07 against
    ``gpt-5.6-luna``). DeepSeek accepts explicit temperatures normally.
    """
    return str(model or "").casefold().startswith("gpt-5")


def completion_kwargs(
    model: object,
    max_output_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> dict:
    """Build provider-portable chat-completion keyword arguments.

    Normalises two incompatibilities so no call site has to know them: the
    GPT-5 family rejects ``max_tokens`` (use ``max_completion_tokens``, which
    DeepSeek also accepts) and rejects any explicit ``temperature``.

    Args:
        model: Target model identifier.
        max_output_tokens: Output-token ceiling, or None to leave unset.
        temperature: Desired sampling temperature, or None to leave default.

    Returns:
        Keyword arguments to splat into ``chat.completions.create``.
    """
    kwargs: dict = {}
    if max_output_tokens is not None:
        kwargs["max_completion_tokens"] = max_output_tokens
    if temperature is not None and not pins_default_temperature(model):
        kwargs["temperature"] = temperature
    return kwargs
