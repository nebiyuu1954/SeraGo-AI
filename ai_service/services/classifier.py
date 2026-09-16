from __future__ import annotations

import logging
import time
from typing import Any

from django.db import connections, transaction
from django.db.utils import OperationalError

from ai_service.groq_client import GroqClient, GroqError
from ai_service.models import AiClassificationLog, AiClassificationRaw
from ai_service.schema import (
    AiClassificationParseError,
    ClassifyJobResult,
    ClassifyRequest,
    ClassifyJobRequestItem,
    parse_ai_classification,
)
from ai_service.token_counter import count_request_tokens, parse_provider_usage

logger = logging.getLogger("ai_service.classifier")


class ClassifierServiceError(Exception):
    """Raised when the classifier can't even start (e.g. no LLM configured)."""


def _sector_list_for_prompt() -> list[tuple[str, str, str]]:
    """Return the real active sectors as (id, slug, name) tuples from the shared DB.

    This is the canonical vocabulary the LLM must choose from. Loaded once per
    classify call so the prompt always reflects the live sector list.

    The table is the .NET EF "Sectors" table (quoted PascalCase identifiers),
    so the SQL must quote them — unquoted `sectors`/`is_active` fails on
    PostgreSQL. It lives in the .NET backend's DB, so we read it through the
    read-only "sectors" database alias (falling back to "default" when the
    alias isn't configured, e.g. single-DB setups). If the table can't be
    read, a ClassifierServiceError is raised (surfaced as a 503 by the view)
    instead of a raw 500.
    """
    alias = "sectors" if "sectors" in connections else "default"
    try:
        with connections[alias].cursor() as cursor:
            cursor.execute(
                """
                SELECT "Id", "Slug", "Name"
                FROM "Sectors"
                WHERE "IsActive" = TRUE
                ORDER BY "Name"
                """
            )
            rows = cursor.fetchall()
    except OperationalError as exc:
        logger.error("Could not load the sector list from the shared DB: %s", exc)
        raise ClassifierServiceError("Sector list unavailable from the shared database") from exc

    return [(str(row[0]), str(row[1]), str(row[2])) for row in rows]


def _build_system_prompt(sectors: list[tuple[str, str, str]]) -> str:
    """Build the system prompt listing the real canonical sectors."""
    lines = [
        "LOW CATEGORICAL TABLE — choose ONE slug from below:",
    ]
    for sector_id, slug, name in sectors:
        lines.append(f"  {slug}   {name}   (id: {sector_id})")

    lines.append(
        "  (if none of the above fit the job, set \"uncategorized\": true "
        "and leave sectorSlug/sectorName/sectorId null — do NOT invent a "
        "sector slug or id)"
    )

    return "\n".join(lines)


def _job_text(item: ClassifyJobRequestItem) -> str:
    """Build the user-prompt text for one job."""
    parts: list[str] = []
    parts.append(f"Title: {item.title}")
    if item.source_sectors:
        parts.append(f"Source sectors (raw): {', '.join(item.source_sectors)}")
    if item.description:
        parts.append(f"Description:\n{item.description}")

    return "\n".join(parts)


def _build_user_prompt(item: ClassifyJobRequestItem) -> str:
    return (
        "Classify this job into exactly one sector from the list above.\n\n"
        + _job_text(item)
        + "\n\nReturn a JSON object with: sectorId, sectorSlug, sectorName, "
        "confidence, reasoning, uncategorized.\n"
        "sectorId must be the canonical sector id from the list above, or null."
    )


def _resolve_sector_by_slug(
    slug: str | None,
    sectors: list[tuple[str, str, str]],
) -> dict[str, Any]:
    """Resolve a canonical sector by slug against the real sector list.

    Returns a dict with sectorId, sectorName, sectorSlug, or nulls when the
    slug is missing/unknown.
    """
    if not slug:
        return {"sectorId": None, "sectorName": None, "sectorSlug": None}

    slug = slug.strip()
    for sector_id, sector_slug, sector_name in sectors:
        if sector_slug == slug:
            return {
                "sectorId": sector_id,
                "sectorName": sector_name,
                "sectorSlug": sector_slug,
            }

    logger.warning("Classifier returned unknown sectorSlug=%s — treating as uncategorized", slug)
    return {"sectorId": None, "sectorName": None, "sectorSlug": None}


def _build_request_payload(system: str, user: str, model: str) -> dict[str, Any]:
    """Reconstruct the exact request payload sent to Groq, for audit logging."""
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "max_tokens": 2048,
    }


