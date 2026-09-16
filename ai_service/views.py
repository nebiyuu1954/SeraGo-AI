"""
AI service views — LLM-powered features:
  - Job-to-sector classification (classify)
"""

import json as _json
import logging

from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt

from ai_service.schema import ClassifyRequest, ClassifyJobResult, parse_ai_classification
from ai_service.services.classifier import ClassifierServiceError, classify_jobs
from ai_service.groq_client import GroqClient, GroqError

logger = logging.getLogger("ai_service.views")


def _ensure_groq() -> GroqClient:
    """Lazily construct the Groq client. Raises GroqError when not configured."""
    try:
        return GroqClient()
    except GroqError as exc:
        logger.error("Groq client unavailable: %s", exc)
        raise ClassifierServiceError("AI provider is not configured") from exc


@csrf_exempt
@require_POST
def classify(request):
    """POST /api/ai/classify — classify one-to-many jobs via the LLM.

    Body: { "jobs": [ { jobId, title, sourceSectors, description }, ... ] }

    Auth: X-Api-Key header (shared secret with the .NET backend), enforced by
    ApiKeyMiddleware.

    Returns: { results: [ { jobId, sectorId, sectorName, sectorSlug,
             subSectorName, alias, confidence, reasoning, uncategorized, error }, ... ] }

    Per-job best-effort: a failure for one job is surfaced as `error` on that
    result and does not abort the rest.
    """
    try:
        raw = request.body
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = _json.loads(raw)
    except (_json.JSONDecodeError, UnicodeDecodeError) as exc:
        # Always log the transport details on a rejected body — this is how we
        # diagnose chunked/missing-body requests from HTTP clients (e.g. the
        # .NET PostAsJsonAsync chunked-encoding issue with wsgiref).
        logger.warning(
            "classify rejected request body: %s | method=%s path=%s proto=%s "
            "content_length=%r transfer_encoding=%r expect=%r conn=%r "
            "remote=%s body=%d bytes head=%r",
            exc,
            request.method,
            request.path,
            request.META.get("SERVER_PROTOCOL"),
            request.META.get("CONTENT_LENGTH"),
            request.META.get("HTTP_TRANSFER_ENCODING"),
            request.META.get("HTTP_EXPECT"),
            request.META.get("HTTP_CONNECTION"),
            request.META.get("REMOTE_ADDR"),
            len(request.body),
            request.body[:300],
        )
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    try:
        classify_request = ClassifyRequest.from_payload(payload)
    except ValueError as exc:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "classify payload parse failed: %s | raw body (%d bytes): %r",
                exc,
                len(request.body),
                request.body[:2000],
            )
        return JsonResponse({"error": str(exc)}, status=400)
    except ClassifierServiceError as exc:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "classify service unavailable: %s | raw body (%d bytes): %r",
                exc,
                len(request.body),
                request.body[:2000],
            )
        return JsonResponse({"error": str(exc)}, status=503)

    try:
        groq = _ensure_groq()
    except ClassifierServiceError as exc:
        return JsonResponse({"error": str(exc)}, status=503)

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "classify ok | method=%s path=%s protocol=%s body_bytes=%d body_head=%r",
            request.method,
            request.path,
            getattr(request.META, "SERVER_PROTOCOL", ""),
            len(request.body),
            request.body[:2000],
        )

    results = classify_jobs(classify_request, groq)
    return JsonResponse({"results": [r.to_dict() for r in results]})
