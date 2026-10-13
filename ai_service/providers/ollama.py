from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

from .base import BaseLlmProvider, LlmError, LlmResponse

logger = logging.getLogger("ai_service.providers.ollama")


class OllamaProvider(BaseLlmProvider):
    """Local / self-hosted Ollama adapter for zero-cost, private text and JSON generation."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        raw_base = (
            base_url
            or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        ).strip().rstrip("/")
        # If user passed /v1 or /v1/, trim to root
        if raw_base.endswith("/v1"):
            raw_base = raw_base[:-3]
        self._base_url = raw_base

        self._model = (
            model
            or os.environ.get("OLLAMA_MODEL")
            or os.environ.get("COVER_LETTER_AI_MODEL")
            or "llama3.1:8b"
        ).strip()
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    @property
    def provider_name(self) -> str:
        return "ollama"

    @property
    def model(self) -> str:
        return self._model

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        timeout_seconds: int = 120,
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
        timeout_seconds: int = 120,
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
        url = f"{self._base_url}/api/chat"

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }

        if is_json:
            payload["format"] = "json"

        t0 = time.perf_counter()
        try:
            resp = self._session.post(url, json=payload, timeout=timeout_seconds)
        except requests.exceptions.ConnectionError as exc:
            raise LlmError(
                f"Could not connect to Ollama at {self._base_url}. Is Ollama running?",
                provider=self.provider_name,
                model=self._model,
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise LlmError(
                f"Ollama call timed out after {timeout_seconds}s",
                provider=self.provider_name,
                model=self._model,
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise LlmError(
                f"Ollama request failed: {exc}",
                provider=self.provider_name,
                model=self._model,
            ) from exc

        latency_ms = int((time.perf_counter() - t0) * 1000)

        if not resp.ok:
            raise LlmError(
                f"Ollama returned {resp.status_code}: {resp.text[:400]}",
                provider=self.provider_name,
                model=self._model,
                status_code=resp.status_code,
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise LlmError(
                "Ollama returned invalid non-JSON HTTP response body",
                provider=self.provider_name,
                model=self._model,
            ) from exc

        text = (data.get("message") or {}).get("content", "")
        prompt_tokens = data.get("prompt_eval_count")
        completion_tokens = data.get("eval_count")
        total_tokens = (prompt_tokens or 0) + (completion_tokens or 0) if prompt_tokens or completion_tokens else None

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
