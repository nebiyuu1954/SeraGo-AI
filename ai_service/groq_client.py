import asyncio
import json
import logging
import os
from typing import Any

import requests

logger = logging.getLogger("ai_service.groq")


class GroqError(Exception):
    """Raised when the Groq API returns a non-2xx or invalid JSON."""


class GroqClient:
    """Tiny synchronous Groq client using the OpenAI-compat endpoint.

    Configured via env:
      GROQ_API_KEY         — Groq API key (also accepts GROQ_CONSOLE_API_KEY,
                             the name the .NET backend uses).
      GROQ_MODEL           — model to use (default: openai/gpt-oss-120b).
      GROQ_BASE_URL        — optional override for the Groq base URL.
    """

    def __init__(self) -> None:
        self._api_key = (
            os.environ.get("GROQ_API_KEY", "") 
            or os.environ.get("GROQ_CONSOLE_API_KEY", "")
        ).strip()
        if not self._api_key:
            raise GroqError("GROQ_API_KEY / GROQ_CONSOLE_API_KEY is not configured.")

        self._model = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b").strip() or "openai/gpt-oss-120b"
        base_url = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1/").strip()
        self._base_url = base_url.rstrip("/") + "/"
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        })

    @property
    def model(self) -> str:
        """The Groq model currently configured for this client."""
        return self._model

    # ------------------------------------------------------------------ public

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        """Post a chat completion and return the parsed JSON object.

        The model is told to return ONLY a JSON object. On any failure
        (network, timeout, non-2xx, bad JSON) a GroqError is raised so the
        caller can treat the job as 'unclassified' rather than using garbage.
        """
        return self._chat_json_raw(system_prompt, user_prompt, timeout_seconds)[0]

    def chat_json_with_raw(
        self,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: int = 60,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Like `chat_json`, but also returns the full raw Groq response dict.

        Returns (parsed_content, raw_response). Use this when you want to store
        the exact request/response for audit (e.g. the classify logging path).
        """
        return self._chat_json_raw(system_prompt, user_prompt, timeout_seconds)

    def _chat_json_raw(
        self,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: int = 60,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Shared implementation: build the request, call Groq, return parsed + raw."""
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "max_tokens": 2048,
        }

        try:
            resp = self._session.post(
                self._base_url + "chat/completions",
                json=payload,
                timeout=timeout_seconds,
            )
        except requests.exceptions.Timeout:
            logger.warning("Groq call timed out after %s seconds", timeout_seconds)
            raise GroqError("Groq timed out")
        except requests.exceptions.RequestException as exc:
            logger.warning("Groq call failed: %s", exc)
            raise GroqError(f"Groq request failed: {exc}") from exc

        if not resp.ok:
            body = resp.text[:500]
            logger.warning(
                "Groq returned %s for chat/completions: %s",
                resp.status_code,
                body,
            )
            raise GroqError(f"Groq returned {resp.status_code}: {body}")

        try:
            data = resp.json()
        except ValueError as exc:
            logger.warning("Groq returned non-JSON: %s", resp.text[:500])
            raise GroqError("Groq returned invalid JSON") from exc

        choices = data.get("choices") or []
        if not choices:
            raise GroqError("Groq returned a completion with no choices")

        message = (choices[0].get("message") or {}).get("content")
        if not message:
            raise GroqError("Groq returned a completion with no content")

        try:
            parsed = json.loads(message)
        except ValueError as exc:
            logger.warning("Groq returned non-JSON content: %s", message[:500])
            raise GroqError("Groq returned invalid JSON in completion content") from exc

        return parsed, data
