from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

from .base import BaseLlmProvider, LlmError, LlmRateLimitError, LlmResponse

logger = logging.getLogger("ai_service.providers.groq")


class GroqProvider(BaseLlmProvider):
    """Groq Cloud LPU provider for ultra-fast text and JSON generation."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self._api_key = (
            api_key
            or os.environ.get("GROQ_API_KEY", "")
            or os.environ.get("GROQ_CONSOLE_API_KEY", "")
        ).strip()
        if not self._api_key:
            raise LlmError(
                "GROQ_API_KEY / GROQ_CONSOLE_API_KEY is not configured.",
                provider="groq",
                model=model or "",
            )

        self._model = (
            model
            or os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
        ).strip()

        raw_base = (
            base_url
            or os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1/")
        ).strip()
        self._base_url = raw_base.rstrip("/") + "/"

        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        })

    @property
    def provider_name(self) -> str:
        return "groq"

    @property
    def model(self) -> str:
        return self._model

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        timeout_seconds: int = 60,
    ) -> LlmResponse:
        return self._call(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            is_json=False,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
        )

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        timeout_seconds: int = 60,
    ) -> LlmResponse:
        return self._call(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            is_json=True,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
        )

    def _call(
        self,
        system_prompt: str,
        user_prompt: str,
        is_json: bool,
        temperature: float,
        max_tokens: int,
        timeout_seconds: int,
    ) -> LlmResponse:
        url = self._base_url + "chat/completions"

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if is_json:
            payload["response_format"] = {"type": "json_object"}

        t0 = time.perf_counter()
        try:
            resp = self._session.post(url, json=payload, timeout=timeout_seconds)
        except requests.exceptions.Timeout as exc:
            raise LlmError(
                f"Groq timed out after {timeout_seconds}s",
                provider=self.provider_name,
                model=self._model,
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise LlmError(
                f"Groq network request failed: {exc}",
                provider=self.provider_name,
                model=self._model,
            ) from exc

        latency_ms = int((time.perf_counter() - t0) * 1000)

        if resp.status_code == 429:
            raise LlmRateLimitError(
                f"Groq rate limit exceeded (429): {resp.text[:300]}",
                provider=self.provider_name,
                model=self._model,
                status_code=429,
                raw_error=resp.text,
            )

        if not resp.ok:
            raise LlmError(
                f"Groq returned {resp.status_code}: {resp.text[:500]}",
                provider=self.provider_name,
                model=self._model,
                status_code=resp.status_code,
                raw_error=resp.text,
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise LlmError(
                "Groq returned invalid non-JSON HTTP response body",
                provider=self.provider_name,
                model=self._model,
            ) from exc

        choices = data.get("choices") or []
        if not choices:
            raise LlmError(
                "Groq returned completion with no choices",
                provider=self.provider_name,
                model=self._model,
            )

        text = (choices[0].get("message") or {}).get("content", "")

        usage = data.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")

        parsed_json = None
        if is_json:
            parsed_json = self.extract_json_from_text(text)

        return LlmResponse(
            content=text,
            parsed_json=parsed_json,
            raw_response=data,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            model=self._model,
            provider=self.provider_name,
        )
