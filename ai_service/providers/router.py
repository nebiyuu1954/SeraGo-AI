from __future__ import annotations

import logging
import os
from typing import Any, Optional

from .base import BaseLlmProvider, LlmError, LlmResponse
from .gemini import GeminiProvider
from .groq import GroqProvider
from .ollama import OllamaProvider
from .openai_compat import OpenAiCompatProvider

logger = logging.getLogger("ai_service.providers.router")

# Default service-to-provider mappings
SERVICE_DEFAULTS = {
    "classification": {
        "primary_provider": "gemini",
        "primary_model": "gemini-2.0-flash",
        "fallback_provider": "groq",
        "fallback_model": "openai/gpt-oss-120b",
    },
    "cover_letter": {
        "primary_provider": "ollama",
        "primary_model": "qwen2.5:3b",
        "fallback_provider": "groq",
        "fallback_model": "openai/gpt-oss-120b",
    },
    "resume_parser": {
        "primary_provider": "gemini",
        "primary_model": "gemini-1.5-flash",
        "fallback_provider": "groq",
        "fallback_model": "openai/gpt-oss-120b",
    },
}


class LlmRouter:
    """Central gateway routing services to their configured primary and fallback LLM providers."""

    @classmethod
    def create_provider(cls, provider_name: str, model: Optional[str] = None) -> BaseLlmProvider:
        """Instantiate a provider adapter by name."""
        name = (provider_name or "").strip().lower()

        if name == "gemini":
            return GeminiProvider(model=model)
        elif name == "ollama":
            return OllamaProvider(model=model)
        elif name == "groq":
            return GroqProvider(model=model)
        elif name in ("deepseek", "openai", "openai_compat", "openrouter"):
            return OpenAiCompatProvider(model=model, provider_name=name)
        else:
            raise LlmError(f"Unknown or unsupported LLM provider: '{provider_name}'")

    @classmethod
    def get_service_config(cls, service_name: str) -> dict[str, str]:
        """Resolve primary and fallback provider and model for a given service."""
        key_prefix = service_name.upper().replace("-", "_")
        defaults = SERVICE_DEFAULTS.get(service_name.lower(), {
            "primary_provider": "gemini",
            "primary_model": "gemini-2.0-flash",
            "fallback_provider": "groq",
            "fallback_model": "llama-3.3-70b-versatile",
        })

        primary_provider = os.environ.get(
            f"{key_prefix}_AI_PROVIDER", defaults["primary_provider"]
        ).strip().lower()

        primary_model = os.environ.get(
            f"{key_prefix}_AI_MODEL", defaults["primary_model"]
        ).strip()

        fallback_provider = os.environ.get(
            f"{key_prefix}_FALLBACK_PROVIDER", defaults["fallback_provider"]
        ).strip().lower()

        fallback_model = os.environ.get(
            f"{key_prefix}_FALLBACK_MODEL", defaults["fallback_model"]
        ).strip()

        return {
            "primary_provider": primary_provider,
            "primary_model": primary_model,
            "fallback_provider": fallback_provider,
            "fallback_model": fallback_model,
        }

    @classmethod
    def execute_chat_json(
        cls,
        service_name: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        timeout_seconds: int = 60,
    ) -> LlmResponse:
        """Execute chat_json with automatic failover to the configured fallback provider."""
        return cls._execute(
            service_name=service_name,
            is_json=True,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
        )

    @classmethod
    def execute_chat(
        cls,
        service_name: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        timeout_seconds: int = 60,
    ) -> LlmResponse:
        """Execute regular text chat with automatic failover to the configured fallback provider."""
        return cls._execute(
            service_name=service_name,
            is_json=False,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
        )

    @classmethod
    def _execute(
        cls,
        service_name: str,
        is_json: bool,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
        timeout_seconds: int,
    ) -> LlmResponse:
        config = cls.get_service_config(service_name)
        primary_name = config["primary_provider"]
        primary_model = config["primary_model"]
        fallback_name = config["fallback_provider"]
        fallback_model = config["fallback_model"]

        # 1. Attempt Primary Provider
        primary_error: Optional[Exception] = None
        try:
            provider = cls.create_provider(primary_name, primary_model)
            if is_json:
                return provider.chat_json(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout_seconds=timeout_seconds,
                )
            else:
                return provider.chat(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout_seconds=timeout_seconds,
                )
        except Exception as exc:
            primary_error = exc
            logger.warning(
                "Primary provider '%s' (%s) failed for service '%s': %s. Attempting fallback '%s' (%s)...",
                primary_name,
                primary_model,
                service_name,
                exc,
                fallback_name,
                fallback_model,
            )

        # 2. Attempt Fallback Provider if configured and different
        if fallback_name and fallback_name != "none" and fallback_name != primary_name:
            try:
                fallback = cls.create_provider(fallback_name, fallback_model)
                if is_json:
                    return fallback.chat_json(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        timeout_seconds=timeout_seconds,
                    )
                else:
                    return fallback.chat(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        timeout_seconds=timeout_seconds,
                    )
            except Exception as fb_exc:
                logger.error(
                    "Fallback provider '%s' also failed for service '%s': %s",
                    fallback_name,
                    service_name,
                    fb_exc,
                )
                raise LlmError(
                    f"Both primary ({primary_name}: {primary_error}) and fallback ({fallback_name}: {fb_exc}) failed",
                    provider=fallback_name,
                    model=fallback_model,
                ) from fb_exc

        # If no fallback or fallback identical, raise the primary error
        raise primary_error
