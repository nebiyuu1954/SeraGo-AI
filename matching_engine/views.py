"""
Matching engine views — the two core features:

  Feature 1 — Job published → For You:
      POST /api/matching/webhook/job-published
      Scores the job against every eligible talent (must have For You
      settings AND a usable profile) and stores 0-100 TalentJobScore rows
      so the talent's For You feed can show them.

  Feature 2 — Application created → score:
      POST /api/matching/webhook/application
      Scores the application (talent snapshot vs job) immediately and
      stores the 0-100 score on ApplicationScore so the recruiter's
      applications page can display it.

Talent-initiated refresh (the "Run AI matching" button on the For You page):
      POST /api/matching/refresh-for-you
      Scores the caller's For You feed jobs against their profile on demand.

Read endpoints used by the .NET backend to annotate its pages:
      POST /api/matching/scores/batch                — For You feed scores
      POST /api/matching/application-scores/batch    — applications page scores
      GET  /api/matching/health                      — health check

All webhook/batch endpoints require the X-Api-Key header.
"""

import json
import logging

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from drf_spectacular.utils import extend_schema, OpenApiExample, OpenApiResponse

from .models import (
    ApplicationScore,
    JobVector,
    SCORING_VERSION,
    TalentJobScore,
    TalentProfileVector,
)

logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────

def _parse_json_body(request) -> dict:
    try:
        return json.loads(request.body)
    except (json.JSONDecodeError, TypeError):
        return {}


def _talent_profile_from_dict(data: dict):
    from .services.scoring import TalentProfileData
    return TalentProfileData(
        user_id=data.get("userId", ""),
        headline=data.get("headline", ""),
        about=data.get("about", ""),
        skills=data.get("skills", []),
        experience_level=data.get("experienceLevel"),
        years_of_experience=data.get("yearsOfExperience"),
        desired_roles=data.get("desiredRoles", []),
        desired_job_types=data.get("desiredJobTypes", []),
        preferred_sector_ids=data.get("preferredSectorIds", []),
        work_mode=data.get("workMode"),
        preferred_locations=data.get("preferredLocations", []),
        work_experience=json.dumps(data.get("workExperience", [])) if isinstance(data.get("workExperience"), list) else data.get("workExperience", "[]"),
        education_history=json.dumps(data.get("educationHistory", [])) if isinstance(data.get("educationHistory"), list) else data.get("educationHistory", "[]"),
        current_industry=data.get("currentIndustry", ""),
        current_profession=data.get("currentProfession", ""),
    )


def _job_data_from_dict(data: dict):
    from .services.scoring import JobData
    skills = data.get("skills", "")
    if isinstance(skills, list):
        skills = json.dumps(skills)
    return JobData(
        job_id=data.get("jobId", ""),
        title=data.get("title", ""),
        description=data.get("description", ""),
        company=data.get("company", ""),
        location=data.get("location", ""),
        sector_id=data.get("sectorId"),
        sector_name=data.get("sectorName", ""),
        experience_level=data.get("experienceLevel"),
        job_type=data.get("jobType"),
        work_mode=data.get("workMode"),
        skills=skills,
        experience_min_years=data.get("experienceMinYears"),
        experience_max_years=data.get("experienceMaxYears"),
    )


def _talent_eligibility(talent):
    """Return (eligible: bool, reason: str | None) for For You scoring.

    A talent can only be scored when they have BOTH:
      - "For You" page settings: at least one preferred sector id, and
      - A usable profile: headline, about, skills, roles, etc.
    Without either, the match cannot be calculated.
    """
    if not talent.user_id:
        return False, "missing user id"
    if not talent.preferred_sector_ids:
        return False, "no for-you settings (no preferred sectors)"
    if not talent.has_data:
        return False, "no profile data (headline/about/skills/roles)"
    return True, None


