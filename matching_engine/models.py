from django.db import models


class TalentProfileVector(models.Model):
    """Stores the embedding vector and keywords for a talent's profile.

    Links to the .NET backend's TalentProfile via talent_user_id (the shared
    ASP.NET Identity user GUID). The embedding is a 384-dim vector from
    sentence-transformers (all-MiniLM-L6-v2).
    """

    talent_user_id = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="The ASP.NET Identity user GUID from the .NET backend.",
    )
    embedding = models.JSONField(
        help_text="384-dim vector from sentence-transformers.",
    )
    keywords = models.JSONField(
        default=list,
        help_text="Extracted keywords (words > 3 chars) from the profile text.",
    )
    profile_hash = models.CharField(
        max_length=64,
        db_index=True,
        help_text="SHA-256 of the profile text. Used to detect changes.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Vector for talent {self.talent_user_id[:8]}\u2026"


class JobVector(models.Model):
    """Stores the embedding vector and keywords for a job posting."""

    job_id = models.UUIDField(
        unique=True,
        db_index=True,
        help_text="The Job.Id UUID from the .NET backend.",
    )
    embedding = models.JSONField(
        help_text="384-dim vector from sentence-transformers.",
    )
    keywords = models.JSONField(
        default=list,
        help_text="Extracted keywords (words > 3 chars) from the job text.",
    )
    job_data_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="SHA-256 of the job data. Used to detect edits.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Vector for job {self.job_id}"


# Current scoring algorithm version. Bump when weights or model change
# to auto-invalidate all cached scores.
SCORING_VERSION = "1.0.0"


class TalentJobScore(models.Model):
    """Pre-computed compatibility score between a talent and a job.

    Staleness is detected by comparing the hashes stored at scoring time
    against the current hashes in TalentProfileVector and JobVector.
    """

    talent_user_id = models.CharField(max_length=64, db_index=True)
    job_id = models.UUIDField(db_index=True)
    score = models.FloatField(help_text="Compatibility score 0-100.")
    matched = models.JSONField(default=list)
    missing = models.JSONField(default=list)
    explanation = models.JSONField(default=dict)

    # ── Staleness tracking ──
    talent_profile_hash = models.CharField(
        max_length=64,
        help_text="Hash of talent profile at scoring time.",
    )
    job_data_hash = models.CharField(
        max_length=64,
        help_text="Hash of job data at scoring time.",
    )
    scoring_version = models.CharField(
        max_length=32,
        default=SCORING_VERSION,
        help_text="Algorithm version used to compute this score.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("talent_user_id", "job_id")
        ordering = ["-score"]

    def __str__(self):
        return f"Score {self.score:.1f} \u2014 {self.talent_user_id[:8]}\u2026 \u2194 {self.job_id}"


class UserActivity(models.Model):
    """Tracks when users were last active for smart scoring prioritization.

    Updated by the .NET backend via webhook on login and periodically.
    Only active users (last 24h) get pre-computed scores.
    """

    user_id = models.CharField(max_length=64, unique=True, db_index=True)
    last_active = models.DateTimeField(db_index=True)
    is_online = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["-last_active"]),
        ]

    def __str__(self):
        status = "online" if self.is_online else "offline"
        return f"{self.user_id[:8]}\u2026 — {status} @ {self.last_active}"


class ApplicationScore(models.Model):
    """Deferred compatibility score for an application.

    When a talent applies, we store the data here instead of scoring immediately.
    Scoring is triggered by:
      1. Nightly batch (low priority)
      2. Recruiter clicking "Match with AI" on-demand
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending scoring"
        SCORED = "scored", "Scored"
        FAILED = "failed", "Failed"

    application_id = models.UUIDField(
        unique=True,
        db_index=True,
        help_text="The JobApplication.Id UUID from the .NET backend.",
    )
    talent_user_id = models.CharField(
        max_length=64,
        db_index=True,
        help_text="The talent's user GUID.",
    )
    job_id = models.UUIDField(
        db_index=True,
        help_text="The Job.Id UUID from the .NET backend.",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )

    # Raw data stored at apply-time for later scoring
    talent_data = models.JSONField(
        help_text="Full talent profile snapshot at time of application.",
    )
    job_data = models.JSONField(
        help_text="Full job data snapshot at time of application.",
    )

    # Scored results (populated when status=scored)
    score = models.FloatField(
        null=True,
        blank=True,
        help_text="Compatibility score 0-100.",
    )
    matched = models.JSONField(default=list, blank=True)
    missing = models.JSONField(default=list, blank=True)
    explanation = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    scored_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"AppScore {self.application_id} — {self.status} ({self.score or 'N/A'})"


class SweepState(models.Model):
    """Tracks the last periodic sweep time for the scoring pipeline."""

    id = models.PositiveIntegerField(primary_key=True, default=1)
    last_sweep_at = models.DateTimeField(auto_now_add=True)
    jobs_scored = models.PositiveIntegerField(default=0)
    users_processed = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"Last sweep: {self.last_sweep_at} — {self.jobs_scored} scores"
