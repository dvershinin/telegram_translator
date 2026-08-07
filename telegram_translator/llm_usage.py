"""Token-only DeepSeek response accounting for the shared billing ledger."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional
from urllib import request

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "http://127.0.0.1:7863/usage/deepseek"
USAGE_MARKER = "LLM_USAGE "


def _value(obj: object, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def deepseek_usage_event(
    response: object, *, requested_model: object, project: str, callsite: str
) -> Optional[Dict[str, Any]]:
    """Build a content-free event from one exact DeepSeek response."""
    model = str(_value(response, "model") or requested_model or "").strip()
    if not model.casefold().startswith("deepseek"):
        return None
    usage = _value(response, "usage")
    values = {
        "prompt_tokens": _value(usage, "prompt_tokens"),
        "prompt_cache_hit_tokens": _value(usage, "prompt_cache_hit_tokens"),
        "prompt_cache_miss_tokens": _value(usage, "prompt_cache_miss_tokens"),
        "completion_tokens": _value(usage, "completion_tokens"),
    }
    request_id = _value(response, "id")
    if (
        not isinstance(request_id, str)
        or not request_id.strip()
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values.values()
        )
        or values["prompt_tokens"]
        != values["prompt_cache_hit_tokens"] + values["prompt_cache_miss_tokens"]
    ):
        logger.warning("DeepSeek response omitted exact usage accounting fields")
        return None
    event: Dict[str, Any] = {
        "request_id": request_id.strip(),
        "project": project,
        "callsite": callsite,
        "model": model,
        "usage": values,
    }
    occurred_at = _value(response, "created")
    if isinstance(occurred_at, (str, int, float)) and not isinstance(occurred_at, bool):
        event["occurred_at"] = occurred_at
    return event


def record_deepseek_usage(
    response: object, *, requested_model: object, project: str, callsite: str
) -> bool:
    """Deliver one exact event to the local ledger or configured JSONL log."""
    event = deepseek_usage_event(
        response, requested_model=requested_model, project=project, callsite=callsite
    )
    if event is None:
        return False
    encoded = json.dumps(event, separators=(",", ":"), ensure_ascii=True).encode()
    try:
        log_path = (os.getenv("LLM_USAGE_LOG") or "").strip()
        if log_path:
            descriptor = os.open(
                os.path.expanduser(log_path),
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            try:
                line = USAGE_MARKER.encode() + encoded + b"\n"
                if os.write(descriptor, line) != len(line):
                    raise OSError("short usage-log write")
            finally:
                os.close(descriptor)
        else:
            endpoint = (os.getenv("LLM_USAGE_ENDPOINT") or DEFAULT_ENDPOINT).strip()
            outbound = request.Request(
                endpoint,
                data=encoded,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with request.urlopen(outbound, timeout=2.0) as response_handle:
                if response_handle.status not in {200, 201}:
                    raise OSError("usage endpoint rejected event")
    except (OSError, ValueError):
        logger.warning("Could not record exact DeepSeek usage", exc_info=True)
        return False
    return True