def _talent_embedding_and_hash(talent):
    """Return (embedding, content_hash) for a talent's profile.

    Reuses the stored TalentProfileVector embedding when the profile text is
    unchanged since it was embedded; otherwise encodes once and returns the
    fresh embedding (the caller persists it).
    """
    from .services.scoring import build_talent_text, encode_text, text_hash
    text = build_talent_text(talent)
    current_hash = text_hash(text)
    stored = (
        TalentProfileVector.objects
        .filter(talent_user_id=talent.user_id)
        .only("embedding", "profile_hash")
        .first()
    )
    if stored is not None and stored.profile_hash == current_hash:
        return stored.embedding, current_hash
    return encode_text(text), current_hash


def _job_embedding_and_hash(job):
    """Return (embedding, content_hash) for a job.

    The embedding depends only on the job content, so it is computed ONCE per
    job id and then shared by every talent scored against it — user 1 in IT
    and user 2 in IT never re-encode the same job. The stored JobVector is
    reused whenever the job data is unchanged; a changed job gets re-encoded
    and its vector refreshed.
    """
    from .services.scoring import build_job_text, encode_text, text_hash
    text = build_job_text(job)
    current_hash = text_hash(text)
    stored = (
        JobVector.objects
        .filter(job_id=job.job_id)
        .only("embedding", "job_data_hash")
        .first()
    )
    if stored is not None and stored.job_data_hash == current_hash:
        return stored.embedding, current_hash
    return encode_text(text), current_hash


def _score_is_current(pair, talent_hash, job_hash):
    """True when a stored TalentJobScore row is still valid (same profile text,
    same job data, same algorithm version)."""
    return (
        pair is not None
        and pair.score is not None
        and pair.scoring_version == SCORING_VERSION
        and pair.talent_profile_hash == talent_hash
        and pair.job_data_hash == job_hash
    )


def _compute_and_store_score(talent, job, talent_vector_obj=None, job_vector_obj=None):
    """Compute a talent-job score and persist TalentJobScore + vectors.

    Caching rules:
      - Job embeddings are stored once per job id and reused for every talent
        (content-hash checked).
      - Talent embeddings are reused when the profile text is unchanged.
      - If an identical, current pair is already stored, the stored score is
        returned with zero encoding and zero recomputation.

    Returns a namespace with score 0-100, matched/missing, explanation and a
    `cached` flag (True = stored result reused).
    """
    from types import SimpleNamespace as _NS
    from .services.scoring import compute_score

    # Talent embedding + content hash (reuse stored vector when unchanged).
    if talent_vector_obj:
        talent_embedding = talent_vector_obj.embedding
        current_talent_hash = talent_vector_obj.profile_hash
    else:
        talent_embedding, current_talent_hash = _talent_embedding_and_hash(talent)

    # Job embedding + content hash (shared across all talents).
    if job_vector_obj:
        job_embedding = job_vector_obj.embedding
        current_job_hash = job_vector_obj.job_data_hash
    else:
        job_embedding, current_job_hash = _job_embedding_and_hash(job)

    # Nothing changed since this pair was last scored → reuse the stored result.
    existing = TalentJobScore.objects.filter(
        talent_user_id=talent.user_id,
        job_id=job.job_id,
    ).first()
    if _score_is_current(existing, current_talent_hash, current_job_hash):
        return _NS(
            score=existing.score,
            matched=existing.matched,
            missing=existing.missing,
            explanation=existing.explanation,
            cached=True,
        )

    # Compute score
    result = compute_score(talent, job, talent_embedding, job_embedding)
    result = _NS(
        score=result.score,
        matched=result.matched,
        missing=result.missing,
        explanation=result.explanation,
        cached=False,
    )

    # Store score with staleness hashes
    TalentJobScore.objects.update_or_create(
        talent_user_id=talent.user_id,
        job_id=job.job_id,
        defaults={
            "score": result.score,
            "matched": result.matched,
            "missing": result.missing,
            "explanation": result.explanation,
            "talent_profile_hash": current_talent_hash,
            "job_data_hash": current_job_hash,
            "scoring_version": SCORING_VERSION,
        },
    )

    # Store/update vectors (job vector is the shared, per-job cache)
    TalentProfileVector.objects.update_or_create(
        talent_user_id=talent.user_id,
        defaults={
            "embedding": talent_embedding,
            "keywords": talent.skills + talent.desired_roles,
            "profile_hash": current_talent_hash,
        },
    )
    JobVector.objects.update_or_create(
        job_id=job.job_id,
        defaults={
            "embedding": job_embedding,
            "keywords": result.matched + result.missing,
            "job_data_hash": current_job_hash,
        },
    )

    return result


