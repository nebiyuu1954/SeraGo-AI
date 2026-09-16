"""Token counting for LLM calls.

Two sources of truth, used for different purposes:

1. Provider usage (authoritative) — Groq returns a ``usage`` field on every
   chat completion: ``prompt_tokens`` (what was SENT, counted by the model),
   ``completion_tokens`` (what was RECEIVED/generated), ``total_tokens``.
   These are exact and are what we store as tokens_sent / tokens_received /
   total_tokens.

2. Local estimate (tiktoken) — used to count BEFORE sending (we cannot know
   the provider's exact count until it responds) and as a cross-check against
   the provider usage. tiktoken is the same BPE tokenizer family OpenAI-
   compatible models use, so the estimate is close — but the provider count
   always wins.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("ai_service.tokens")


# ── Local estimate (tiktoken) ───────────────────────────────────────────

# Map model names to tiktoken encodings. Unknown models fall back to a
# reasonable default (cl100k_base, the gpt-3.5/gpt-4 family tokenizer).
_MODEL_ENCODING_OVERRIDES: dict[str, str] = {
    "openai/gpt-oss-120b": "o200k_base",
    "gpt-oss-120b": "o200k_base",
    "gpt-oss-20b": "o200k_base",
    "openai/gpt-oss-20b": "o200k_base",
}

_encoding_cache: dict[str, Any] = {}


def _get_encoding(model: str | None) -> Any | None:
    """Return the tiktoken encoding for a model, or None if unavailable."""
    try:
        import tiktoken
    except ImportError:
        return None

    name = (model or "").strip().lower()
    if name in _encoding_cache:
        return _encoding_cache[name]

    encoding: Any | None = None
    override = _MODEL_ENCODING_OVERRIDES.get(name)
    if override:
        try:
            encoding = tiktoken.get_encoding(override)
        except Exception:  # noqa: BLE001 — fall through to model lookup
            encoding = None

    if encoding is None:
        try:
            encoding = tiktoken.encoding_for_model(name)
        except Exception:  # noqa: BLE001 — unknown model
            try:
                encoding = tiktoken.get_encoding("cl100k_base")
            except Exception:  # noqa: BLE001 — nothing works, give up
                encoding = None

    _encoding_cache[name] = encoding
    return encoding


def count_tokens(text: str, model: str | None = None) -> int:
    """Local token estimate for a piece of text.

    Uses tiktoken when available (accurate for OpenAI-compatible models).
    Falls back to a chars/4 heuristic when tiktoken is missing or the model
    is unknown — clearly an estimate, never the source of truth.
    """
    if not text:
        return 0

    encoding = _get_encoding(model)
    if encoding is not None:
        try:
            return len(encoding.encode(text))
        except Exception:  # noqa: BLE001 — fall back to heuristic
            pass

    # Heuristic fallback: ~4 chars per token (varies by language/model).
    return max(1, len(text) // 4)


def count_request_tokens(request_payload: dict[str, Any], model: str | None = None) -> int:
    """Local token estimate for a full chat-completions request payload.

    Counts every message's content plus the standard per-message overhead
    OpenAI-compatible APIs apply. This is the number we expect the provider
    to report as prompt_tokens (before its own chat-template additions).
    """
    messages = request_payload.get("messages") if isinstance(request_payload, dict) else None
    if not isinstance(messages, list):
        return 0

    total = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            total += count_tokens(content, model)
        elif isinstance(content, list):
            # Multimodal content parts — count text parts only.
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total += count_tokens(part["text"], model)
        total += 3  # per-message overhead (role markers etc.)

    total += 3  # reply-priming overhead
    return total


# ── Provider usage (authoritative) ──────────────────────────────────────

def parse_provider_usage(response_payload: dict[str, Any] | None) -> dict[str, int | None]:
    """Extract exact token counts from a Groq chat-completions response.

    Returns ``{"prompt_tokens": ..., "completion_tokens": ..., "total_tokens": ...}``
    with None for any field the provider did not return. These are the counts
    we store as the source of truth — the model counted them itself.
    """
    if not isinstance(response_payload, dict):
        return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}

    usage = response_payload.get("usage")
    if not isinstance(usage, dict):
        return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}

    def _int_or_none(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "prompt_tokens": _int_or_none(usage.get("prompt_tokens")),
        "completion_tokens": _int_or_none(usage.get("completion_tokens")),
        "total_tokens": _int_or_none(usage.get("total_tokens")),
    }