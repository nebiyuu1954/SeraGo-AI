from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("ai_service.providers")


class LlmError(Exception):
    """Base exception for all LLM provider failures."""

    def __init__(
        self,
        message: str,
        provider: str = "",
        model: str = "",
        status_code: Optional[int] = None,
        raw_error: Optional[Any] = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.status_code = status_code
        self.raw_error = raw_error


class LlmRateLimitError(LlmError):
    """Raised when an LLM provider returns 429 Too Many Requests or quota exhaustion."""
    pass


@dataclass
class LlmResponse:
    """Standardized response container across all LLM providers."""
    content: str
    parsed_json: Optional[dict[str, Any]] = None
    raw_response: Optional[dict[str, Any]] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    latency_ms: int = 0
    model: str = ""
    provider: str = ""


class BaseLlmProvider(ABC):
    """Abstract interface implemented by all LLM adapters (Gemini, Groq, Ollama, DeepSeek, etc.)."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Identifier for the provider, e.g. 'gemini', 'ollama', 'groq', 'deepseek'."""
        pass

    @property
    @abstractmethod
    def model(self) -> str:
        """The specific model name configured for this provider instance."""
        pass

    @abstractmethod
    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        timeout_seconds: int = 60,
    ) -> LlmResponse:
        """Generate regular text completion (e.g. for cover letters, summaries)."""
        pass

    @abstractmethod
    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        timeout_seconds: int = 60,
    ) -> LlmResponse:
        """Generate and parse structured JSON completion (e.g. for classification)."""
        pass

    @staticmethod
    def extract_json_from_text(text: str) -> dict[str, Any]:
        """Utility helper to parse JSON, stripping markdown codeblocks if present."""
        cleaned = text.strip()
        # 1. Match code fences ```json ... ``` or ``` ... ```
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
        if fence_match:
            cleaned = fence_match.group(1).strip()
        else:
            # 2. Match outer brackets
            start_brace = cleaned.find("{")
            end_brace = cleaned.rfind("}")
            if start_brace != -1 and end_brace != -1 and end_brace > start_brace:
                cleaned = cleaned[start_brace : end_brace + 1]

        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
            raise LlmError("Expected JSON object, got non-object root")
        except json.JSONDecodeError as exc:
            raise LlmError(f"Invalid JSON returned: {exc}") from exc