# ══════════════════════════════════════════════════════════════════════
#  FEATURE 1 — WEBHOOK: Job Published → For You scores
# ══════════════════════════════════════════════════════════════════════

@extend_schema(
    tags=["Webhooks"],
    summary="Score platform job against eligible talents",
    description=(
        "Called by .NET when admin approves a job. Only talents that have "
        "both For You settings (preferred sectors) and a usable profile are "
        "scored (0-100); everyone else is returned as skipped with a reason. "
        "Requires X-Api-Key."
    ),
    examples=[
        OpenApiExample(
            "Job published",
            value={
                "job": {
                    "jobId": "550e8400-e29b-41d4-a716-446655440000",
                    "title": "Senior Python Developer",
                    "description": "We need a Python developer",
                    "sectorId": "a0000000-0000-0000-0000-000000000001",
                    "sectorName": "Technology & IT",
                },
                "eligibleTalents": [
                    {
                        "userId": "uuid",
                        "headline": "Python Developer",
                        "skills": ["Python", "Django"],
                        "currentIndustry": "Technology",
                        "preferredSectorIds": ["a0000000-0000-0000-0000-000000000001"],
                    }
                ],
            },
        )
    ],
    responses={200: OpenApiResponse(description="Scores computed")},
)
@csrf_exempt
@require_POST
def webhook_job_published(request):
    """Score a published job against eligible talents (0-100 per talent)."""
    body = _parse_json_body(request)
    job_dict = body.get("job")
    talents_list = body.get("eligibleTalents", [])

    if not job_dict or not job_dict.get("jobId"):
        return JsonResponse({"error": "job.jobId is required"}, status=400)

    job = _job_data_from_dict(job_dict)
    if not job.has_data:
        return JsonResponse({"status": "skipped", "reason": "job has no data"}, status=200)

    # Separate eligible talents (For You settings + profile) from skipped ones.
    eligible = []
    skipped = []
    for raw_talent in talents_list:
        talent = _talent_profile_from_dict(raw_talent)
        ok, reason = _talent_eligibility(talent)
        if ok:
            eligible.append(talent)
        else:
            skipped.append({"userId": talent.user_id, "reason": reason})

    if not eligible:
        return JsonResponse({
            "status": "ok",
            "eligible": 0,
            "scored": 0,
            "skipped": len(skipped),
            "skippedReasons": skipped,
        }, status=200)

    from types import SimpleNamespace as _NS

    # Encode the job ONCE (or reuse its stored vector) — every talent scored
    # against this job shares the same embedding, across this call and every
    # future one (job-published, For You refresh, applications).
    job_embedding, job_hash = _job_embedding_and_hash(job)
    job_vector_obj = _NS(embedding=job_embedding, job_data_hash=job_hash)

    scored = 0
    cached = 0
    for talent in eligible:
        try:
            result = _compute_and_store_score(talent, job, job_vector_obj=job_vector_obj)
            if result.cached:
                cached += 1
            else:
                scored += 1
        except Exception as exc:
            logger.warning("Failed to score talent %s: %s", talent.user_id[:8], exc)

    logger.info(
        "webhook_job_published: job %s scored against %d/%d eligible talents "
        "(%d reused, %d skipped)",
        str(job.job_id)[:8], scored, len(eligible), cached, len(skipped),
    )
    return JsonResponse({
        "status": "ok",
        "eligible": len(eligible),
        "scored": scored,
        "cached": cached,
        "skipped": len(skipped),
        "skippedReasons": skipped,
    })


# ══════════════════════════════════════════════════════════════════════
#  TALENT-INITIATED — Refresh For You scores for one talent vs many jobs
# ══════════════════════════════════════════════════════════════════════