def _log_classification(
    *,
    job_id: str,
    original_sector_slug: str,
    original_sector_name: str,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any] | None,
    model: str,
    parsed: dict[str, Any] | None,
    error: str | None,
    latency_ms: int,
    jobs_sent: int,
) -> AiClassificationLog:
    """Write one raw + one log row for a classify attempt, atomically.

    Token accounting: tokens_sent/tokens_received/total_tokens come from the
    provider's exact `usage` field; est_tokens_sent is a local tiktoken
    estimate of the request payload; jobs_sent is the whole batch size.

    Returns the created AiClassificationLog row.
    """
    if response_payload is None:
        response_payload = {}

    usage = parse_provider_usage(response_payload)

    status: str
    if error:
        status = "error"
    elif parsed and parsed.get("uncategorized"):
        status = "uncategorized"
    else:
        status = "ok"

    raw = AiClassificationRaw.objects.create(
        request_payload=request_payload,
        response_payload=response_payload,
        model=model,
        status=status,
        latency_ms=latency_ms,
        tokens_sent=usage["prompt_tokens"],
        tokens_received=usage["completion_tokens"],
        total_tokens=usage["total_tokens"],
        est_tokens_sent=count_request_tokens(request_payload, model),
        jobs_sent=jobs_sent,
    )

    sector_slug = ""
    sector_name = ""
    confidence: float | None = None
    reasoning = ""
    categorized = False

    if parsed:
        raw_slug = parsed.get("sectorSlug")
        if raw_slug:
            sector_slug = str(raw_slug).strip() or ""
        raw_name = parsed.get("sectorName")
        if raw_name:
            sector_name = str(raw_name).strip() or ""
        conf = parsed.get("confidence")
        try:
            confidence = float(conf) if conf is not None else None
        except (TypeError, ValueError):
            confidence = None
        reasoning = parsed.get("reasoning") or ""
        if reasoning:
            reasoning = str(reasoning).strip()
        categorized = bool(parsed.get("uncategorized", False)) is False and bool(sector_slug)

    log = AiClassificationLog.objects.create(
        job_id=job_id,
        sector_slug=sector_slug,
        sector_name=sector_name,
        confidence=confidence,
        reasoning=reasoning,
        categorized=categorized,
        original_sector_slug=original_sector_slug,
        original_sector_name=original_sector_name,
        log_id=raw,
    )

    return log


def classify_jobs(request: ClassifyRequest, groq: GroqClient) -> list[ClassifyJobResult]:
    """Classify one-to-many jobs via the LLM.

    Each job is classified independently: a failure or uncategorization for one
    job does not affect the others. Per-job errors are surfaced as `error` on
    the result, not as an exception.

    Every classify attempt also writes an `AiClassificationRaw` + `AiClassificationLog`
    row so the exact Groq request/response and the parsed result are persisted for
    audit and debugging.
    """
    sectors = _sector_list_for_prompt()
    system = _build_system_prompt(sectors)
    model = groq.model

    results: list[ClassifyJobResult] = []

    for item in request.jobs:
        if not item.job_id:
            results.append(
                ClassifyJobResult(
                    job_id=item.job_id or "",
                    sector_id=None,
                    sector_name=None,
                    sector_slug=None,
                    sub_sector_name=None,
                    alias=None,
                    confidence=None,
                    reasoning=None,
                    uncategorized=True,
                    error="jobId is required",
                )
            )
            continue

        # The original source sector text to record on the SubSector mapping.
        # Prefer the first source sector if present, else empty.
        alias = item.source_sectors[0] if item.source_sectors else ""

        # Record the sector the job had before this call, as sent by .NET.
        original_slug = ""
        original_name = ""
        src = item.extra_original or {}
        if isinstance(src, dict):
            original_slug = str(src.get("sectorSlug") or "").strip()
            original_name = str(src.get("sectorName") or "").strip()

        t0 = time.perf_counter()
        parsed: dict[str, Any] | None = None
        response_payload: dict[str, Any] | None = None
        error: str | None = None

        try:
            user = _build_user_prompt(item)
            request_payload = _build_request_payload(system, user, model)
            parsed, response_payload = groq.chat_json_with_raw(system, user, timeout_seconds=60)
            parsed = parse_ai_classification(parsed)

        except GroqError as exc:
            logger.warning("Groq failed for job %s: %s", item.job_id, exc)
            error = str(exc)

        except AiClassificationParseError as exc:
            logger.warning("Failed to parse AI classification for job %s: %s", item.job_id, exc)
            error = f"parse_error: {exc}"

        except Exception as exc:  # noqa: BLE001 — per-job safety net
            logger.exception("Unexpected error classifying job %s", item.job_id)
            error = f"error: {exc}"

        finally:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            _log_classification(
                job_id=item.job_id,
                original_sector_slug=original_slug,
                original_sector_name=original_name,
                request_payload=request_payload,
                response_payload=response_payload,
                model=model,
                parsed=parsed,
                error=error,
                latency_ms=latency_ms,
                jobs_sent=len(request.jobs),
            )

        # Build the result returned to .NET.
        if error:
            results.append(
                ClassifyJobResult(
                    job_id=item.job_id,
                    sector_id=None,
                    sector_name=None,
                    sector_slug=None,
                    sub_sector_name=None,
                    alias=alias,
                    confidence=None,
                    reasoning=None,
                    uncategorized=True,
                    error=error,
                )
            )
            continue

        if parsed["uncategorized"] or parsed["sectorSlug"] is None:
            results.append(
                ClassifyJobResult(
                    job_id=item.job_id,
                    sector_id=None,
                    sector_name=None,
                    sector_slug=None,
                    sub_sector_name=None,
                    alias=alias,
                    confidence=parsed["confidence"],
                    reasoning=parsed["reasoning"],
                    uncategorized=True,
                    error=None,
                )
            )
            continue

        resolved = _resolve_sector_by_slug(parsed["sectorSlug"], sectors)
        results.append(
            ClassifyJobResult(
                job_id=item.job_id,
                sector_id=resolved["sectorId"],
                sector_name=parsed["sectorName"] or resolved["sectorName"],
                sector_slug=resolved["sectorSlug"],
                sub_sector_name=parsed["sectorName"] or resolved["sectorName"],
                alias=alias,
                confidence=parsed["confidence"],
                reasoning=parsed["reasoning"],
                uncategorized=False,
                error=None,
            )
        )

    return results
