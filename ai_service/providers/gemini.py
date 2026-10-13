from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

from .base import BaseLlmProvider, LlmError, LlmRateLimitError, LlmResponse

logger = logging.getLogger("ai_service.providers.gemini")


class GeminiProvider(BaseLlmProvider):
    """Google Gemini adapter using the Google AI Generative Language REST API.

    Features:
      - Native JSON schema mode via responseMimeType="application/json"
      - 15 RPM / 1M TPM free tier support
      - Token usage extraction from usageMetadata
    """

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self._api_key = (
            api_key
            or os.environ.get("GEMINI_API_KEY", "")
            or os.environ.get("GOOGLE_API_KEY", "")
        ).strip()
        if not self._api_key:
            raise LlmError(
                "GEMINI_API_KEY / GOOGLE_API_KEY is not configured.",
                provider="gemini",
                model=model or "",
            )

        self._model = (
            model or os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
        ).strip()
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    @property
    def provider_name(self) -> str:
        return "gemini"

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
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{self._model}:generateContent"
            f"?key={self._api_key}"
        )

        gen_config: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        }
        if is_json:
            gen_config["responseMimeType"] = "application/json"

        payload: dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": user_prompt}],
                }
            ],
            "generationConfig": gen_config,
        }

        if system_prompt:
            payload["systemInstruction"] = {
                "parts": [{"text": system_prompt}],
            }

        t0 = time.perf_counter()
        try:
            resp = self._session.post(url, json=payload, timeout=timeout_seconds)
        except requests.exceptions.Timeout as exc:
            raise LlmError(
                f"Gemini timed out after {timeout_seconds}s",
                provider=self.provider_name,
                model=self._model,
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise LlmError(
                f"Gemini network request failed: {exc}",
                provider=self.provider_name,
                model=self._model,
            ) from exc

        latency_ms = int((time.perf_counter() - t0) * 1000)

        if resp.status_code == 429:
            raise LlmRateLimitError(
                f"Gemini rate limit exceeded (429): {resp.text[:300]}",
                provider=self.provider_name,
                model=self._model,
                status_code=429,
                raw_error=resp.text,
            )

        if not resp.ok:
            raise LlmError(
                f"Gemini API error ({resp.status_code}): {resp.text[:500]}",
                provider=self.provider_name,
                model=self._model,
                status_code=resp.status_code,
                raw_error=resp.text,
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise LlmError(
                "Gemini returned invalid non-JSON HTTP response body",
                provider=self.provider_name,
                model=self._model,
            ) from exc

        candidates = data.get("candidates") or []
        if not candidates:
            raise LlmError(
                f"Gemini returned no candidates (feedback: {data.get('promptFeedback')})",
                provider=self.provider_name,
                model=self._model,
            )

        candidate = candidates[0]
        content_obj = candidate.get("content") or {}
        parts = content_obj.get("parts") or []
        text = "".join(p.get("text", "") for p in parts)

        # Parse token usage
        usage = data.get("usageMetadata") or {}
        prompt_tokens = usage.get("promptTokenCount")
        completion_tokens = usage.get("candidatesTokenCount")
        total_tokens = usage.get("totalTokenCount")

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