@extend_schema(
    tags=["Webhooks"],
    summary="Score one talent against a list of jobs now",
    description=(
        "Called by .NET when a talent hits the 'Run AI matching' button on "
        "the For You page. Scores their feed jobs against their profile right "
        "away (0-100 each), stores the scores, and returns them so the page "
        "can display match percentages immediately. Requires X-Api-Key."
    ),
    examples=[
        OpenApiExample(
            "Refresh For You",
            value={
                "talent": {
                    "userId": "uuid",
                    "headline": "Backend Developer",
                    "skills": ["Python", "Django"],
                    "preferredSectorIds": ["a0000000-0000-0000-0000-000000000001"],
                },
                "jobs": [
                    {
                        "jobId": "550e8400-e29b-41d4-a716-446655440000",
                        "title": "Senior Python Developer",
                        "skills": "Python, Django",
                    }
                ],
            },
        )
    ],
    responses={200: OpenApiResponse(description="Scores computed")},
)
@csrf_exempt
@require_POST
def refresh_for_you(request):
    """Score one talent against a list of jobs immediately (For You refresh)."""
    body = _parse_json_body(request)
    talent_dict = body.get("talent")
    raw_jobs = body.get("jobs", []) or []

    if not talent_dict or not talent_dict.get("userId"):
        return JsonResponse({"error": "talent.userId is required"}, status=400)

    talent = _talent_profile_from_dict(talent_dict)
    ok, reason = _talent_eligibility(talent)
    if not ok:
        return JsonResponse({
            "status": "skipped",
            "reason": reason,
            "scored": 0,
            "total": len(raw_jobs),
            "scores": {},
        }, status=200)

    from uuid import UUID
    from .services.scoring import build_job_text, text_hash, encode_texts, compute_score
    from django.db import transaction

    skipped_jobs = []

    # Parse jobs once; drop malformed / data-less ones up front. Job ids are
    # normalized to UUID objects so the cache lookups below key consistently.
    plan = []
    for raw in raw_jobs[:200]:
        if not isinstance(raw, dict) or not raw.get("jobId"):
            continue
        try:
            job_uuid = UUID(str(raw.get("jobId")))
        except (ValueError, TypeError):
            skipped_jobs.append({"jobId": str(raw.get("jobId"))[:8], "reason": "invalid job id"})
            continue
        job = _job_data_from_dict(raw)
        if not job.has_data:
            skipped_jobs.append({"jobId": str(job_uuid)[:8], "reason": "job has no data"})
            continue
        job_text = build_job_text(job)
        plan.append({
            "raw_id": str(job_uuid),
            "uuid": job_uuid,
            "job": job,
            "hash": text_hash(job_text),
            "embedding": None,
            "cached": False,
            "score": None, "matched": None, "missing": None, "explanation": None,
        })

    if not plan:
        return JsonResponse({
            "status": "ok", "scored": 0, "cached": 0,
            "total": len(raw_jobs), "skipped": skipped_jobs, "scores": {},
        }, status=200)

    # Preload the stored rows for this talent + whole feed (2 queries instead
    # of 2 per job) so unchanged work is skipped without any encoding.
    job_ids = [p["uuid"] for p in plan]
    stored_pairs = {
        row.job_id: row
        for row in TalentJobScore.objects.filter(
            talent_user_id=talent.user_id, job_id__in=job_ids)
    }
    stored_vectors = {
        row.job_id: row
        for row in JobVector.objects.filter(job_id__in=job_ids)
    }

    # Encode the talent ONCE for the whole run (or reuse its stored vector).
    talent_embedding, talent_hash = _talent_embedding_and_hash(talent)

    cached_count = 0
    needs_encode = []
    for p in plan:
        pair = stored_pairs.get(p["uuid"])
        if _score_is_current(pair, talent_hash, p["hash"]):
            # Unchanged pair — reuse the stored score, zero model calls.
            p["cached"] = True
            p["score"], p["matched"], p["missing"], p["explanation"] = (
                pair.score, pair.matched, pair.missing, pair.explanation)
            cached_count += 1
            continue
        vector = stored_vectors.get(p["uuid"])
        if vector is not None and vector.job_data_hash == p["hash"]:
            # Job embedding already computed once (shared across all users).
            p["embedding"] = vector.embedding
        else:
            needs_encode.append(p)

    # One batched model call for every job that genuinely needs embedding.
    if needs_encode:
        embeddings = encode_texts([build_job_text(p["job"]) for p in needs_encode])
        for p, emb in zip(needs_encode, embeddings):
            p["embedding"] = emb

    # Score + persist only the new/stale pairs, all in one transaction.
    scores = {}
    scored_count = 0
    with transaction.atomic():
        for p in plan:
            if p["cached"]:
                scores[p["raw_id"]] = {
                    "score": p["score"], "matched": p["matched"],
                    "missing": p["missing"], "explanation": p["explanation"],
                }
                continue
            try:
                result = compute_score(talent, p["job"], talent_embedding, p["embedding"])
                p["score"], p["matched"], p["missing"], p["explanation"] = (
                    result.score, result.matched, result.missing, result.explanation)

                TalentJobScore.objects.update_or_create(
                    talent_user_id=talent.user_id,
                    job_id=p["uuid"],
                    defaults={
                        "score": result.score,
                        "matched": result.matched,
                        "missing": result.missing,
                        "explanation": result.explanation,
                        "talent_profile_hash": talent_hash,
                        "job_data_hash": p["hash"],
                        "scoring_version": SCORING_VERSION,
                    },
                )
                JobVector.objects.update_or_create(
                    job_id=p["uuid"],
                    defaults={
                        "embedding": p["embedding"],
                        "keywords": result.matched + result.missing,
                        "job_data_hash": p["hash"],
                    },
                )
                scored_count += 1
            except Exception as exc:
                logger.warning(
                    "Failed to score job %s for talent %s: %s",
                    p["raw_id"][:8], talent.user_id[:8], exc,
                )
                skipped_jobs.append({"jobId": p["raw_id"][:8], "reason": "scoring failed"})
                continue
            scores[p["raw_id"]] = {
                "score": p["score"], "matched": p["matched"],
                "missing": p["missing"], "explanation": p["explanation"],
            }

        # Refresh the talent's stored profile vector once per run.
        TalentProfileVector.objects.update_or_create(
            talent_user_id=talent.user_id,
            defaults={
                "embedding": talent_embedding,
                "keywords": talent.skills + talent.desired_roles,
                "profile_hash": talent_hash,
            },
        )

    logger.info(
        "refresh_for_you: scored %d / reused %d / skipped %d for talent %s (feed %d)",
        scored_count, cached_count, len(skipped_jobs), talent.user_id[:8], len(plan),
    )
    return JsonResponse({
        "status": "ok",
        "scored": scored_count,
        "cached": cached_count,
        "total": len(raw_jobs),
        "skipped": skipped_jobs,
        "scores": scores,
    })


