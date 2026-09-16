from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ------------------------------------------------------------------ request

@dataclass
class ClassifyJobRequestItem:
    job_id: str
    title: str
    source_sectors: list[str] | None
    description: str
    # The sector the job currently has, as sent by .NET, so the classify log
    # can record the 'before' state (original_sector_slug/name).
    extra_original: dict[str, Any] | None = None

    @classmethod
    def from_payload(cls, item: dict[str, Any]) -> ClassifyJobRequestItem:
        source = item.get("sourceSectors")
        if source is None:
            source = []
        if not isinstance(source, list):
            source = [str(source)]
        return cls(
            job_id=str(item.get("jobId") or item.get("job_id") or ""),
            title=str(item.get("title") or ""),
            source_sectors=[str(s) for s in source] if source else [],
            description=str(item.get("description") or ""),
            extra_original=item.get("extraOriginal"),
        )


@dataclass
class ClassifyRequest:
    jobs: list[ClassifyJobRequestItem]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ClassifyRequest:
        raw = payload.get("jobs")
        if not isinstance(raw, list) or not raw:
            raise ValueError("Expected a non-empty 'jobs' array")
        return cls(jobs=[ClassifyJobRequestItem.from_payload(item) for item in raw])


# ------------------------------------------------------------------ response

@dataclass
class ClassifyJobResult:
    job_id: str
    sector_id: str | None
    sector_name: str | None
    sector_slug: str | None
    sub_sector_name: str | None
    alias: str | None
    confidence: float | None
    reasoning: str | None
    uncategorized: bool
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "jobId": self.job_id,
            "sectorId": self.sector_id,
            "sectorName": self.sector_name,
            "sectorSlug": self.sector_slug,
            "subSectorName": self.sub_sector_name,
            "alias": self.alias,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "uncategorized": self.uncategorized,
            "error": self.error,
        }


# ------------------------------------------------------------------ AI response parsing

class AiClassificationParseError(Exception):
    """The LLM returned something we couldn't interpret as a classification."""


def parse_ai_classification(raw: Any, uncategorized_default: bool = False) -> dict[str, Any]:
    """Turn the LLM JSON response into a normalized classification dict.

    Expected keys (camelCase, matching the LLM prompt schema):
      sectorSlug, sectorName, confidence, reasoning, uncategorized

    Returns a dict with at least: sectorSlug, sectorName, confidence, reasoning,
    uncategorized. Missing fields are filled with safe defaults.
    """
    if not isinstance(raw, dict):
        raise AiClassificationParseError(f"Expected a JSON object, got {type(raw)!r}")

    uncategorized = bool(raw.get("uncategorized", uncategorized_default))

    sector_slug: str | None = raw.get("sectorSlug")
    if sector_slug is not None:
        sector_slug = str(sector_slug).strip() or None

    sector_name: str | None = raw.get("sectorName")
    if sector_name is not None:
        sector_name = str(sector_name).strip() or None

    confidence = raw.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    if confidence is not None and not (0.0 <= confidence <= 1.0):
        confidence = None

    reasoning = raw.get("reasoning")
    if reasoning is not None:
        reasoning = str(reasoning).strip() or None

    return {
        "sectorSlug": sector_slug,
        "sectorName": sector_name,
        "confidence": confidence,
        "reasoning": reasoning,
        "uncategorized": uncategorized,
    }
