"""
Matching engine views — webhook endpoints, on-demand matching, and read endpoints.

Webhook endpoints (called by .NET):
  POST /api/matching/webhook/job-published     — score platform job against talents
  POST /api/matching/webhook/scraped-job       — score scraped job against active talents
  POST /api/matching/webhook/application       — score an application
  POST /api/matching/webhook/talent-updated    — invalidate scores on profile update
  POST /api/matching/webhook/talent-login      — trigger background scoring on login

On-demand endpoints:
  POST /api/matching/on-demand-match           — user clicks "AI Match" button

Read endpoints:
  GET  /api/matching/for-you/{user_id}         — ranked jobs with staleness check
  GET  /api/matching/talent-score/{uid}/{jid}  — specific talent-job score
  GET  /api/matching/health                    — health check
"""

import json
import logging
import time

from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from drf_spectacular.utils import extend_schema, OpenApiExample, OpenApiResponse

from .models import (
    JobVector,
    SCORING_VERSION,
    TalentJobScore,
    TalentProfileVector,
    UserActivity,
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


def _is_score_fresh(score: TalentJobScore, current_talent_hash: str, current_job_hash: str) -> bool:
    """Check if a score is still fresh (not stale)."""
    if score.scoring_version != SCORING_VERSION:
        return False
    if score.talent_profile_hash != current_talent_hash:
        return False
    if score.job_data_hash != current_job_hash:
        return False
    return True


# ══════════════════════════════════════════════════════════════════════
#  WEBHOOK: Job Published (platform jobs)
# ══════════════════════════════════════════════════════════════════════

@extend_schema(
    tags=["Webhooks"],
    summary="Score platform job against eligible talents",
    description=(
        "Called by .NET when admin approves a job. Scores the job against "
        "all talents whose currentIndustry matches the job's sector. "
        "Requires X-Api-Key header."
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
    """Score a platform job against all eligible talents."""
    body = _parse_json_body(request)
    job_dict = body.get("job")
    talents_list = body.get("eligibleTalents", [])

    if not job_dict or not job_dict.get("jobId"):
        return JsonResponse({"error": "job.jobId is required"}, status=400)

    job = _job_data_from_dict(job_dict)
    if not job.has_data:
        return JsonResponse({"status": "skipped", "reason": "job has no data"}, status=200)

    if not talents_list:
        return JsonResponse({"status": "ok", "scored": 0}, status=200)

    talents = [_talent_profile_from_dict(t) for t in talents_list]
    talents = [t for t in talents if t.has_data and t.user_id]

    if not talents:
        return JsonResponse({"status": "ok", "scored": 0}, status=200)

    from .services.scoring import encode_text, text_hash, build_job_text

    job_text = build_job_text(job)
    job_embedding = encode_text(job_text)
    job_hash = text_hash(job_text)

    scored = 0
    for talent in talents:
        try:
            from .tasks import _compute_and_store_score
            _compute_and_store_score(talent, job)
            scored += 1
        except Exception as exc:
            logger.warning("Failed to score talent %s: %s", talent.user_id[:8], exc)

    # Store job vector
    JobVector.objects.update_or_create(
        job_id=job.job_id,
        defaults={
            "embedding": job_embedding,
            "keywords": [],
            "job_data_hash": job_hash,
        },
    )

    logger.info("webhook_job_published: scored job %s against %d talents", str(job.job_id)[:8], scored)
    return JsonResponse({"status": "ok", "scored": scored})


# ══════════════════════════════════════════════════════════════════════
#  WEBHOOK: Scraped Job (hot sector pre-compute)
# ══════════════════════════════════════════════════════════════════════

@extend_schema(
    tags=["Webhooks"],
    summary="Score scraped job against active talents",
    description="Enqueues async scoring for hot-sector scraped jobs. Requires X-Api-Key.",
    responses={200: OpenApiResponse(description="Task queued")},
)
@csrf_exempt
@require_POST
def webhook_scraped_job(request):
    """Score a scraped job against active talents in the same sector.

    Called by .NET when a hot-sector scraped job is inserted.
    Enqueues a Celery task for async processing.
    """
    body = _parse_json_body(request)
    job_dict = body.get("job")
    active_talents = body.get("activeTalents", [])

    if not job_dict or not job_dict.get("jobId"):
        return JsonResponse({"error": "job.jobId is required"}, status=400)

    if not active_talents:
        return JsonResponse({"status": "ok", "scored": 0, "reason": "no active talents"}, status=200)

    # Enqueue Celery task for async scoring
    from .tasks import score_job_for_active_users
    task = score_job_for_active_users.delay(job_dict, active_talents)

    return JsonResponse({
        "status": "queued",
        "taskId": task.id,
        "talentCount": len(active_talents),
    })


# ══════════════════════════════════════════════════════════════════════
#  WEBHOOK: Application Created
# ══════════════════════════════════════════════════════════════════════

@extend_schema(
    tags=["Webhooks"],
    summary="Store application for deferred scoring",
    description=(
        "Stores the application data for later scoring (nightly batch or "
        "recruiter on-demand). Does NOT score immediately. Requires X-Api-Key."
    ),
    responses={200: OpenApiResponse(description="Application queued")},
)
@csrf_exempt
@require_POST
def webhook_application(request):
    """Store a new application for deferred scoring.

    We don't score immediately because the recruiter doesn't need it right away.
    Scoring happens:
      1. In the nightly batch (low priority)
      2. When the recruiter clicks "Match with AI" (on-demand)

    This keeps the application endpoint fast and doesn't compete with
    talent-facing scoring (which has higher priority).
    """
    from .models import ApplicationScore
    from .services.scoring import text_hash, build_talent_text, build_job_text

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

    # Store for later scoring (fast — no ML computation here)
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

    logger.info(
        "webhook_application: stored application %s for deferred scoring (created=%s)",
        str(application_id)[:8], created,
    )

    return JsonResponse({
        "applicationId": str(application_id),
        "status": "queued",
        "message": "Application queued for scoring. Score will be available shortly.",
    })


# ══════════════════════════════════════════════════════════════════════
#  WEBHOOK: Talent Profile Updated
# ══════════════════════════════════════════════════════════════════════

# Fields that affect scoring when changed.
# We only recompute if these fields differ between old and new profiles.
_SCORING_FIELDS = frozenset({
    "city",
    "country",
    # Professional profile (except about)
    "experienceLevel",
    "yearsOfExperience",
    "currentIndustry",
    "currentProfession",
    "workMode",
    # Skills only (not desiredRoles, desiredJobTypes)
    "skills",
    # Lists — compared by length/contents
    "workExperience",
    "educationHistory",
})


def _has_scoring_fields_changed(old_data: dict, new_data: dict) -> bool:
    """Check if any scoring-relevant fields changed between old and new profile.

    Only these changes trigger score invalidation:
      - City, Country
      - Professional profile (experienceLevel, yearsOfExperience, currentIndustry,
        currentProfession, workMode) — but NOT about/desiredRoles/desiredJobTypes
      - Skills (the skills list itself)
      - Work experience (list contents)
      - Education history (list contents)
    """
    for field in _SCORING_FIELDS:
        old_val = old_data.get(field)
        new_val = new_data.get(field)

        # For list fields, compare as strings (JSON)
        if isinstance(old_val, list) and isinstance(new_val, list):
            if json.dumps(old_val, sort_keys=True) != json.dumps(new_val, sort_keys=True):
                return True
        elif old_val != new_val:
            return True

    return False


@extend_schema(
    tags=["Webhooks"],
    summary="Invalidate scores on profile update",
    description=(
        "Called by .NET when a talent updates their profile. Only recomputes "
        "hash if scoring-relevant fields changed (city, country, skills, etc). "
        "Requires X-Api-Key."
    ),
    responses={200: OpenApiResponse(description="Scores invalidated")},
)
@csrf_exempt
@require_POST
def webhook_talent_updated(request):
    """Invalidate scores when a talent updates their profile.

    Only recomputes the profile hash if scoring-relevant fields changed:
      - City, Country
      - Professional profile (except about)
      - Skills (not desired roles/types)
      - Work experience list
      - Education history list

    If only non-scoring fields changed (about, headline, desired roles, etc.),
    we skip the hash update entirely — no scores become stale.
    """
    body = _parse_json_body(request)
    talent_dict = body.get("talentProfile")
    previous_data = body.get("previousProfile")  # optional: old profile snapshot

    if not talent_dict or not talent_dict.get("userId"):
        return JsonResponse({"error": "talentProfile.userId is required"}, status=400)

    user_id = talent_dict["userId"]

    # Check if scoring-relevant fields actually changed
    if previous_data:
        changed = _has_scoring_fields_changed(previous_data, talent_dict)
        if not changed:
            return JsonResponse({
                "status": "unchanged",
                "reason": "only non-scoring fields changed (headline, about, desired roles, etc.)",
            }, status=200)

    from .services.scoring import encode_text, text_hash, build_talent_text

    talent = _talent_profile_from_dict(talent_dict)
    if not talent.has_data:
        return JsonResponse({"status": "skipped", "reason": "no profile data"}, status=200)

    talent_text = build_talent_text(talent)
    talent_embedding = encode_text(talent_text)
    new_hash = text_hash(talent_text)

    # Check if hash actually changed
    existing = TalentProfileVector.objects.filter(talent_user_id=user_id).first()
    if existing and existing.profile_hash == new_hash:
        return JsonResponse({"status": "unchanged"}, status=200)

    TalentProfileVector.objects.update_or_create(
        talent_user_id=user_id,
        defaults={
            "embedding": talent_embedding,
            "keywords": talent.skills + talent.desired_roles,
            "profile_hash": new_hash,
        },
    )

    # Don't delete scores — staleness detection will handle it on read.
    stale_count = TalentJobScore.objects.filter(
        talent_user_id=user_id,
    ).exclude(
        talent_profile_hash=new_hash,
    ).count()

    logger.info("Talent %s updated: %d scores now stale", user_id[:8], stale_count)

    return JsonResponse({
        "status": "ok",
        "stale_scores": stale_count,
    })


# ══════════════════════════════════════════════════════════════════════
#  WEBHOOK: Talent Login (activity signal)
# ══════════════════════════════════════════════════════════════════════

@extend_schema(
    tags=["Webhooks"],
    summary="Trigger background scoring on talent login",
    description=(
        "Called by .NET when a talent signs in. Updates activity status and "
        "enqueues background scoring for un-scored jobs. Requires X-Api-Key."
    ),
    responses={200: OpenApiResponse(description="Activity updated, scoring enqueued")},
)
@csrf_exempt
@require_POST
def webhook_talent_login(request):
    """Called by .NET when a talent logs in. Updates activity + triggers background scoring."""
    body = _parse_json_body(request)
    user_id = body.get("userId")
    talent_data = body.get("talentProfile")
    sector_ids = body.get("sectorIds", [])

    if not user_id:
        return JsonResponse({"error": "userId is required"}, status=400)

    now = timezone.now()

    # Update activity
    UserActivity.objects.update_or_create(
        user_id=user_id,
        defaults={
            "last_active": now,
            "is_online": True,
        },
    )

    # Find un-scored jobs for this user's sectors
    scored_job_ids = TalentJobScore.objects.filter(
        talent_user_id=user_id,
    ).values_list("job_id", flat=True)

    unscored_jobs = JobVector.objects.exclude(job_id__in=scored_job_ids)

    if unscored_jobs.exists() and talent_data:
        # Enqueue background scoring
        from .tasks import score_for_user
        score_for_user.delay(
            user_id=user_id,
            talent_data=talent_data,
            job_ids=[str(jid) for jid in unscored_jobs.values_list("job_id", flat=True)[:100]],
        )

    return JsonResponse({
        "status": "ok",
        "unscoredJobs": unscored_jobs.count(),
    })


# ══════════════════════════════════════════════════════════════════════
#  ON-DEMAND: User clicks "AI Match"
# ══════════════════════════════════════════════════════════════════════

@extend_schema(
    tags=["Matching"],
    summary="On-demand match: compute scores for uncached jobs",
    description=(
        "Called when talent clicks 'AI Match' button. Returns cached scores "
        "for fresh jobs, recomputes stale/missing scores inline. Returns "
        "results sorted by score descending."
    ),
    responses={200: OpenApiResponse(description="Scored jobs list")},
)
@csrf_exempt
@require_POST
def on_demand_match(request):
    """User clicks "AI Match" — compute scores for uncached jobs."""
    start = time.time()
    body = _parse_json_body(request)
    user_id = body.get("userId")
    talent_data = body.get("talentProfile")

    if not user_id:
        return JsonResponse({"error": "userId is required"}, status=400)

    if not talent_data:
        return JsonResponse({"error": "talentProfile is required"}, status=400)

    talent = _talent_profile_from_dict(talent_data)
    if not talent.has_data:
        return JsonResponse({"error": "talent has no profile data"}, status=400)

    # Find all job vectors
    all_job_vectors = JobVector.objects.all()

    if not all_job_vectors.exists():
        return JsonResponse({"status": "ok", "scored": 0, "jobs": []}, status=200)

    # Check which scores exist and are fresh
    from .services.scoring import text_hash, build_talent_text

    talent_text = build_talent_text(talent)
    current_talent_hash = text_hash(talent_text)

    existing_scores = {
        s.job_id: s
        for s in TalentJobScore.objects.filter(talent_user_id=user_id)
    }

    scored_results = []
    stale_count = 0
    fresh_count = 0

    for job_vector in all_job_vectors:
        existing = existing_scores.get(job_vector.job_id)

        if existing:
            # Check staleness
            if _is_score_fresh(existing, current_talent_hash, job_vector.job_data_hash):
                # Fresh — use cached
                scored_results.append({
                    "jobId": str(existing.job_id),
                    "score": existing.score,
                    "matched": existing.matched,
                    "missing": existing.missing,
                    "cached": True,
                })
                fresh_count += 1
                continue
            else:
                stale_count += 1

        # Score this job (we only have vectors, so we do embedding similarity)
        # For full scoring with heuristics, we need the job data from .NET
        # This is a simplified scoring path using stored vectors
        from .services.scoring import cosine_similarity
        if cosine_similarity is None:
            continue

        talent_emb = None
        # Get talent embedding from stored vector or compute
        talent_vec = TalentProfileVector.objects.filter(talent_user_id=user_id).first()
        if talent_vec:
            talent_emb = talent_vec.embedding

        if talent_emb and job_vector.embedding:
            sim = float(cosine_similarity([talent_emb], [job_vector.embedding])[0][0])
            sim_normalized = (sim + 1) / 2  # map to [0, 1]
            score = round(sim_normalized * 100, 1)

            TalentJobScore.objects.update_or_create(
                talent_user_id=user_id,
                job_id=job_vector.job_id,
                defaults={
                    "score": score,
                    "matched": [],
                    "missing": [],
                    "explanation": {"embedding_similarity": round(sim_normalized, 4)},
                    "talent_profile_hash": current_talent_hash,
                    "job_data_hash": job_vector.job_data_hash,
                    "scoring_version": SCORING_VERSION,
                },
            )

            scored_results.append({
                "jobId": str(job_vector.job_id),
                "score": score,
                "matched": [],
                "missing": [],
                "cached": False,
            })

    # Sort by score descending
    scored_results.sort(key=lambda x: x["score"], reverse=True)

    elapsed = time.time() - start
    logger.info(
        "on_demand_match %s: %d scored, %d fresh, %d stale, %.1fs",
        user_id[:8], len(scored_results), fresh_count, stale_count, elapsed,
    )

    return JsonResponse({
        "status": "ok",
        "scored": len(scored_results),
        "fresh": fresh_count,
        "recomputed": stale_count + len(scored_results) - fresh_count,
        "elapsed_seconds": round(elapsed, 1),
        "jobs": scored_results,
    })


# ══════════════════════════════════════════════════════════════════════
#  READ: For You (with staleness detection)
# ══════════════════════════════════════════════════════════════════════

@extend_schema(
    tags=["Scores"],
    summary="Get ranked job recommendations",
    description=(
        "Returns top 50 scored jobs for a talent, sorted by score descending. "
        "Each job includes a 'fresh' flag indicating if the score is still valid."
    ),
    responses={200: OpenApiResponse(description="Ranked jobs with staleness flags")},
)
@require_GET
def for_you(request, user_id):
    """Get ranked job recommendations with staleness detection."""
    # Get current talent hash for staleness check
    talent_vec = TalentProfileVector.objects.filter(talent_user_id=user_id).first()
    current_talent_hash = talent_vec.profile_hash if talent_vec else ""

    scores = TalentJobScore.objects.filter(
        talent_user_id=user_id,
    ).select_related().order_by("-score")[:50]

    results = []
    stale_count = 0

    for s in scores:
        # Get current job hash
        job_vec = JobVector.objects.filter(job_id=s.job_id).first()
        current_job_hash = job_vec.job_data_hash if job_vec else s.job_data_hash

        is_fresh = _is_score_fresh(s, current_talent_hash, current_job_hash)

        if not is_fresh:
            stale_count += 1

        results.append({
            "jobId": str(s.job_id),
            "score": s.score,
            "matched": s.matched,
            "missing": s.missing,
            "explanation": s.explanation,
            "fresh": is_fresh,
        })

    return JsonResponse({
        "userId": user_id,
        "count": len(results),
        "staleCount": stale_count,
        "jobs": results,
    })


# ══════════════════════════════════════════════════════════════════════
#  READ: Talent-Job score
# ══════════════════════════════════════════════════════════════════════

@extend_schema(
    tags=["Scores"],
    summary="Get score for a specific talent-job pair",
    responses={200: OpenApiResponse(description="Score details")},
)
@require_GET
def talent_job_score(request, user_id, job_id):
    """Get the stored score for a specific talent-job pair."""
    try:
        score = TalentJobScore.objects.get(
            talent_user_id=user_id,
            job_id=job_id,
        )
    except TalentJobScore.DoesNotExist:
        return JsonResponse({"error": "Score not found"}, status=404)

    return JsonResponse({
        "talentUserId": score.talent_user_id,
        "jobId": str(score.job_id),
        "score": score.score,
        "matched": score.matched,
        "missing": score.missing,
        "explanation": score.explanation,
        "scoringVersion": score.scoring_version,
    })


# ══════════════════════════════════════════════════════════════════════
#  ON-DEMAND: Recruiter clicks "Match with AI" on an application
# ══════════════════════════════════════════════════════════════════════

@extend_schema(
    tags=["Matching"],
    summary="Score application on-demand (recruiter)",
    description=(
        "Called when recruiter clicks 'Match with AI' on an application. "
        "Returns cached score if already computed, otherwise scores now."
    ),
    responses={200: OpenApiResponse(description="Application score")},
)
@csrf_exempt
@require_POST
def score_application(request):
    """Score a specific application on-demand when the recruiter clicks "Match with AI".

    Scores one application and returns the result. If the application was
    already scored and is still fresh, returns the cached score.
    """
    from .models import ApplicationScore
    from .services.scoring import (
        encode_text,
        text_hash,
        build_talent_text,
        build_job_text,
    )

    body = _parse_json_body(request)
    application_id = body.get("applicationId")

    if not application_id:
        return JsonResponse({"error": "applicationId is required"}, status=400)

    try:
        app_score = ApplicationScore.objects.get(application_id=application_id)
    except ApplicationScore.DoesNotExist:
        return JsonResponse({"error": "Application not found"}, status=404)

    # Already scored and fresh?
    if app_score.status == ApplicationScore.Status.SCORED and app_score.score is not None:
        return JsonResponse({
            "applicationId": str(application_id),
            "score": app_score.score,
            "matched": app_score.matched,
            "missing": app_score.missing,
            "explanation": app_score.explanation,
            "cached": True,
        })

    # Score it now
    talent = _talent_profile_from_dict(app_score.talent_data)
    job = _job_data_from_dict(app_score.job_data)

    if not talent.has_data or not job.has_data:
        return JsonResponse({
            "applicationId": str(application_id),
            "score": None,
            "reason": "insufficient data",
        }, status=200)

    from .tasks import _compute_and_store_score
    result = _compute_and_store_score(talent, job)

    # Update the ApplicationScore record
    app_score.score = result.score
    app_score.matched = result.matched
    app_score.missing = result.missing
    app_score.explanation = result.explanation
    app_score.status = ApplicationScore.Status.SCORED
    app_score.scored_at = timezone.now()
    app_score.save(update_fields=["score", "matched", "missing", "explanation", "status", "scored_at"])

    return JsonResponse({
        "applicationId": str(application_id),
        "score": result.score,
        "matched": result.matched,
        "missing": result.missing,
        "explanation": result.explanation,
        "cached": False,
    })


# ══════════════════════════════════════════════════════════════════════
#  READ: Application score (for recruiter to fetch)
# ══════════════════════════════════════════════════════════════════════

@extend_schema(
    tags=["Scores"],
    summary="Get score for a specific application",
    description="Returns the match score for an application (may be pending).",
    responses={200: OpenApiResponse(description="Application score info")},
)
@require_GET
def application_score(request, application_id):
    """Get the score for a specific application.

    Used by the recruiter's applications page to display match scores.
    Returns null score if not yet computed.
    """
    from .models import ApplicationScore

    try:
        app_score = ApplicationScore.objects.get(application_id=application_id)
    except ApplicationScore.DoesNotExist:
        return JsonResponse({"error": "Application not found"}, status=404)

    return JsonResponse({
        "applicationId": str(application_id),
        "status": app_score.status,
        "score": app_score.score,
        "matched": app_score.matched,
        "missing": app_score.missing,
        "explanation": app_score.explanation,
    })


# ══════════════════════════════════════════════════════════════════════
#  SECTOR CLASSIFICATION (called by scraper and .NET backend)
# ══════════════════════════════════════════════════════════════════════

@extend_schema(
    tags=["Sector Classification"],
    summary="Classify job into canonical sector",
    description=(
        "4-layer classification: alias match → keyword rules → "
        "embedding similarity → LLM fallback. Returns sector ID, name, "
        "confidence, and which layer solved it."
    ),
    examples=[
        OpenApiExample(
            "With raw sector",
            value={"title": "Senior Software Engineer", "rawSector": "Software Design & Development"},
        ),
        OpenApiExample(
            "Without raw sector",
            value={"title": "Registered Nurse"},
        ),
    ],
    responses={200: OpenApiResponse(description="Classification result")},
)
@csrf_exempt
@require_POST
def classify_sector(request):
    """Classify a job into a canonical sector using 4-layer classification.

    Called by:
      - SeraGo-Scraper when inserting scraped jobs
      - .NET backend when admin creates a platform job without sector

    Layers (stops at first confident match):
      1. Alias match — exact lookup against SectorAliases (free, <1ms)
      2. Keyword rules — hardcoded title rules (free, <1ms)
      3. Embedding similarity — sentence-transformers (free, ~15ms)
      4. LLM fallback — GPT-4o-mini for ambiguous cases (~$0.000015)

    Request body:
      { "title": "Senior Software Engineer",
        "rawSector": "Software Design & Development",
        "description": "We are looking for..." }

    Response:
      { "sectorId": "uuid",
        "sectorName": "Technology & IT",
        "sectorSlug": "technology-it",
        "confidence": 0.95,
        "method": "embedding" }
    """
    from .services.sector_classifier import get_sector_classifier

    body = _parse_json_body(request)
    title = body.get("title", "")
    raw_sector = body.get("rawSector", "")
    description = body.get("description", "")

    if not title and not raw_sector:
        return JsonResponse(
            {"error": "title or rawSector is required"}, status=400
        )

    classifier = get_sector_classifier()
    result = classifier.classify(
        title=title,
        raw_sector=raw_sector,
        description=description,
    )

    if result is None:
        return JsonResponse({
            "sectorId": None,
            "sectorName": None,
            "sectorSlug": None,
            "confidence": 0,
            "method": "none",
            "message": "Could not classify into any sector",
        }, status=200)

    return JsonResponse({
        "sectorId": result.sector_id,
        "sectorName": result.sector_name,
        "sectorSlug": result.sector_slug,
        "confidence": result.confidence,
        "method": result.method,
    })


@require_GET
def classify_sector_health(request):
    """Health check + sector classification stats."""
    from .services.sector_classifier import get_sector_classifier

    classifier = get_sector_classifier()
    sectors = classifier._get_sectors()

    return JsonResponse({
        "status": "ok",
        "canonical_sectors": len(sectors),
        "sector_names": [s["name"] for s in sectors],
        "embedding_model_loaded": classifier._embedding_model is not None,
    })


# ══════════════════════════════════════════════════════════════════════
#  READ: Health check
# ══════════════════════════════════════════════════════════════════════

@extend_schema(
    tags=["Health"],
    summary="Service health check",
    description="Returns service status, vector counts, and last sweep time.",
    responses={200: OpenApiResponse(description="Health status")},
)
@require_GET
def health(request):
    """Health check endpoint."""
    from .models import SweepState
    sweep = SweepState.objects.filter(id=1).first()

    return JsonResponse({
        "status": "ok",
        "service": "matching_engine",
        "scoring_version": SCORING_VERSION,
        "talent_vectors": TalentProfileVector.objects.count(),
        "job_vectors": JobVector.objects.count(),
        "scores": TalentJobScore.objects.count(),
        "active_users": UserActivity.objects.filter(
            last_active__gte=timezone.now() - timezone.timedelta(hours=24)
        ).count(),
        "last_sweep": sweep.last_sweep_at.isoformat() if sweep else None,
    })