# ══════════════════════════════════════════════════════════════════════
#  FEATURE 2 — WEBHOOK: Application Created → immediate score
# ══════════════════════════════════════════════════════════════════════

@extend_schema(
    tags=["Webhooks"],
    summary="Score application immediately",
    description=(
        "Called by .NET when a talent applies. Scores the application "
        "(talent snapshot vs job) right away and stores the 0-100 score on "
        "ApplicationScore so the recruiter's applications page can show it. "
        "Requires X-Api-Key."
    ),
    responses={200: OpenApiResponse(description="Application scored")},
)
@csrf_exempt
@require_POST
def webhook_application(request):
    """Score a new application immediately (0-100) and store it."""
    body = _parse_json_body(request)
    application_id = body.get("applicationId")
    talent_dict = body.get("talentProfile")
    job_dict = body.get("job")

    if not application_id:
        return JsonResponse({"error": "applicationId is required"}, status=400)
    if not job_dict or not job_dict.get("jobId"):
        return JsonResponse({"error": "job.jobId is required"}, status=400)
    if not talent_dict:
        return JsonResponse({"error": "talentProfile is required"}, status=400)

    app_score, created = ApplicationScore.objects.update_or_create(
        application_id=application_id,
        defaults={
            "talent_user_id": talent_dict.get("userId", ""),
            "job_id": job_dict["jobId"],
            "talent_data": talent_dict,
            "job_data": job_dict,
            "status": ApplicationScore.Status.PENDING,
        },
    )

    talent = _talent_profile_from_dict(talent_dict)
    job = _job_data_from_dict(job_dict)

    if not talent.user_id or not talent.has_data or not job.has_data:
        app_score.status = ApplicationScore.Status.FAILED
        app_score.save(update_fields=["status"])
        logger.info(
            "webhook_application: application %s stored but not scored (insufficient data)",
            str(application_id)[:8],
        )
        return JsonResponse({
            "applicationId": str(application_id),
            "status": app_score.status,
            "score": None,
            "reason": "insufficient data to score (missing talent profile or job data)",
        }, status=200)

    try:
        result = _compute_and_store_score(talent, job)
    except Exception as exc:
        logger.exception("Failed to score application %s: %s", str(application_id)[:8], exc)
        app_score.status = ApplicationScore.Status.FAILED
        app_score.save(update_fields=["status"])
        return JsonResponse({
            "applicationId": str(application_id),
            "status": app_score.status,
            "score": None,
            "error": "scoring failed",
        }, status=200)

    app_score.score = result.score
    app_score.matched = result.matched
    app_score.missing = result.missing
    app_score.explanation = result.explanation
    app_score.status = ApplicationScore.Status.SCORED
    app_score.scored_at = timezone.now()
    app_score.save(update_fields=["score", "matched", "missing", "explanation", "status", "scored_at"])

    logger.info(
        "webhook_application: scored application %s -> %.1f (created=%s)",
        str(application_id)[:8], result.score, created,
    )

    return JsonResponse({
        "applicationId": str(application_id),
        "status": app_score.status,
        "score": result.score,
        "matched": result.matched,
        "missing": result.missing,
        "explanation": result.explanation,
        "cached": False,
    })


# ══════════════════════════════════════════════════════════════════════
#  RECRUITER-INITIATED — Score a batch of stored applications now
# ══════════════════════════════════════════════════════════════════════

@extend_schema(
    tags=["Webhooks"],
    summary="Score stored applications now (recruiter 'Run AI matching')",
    description=(
        "Called by .NET when a recruiter hits 'Run AI matching' on a job's "
        "applicants. Each application is scored (0-100) from its apply-time "
        "profile snapshot vs the job, and the score is stored on "
        "ApplicationScore so the page can show it. The job embedding is "
        "computed once and shared by every application of that job. Requires "
        "X-Api-Key."
    ),
    responses={200: OpenApiResponse(description="Applications scored")},
)
@csrf_exempt
@require_POST
def applications_rescore(request):
    """Score a batch of applications immediately (recruiter-side refresh)."""
    body = _parse_json_body(request)
    raw_entries = body.get("applications", []) or []

    if not raw_entries:
        return JsonResponse({"error": "applications is required"}, status=400)

    from types import SimpleNamespace as _NS
    from uuid import UUID
    from django.db import transaction

    plan = []
    failed = []
    for raw in raw_entries[:200]:
        if not isinstance(raw, dict):
            failed.append({"applicationId": None, "reason": "malformed entry"})
            continue
        app_id = raw.get("applicationId")
        try:
            app_uuid = UUID(str(app_id))
        except (ValueError, TypeError):
            failed.append({
                "applicationId": str(app_id)[:8] if app_id else None,
                "reason": "invalid application id",
            })
            continue
        talent_dict = raw.get("talentProfile")
        job_dict = raw.get("job")
        if not isinstance(talent_dict, dict) or not talent_dict.get("userId"):
            failed.append({"applicationId": str(app_uuid)[:8], "reason": "no profile snapshot in application"})
            continue
        job = _job_data_from_dict(job_dict or {})
        if not job_dict or not job_dict.get("jobId") or not job.has_data:
            failed.append({"applicationId": str(app_uuid)[:8], "reason": "job has no data"})
            continue
        talent = _talent_profile_from_dict(talent_dict)
        if not talent.has_data:
            failed.append({
                "applicationId": str(app_uuid)[:8],
                "reason": "no profile data in snapshot (applicant did not share a full profile)",
            })
            continue
        plan.append({
            "app_uuid": app_uuid,
            "talent": talent,
            "job": job,
            "talent_dict": talent_dict,
            "job_dict": job_dict,
        })

    if not plan:
        return JsonResponse({
            "status": "ok", "scored": 0, "cached": 0,
            "failed": failed, "total": len(raw_entries),
        }, status=200)

    # The job embedding is shared by every application of the same job —
    # computed once here (or reused from JobVector) for the whole batch.
    job_vectors = {}
    for p in plan:
        job_id = p["job"].job_id
        if job_id not in job_vectors:
            embedding, job_hash = _job_embedding_and_hash(p["job"])
            job_vectors[job_id] = _NS(embedding=embedding, job_data_hash=job_hash)

    scored = 0
    cached = 0
    with transaction.atomic():
        for p in plan:
            try:
                result = _compute_and_store_score(
                    p["talent"], p["job"],
                    job_vector_obj=job_vectors[p["job"].job_id],
                )
                if result.cached:
                    cached += 1
                else:
                    scored += 1

                app_row, _ = ApplicationScore.objects.update_or_create(
                    application_id=p["app_uuid"],
                    defaults={
                        "talent_user_id": p["talent"].user_id,
                        "job_id": p["job"].job_id,
                        "talent_data": p["talent_dict"],
                        "job_data": p["job_dict"],
                        "status": ApplicationScore.Status.PENDING,
                    },
                )
                app_row.score = result.score
                app_row.matched = result.matched
                app_row.missing = result.missing
                app_row.explanation = result.explanation
                app_row.status = ApplicationScore.Status.SCORED
                app_row.scored_at = timezone.now()
                app_row.save(update_fields=[
                    "score", "matched", "missing", "explanation",
                    "status", "scored_at", "talent_data", "job_data",
                ])
            except Exception as exc:
                logger.warning(
                    "Failed to score application %s: %s",
                    str(p["app_uuid"])[:8], exc,
                )
                failed.append({"applicationId": str(p["app_uuid"])[:8], "reason": "scoring failed"})

    logger.info(
        "applications_rescore: scored %d / reused %d / failed %d of %d applications",
        scored, cached, len(failed), len(raw_entries),
    )
    return JsonResponse({
        "status": "ok",
        "scored": scored,
        "cached": cached,
        "failed": failed,
        "total": len(raw_entries),
    })


# ══════════════════════════════════════════════════════════════════════
#  READ: Batch talent-job scores (For You feed annotation)
# ══════════════════════════════════════════════════════════════════════

@extend_schema(
    tags=["Scores"],
    summary="Batch-fetch talent-job scores",
    description=(
        "Returns the stored 0-100 scores for one talent against a list of "
        "job ids. Used by the .NET For You feed to annotate a page of jobs "
        "in a single call."
    ),
    responses={200: OpenApiResponse(description="Map of jobId -> score payload")},
)
@csrf_exempt
@require_POST
def scores_batch(request):
    """Batch-fetch stored scores for one talent vs many jobs (For You feed)."""
    body = _parse_json_body(request)
    user_id = body.get("userId")
    raw_job_ids = body.get("jobIds", []) or []

    if not user_id:
        return JsonResponse({"error": "userId is required"}, status=400)

    valid_ids = []
    for raw in raw_job_ids[:200]:
        try:
            from uuid import UUID
            valid_ids.append(UUID(str(raw)))
        except (ValueError, TypeError):
            continue

    scores = {}
    if valid_ids:
        rows = TalentJobScore.objects.filter(
            talent_user_id=user_id,
            job_id__in=valid_ids,
        )
        for s in rows:
            scores[str(s.job_id)] = {
                "score": s.score,
                "matched": s.matched,
                "missing": s.missing,
                "explanation": s.explanation,
            }

    return JsonResponse({"userId": user_id, "count": len(scores), "scores": scores})


# ══════════════════════════════════════════════════════════════════════
#  READ: Batch application scores (recruiter applications page)
# ══════════════════════════════════════════════════════════════════════

@extend_schema(
    tags=["Scores"],
    summary="Batch-fetch application scores",
    description=(
        "Returns stored 0-100 scores for a list of application ids. Used by "
        "the .NET recruiter applications page to annotate its list in a "
        "single call."
    ),
    responses={200: OpenApiResponse(description="Map of applicationId -> score payload")},
)
@csrf_exempt
@require_POST
def application_scores_batch(request):
    """Batch-fetch stored application scores (recruiter applications page)."""
    body = _parse_json_body(request)
    raw_ids = body.get("applicationIds", []) or []

    valid_ids = []
    for raw in raw_ids[:200]:
        try:
            from uuid import UUID
            valid_ids.append(UUID(str(raw)))
        except (ValueError, TypeError):
            continue

    scores = {}
    if valid_ids:
        rows = ApplicationScore.objects.filter(application_id__in=valid_ids)
        for s in rows:
            scores[str(s.application_id)] = {
                "status": s.status,
                "score": s.score,
                "matched": s.matched,
                "missing": s.missing,
                "explanation": s.explanation,
            }

    return JsonResponse({"count": len(scores), "scores": scores})


# ══════════════════════════════════════════════════════════════════════
#  RESUME: PDF → profile fields (called by the .NET backend)
# ══════════════════════════════════════════════════════════════════════

@extend_schema(
    tags=["Resume"],
    summary="Extract profile fields from a resume PDF",
    description=(
        "Reads a text-based resume PDF and returns whatever profile fields "
        "it can find. The .NET backend calls this with a short-lived "
        "presigned R2 URL for the talent's private resume. Requires "
        "X-Api-Key.\n\n"
        "EVERY field is optional: anything the parser is unsure about comes "
        "back null so the user fills it in by hand. Nothing is invented. An "
        "image-only (scanned) PDF returns zero characters and all-null "
        "fields rather than an error, so the caller can show a plain "
        "\"we couldn't read your resume\" message."
    ),
    examples=[
        OpenApiExample(
            "Parse resume",
            value={"resumeUrl": "https://<account>.r2.cloudflarestorage.com/"
                                 "<bucket>/resumes/<userId>/20260916_120000_cv.pdf"
                                 "?X-Amz-Algorithm=AWS4-HMAC-SHA256&..."},
        )
    ],
    responses={200: OpenApiResponse(description="Extracted profile fields")},
)
@csrf_exempt
@require_POST
def parse_resume(request):
    """Extract profile fields from a resume PDF at a (presigned) URL.

    Parse-only and stateless: nothing is written. The talent reviews the
    fields and saves them through the normal profile endpoint.
    """
    from .services.resume_parser import parse_resume_url

    body = _parse_json_body(request)
    url = str(body.get("resumeUrl") or "").strip()
    if not url:
        return JsonResponse(
            {"success": False, "profile": None, "error": "resumeUrl is required"},
            status=400,
        )

    return JsonResponse(parse_resume_url(url))


# ══════════════════════════════════════════════════════════════════════
#  READ: Health check
# ══════════════════════════════════════════════════════════════════════

@extend_schema(
    tags=["Health"],
    summary="Service health check",
    description="Returns service status and stored vector/score counts.",
    responses={200: OpenApiResponse(description="Health status")},
)
@require_GET
def health(request):
    """Health check endpoint."""
    return JsonResponse({
        "status": "ok",
        "service": "matching_engine",
        "scoring_version": SCORING_VERSION,
        "talent_vectors": TalentProfileVector.objects.count(),
        "job_vectors": JobVector.objects.count(),
        "for_you_scores": TalentJobScore.objects.count(),
        "application_scores": ApplicationScore.objects.count(),
    })
